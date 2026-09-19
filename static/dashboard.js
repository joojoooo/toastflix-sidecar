const ui = {
  login: document.querySelector("#login"), console: document.querySelector("#console"),
  form: document.querySelector("#login-form"), token: document.querySelector("#admin-token"),
  loginError: document.querySelector("#login-error"), lock: document.querySelector("#lock-button"),
  dot: document.querySelector("#live-dot"), connection: document.querySelector("#connection-label"),
  serverTime: document.querySelector("#server-time"), remote: document.querySelector("#remote-status"),
  autoUpload: document.querySelector("#automatic-upload-toggle"),
  autoUploadLabel: document.querySelector("#automatic-upload-label"),
  cache: document.querySelector("#cache-status"), proxy: document.querySelector("#proxy-status"),
  ttl: document.querySelector("#ttl-status"), retention: document.querySelector("#retention-status"),
  serverMeta: document.querySelector("#server-meta"),
  sessions: document.querySelector("#sessions"), noSessions: document.querySelector("#no-sessions"),
  historySection: document.querySelector("#history-section"), history: document.querySelector("#history"),
  offsets: document.querySelector("#offset-rows"),
  noOffsets: document.querySelector("#no-offsets"), decisions: document.querySelector("#decision-rows"),
  noDecisions: document.querySelector("#no-decisions"), traffic: document.querySelector("#traffic-rows"),
  noTraffic: document.querySelector("#no-traffic"), trafficSearch: document.querySelector("#traffic-search"),
  trafficFilters: document.querySelector("#traffic-filters"), notice: document.querySelector("#notice"),
  drawer: document.querySelector("#activity-drawer"), drawerTitle: document.querySelector("#drawer-title"),
  drawerContent: document.querySelector("#drawer-content"), copyTransaction: document.querySelector("#copy-transaction"),
  uploadDialog: document.querySelector("#upload-dialog"),
  uploadDialogMessage: document.querySelector("#upload-dialog-message"),
  uploadHostField: document.querySelector("#upload-vps-host-field"), uploadHost: document.querySelector("#upload-vps-host"),
  uploadAccessField: document.querySelector("#upload-vps-access-field"), uploadAccess: document.querySelector("#upload-vps-access"),
  playerUploadDialog: document.querySelector("#player-upload-dialog"),
  playerUploadSummary: document.querySelector("#player-upload-summary"),
  playerUploadError: document.querySelector("#player-upload-error"),
  playerUploadKey: document.querySelector("#player-upload-key"),
  playerUploadKeyRule: document.querySelector("#player-upload-key-rule"),
  playerUploadMedia: document.querySelector("#player-upload-media"),
  playerUploadResolution: document.querySelector("#player-upload-resolution"),
  playerUploadVideoFp: document.querySelector("#player-upload-video-fp"),
  playerUploadAudioFp: document.querySelector("#player-upload-audio-fp"),
  playerUploadIdentityRule: document.querySelector("#player-upload-identity-rule"),
  playerUploadSuggestions: document.querySelector("#player-upload-suggestions"),
  playerUploadHostField: document.querySelector("#player-upload-host-field"),
  playerUploadHost: document.querySelector("#player-upload-host"),
  playerUploadHostRule: document.querySelector("#player-upload-host-rule"),
  playerUploadAccessField: document.querySelector("#player-upload-access-field"),
  playerUploadAccess: document.querySelector("#player-upload-access"),
  playerUploadAccessRule: document.querySelector("#player-upload-access-rule"),
  playerUploadAccessHint: document.querySelector("#player-upload-access-hint"),
};

let token = sessionStorage.getItem("sidecar-admin-token") || "";
let streamController = null;
let reconnectTimer = null;
let currentState = null;
let sessionSignature = "";
let offsetSignature = "";
let decisionSignature = "";
let activityFilter = "all";
let activityRecords = new Map();
let openTransaction = null;
let noticeTimer = null;
const selectedTabs = new Map();
const objectUrls = new Set();

function element(tag, className = "", text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function formatTime(value, includeDate = false) {
  if (!value) return "—";
  const options = includeDate
    ? { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit" }
    : { hour: "2-digit", minute: "2-digit", second: "2-digit" };
  return new Date(value * 1000).toLocaleString([], options);
}

function formatOffset(value) {
  const number = Number(value || 0);
  return `${number >= 0 ? "+" : ""}${number.toFixed(3)}`;
}

function formatBytes(value) {
  let number = Number(value || 0);
  if (!number) return "0 B";
  const units = ["B", "KB", "MB", "GB"];
  let unit = 0;
  while (number >= 1024 && unit < units.length - 1) { number /= 1024; unit += 1; }
  return `${number.toFixed(unit ? 1 : 0)} ${units[unit]}`;
}

function jsonText(value) { return JSON.stringify(value, null, 2); }

async function copyText(value) {
  const text = typeof value === "string" ? value : jsonText(value);
  try {
    await navigator.clipboard.writeText(text);
  } catch (_) {
    const area = element("textarea");
    area.value = text; area.style.position = "fixed"; area.style.opacity = "0";
    document.body.append(area); area.select(); document.execCommand("copy"); area.remove();
  }
  showNotice("Copied to clipboard.");
}

function showNotice(message, bad = false) {
  clearTimeout(noticeTimer);
  ui.notice.textContent = message;
  ui.notice.classList.toggle("bad", bad);
  ui.notice.hidden = false;
  noticeTimer = setTimeout(() => { ui.notice.hidden = true; }, 5500);
}

function setConnected(connected, label = "") {
  ui.dot.classList.toggle("live", connected);
  ui.connection.textContent = label || (connected ? "Live SSE" : "Disconnected");
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: {
      Authorization: `Bearer ${token}`,
      ...(options.body ? { "Content-Type": "application/json" } : {}),
      ...(options.headers || {}),
    },
  });
  let body;
  try { body = await response.json(); } catch (_) { body = {}; }
  if (!response.ok) {
    const detail = body.detail || body;
    const error = new Error(typeof detail === "string" ? detail : detail.message || detail.remote_error || jsonText(detail));
    error.status = response.status;
    error.detail = detail;
    throw error;
  }
  return body;
}

function requestUploadDetails(fields, defaults = {}) {
  const missing = new Set(fields);
  if (!missing.size) return Promise.resolve({});
  if (ui.uploadDialog.open) return Promise.resolve(null);
  const labels = [...missing].map((name) => name === "vpsHost" ? "VPS host" : "VPS access");
  ui.uploadDialogMessage.textContent = `Enter the missing ${labels.join(" and ")} to upload this offset. The value will be retained with the local offset record for later uploads.`;
  ui.uploadHostField.hidden = !missing.has("vpsHost");
  ui.uploadHost.required = missing.has("vpsHost");
  ui.uploadHost.value = missing.has("vpsHost") ? String(defaults.vpsHost || "") : "";
  ui.uploadAccessField.hidden = !missing.has("vpsAccess");
  ui.uploadAccess.required = missing.has("vpsAccess");
  ui.uploadAccess.value = missing.has("vpsAccess") ? String(defaults.vpsAccess || "") : "";
  ui.uploadDialog.returnValue = "cancel";
  return new Promise((resolve) => {
    ui.uploadDialog.addEventListener("close", () => {
      const access = ui.uploadAccess.value.trim();
      ui.uploadAccess.value = "";
      if (ui.uploadDialog.returnValue !== "upload") { resolve(null); return; }
      const result = {};
      if (missing.has("vpsHost")) result.vpsHost = ui.uploadHost.value.trim();
      if (missing.has("vpsAccess")) result.vpsAccess = access;
      resolve(result);
    }, { once: true });
    ui.uploadDialog.showModal();
    requestAnimationFrame(() => {
      (missing.has("vpsHost") ? ui.uploadHost : ui.uploadAccess).focus();
    });
  });
}

function requestPlayerUploadDetails(info, previous = {}, errorMessage = "", forceIdentity = false) {
  if (ui.playerUploadDialog.open) return Promise.resolve(null);
  const identity = { ...info.identity, ...previous };
  const fields = [
    [ui.playerUploadMedia, "media_key"],
    [ui.playerUploadResolution, "resolution"],
    [ui.playerUploadVideoFp, "video_fingerprint"],
    [ui.playerUploadAudioFp, "audio_fingerprint"],
  ];
  ui.playerUploadKey.value = previous.cache_key ?? info.cache_key ?? "";
  ui.playerUploadKey.readOnly = info.local_record_found;
  fields.forEach(([input, name]) => {
    input.value = identity[name] ?? "";
    input.readOnly = info.local_record_found;
  });
  const configured = info.connection.configured_api;
  ui.playerUploadHostField.hidden = configured;
  ui.playerUploadAccessField.hidden = configured;
  ui.playerUploadHost.value = previous.vpsHost ?? info.connection.vpsHost ?? "";
  ui.playerUploadAccess.value = "";
  ui.playerUploadSummary.textContent = `Upload the selected ${formatOffset(info.selected_offset)} s (${info.selected_source}) value. Fields already known are prefilled; check guessed values against the same video edition.`;
  ui.playerUploadError.textContent = errorMessage;
  ui.playerUploadError.hidden = !errorMessage;
  ui.playerUploadAccessHint.textContent = info.connection.vpsAccessAvailable
    ? "Access is available on the server. Enter a replacement only if you change the host or the current access fails."
    : "Required without a configured OFFSET_API_URL; this value is saved only after a successful upload with complete identity.";

  const updateRules = () => {
    const needsTuple = forceIdentity || !ui.playerUploadKey.value.trim();
    fields.forEach(([input]) => { input.required = needsTuple; });
    ui.playerUploadKeyRule.textContent = needsTuple ? "or generate from all four fields" : "known; identity fields optional";
    ui.playerUploadIdentityRule.textContent = needsTuple
      ? "All four identity fields are mandatory to generate the cache key. Resolution may be estimated, but the video fingerprint must be the exact ToastFlix value to target the right offset."
      : "The exact cache key permits a remote attempt without the video URL or complete identity. Fill all four fields to save a local record too; the remote server may still reject a key-only report.";
    const hostChanged = ui.playerUploadHost.value.trim().replace(/\/$/, "")
      !== String(info.connection.vpsHost || "").replace(/\/$/, "");
    ui.playerUploadHost.required = !configured;
    ui.playerUploadAccess.required = !configured && (!info.connection.vpsAccessAvailable || hostChanged);
    ui.playerUploadHostRule.textContent = configured ? "optional" : "required";
    ui.playerUploadAccessRule.textContent = ui.playerUploadAccess.required ? "required" : "available on server";
  };
  ui.playerUploadKey.addEventListener("input", updateRules);
  ui.playerUploadHost.addEventListener("input", updateRules);
  updateRules();

  const incompleteIdentity = fields.some(([input]) => !input.value.trim());
  const suggestions = info.local_record_found || (!incompleteIdentity && !forceIdentity)
    ? [] : info.suggestions || [];
  ui.playerUploadSuggestions.hidden = !suggestions.length;
  ui.playerUploadSuggestions.replaceChildren();
  if (suggestions.length) {
    ui.playerUploadSuggestions.append(
      element("strong", "", "Possible identity from another track"),
      element("p", "", "Use only if it is the same video edition and audio source. A wrong match can overwrite another offset."),
    );
    suggestions.forEach((candidate) => {
      const button = element("button", "button subtle compact",
        `${candidate.source} · ${candidate.resolution}p · video ${candidate.video_fingerprint} · audio ${candidate.audio_fingerprint}`);
      button.type = "button";
      button.addEventListener("click", () => {
        ui.playerUploadKey.value = candidate.cache_key;
        ui.playerUploadMedia.value = candidate.media_key;
        ui.playerUploadResolution.value = candidate.resolution;
        ui.playerUploadVideoFp.value = candidate.video_fingerprint;
        ui.playerUploadAudioFp.value = candidate.audio_fingerprint;
        updateRules();
      });
      ui.playerUploadSuggestions.append(button);
    });
  }

  ui.playerUploadDialog.returnValue = "cancel";
  return new Promise((resolve) => {
    ui.playerUploadDialog.addEventListener("close", () => {
      ui.playerUploadKey.removeEventListener("input", updateRules);
      ui.playerUploadHost.removeEventListener("input", updateRules);
      const access = ui.playerUploadAccess.value.trim();
      ui.playerUploadAccess.value = "";
      if (ui.playerUploadDialog.returnValue !== "upload") { resolve(null); return; }
      const result = {
        cache_key: ui.playerUploadKey.value.trim(),
        media_key: ui.playerUploadMedia.value.trim(),
        resolution: ui.playerUploadResolution.value.trim(),
        video_fingerprint: ui.playerUploadVideoFp.value.trim(),
        audio_fingerprint: ui.playerUploadAudioFp.value.trim(),
      };
      const host = ui.playerUploadHost.value.trim();
      if (!configured && host !== info.connection.vpsHost) result.vpsHost = host;
      if (access) result.vpsAccess = access;
      resolve(result);
    }, { once: true });
    ui.playerUploadDialog.showModal();
    requestAnimationFrame(() => {
      const firstMissing = fields.find(([input]) => !input.value);
      (firstMissing?.[0] || ui.playerUploadKey).focus();
    });
  });
}

