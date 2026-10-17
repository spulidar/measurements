"use strict";

const WINDOW_LABELS = { "00": "00–06", "06": "06–12", "12": "12–18", "18": "18–24" };
const MONTH_NAMES = ["Janeiro", "Fevereiro", "Março", "Abril", "Maio", "Junho",
  "Julho", "Agosto", "Setembro", "Outubro", "Novembro", "Dezembro"];
const $ = id => document.getElementById(id);
const state = { manifest: null, date: null, window: null, year: null, month: null, channel: "all" };

function node(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = text;
  return element;
}

function isoDateLabel(key) {
  return new Intl.DateTimeFormat("pt-BR", { day: "numeric", month: "long", year: "numeric" })
    .format(new Date(Number(key.slice(0, 4)), Number(key.slice(4, 6)) - 1,
      Number(key.slice(6, 8)), 12));
}

function writeURL() {
  const params = new URLSearchParams(window.location.search);
  if (state.date) params.set("date", state.date);
  if (state.window) params.set("window", state.window);
  window.history.replaceState(null, "", window.location.pathname + "?" + params.toString());
}

function initSelectors() {
  const year = $("year");
  const month = $("month");
  for (const value of state.manifest.years) {
    const option = node("option", "", value);
    option.value = value;
    year.append(option);
  }
  MONTH_NAMES.forEach((label, index) => {
    const option = node("option", "", label);
    option.value = String(index + 1).padStart(2, "0");
    month.append(option);
  });
  year.addEventListener("change", () => {
    state.year = year.value;
    const available = Object.keys(state.manifest.days).filter(d => d.startsWith(state.year)).sort();
    state.month = available.length ? available[available.length - 1].slice(4, 6) : "01";
    state.date = available.length ? available[available.length - 1] : null;
    state.window = null;
    state.channel = "all";
    refresh();
  });
  month.addEventListener("change", () => {
    state.month = month.value;
    const available = datesInMonth();
    state.date = available.length ? available[available.length - 1] : null;
    state.window = null;
    state.channel = "all";
    refresh();
  });
}

function datesInMonth() {
  return Object.keys(state.manifest.days).filter(d =>
    d.startsWith(state.year + state.month)).sort();
}

function renderCalendar() {
  $("year").value = state.year;
  $("month").value = state.month;
  const grid = $("calendar");
  grid.replaceChildren();
  const first = new Date(Number(state.year), Number(state.month) - 1, 1);
  const offset = (first.getDay() + 6) % 7;
  const length = new Date(Number(state.year), Number(state.month), 0).getDate();
  const active = new Set(datesInMonth());
  for (let i = 0; i < offset; i++) grid.append(node("span", "day-blank"));
  for (let day = 1; day <= length; day++) {
    const key = state.year + state.month + String(day).padStart(2, "0");
    const button = node("button", "day", String(day));
    button.type = "button";
    button.disabled = !active.has(key);
    if (active.has(key)) button.classList.add("has-data");
    if (key === state.date) {
      button.classList.add("selected");
      button.setAttribute("aria-current", "date");
    }
    button.setAttribute("aria-label", isoDateLabel(key) +
      (active.has(key) ? ", com medições" : ", sem medições"));
    button.addEventListener("click", () => {
      state.date = key;
      state.window = null;
      state.channel = "all";
      refresh();
    });
    grid.append(button);
  }
}

function availableWindows() {
  return state.date ? (state.manifest.days[state.date]?.publication_windows || {}) : {};
}

function renderWindowTabs() {
  const tabs = $("window-tabs");
  tabs.replaceChildren();
  const windows = availableWindows();
  const count = Object.keys(windows).length;
  $("window-count").textContent = count + (count === 1 ? " janela" : " janelas");
  if (!windows[state.window]) state.window = Object.keys(WINDOW_LABELS).find(key => windows[key]) || null;
  for (const [key, label] of Object.entries(WINDOW_LABELS)) {
    const button = node("button", "window-tab", label);
    button.type = "button";
    button.disabled = !windows[key];
    button.title = windows[key] ? "Ver janela em horário local" : "Sem produtos nesta janela";
    if (key === state.window) {
      button.classList.add("active");
      button.setAttribute("aria-pressed", "true");
    } else button.setAttribute("aria-pressed", "false");
    button.addEventListener("click", () => {
      state.window = key;
      state.channel = "all";
      refresh();
    });
    tabs.append(button);
  }
}

