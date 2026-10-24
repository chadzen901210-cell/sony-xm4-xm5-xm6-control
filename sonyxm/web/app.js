"use strict";

// The server only accepts requests carrying the token it put in this window's address.
const token = new URLSearchParams(location.search).get("t") || "";
const $ = (id) => document.getElementById(id);

const ui = {
  name: $("device-name"),
  connection: $("connection"),
  connectionText: $("connection-text"),
  modelNote: $("model-note"),
  battery: $("battery"),
  batteryFill: $("battery-fill"),
  batteryText: $("battery-text"),
  banner: $("banner"),
  busy: $("busy"),
  modes: $("modes"),
  modeButtons: [...document.querySelectorAll("#modes button")],
  windButton: $("mode-wind"),
  ambient: $("ambient"),
  level: $("level"),
  levelValue: $("level-value"),
  autoAmbientRow: $("auto-ambient-row"),
  autoAmbient: $("auto-ambient"),
  voice: $("voice"),
  eqCard: $("eq-card"),
  eqPreset: $("eq-preset"),
  eqBands: $("eq-bands"),
  eqArea: $("eq-area"),
  eqLine: $("eq-line"),
  eqDots: $("eq-dots"),
  headCard: $("head-card"),
  headToggle: $("head-toggle"),
  headUnsupported: $("head-unsupported"),
  stage: $("stage"),
  stageNote: $("stage-note"),
  canvas: $("head-canvas"),
  yaw: $("yaw"),
  pitch: $("pitch"),
  roll: $("roll"),
  recenter: $("recenter"),
  headRate: $("head-rate"),
  headNotice: $("head-notice"),
  headMessage: $("head-message"),
  repair: $("repair"),
  language: $("language"),
  donate: $("donate"),
  toast: $("toast"),
};

// Presets to offer until a connected model says which ones it has
const EQ_PRESETS = [
  "off", "bright", "excited", "mellow", "relaxed", "vocal", "treble_boost", "bass_boost", "speech",
  "manual", "custom_1", "custom_2",
];
// Presets whose bands can be set by hand; other presets switch to Manual when a band moves
const EQ_BAND_SLOTS = ["manual", "custom_1", "custom_2"];
// The five bands of the XM4 and XM5, until an equalizer message says otherwise
const EQ_DEFAULT_LABELS = ["400 Hz", "1 kHz", "2.5 kHz", "6.3 kHz", "16 kHz"];

const state = {
  connected: false,
  // What the connected model can do; until one connects, assume the XM5
  model: { wind: false, headTracking: true, tested: true },
  presets: EQ_PRESETS, // equalizer presets the connected model offers
  noise: null, // {mode, level, voice, autoAmbient} as last reported by the headphones
  eq: null, // {preset, clearBass, bands, labels, stepMax}
  pending: 0, // changes sent and not yet confirmed
  sliding: false, // the user is moving a slider: don't move it under their hand
  appGone: false, // the program behind this window has stopped
  headRestored: false,
  last: {}, // latest message of each type, to redraw text when the language changes
};

const head3d = window.createHead3D(ui.canvas);

// ---------------------------------------------------------------- Language

const LANGUAGES = window.XM5_LANGUAGES;
let language = chooseLanguage();

function chooseLanguage() {
  try {
    const saved = localStorage.getItem("language");
    if (saved && LANGUAGES[saved]) return saved;
  } catch {
    // no saved choice
  }
  for (const wanted of navigator.languages || [navigator.language || "en"]) {
    const code = wanted.toLowerCase();
    if (code.startsWith("zh")) return "zh-Hant"; // only Traditional Chinese is offered
    const base = code.split("-")[0];
    if (LANGUAGES[base]) return base;
  }
  return "en";
}

function t(key, params = {}) {
  const text = LANGUAGES[language].strings[key] ?? LANGUAGES.en.strings[key] ?? key;
  return text.replace(/\{(\w+)\}/g, (_, name) => String(params[name] ?? ""));
}