async function fetchAudio(path) {
  const response = await fetch(path, { headers: { Authorization: `Bearer ${token}` } });
  if (!response.ok) {
    let detail = `HTTP ${response.status}`;
    try { const body = await response.json(); detail = body.detail || detail; } catch (_) { /* empty */ }
    throw new Error(typeof detail === "string" ? detail : jsonText(detail));
  }
  const data = await response.arrayBuffer();
  const url = URL.createObjectURL(new Blob([data], { type: "audio/wav" }));
  objectUrls.add(url);
  const context = new (window.AudioContext || window.webkitAudioContext)();
  const decoded = await context.decodeAudioData(data.slice(0));
  await context.close();
  return { url, decoded };
}

function shiftedWav(buffer, shiftSeconds) {
  const channels = buffer.numberOfChannels;
  const sampleRate = buffer.sampleRate;
  const frameCount = buffer.length;
  const bytesPerSample = 2;
  const blockAlign = channels * bytesPerSample;
  const dataLength = frameCount * blockAlign;
  const wav = new ArrayBuffer(44 + dataLength);
  const view = new DataView(wav);
  const writeText = (offset, value) => {
    for (let index = 0; index < value.length; index += 1) {
      view.setUint8(offset + index, value.charCodeAt(index));
    }
  };
  writeText(0, "RIFF"); view.setUint32(4, 36 + dataLength, true);
  writeText(8, "WAVE"); writeText(12, "fmt ");
  view.setUint32(16, 16, true); view.setUint16(20, 1, true);
  view.setUint16(22, channels, true); view.setUint32(24, sampleRate, true);
  view.setUint32(28, sampleRate * blockAlign, true);
  view.setUint16(32, blockAlign, true); view.setUint16(34, 16, true);
  writeText(36, "data"); view.setUint32(40, dataLength, true);

  const numericShift = Number(shiftSeconds);
  const shiftFrames = Math.round((Number.isFinite(numericShift) ? numericShift : 0) * sampleRate);
  const channelData = Array.from({ length: channels }, (_, channel) => buffer.getChannelData(channel));
  let outputOffset = 44;
  for (let outputFrame = 0; outputFrame < frameCount; outputFrame += 1) {
    const sourceFrame = outputFrame - shiftFrames;
    for (let channel = 0; channel < channels; channel += 1) {
      const sample = sourceFrame >= 0 && sourceFrame < frameCount
        ? Math.max(-1, Math.min(1, channelData[channel][sourceFrame]))
        : 0;
      view.setInt16(outputOffset, sample < 0 ? sample * 0x8000 : sample * 0x7fff, true);
      outputOffset += bytesPerSample;
    }
  }
  return new Blob([wav], { type: "audio/wav" });
}

function releaseObjectUrl(url) {
  if (!url) return;
  URL.revokeObjectURL(url);
  objectUrls.delete(url);
}

function mediaLink(mediaKey) {
  const value = String(mediaKey || "");
  const imdb = value.match(/(?:^|:)(tt\d{5,})(?::|$)/i);
  if (imdb) return `https://www.imdb.com/title/${imdb[1].toLowerCase()}/`;
  const typedTmdb = value.match(/^(movie|series|tv):(\d+)(?::|$)/i);
  if (typedTmdb) {
    const type = typedTmdb[1].toLowerCase() === "movie" ? "movie" : "tv";
    return `https://www.themoviedb.org/${type}/${typedTmdb[2]}`;
  }
  const explicitTmdb = value.match(/tmdb(?::|-)(movie|tv|series)?(?::|-)?(\d+)/i);
  if (explicitTmdb) {
    const declared = explicitTmdb[1]?.toLowerCase();
    const type = declared === "movie" || (!declared && value.toLowerCase().startsWith("movie:")) ? "movie" : "tv";
    return `https://www.themoviedb.org/${type}/${explicitTmdb[2]}`;
  }
  return null;
}

function mediaTitle(mediaKey) {
  const link = mediaLink(mediaKey);
  const heading = element("h3");
  if (link) {
    const anchor = element("a", "", mediaKey);
    anchor.href = link; anchor.target = "_blank"; anchor.rel = "noreferrer";
    heading.append(anchor);
  } else {
    heading.textContent = mediaKey || "Unidentified media";
  }
  return heading;
}

function languageInfo(value) {
  const code = String(value || "unknown").toLowerCase();
  if (["ita", "it", "italian"].includes(code)) return { code: "ITA", name: "Italian audio track" };
  if (["eng", "en", "english"].includes(code)) return { code: "ENG", name: "English audio track" };
  return { code: code.slice(0, 4).toUpperCase() || "?", name: `${value || "Unknown"} audio track` };
}

function metadataValue(player, ...names) {
  const sources = [
    player.sync_metadata || {}, player.prepare_request || {},
    player.audio_track?.metadata || {}, player.audio_metadata || {},
  ];
  for (const source of sources) {
    for (const name of names) {
      if (source[name] !== undefined && source[name] !== null && source[name] !== "") return source[name];
    }
  }
  return undefined;
}

function incomingOffsetExplanation(player, offset) {
  const value = `${formatOffset(offset)} s`;
  if (["init", "segment"].includes(player.last_request_kind)) {
    return `The sidecar wrote ${value} into this ${player.last_request_kind} URL when it generated the audio playlist.`;
  }
  if (player.last_request_kind === "playlist") {
    return `The top-level audio playlist request arrived with ${value}. This sidecar initially emits that URL with +0.000 s, so a non-zero value came from upstream ToastFlix or a previously cached URL.`;
  }
  return `The incoming HLS URL contained ${value}. Inspect the HLS traffic entries to trace where the URL was produced.`;
}

function sourceInfo(player) {
  if (player.control_source === "manual") return {
    label: "Manual dashboard override", detail: "Your edited value currently takes priority.", style: "manual",
  };
  if (player.control_source === "cached") return {
    label: `Database cache · ${player.cache_source || "unknown location"}`,
    detail: "The sidecar lookup returned this stored offset; no new alignment was calculated.", style: "cached",
  };
  if (player.control_source === "calculated") return {
    label: "Calculated by this sidecar", detail: "The sync engine measured this offset from media samples.", style: "calculated",
  };
  const syncStatus = String(player.sync_result?.status || "").toLowerCase();
  const carriedOffset = Number(player.offset_candidates?.request?.offset ?? player.current_offset ?? 0);
  const isZeroFallback = Math.abs(carriedOffset) < 0.0005;
  if (syncStatus === "incompatible") return {
    label: isZeroFallback ? "Zero fallback retained after failed sync" : "Existing stream offset retained after failed sync",
    detail: isZeroFallback
      ? "Automatic sync could not find a reliable offset, so the sidecar kept its initial +0.000 s default."
      : `Automatic sync could not find a reliable offset. ${incomingOffsetExplanation(player, carriedOffset)}`,
    style: "",
  };
  if (syncStatus === "error") return {
    label: isZeroFallback ? "Zero fallback retained after sync error" : "Existing stream offset retained after sync error",
    detail: isZeroFallback
      ? "Automatic sync ended with an error, so the sidecar kept its initial +0.000 s default."
      : `Automatic sync ended with an error. ${incomingOffsetExplanation(player, carriedOffset)}`,
    style: "",
  };
  if (!isZeroFallback) return {
    label: "Offset carried by incoming HLS URL",
    detail: incomingOffsetExplanation(player, carriedOffset),
    style: "",
  };
  return {
    label: "Initial sidecar zero default",
    detail: "The sidecar is using its initial +0.000 s value while it waits for a saved or calculated offset.",
    style: "",
  };
}

function databaseLookupInfo(player) {
  if (player.cache_hit === true) return `Hit · ${player.cache_source || "unknown"}`;
  if (player.cache_hit !== false) return "Not completed";
  const syncStatus = String(player.sync_result?.status || "").toLowerCase();
  if (syncStatus === "ok") return "Miss · automatic sync succeeded";
  if (syncStatus === "incompatible") return "Miss · automatic sync failed (no reliable offset)";
  if (syncStatus === "error") return "Miss · automatic sync ended with an error";
  return "Miss · waiting for automatic sync";
}