function figureInfo(filename, publicationId) {
  const prefix = "rcs_" + publicationId + "_";
  if (!filename.startsWith(prefix) || !filename.endsWith(".webp")) return null;
  const suffix = filename.slice(prefix.length, -5);
  if (suffix === "mean") return { channel: "mean", label: "Perfil médio", height: "" };
  const match = /^(.*)_(\d+(?:\.\d+)?km)$/.exec(suffix);
  if (!match) return null;
  return {
    channel: match[1],
    label: match[1].replace("_", " · ") + " · " + match[2],
    height: match[2],
  };
}

function renderPlots() {
  const plots = $("plots");
  const empty = $("empty-state");
  const selector = $("channel");
  plots.replaceChildren();
  selector.replaceChildren();
  const allOption = node("option", "", "Todos os canais");
  allOption.value = "all";
  selector.append(allOption);
  empty.hidden = true;
  $("provenance").hidden = true;
  const record = availableWindows()[state.window];
  if (!record) {
    $("figure-count").textContent = "";
    selector.disabled = true;
    empty.textContent = state.date
      ? "Não há produtos nesta janela de publicação."
      : "Nenhuma medição disponível. Selecione um dia no calendário.";
    empty.hidden = false;
    return;
  }

  const items = Object.entries(record.assets || {}).map(([filename, path]) => {
    return { filename, path, info: figureInfo(filename, record.publication_window_id) };
  }).filter(item => item.info);
  const channels = [...new Set(items.map(item => item.info.channel))].sort();
  for (const channel of channels) {
    const option = node("option", "", channel === "mean"
      ? "Perfil médio" : channel.replace("_", " · "));
    option.value = channel;
    selector.append(option);
  }
  if (state.channel !== "all" && !channels.includes(state.channel)) state.channel = "all";
  selector.value = state.channel;
  selector.disabled = false;

  const visible = items.filter(item => state.channel === "all" || item.info.channel === state.channel);
  $("figure-count").textContent = visible.length + (visible.length === 1 ? " figura" : " figuras");
  if (!visible.length) {
    empty.textContent = "Nenhuma imagem correspondente a este filtro.";
    empty.hidden = false;
  }
  for (const item of visible) {
    const card = node("article", "plot-card");
    const link = node("a", "plot-link");
    link.href = "../" + item.path;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.title = "Abrir imagem completa";
    const img = node("img");
    img.src = link.href;
    img.alt = "Quicklook lidar " + item.info.label + " — " + isoDateLabel(state.date);
    img.loading = "lazy";
    img.decoding = "async";
    link.append(img);
    const caption = node("div", "plot-caption");
    caption.append(node("strong", "", item.info.label));
    caption.append(node("span", "", "Abrir imagem ↗"));
    card.append(link, caption);
    plots.append(card);
  }

  const sourceList = $("source-list");
  sourceList.replaceChildren();
  for (const id of record.source_sessions || []) sourceList.append(node("code", "source-id", id));
  $("source-count").textContent = "(" + (record.source_sessions || []).length + ")";
  $("provenance").hidden = false;
}

function refresh() {
  renderCalendar();
  $("day-heading").textContent = state.date ? isoDateLabel(state.date) : "Sem medições";
  renderWindowTabs();
  renderPlots();
  writeURL();
}

async function boot() {
  try {
    const response = await fetch("manifest.json", { cache: "no-store" });
    if (!response.ok) throw new Error("Manifest HTTP " + response.status);
    const manifest = await response.json();
    if (manifest.schema_version !== 2 || !manifest.days || !Array.isArray(manifest.years))
      throw new Error("Catálogo incompatível com o formato de publicação v2");
    state.manifest = manifest;
    $("station-name").textContent = manifest.station_id.toUpperCase();
    initSelectors();
    $("channel").addEventListener("change", event => {
      state.channel = event.target.value;
      renderPlots();
    });
    const dates = Object.keys(manifest.days).sort();
    const params = new URLSearchParams(window.location.search);
    const requested = params.get("date");
    state.date = requested && manifest.days[requested] ? requested : dates[dates.length - 1] || null;
    if (state.date) {
      state.year = state.date.slice(0, 4);
      state.month = state.date.slice(4, 6);
    } else {
      state.year = manifest.years[0] || String(new Date().getFullYear());
      state.month = String(new Date().getMonth() + 1).padStart(2, "0");
      if (!manifest.years.length) {
        const option = node("option", "", state.year);
        option.value = state.year;
        $("year").append(option);
      }
    }
    const requestedWindow = params.get("window");
    state.window = requestedWindow && availableWindows()[requestedWindow] ? requestedWindow : null;
    refresh();
  } catch (error) {
    $("day-heading").textContent = "Não foi possível carregar o preview";
    $("empty-state").hidden = false;
    $("empty-state").textContent = error.message +
      ". Abra com um servidor HTTP local e gere o preview novamente.";
  }
}
boot();