function applyLanguage() {
  document.documentElement.lang = language;
  for (const element of document.querySelectorAll("[data-i18n]")) {
    element.textContent = t(element.dataset.i18n);
  }
  for (const element of document.querySelectorAll("[data-i18n-aria]")) {
    element.setAttribute("aria-label", t(element.dataset.i18nAria));
  }
  fillPresetMenu();
  // Redraw everything that was put together from messages
  for (const type of ["connection", "battery", "noise", "eq", "headStatus"]) {
    if (state.last[type]) handle(state.last[type], { replay: true });
  }
  if (state.appGone) showBanner(t("appGone"));
}

function fillLanguageMenu() {
  for (const [code, info] of Object.entries(LANGUAGES)) {
    const option = new Option(info.name, code);
    option.lang = code;
    ui.language.add(option);
  }
  ui.language.value = language;
  ui.language.addEventListener("change", () => {
    language = ui.language.value;
    try {
      localStorage.setItem("language", language);
    } catch {
      // remembering the choice is a nicety
    }
    applyLanguage();
    sendLabels();
  });
}

// The tray menu belongs to the program, not to this window: tell it our texts
function sendLabels() {
  const plain = (key) => t(key).replace(/­/g, ""); // soft hyphens only help the window's narrow buttons
  post("/api/labels", {
    open: plain("tray.open"),
    quit: plain("tray.quit"),
    noise_cancelling: plain("mode.noise_cancelling"),
    ambient: plain("mode.ambient"),
    wind: plain("mode.wind"),
    off: plain("mode.off"),
    notConnected: plain("connection.disconnected"),
  }).catch(() => {
    // the menu keeps the texts it had
  });
}

// ---------------------------------------------------------------- Server

async function post(path, body = {}) {
  const response = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-XM5-Token": token },
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    const detail = await response.json().catch(() => ({}));
    throw new Error(detail.error || `HTTP ${response.status}`);
  }
}

// A change to the headphones: shows the spinner until the headphones confirm it.
function change(path, body) {
  state.pending += 1;
  updateControls();
  post(path, body).catch((error) => {
    state.pending = Math.max(0, state.pending - 1);
    updateControls();
    showToast(t("error.request"), error.message);
  });
}

function listen() {
  let failures = 0;
  const source = new EventSource(`/api/events?t=${encodeURIComponent(token)}`);
  source.onopen = () => {
    failures = 0;
    if (state.appGone) {
      state.appGone = false;
      hideBanner();
    }
  };
  source.onmessage = (event) => handle(JSON.parse(event.data));
  source.onerror = () => {
    failures += 1;
    if (failures >= 3 && !state.appGone) {
      state.appGone = true;
      state.connected = false;
      showBanner(t("appGone"));
      updateControls();
    }
  };
}

function handle(message, { replay = false } = {}) {
  if (message.type !== "done" && message.type !== "head") state.last[message.type] = message;
  switch (message.type) {
    case "connection": return showConnection(message);
    case "battery": return showBattery(message);
    case "noise": return showNoise(message);
    case "eq": return showEq(message);
    case "done": return finished(message);
    case "headStatus": return showHeadStatus(message, replay);
    case "head": return showPose(message);
    case "quit": return quit();
  }
}

// The app was quit from its tray icon: this window has nothing left to talk to
function quit() {
  state.appGone = true;
  state.connected = false;
  showBanner(t("appGone"));
  updateControls();
  window.close();
}

function finished(message) {
  state.pending = Math.max(0, state.pending - 1);
  if (message.error) showToast(message.code ? t(`error.${message.code}`) : message.error, message.error);
  updateControls();
}

// ---------------------------------------------------------------- Connection and battery

function showConnection(message) {
  state.connected = message.state === "connected";
  ui.connection.dataset.state = message.state;
  if (message.name) ui.name.textContent = message.name;
  ui.connectionText.textContent = t(`connection.${message.state}`);
  if (message.state === "disconnected") {
    state.pending = 0;
    const summary = t(`disconnected.${message.kind || "lost"}`);
    showBanner(`${summary} ${t("retrying", { s: message.retryIn })}`, message.reason);
  } else if (message.state === "connected" && !state.appGone) {
    hideBanner();
  }
  if (message.state === "connected") showModel(message);
  updateControls();
}