function makeField(label, value, link = false, edit = null) {
  const field = element("div", "data-field");
  field.append(element("span", "", label));
  const line = element("div", "data-value");
  const text = value === null || value === undefined || value === "" ? "—" : String(value);
  if (link && /^https?:\/\//i.test(text)) {
    const anchor = element("a", "", text);
    anchor.href = text; anchor.target = "_blank"; anchor.rel = "noreferrer";
    line.append(anchor);
  } else {
    line.append(element("code", "", text));
  }
  const copy = element("button", "copy-button", "Copy");
  copy.type = "button";
  copy.disabled = text === "—";
  copy.addEventListener("click", () => copyText(text));
  line.append(copy);
  if (edit) {
    const button = element("button", "copy-button", "Edit");
    button.type = "button";
    button.addEventListener("click", () => {
      const form = element("form", "data-edit-form");
      const input = element(edit.json ? "textarea" : "input");
      if (!edit.json) input.type = edit.key === "resolution" ? "number" : "text";
      if (edit.key === "resolution") { input.min = "1"; input.max = "4320"; input.step = "1"; }
      input.value = text === "—" ? (edit.json ? "{}" : "")
        : edit.key === "resolution" ? text.replace(/p$/, "") : text;
      input.setAttribute("aria-label", label);
      input.required = true;
      const actions = element("div", "data-edit-actions");
      const save = element("button", "copy-button", "Save"); save.type = "submit";
      const cancel = element("button", "copy-button", "Cancel"); cancel.type = "button";
      cancel.addEventListener("click", () => form.replaceWith(line));
      actions.append(save, cancel); form.append(input, actions);
      form.addEventListener("submit", async (event) => {
        event.preventDefault();
        let nextValue = input.value.trim();
        if (edit.json) {
          try { nextValue = JSON.parse(nextValue); }
          catch (_) { showNotice(`${label} must be valid JSON.`, true); return; }
        }
        save.disabled = true;
        try {
          const result = await api(`/api/dashboard/players/${encodeURIComponent(edit.playbackId)}/metadata`, {
            method: "PATCH", body: JSON.stringify({
              field: edit.key, value: nextValue,
              discover_reference_audio: edit.key === "video_url",
            }),
          });
          const index = currentState?.players?.findIndex((player) => player.playback_id === edit.playbackId) ?? -1;
          if (index >= 0) {
            currentState.players[index] = { ...currentState.players[index], ...result.player };
            renderSessions(currentState.players);
          }
          if (form.isConnected) form.replaceWith(line);
          showNotice(result.message);
        } catch (error) { showNotice(error.message, true); save.disabled = false; }
      });
      line.replaceWith(form);
      input.focus(); input.select();
    });
    line.append(button);
  }
  field.append(line);
  return field;
}

function numericField(label, value, step, className) {
  const wrapper = element("div");
  wrapper.append(element("label", "", label));
  const input = element("input", className);
  input.type = "number"; input.step = step; input.value = value;
  wrapper.append(input);
  return { wrapper, input };
}

function waveformPeaks(buffer) {
  const samples = buffer.getChannelData(0);
  const blockSize = 64;
  const count = Math.ceil(samples.length / blockSize);
  const low = new Float32Array(count);
  const high = new Float32Array(count);
  for (let block = 0; block < count; block += 1) {
    let min = 1; let max = -1;
    for (let index = block * blockSize; index < Math.min(samples.length, (block + 1) * blockSize); index += 1) {
      min = Math.min(min, samples[index]); max = Math.max(max, samples[index]);
    }
    low[block] = min; high[block] = max;
  }
  return { blockSize, low, high };
}

function drawWaveform(canvas, buffers, peaks, viewStart, viewSeconds, shiftSeconds, cursorSeconds, cache) {
  if (!canvas.clientWidth || !canvas.clientHeight) return;
  const ratio = window.devicePixelRatio || 1;
  const width = Math.max(1, Math.round(canvas.clientWidth * ratio));
  const height = Math.max(1, Math.round(canvas.clientHeight * ratio));
  if (canvas.width !== width || canvas.height !== height) {
    canvas.width = width; canvas.height = height;
  }
  const key = `${width}|${height}|${viewStart}|${viewSeconds}|${shiftSeconds}`;
  if (cache.key !== key) {
    const background = cache.canvas;
    background.width = width; background.height = height;
    const context = background.getContext("2d");
    context.fillStyle = "#070809"; context.fillRect(0, 0, width, height);
    context.strokeStyle = "rgba(255,255,255,.1)"; context.beginPath();
    context.moveTo(0, height / 2); context.lineTo(width, height / 2); context.stroke();
    const render = (buffer, envelope, color, shift) => {
      if (!buffer) return;
      const samples = buffer.getChannelData(0);
      const { blockSize, low, high } = envelope;
      const samplesPerPixel = buffer.sampleRate * viewSeconds / width;
      const firstSample = (viewStart - shift) * buffer.sampleRate;
      context.strokeStyle = color; context.lineWidth = ratio; context.beginPath();
      for (let x = 0; x < width; x += 1) {
        const start = Math.max(0, Math.floor(firstSample + x * samplesPerPixel));
        const end = Math.min(samples.length, Math.ceil(firstSample + (x + 1) * samplesPerPixel));
        if (end <= start) continue;
        let min = 1; let max = -1;
        const firstWhole = Math.ceil(start / blockSize) * blockSize;
        const lastWhole = Math.floor(end / blockSize) * blockSize;
        for (let index = start; index < Math.min(end, firstWhole); index += 1) {
          min = Math.min(min, samples[index]); max = Math.max(max, samples[index]);
        }
        for (let index = firstWhole; index < lastWhole; index += blockSize) {
          min = Math.min(min, low[index / blockSize]);
          max = Math.max(max, high[index / blockSize]);
        }
        for (let index = Math.max(firstWhole, lastWhole); index < end; index += 1) {
          min = Math.min(min, samples[index]); max = Math.max(max, samples[index]);
        }
        context.moveTo(x, (1 + min) * height / 2);
        context.lineTo(x, (1 + max) * height / 2);
      }
      context.stroke();
    };
    render(buffers.reference, peaks.reference, "#8db6ff", 0);
    render(buffers.replacement, peaks.replacement, "#ff5b21", shiftSeconds);
    cache.key = key;
  }
  const context = canvas.getContext("2d");
  context.drawImage(cache.canvas, 0, 0);
  if (Number.isFinite(cursorSeconds) && cursorSeconds >= viewStart && cursorSeconds <= viewStart + viewSeconds) {
    const cursorX = ((cursorSeconds - viewStart) / viewSeconds) * width;
    context.strokeStyle = "#f2efe8"; context.lineWidth = 1.5 * ratio;
    context.beginPath(); context.moveTo(cursorX, 0); context.lineTo(cursorX, height); context.stroke();
    context.fillStyle = "#f2efe8"; context.beginPath();
    context.moveTo(cursorX - 7 * ratio, 0);
    context.lineTo(cursorX + 7 * ratio, 0);
    context.lineTo(cursorX, 9 * ratio);
    context.closePath(); context.fill();
  }
}

function rawBlock(title, value) {
  const block = element("div", "raw-block");
  const heading = element("h4", "", title);
  const copy = element("button", "copy-button", "Copy JSON");
  copy.type = "button"; copy.addEventListener("click", () => copyText(value));
  heading.append(" ", copy);
  block.append(heading, element("pre", "", jsonText(value)));
  return block;
}

