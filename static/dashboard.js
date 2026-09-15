const ui = {
  login: document.querySelector("#login"),
  console: document.querySelector("#console"),
  form: document.querySelector("#login-form"),
  token: document.querySelector("#admin-token"),
  loginError: document.querySelector("#login-error"),
  lock: document.querySelector("#lock-button"),
  dot: document.querySelector("#live-dot"),
  connection: document.querySelector("#connection-label"),
  serverTime: document.querySelector("#server-time"),
  remote: document.querySelector("#remote-status"),
  serverMeta: document.querySelector("#server-meta"),
  players: document.querySelector("#players"),
  noPlayers: document.querySelector("#no-players"),
  offsets: document.querySelector("#offset-rows"),
  noOffsets: document.querySelector("#no-offsets"),
  events: document.querySelector("#events"),
  notice: document.querySelector("#notice"),
};

let token = sessionStorage.getItem("sidecar-admin-token") || "";
let timer = null;
let noticeTimer = null;

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function fmtTime(value, includeDate = false) {
  if (!value) return "—";
  const options = includeDate
    ? { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit" }
    : { hour: "2-digit", minute: "2-digit", second: "2-digit" };
  return new Date(value * 1000).toLocaleString([], options);
}

function fmtOffset(value) {
  const number = Number(value || 0);
  return `${number >= 0 ? "+" : ""}${number.toFixed(3)}`;
}

function jsonText(value) {
  return JSON.stringify(value, null, 2);
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
  let body = null;
  try { body = await response.json(); } catch (_) { body = {}; }
  if (!response.ok) {
    const detail = body.detail || body;
    const error = new Error(typeof detail === "string" ? detail : jsonText(detail));
    error.status = response.status;
    throw error;
  }
  return body;
}

function showNotice(message, bad = false) {
  clearTimeout(noticeTimer);
  ui.notice.textContent = message;
  ui.notice.classList.toggle("bad", bad);
  ui.notice.hidden = false;
  noticeTimer = setTimeout(() => { ui.notice.hidden = true; }, 5000);
}

function setConnected(connected) {
  ui.dot.classList.toggle("live", connected);
  ui.connection.textContent = connected ? "Live · 1.2s" : "Disconnected";
}

function badge(text, kind = "") {
  return el("span", `badge ${kind}`.trim(), text);
}

function field(labelText, value, step, className) {
  const wrap = el("div");
  const label = el("label", "", labelText);
  const input = el("input", className);
  input.type = "number";
  input.step = step;
  input.value = value;
  wrap.append(label, input);
  return { wrap, input };
}

function playerCard(player) {
  const card = el("article", "player panel");
  card.dataset.player = player.playback_id;
  const head = el("div", "player-head");
  const identity = el("div");
  identity.append(
    el("span", "mono", `PLAYBACK ${player.playback_id}`),
    el("h3", "", player.audio_metadata?.media_key || player.hid),
  );
  const badges = el("div", "badges");
  badges.append(
    badge(player.active ? "active" : "idle", player.active ? "good" : ""),
    badge(player.cache_hit === true ? `cache hit · ${player.cache_source || "?"}` : player.cache_hit === false ? "cache miss" : "cache pending", player.cache_hit ? "good" : ""),
    badge(player.cached_audio ? "audio reused" : "audio fresh"),
  );
  if (player.control_source === "manual") badges.append(badge("custom", "hot"));
  head.append(identity, badges);

  const readout = el("div", "offset-readout");
  readout.append(el("span", "", "Effective offset"));
  const number = el("strong", "", fmtOffset(player.current_offset));
  number.append(el("small", "", " s"));
  readout.append(number);

  const edit = el("div", "edit-grid");
  const offset = field("Offset · seconds", Number(player.current_offset).toFixed(3), "0.001", "offset-input");
  const rate = field("Rate", Number(player.current_rate).toFixed(9), "0.000001", "rate-input");
  edit.append(offset.wrap, rate.wrap);

  const nudges = el("div", "nudge-row");
  [-0.5, -0.1, 0.1, 0.5].forEach((amount) => {
    const button = el("button", "", `${amount > 0 ? "+" : ""}${amount}s`);
    button.type = "button";
    button.addEventListener("click", () => {
      offset.input.value = (Number(offset.input.value) + amount).toFixed(3);
    });
    nudges.append(button);
  });

  const buttons = el("div", "button-row");
  const apply = el("button", "accent", "Apply & save local");
  apply.type = "button";
  apply.addEventListener("click", () => editPlayer(player.playback_id, offset.input.value, rate.input.value));
  const upload = el("button", "ghost", "Upload remote");
  upload.type = "button";
  upload.disabled = !player.cache_key;
  upload.addEventListener("click", () => uploadOffset(player.cache_key));
  buttons.append(apply, upload);

  const state = el(
    "div",
    "player-state",
    player.pending_player_request
      ? "● Waiting for the player's next request"
      : `Last ${player.last_request_kind || "request"}: ${fmtTime(player.last_request_at)} · ${player.request_count} request(s)`,
  );
  const details = el("details");
  details.append(el("summary", "", "All safe metadata & sync diagnostics"));
  const debug = { ...player };
  details.append(el("pre", "", jsonText(debug)));
  card.append(head, readout, edit, nudges, buttons, state, details);
  return card;
}

function renderPlayers(players) {
  ui.noPlayers.hidden = players.length > 0;
  const activeElement = document.activeElement;
  if (activeElement && ui.players.contains(activeElement)) return;
  ui.players.replaceChildren(...players.map(playerCard));
}

function offsetRow(record, remoteConfigured) {
  const row = el("tr");
  const media = el("td", "media-cell");
  media.append(el("strong", "", record.media_key), el("small", "", record.cache_key));
  const resolution = el("td", "", record.resolution ? `${record.resolution}p` : "—");
  const offsetCell = el("td");
  const offset = el("input");
  offset.type = "number"; offset.step = "0.001"; offset.value = Number(record.offset_seconds || 0).toFixed(3);
  offsetCell.append(offset);
  const rateCell = el("td");
  const rate = el("input");
  rate.type = "number"; rate.step = "0.000001"; rate.value = Number(record.rate || 1).toFixed(9);
  rateCell.append(rate);
  const origin = el("td");
  origin.append(badge(record.details?.custom ? "custom" : (record.details?.cached ? "cached" : "measured"), record.details?.custom ? "hot" : ""));
  const updated = el("td", "mono", fmtTime(record.updated_at, true));
  const actions = el("td", "actions");
  const save = el("button", "small", "Save");
  save.type = "button";
  save.addEventListener("click", () => editOffset(record.cache_key, offset.value, rate.value));
  const upload = el("button", "ghost small", "Upload");
  upload.type = "button"; upload.disabled = !remoteConfigured;
  upload.title = remoteConfigured ? "Upload this record to the remote offset DB" : "Configure OFFSET_API_URL first";
  upload.addEventListener("click", () => uploadOffset(record.cache_key));
  actions.append(save, upload);
  row.append(media, resolution, offsetCell, rateCell, origin, updated, actions);
  return row;
}

function renderOffsets(records, remoteConfigured) {
  ui.noOffsets.hidden = records.length > 0;
  const activeElement = document.activeElement;
  if (activeElement && ui.offsets.contains(activeElement)) return;
  ui.offsets.replaceChildren(...records.map((record) => offsetRow(record, remoteConfigured)));
}

function renderEvents(events) {
  const nodes = events.slice(0, 80).map((event) => {
    const row = el("div", "event");
    const details = { ...event };
    delete details.at; delete details.kind;
    row.append(
      el("time", "", fmtTime(event.at)),
      el("b", "", event.kind),
      el("span", "", Object.keys(details).length ? JSON.stringify(details) : "—"),
    );
    return row;
  });
  ui.events.replaceChildren(...(nodes.length ? nodes : [el("div", "empty", "No events yet.")]));
}

function render(state) {
  setConnected(true);
  ui.serverTime.textContent = fmtTime(state.server.server_time, true);
  ui.remote.textContent = state.server.remote_db_configured ? "CONNECTED" : "NOT CONFIGURED";
  ui.remote.style.color = state.server.remote_db_configured ? "var(--lime)" : "var(--danger)";
  ui.serverMeta.textContent = `${state.server.service} · v${state.server.version} · ${state.server.public_url || location.origin}`;
  renderPlayers(state.players || []);
  renderOffsets(state.offsets || [], state.server.remote_db_configured);
  renderEvents(state.events || []);
}

async function refresh(first = false) {
  if (!token) return;
  try {
    const state = await api("/api/dashboard/state");
    ui.login.hidden = true;
    ui.console.hidden = false;
    ui.lock.hidden = false;
    ui.loginError.textContent = "";
    render(state);
  } catch (error) {
    setConnected(false);
    if (error.status === 401 || error.status === 503) {
      clearInterval(timer);
      timer = null;
      ui.login.hidden = false;
      ui.console.hidden = true;
      ui.lock.hidden = true;
      ui.loginError.textContent = error.message;
      if (!first) sessionStorage.removeItem("sidecar-admin-token");
    }
  }
}

function startPolling() {
  clearInterval(timer);
  refresh(true);
  timer = setInterval(refresh, 1200);
}

async function editPlayer(playbackId, offset, rate) {
  try {
    const result = await api(`/api/dashboard/players/${encodeURIComponent(playbackId)}/offset`, {
      method: "PATCH", body: JSON.stringify({ offset: Number(offset), rate: Number(rate) }),
    });
    showNotice(result.message);
    await refresh();
  } catch (error) { showNotice(error.message, true); }
}

async function editOffset(cacheKey, offset, rate) {
  try {
    const result = await api(`/api/dashboard/offsets/${encodeURIComponent(cacheKey)}`, {
      method: "PATCH", body: JSON.stringify({ offset: Number(offset), rate: Number(rate) }),
    });
    showNotice(result.message);
    await refresh();
  } catch (error) { showNotice(error.message, true); }
}

async function uploadOffset(cacheKey) {
  if (!cacheKey) return;
  try {
    const result = await api(`/api/dashboard/offsets/${encodeURIComponent(cacheKey)}/upload`, { method: "POST" });
    showNotice(`Uploaded to remote DB (HTTP ${result.storage.remote_status}).`);
    await refresh();
  } catch (error) { showNotice(error.message, true); }
}

ui.form.addEventListener("submit", (event) => {
  event.preventDefault();
  token = ui.token.value.trim();
  sessionStorage.setItem("sidecar-admin-token", token);
  startPolling();
});

ui.lock.addEventListener("click", () => {
  clearInterval(timer);
  timer = null;
  token = "";
  sessionStorage.removeItem("sidecar-admin-token");
  ui.token.value = "";
  ui.login.hidden = false;
  ui.console.hidden = true;
  ui.lock.hidden = true;
  ui.connection.textContent = "Locked";
  ui.dot.classList.remove("live");
});

if (token) startPolling();