// What the connected model can do. The last model stays on screen while disconnected.
function showModel(message) {
  // Fields missing from the message mean the XM5's defaults
  state.model = {
    wind: message.wind === true,
    headTracking: message.headTracking !== false,
    tested: message.tested !== false,
  };
  if (Array.isArray(message.eqPresets) && message.eqPresets.join() !== state.presets.join()) {
    state.presets = message.eqPresets;
    fillPresetMenu();
    if (state.eq) showPreset(state.eq.preset);
  }
  ui.windButton.hidden = !state.model.wind;
  ui.modelNote.hidden = state.model.tested;
  if (state.noise) showNoise(state.noise); // which mode is highlighted depends on the buttons shown
  updateHeadSupport();
  if (state.last.headStatus) showHeadStatus(state.last.headStatus, true);
}

function showBattery(message) {
  ui.battery.hidden = false;
  ui.batteryText.textContent = `${message.level}%`;
  ui.batteryFill.setAttribute("width", String(Math.max(1.5, (21 * message.level) / 100)));
  ui.battery.classList.toggle("charging", message.charging === "charging");
  ui.battery.classList.toggle("low", message.level <= 20 && message.charging !== "charging");
  const status = { charging: t("battery.charging"), charged: t("battery.charged") }[message.charging];
  ui.battery.title = status ? `${t("battery")}: ${status}` : t("battery");
}

// ---------------------------------------------------------------- Noise control

// The modes the connected model has (wind noise reduction only on the XM4), in button order
function shownModeButtons() {
  return ui.modeButtons.filter((button) => !button.hidden);
}

function showNoise(message) {
  state.noise = message;
  const modes = shownModeButtons().map((button) => button.dataset.mode);
  const index = modes.indexOf(message.mode);
  ui.modes.style.setProperty("--index", String(Math.max(index, 0)));
  ui.modes.style.setProperty("--count", String(modes.length));
  ui.modes.classList.toggle("many", modes.length > 3);
  ui.modes.classList.toggle("none", index < 0);
  for (const button of ui.modeButtons) {
    button.setAttribute("aria-checked", String(button.dataset.mode === message.mode));
  }
  if (!state.sliding) {
    ui.level.value = String(message.level);
    ui.levelValue.textContent = String(message.level);
    paintSlider(ui.level);
  }
  ui.voice.checked = message.voice;
  ui.autoAmbient.checked = message.autoAmbient === true;
  updateControls();
}

for (const button of ui.modeButtons) {
  button.addEventListener("click", () => {
    const mode = button.dataset.mode;
    if (!state.noise || state.noise.mode === mode) return;
    showNoise({ ...state.noise, mode }); // move the highlight now; the headphones confirm shortly
    change("/api/noise", { mode });
  });
}

ui.modes.addEventListener("keydown", (event) => {
  const step = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 }[event.key];
  if (!step) return;
  event.preventDefault();
  const buttons = shownModeButtons();
  const current = buttons.indexOf(document.activeElement);
  const next = buttons[(current + step + buttons.length) % buttons.length];
  next.focus();
  next.click();
});

ui.level.addEventListener("input", () => {
  state.sliding = true;
  ui.levelValue.textContent = ui.level.value;
  paintSlider(ui.level);
});

// "change" fires when the slider is released (or on each arrow key press)
ui.level.addEventListener("change", () => {
  state.sliding = false;
  change("/api/noise", { mode: "ambient", level: Number(ui.level.value) });
});

ui.voice.addEventListener("change", () => change("/api/noise", { mode: "ambient", voice: ui.voice.checked }));

ui.autoAmbient.addEventListener("change", () =>
  change("/api/noise", { mode: "ambient", autoAmbient: ui.autoAmbient.checked }));

// ---------------------------------------------------------------- Equalizer

const eqSliders = []; // Clear Bass first (if the headphones have it), then the bands
let eqLayout = ""; // what the sliders were built for: rebuilt only when this changes
let eqClearBass = false; // eqSliders[0] is the Clear Bass slider
let eqStepMax = 10; // the sliders go from -eqStepMax to +eqStepMax
let eqFreqs = []; // centre frequency of each band in Hz, for the curve