function createAlignmentLab(player, offsetInput) {
  const lab = element("div", "tab-panel alignment");
  lab.append(element("h4", "", "Manual alignment lab"));
  const needsVideo = !metadataValue(player, "video_url", "videoUrl", "videoURL", "stream_url", "streamUrl")
    && !metadataValue(player, "reference_audio_url", "referenceAudioUrl", "referenceAudio");
  const note = element("p", "alignment-note", "Blue is the player/reference audio; orange is the replacement. Drag or tap the waveform to seek. Sample length is used when loading; use Waveform zoom to inspect the loaded sample. The offset slider moves both the orange waveform and its browser audio preview."
    + (needsVideo ? " Add a Video URL in Links & credentials to load the blue waveform." : ""));
  const controls = element("div", "alignment-controls");
  const defaultPosition = player.sync_result?.measurements?.[0]?.position || 60;
  const position = numericField("Timeline position · seconds", Number(defaultPosition).toFixed(3), "0.001", "");
  const seconds = numericField("Sample length · seconds", "8", "1", "");
  position.input.min = "0"; seconds.input.min = "1"; seconds.input.max = "30";
  const load = element("button", "button primary", "Load both waveforms");
  controls.append(position.wrapper, seconds.wrapper, load);
  const offsetSlider = element("div", "offset-slider-control");
  const sliderLabel = element("label");
  const sliderValue = element("strong", "", `${formatOffset(offsetInput.value)} s`);
  sliderLabel.append("Preview offset", sliderValue);
  const slider = element("input");
  slider.type = "range"; slider.step = "0.001";
  const setSliderBounds = () => {
    const value = Number(offsetInput.value) || 0;
    slider.min = String(Math.min(-5, Math.floor(value) - 1));
    slider.max = String(Math.max(5, Math.ceil(value) + 1));
    slider.value = String(value);
    sliderValue.textContent = `${formatOffset(value)} s`;
  };
  setSliderBounds();
  offsetSlider.append(sliderLabel, slider);
  const zoomControl = element("div", "zoom-slider-control");
  const zoomLabel = element("label");
  const zoomValue = element("strong", "", "1.0×");
  zoomLabel.append("Waveform zoom", zoomValue);
  const zoomSlider = element("input");
  zoomSlider.type = "range"; zoomSlider.min = "1"; zoomSlider.max = "16";
  zoomSlider.step = "0.1"; zoomSlider.value = "1"; zoomSlider.disabled = true;
  zoomSlider.setAttribute("aria-label", "Waveform zoom");
  zoomControl.append(zoomLabel, zoomSlider);
  const canvas = element("canvas", "alignment-waveform");
  canvas.tabIndex = 0;
  canvas.setAttribute("role", "slider");
  canvas.setAttribute("aria-label", "Waveform playback cursor");
  canvas.setAttribute("aria-valuemin", "0");
  canvas.setAttribute("aria-valuemax", "0");
  canvas.setAttribute("aria-valuenow", "0");
  canvas.setAttribute("aria-disabled", "true");
  canvas.title = "Tap or drag to seek both previews";
  const audioGrid = element("div", "alignment-audio");
  const referenceWrap = element("div"); referenceWrap.append(element("label", "", "Reference / player audio"));
  const replacementWrap = element("div"); replacementWrap.append(element("label", "", "Replacement audio track"));
  const referenceAudio = element("audio"); referenceAudio.controls = true;
  const replacementAudio = element("audio"); replacementAudio.controls = true;
  referenceWrap.append(referenceAudio); replacementWrap.append(replacementAudio);
  audioGrid.append(referenceWrap, replacementWrap);
  const status = element("p", "alignment-note", "No comparison loaded yet.");
  const buffers = { reference: null, replacement: null };
  const peaks = { reference: null, replacement: null };
  const waveformCache = { canvas: document.createElement("canvas"), key: null };
  let loadedDuration = 0;
  let viewStart = 0;
  let cursorTime = 0;
  let animationFrame = null;
  let shiftedReplacementUrl = "";
  let replacementShiftTimer = null;
  let replacementShiftVersion = 0;
  let resumeReplacementAfterShift = false;
  const pendingSeeks = new WeakMap();
  const visibleSeconds = () => loadedDuration / Number(zoomSlider.value);
  const maxViewStart = () => Math.max(0, loadedDuration - visibleSeconds());
  const clampViewStart = (value) => Math.max(0, Math.min(maxViewStart(), value));
  const followCursor = () => {
    if (cursorTime < viewStart) viewStart = clampViewStart(cursorTime);
    else if (cursorTime > viewStart + visibleSeconds()) {
      viewStart = clampViewStart(cursorTime - visibleSeconds());
    }
  };
  const redraw = () => {
    if (loadedDuration) {
      drawWaveform(canvas, buffers, peaks, viewStart, visibleSeconds(), Number(offsetInput.value) || 0, cursorTime, waveformCache);
      zoomValue.textContent = `${Number(zoomSlider.value).toFixed(1)}× · ${viewStart.toFixed(1)}–${Math.min(loadedDuration, viewStart + visibleSeconds()).toFixed(1)} s`;
    }
    canvas.setAttribute("aria-valuemax", String(loadedDuration));
    canvas.setAttribute("aria-valuenow", String(cursorTime.toFixed(3)));
  };
  const seek = (value) => {
    if (!loadedDuration) return;
    cursorTime = Math.max(0, Math.min(loadedDuration, Number(value) || 0));
    followCursor();
    [referenceAudio, replacementAudio].forEach((audio) => {
      try {
        const audioTime = Math.min(cursorTime, Number.isFinite(audio.duration) ? audio.duration : cursorTime);
        if (!Number.isFinite(audio.currentTime) || Math.abs(audio.currentTime - audioTime) > 0.001) {
          pendingSeeks.set(audio, audioTime);
          audio.currentTime = audioTime;
        }
      }
      catch (_) {
        pendingSeeks.delete(audio); // Media metadata may still be loading.
      }
    });
    redraw();
  };
  const applyReplacementOffset = () => {
    if (!buffers.replacement) return;
    clearTimeout(replacementShiftTimer);
    replacementShiftTimer = null;
    resumeReplacementAfterShift ||= !replacementAudio.paused;
    const version = ++replacementShiftVersion;
    const nextUrl = URL.createObjectURL(shiftedWav(buffers.replacement, offsetInput.value));
    objectUrls.add(nextUrl);
    const previousUrl = shiftedReplacementUrl;
    shiftedReplacementUrl = nextUrl;
    replacementAudio.addEventListener("loadedmetadata", () => {
      if (version !== replacementShiftVersion) return;
      seek(cursorTime);
      if (resumeReplacementAfterShift) {
        resumeReplacementAfterShift = false;
        replacementAudio.play().catch(() => {});
      }
    }, { once: true });
    replacementAudio.src = nextUrl;
    replacementAudio.dataset.appliedOffset = String(Number(offsetInput.value) || 0);
    releaseObjectUrl(previousUrl);
  };
  const queueReplacementOffset = () => {
    if (!buffers.replacement) return;
    clearTimeout(replacementShiftTimer);
    replacementShiftTimer = setTimeout(applyReplacementOffset, 75);
  };
  const animateCursor = () => {
    cancelAnimationFrame(animationFrame);
    const tick = () => {
      const activeAudio = !referenceAudio.paused ? referenceAudio : !replacementAudio.paused ? replacementAudio : null;
      if (!activeAudio) { redraw(); return; }
      cursorTime = activeAudio.currentTime;
      followCursor();
      redraw();
      animationFrame = requestAnimationFrame(tick);
    };
    animationFrame = requestAnimationFrame(tick);
  };
  const seekFromPointer = (event) => {
    if (!loadedDuration) return;
    const bounds = canvas.getBoundingClientRect();
    seek(viewStart + ((event.clientX - bounds.left) / bounds.width) * visibleSeconds());
  };
  canvas.addEventListener("pointerdown", (event) => {
    if (!loadedDuration) return;
    canvas.setPointerCapture(event.pointerId); seekFromPointer(event);
  });
  canvas.addEventListener("pointermove", (event) => {
    if (canvas.hasPointerCapture(event.pointerId)) seekFromPointer(event);
  });
  canvas.addEventListener("keydown", (event) => {
    if (!loadedDuration || !["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    if (event.key === "Home") seek(0);
    else if (event.key === "End") seek(loadedDuration);
    else seek(cursorTime + (event.key === "ArrowRight" ? 0.1 : -0.1));
  });
  [referenceAudio, replacementAudio].forEach((audio) => {
    audio.addEventListener("play", animateCursor);
    audio.addEventListener("seeking", () => {
      const expected = pendingSeeks.get(audio);
      pendingSeeks.delete(audio);
      if (expected !== undefined && (
        Math.abs(audio.currentTime - expected) < 0.05
        || (Number.isFinite(audio.duration) && expected > audio.duration
          && Math.abs(audio.currentTime - audio.duration) < 0.05)
      )) return;
      cursorTime = audio.currentTime; followCursor(); redraw();
    });
    audio.addEventListener("loadedmetadata", () => seek(cursorTime));
    audio.addEventListener("ended", redraw);
  });
  zoomSlider.addEventListener("input", () => {
    viewStart = clampViewStart(cursorTime - visibleSeconds() / 2);
    redraw();
  });
  new ResizeObserver(redraw).observe(canvas);
  load.addEventListener("click", async () => {
    if (needsVideo) {
      const message = "Add a Video URL in Links & credentials, then load both waveforms.";
      status.textContent = message;
      showNotice(message);
      return;
    }
    load.disabled = true; status.textContent = "Downloading and decoding both samples…";
    const query = new URLSearchParams({ position: position.input.value, seconds: seconds.input.value });
    try {
      const [reference, replacement] = await Promise.all([
        fetchAudio(`/api/dashboard/players/${encodeURIComponent(player.playback_id)}/alignment/reference.wav?${query}`),
        fetchAudio(`/api/dashboard/players/${encodeURIComponent(player.playback_id)}/alignment/replacement.wav?${query}`),
      ]);
      referenceAudio.src = reference.url;
      buffers.reference = reference.decoded; buffers.replacement = replacement.decoded;
      peaks.reference = waveformPeaks(reference.decoded);
      peaks.replacement = waveformPeaks(replacement.decoded);
      loadedDuration = Math.max(reference.decoded.duration, replacement.decoded.duration);
      viewStart = 0;
      waveformCache.key = null;
      zoomSlider.disabled = false;
      canvas.setAttribute("aria-disabled", "false");
      applyReplacementOffset();
      releaseObjectUrl(replacement.url);
      seek(0);
      status.textContent = "Loaded. Drag the cursor to seek, then slide the orange waveform until the events align. The replacement player uses the same offset.";
    } catch (error) { status.textContent = error.message; showNotice(error.message, true); }
    finally { load.disabled = false; }
  });
  offsetInput.addEventListener("input", () => {
    setSliderBounds(); redraw(); queueReplacementOffset();
  });
  slider.addEventListener("input", () => {
    offsetInput.value = Number(slider.value).toFixed(3);
    offsetInput.dispatchEvent(new Event("input"));
  });
  slider.addEventListener("change", applyReplacementOffset);
  offsetInput.addEventListener("change", applyReplacementOffset);
  lab.append(note, controls, offsetSlider, zoomControl, canvas, audioGrid, status);
  return lab;
}

function createAudioPanel(player) {
  const panel = element("div", "tab-panel");
  const layout = element("div", "audio-preview-grid");
  const stage = element("div", "preview-stage");
  stage.append(element("strong", "", "Downloaded audio segment preview"), element("p", "alignment-note", "Choose one of the two latest source segments to download, decrypt, and preview as WAV."));
  const audio = element("audio"); audio.controls = true;
  const stageStatus = element("p", "alignment-note", "No segment loaded.");
  stage.append(audio, stageStatus);
  const list = element("div", "segment-list");
  const segments = player.audio_track?.segments || [];
  segments.slice(-2).forEach((segment) => {
    const row = element("div", "segment");
    row.append(
      element("span", "", `#${segment.index}`),
      element("span", "", `${Number(segment.start).toFixed(3)}s`),
      element("span", "duration", `${Number(segment.duration).toFixed(3)}s`),
    );
    const link = element("a", "", segment.url);
    link.href = segment.url; link.target = "_blank"; link.rel = "noreferrer"; link.title = segment.url;
    const play = element("button", "button subtle compact", segment.preview_cached ? "Play cached WAV" : "Download & play");
    play.type = "button";
    play.addEventListener("click", async () => {
      play.disabled = true; stageStatus.textContent = `Preparing source segment #${segment.index}…`;
      try {
        const preview = await fetchAudio(`/api/dashboard/audio/${player.hid}/segments/${segment.index}/preview.wav`);
        audio.src = preview.url;
        stageStatus.textContent = `Segment #${segment.index} · ${preview.decoded.duration.toFixed(3)} seconds · ${preview.decoded.sampleRate} Hz`;
        audio.play().catch(() => {});
      } catch (error) { stageStatus.textContent = error.message; showNotice(error.message, true); }
      finally { play.disabled = false; }
    });
    row.append(link, play); list.append(row);
  });
  if (!segments.length) list.append(element("div", "empty", "No audio segment metadata is available."));
  layout.append(stage, list); panel.append(layout);
  requestAnimationFrame(() => { list.scrollTop = list.scrollHeight; });
  return panel;
}

function timelineSeconds(value) {
  const seconds = Math.max(0, Number(value) || 0);
  const minutes = Math.floor(seconds / 60);
  const remainder = seconds - minutes * 60;
  return minutes ? `${minutes}:${remainder.toFixed(3).padStart(6, "0")}` : `${remainder.toFixed(3)}s`;
}

function trackDuration(player) {
  const durations = player.audio_track?.metadata?.durs || player.audio_metadata?.durs || [];
  const total = durations.reduce((sum, value) => sum + (Number(value) || 0), 0);
  return total || Number(player.audio_metadata?.segment_summary?.duration_seconds) || 0;
}

function cutAction(value) {
  const action = String(value || "").toLowerCase();
  if (["audio_cut", "cut_audio", "skip_audio"].includes(action)) return "audio_cut";
  if (action === "english_bridge") return "english_bridge";
  return "mute";
}

function cutActionLabel(action) {
  return ({
    audio_cut: "Remove replacement audio",
    english_bridge: "Use English bridge",
    mute: "Leave a silent video gap",
  })[cutAction(action)];
}

function createCutsPanel(player) {
  const panel = element("div", "tab-panel cuts-panel");
  const duration = trackDuration(player);
  let cuts = (player.current_cuts || []).map((item) => ({
    start_sec: Number(item.start_sec ?? item.start ?? 0),
    end_sec: Number(item.end_sec ?? item.end ?? 0),
    duration_sec: Number(item.duration_sec ?? Math.max(0, Number(item.end_sec ?? item.end ?? 0) - Number(item.start_sec ?? item.start ?? 0))),
    action: cutAction(item.action || item.type),
  }));

  const header = element("div", "cuts-heading");
  const title = element("div");
  title.append(element("h4", "", "Cuts timeline"), element(
    "p", "alignment-note",
    "See every removed or inserted interval against the source track, then edit exact boundaries below.",
  ));
  const source = element(
    "span", `cuts-source ${player.cuts_source === "manual" ? "manual" : ""}`.trim(),
    player.cuts_source === "manual" ? "Dashboard override" : "Player-provided cuts",
  );
  header.append(title, source);

  const stats = element("div", "cut-stats");
  const timeline = element("div", "cuts-visual");
  const ruler = element("div", "cuts-ruler");
  const track = element("div", "cuts-track");
  const baseline = element("div", "cuts-baseline");
  baseline.append(element("span", "", "Replacement audio source"));
  track.append(baseline);
  timeline.append(ruler, track);

  const legend = element("div", "cuts-legend");
  [
    ["audio_cut", "Replacement removed"],
    ["english_bridge", "English audio inserted"],
    ["mute", "Silent gap"],
  ].forEach(([action, label]) => {
    const item = element("span");
    item.append(element("i", `action-${action}`), label);
    legend.append(item);
  });

  const bridge = element("div", "bridge-picker");
  const bridgeField = element("div");
  const bridgeLabel = element("label", "", "English bridge audio HID");
  const bridgeInput = element("input");
  bridgeInput.type = "text";
  bridgeInput.maxLength = 16;
  bridgeInput.placeholder = "Choose another prepared audio track";
  bridgeInput.value = player.current_bridge_hid || "";
  const listId = `bridge-${player.playback_id}`;
  bridgeInput.setAttribute("list", listId);
  const bridgeOptions = element("datalist");
  bridgeOptions.id = listId;
  (currentState?.players || []).filter((candidate) => (
    candidate.hid !== player.hid && candidate.session_id === player.session_id
  )).forEach((candidate) => {
    const option = element("option");
    const language = languageInfo(candidate.audio_metadata?.language || candidate.audio_track?.metadata?.language);
    option.value = candidate.hid;
    option.label = `${language.code} · ${candidate.audio_metadata?.media_key || "prepared track"}`;
    bridgeOptions.append(option);
  });
  bridgeField.append(bridgeLabel, bridgeInput, bridgeOptions);
  bridge.append(bridgeField, element(
    "p", "alignment-note",
    "Required only for English bridge intervals. Use the HID of another prepared track from this session.",
  ));

  const editor = element("div", "cuts-editor");
  const editorHead = element("div", "cut-row cut-row-head");
  ["Cut", "Action", "Start", "End", "Duration", ""].forEach((label) => editorHead.append(element("span", "", label)));
  const rows = element("div", "cut-rows");
  const add = element("button", "button subtle compact", "+ Add interval");
  add.type = "button";

  const status = element("p", "cuts-status");
  const actions = element("div", "action-row cuts-actions");
  const save = element("button", "button primary", "Save cuts & queue playlist");
  save.type = "button";
  const restore = element("button", "button subtle", "Restore player-provided cuts");
  restore.type = "button";
  restore.disabled = player.cuts_source !== "manual";
  actions.append(save, restore);

  const validate = () => {
    const normalized = cuts.map((item, index) => ({
      index,
      start_sec: Number(item.start_sec),
      end_sec: Number(item.end_sec),
      duration_sec: Number(item.duration_sec),
      action: cutAction(item.action),
    }));
    const invalid = normalized.find((item) => (
      !Number.isFinite(item.start_sec) || !Number.isFinite(item.end_sec) || !Number.isFinite(item.duration_sec)
      || item.start_sec < 0 || item.end_sec <= item.start_sec || item.duration_sec <= 0
      || item.end_sec > 86400 || item.duration_sec > 86400
    ));
    if (invalid) return { error: `Cut ${invalid.index + 1} needs a start ≥ 0, an end after its start, and a positive duration.` };
    const sorted = [...normalized].sort((a, b) => a.start_sec - b.start_sec || a.end_sec - b.end_sec);
    const bridgeHid = bridgeInput.value.trim();
    if (bridgeHid && !/^[0-9a-f]{16}$/.test(bridgeHid)) return { error: "The bridge HID must contain 16 lowercase hexadecimal characters." };
    if (sorted.some((item) => item.action === "english_bridge") && !bridgeHid) {
      return { error: "Choose an English bridge track for the bridge intervals." };
    }
    return {
      cuts: sorted.map((item) => ({
        start_sec: item.start_sec,
        end_sec: item.end_sec,
        duration_sec: item.duration_sec,
        action: item.action,
      })),
      bridge_hid: bridgeHid,
    };
  };

  const renderVisual = () => {
    const finiteEnds = cuts.map((item) => Number(item.end_sec)).filter(Number.isFinite);
    const scale = Math.max(duration, ...finiteEnds, 1);
    ruler.replaceChildren();
    for (let index = 0; index <= 4; index += 1) {
      const tick = element("span", "", timelineSeconds(scale * index / 4));
      tick.style.left = `${index * 25}%`;
      ruler.append(tick);
    }
    track.querySelectorAll(".cut-block,.cuts-empty").forEach((node) => node.remove());
    cuts.forEach((item, index) => {
      const start = Number(item.start_sec);
      const end = Number(item.end_sec);
      if (!Number.isFinite(start) || !Number.isFinite(end) || end <= start) return;
      const block = element("button", `cut-block action-${cutAction(item.action)}`);
      block.type = "button";
      block.style.left = `${Math.max(0, Math.min(100, start / scale * 100))}%`;
      block.style.width = `${Math.max(.7, Math.min(100 - start / scale * 100, (end - start) / scale * 100))}%`;
      block.title = `Cut ${index + 1}: ${cutActionLabel(item.action)} · ${timelineSeconds(start)}–${timelineSeconds(end)}`;
      block.setAttribute("aria-label", block.title);
      block.append(element("strong", "", String(index + 1)), element("span", "", `${Number(item.duration_sec).toFixed(3)}s`));
      block.addEventListener("click", () => rows.children[index]?.querySelector("input")?.focus());
      track.append(block);
    });
    if (!cuts.length) track.append(element("span", "cuts-empty", "No cuts — the replacement track plays continuously."));

    const audioRemoved = cuts.filter((item) => cutAction(item.action) === "audio_cut")
      .reduce((sum, item) => sum + Math.max(0, Number(item.duration_sec) || 0), 0);
    const gapsAdded = cuts.filter((item) => cutAction(item.action) !== "audio_cut")
      .reduce((sum, item) => sum + Math.max(0, Number(item.duration_sec) || 0), 0);
    const estimated = Math.max(0, duration - audioRemoved + gapsAdded);
    const stat = (value, label) => {
      const item = element("div");
      item.append(element("strong", "", value), element("span", "", label));
      return item;
    };
    stats.replaceChildren(
      stat(`${cuts.length}`, "Intervals"),
      stat(timelineSeconds(audioRemoved), "Audio removed"),
      stat(timelineSeconds(gapsAdded), "Video gaps"),
      stat(duration ? timelineSeconds(estimated) : "—", "Estimated result"),
    );
    const checked = validate();
    save.disabled = Boolean(checked.error);
    status.textContent = checked.error || (
      player.pending_cuts_playlist
        ? "Saved revision is waiting for the next top-level HLS playlist request. Restart playback if the player does not reload it."
        : "Changes are previewed locally until you save. A playlist reload is required when the interval structure changes."
    );
    status.classList.toggle("error", Boolean(checked.error));
  };

  const renderRows = () => {
    const fragment = document.createDocumentFragment();
    cuts.forEach((item, index) => {
      const row = element("div", "cut-row");
      row.append(element("strong", "cut-index", String(index + 1)));
      const action = element("select");
      ["audio_cut", "english_bridge", "mute"].forEach((value) => {
        const option = element("option", "", cutActionLabel(value));
        option.value = value; option.selected = cutAction(item.action) === value; action.append(option);
      });
      action.setAttribute("aria-label", `Cut ${index + 1} action`);
      const start = element("input"); start.type = "number"; start.min = "0"; start.max = "86400"; start.step = "0.001";
      start.value = Number(item.start_sec).toFixed(3); start.setAttribute("aria-label", `Cut ${index + 1} start`);
      const end = element("input"); end.type = "number"; end.min = "0"; end.max = "86400"; end.step = "0.001";
      end.value = Number(item.end_sec).toFixed(3); end.setAttribute("aria-label", `Cut ${index + 1} end`);
      const span = element("input", "cut-duration"); span.type = "number"; span.min = "0.001"; span.max = "86400"; span.step = "0.001";
      span.value = Number(item.duration_sec).toFixed(3); span.setAttribute("aria-label", `Cut ${index + 1} duration`);
      const remove = element("button", "button danger compact", "Remove"); remove.type = "button";
      action.addEventListener("change", () => { item.action = action.value; renderVisual(); });
      start.addEventListener("input", () => {
        item.start_sec = start.value === "" ? NaN : Number(start.value);
        renderVisual();
      });
      end.addEventListener("input", () => {
        item.end_sec = end.value === "" ? NaN : Number(end.value);
        renderVisual();
      });
      span.addEventListener("input", () => {
        item.duration_sec = span.value === "" ? NaN : Number(span.value);
        renderVisual();
      });
      remove.addEventListener("click", () => { cuts.splice(index, 1); renderRows(); });
      row.append(action, start, end, span, remove); fragment.append(row);
    });
    rows.replaceChildren(fragment);
    renderVisual();
  };

  add.addEventListener("click", () => {
    const previousEnd = cuts.reduce((latest, item) => Math.max(latest, Number(item.end_sec) || 0), 0);
    const start = duration && previousEnd >= duration ? Math.max(0, duration - 5) : previousEnd;
    cuts.push({ start_sec: start, end_sec: start + 5, duration_sec: 5, action: "audio_cut" });
    renderRows();
    rows.lastElementChild?.querySelector("select")?.focus();
  });
  bridgeInput.addEventListener("input", renderVisual);
  save.addEventListener("click", async () => {
    const checked = validate();
    if (checked.error) { showNotice(checked.error, true); return; }
    save.disabled = true;
    try {
      const result = await api(`/api/dashboard/players/${encodeURIComponent(player.playback_id)}/cuts`, {
        method: "PATCH", body: JSON.stringify(checked),
      });
      const index = currentState?.players?.findIndex((item) => item.playback_id === player.playback_id) ?? -1;
      if (index >= 0) currentState.players[index] = { ...currentState.players[index], ...result.player };
      sessionSignature = ""; renderSessions(currentState.players); showNotice(result.message);
    } catch (error) { showNotice(error.message, true); save.disabled = false; }
  });
  restore.addEventListener("click", async () => {
    restore.disabled = true;
    try {
      const result = await api(`/api/dashboard/players/${encodeURIComponent(player.playback_id)}/cuts/restore`, { method: "POST" });
      const index = currentState?.players?.findIndex((item) => item.playback_id === player.playback_id) ?? -1;
      if (index >= 0) currentState.players[index] = { ...currentState.players[index], ...result.player };
      sessionSignature = ""; renderSessions(currentState.players); showNotice(result.message);
    } catch (error) { showNotice(error.message, true); restore.disabled = false; }
  });

  editor.append(editorHead, rows, add);
  panel.append(header, stats, timeline, legend, bridge, editor, status, actions);
  renderRows();
  return panel;
}

function createNetworkPanel(player) {
  const panel = element("div", "tab-panel");
  const audioMeta = player.audio_track?.metadata || player.audio_metadata || {};
  const fields = element("div", "data-grid");
  [
    ["Video URL", metadataValue(player, "video_url", "videoUrl", "videoURL", "stream_url", "streamUrl"), true, "video_url"],
    ["Reference audio URL", metadataValue(player, "reference_audio_url", "referenceAudioUrl", "referenceAudio"), true, "reference_audio_url"],
    ["VPS host", metadataValue(player, "vpsHost", "vps_host"), true, "vpsHost"],
    ["VPS access", metadataValue(player, "vpsAccess", "vps_access"), false, "vpsAccess"],
    ["Audio base URL", metadataValue(player, "base_url", "baseUrl") || audioMeta.base_url, true, "base_url"],
    ["Provider", metadataValue(player, "provider"), false, "provider"],
    ["Server", metadataValue(player, "server"), false, "server"],
    ["Audio headers", jsonText(player.metadata_edits?.audio_headers || audioMeta.headers || metadataValue(player, "audio_headers", "audioHeaders") || {}), false, "audio_headers", true],
    ["Video headers", jsonText(metadataValue(player, "video_headers", "videoHeaders") || {}), false, "video_headers", true],
  ].forEach(([label, value, link, key, json]) => {
    fields.append(makeField(label, value, link, { playbackId: player.playback_id, key, json }));
  });
  panel.append(fields);
  return panel;
}

function createDetailsPanel(player, lang, source, registration) {
  const panel = element("div", "tab-panel");
  const fields = element("div", "data-grid");
  [
    ["Track language", lang.name, false], ["Offset provenance", source.label, false],
    ["Database lookup", databaseLookupInfo(player), false],
    ["Audio playlist setup", registration, false],
    ["Last HLS request type", player.last_request_kind || "None yet", false],
    ["Last offset received in an HLS request", player.request_count ? `${formatOffset(player.last_requested_offset)} s` : "No HLS request received yet", false],
    ["Cache key", player.cache_key, false, "cache_key"],
    ["Video fingerprint", metadataValue(player, "video_fingerprint", "videoFingerprint"), false, "video_fingerprint"],
    ["Audio fingerprint", metadataValue(player, "audio_fingerprint", "audioFingerprint", "source_fingerprint"), false, "audio_fingerprint"],
    ["Resolution", player.resolution ? `${player.resolution}p` : null, false, "resolution"],
  ].forEach(([label, value, link, key]) => {
    fields.append(makeField(label, value, link,
      key ? { playbackId: player.playback_id, key } : null));
  });
  panel.append(fields);
  return panel;
}

function createRawPanel(player) {
  const panel = element("div", "tab-panel raw-columns");
  panel.append(
    rawBlock("Complete audio metadata", player.audio_track || player.audio_metadata || {}),
    rawBlock("Complete prepare/cache request", player.prepare_request || {}),
    rawBlock("Complete sync input", player.sync_metadata || {}),
    rawBlock("Complete sync output", player.sync_result || {}),
    rawBlock("Complete playback state", player),
  );
  return panel;
}

function createTrack(player) {
  const track = element("article", "track");
  track.dataset.playbackId = player.playback_id;
  const head = element("div", "track-head");
  const lang = languageInfo(player.audio_metadata?.language || player.audio_track?.metadata?.language);
  const language = element("div", "language");
  const languageText = element("div");
  languageText.append(element("strong", "", lang.name), element("small", "", `HID ${player.hid}`));
  language.append(element("span", "language-mark", lang.code), languageText);
  const registration = player.cached_audio
    ? "Existing fresh audio playlist reused from the sidecar cache"
    : "Audio playlist received from ToastFlix and stored by the sidecar";
  const requestState = player.request_count > 0
    ? (player.active ? { text: "player stream active", style: "live" } : { text: "no recent player requests", style: "" })
    : (player.ended_at ? { text: "playback ended before streaming", style: "" } : { text: "prepared · waiting for player", style: "" });
  head.append(language, element("span", `track-state ${requestState.style}`.trim(), requestState.text));

  const source = sourceInfo(player);
  const offsetHero = element("div", "offset-hero");
  const offsetValue = element("div", "offset-value");
  offsetValue.append(element("span", "", "Effective audio offset"));
  const offsetNumber = element("div", "offset-number", formatOffset(player.current_offset));
  offsetNumber.append(element("small", "", " s"));
  offsetValue.append(offsetNumber);
  const description = element("div");
  description.append(element("strong", `offset-source ${source.style}`.trim(), source.label), element("p", "", source.detail));
  offsetHero.append(offsetValue, description);

  const candidates = element("div", "candidate-list");
  const requestCandidateOffset = Number(player.offset_candidates?.request?.offset ?? 0);
  const requestCandidateName = Math.abs(requestCandidateOffset) < 0.0005
    ? "Sidecar initial zero default"
    : "Offset carried by incoming HLS URL";
  const candidateNames = { request: requestCandidateName, cached: "Cached database value", calculated: "Sidecar calculated value", manual: "Manual value" };
  Object.entries(player.offset_candidates || {}).forEach(([name, candidate]) => {
    const selected = name === player.control_source;
    const row = element("button", `candidate ${selected ? "selected" : ""}`.trim());
    row.type = "button"; row.setAttribute("aria-pressed", String(selected));
    row.title = selected ? "This offset is currently in use" : `Use ${candidateNames[name] || name}`;
    row.append(
      element("span", "candidate-label", `${candidateNames[name] || name} · ${candidate.source || ""}`),
      element("strong", "", `${formatOffset(candidate.offset)} s`),
    );
    if (selected) row.append(element("span", "candidate-current", "In use"));
    else if (name !== "manual") row.addEventListener("click", () => restorePlayer(player.playback_id, name));
    candidates.append(row);
  });

  const controls = element("div", "edit-controls");
  const offset = numericField("Manual offset · seconds", Number(player.current_offset).toFixed(3), "0.001", "offset-input");
  const rate = numericField("Playback rate", Number(player.current_rate).toFixed(9), "0.000001", "rate-input");
  controls.append(offset.wrapper, rate.wrapper);
  const nudges = element("div", "nudge-row");
  [-1, -0.5, -0.1, -0.01, 0.01, 0.1, 0.5, 1].forEach((amount) => {
    const button = element("button", "button", `${amount > 0 ? "+" : ""}${amount}s`);
    button.type = "button";
    button.addEventListener("click", () => { offset.input.value = (Number(offset.input.value) + amount).toFixed(3); offset.input.dispatchEvent(new Event("input")); });
    nudges.append(button);
  });
  const actions = element("div", "action-row");
  const save = element("button", "button primary", "Apply manual offset");
  save.type = "button"; save.addEventListener("click", () => editPlayer(player.playback_id, offset.input.value, rate.input.value));
  const upload = element(
    "button", "button subtle", `Upload selected ${formatOffset(player.current_offset)}s`
  );
  upload.type = "button";
  upload.title = "Review the selected value, offset identity, and VPS destination before uploading; missing details will be requested";
  upload.addEventListener("click", () => uploadPlayer(player.playback_id));
  actions.append(save, upload);
  const applyState = element("p", `apply-state ${player.pending_player_request ? "pending" : ""}`,
    player.pending_player_request
      ? "Waiting for the next HLS request to use this revision."
      : `Applied revision ${player.revision} · last ${player.last_request_kind || "request"} ${formatTime(player.last_request_at)} · ${player.request_count} total requests.`);

  const tabs = element("div", "tabs");
  const panels = {
    details: createDetailsPanel(player, lang, source, registration),
    audio: createAudioPanel(player),
    alignment: createAlignmentLab(player, offset.input),
    cuts: createCutsPanel(player),
    network: createNetworkPanel(player),
    raw: createRawPanel(player),
  };
  const selected = selectedTabs.get(player.playback_id) || "details";
  Object.entries({ details: "Overview", audio: "Audio preview", alignment: "Alignment lab", cuts: `Cuts · ${(player.current_cuts || []).length}`, network: "Links & credentials", raw: "Complete metadata" }).forEach(([key, label]) => {
    const button = element("button", `tab-button ${selected === key ? "selected" : ""}`, label);
    button.type = "button"; button.dataset.tab = key;
    button.addEventListener("click", () => {
      selectedTabs.set(player.playback_id, key);
      tabs.querySelectorAll(".tab-button").forEach((item) => item.classList.toggle("selected", item === button));
      Object.entries(panels).forEach(([name, panel]) => { panel.hidden = name !== key; });
    });
    tabs.append(button);
  });
  Object.entries(panels).forEach(([key, panel]) => { panel.hidden = key !== selected; });
  track.append(head, offsetHero, candidates, controls, nudges, actions, applyState, tabs, ...Object.values(panels));
  return track;
}

function groupPlayers(players) {
  const groups = new Map();
  players.forEach((player) => {
    const mediaKey = player.audio_metadata?.media_key || player.audio_track?.metadata?.media_key || "Unidentified media";
    const key = `${player.session_id}:${mediaKey}`;
    if (!groups.has(key)) groups.set(key, { key, mediaKey, sessionId: player.session_id, tracks: [], updatedAt: 0, active: false });
    const group = groups.get(key);
    group.tracks.push(player); group.updatedAt = Math.max(group.updatedAt, player.updated_at || 0); group.active ||= Boolean(player.active);
  });
  return [...groups.values()].sort((a, b) => b.updatedAt - a.updatedAt);
}

function createSession(group, isHistory = false) {
  const card = element("article", "session-card panel");
  const header = element("header", "session-header");
  const title = element("div", "session-title");
  title.append(element("span", "", `${isHistory ? "Previous" : "Current"} media · session ${group.sessionId}`), mediaTitle(group.mediaKey));
  const summary = element("div", "session-summary");
  const streamActive = group.tracks.some((track) => track.request_count > 0 && track.active);
  summary.append(element(
    "span", `session-state ${streamActive ? "live" : ""}`.trim(),
    streamActive ? "Player stream active" : group.active ? "Prepared media session" : "Playback ended",
  ));
  summary.append(element("span", "session-meta", `${group.tracks.length} audio track${group.tracks.length === 1 ? "" : "s"}`));
  const resolutions = [...new Set(group.tracks.map((track) => track.resolution).filter(Boolean))];
  if (resolutions.length) summary.append(element("span", "session-meta", resolutions.map((item) => `${item}p`).join(" / ")));
  header.append(title, summary);
  const tracks = element("div", "tracks");
  group.tracks.sort((a, b) => String(a.audio_metadata?.language).localeCompare(String(b.audio_metadata?.language))).forEach((track) => tracks.append(createTrack(track)));
  card.append(header, tracks);
  return card;
}

function renderSessions(players) {
  const signature = jsonText(players.map((player) => [
    player.playback_id, player.current_offset, player.current_rate, player.control_source,
    player.revision, player.applied_revision, player.cache_key, player.cache_hit,
    player.cache_source, player.active, player.ended_at, Boolean(player.request_count),
    player.sync_result, player.sync_metadata, player.metadata_edits, player.current_cuts,
    player.current_bridge_hid, player.cuts_source, player.cuts_revision,
    player.cuts_applied_revision, player.pending_cuts_playlist,
  ]));
  if (signature === sessionSignature) return;
  sessionSignature = signature;
  const groups = groupPlayers(players);
  const active = groups.filter((group) => group.active);
  const history = groups.filter((group) => !group.active);
  ui.sessions.replaceChildren(...active.map((group) => createSession(group, false)));
  ui.noSessions.hidden = active.length > 0;
  ui.historySection.hidden = history.length === 0;
  ui.history.replaceChildren(...history.map((group) => createSession(group, true)));
}

function provenance(record) {
  if (record.details?.custom) return "Manual edit · automatic value retained";
  if (record.details?.restored_source === "request") return "Restored sidecar fallback";
  if (record.details?.restored_source === "cached") return "Restored database cache";
  if (record.details?.restored_source === "calculated") return "Restored sidecar calculation";
  if (record.details?.cached) return `Database cache · ${record.details.cache_source || "unknown"}`;
  if (record.status === "ok") return "Sidecar calculation";
  if (record.status === "incompatible") return "Automatic sync failed · no reliable offset";
  return record.status || "Unknown";
}

function renderOffsets(records) {
  const signature = jsonText(records.map((record) => [record.cache_key, record.updated_at, record.offset_seconds, record.rate, record.upload_context]));
  if (signature === offsetSignature) return;
  offsetSignature = signature;
  const rows = records.map((record) => {
    const row = element("tr");
    const media = element("td", "media-cell");
    const title = element("strong");
    const link = mediaLink(record.media_key);
    if (link) { const anchor = element("a", "", record.media_key); anchor.href = link; anchor.target = "_blank"; anchor.rel = "noreferrer"; title.append(anchor); }
    else title.textContent = record.media_key;
    media.append(title, element("small", "", record.cache_key));
    const offsetCell = element("td");
    const offset = element("input"); offset.type = "number"; offset.step = "0.001"; offset.value = Number(record.offset_seconds || 0).toFixed(3); offsetCell.append(offset);
    const rateCell = element("td");
    const rate = element("input"); rate.type = "number"; rate.step = "0.000001"; rate.value = Number(record.rate || 1).toFixed(9); rateCell.append(rate);
    const source = element("td", "", provenance(record));
    const actions = element("td", "row-actions");
    const save = element("button", "button primary compact", "Save edit");
    save.type = "button"; save.addEventListener("click", () => editOffset(record.cache_key, offset.value, rate.value));
    const automatic = record.details?.automatic_result;
    const restore = element(
      "button", "button subtle compact",
      automatic ? `Restore automatic ${formatOffset(automatic.offset)}s` : "No automatic value"
    );
    restore.type = "button"; restore.disabled = !automatic;
    restore.addEventListener("click", () => restoreOffset(record.cache_key));
    const upload = element("button", "button subtle compact", "Upload");
    const canUpload = Boolean(currentState?.server?.remote_db_configured || record.upload_context?.vpsHost);
    upload.type = "button";
    upload.title = currentState?.server?.remote_db_configured
      ? `Upload to ${currentState.server.configured_offset_api_url}`
      : canUpload ? `Upload through ${record.upload_context.vpsHost}` : "Upload; missing VPS connection details will be requested";
    upload.addEventListener("click", () => uploadOffset(record.cache_key));
    const inspect = element("button", "button subtle compact", "Inspect JSON");
    inspect.type = "button"; inspect.addEventListener("click", () => openJsonDrawer(
      `Offset · ${record.cache_key}`, "Complete offset record", record
    ));
    actions.append(save, restore, upload, inspect);
    row.append(media, element("td", "", record.resolution ? `${record.resolution}p` : "—"), offsetCell, rateCell, source, actions);
    return row;
  });
  ui.offsets.replaceChildren(...rows); ui.noOffsets.hidden = rows.length > 0;
}

function renderDecisions(events) {
  const signature = jsonText(events);
  if (signature === decisionSignature) return;
  decisionSignature = signature;
  const rows = events.map((event) => {
    const row = element("tr");
    const detail = { ...event };
    delete detail.at; delete detail.kind; delete detail.playback_id;
    const details = element("td", "decision-details");
    const serialized = jsonText(detail);
    const code = element("code", "", serialized); code.title = serialized;
    details.append(code);
    const copyCell = element("td");
    const copy = element("button", "copy-button", "Copy event");
    copy.type = "button"; copy.addEventListener("click", (clickEvent) => {
      clickEvent.stopPropagation(); copyText(event);
    }); copyCell.append(copy);
    row.append(
      element("td", "mono", formatTime(event.at)), element("td", "mono", event.kind || "—"),
      element("td", "mono", event.playback_id || "—"), details, copyCell,
    );
    row.addEventListener("click", () => openJsonDrawer(
      `Decision · ${event.kind || "event"}`, "Complete internal event", event
    ));
    return row;
  });
  ui.decisions.replaceChildren(...rows); ui.noDecisions.hidden = rows.length > 0;
}

function mergeActivity(records) {
  records.forEach((record) => activityRecords.set(Number(record.id), record));
  if (activityRecords.size > 1000) {
    const ids = [...activityRecords.keys()].sort((a, b) => b - a);
    ids.slice(1000).forEach((id) => activityRecords.delete(id));
  }
}

function renderTraffic() {
  const search = ui.trafficSearch.value.trim().toLowerCase();
  const records = [...activityRecords.values()].sort((a, b) => b.id - a).filter((record) => {
    const category = String(record.category || "");
    if (activityFilter === "stremio" && !category.startsWith("stremio-")) return false;
    if (activityFilter === "media" && !["audio-source", "sync-source"].includes(category)) return false;
    if (activityFilter === "database" && category !== "offset-db") return false;
    if (activityFilter === "dashboard" && category !== "dashboard") return false;
    if (!search) return true;
    return [record.url, record.method, record.category, record.status, record.error].join(" ").toLowerCase().includes(search);
  });
  const fragment = document.createDocumentFragment();
  records.forEach((record) => {
    const row = element("tr"); row.dataset.activityId = record.id;
    const status = element("td", Number(record.status) >= 400 || record.error ? "http-bad" : "http-good", record.status || (record.error ? "ERR" : "—"));
    row.append(
      element("td", "mono", formatTime(record.created_at)),
      element("td", `flow ${record.direction}`, record.direction === "inbound" ? "↙ IN + RESPONSE" : "↗ OUT + RESPONSE"),
      element("td", "", record.category), element("td", "mono", record.method || "—"),
      element("td", "url", record.url || "—"), status,
      element("td", "mono", record.duration_ms == null ? "—" : `${Number(record.duration_ms).toFixed(1)}ms`),
      element("td", "mono", `${formatBytes(record.request_bytes)} / ${formatBytes(record.response_bytes)}`),
    );
    row.addEventListener("click", () => openActivity(record.id)); fragment.append(row);
  });
  ui.traffic.replaceChildren(fragment); ui.noTraffic.hidden = records.length > 0;
}

function drawerSection(title, value) {
  const section = element("section", "drawer-section");
  const heading = element("h3", "", title);
  const copy = element("button", "copy-button", "Copy"); copy.type = "button"; copy.addEventListener("click", () => copyText(value));
  heading.append(" ", copy); section.append(heading, element("pre", "", typeof value === "string" ? value : jsonText(value)));
  return section;
}

async function openActivity(activityId) {
  ui.drawer.classList.add("open"); ui.drawer.setAttribute("aria-hidden", "false");
  ui.drawerTitle.textContent = `Transaction #${activityId}`;
  ui.drawerContent.replaceChildren(element("div", "empty", "Loading complete request and response…"));
  try {
    openTransaction = await api(`/api/dashboard/activity/${activityId}`);
    const summary = {
      created_at: openTransaction.created_at, direction: openTransaction.direction,
      category: openTransaction.category, method: openTransaction.method, url: openTransaction.url,
      status: openTransaction.status, duration_ms: openTransaction.duration_ms, error: openTransaction.error,
    };
    ui.drawerContent.replaceChildren(
      drawerSection("Transaction", summary),
      drawerSection("Request headers", openTransaction.request_headers || {}),
      drawerSection("Request body", openTransaction.request_body || null),
      drawerSection("Response headers", openTransaction.response_headers || {}),
      drawerSection("Response body", openTransaction.response_body || null),
      drawerSection("Runtime metadata", openTransaction.metadata || {}),
    );
  } catch (error) { ui.drawerContent.replaceChildren(element("div", "error", error.message)); }
}

function closeDrawer() { ui.drawer.classList.remove("open"); ui.drawer.setAttribute("aria-hidden", "true"); }

function openJsonDrawer(title, sectionTitle, value) {
  openTransaction = value;
  ui.drawer.classList.add("open"); ui.drawer.setAttribute("aria-hidden", "false");
  ui.drawerTitle.textContent = title;
  ui.drawerContent.replaceChildren(drawerSection(sectionTitle, value));
}

function renderServer(server) {
  ui.serverTime.textContent = formatTime(server.server_time, true);
  if (server.remote_db_configured) ui.remote.textContent = `Configured · ${server.configured_offset_api_url}`;
  else if (server.dynamic_vps_hosts?.length) ui.remote.textContent = `Playback vpsHost · ${server.dynamic_vps_hosts.join(", ")}`;
  else ui.remote.textContent = "No destination yet";
  ui.autoUpload.checked = Boolean(server.automatic_remote_upload_enabled);
  ui.autoUpload.disabled = false;
  ui.autoUploadLabel.textContent = ui.autoUpload.checked ? "Enabled" : "Manual only";
  ui.cache.textContent = server.cache_dir || "—";
  ui.proxy.textContent = server.audio_proxy_configured ? "Configured" : "Direct connection";
  ui.ttl.textContent = `${server.session_ttl_seconds} seconds`;
  ui.retention.textContent = `${server.activity_max_rows} complete transactions`;
  ui.serverMeta.textContent = `${server.service} · v${server.version} · ${server.public_url || location.origin}`;
}

function applyState(state, activities = []) {
  const oldPlayers = new Map((currentState?.players || []).map((player) => [player.playback_id, player]));
  const players = (state.players || []).map((player) => {
    const previous = oldPlayers.get(player.playback_id);
    return player.audio_track || !previous?.audio_track ? player : { ...player, audio_track: previous.audio_track };
  });
  currentState = {
    ...(currentState || {}), ...state, players,
    offsets: state.offsets || currentState?.offsets || [],
  };
  renderServer(currentState.server);
  renderSessions(currentState.players);
  renderOffsets(currentState.offsets);
  renderDecisions(currentState.events || []);
  mergeActivity(state.activity || []); mergeActivity(activities); renderTraffic();
}

async function editPlayer(playbackId, offset, rate) {
  try {
    const result = await api(`/api/dashboard/players/${encodeURIComponent(playbackId)}/offset`, {
      method: "PATCH", body: JSON.stringify({ offset: Number(offset), rate: Number(rate) }),
    });
    showNotice(result.message);
  } catch (error) { showNotice(error.message, true); }
}

async function restorePlayer(playbackId, source) {
  try {
    const query = source ? `?source=${encodeURIComponent(source)}` : "";
    const result = await api(`/api/dashboard/players/${encodeURIComponent(playbackId)}/offset/restore${query}`, { method: "POST" });
    showNotice(result.message);
  } catch (error) { showNotice(error.message, true); }
}

async function editOffset(cacheKey, offset, rate) {
  try {
    const result = await api(`/api/dashboard/offsets/${encodeURIComponent(cacheKey)}`, {
      method: "PATCH", body: JSON.stringify({ offset: Number(offset), rate: Number(rate) }),
    });
    showNotice(result.message);
  } catch (error) { showNotice(error.message, true); }
}

async function restoreOffset(cacheKey) {
  try {
    const result = await api(`/api/dashboard/offsets/${encodeURIComponent(cacheKey)}/restore`, { method: "POST" });
    showNotice(result.message);
  } catch (error) { showNotice(error.message, true); }
}

async function uploadPlayer(playbackId) {
  const path = `/api/dashboard/players/${encodeURIComponent(playbackId)}/offset`;
  let info;
  try { info = await api(`${path}/upload-info`); }
  catch (error) { showNotice(error.message, true); return; }
  if (!info.offset_observed) {
    showNotice("No HLS offset has been observed yet. Wait for playback or apply a manual offset first.", true);
    return;
  }
  let previous = {};
  let errorMessage = "";
  let forceIdentity = false;
  while (true) {
    const entered = await requestPlayerUploadDetails(info, previous, errorMessage, forceIdentity);
    if (!entered) return;
    try {
      const result = await api(`${path}/upload`, { method: "POST", body: JSON.stringify(entered) });
      if (result.storage.local_saved) showNotice(`Uploaded successfully (HTTP ${result.storage.remote_status}) and saved locally.`);
      else if (result.storage.local_error) showNotice(`Uploaded remotely (HTTP ${result.storage.remote_status}), but local save failed: ${result.storage.local_error}`, true);
      else showNotice(`Uploaded using the cache key (HTTP ${result.storage.remote_status}). No local record was saved because identity fields are incomplete.`);
      return;
    } catch (error) {
      if (error.status !== 409 && error.status !== 502) { showNotice(error.message, true); return; }
      errorMessage = error.message;
      forceIdentity = error.detail?.code === "UPLOAD_IDENTITY_REQUIRED";
      previous = entered;
    }
  }
}

async function uploadOffset(cacheKey) {
  if (!cacheKey) return;
  let suppliedContext = {};
  let serverPrompted = false;
  while (true) {
    try {
      const options = { method: "POST" };
      if (Object.keys(suppliedContext).length) options.body = JSON.stringify(suppliedContext);
      const result = await api(`/api/dashboard/offsets/${encodeURIComponent(cacheKey)}/upload`, options);
      showNotice(`Uploaded successfully (HTTP ${result.storage.remote_status}).`);
      return;
    } catch (error) {
      const missing = error.detail?.code === "UPLOAD_CONTEXT_REQUIRED" && Array.isArray(error.detail.missing_fields)
        ? error.detail.missing_fields : [];
      if (missing.length && !serverPrompted) {
        const entered = await requestUploadDetails(missing, suppliedContext);
        if (!entered) return;
        suppliedContext = { ...suppliedContext, ...entered };
        serverPrompted = true;
        continue;
      }
      showNotice(error.message, true);
      return;
    }
  }
}

async function setAutomaticUpload(enabled) {
  ui.autoUpload.disabled = true;
  try {
    const result = await api("/api/dashboard/settings/automatic-upload", {
      method: "PATCH", body: JSON.stringify({ enabled }),
    });
    if (currentState?.server) currentState.server.automatic_remote_upload_enabled = result.enabled;
    renderServer(currentState.server);
    showNotice(result.message);
  } catch (error) {
    ui.autoUpload.checked = !enabled;
    ui.autoUpload.disabled = false;
    showNotice(error.message, true);
  }
}

function unlock() {
  ui.login.hidden = true; ui.console.hidden = false; ui.lock.hidden = false;
  ui.loginError.textContent = ""; setConnected(true, "Connecting SSE…");
}

function showLogin(message = "") {
  ui.login.hidden = false; ui.console.hidden = true; ui.lock.hidden = true;
  ui.loginError.textContent = message; setConnected(false, "Locked");
}

function initializeSectionToggles() {
  document.querySelectorAll("[data-section-toggle]").forEach((button) => {
    const target = document.getElementById(button.dataset.sectionToggle);
    if (!target) return;
    const storageKey = `sidecar-section-${target.id}`;
    const saved = sessionStorage.getItem(storageKey);
    if (saved !== null) target.hidden = saved === "hidden";
    const update = () => {
      const title = button.dataset.title || "section";
      button.textContent = `${target.hidden ? "Show" : "Hide"} ${title}`;
      button.setAttribute("aria-expanded", String(!target.hidden));
      button.setAttribute("aria-controls", target.id);
    };
    button.addEventListener("click", () => {
      target.hidden = !target.hidden;
      sessionStorage.setItem(storageKey, target.hidden ? "hidden" : "shown");
      update();
    });
    update();
  });
}

async function connectStream() {
  clearTimeout(reconnectTimer);
  if (streamController) streamController.abort();
  streamController = new AbortController();
  unlock();
  try {
    const response = await fetch("/api/dashboard/events", {
      headers: { Authorization: `Bearer ${token}`, Accept: "text/event-stream" },
      signal: streamController.signal,
    });
    if (!response.ok) {
      let message = `HTTP ${response.status}`;
      try { const body = await response.json(); message = body.detail || message; } catch (_) { /* empty */ }
      const error = new Error(typeof message === "string" ? message : jsonText(message)); error.status = response.status; throw error;
    }
    setConnected(true, "Live SSE");
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true }).replace(/\r\n/g, "\n");
      let boundary;
      while ((boundary = buffer.indexOf("\n\n")) >= 0) {
        const message = buffer.slice(0, boundary); buffer = buffer.slice(boundary + 2);
        const data = message.split("\n").filter((line) => line.startsWith("data:"))
          .map((line) => line.slice(5).trimStart()).join("\n");
        if (!data) continue;
        const payload = JSON.parse(data);
        if (payload.type === "snapshot") applyState(payload.state);
        else if (payload.type === "update") applyState(payload.state, payload.activity || []);
      }
    }
    throw new Error("Event stream ended");
  } catch (error) {
    if (error.name === "AbortError") return;
    setConnected(false, "Reconnecting…");
    if (error.status === 401 || error.status === 503) {
      sessionStorage.removeItem("sidecar-admin-token"); showLogin(error.message); return;
    }
    reconnectTimer = setTimeout(connectStream, 1800);
  }
}

