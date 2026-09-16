import asyncio
import hashlib
import math
import os
import re
import shutil
import statistics
import tempfile
from array import array
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx

from activity import logged_http_request
from audio import AudioStore
from offsets import OffsetStore
from security import resolves_publicly, valid_public_url


class SyncEngine:
    SYNC_ALGORITHM = "fastpass-vidfast-v3"
    VIDFAST_SAMPLE_RESOLUTIONS = (480, 720, 1080, 1440)
    SYNC_MAX_DEVIATION = 0.25
    SYNC_MAX_RATE_DELTA = 0.002
    SYNC_MAX_LINEAR_DEVIATION = 0.15
    SYNC_MAX_END_DEVIATION = 2.0

    def __init__(self, audio: AudioStore, offsets: OffsetStore, proxy: str = ""):
        self.audio = audio
        self.offsets = offsets
        self.proxy = proxy
        self.activity = getattr(audio, "activity", None)
        self.sample_seconds = 5

    async def _get(self, url: str, headers: dict):
        if not valid_public_url(url) or not await resolves_publicly(url):
            raise ValueError("media URL is not public HTTPS")
        kwargs = {"timeout": 30, "follow_redirects": False}
        if self.proxy:
            kwargs["proxy"] = self.proxy
        try:
            async with httpx.AsyncClient(**kwargs) as client:
                response = await logged_http_request(
                    self.activity, client, "GET", url, "sync-source", headers=headers
                )
        except httpx.HTTPError as exc:
            raise RuntimeError(f"media fetch failed: {exc}") from exc
        if response.status_code in (301, 302, 307, 308):
            location = response.headers.get("location", "")
            if not await resolves_publicly(urljoin(url, location)):
                raise ValueError("media redirect is not public HTTPS")
            return await self._get(urljoin(url, location), headers)
        response.raise_for_status()
        return response

    @staticmethod
    def _playlist(text: str, master_url: str):
        entries, pending, elapsed = [], None, 0.0
        map_url = None
        for raw in text.splitlines():
            line = raw.strip()
            if line.startswith("#EXT-X-MAP:"):
                match = re.search(r'URI="([^"]+)"', line)
                map_url = urljoin(master_url, match.group(1)) if match else None
            elif line.startswith("#EXTINF:"):
                pending = float(line.split(":", 1)[1].split(",", 1)[0])
            elif pending is not None and line and not line.startswith("#"):
                entries.append({"url": urljoin(master_url, line), "duration": pending, "start": elapsed})
                elapsed += pending
                pending = None
        if not entries:
            raise ValueError("empty media playlist")
        return entries, map_url

    async def _video_entries(self, url: str, headers: dict):
        response = await self._get(url, headers)
        return self._playlist(response.text, url)

    async def reference_audio_url(self, video_url: str, headers: dict) -> str | None:
        """Find the default audio rendition advertised by an HLS master playlist."""
        response = await self._get(video_url, headers)
        renditions = []
        stream_groups = []
        for line in response.text.splitlines():
            if line.startswith("#EXT-X-STREAM-INF:"):
                match = re.search(r'(?:^|[:,])AUDIO="([^"]+)"', line)
                if match:
                    stream_groups.append(match.group(1))
                continue
            if not line.startswith("#EXT-X-MEDIA:"):
                continue
            attributes = dict(re.findall(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)', line))
            if attributes.get("TYPE") != "AUDIO" or not attributes.get("URI"):
                continue
            uri = attributes["URI"].strip('"')
            candidate = urljoin(str(response.url), uri)
            if valid_public_url(candidate):
                renditions.append((
                    attributes.get("GROUP-ID", "").strip('"'),
                    attributes.get("DEFAULT") == "YES", candidate,
                ))
        if not renditions:
            return None
        if stream_groups:
            renditions = [item for item in renditions if item[0] == stream_groups[0]]
            if not renditions:
                return None
        return next((url for _, default, url in renditions if default), renditions[0][2])

    async def _vidfast_sample_url(self, url: str, headers: dict,
                                  duration: float, provider: str) -> str:
        """Use a lighter rendition (480p) for sync when its timeline matches."""
        match = re.search(r"index-s(\d+)p", url, re.IGNORECASE)
        if not match:
            return url
        current_resolution = int(match.group(1))
        for resolution in self.VIDFAST_SAMPLE_RESOLUTIONS:
            if resolution >= current_resolution:
                break
            candidate = re.sub(
                r"index-s\d+p", f"index-s{resolution}p", url,
                count=1, flags=re.IGNORECASE,
            )
            try:
                entries, _ = await self._video_entries(candidate, headers)
            except Exception as error:
                print(f"[sidecar sync] Vidfast {resolution}p unavailable: {type(error).__name__}")
                continue
            candidate_duration = sum(item["duration"] for item in entries)
            if abs(candidate_duration - duration) <= 1.0:
                print(f"[sidecar sync] Vidfast sync rendition downsampled to: {resolution}p")
                return candidate
        return url

    async def _download(self, url: str, path: Path, headers: dict):
        response = await self._get(url, headers)
        path.write_bytes(response.content)

    @staticmethod
    def _sample_entries(entries, position: float, sample_seconds: float = 5.0):
        target = next((i for i, item in enumerate(entries)
                       if item["start"] <= position < item["start"] + item["duration"]),
                      len(entries) - 1)
        first = max(0, target - 1)
        local_seek = max(0.0, position - entries[first]["start"])
        selected, available = [], 0.0
        for item in entries[first:]:
            selected.append(item)
            available += item["duration"]
            if available >= local_seek + sample_seconds + 5.0:
                break
        return selected, local_seek, sum(item["duration"] for item in entries)

    async def _decode_video(self, url: str, headers: dict, position: float, directory: Path, sample_seconds: float = 5.0):
        _, entries, map_url = (url, *await self._video_entries(url, headers))
        selected, local_seek, duration = self._sample_entries(entries, position, sample_seconds)
        lines = ["#EXTM3U", "#EXT-X-VERSION:7", "#EXT-X-PLAYLIST-TYPE:VOD", f"#EXT-X-TARGETDURATION:{int(max(x['duration'] for x in selected)) + 1}"]
        if map_url:
            await self._download(map_url, directory / "video-init.mp4", headers)
            lines.append('#EXT-X-MAP:URI="video-init.mp4"')
        for number, item in enumerate(selected):
            name = f"video-{number}.m4s"
            await self._download(item["url"], directory / name, headers)
            lines += [f"#EXTINF:{item['duration']:.6f},", name]
        lines.append("#EXT-X-ENDLIST")
        playlist = directory / "video.m3u8"
        playlist.write_text("\n".join(lines) + "\n")
        return playlist, local_seek, duration

    async def _decode_reference_audio(self, url: str, headers: dict, position: float, directory: Path, sample_seconds: float = 5.0):
        response = await self._get(url, headers)
        entries, map_url = self._playlist(response.text, url)
        selected, local_seek, duration = self._sample_entries(entries, position, sample_seconds)
        lines = ["#EXTM3U", "#EXT-X-VERSION:7", "#EXT-X-PLAYLIST-TYPE:VOD",
                 f"#EXT-X-TARGETDURATION:{int(max(item['duration'] for item in selected)) + 1}"]
        key_line = next((line.strip() for line in response.text.splitlines()
                         if line.strip().startswith("#EXT-X-KEY:")), "")
        if key_line and "METHOD=NONE" not in key_line.upper():
            key_match = re.search(r'URI="([^"]+)"', key_line)
            if key_match:
                key_response = await self._get(urljoin(url, key_match.group(1)), headers)
                (directory / "reference.key").write_bytes(key_response.content)
                key_line = re.sub(r'URI="[^"]+"', 'URI="reference.key"', key_line, count=1)
            lines.append(key_line)
        if map_url:
            await self._download(map_url, directory / "reference-init.mp4", headers)
            lines.append('#EXT-X-MAP:URI="reference-init.mp4"')
        for number, item in enumerate(selected):
            name = f"reference-{number}.m4s"
            await self._download(item["url"], directory / name, headers)
            lines += [f"#EXTINF:{item['duration']:.6f},", name]
        lines.append("#EXT-X-ENDLIST")
        playlist = directory / "reference.m3u8"
        playlist.write_text("\n".join(lines) + "\n")
        return playlist, local_seek, duration

    async def _media_start_time(self, url: str, headers: dict) -> float:
        """Read the initial timestamp of Cinejoy's video-only fMP4 stream."""
        response = await self._get(url, headers)
        match = re.search(r'#EXT-X-MAP:URI="([^"]+)"', response.text)
        first = next((line.strip() for line in response.text.splitlines()
                      if line.strip() and not line.startswith("#")), "")
        if not match or not first:
            return 0.0
        init_response, segment_response = await asyncio.gather(
            self._get(urljoin(url, match.group(1)), headers),
            self._get(urljoin(url, first), headers),
        )
        root = Path(tempfile.mkdtemp(prefix="cinejoy-start-"))
        try:
            sample = root / "sample.mp4"
            sample.write_bytes(init_response.content + segment_response.content)
            process = await asyncio.create_subprocess_exec(
                "ffprobe", "-v", "error", "-show_entries", "stream=start_time",
                "-of", "default=nw=1:nk=1", str(sample),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            output, _ = await asyncio.wait_for(process.communicate(), timeout=20)
            values = output.decode(errors="replace").strip().splitlines()
            return float(values[0]) if values else 0.0
        finally:
            shutil.rmtree(root, ignore_errors=True)

    async def _decode_audio(self, hid: str, position: float, directory: Path,
                            sample_seconds: float = 5.0, headers: dict | None = None):
        metadata = self.audio.metadata(hid)
        index = next((i for i, start in enumerate(metadata["starts"]) if start <= position < start + metadata["durs"][i]), len(metadata["segs"]) - 1)
        first = max(0, index - 1)
        local_seek = max(0.0, position - metadata["starts"][first])
        needed = 0.0
        last = first
        while last < len(metadata["segs"]) and needed < (local_seek + sample_seconds + 5.0):
            needed += metadata["durs"][last]
            last += 1
        selected = range(first, max(first + 1, last))
        iv = f",IV={metadata['iv']}" if metadata.get("iv") else ""
        lines = ["#EXTM3U", "#EXT-X-VERSION:3", "#EXT-X-PLAYLIST-TYPE:VOD",
                 f"#EXT-X-TARGETDURATION:{int(max(metadata['durs'][item] for item in selected)) + 1}",
                 f'#EXT-X-KEY:METHOD=AES-128,URI="audio.key"{iv}']
        (directory / "audio.key").write_bytes((self.audio._dir(hid) / "enc.key").read_bytes())
        for number, item in enumerate(selected):
            name = f"audio-{number}.ts"
            await self._download(metadata["segs"][item], directory / name,
                                 headers if headers is not None else metadata.get("headers") or {})
            lines += [f"#EXTINF:{metadata['durs'][item]:.6f},", name]
        lines.append("#EXT-X-ENDLIST")
        playlist = directory / "audio.m3u8"
        playlist.write_text("\n".join(lines) + "\n")
        return playlist, local_seek, sum(metadata["durs"])

    @staticmethod
    async def _pcm(playlist: Path, seek: float, output: Path, audio_map: bool = True, sample_seconds: float = 5.0):
        command = [
            "ffmpeg", "-v", "error", "-allowed_extensions", "ALL",
            "-protocol_whitelist", "file,crypto", "-i", str(playlist),
            "-ss", f"{max(0.0, seek):.3f}", "-t", f"{sample_seconds:g}",
        ]
        if audio_map:
            command += ["-map", "0:a:0", "-vn"]
        command += ["-ac", "1", "-ar", "8000", "-f", "s16le", "-y", str(output)]
        process = await asyncio.create_subprocess_exec(*command, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        _, error = await asyncio.wait_for(process.communicate(), timeout=60)
        min_size = int(sample_seconds * 8000 * 2 * 0.70)
        if process.returncode or not output.exists() or output.stat().st_size < min_size:
            raise RuntimeError((error.decode(errors="replace") or "sample decode failed")[:300])

    @staticmethod
    async def _wav(playlist: Path, seek: float, output: Path, sample_seconds: float = 8.0):
        command = [
            "ffmpeg", "-v", "error", "-allowed_extensions", "ALL",
            "-protocol_whitelist", "file,crypto", "-i", str(playlist),
            "-ss", f"{max(0.0, seek):.3f}", "-t", f"{sample_seconds:g}",
            "-map", "0:a:0", "-vn", "-ac", "1", "-ar", "48000",
            "-c:a", "pcm_s16le", "-y", str(output),
        ]
        process = await asyncio.create_subprocess_exec(
            *command, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
        )
        _, error = await asyncio.wait_for(process.communicate(), timeout=60)
        if process.returncode or not output.exists() or output.stat().st_size <= 44:
            raise RuntimeError((error.decode(errors="replace") or "ffmpeg preview failed")[:500])

    async def manual_preview(self, payload: dict, kind: str, position: float,
                             sample_seconds: float = 8.0) -> Path:
        if kind not in {"reference", "replacement"}:
            raise ValueError("preview kind must be reference or replacement")
        audio_hid = str(payload.get("audio_hid") or "")
        if not audio_hid:
            raise ValueError("playback has no audio_hid")
        position = max(0.0, float(position))
        sample_seconds = min(30.0, max(1.0, float(sample_seconds)))
        video_headers = payload.get("video_headers")
        audio_headers = payload.get("audio_headers")
        source = (
            str(payload.get("video_fingerprint") or ""),
            str(payload.get("video_url") or ""),
            str(payload.get("reference_audio_url") or ""),
            repr(sorted(video_headers.items())) if isinstance(video_headers, dict) else "",
            repr(sorted(audio_headers.items())) if isinstance(audio_headers, dict) else "",
        )
        preview_key = hashlib.sha1(
            f"{kind}|{source}|{position:.3f}|{sample_seconds:.3f}".encode()
        ).hexdigest()[:12]
        output = self.audio._dir(audio_hid) / (
            f"align_{kind}_{int(round(position * 1000))}_{preview_key}.wav"
        )
        if output.exists() and output.stat().st_size > 44:
            return output
        with tempfile.TemporaryDirectory(prefix="sidecar-align-") as directory:
            root = Path(directory)
            if kind == "replacement":
                playlist, seek, _ = await self._decode_audio(
                    audio_hid, position, root, sample_seconds=sample_seconds,
                    headers=audio_headers if isinstance(audio_headers, dict) else None,
                )
            else:
                video_headers = (
                    payload.get("video_headers")
                    if isinstance(payload.get("video_headers"), dict) else {}
                )
                reference_url = str(
                    payload.get("reference_audio_url") or payload.get("referenceAudio") or ""
                ).strip()
                if reference_url:
                    playlist, seek, _ = await self._decode_reference_audio(
                        reference_url, video_headers, position, root,
                        sample_seconds=sample_seconds,
                    )
                else:
                    video_url = str(payload.get("video_url") or "")
                    if not video_url:
                        raise ValueError("playback has no video or reference audio URL")
                    playlist, seek, _ = await self._decode_video(
                        video_url, video_headers, position, root,
                        sample_seconds=sample_seconds,
                    )
            temporary_output = root / "preview.wav"
            await self._wav(playlist, seek, temporary_output, sample_seconds)
            shutil.copy2(temporary_output, output)
        return output

    @staticmethod
    def _envelope(path: Path):
        values = array("h")
        values.frombytes(path.read_bytes())
        if not values:
            return []
        if os.sys.byteorder != "little":
            values.byteswap()
        step, window = 80, 160
        prefix = [0.0]
        for value in values:
            prefix.append(prefix[-1] + abs(value))
        envelope = []
        for center in range(0, len(values), step):
            lo, hi = max(0, center - window // 2), min(len(values), center + window // 2)
            envelope.append((prefix[hi] - prefix[lo]) / max(1, hi - lo))
        mean = sum(envelope) / len(envelope)
        std = math.sqrt(sum((value - mean) ** 2 for value in envelope) / len(envelope)) or 1.0
        return [(value - mean) / std for value in envelope]

    @staticmethod
    def _lag(reference, candidate, max_seconds=5):
        best = (-2.0, 0)
        for lag in range(-max_seconds * 100, max_seconds * 100 + 1):
            if lag >= 0:
                size = min(len(reference), len(candidate) - lag)
                left, right = reference[:size], candidate[lag:lag + size]
            else:
                size = min(len(candidate), len(reference) + lag)
                left, right = reference[-lag:-lag + size], candidate[:size]
            if size < min(200, len(reference) // 2):
                continue
            lm, rm = sum(left) / size, sum(right) / size
            lv = sum((value - lm) ** 2 for value in left)
            rv = sum((value - rm) ** 2 for value in right)
            denominator = math.sqrt(lv * rv)
            if denominator:
                correlation = sum((left[i] - lm) * (right[i] - rm) for i in range(size)) / denominator
                if correlation > best[0]:
                    best = correlation, lag
        return best[1] / 100.0, best[0]

    def _evaluate_measurements(self, video_duration: float, audio_duration: float,
                               measurements: list[dict], video_start_time: float = 0.0,
                               correlation_floor: float = 0.70) -> dict:
        result = {
            "status": "incompatible",
            "video_duration": video_duration,
            "audio_duration": audio_duration,
            "measurements": measurements,
        }
        valid = [item for item in measurements
                 if item and float(item.get("correlation") or 0.0) >= correlation_floor]
        if len(valid) < 2:
            return result

        # 1. Constant offset evaluation
        measured = statistics.median(float(item["offset"]) for item in valid)
        deviation = max(abs(float(item["offset"]) - measured) for item in valid)
        if deviation <= self.SYNC_MAX_DEVIATION:
            result.update({
                "status": "ok",
                "offset": round(-measured + video_start_time, 3),
                "rate": 1.0,
                "sync_mode": "constant",
                "confidence": min(float(item["correlation"]) for item in valid),
                "deviation": deviation,
            })
            return result

        result["deviation"] = deviation

        # 2. Standard 24.000 vs 23.976 fps linear drift check
        if len(valid) >= 2:
            sorted_valid = sorted(valid, key=lambda x: float(x["position"]))
            t1 = float(sorted_valid[0]["position"])
            t2 = float(sorted_valid[-1]["position"])
            dt = t2 - t1
            if dt >= 1200:  # at least 20 minutes apart
                o1 = float(sorted_valid[0]["offset"])
                o2 = float(sorted_valid[-1]["offset"])
                slope = (o2 - o1) / dt
                drift_hr = slope * 3600.0
                std_rate = None
                if -3.75 <= drift_hr <= -3.45:
                    std_rate = 1000.0 / 1001.0  # 24.000 -> 23.976 fps
                elif 3.45 <= drift_hr <= 3.75:
                    std_rate = 1001.0 / 1000.0  # 23.976 -> 24.000 fps

                if std_rate:
                    interc = o1 - (std_rate - 1.0) * t1
                    end_dev = abs(interc + std_rate * video_duration - audio_duration)
                    if end_dev <= self.SYNC_MAX_END_DEVIATION:
                        result.update({
                            "status": "ok",
                            "offset": round(-interc + video_start_time, 3),
                            "rate": round(std_rate, 9),
                            "sync_mode": "linear",
                            "confidence": min(float(item["correlation"]) for item in valid),
                            "deviation": abs(drift_hr - (-3.600614 if std_rate < 1.0 else 3.6036)),
                            "drift_per_hour": drift_hr,
                            "candidate_rate": std_rate,
                            "end_deviation": end_dev,
                        })
                        return result

        # 3. General linear regression
        if len(valid) < 3:
            return result

        positions = [float(item["position"]) for item in valid]
        offsets = [float(item["offset"]) for item in valid]
        mean_position = statistics.mean(positions)
        mean_offset = statistics.mean(offsets)
        denominator = sum((position - mean_position) ** 2 for position in positions)
        if denominator <= 0 or max(positions) - min(positions) < 60.0:
            return result

        slope = sum((position - mean_position) * (offset - mean_offset)
                    for position, offset in zip(positions, offsets)) / denominator
        intercept = mean_offset - slope * mean_position
        rate = 1.0 + slope
        linear_deviation = max(abs(offset - (intercept + slope * position))
                               for position, offset in zip(positions, offsets))
        end_deviation = abs(intercept + rate * video_duration - audio_duration)
        result.update({
            "candidate_rate": rate,
            "linear_deviation": linear_deviation,
            "drift_per_hour": slope * 3600.0,
            "end_deviation": end_deviation,
        })
        if (abs(rate - 1.0) > self.SYNC_MAX_RATE_DELTA
                or linear_deviation > self.SYNC_MAX_LINEAR_DEVIATION
                or end_deviation > self.SYNC_MAX_END_DEVIATION):
            return result

        result.update({
            "status": "ok",
            "offset": round(-intercept + video_start_time, 3),
            "rate": round(rate, 9),
            "sync_mode": "linear",
            "confidence": min(float(item["correlation"]) for item in valid),
            "deviation": linear_deviation,
        })
        return result

    async def measure(self, payload: dict):
        media_key = str(payload.get("media_key") or "")
        resolution = int(payload.get("resolution") or 0)
        provider = str(payload.get("provider") or "").strip().lower()
        video_url = str(payload.get("video_url") or "")
        video_headers = payload.get("video_headers") if isinstance(payload.get("video_headers"), dict) else {}
        reference_audio_url = str(
            payload.get("reference_audio_url") or payload.get("referenceAudio") or ""
        ).strip()
        audio_hid = str(payload.get("audio_hid") or "")
        video_fp = str(payload.get("video_fingerprint") or "")
        metadata = self.audio.metadata(audio_hid)
        audio_fp = str(payload.get("audio_fingerprint") or metadata.get("source_fingerprint") or "")
        cache_key = self.offsets.key(media_key, resolution, video_fp, audio_fp)
        payload["cache_key"] = cache_key
        lookup = await self.offsets.lookup({
            "cache_key": cache_key,
            "media_key": media_key,
            "resolution": resolution,
            "video_fingerprint": video_fp,
            "audio_fingerprint": audio_fp,
            "vpsAccess": payload.get("vpsAccess") or payload.get("vps_access") or "",
            "vpsHost": payload.get("vpsHost") or payload.get("vps_host") or "",
            "video_url": video_url,
            "provider": payload.get("provider", ""),
            "server": payload.get("server", ""),
        })
        lookup_source = lookup.get("_cache_source") if isinstance(lookup, dict) else None
        lookup_details = lookup.get("details") if isinstance(lookup, dict) else {}
        lookup_status = str(
            (lookup.get("status") if isinstance(lookup, dict) else "")
            or (lookup_details.get("status") if isinstance(lookup_details, dict) else "")
        ).strip().lower()
        retry_old_vidfast = (
            provider == "vidfast"
            and lookup_status == "incompatible"
            and (lookup_details.get("sync_algorithm") if isinstance(lookup_details, dict) else "")
            != self.SYNC_ALGORITHM
        )
        if lookup and not retry_old_vidfast:
            cached_value = dict(lookup.get("details") or lookup)
            cached_value.pop("_cache_source", None)
            result = {"status": "ok", **cached_value, "cached": True}
            if reference_audio_url and not result.get("video_start_time"):
                lookup = None
            else:
                result["cache_key"] = cache_key
                result["cache_source"] = lookup_source or "unknown"
                return result

        video_entries, _ = await self._video_entries(video_url, video_headers)
        video_duration = sum(item["duration"] for item in video_entries)
        sample_video_url = await self._vidfast_sample_url(
            video_url, video_headers, video_duration, provider
        )
        video_start_time = 0.0
        if reference_audio_url:
            video_start_time = await self._media_start_time(video_url, video_headers)
        reference_duration = video_duration
        if reference_audio_url:
            reference_entries, _ = await self._video_entries(reference_audio_url, video_headers)
            reference_duration = sum(item["duration"] for item in reference_entries)
            if abs(reference_duration - video_duration) > 1.0:
                raise ValueError("reference audio timeline mismatch")
        audio_duration = sum(metadata["durs"])
        common = min(video_duration, reference_duration, audio_duration)
        if common < 90:
            raise ValueError("media too short")

        # Fast Pass: 3 points @ 5s each
        fast_points = [
            (round(common * 0.2, 3), 5.0),
            (round(common * 0.4, 3), 5.0),
            (round(common * 0.7, 3), 5.0),
        ]
        fallback_positions = sorted({
            min(60.0, common * 0.1),
            round(common * 0.6, 3),
            max(30.0, common - 90.0),
        })

        correlation_floor = 0.70
        measurements = []
        with tempfile.TemporaryDirectory(prefix="sidecar-sync-") as directory:
            root = Path(directory)

            async def _sample_point(position: float, duration: float, index: int) -> dict:
                video_dir, audio_dir = root / f"video-{index}", root / f"audio-{index}"
                video_dir.mkdir(exist_ok=True)
                audio_dir.mkdir(exist_ok=True)
                if reference_audio_url:
                    reference_playlist, reference_seek, _ = await self._decode_reference_audio(
                        reference_audio_url, video_headers, position, video_dir, sample_seconds=duration
                    )
                else:
                    reference_playlist, reference_seek, _ = await self._decode_video(
                        sample_video_url, video_headers, position, video_dir, sample_seconds=duration
                    )
                audio_playlist, audio_seek, _ = await self._decode_audio(
                    audio_hid, position, audio_dir, sample_seconds=duration
                )
                video_pcm, audio_pcm = root / f"video-{index}.pcm", root / f"audio-{index}.pcm"
                samples = await asyncio.gather(
                    self._pcm(reference_playlist, reference_seek, video_pcm, sample_seconds=duration),
                    self._pcm(audio_playlist, audio_seek, audio_pcm, sample_seconds=duration),
                    return_exceptions=True,
                )
                failure = next((sample for sample in samples if isinstance(sample, BaseException)), None)
                if failure is not None:
                    raise RuntimeError(f"{type(failure).__name__}: {str(failure)[:260]}")
                lag, correlation = self._lag(self._envelope(video_pcm), self._envelope(audio_pcm))
                return {"position": position, "lag": lag, "offset": lag, "correlation": correlation, "duration": duration}

            # Phase 1: Fast Pass (5s / 7s / 5s)
            fast_results = await asyncio.gather(*(
                _sample_point(pos, dur, i) for i, (pos, dur) in enumerate(fast_points)
            ), return_exceptions=True)
            for res in fast_results:
                if not isinstance(res, BaseException):
                    measurements.append(res)

            fast_valid = [item for item in measurements if item["correlation"] >= correlation_floor]
            if len(fast_valid) >= 2:
                measured = statistics.median(item["offset"] for item in fast_valid)
                deviation = max(abs(item["offset"] - measured) for item in fast_valid)
                if deviation <= self.SYNC_MAX_DEVIATION:
                    result = {
                        "status": "ok",
                        "offset": round(-measured + video_start_time, 3),
                        "rate": 1.0,
                        "confidence": min(item["correlation"] for item in fast_valid),
                        "deviation": deviation,
                        "sync_mode": "fast",
                        "video_duration": video_duration,
                        "audio_duration": audio_duration,
                        "measurements": measurements,
                    }
                    if reference_audio_url:
                        result["video_start_time"] = round(video_start_time, 3)
                    result["sync_algorithm"] = self.SYNC_ALGORITHM
                    result["cache_key"] = cache_key
                    result["cached"] = False
                    result["cache_source"] = None
                    return result

            # Phase 2: Fallback positions (5.0s each) if Fast Pass did not have low deviation
            additional_positions = [
                pos for pos in fallback_positions
                if not any(abs(pos - m["position"]) < 30.0 for m in measurements)
            ]
            start_idx = len(measurements)
            add_results = await asyncio.gather(*(
                _sample_point(pos, 5.0, start_idx + i) for i, pos in enumerate(additional_positions)
            ), return_exceptions=True)
            for res in add_results:
                if not isinstance(res, BaseException):
                    measurements.append(res)

        result = self._evaluate_measurements(
            video_duration, audio_duration, measurements,
            video_start_time=video_start_time,
            correlation_floor=correlation_floor,
        )
        if reference_audio_url and result.get("status") == "ok":
            result["video_start_time"] = round(video_start_time, 3)
        result["sync_algorithm"] = self.SYNC_ALGORITHM
        result["cache_key"] = cache_key
        result["cached"] = False
        result["cache_source"] = None
        return result