// Five bands plus Clear Bass on the XM4 and XM5, ten bands and no Clear Bass on the XM6
function buildEqualizer(labels, clearBass, stepMax) {
  eqLayout = JSON.stringify([labels, clearBass, stepMax]);
  eqClearBass = clearBass;
  eqStepMax = stepMax;
  eqFreqs = bandFrequencies(labels);
  eqSliders.length = 0;
  state.sliding = false;
  ui.eqBands.replaceChildren();
  ui.eqBands.classList.toggle("dense", labels.length > 5);
  const rows = labels.map((label, i) => ({ label, id: `eq-band-${i}` }));
  if (clearBass) rows.unshift({ label: null, id: "eq-clear-bass" });
  rows.forEach((row) => {
    const wrapper = document.createElement("div");
    wrapper.className = row.label ? "eq-band" : "eq-band clear-bass";
    const label = document.createElement("label");
    label.htmlFor = row.id;
    if (row.label) {
      label.textContent = row.label;
    } else {
      label.dataset.i18n = "eq.clearBass";
      label.textContent = t("eq.clearBass");
    }
    const slider = document.createElement("input");
    Object.assign(slider, {
      type: "range", id: row.id, min: String(-stepMax), max: String(stepMax), step: "1", value: "0",
    });
    const output = document.createElement("output");
    output.htmlFor = row.id;
    output.textContent = "0";
    slider.addEventListener("input", () => {
      state.sliding = true;
      output.textContent = signed(Number(slider.value));
      paintSlider(slider);
      drawCurve(currentBands());
    });
    slider.addEventListener("change", () => {
      state.sliding = false;
      const values = eqSliders.map((s) => Number(s.input.value));
      if (!EQ_BAND_SLOTS.includes(ui.eqPreset.value)) ui.eqPreset.value = "manual";
      change("/api/eq", { clearBass: eqClearBass ? values[0] : null, bands: currentBands() });
    });
    wrapper.append(label, slider, output);
    ui.eqBands.append(wrapper);
    eqSliders.push({ input: slider, output });
    paintSlider(slider);
  });
}

ui.eqPreset.addEventListener("change", () => change("/api/eq", { preset: ui.eqPreset.value }));

// "2.5 kHz" -> 2500, "31 Hz" -> 31
function parseFrequency(label) {
  const match = /([\d.]+)\s*(k?)hz/i.exec(label);
  return match ? Number(match[1]) * (match[2] ? 1000 : 1) : NaN;
}

function bandFrequencies(labels) {
  const frequencies = labels.map(parseFrequency);
  if (frequencies.every((f) => f > 0)) return frequencies;
  // Labels we can't read: spread the bands evenly (on a log axis) from 100 Hz to 16 kHz
  return labels.map((_, i) => 100 * 160 ** (i / Math.max(1, labels.length - 1)));
}

function fillPresetMenu() {
  const selected = ui.eqPreset.value;
  ui.eqPreset.replaceChildren(...state.presets.map((p) => new Option(t(`eq.preset.${p}`), p)));
  const unknown = new Option(t("eq.preset.unknown"), "unknown");
  unknown.hidden = true;
  ui.eqPreset.add(unknown);
  ui.eqPreset.value = selected || "off";
  if (ui.eqPreset.selectedIndex < 0) ui.eqPreset.value = "unknown"; // the new list lacks that preset
}

function showPreset(preset) {
  ui.eqPreset.value = state.presets.includes(preset) ? preset : "unknown";
}

function showEq(message) {
  state.eq = message;
  const bands = message.bands ?? [];
  ui.eqCard.hidden = bands.length === 0; // the headphones did not send band values
  showPreset(message.preset);
  if (bands.length > 0) {
    const clearBass = message.clearBass != null;
    const labels = bands.map((_, i) => message.labels?.[i] ?? String(i + 1));
    const stepMax = message.stepMax || 10;
    if (eqLayout !== JSON.stringify([labels, clearBass, stepMax])) buildEqualizer(labels, clearBass, stepMax);
    if (!state.sliding) {
      const values = clearBass ? [message.clearBass, ...bands] : bands;
      eqSliders.forEach((slider, i) => {
        slider.input.value = String(values[i]);
        slider.output.textContent = signed(values[i]);
        paintSlider(slider.input);
      });
      drawCurve(bands);
    }
  }
  updateControls();
}