ui.form.addEventListener("submit", (event) => {
  event.preventDefault(); token = ui.token.value.trim();
  sessionStorage.setItem("sidecar-admin-token", token); connectStream();
});
ui.lock.addEventListener("click", () => {
  if (streamController) streamController.abort(); clearTimeout(reconnectTimer);
  token = ""; sessionStorage.removeItem("sidecar-admin-token"); ui.token.value = ""; showLogin();
});
ui.trafficSearch.addEventListener("input", renderTraffic);
ui.autoUpload.addEventListener("change", () => setAutomaticUpload(ui.autoUpload.checked));
ui.trafficFilters.addEventListener("click", (event) => {
  const button = event.target.closest("button[data-filter]"); if (!button) return;
  activityFilter = button.dataset.filter;
  ui.trafficFilters.querySelectorAll("button").forEach((item) => item.classList.toggle("selected", item === button));
  renderTraffic();
});
document.querySelectorAll("[data-close-drawer]").forEach((node) => node.addEventListener("click", closeDrawer));
ui.copyTransaction.addEventListener("click", () => { if (openTransaction) copyText(openTransaction); });
document.addEventListener("keydown", (event) => { if (event.key === "Escape") closeDrawer(); });
window.addEventListener("beforeunload", () => { objectUrls.forEach((url) => URL.revokeObjectURL(url)); });

initializeSectionToggles();
if (token) connectStream();