function currentBands() {
  return eqSliders.slice(eqClearBass ? 1 : 0).map((s) => Number(s.input.value));
}

// A smooth curve through the bands, on a log frequency axis
function drawCurve(bands) {
  if (bands.length === 0) return;
  const width = 300;
  const zero = 42;
  // A bit of room before the first and after the last band (for five bands: 250 Hz to 22 kHz)
  const logMin = Math.log10(eqFreqs[0] / 1.6);
  const logMax = Math.log10(eqFreqs[eqFreqs.length - 1] * 1.375);
  const points = bands.map((value, i) => [
    ((Math.log10(eqFreqs[i]) - logMin) / (logMax - logMin)) * width,
    zero - value * (36 / eqStepMax),
  ]);
  const ends = [[0, points[0][1]], ...points, [width, points[points.length - 1][1]]];
  let path = `M${ends[0][0]},${ends[0][1]}`;
  for (let i = 0; i < ends.length - 1; i++) {
    // Catmull-Rom spline through the points, as cubic Béziers
    const p0 = ends[Math.max(0, i - 1)];
    const p1 = ends[i];
    const p2 = ends[i + 1];
    const p3 = ends[Math.min(ends.length - 1, i + 2)];
    const c1 = [p1[0] + (p2[0] - p0[0]) / 6, p1[1] + (p2[1] - p0[1]) / 6];
    const c2 = [p2[0] - (p3[0] - p1[0]) / 6, p2[1] - (p3[1] - p1[1]) / 6];
    path += ` C${c1[0].toFixed(1)},${c1[1].toFixed(1)} ${c2[0].toFixed(1)},${c2[1].toFixed(1)} ${p2[0].toFixed(1)},${p2[1].toFixed(1)}`;
  }
  ui.eqLine.setAttribute("d", path);
  ui.eqArea.setAttribute("d", `${path} L${width},${zero} L0,${zero} Z`);
  ui.eqDots.replaceChildren(...points.map(([x, y]) => {
    const dot = document.createElementNS("http://www.w3.org/2000/svg", "circle");
    dot.setAttribute("cx", x.toFixed(1));
    dot.setAttribute("cy", y.toFixed(1));
    dot.setAttribute("r", "3.5");
    return dot;
  }));
}

// ---------------------------------------------------------------- Controls

function updateControls() {
  // Controls are not disabled while a change is pending, so keyboard focus is
  // never lost; further changes simply queue up behind it.
  const ready = state.connected && !state.appGone;
  for (const button of ui.modeButtons) button.disabled = !(ready && state.noise);
  const ambient = state.noise?.mode === "ambient";
  // Only some headphones (the XM6) can choose the ambient level themselves; the rest send null
  const hasAuto = state.noise?.autoAmbient != null;
  ui.autoAmbientRow.hidden = !(hasAuto && ambient);
  ui.autoAmbient.disabled = !(ready && ambient);
  ui.level.disabled = !(ready && ambient) || (hasAuto && ui.autoAmbient.checked);
  ui.voice.disabled = !(ready && ambient);
  ui.ambient.classList.toggle("inactive", !ambient);
  ui.eqPreset.disabled = !(ready && state.eq);
  for (const slider of eqSliders) slider.input.disabled = !(ready && state.eq);
  ui.busy.hidden = state.pending === 0;
}

// Sliders paint their filled part through CSS variables
function paintSlider(slider) {
  const min = Number(slider.min);
  const max = Number(slider.max);
  const pct = ((Number(slider.value) - min) / (max - min)) * 100;
  slider.style.setProperty("--pct", `${pct}%`);
  if (min < 0) {
    // Fill from the zero point towards the value
    const zero = (-min / (max - min)) * 100;
    slider.style.setProperty("--lo", `${Math.min(zero, pct)}%`);
    slider.style.setProperty("--hi", `${Math.max(zero, pct)}%`);
  }
}

function signed(value) {
  return value > 0 ? `+${value}` : value < 0 ? `−${-value}` : "0";
}

// ---------------------------------------------------------------- Head tracking

function setHeadTracking(enabled) {
  try {
    localStorage.setItem("headTracking", enabled ? "on" : "off");
  } catch {
    // remembering the choice is a nicety
  }
  post("/api/head", { enabled }).catch((error) => showToast(t("error.request"), error.message));
}

function rememberedHeadTracking() {
  try {
    return localStorage.getItem("headTracking") === "on";
  } catch {
    return false;
  }
}

// A model without the sensor (the XM4): say so, dim the card and keep the switch off
function updateHeadSupport() {
  const supported = state.model.headTracking;
  ui.headCard.classList.toggle("no-sensor", !supported);
  ui.headUnsupported.hidden = supported;
  // A tracker that is somehow still running can be switched off
  ui.headToggle.disabled = !supported && !ui.headToggle.checked;
}

function showHeadStatus(message, replay) {
  if (!state.headRestored && !replay) {
    state.headRestored = true;
    if (message.state === "off" && state.model.headTracking && rememberedHeadTracking()) {
      ui.headToggle.checked = true;
      setHeadTracking(true);
      return;
    }
  }
  const running = message.state === "running";
  // Without the sensor nothing can be starting or broken; only a tracker that runs anyway shows as on
  ui.headToggle.checked = message.state !== "off" && (state.model.headTracking || running);
  ui.stage.dataset.state = message.state;
  ui.stageNote.textContent = { off: t("head.off"), starting: t("head.starting") }[message.state] || "";
  const problem = state.model.headTracking && ["unavailable", "repairing"].includes(message.state);
  ui.headNotice.hidden = !problem;
  if (problem) {
    const key = message.state === "repairing" ? "head.repairing" : `head.${message.code || "error"}`;
    ui.headMessage.textContent = t(key);
    ui.headMessage.title = message.message || "";
  }
  ui.repair.disabled = message.state === "repairing";
  ui.recenter.disabled = !running;
  if (!running) {
    ui.yaw.textContent = ui.pitch.textContent = ui.roll.textContent = "–";
    ui.headRate.textContent = "";
    head3d.reset();
  }
  head3d.setActive(running);
  updateHeadSupport();
}

function showPose(message) {
  head3d.setTarget(message.q);
  ui.yaw.textContent = `${signed(Math.round(message.yaw))}°`;
  ui.pitch.textContent = `${signed(Math.round(message.pitch))}°`;
  ui.roll.textContent = `${signed(Math.round(message.roll))}°`;
  ui.headRate.textContent = t("head.rate", { n: Math.round(message.rate) });
  ui.stageNote.textContent = message.settled ? "" : t("head.calibrating");
}

ui.headToggle.addEventListener("change", () => setHeadTracking(ui.headToggle.checked));
ui.recenter.addEventListener("click", () =>
  post("/api/head/recenter").catch((e) => showToast(t("error.request"), e.message)));
ui.repair.addEventListener("click", () =>
  post("/api/head/repair").catch((e) => showToast(t("error.request"), e.message)));

// The app opens the donation page in the default browser, where the user is
// signed in to PayPal; this window's own browser profile is not.
ui.donate.addEventListener("click", (event) => {
  event.preventDefault();
  post("/api/donate").catch(() => window.open(ui.donate.href, "_blank", "noopener"));
});

// ---------------------------------------------------------------- Messages

function showBanner(text, detail = "") {
  ui.banner.textContent = text;
  ui.banner.title = detail;
  ui.banner.hidden = false;
}

function hideBanner() {
  ui.banner.hidden = true;
}

let toastTimer = 0;
function showToast(text, detail = "") {
  ui.toast.textContent = text;
  ui.toast.title = detail;
  ui.toast.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (ui.toast.hidden = true), 5000);
}

// ---------------------------------------------------------------- Start

buildEqualizer(EQ_DEFAULT_LABELS, true, 10);
fillLanguageMenu();
applyLanguage();
sendLabels();
ui.stageNote.textContent = t("head.off");
paintSlider(ui.level);
drawCurve(currentBands());
updateControls();
listen();
