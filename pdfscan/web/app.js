"use strict";
/* PDF Scan review app. Talks only to the local service that served this page. */
const $ = id => document.getElementById(id);
const t = (key, vars) => window.I18N.t(key, vars);
const HIGHLIGHTERS = ["#ffd83d", "#ff9cc2", "#9be58f", "#ffb366", "#93ccff", "#c9a8ff", "#7fe0d4", "#ffc9a8"];
const SIZES = [400, 700, 1000, 1300, 1800, 2400];
const STORE = "pdfscan.session.v3";
const MAX_FILES = 50;
const TOGGLES = [["case", "caseSensitive", false], ["flexible", "flexible", true], ["ocr", "ocr", true],
  ["email", "email", false], ["phone", "phone", false], ["cards", "cards", false], ["ids", "ids", false],
  ["auto-logos", "autoLogos", false], ["urls", "urls", false], ["qr", "qr", false], ["watermarks", "watermarks", false]];
const RULE_INPUTS = ["targets", "scope", "languages", "preset", ...TOGGLES.map(x => x[0])];
const emptyDecisions = () => ({ignored: [], manual: [], protected: [], removePages: [], keepPages: [], adjusted: {}});
const MM = 25.4 / 72; // points to millimetres
const MIN_SIDE = .003;

// Everything that belongs to one PDF lives in its file record; Find settings and export options are shared.
const FILE_FIELDS = ["job", "plan", "candidate", "page", "dirty", "stale", "downloaded", "pagesExplicit", "pagesError",
  "imageDigests", "decisions", "seen", "history", "future", "selected", "pagesInput", "error"];
const blankFile = (job = null) => ({job, plan: null, candidate: null, page: 1, dirty: true, stale: true, downloaded: false,
  pagesExplicit: false, pagesError: "", imageDigests: [], decisions: emptyDecisions(), seen: new Set(), history: [], future: [],
  selected: null, pagesInput: "", error: ""});
const state = {
  token: "", models: [], files: [], cur: blankFile(), mode: "inspect", view: "original", tab: "find", zoom: 1, split: 50,
  busy: false, logoSha256: "", logoFile: null, scope: "one", hot: null,
};
// state.plan, state.decisions, ... read and write the file being shown.
for (const key of FILE_FIELDS) Object.defineProperty(state, key, {get: () => state.cur[key], set: value => { state.cur[key] = value; }});
const batch = () => state.files.length > 1;
const fileIndex = (rec = state.cur) => state.files.indexOf(rec);

/* ---------- Utilities ---------- */
function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (key === "class") node.className = value;
    else if (key === "style") for (const [k, v] of Object.entries(value)) k.startsWith("--") ? node.style.setProperty(k, v) : (node.style[k] = v);
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else if (key === "dataset") Object.assign(node.dataset, value);
    else if (value !== undefined && value !== null && value !== false) node.setAttribute(key, value === true ? "" : value);
  }
  node.append(...children.filter(c => c !== null && c !== undefined && c !== false));
  return node;
}
const ICONS = {
  scan: '<path d="M4 7V4h3M16 7V4h-3M4 13v3h3M16 13v3h-3M4 10h12"/>',
  pen: '<path d="M4 16l1-4 8-8 3 3-8 8z"/><path d="M11 6l3 3"/>',
  shield: '<path d="M10 3l6 2.5v4.5c0 3.5-2.5 6-6 7-3.5-1-6-3.5-6-7V5.5z"/><path d="M7.5 10l2 2 3-3.5"/>',
  alert: '<circle cx="10" cy="10" r="7"/><path d="M10 6.5v4.5M10 13.6v.1"/>',
  ok: '<circle cx="10" cy="10" r="7"/><path d="M7 10.2l2 2 4-4.4"/>',
  close: '<path d="M6 6l8 8M14 6l-8 8"/>',
};
function icon(name) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 20 20");
  svg.setAttribute("aria-hidden", "true");
  svg.innerHTML = ICONS[name];
  return svg;
}
const plural = (n, key) => t(key + (n === 1 ? ".one" : ".other"), {n});
function ranges(pages) {
  const out = [];
  for (let i = 0; i < pages.length; i++) {
    let j = i;
    while (j + 1 < pages.length && pages[j + 1] === pages[j] + 1) j++;
    out.push(j > i ? pages[i] + "–" + pages[j] : String(pages[i]));
    i = j;
  }
  return out.join(", ");
}
function bytes(n) {
  if (n < 1024 * 1024) return Math.max(1, Math.round(n / 1024)) + " KB";
  return (n / 1024 / 1024).toFixed(n < 10 * 1024 * 1024 ? 1 : 0) + " MB";
}
let noticeTimer;
function note(message, error = false) {
  const box = $("notice");
  clearTimeout(noticeTimer);
  if (!message) { box.hidden = true; return; }
  box.textContent = message;
  box.className = "toast" + (error ? " error" : "");
  box.hidden = false;
  noticeTimer = setTimeout(() => { box.hidden = true; }, error ? 9000 : 5200);
}
async function request(path, {method = "GET", body, raw = false, headers = {}, keepalive = false} = {}) {
  const response = await fetch(path, {
    method, keepalive,
    headers: {"X-Scan-Token": state.token, ...(!raw && body !== undefined ? {"Content-Type": "application/json"} : {}), ...headers},
    body: body === undefined ? undefined : raw ? body : JSON.stringify(body),
  });
  const type = response.headers.get("Content-Type") || "";
  if (!response.ok) {
    let message = t("err.generic");
    if (type.includes("application/json")) { try { message = (await response.json()).error || message; } catch { /* keep generic */ } }
    throw new Error(window.I18N.error(message));
  }
  return type.includes("application/json") ? response.json() : response.blob();
}

/* ---------- Rules and options ---------- */
function config(rec = state.cur) {
  const c = {text: $("targets").value.split("\n").map(x => x.trim()).filter(Boolean),
    scope: $("scope").value, languages: $("languages").value, preset: $("preset").value,
    imageDigests: [...rec.imageDigests], logoSha256: rec.job?.logo && rec.job.logo === state.logoSha256 ? state.logoSha256 : ""};
  for (const [id, key] of TOGGLES) c[key] = $(id).checked;
  return c;
}
function setConfig(c) {
  $("targets").value = (c.text || []).join("\n");
  for (const [id, key, fallback] of TOGGLES) $(id).checked = c[key] ?? fallback;
  $("scope").value = c.scope || "all";
  $("languages").value = c.languages || "eng+chi_tra";
  $("preset").value = c.preset || "generic";
}
function output() {
  return {fill: document.querySelector('input[name="fill"]:checked').value,
    quality: $("compact").checked ? "compact" : "lossless", searchable: $("searchable").checked};
}
function setOutput(o = {}) {
  document.querySelector(`input[name="fill"][value="${o.fill === "black" ? "black" : "white"}"]`).checked = true;
  $("compact").checked = o.quality === "compact";
  $("searchable").checked = !!o.searchable;
}

/* ---------- Session persistence (this tab only) ---------- */
let saveTimer;
function save() {
  clearTimeout(saveTimer);
  saveTimer = setTimeout(() => {
    try {
      if (!state.files.length) { sessionStorage.removeItem(STORE); return; }
      state.cur.pagesInput = $("remove-pages").value;
      sessionStorage.setItem(STORE, JSON.stringify({
        active: fileIndex(), config: config(), output: output(), tab: state.tab, scope: state.scope,
        files: state.files.map(r => ({id: r.job.id, decisions: r.decisions, imageDigests: r.imageDigests, page: r.page,
          pagesExplicit: r.pagesExplicit, pagesInput: r.pagesInput, dirty: r.dirty,
          revision: !r.stale && r.candidate ? r.candidate.revision : null, seen: [...r.seen]})),
      }));
    } catch { /* storage unavailable: resume is a convenience only */ }
  }, 150);
}
async function restore() {
  let saved;
  try { saved = JSON.parse(sessionStorage.getItem(STORE) || "null"); } catch { saved = null; }
  if (!saved?.files?.length) return false;
  const records = [];
  for (const f of saved.files) {
    try {
      const job = await request("/api/jobs/" + f.id);
      const rec = blankFile(job);
      Object.assign(rec, {plan: job.plan, candidate: job.candidate, imageDigests: f.imageDigests || [],
        decisions: {...emptyDecisions(), ...(f.decisions || {})}, pagesExplicit: !!f.pagesExplicit, pagesInput: f.pagesInput || "",
        dirty: !job.plan || !!f.dirty, page: Math.min(Math.max(1, f.page || 1), job.pages.length)});
      rec.stale = !(job.candidate && f.revision === job.candidate.revision);
      rec.seen = new Set(rec.stale ? [] : f.seen || []);
      records.push(rec);
    } catch { /* that session expired; keep the rest */ }
  }
  if (!records.length) {
    try { sessionStorage.removeItem(STORE); } catch { /* ignore */ }
    return false;
  }
  state.files = records;
  setConfig(saved.config || records[0].plan?.config || {});
  setOutput(saved.output);
  state.scope = saved.scope === "group" ? "group" : "one";
  state.logoSha256 = records.find(r => r.job.logo)?.job.logo || "";
  if (state.logoSha256) showLogo(state.logoSha256, t("logo.saved"), records.find(r => r.job.logo).job.id);
  activate(records[Math.min(Math.max(0, saved.active || 0), records.length - 1)], {keepTab: true});
  setTab(saved.tab || (state.plan ? "review" : "find"), false);
  renderAll();
  note(t("note.resumed"));
  return true;
}

/* ---------- Busy work with progress ---------- */
let poll = null;
let cancelRequested = false;
const progress = {job: null, prefix: ""}; // which document the bar is following, and "File 2 of 5"
function progressText(text) { $("progress-text").textContent = (progress.prefix ? progress.prefix + " · " : "") + text; }
function follow(rec, i = 0, total = 1, stage = "") {
  progress.job = rec.job;
  progress.prefix = total > 1 ? t("batch.prefix", {n: i + 1, total, name: rec.job.name}) : "";
  $("progress").classList.add("indeterminate");
  $("progress-bar").style.removeProperty("--p");
  if (stage) progressText(t(stage + ".start"));
}
function showProgress(kind) {
  cancelRequested = false;
  progress.job = state.job; progress.prefix = "";
  $("progress").hidden = false;
  $("progress").classList.add("indeterminate");
  $("progress-bar").style.removeProperty("--p");
  $("progress-text").textContent = t(kind + ".start");
  $("cancel").hidden = !state.job && kind !== "open";
  $("scan").hidden = kind !== "detect";
  poll = setInterval(async () => {
    if (!progress.job) return;
    try {
      const p = (await request("/api/jobs/" + progress.job.id + "/progress")).progress;
      if (!p || !p.total) return;
      $("progress").classList.remove("indeterminate");
      $("progress-bar").style.setProperty("--p", p.done / p.total);
      progressText(t(p.stage + ".page", {n: Math.min(p.done + 1, p.total), total: p.total}));
    } catch { /* polling is best-effort */ }
  }, 350);
}
function hideProgress() {
  clearInterval(poll);
  poll = null;
  progress.job = null;
  $("progress").hidden = true;
  $("scan").hidden = true;
}
async function task(kind, fn) {
  if (state.busy) return;
  state.busy = true;
  note("");
  if (state.job) renderAll(); else buttons(); // re-render so every edit control shows as disabled
  showProgress(kind);
  try { await fn(); } catch (err) { note(err.message, true); }
  finally { state.busy = false; hideProgress(); if (state.job) renderAll(); else buttons(); save(); }
}

/* ---------- Document lifecycle: one or many PDFs ---------- */
function showWorkspace(on) {
  document.body.dataset.view = on ? "workspace" : "landing";
  $("landing").hidden = on; $("workspace").hidden = !on;
  $("doc-title").hidden = !on; $("flow").hidden = !on; $("clear").hidden = !on;
}
function activate(rec, {keepTab = false} = {}) {
  if (!rec) return;
  if (state.cur.job && state.cur !== rec) state.cur.pagesInput = $("remove-pages").value;
  state.cur = rec;
  state.zoom = 1;
  $("remove-pages").value = rec.pagesInput || "";
  $("download-box").hidden = !rec.downloaded || !$("download-box").dataset.zip;
  showWorkspace(true);
  $("filename").textContent = rec.job.name;
  $("filename").title = rec.job.name;
  buildThumbs();
  setView(state.view === "original" || (rec.candidate && !rec.stale) ? state.view : "original");
  setMode("inspect", false);
  syncPagesInput();
  if (!keepTab && state.tab === "export" && !rec.candidate && !batch()) setTab("review", false);
  renderAll();
  $("mat").scrollTo({top: 0});
}
function switchFile(step) {
  if (!batch() || state.busy) return;
  const n = state.files.length;
  activate(state.files[(fileIndex() + step + n) % n]);
  note(t("note.switched", {name: state.job.name, n: fileIndex() + 1, total: n}));
}
const isPdf = f => /\.pdf$/i.test(f.name) || f.type === "application/pdf";
async function openDocuments(list, {add = false} = {}) {
  const files = [...list];
  const skipped = files.filter(f => !isPdf(f) || f.size > 50 * 1024 * 1024).map(f => f.name);
  let pdfs = files.filter(f => isPdf(f) && f.size <= 50 * 1024 * 1024);
  const room = MAX_FILES - (add ? state.files.length : 0);
  if (!pdfs.length) { note(t("err.pdfOnly"), true); return; }
  if (pdfs.length > room) { note(t("err.tooMany", {n: MAX_FILES}), true); pdfs = pdfs.slice(0, Math.max(0, room)); if (!pdfs.length) return; }
  await task("open", async () => {
    if (!add && state.files.length) await removeAll();
    if (!add) resetLogo();
    const failed = [...skipped];
    const added = [];
    for (const [i, file] of pdfs.entries()) {
      if (cancelRequested) break;
      progress.prefix = "";
      $("progress").classList.remove("indeterminate");
      $("progress-bar").style.setProperty("--p", i / pdfs.length);
      progressText(pdfs.length > 1 ? t("open.file", {n: i + 1, total: pdfs.length, name: file.name}) : t("open.start"));
      try {
        const job = await request("/api/upload", {method: "POST", raw: true, body: file,
          headers: {"Content-Type": "application/pdf", "X-Filename": encodeURIComponent(file.name)}});
        const rec = blankFile(job);
        state.files.push(rec);
        added.push(rec);
        if (state.logoFile) await uploadLogo(rec, state.logoFile).catch(() => {});
      } catch (err) { failed.push(file.name + " (" + err.message + ")"); }
    }
    if (!added.length) throw new Error(t("note.openFailed", {list: failed.join(", ")}));
    activate(added[0]);
    setTab("find", false);
    renderAll();
    const opened = added.length > 1 ? t(add ? "note.added" : "note.openedMany", {n: added.length}) : t(add ? "note.added1" : "note.opened");
    note(opened + (failed.length ? " " + t("note.openFailed", {list: failed.join(", ")}) : ""), !!failed.length);
    if (!add) $("targets").focus({preventScroll: true});
  });
}
async function openDemo() {
  await task("demo", async () => {
    if (state.files.length) await removeAll();
    const job = await request("/api/demo", {method: "POST"});
    const rec = blankFile(job);
    state.files.push(rec);
    resetLogo();
    activate(rec);
    if (job.demoRules) setConfig(job.demoRules);
    setTab("find", false);
    renderAll();
    note(t("note.demo"));
    $("targets").focus({preventScroll: true});
  });
}
async function removeAll() {
  const ids = state.files.map(r => r.job.id);
  state.files = [];
  state.cur = blankFile();
  await Promise.all(ids.map(id => request("/api/jobs/" + id, {method: "DELETE"}).catch(() => {})));
}
async function closeDocument() {
  if (!state.files.length) return;
  await task("close", async () => {
    await removeAll();
    $("targets").value = "";
    resetLogo();
    showWorkspace(false);
    $("file-menu").hidePopover?.();
    note(t("note.closed"));
  });
}
async function removeFile(rec) {
  if (state.busy) return;
  if (state.files.length === 1) { $("file-menu").hidePopover?.(); openConfirm(); return; }
  const i = state.files.indexOf(rec);
  state.files.splice(i, 1);
  request("/api/jobs/" + rec.job.id, {method: "DELETE"}).catch(() => {});
  if ($("download-box").dataset.zip) { $("download-box").hidden = true; for (const r of state.files) r.downloaded = false; }
  if (rec === state.cur) activate(state.files[Math.min(i, state.files.length - 1)]);
  else renderAll();
  renderFileMenu();
  note(t("files.removed", {name: rec.job.name}));
}

/* ---------- Derived state ---------- */
const pageData = (n = state.page) => state.plan?.pages.find(p => p.number === n);
function isRemoved(n) {
  if (state.decisions.removePages.includes(n)) return true;
  if (state.decisions.keepPages.includes(n)) return false;
  return !!pageData(n)?.removeProposed;
}
const activeCount = pr => (pr?.detections || []).filter(d => !state.decisions.ignored.includes(d.id)).length;
function groups() {
  const map = new Map();
  for (const pr of state.plan?.pages || []) {
    for (const d of pr.detections) {
      if (!map.has(d.rule)) map.set(d.rule, {rule: d.rule, label: groupLabel(d), ids: [], pages: new Set(), colour: HIGHLIGHTERS[map.size % HIGHLIGHTERS.length]});
      const g = map.get(d.rule);
      g.ids.push(d.id); g.pages.add(pr.number);
    }
  }
  return [...map.values()];
}
let colourCache = new Map();
function colourFor(rule) { return colourCache.get(rule) || HIGHLIGHTERS[0]; }
function groupLabel(d) {
  const r = d.rule;
  if (r.startsWith("text:")) return d.matched;
  if (r.startsWith("pattern:")) return t("g." + r.slice(8));
  if (r === "visual:qr") return t("g.qr");
  if (r.startsWith("visual:reference")) return t("g.reference");
  if (r.startsWith("visual:")) return t("g.logo");
  if (r.startsWith("watermark:")) return t("g.watermark." + r.split(":")[1]);
  if (r.startsWith("image:")) return d.heuristic ? t("g.crest") : t("g.image");
  if (r.startsWith("exam:")) return t("g.scanCrest");
  return d.label;
}
const methodLabel = m => window.I18N.has("m." + m) ? t("m." + m) : m;
const mappedOutput = (n = state.page) => state.candidate?.pageMap.find(p => p.original === n)?.output;

/* ---------- History ---------- */
function remember() {
  state.history.push(JSON.stringify(state.decisions));
  if (state.history.length > 200) state.history.shift();
  state.future = [];
}
function changed() {
  state.stale = true;
  state.downloaded = false;
  $("download-box").hidden = true;
  syncPagesInput();
  renderAll();
}
function undo(redo = false) {
  const from = redo ? state.future : state.history;
  const to = redo ? state.history : state.future;
  if (!from.length || state.busy) return;
  to.push(JSON.stringify(state.decisions));
  state.decisions = {...emptyDecisions(), ...JSON.parse(from.pop())};
  state.pagesExplicit = true;
  changed();
  note(t(redo ? "note.redo" : "note.undo"));
}

/* ---------- Rendering ---------- */
function renderAll() {
  if (!state.job) { buttons(); return; }
  colourCache = new Map(groups().map(g => [g.rule, g.colour]));
  $("filedetail").textContent = batch() ? t("file.detail", {n: fileIndex() + 1, total: state.files.length, pages: plural(state.job.pages.length, "pages")})
    : plural(state.job.pages.length, "pages");
  renderThumbs(); renderSheet(); renderReview(); renderExport(); renderFlow(); renderFileMenu(); buttons(); save();
}
function buildThumbs() {
  const list = $("thumbs");
  list.replaceChildren();
  for (const p of state.job.pages) {
    const img = el("img", {alt: "", loading: "lazy", decoding: "async", src: `/api/jobs/${state.job.id}/original/${p.number}?w=160`});
    img.addEventListener("load", () => img.classList.add("loaded"));
    const button = el("button", {type: "button", class: "thumb", dataset: {page: p.number}, onclick: () => goto(p.number)},
      el("span", {class: "thumb-img", style: {aspectRatio: p.width + " / " + p.height}}, img),
      el("span", {class: "num"}, String(p.number)));
    list.append(el("li", {}, button));
  }
}
function renderThumbs() {
  for (const button of $("thumbs").querySelectorAll(".thumb")) {
    const n = Number(button.dataset.page);
    const pr = pageData(n);
    const count = activeCount(pr);
    const removed = isRemoved(n);
    button.classList.toggle("removed", removed);
    button.classList.toggle("seen", state.seen.has(n) && !state.stale);
    const current = n === state.page;
    if (current) button.setAttribute("aria-current", "page"); else button.removeAttribute("aria-current");
    let label = t("thumb.label", {n}) + (count ? ", " + plural(count, "thumb.found") : "") + (removed ? ", " + t("thumb.removed") : "");
    button.setAttribute("aria-label", label);
    button.querySelector(".badge")?.remove();
    const manual = state.decisions.manual.filter(r => r.page === n).length;
    if (count + manual) button.querySelector(".thumb-img").append(el("span", {class: "badge", "aria-hidden": "true"}, String(count + manual)));
    else if (pr?.warnings?.length && pr.warnings.some(w => /Scanned|OCR disabled/.test(w))) {
      button.querySelector(".thumb-img").append(el("span", {class: "badge warn", "aria-hidden": "true", title: t("thumb.scan")}, icon("scan")));
    }
  }
  const current = $("thumbs").querySelector('[aria-current="page"]');
  current?.scrollIntoView({block: "nearest", inline: "nearest"});
}

function sheetSize() {
  const page = state.job.pages[state.page - 1];
  const mat = $("mat");
  const narrow = matchMedia("(max-width: 900px)").matches;
  const availW = Math.max(240, mat.clientWidth - (narrow ? 28 : 80));
  const availH = Math.max(240, mat.clientHeight - (narrow ? 66 : 116));
  const ratio = page.width / page.height;
  const fit = Math.min(availW, Math.max(availH * ratio, Math.min(availW, 620)));
  return {width: Math.round(fit * state.zoom), ratio};
}
function bucket(px) { return SIZES.find(s => s >= px) || SIZES[SIZES.length - 1]; }
function renderSheet() {
  const sheet = $("sheet");
  const {width, ratio} = sheetSize();
  sheet.style.width = width + "px";
  sheet.style.height = Math.round(width / ratio) + "px";
  const w = bucket(width * (window.devicePixelRatio || 1));
  const src = `/api/jobs/${state.job.id}/original/${state.page}?w=${w}`;
  const original = $("original");
  if (original.getAttribute("src") !== src) {
    original.classList.remove("loaded");
    original.onload = () => original.classList.add("loaded");
    original.src = src;
  }
  original.alt = t("sheet.original", {n: state.page});
  $("zoom-label").textContent = Math.round(state.zoom * 100) + "%";
  sheet.dataset.view = state.view;
  sheet.style.setProperty("--split", (state.view === "result" ? 0 : state.split) + "%");
  const drawable = ["remove", "keep"].includes(state.mode) && state.view === "original";
  sheet.classList.toggle("drawable", drawable);
  sheet.tabIndex = drawable ? 0 : -1;
  if (drawable) { sheet.setAttribute("role", "application"); sheet.setAttribute("aria-label", t(state.mode === "keep" ? "sheet.drawKeep" : "sheet.drawRemove", {n: state.page})); }
  else { sheet.removeAttribute("role"); sheet.removeAttribute("aria-label"); }
  const wantsResult = state.view !== "original";
  const current = state.candidate && !state.stale;
  const out = current ? mappedOutput() : undefined;
  $("stale-veil").hidden = !wantsResult || !!current;
  $("stale-veil").querySelector("p").textContent = state.candidate ? t("page.stale") : t("page.noPreview");
  $("removed-veil").hidden = !(wantsResult && current && out === null);
  $("result-layer").hidden = !(wantsResult && current && out);
  $("handle").hidden = !(state.view === "compare" && current && out);
  if (wantsResult && current && out) {
    const csrc = `/api/jobs/${state.job.id}/candidate/${state.candidate.revision}/${out}?w=${w}`;
    const cimg = $("candidate");
    if (cimg.getAttribute("src") !== csrc) cimg.src = csrc;
    cimg.alt = t("sheet.result", {n: state.page});
  }
  const ghosts = $("ghosts");
  ghosts.replaceChildren();
  if (wantsResult && current && out) {
    const pr = pageData();
    const rects = (pr?.detections || []).filter(d => !state.decisions.ignored.includes(d.id)).map(d => [rectOf(d), colourFor(d.rule)]);
    rects.push(...state.decisions.manual.filter(r => r.page === state.page).map(r => [r.rect, "#ff9f6b"]));
    for (const [rect, colour] of rects) {
      const g = el("span", {class: "ghost", style: {"--hl": colour}});
      place(g, rect);
      ghosts.append(g);
    }
  }
  if (wantsResult && current && !state.seen.has(state.page)) {
    state.seen.add(state.page);
    queueMicrotask(() => { renderThumbs(); renderExport(); });
  }
  renderOverlays();
  $("page-input").value = state.page;
  $("page-total").textContent = "/ " + state.job.pages.length;
}
function place(node, rect) {
  node.style.left = rect[0] * 100 + "%"; node.style.top = rect[1] * 100 + "%";
  node.style.width = (rect[2] - rect[0]) * 100 + "%"; node.style.height = (rect[3] - rect[1]) * 100 + "%";
}
const letter = i => String.fromCharCode(65 + i % 26) + (i >= 26 ? Math.floor(i / 26) : "");
function renderOverlays() {
  const layer = $("overlays");
  layer.replaceChildren();
  // Marks stamp in once per page and find; edits redraw them without replaying it.
  const stamp = (state.plan?.planHash || "") + ":" + state.page + ":" + state.mode;
  layer.classList.toggle("settled", layer.dataset.stamp === stamp);
  layer.dataset.stamp = stamp;
  const tags = el("div", {class: "tag-layer"});
  const pr = pageData();
  const editing = canEdit();
  if (state.mode === "image") {
    const images = pr?.images || state.job.pages[state.page - 1].images;
    images.forEach((image, i) => {
      const selected = state.imageDigests.includes(image.digest);
      const node = el("button", {type: "button", class: "region image" + (selected ? " selected" : ""), "aria-pressed": String(selected),
        "aria-label": t("image.pick", {n: i + 1}), onclick: () => toggleImage(image.digest)});
      place(node, image.rect);
      layer.append(node);
    });
  } else {
    (pr?.detections || []).forEach((d, i) => {
      const restored = state.decisions.ignored.includes(d.id);
      const target = {kind: "det", id: d.id};
      const rect = rectOf(d);
      const node = el("div", {class: "region" + (restored ? " restored" : "") + (state.hot === d.id ? " hot" : "") + (isSelected(target) ? " selected" : "") + (editing ? " editable" : ""),
        dataset: {region: d.id}, style: {"--hl": colourFor(d.rule), "--i": i}, title: groupLabel(d),
        onpointerdown: e => startGesture(e, target, "move"),
        onmouseenter: () => hot(d.id, true), onmouseleave: () => hot(d.id, false)});
      place(node, rect);
      const tag = el("span", {class: "region-tag", dataset: {tag: d.id}, style: {"--hl": colourFor(d.rule)}}, letter(i));
      placeTag(tag, rect);
      if (!restored) tags.append(tag);
      layer.append(node);
    });
  }
  for (const [field, kind] of [["manual", "manual"], ["protected", "keep"]]) {
    state.decisions[field].forEach((r, index) => {
      if (r.page !== state.page) return;
      const target = {kind: field, index};
      const node = el("div", {class: "region " + kind + (isSelected(target) ? " selected" : "") + (editing ? " editable" : ""),
        dataset: {drawn: field + ":" + index}, title: t(kind === "keep" ? "item.keepArea" : "item.drawn"),
        onpointerdown: e => startGesture(e, target, "move")});
      place(node, r.rect);
      layer.append(node);
    });
  }
  layer.append(tags);
  const sel = selectedInfo();
  if (editing && sel && sel.page === state.page) layer.append(editFrame(sel.rect));
}
function placeTag(tag, rect) {
  tag.style.left = rect[0] * 100 + "%"; tag.style.top = rect[1] * 100 + "%";
  tag.style.bottom = "auto"; tag.style.transform = "translateY(calc(-100% - 2px))";
}
function hot(id, on) {
  state.hot = on ? id : null;
  document.querySelectorAll("[data-region]").forEach(n => n.classList.toggle("hot", on && n.dataset.region === id));
  document.querySelectorAll("[data-item]").forEach(n => n.classList.toggle("hot", on && n.dataset.item === id));
}
function renderReview() {
  const has = !!state.plan;
  $("review-empty").hidden = has;
  $("review-content").hidden = !has;
  const all = groups();
  const total = (state.plan?.pages || []).reduce((n, p) => n + activeCount(p), 0) + state.decisions.manual.length;
  $("review-count").hidden = !has || !total;
  $("review-count").textContent = total;
  if (!has) return;
  const pagesWith = new Set([...(state.plan.pages.filter(p => activeCount(p)).map(p => p.number)), ...state.decisions.manual.map(r => r.page)]);
  $("review-summary").textContent = total ? t("review.summary", {areas: plural(total, "areas"), pages: plural(pagesWith.size, "pagesOn")}) : t("review.none");
  const list = $("groups");
  list.replaceChildren();
  for (const g of all) {
    const kept = g.ids.filter(id => state.decisions.ignored.includes(id)).length;
    const off = kept === g.ids.length;
    list.append(el("li", {class: off ? "off" : ""},
      el("span", {class: "hl-swatch", style: {"--hl": g.colour}}),
      el("span", {class: "g-text"}, el("strong", {title: g.label}, g.label),
        el("small", {}, plural(g.ids.length, "found") + " · " + plural(g.pages.size, "pagesOn") + (kept && !off ? " · " + t("kept", {n: kept}) : "")
          + (g.ids.some(id => state.decisions.adjusted[id]) ? " · " + t("edit.adjustedN", {n: g.ids.filter(id => state.decisions.adjusted[id]).length}) : ""))),
      el("button", {type: "button", class: "mini", disabled: state.busy ? true : null,
        onclick: () => { remember(); state.decisions.ignored = off ? state.decisions.ignored.filter(id => !g.ids.includes(id)) : [...new Set([...state.decisions.ignored, ...g.ids])]; changed(); }},
        off ? t("group.removeAll") : t("group.keepAll"))));
  }
  $("page-heading").textContent = t("review.onPage", {n: state.page});
  $("drop-page").checked = isRemoved(state.page);
  const items = $("detections");
  const focused = document.activeElement?.closest?.("#detections .i-pick")?.closest("li")?.dataset.item;
  items.replaceChildren();
  const pr = pageData();
  (pr?.detections || []).forEach((d, i) => {
    const restored = state.decisions.ignored.includes(d.id);
    const target = {kind: "det", id: d.id};
    const title = ["image-match", "watermark-image"].includes(d.method) || !d.matched ? groupLabel(d) : d.matched;
    const meta = [methodLabel(d.method), d.approximate ? t("close", {read: d.read}) : "", d.confidence ? t("similar", {n: Math.round(d.confidence * 100)}) : "", d.heuristic ? t("guess") : "",
      state.decisions.adjusted[d.id] ? t("edit.adjusted") : ""].filter(Boolean).join(" · ");
    items.append(el("li", {class: (restored ? "restored" : "") + (isSelected(target) ? " selected" : ""), dataset: {item: d.id}, onmouseenter: () => hot(d.id, true), onmouseleave: () => hot(d.id, false)},
      el("span", {class: "tag", style: {"--hl": colourFor(d.rule)}}, letter(i)),
      el("button", {type: "button", class: "i-text i-pick", "aria-pressed": String(isSelected(target)), "aria-label": t("edit.pick", {label: title, letter: letter(i)}), onclick: () => select(isSelected(target) ? null : target, {refocus: d.id})},
        el("strong", {title}, title), el("small", {}, meta)),
      el("button", {type: "button", class: "mini", disabled: state.busy ? true : null,
        onclick: () => { remember(); state.decisions.ignored = restored ? state.decisions.ignored.filter(x => x !== d.id) : [...state.decisions.ignored, d.id]; changed(); }},
        restored ? t("item.remove") : t("item.keep"))));
  });
  for (const [field, key] of [["manual", "item.drawn"], ["protected", "item.keepArea"]]) {
    state.decisions[field].forEach((r, index) => {
      if (r.page !== state.page) return;
      const target = {kind: field, index};
      items.append(el("li", {class: isSelected(target) ? "selected" : "", dataset: {item: field + ":" + index}},
        el("span", {class: "tag", style: {"--hl": field === "manual" ? "#ff9f6b" : "#1683a3"}}, icon(field === "manual" ? "pen" : "shield")),
        el("button", {type: "button", class: "i-text i-pick", "aria-pressed": String(isSelected(target)), "aria-label": t("edit.pick", {label: t(key), letter: ""}), onclick: () => select(isSelected(target) ? null : target, {refocus: field + ":" + index})},
          el("strong", {}, t(key)), el("small", {}, t(field === "manual" ? "item.drawn.p" : "item.keepArea.p"))),
        el("button", {type: "button", class: "mini", onclick: () => deleteRegion(field, index)}, t("item.delete"))));
    });
  }
  if (!items.children.length) items.append(el("li", {class: "none"}, t("review.pageEmpty")));
  const warnings = $("page-warnings");
  warnings.replaceChildren(...(pr?.warnings || []).map(w => el("p", {}, icon("alert"), window.I18N.warning(w))));
  if (state.decisions.protected.some(r => r.page === state.page)) warnings.append(el("p", {}, icon("alert"), t("warn.keep")));
  if (focused) items.querySelector(`[data-item="${CSS.escape(focused)}"] .i-pick`)?.focus({preventScroll: true});
  renderEditor();
  renderPagesSummary();
  const n = state.imageDigests.length;
  $("selected-images").textContent = n ? plural(n, "images") : "";
  $("selected-images").hidden = !n;
}
function renderPagesSummary() {
  $("remove-pages-error").hidden = !state.pagesError;
  $("remove-pages-error").textContent = state.pagesError;
  $("remove-pages").setAttribute("aria-invalid", String(!!state.pagesError));
  const removed = state.job ? state.job.pages.map(p => p.number).filter(isRemoved) : [];
  $("remove-pages-summary").textContent = state.pagesError ? "" : removed.length ? t("pages.removing", {list: ranges(removed), n: removed.length}) : t("pages.none");
}
function fileStatus(rec) {
  // [tone, text] for one file: "find" (needs Find), "preview" (needs a preview), "ready", "checked" or "error".
  if (rec.error) return ["error", t("bf.error", {message: rec.error})];
  if (!rec.plan || rec.dirty) return ["find", t("bf.find")];
  const areas = plural(recCount(rec), "areas");
  if (!recReady(rec)) return ["preview", t("bf.preview", {areas})];
  const kept = rec.candidate.pageMap.filter(p => p.output !== null).map(p => p.original);
  const seen = kept.filter(n => rec.seen.has(n)).length;
  if (rec.candidate.residual?.length) return ["error", t("bf.residual", {areas})];
  return seen === kept.length ? ["checked", t("bf.checked", {areas})] : ["ready", t("bf.ready", {areas, seen, n: kept.length})];
}
function renderBatch() {
  const box = $("batch-box");
  box.hidden = !batch();
  if (!batch()) return;
  $("batch-h").textContent = t("batch.h") + " · " + plural(state.files.length, "files");
  const list = $("batch-files");
  list.replaceChildren(...state.files.map((rec, i) => {
    const [tone, text] = fileStatus(rec);
    return el("li", {class: "bf " + tone + (rec === state.cur ? " current" : "")},
      el("span", {class: "bf-dot", "aria-hidden": "true"}),
      el("button", {type: "button", class: "bf-open", "aria-current": rec === state.cur ? "true" : null, disabled: state.busy ? true : null,
        onclick: () => { if (rec !== state.cur) activate(rec); }},
        el("strong", {title: rec.job.name}, rec.job.name), el("small", {}, text)),
      el("span", {class: "bf-num", "aria-hidden": "true"}, String(i + 1)));
  }));
  const unseen = state.files.filter(r => fileStatus(r)[0] === "ready").length;
  const warn = $("batch-warn");
  warn.hidden = !unseen;
  if (unseen) warn.replaceChildren(icon("alert"), plural(unseen, "batch.unseen"));
}
function renderFileMenu() {
  const list = $("file-list");
  if (!list) return;
  $("fm-count").textContent = plural(state.files.length, "files");
  list.replaceChildren(...state.files.map((rec, i) => {
    const [tone, text] = fileStatus(rec);
    return el("li", {class: "fm-item " + tone + (rec === state.cur ? " current" : "")},
      el("button", {type: "button", class: "fm-open", "aria-current": rec === state.cur ? "true" : null, disabled: state.busy ? true : null,
        onclick: () => { $("file-menu").hidePopover?.(); if (rec !== state.cur) activate(rec); }},
        el("span", {class: "bf-dot", "aria-hidden": "true"}),
        el("span", {class: "fm-text"}, el("strong", {title: rec.job.name}, rec.job.name),
          el("small", {}, plural(rec.job.pages.length, "pages") + " · " + text))),
      el("button", {type: "button", class: "fm-remove", "aria-label": t("files.remove", {name: rec.job.name}), title: t("files.remove", {name: rec.job.name}),
        disabled: state.busy ? true : null, onclick: () => removeFile(rec)},
        icon("close")));
  }));
}
function renderExport() {
  renderBatch();
  const current = state.candidate && !state.stale;
  $("result-box").hidden = !current;
  if (current) {
    const kept = state.candidate.pageMap.filter(p => p.output !== null).map(p => p.original);
    const seen = kept.filter(n => state.seen.has(n)).length;
    $("seen-bar").style.setProperty("--p", kept.length ? seen / kept.length : 1);
    $("seen-meter").classList.toggle("done", seen === kept.length);
    $("seen-text").textContent = seen === kept.length ? t("seen.all", {n: kept.length}) : t("seen.some", {seen, n: kept.length});
    $("next-unseen").hidden = seen === kept.length;
    const residual = state.candidate.residual || [];
    const box = $("residual");
    if (state.candidate.output?.searchable) {
      box.hidden = false;
      box.className = "alert" + (residual.length ? "" : " ok");
      box.replaceChildren(icon(residual.length ? "alert" : "ok"), residual.length
        ? t("residual.found", {list: residual.map(r => t("residual.item", {page: r.page, targets: r.targets.join(", ")})).join("; ")})
        : t("residual.clean"));
    } else box.hidden = true;
  }
}
function renderFlow() {
  const all = state.files.length ? state.files : [state.cur];
  const steps = {find: all.every(r => r.plan && !r.dirty), review: all.every(recReady), export: all.every(r => r.downloaded)};
  for (const li of $("flow").children) {
    li.classList.toggle("done", !!steps[li.dataset.step]);
    li.classList.toggle("current", li.dataset.step === state.tab);
  }
}
function buttons() {
  const b = state.busy, job = !!state.job, many = batch();
  $("detect").disabled = !job || b || !!state.pagesError;
  // Labels follow the size of the set: one PDF exports a PDF, several export one zip.
  $("detect").firstElementChild.textContent = many ? t("find.goAll", {n: state.files.length}) : t("find.go");
  $("preview-2").firstElementChild.textContent = many ? t("previewAll") : t("preview.build");
  $("export").firstElementChild.textContent = many ? t("export.zip", {n: state.files.length}) : t("export.go");
  const canPreview = job && !!state.plan && !state.dirty && !state.pagesError && !b;
  for (const node of document.querySelectorAll('[data-action="preview"]')) node.disabled = !canPreview;
  $("preview").classList.toggle("ready", canPreview && state.stale && state.tab === "review");
  if (many) {
    const found = state.files.every(r => r.plan && !r.dirty);
    $("preview-2").disabled = b || !found || state.files.every(recReady);
    $("export").disabled = b || !found || !!state.pagesError;
    $("export").classList.toggle("ready", !$("export").disabled && !state.files.every(r => r.downloaded));
    $("export").title = found ? "" : t("batch.needsFind");
  } else {
    $("export").disabled = !state.candidate || state.stale || state.dirty || !!state.pagesError || b;
    $("export").classList.toggle("ready", !$("export").disabled && !state.downloaded);
    $("export").removeAttribute("title");
  }
  $("report").disabled = !state.plan || b;
  $("demo").disabled = b; $("file").disabled = b; $("clear").disabled = b; $("add-files").disabled = b;
  $("add-files-label").classList.toggle("disabled", b || state.files.length >= MAX_FILES);
  $("prev").disabled = b || state.page <= 1;
  $("next").disabled = b || !job || state.page >= state.job.pages.length;
  $("next-finding").disabled = b || !state.plan;
  $("undo").disabled = b || !state.history.length;
  $("redo").disabled = b || !state.future.length;
  for (const id of [...RULE_INPUTS, "force", "load-rules", "remove-pages", "drop-page", "searchable", "compact"]) $(id).disabled = b || (id === "remove-pages" && !job);
  document.querySelectorAll('input[name="fill"]').forEach(n => { n.disabled = b; });
  $("logo-file").disabled = !job || b;
  $("logo-upload-label").classList.toggle("disabled", !job || b);
  $("clear-logo").disabled = b;
  document.querySelectorAll(".tool, .views button").forEach(n => { n.disabled = b; });
  if (state.tab === "review" && state.dirty && state.plan) $("preview").title = t("preview.needsFind");
  else $("preview").removeAttribute("title");
}

/* ---------- Navigation, view, tools ---------- */
function goto(n) {
  if (!state.job) return;
  n = Math.min(Math.max(1, n), state.job.pages.length);
  if (n === state.page) return;
  state.page = n; state.selected = null;
  renderSheet(); renderThumbs(); renderReview(); buttons(); save();
  $("mat").scrollTo({top: 0});
}
function setView(view) {
  state.view = view;
  document.querySelectorAll(".views button").forEach(b => b.setAttribute("aria-checked", String(b.dataset.view === view)));
  if (view !== "original" && ["remove", "keep", "image"].includes(state.mode)) setMode("inspect", false);
  if (state.job) { renderSheet(); if (state.plan) renderEditor(); }
}
function setMode(mode, render = true) {
  state.mode = mode;
  state.selected = null;
  document.querySelectorAll(".tool").forEach(b => b.setAttribute("aria-pressed", String(b.dataset.mode === mode)));
  if (mode !== "inspect" && state.view !== "original") setView("original");
  if (render && state.job) { renderSheet(); if (state.plan) renderEditor(); }
  if (mode === "remove" || mode === "keep" || mode === "image") note(t("tool." + mode + ".help"));
}
function setTab(tab, focus = true) {
  state.tab = tab;
  for (const button of document.querySelectorAll('.tabs [role="tab"]')) {
    const on = button.dataset.tab === tab;
    button.setAttribute("aria-selected", String(on));
    button.tabIndex = on ? 0 : -1;
    $("panel-" + button.dataset.tab).hidden = !on;
    if (on) {
      const ink = document.querySelector(".tab-ink");
      ink.style.setProperty("--ink-x", button.offsetLeft + 10 + "px");
      ink.style.setProperty("--ink-s", Math.max(1, button.offsetWidth - 20));
      if (focus && document.activeElement?.closest?.(".tabs")) button.focus();
    }
  }
  renderFlow(); buttons(); save();
}
function setZoom(z) {
  state.zoom = Math.min(4, Math.max(.5, Math.round(z * 100) / 100));
  if (state.job) renderSheet();
}
function nextFinding(fromStart = false) {
  // Walks through the rest of this file, then the following files, then wraps round.
  if ((state.busy && !fromStart) || (!state.plan && !state.files.some(r => r.plan))) return;
  const recs = state.files.length ? state.files : [state.cur];
  const start = Math.max(0, fileIndex()), cur = state.cur, total = cur.job.pages.length;
  const hasArea = (rec, n) => (rec.plan?.pages.find(p => p.number === n)?.detections || []).some(d => !rec.decisions.ignored.includes(d.id))
    || rec.decisions.manual.some(r => r.page === n);
  const order = [];
  for (let n = fromStart ? 1 : state.page + 1; n <= total; n++) order.push([cur, n]);
  for (let k = 1; k < recs.length; k++) { const r = recs[(start + k) % recs.length]; for (let n = 1; n <= r.job.pages.length; n++) order.push([r, n]); }
  if (!fromStart) for (let n = 1; n <= state.page; n++) order.push([cur, n]);
  const hit = order.find(([r, n]) => hasArea(r, n));
  if (!hit) { if (!fromStart) note(t("note.noFindings")); return; }
  if (hit[0] === cur) { goto(hit[1]); return; }
  hit[0].page = hit[1];
  activate(hit[0]);
  if (!fromStart) note(t("note.switched", {name: state.job.name, n: fileIndex() + 1, total: state.files.length}));
}
function nextUnseen() {
  if (!state.candidate) return;
  const kept = state.candidate.pageMap.filter(p => p.output !== null).map(p => p.original);
  const n = kept.find(x => x > state.page && !state.seen.has(x)) ?? kept.find(x => !state.seen.has(x));
  if (n) { if (state.view === "original") setView("result"); goto(n); if (n === state.page) renderSheet(); }
}

/* ---------- Edits ---------- */
function toggleImage(digest) {
  if (state.busy) return;
  state.imageDigests = state.imageDigests.includes(digest) ? state.imageDigests.filter(x => x !== digest) : [...state.imageDigests, digest];
  rulesChanged(false);
  note(t("note.imagePicked"));
}
function deleteRegion(field, index) {
  if (state.busy) return;
  remember();
  state.decisions[field].splice(index, 1);
  state.selected = null;
  changed();
}
function rulesChanged(everyFile = true) {
  // Find settings are shared, so a change makes every file need Find again.
  for (const rec of everyFile ? state.files : [state.cur]) { rec.dirty = true; rec.stale = true; rec.downloaded = false; }
  $("download-box").hidden = true;
  renderAll();
}
function setRemovedPages(pages) {
  const all = state.job.pages.map(p => p.number);
  state.decisions.removePages = pages;
  state.decisions.keepPages = all.filter(n => !pages.includes(n));
  state.pagesExplicit = true;
}
function syncPagesInput() {
  if (!state.job || state.pagesError) return;
  if (state.pagesExplicit || state.plan) $("remove-pages").value = ranges(state.job.pages.map(p => p.number).filter(isRemoved));
}
function parsePages(value, total) {
  if (!value.trim()) return [];
  const pages = new Set();
  for (const part of value.replace(/[–—~～]/g, "-").split(/[,，、;；]+/).map(x => x.trim()).filter(Boolean)) {
    const m = part.match(/^(\d+)(?:\s*-\s*(\d+))?$/);
    if (!m) throw new Error(t("pages.format"));
    const a = Number(m[1]), b = Number(m[2] || m[1]);
    if (a < 1 || b > total || a > b) throw new Error(t("pages.range", {total}));
    for (let p = a; p <= b; p++) pages.add(p);
  }
  if (pages.size === total) throw new Error(t("pages.all"));
  return [...pages].sort((x, y) => x - y);
}

/* ---------- Drawing and compare ---------- */
let drawing = null; // {start, end, mode, how: "drag" | "click" | "keys"}
let cursor = [.5, .5];
function point(e, clamp = true) {
  const r = $("sheet").getBoundingClientRect();
  const x = (e.clientX - r.left) / r.width, y = (e.clientY - r.top) / r.height;
  return clamp ? [Math.max(0, Math.min(1, x)), Math.max(0, Math.min(1, y))] : [x, y];
}
const box = (a, b) => [Math.min(a[0], b[0]), Math.min(a[1], b[1]), Math.max(a[0], b[0]), Math.max(a[1], b[1])];
const canDraw = () => !state.busy && state.view === "original" && ["remove", "keep"].includes(state.mode);
function showDrawing() {
  $("drawing").hidden = false;
  $("drawing").className = "drawing" + (drawing.mode === "keep" ? " keep" : "");
  place($("drawing"), box(drawing.start, drawing.end));
}
function cancelDrawing() { drawing = null; $("drawing").hidden = true; }
function commitDrawing() {
  const rect = box(drawing.start, drawing.end);
  const mode = drawing.mode;
  cancelDrawing();
  if (rect[2] - rect[0] > .004 && rect[3] - rect[1] > .004) {
    remember();
    state.decisions[mode === "remove" ? "manual" : "protected"].push({page: state.page, rect});
    changed();
    note(t(mode === "remove" ? "note.drawn" : "note.kept"));
    return true;
  }
  return false;
}
// Drag to draw; or click two opposite corners (single-pointer alternative).
$("sheet").addEventListener("pointerdown", e => {
  if (!canDraw() || e.button > 0) return;
  e.preventDefault();
  if (drawing?.how === "click") return;
  drawing = {start: point(e), end: point(e), mode: state.mode, how: "drag"};
  $("sheet").setPointerCapture(e.pointerId);
  showDrawing();
});
$("sheet").addEventListener("pointermove", e => {
  if (!drawing || drawing.how === "keys") return;
  drawing.end = point(e);
  place($("drawing"), box(drawing.start, drawing.end));
});
$("sheet").addEventListener("pointerup", e => {
  if (!drawing || drawing.how === "keys") return;
  drawing.end = point(e);
  if (drawing.how === "drag") {
    if (commitDrawing()) return;
    drawing = {start: point(e), end: point(e), mode: state.mode, how: "click"};
    showDrawing();
    note(t("draw.second"));
    return;
  }
  commitDrawing();
});
$("sheet").addEventListener("pointercancel", () => { if (drawing?.how === "drag") cancelDrawing(); });
// Keyboard drawing: arrows move a crosshair, Enter sets each corner, Esc cancels.
function placeCursor() {
  const c = $("kbd-cursor");
  c.style.left = cursor[0] * 100 + "%";
  c.style.top = cursor[1] * 100 + "%";
}
$("sheet").addEventListener("focus", () => {
  if (!canDraw()) return;
  $("kbd-cursor").hidden = false;
  placeCursor();
  note(t("draw.kbd"));
});
$("sheet").addEventListener("blur", () => { $("kbd-cursor").hidden = true; if (drawing?.how === "keys") cancelDrawing(); });
$("sheet").addEventListener("keydown", e => {
  if (!canDraw()) return;
  const step = e.shiftKey ? .05 : .01;
  const moves = {ArrowLeft: [-step, 0], ArrowRight: [step, 0], ArrowUp: [0, -step], ArrowDown: [0, step]};
  if (moves[e.key]) {
    e.preventDefault(); e.stopPropagation();
    cursor = [Math.min(1, Math.max(0, cursor[0] + moves[e.key][0])), Math.min(1, Math.max(0, cursor[1] + moves[e.key][1]))];
    $("kbd-cursor").hidden = false;
    placeCursor();
    if (drawing?.how === "keys") { drawing.end = [...cursor]; place($("drawing"), box(drawing.start, drawing.end)); }
  } else if (e.key === "Enter" || e.key === " ") {
    e.preventDefault(); e.stopPropagation();
    if (drawing?.how === "keys") { if (!commitDrawing()) note(t("draw.tooSmall"), true); }
    else { drawing = {start: [...cursor], end: [...cursor], mode: state.mode, how: "keys"}; showDrawing(); note(t("draw.kbdSecond")); }
  }
});
$("sheet").addEventListener("click", e => {
  if (performance.now() - swallowClick < 350) return;
  if (state.selected && state.mode === "inspect" && !e.target.closest(".region, .edit-frame")) select(null);
  // Single-pointer alternative to dragging the compare divider.
  if (state.view === "compare" && !$("handle").hidden && !e.target.closest(".handle, .stale-veil")) {
    state.split = Math.round(point(e)[0] * 1000) / 10;
    $("sheet").style.setProperty("--split", state.split + "%");
  }
});
let splitting = false;
$("handle").addEventListener("pointerdown", e => { splitting = true; $("handle").setPointerCapture(e.pointerId); $("handle").classList.add("dragging"); e.preventDefault(); });
$("handle").addEventListener("pointermove", e => {
  if (!splitting) return;
  state.split = Math.round(point(e)[0] * 1000) / 10;
  $("sheet").style.setProperty("--split", state.split + "%");
});
$("handle").addEventListener("pointerup", () => { splitting = false; $("handle").classList.remove("dragging"); });
$("handle").addEventListener("keydown", e => {
  if (e.key === "ArrowLeft" || e.key === "ArrowRight") {
    e.preventDefault(); e.stopPropagation();
    state.split = Math.min(100, Math.max(0, state.split + (e.key === "ArrowLeft" ? -5 : 5)));
    $("sheet").style.setProperty("--split", state.split + "%");
  }
});

/* ---------- Editing boxes: move, resize, exact size, one box or the whole group ---------- */
const rectOf = d => state.decisions.adjusted[d.id] || d.rect;
const canEdit = () => !state.busy && state.mode === "inspect" && state.view === "original";
const sameTarget = (a, b) => !!a && !!b && a.kind === b.kind && (a.kind === "det" ? a.id === b.id : a.index === b.index);
const isSelected = target => sameTarget(state.selected, target);
function findDetection(id) {
  for (const pr of state.plan?.pages || []) {
    const i = pr.detections.findIndex(d => d.id === id);
    if (i !== -1) return {d: pr.detections[i], page: pr.number, index: i};
  }
  return null;
}
function members(target) {
  // Every box in the same found group, on every page (the selected one included).
  if (target?.kind !== "det") return [];
  const own = findDetection(target.id);
  if (!own) return [];
  return state.plan.pages.flatMap(pr => pr.detections.filter(d => d.rule === own.d.rule).map(d => ({kind: "det", id: d.id})));
}
function selectedInfo() {
  const s = state.selected;
  if (!s) return null;
  if (s.kind === "det") {
    const f = findDetection(s.id);
    if (!f) return null;
    return {target: s, page: f.page, rect: rectOf(f.d), d: f.d, letter: letter(f.index),
      label: ["image-match", "watermark-image"].includes(f.d.method) || !f.d.matched ? groupLabel(f.d) : f.d.matched, group: groupLabel(f.d), size: members(s).length};
  }
  const r = state.decisions[s.kind]?.[s.index];
  if (!r) return null;
  return {target: s, page: r.page, rect: r.rect, label: t(s.kind === "manual" ? "item.drawn" : "item.keepArea"), size: 1};
}
function targetRect(target) {
  if (target.kind === "det") { const f = findDetection(target.id); return f && rectOf(f.d); }
  return state.decisions[target.kind]?.[target.index]?.rect;
}
const round5 = v => Math.round(v * 1e5) / 1e5;
function fit(rect) {
  // Keep a box on the page: shift it back inside before shrinking it, and never let it collapse.
  let [x0, y0, x1, y1] = rect;
  if (x1 - x0 < MIN_SIDE) { const c = (x0 + x1) / 2; x0 = c - MIN_SIDE / 2; x1 = c + MIN_SIDE / 2; }
  if (y1 - y0 < MIN_SIDE) { const c = (y0 + y1) / 2; y0 = c - MIN_SIDE / 2; y1 = c + MIN_SIDE / 2; }
  if (x0 < 0) { x1 -= x0; x0 = 0; }
  if (x1 > 1) { x0 -= x1 - 1; x1 = 1; }
  if (y0 < 0) { y1 -= y0; y0 = 0; }
  if (y1 > 1) { y0 -= y1 - 1; y1 = 1; }
  x0 = Math.max(0, x0); y0 = Math.max(0, y0);
  const out = [round5(x0), round5(y0), round5(x1), round5(y1)];
  if (out[2] <= out[0]) out[2] = Math.min(1, out[0] + MIN_SIDE);
  if (out[3] <= out[1]) out[3] = Math.min(1, out[1] + MIN_SIDE);
  return out;
}
function delta(from, to) {
  // Edge changes as a share of the box's own size, so smaller copies change in proportion.
  const w = from[2] - from[0], h = from[3] - from[1];
  return [(to[0] - from[0]) / w, (to[1] - from[1]) / h, (to[2] - from[2]) / w, (to[3] - from[3]) / h];
}
function moved(rect, k) {
  const w = rect[2] - rect[0], h = rect[3] - rect[1];
  return fit([rect[0] + k[0] * w, rect[1] + k[1] * h, rect[2] + k[2] * w, rect[3] + k[3] * h]);
}
function setRect(target, rect) {
  if (target.kind === "det") {
    const f = findDetection(target.id);
    if (!f) return;
    if (rect.every((v, i) => Math.abs(v - f.d.rect[i]) < 1e-5)) delete state.decisions.adjusted[target.id];
    else state.decisions.adjusted[target.id] = rect;
  } else if (state.decisions[target.kind]?.[target.index]) state.decisions[target.kind][target.index].rect = rect;
}
function plannedRects(target, from, to) {
  // What every affected box will become: [[target, rect], ...].
  const out = [[target, fit(to)]];
  if (target.kind === "det" && state.scope === "group") {
    const k = delta(from, to);
    for (const m of members(target)) if (!sameTarget(m, target)) out.push([m, moved(targetRect(m), k)]);
  }
  return out;
}
let lastKeyEdit = 0;
function commitEdit(target, from, to, {coalesce = false} = {}) {
  if (to.every((v, i) => Math.abs(v - from[i]) < 1e-6)) return;
  if (!(coalesce && performance.now() - lastKeyEdit < 900)) remember();
  lastKeyEdit = coalesce ? performance.now() : 0;
  const planned = plannedRects(target, from, to);
  for (const [m, rect] of planned) setRect(m, rect);
  changed();
  if (planned.length > 1 && !coalesce) note(t("note.editedGroup", {n: planned.length, label: selectedInfo()?.group || ""}));
}
function select(target, {refocus = null} = {}) {
  state.selected = target;
  if (target && state.tab !== "review") setTab("review", false);
  renderOverlays(); renderReview();
  // The list is rebuilt, so put keyboard focus back on the row the user acted on.
  if (refocus) document.querySelector(`[data-item="${CSS.escape(refocus)}"] .i-pick`)?.focus({preventScroll: true});
  // Beside the page, bring the editor into view; when stacked, keep the page still under the finger.
  if (target && !matchMedia("(max-width: 900px)").matches) requestAnimationFrame(() => {
    const smooth = matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth";
    if (!$("box-editor").hidden) $("box-editor").scrollIntoView({behavior: smooth, block: "nearest"});
  });
}

const EDGES = ["nw", "n", "ne", "e", "se", "s", "sw", "w"];
function editFrame(rect) {
  const frame = el("div", {class: "edit-frame", "aria-hidden": "true", onpointerdown: e => startGesture(e, state.selected, "move")},
    ...EDGES.map(edge => el("i", {class: "grip", dataset: {edge}, onpointerdown: e => startGesture(e, state.selected, edge)})),
    el("span", {class: "edit-readout"}));
  place(frame, rect);
  sizeFrame(frame, rect);
  return frame;
}
function sizeFrame(frame, rect) {
  const r = $("sheet").getBoundingClientRect();
  frame.classList.toggle("narrow", (rect[2] - rect[0]) * r.width < 44);
  frame.classList.toggle("short", (rect[3] - rect[1]) * r.height < 30);
  // Touch grips are bigger, so boxes count as small sooner.
  const coarse = matchMedia("(pointer: coarse)").matches;
  frame.classList.toggle("tiny", (rect[2] - rect[0]) * r.width < (coarse ? 64 : 30) || (rect[3] - rect[1]) * r.height < (coarse ? 40 : 18));
}
function mmSize(rect, page = state.page) {
  const p = state.job.pages[page - 1];
  return [(rect[2] - rect[0]) * p.width * MM, (rect[3] - rect[1]) * p.height * MM];
}
const fmt = v => (Math.round(v * 10) / 10).toFixed(1);

let gesture = null; // {target, edge, origin, start, rect, moved, pointerId}
let swallowClick = 0;
function startGesture(e, target, edge) {
  if (!target || !canEdit() || e.button > 0) return;
  e.stopPropagation();
  const already = isSelected(target);
  // On touch, the first tap only selects, so the page can still scroll under unselected marks.
  if (e.pointerType === "touch" && !already) { swallowClick = performance.now(); select(target); return; }
  e.preventDefault();
  if (!already) select(target);
  const start = targetRect(target);
  if (!start) return;
  gesture = {target, edge, origin: point(e, false), start, rect: start, moved: false, pointerId: e.pointerId};
  $("sheet").setPointerCapture(e.pointerId);
  $("sheet").classList.add("editing", "edit-" + edge);
}
function gestureRect(g, p) {
  const sheet = $("sheet").getBoundingClientRect();
  let dx = p[0] - g.origin[0], dy = p[1] - g.origin[1];
  let [x0, y0, x1, y1] = g.start;
  if (g.edge === "move") {
    dx = Math.max(-x0, Math.min(1 - x1, dx)); dy = Math.max(-y0, Math.min(1 - y1, dy));
    return [x0 + dx, y0 + dy, x1 + dx, y1 + dy].map(round5);
  }
  const minW = Math.max(MIN_SIDE, 4 / sheet.width), minH = Math.max(MIN_SIDE, 4 / sheet.height);
  if (g.edge.includes("w")) x0 = Math.max(0, Math.min(x1 - minW, x0 + dx));
  if (g.edge.includes("e")) x1 = Math.min(1, Math.max(x0 + minW, x1 + dx));
  if (g.edge.includes("n")) y0 = Math.max(0, Math.min(y1 - minH, y0 + dy));
  if (g.edge.includes("s")) y1 = Math.min(1, Math.max(y0 + minH, y1 + dy));
  return [x0, y0, x1, y1].map(round5);
}
function paintGesture(g) {
  const layer = $("overlays");
  for (const [m, rect] of plannedRects(g.target, g.start, g.rect)) {
    const node = m.kind === "det" ? layer.querySelector(`[data-region="${m.id}"]`) : layer.querySelector(`[data-drawn="${m.kind}:${m.index}"]`);
    if (!node) continue; // on another page
    place(node, rect);
    const tag = m.kind === "det" && layer.querySelector(`[data-tag="${m.id}"]`);
    if (tag) placeTag(tag, rect);
  }
  const frame = layer.querySelector(".edit-frame");
  if (frame) {
    place(frame, g.rect); sizeFrame(frame, g.rect);
    const [w, h] = mmSize(g.rect);
    const count = g.target.kind === "det" && state.scope === "group" ? members(g.target).length : 1;
    frame.querySelector(".edit-readout").textContent = t("edit.readout", {w: fmt(w), h: fmt(h)}) + (count > 1 ? " · " + t("edit.readoutAll", {n: count}) : "");
    frame.classList.add("live");
  }
}
$("sheet").addEventListener("pointermove", e => {
  if (!gesture || e.pointerId !== gesture.pointerId) return;
  const p = point(e, false);
  const r = $("sheet").getBoundingClientRect();
  if (!gesture.moved && Math.hypot((p[0] - gesture.origin[0]) * r.width, (p[1] - gesture.origin[1]) * r.height) < 3) return;
  gesture.moved = true;
  gesture.rect = gestureRect(gesture, p);
  paintGesture(gesture);
});
function endGesture(commit) {
  if (!gesture) return;
  const g = gesture;
  gesture = null;
  $("sheet").classList.remove("editing", "edit-move", ...EDGES.map(x => "edit-" + x));
  swallowClick = performance.now();
  if (commit && g.moved) commitEdit(g.target, g.start, g.rect);
  else renderOverlays();
}
$("sheet").addEventListener("pointerup", e => { if (gesture && e.pointerId === gesture.pointerId) endGesture(true); });
$("sheet").addEventListener("pointercancel", () => endGesture(false));
$("sheet").addEventListener("lostpointercapture", () => endGesture(true));

function nudge(key, shift, resize) {
  const info = selectedInfo();
  if (!info || info.page !== state.page || !canEdit()) return false;
  const p = state.job.pages[state.page - 1];
  const mm = shift ? 2 : .25;
  const dx = {ArrowLeft: -mm, ArrowRight: mm}[key] / (p.width * MM) || 0, dy = {ArrowUp: -mm, ArrowDown: mm}[key] / (p.height * MM) || 0;
  const r = info.rect;
  const next = resize ? [r[0], r[1], Math.max(r[0] + MIN_SIDE, r[2] + dx), Math.max(r[1] + MIN_SIDE, r[3] + dy)]
    : [r[0] + dx, r[1] + dy, r[2] + dx, r[3] + dy];
  commitEdit(info.target, r, fit(next), {coalesce: true});
  return true;
}

function renderEditor() {
  const box = $("box-editor");
  const info = selectedInfo();
  if (!info || info.page !== state.page) { box.hidden = true; return; }
  box.hidden = false;
  const isDet = info.target.kind === "det";
  const tag = $("be-tag");
  tag.replaceChildren(isDet ? info.letter : icon(info.target.kind === "manual" ? "pen" : "shield"));
  tag.style.setProperty("--hl", isDet ? colourFor(info.d.rule) : info.target.kind === "manual" ? "#ff9f6b" : "#1683a3");
  $("be-title").textContent = info.label;
  $("be-title").title = info.label;
  const adjusted = isDet && !!state.decisions.adjusted[info.target.id];
  $("be-meta").textContent = (isDet ? t("edit.meta", {letter: info.letter, n: info.page}) : t("review.onPage", {n: info.page})) + (adjusted ? " · " + t("edit.adjusted") : "");
  const grouped = isDet && info.size > 1;
  const all = grouped && state.scope === "group";
  $("be-scope").hidden = !grouped;
  if (grouped) {
    $("be-scope-one").setAttribute("aria-pressed", String(!all));
    $("be-scope-group").setAttribute("aria-pressed", String(all));
    $("be-scope-group-text").textContent = t("edit.group", {n: info.size, label: info.group});
    $("be-scope-group").title = t("edit.group", {n: info.size, label: info.group});
  }
  const p = state.job.pages[info.page - 1];
  const values = {left: info.rect[0] * p.width * MM, top: info.rect[1] * p.height * MM,
    width: (info.rect[2] - info.rect[0]) * p.width * MM, height: (info.rect[3] - info.rect[1]) * p.height * MM};
  for (const [key, value] of Object.entries(values)) {
    const input = $("be-" + key);
    if (document.activeElement !== input) input.value = fmt(value);
    input.disabled = !canEdit();
  }
  const groupAdjusted = all ? members(info.target).filter(m => state.decisions.adjusted[m.id]).length : 0;
  $("be-reset").hidden = !isDet;
  $("be-reset").disabled = state.busy || !(all ? groupAdjusted : adjusted);
  $("be-reset").textContent = all ? t("edit.resetAll", {n: info.size}) : t("edit.reset");
  $("be-delete").hidden = isDet;
  $("be-delete").disabled = state.busy;
  for (const id of ["be-grow", "be-shrink"]) $(id).disabled = !canEdit();
  $("be-hint").textContent = canEdit() ? t("edit.hint") : t("edit.needsInspect");
}
function editorInput() {
  const info = selectedInfo();
  if (!info || !canEdit()) return;
  const p = state.job.pages[info.page - 1];
  const read = id => { const v = parseFloat(String($(id).value).replace(",", ".")); return Number.isFinite(v) ? v : null; };
  const [left, top, width, height] = ["be-left", "be-top", "be-width", "be-height"].map(read);
  if ([left, top, width, height].some(v => v === null || v < 0) || width <= 0 || height <= 0) { renderEditor(); return; }
  const x0 = left / (p.width * MM), y0 = top / (p.height * MM);
  commitEdit(info.target, info.rect, fit([x0, y0, x0 + width / (p.width * MM), y0 + height / (p.height * MM)]));
  const now = selectedInfo();
  if (now) {
    const pg = state.job.pages[now.page - 1], r = now.rect;
    [["be-left", r[0] * pg.width], ["be-top", r[1] * pg.height], ["be-width", (r[2] - r[0]) * pg.width], ["be-height", (r[3] - r[1]) * pg.height]]
      .forEach(([id, v]) => { $(id).value = fmt(v * MM); });
  }
}
function grow(mm) {
  const info = selectedInfo();
  if (!info || !canEdit()) return;
  const p = state.job.pages[info.page - 1];
  const gx = mm / (p.width * MM), gy = mm / (p.height * MM);
  const r = info.rect;
  if (r[2] - r[0] + 2 * gx < MIN_SIDE * 2 || r[3] - r[1] + 2 * gy < MIN_SIDE * 2) { note(t("edit.tooSmall"), true); return; }
  commitEdit(info.target, r, fit([r[0] - gx, r[1] - gy, r[2] + gx, r[3] + gy]));
}
function resetBoxes() {
  if (state.busy) return;
  const info = selectedInfo();
  if (!info || info.target.kind !== "det") return;
  const all = state.scope === "group" && info.size > 1;
  remember();
  for (const m of all ? members(info.target) : [info.target]) delete state.decisions.adjusted[m.id];
  changed();
  note(t(all ? "note.resetAll" : "note.reset", {n: info.size}));
}
for (const id of ["be-left", "be-top", "be-width", "be-height"]) {
  $(id).addEventListener("change", editorInput);
  $(id).addEventListener("keydown", e => { if (e.key === "Enter") { e.preventDefault(); editorInput(); } });
}
$("be-grow").addEventListener("click", () => grow(.5));
$("be-shrink").addEventListener("click", () => grow(-.5));
$("be-reset").addEventListener("click", resetBoxes);
$("be-delete").addEventListener("click", () => { const s = state.selected; if (s && s.kind !== "det") deleteRegion(s.kind, s.index); });
$("be-done").addEventListener("click", () => select(null));
for (const [id, scope] of [["be-scope-one", "one"], ["be-scope-group", "group"]]) $(id).addEventListener("click", () => { state.scope = scope; renderEditor(); save(); });

function sweep() {
  // One reveal after a preview is built: the divider crosses the page once, then rests.
  if (matchMedia("(prefers-reduced-motion: reduce)").matches) return;
  const keys = [[0, 50], [450, 96], [1150, 4], [1600, 50]];
  const start = performance.now();
  const ease = x => x < .5 ? 2 * x * x : 1 - Math.pow(-2 * x + 2, 2) / 2;
  const frame = now => {
    if (splitting || state.view !== "compare") return;
    const t = Math.max(1, now - start);
    const i = keys.findIndex(k => k[0] > t);
    if (i === -1) { state.split = 50; $("sheet").style.setProperty("--split", "50%"); return; }
    const [t0, a] = keys[i - 1], [t1, b] = keys[i];
    state.split = a + (b - a) * ease((t - t0) / (t1 - t0));
    $("sheet").style.setProperty("--split", state.split + "%");
    requestAnimationFrame(frame);
  };
  requestAnimationFrame(frame);
}

/* ---------- Logo reference (shared by every file in the set) ---------- */
function resetLogo() {
  state.logoSha256 = "";
  state.logoFile = null;
  $("logo-file").value = "";
  $("logo-reference").hidden = true;
  $("logo-thumbnail").removeAttribute("src");
  $("logo-name").textContent = "";
}
function showLogo(sha, name, jobId = state.job.id) {
  $("logo-thumbnail").src = "/api/jobs/" + jobId + "/logo?v=" + sha;
  $("logo-name").textContent = name;
  $("logo-reference").hidden = false;
}
async function uploadLogo(rec, file) {
  const result = await request("/api/jobs/" + rec.job.id + "/logo", {method: "POST", raw: true, body: file, headers: {"Content-Type": file.type}});
  rec.job.logo = result.sha256;
  rec.plan = null; rec.candidate = null; rec.dirty = true; rec.stale = true;
  return result.sha256;
}
$("logo-file").addEventListener("change", () => {
  const file = $("logo-file").files[0];
  if (!file) return;
  task("logo", async () => {
    if (file.size > 5 * 1024 * 1024 || !["image/png", "image/jpeg", "image/webp"].includes(file.type)) throw new Error(t("err.logo"));
    let sha = "";
    for (const rec of state.files) sha = await uploadLogo(rec, file);
    state.logoSha256 = sha;
    state.logoFile = file;
    showLogo(sha, file.name);
    rulesChanged();
    note(t("note.logo"));
  }).finally(() => { $("logo-file").value = ""; });
});
$("clear-logo").addEventListener("click", () => task("logo", async () => {
  for (const rec of state.files) {
    await request("/api/jobs/" + rec.job.id + "/logo", {method: "DELETE"});
    rec.job.logo = ""; rec.plan = null; rec.candidate = null;
  }
  resetLogo();
  rulesChanged();
  note(t("note.logoRemoved"));
}));

/* ---------- Main actions ---------- */
const recCount = rec => !rec.plan ? 0 : rec.plan.pages.reduce((n, p) => n + p.detections.filter(d => !rec.decisions.ignored.includes(d.id)).length, 0) + rec.decisions.manual.length;
const recReady = rec => !!rec.plan && !rec.dirty && !!rec.candidate && !rec.stale;
async function detectFile(rec) {
  const plan = await request("/api/jobs/" + rec.job.id + "/detect", {method: "POST", body: {config: config(rec), forceOcr: $("force").checked}});
  Object.assign(rec, {plan, dirty: false, stale: true, candidate: null, downloaded: false, selected: null, error: ""});
  const ids = new Set(plan.pages.flatMap(p => p.detections.map(d => d.id)));
  rec.decisions.ignored = rec.decisions.ignored.filter(id => ids.has(id));
  rec.decisions.adjusted = Object.fromEntries(Object.entries(rec.decisions.adjusted || {}).filter(([id]) => ids.has(id)));
  if (!rec.pagesExplicit) { rec.decisions.removePages = []; rec.decisions.keepPages = []; rec.pagesInput = ""; }
  return plan;
}
async function previewFile(rec) {
  // Remember exactly what was sent: a change made while the preview builds leaves it out of date.
  const sent = JSON.stringify({...rec.decisions, output: output()});
  rec.candidate = await request("/api/jobs/" + rec.job.id + "/preview", {method: "POST",
    body: {planHash: rec.plan.planHash, decisions: JSON.parse(sent)}});
  Object.assign(rec, {stale: JSON.stringify({...rec.decisions, output: output()}) !== sent, seen: new Set(), downloaded: false, error: ""});
}
// Run one step over several files; a failure in one file is recorded and the rest carry on.
async function eachFile(list, stage, fn) {
  const failed = [];
  for (const [i, rec] of list.entries()) {
    if (cancelRequested) throw new Error(t("note.stopped"));
    follow(rec, i, list.length, stage);
    try { await fn(rec); }
    catch (err) {
      if (cancelRequested || /^Cancelled/.test(err.message) || err.message === window.I18N.error("Cancelled. Nothing was changed.")) throw err;
      if (list.length === 1) throw err;
      rec.error = err.message;
      failed.push(rec.job.name);
    }
  }
  return failed;
}
$("detect").addEventListener("click", () => task("detect", async () => {
  const pending = state.files.filter(r => r.dirty || !r.plan);
  const list = $("force").checked || !pending.length ? state.files : pending;
  const failed = await eachFile(list, "detect", detectFile);
  syncPagesInput();
  setTab("review", false);
  renderAll();
  if (batch()) {
    const areas = list.reduce((n, r) => n + recCount(r), 0);
    const withAreas = list.filter(r => recCount(r)).length;
    note(t("note.foundBatch", {areas: plural(areas, "areas"), n: withAreas, total: list.length})
      + (failed.length ? " " + t("note.failed", {list: failed.join(", ")}) : ""), !!failed.length);
  } else {
    const plan = state.plan;
    const n = plan.pages.reduce((sum, p) => sum + p.detections.length, 0);
    const reused = plan.ocr.reusedPages ? " " + t("note.ocrReused", {n: plan.ocr.reusedPages}) : "";
    note((n ? t("note.found", {areas: plural(n, "areas"), pages: plural(plan.pages.filter(p => p.detections.length).length, "pagesOn")}) : t("note.foundNone")) + reused);
  }
  if (state.files.some(r => recCount(r)) && !activeCount(pageData())) nextFinding(true);
}));
async function buildPreview() {
  await task("apply", async () => {
    await previewFile(state.cur);
    $("download-box").hidden = true;
    if (state.view === "original") state.split = 50;
    setView(state.view === "original" ? "compare" : state.view);
    if (state.view === "compare") sweep();
    setTab("export", false);
    renderAll();
    const left = state.candidate.residual?.length;
    note(left ? t("note.previewResidual") : t("note.preview"), !!left);
  });
}
async function buildAllPreviews() {
  await task("apply", async () => {
    if (state.files.some(r => r.dirty || !r.plan)) throw new Error(t("batch.needsFind"));
    const list = state.files.filter(r => !recReady(r));
    const failed = await eachFile(list, "apply", previewFile);
    if (state.view === "original") { state.split = 50; setView("compare"); }
    setTab("export", false);
    renderAll();
    const left = state.files.filter(r => r.candidate?.residual?.length).length;
    note(failed.length ? t("note.failed", {list: failed.join(", ")}) : left ? t("note.previewResidual") : t("note.previewsBuilt", {files: plural(state.files.length, "files")}), !!(failed.length || left));
  });
}
$("preview").addEventListener("click", buildPreview);
document.querySelector('.stale-veil [data-action="preview"]').addEventListener("click", buildPreview);
$("preview-2").addEventListener("click", () => (batch() ? buildAllPreviews() : buildPreview()));
function showDownload(url, filename, meta) {
  const link = $("download-clean");
  link.href = url;
  link.setAttribute("download", filename);
  $("download-name").textContent = filename;
  $("download-meta").textContent = meta;
  $("download-box").hidden = false;
  link.click();
  requestAnimationFrame(() => $("download-box").scrollIntoView({behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth", block: "nearest"}));
}
let exportConfirmed = false;
$("confirm-export").addEventListener("click", e => {
  if (e.target === $("confirm-export") || e.target.closest("[data-close]")) $("confirm-export").close();
  if (e.target.closest("#confirm-export-yes")) { $("confirm-export").close(); exportConfirmed = true; $("export").click(); }
});
$("export").addEventListener("click", () => task("export", async () => {
  if (!batch()) {
    const result = await request("/api/jobs/" + state.job.id + "/approve-export", {method: "POST", body: {sha256: state.candidate.sha256, reviewed: true}});
    $("download-box").dataset.zip = "";
    showDownload(result.downloadUrl, result.filename, [plural(state.candidate.outputPages, "pages"), bytes(state.candidate.bytes),
      state.candidate.output.searchable ? t("dl.searchable") : t("dl.imageOnly")].join(" · "));
    state.downloaded = true;
    renderFlow(); buttons();
    note(t("note.exported"));
    return;
  }
  // Every file goes into one zip. Ask first if any file has result pages nobody has looked at.
  if (state.files.some(r => r.dirty || !r.plan)) throw new Error(t("batch.needsFind"));
  const unchecked = state.files.filter(r => fileStatus(r)[0] !== "checked");
  if (unchecked.length && !exportConfirmed) {
    const names = unchecked.slice(0, 5).map(r => r.job.name).join(", ") + (unchecked.length > 5 ? " " + t("cx.more", {n: unchecked.length - 5}) : "");
    $("confirm-export-p").textContent = t("cx.p", {list: names});
    queueMicrotask(() => $("confirm-export").showModal());
    return;
  }
  exportConfirmed = false;
  const missing = state.files.filter(r => !recReady(r));
  const failed = await eachFile(missing, "apply", previewFile);
  if (failed.length) throw new Error(t("note.failed", {list: failed.join(", ")}));
  progress.job = null; progress.prefix = "";
  progressText(t("zip.start"));
  const result = await request("/api/bundle", {method: "POST",
    body: {items: state.files.map(r => ({id: r.job.id, sha256: r.candidate.sha256})), reviewed: true}});
  $("download-box").dataset.zip = "1";
  showDownload(result.downloadUrl, result.filename, [plural(result.files.length, "files"), bytes(result.bytes)].join(" · "));
  for (const rec of state.files) rec.downloaded = true;
  renderAll();
  note(t("note.zip", {files: plural(result.files.length, "files")}));
}));
$("cancel").addEventListener("click", () => {
  cancelRequested = true;
  const job = progress.job || state.job;
  if (job) request("/api/jobs/" + job.id + "/cancel", {method: "POST"}).catch(() => {});
  $("progress-text").textContent = t("cancelling");
});
function download(blob, name) {
  const url = URL.createObjectURL(blob);
  const a = el("a", {href: url, download: name});
  document.body.append(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 30000);
}
$("report").addEventListener("click", () => task("report", async () => {
  const report = await request("/api/jobs/" + state.job.id + "/report");
  download(new Blob([JSON.stringify({...report, decisions: state.decisions}, null, 2)], {type: "application/json"}), "private-report.json");
  note(t("note.report"));
}));
$("save-rules").addEventListener("click", () => {
  const c = config();
  c.imageDigests = [];
  delete c.logoSha256;
  download(new Blob([JSON.stringify(c, null, 2)], {type: "application/json"}), "pdfscan-settings.json");
  note(t("note.rulesSaved"));
});
$("load-rules").addEventListener("change", async () => {
  try {
    const file = $("load-rules").files[0];
    if (!file) return;
    if (file.size > 100000) throw new Error(t("err.rulesBig"));
    const c = JSON.parse(await file.text());
    if (!c || !Array.isArray(c.text) || c.text.some(x => typeof x !== "string")) throw new Error(t("err.rules"));
    setConfig(c);
    state.imageDigests = [];
    rulesChanged();
    note(t("note.rulesLoaded"));
  } catch (err) { note(err instanceof SyntaxError ? t("err.rules") : err.message, true); }
  finally { $("load-rules").value = ""; }
});

/* ---------- Wiring ---------- */
$("demo").addEventListener("click", () => openDemo());
$("file").addEventListener("change", () => { const f = [...$("file").files]; $("file").value = ""; if (f.length) openDocuments(f); });
$("add-files").addEventListener("change", () => { const f = [...$("add-files").files]; $("add-files").value = ""; $("file-menu").hidePopover?.(); if (f.length) openDocuments(f, {add: true}); });
for (const target of [$("dropzone"), document.body]) {
  target.addEventListener("dragover", e => { if (e.dataTransfer?.types?.includes("Files")) { e.preventDefault(); $("dropzone").classList.add("dragging"); } });
  target.addEventListener("dragleave", e => { if (!e.relatedTarget) $("dropzone").classList.remove("dragging"); });
  target.addEventListener("drop", e => {
    e.preventDefault(); $("dropzone").classList.remove("dragging");
    const f = e.dataTransfer?.files?.[0];
    if (f && !state.busy) openDocuments([...e.dataTransfer.files], {add: !!state.files.length});
  });
}
function openConfirm() {
  $("confirm-delete-h").textContent = batch() ? t("confirm.hMany", {n: state.files.length}) : t("confirm.h");
  $("confirm-delete").showModal();
}
$("clear").addEventListener("click", openConfirm);
// Place the file menu under its button (popovers open in the top layer, centred by default).
$("file-menu").addEventListener("beforetoggle", e => {
  if (e.newState !== "open") return;
  renderFileMenu();
  const r = $("file-switch").getBoundingClientRect();
  const menu = $("file-menu");
  const width = Math.min(420, innerWidth - 24);
  menu.style.width = width + "px";
  menu.style.left = Math.max(12, Math.min(r.left, innerWidth - width - 12)) + "px";
  menu.style.top = r.bottom + 8 + "px";
});
$("confirm-delete").addEventListener("click", e => {
  if (e.target === $("confirm-delete") || e.target.closest("[data-close]")) $("confirm-delete").close();
  if (e.target.closest("#confirm-delete-yes")) { $("confirm-delete").close(); closeDocument(); }
});
for (const id of RULE_INPUTS) $(id).addEventListener(id === "targets" ? "input" : "change", rulesChanged);
for (const node of [...document.querySelectorAll('input[name="fill"]'), $("compact"), $("searchable")]) node.addEventListener("change", () => {
  for (const rec of state.files) { rec.stale = true; rec.downloaded = false; }
  $("download-box").hidden = true; renderAll();
});
$("remove-pages").addEventListener("input", () => {
  if (!state.job || state.busy) return;
  try {
    const pages = parsePages($("remove-pages").value, state.job.pages.length);
    state.pagesError = "";
    remember();
    setRemovedPages(pages);
    state.stale = true;
  } catch (err) { state.pagesError = err.message; }
  renderThumbs(); renderReview(); renderSheet(); buttons(); save();
});
$("drop-page").addEventListener("change", () => {
  remember();
  const pages = new Set(state.job.pages.map(p => p.number).filter(isRemoved));
  if ($("drop-page").checked) pages.add(state.page); else pages.delete(state.page);
  if (pages.size >= state.job.pages.length) { $("drop-page").checked = false; state.history.pop(); note(t("pages.all"), true); return; }
  state.pagesError = "";
  setRemovedPages([...pages].sort((a, b) => a - b));
  changed();
});
$("prev").addEventListener("click", () => goto(state.page - 1));
$("next").addEventListener("click", () => goto(state.page + 1));
$("page-input").addEventListener("change", () => { const n = parseInt($("page-input").value, 10); if (n) goto(n); else $("page-input").value = state.page; });
$("page-input").addEventListener("focus", () => $("page-input").select());
$("next-finding").addEventListener("click", () => nextFinding());
$("next-unseen").addEventListener("click", nextUnseen);
$("zoom-in").addEventListener("click", () => setZoom(state.zoom * 1.25));
$("zoom-out").addEventListener("click", () => setZoom(state.zoom / 1.25));
$("zoom-label").addEventListener("click", () => setZoom(1));
$("undo").addEventListener("click", () => undo(false));
$("redo").addEventListener("click", () => undo(true));
$("mat").addEventListener("wheel", e => { if (e.ctrlKey && state.job) { e.preventDefault(); setZoom(state.zoom * (e.deltaY < 0 ? 1.1 : 1 / 1.1)); } }, {passive: false});
document.querySelectorAll(".tool").forEach(b => b.addEventListener("click", () => setMode(b.dataset.mode)));
document.querySelectorAll(".views button").forEach(b => b.addEventListener("click", () => setView(b.dataset.view)));
document.querySelectorAll("[data-tab]").forEach(b => b.addEventListener("click", () => setTab(b.dataset.tab)));
document.querySelector(".tabs").addEventListener("keydown", e => {
  if (!["ArrowLeft", "ArrowRight"].includes(e.key)) return;
  const order = ["find", "review", "export"];
  const i = (order.indexOf(state.tab) + (e.key === "ArrowRight" ? 1 : 2)) % 3;
  setTab(order[i]);
  document.getElementById("tab-" + order[i]).focus();
  e.preventDefault();
});
$("shortcuts-open").addEventListener("click", () => $("shortcuts").showModal());
$("shortcuts").addEventListener("click", e => { if (e.target === $("shortcuts") || e.target.closest("[data-close]")) $("shortcuts").close(); });
new ResizeObserver(() => { if (state.job) renderSheet(); }).observe($("mat"));
window.addEventListener("resize", () => setTab(state.tab, false));

document.addEventListener("keydown", e => {
  const typing = e.target.closest?.('textarea, select, [contenteditable], input:not([type="checkbox"]):not([type="radio"])');
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "z" && !typing) { e.preventDefault(); undo(e.shiftKey); return; }
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "y" && !typing) { e.preventDefault(); undo(true); return; }
  if (e.key === "Escape") {
    if (gesture) { endGesture(false); renderAll(); return; }
    if (drawing) cancelDrawing();
    else if (state.selected && !typing) select(null);
    else if (state.mode !== "inspect") setMode("inspect");
    return;
  }
  if (e.altKey && !typing && state.selected && e.key.startsWith("Arrow") && nudge(e.key, e.shiftKey, true)) { e.preventDefault(); return; }
  if (typing || e.ctrlKey || e.metaKey || e.altKey || $("shortcuts").open || $("confirm-delete").open || $("confirm-export").open) return;
  if (e.key === "?") { $("shortcuts").showModal(); return; }
  if (!state.job) return;
  if (state.selected && e.key.startsWith("Arrow") && !e.target.closest(".tabs, .seg, .rail") && nudge(e.key, e.shiftKey, false)) { e.preventDefault(); return; }
  const key = e.key.toLowerCase();
  const actions = {
    arrowleft: () => goto(state.page - 1), arrowright: () => goto(state.page + 1), pageup: () => goto(state.page - 1), pagedown: () => goto(state.page + 1),
    home: () => goto(1), end: () => goto(state.job.pages.length), n: () => nextFinding(),
    "[": () => switchFile(-1), "]": () => switchFile(1),
    v: () => setMode("inspect"), r: () => setMode("remove"), k: () => setMode("keep"), i: () => setMode("image"),
    1: () => setView("original"), 2: () => setView("compare"), 3: () => setView("result"),
    "+": () => setZoom(state.zoom * 1.25), "=": () => setZoom(state.zoom * 1.25), "-": () => setZoom(state.zoom / 1.25), 0: () => setZoom(1),
    delete: () => state.selected && state.selected.kind !== "det" && deleteRegion(state.selected.kind, state.selected.index),
    backspace: () => state.selected && state.selected.kind !== "det" && deleteRegion(state.selected.kind, state.selected.index),
  };
  if (actions[key] && !(state.busy && "vrki".includes(key))) { e.preventDefault(); actions[key](); }
});

/* ---------- Language and theme ---------- */
function applyLanguage(lang) {
  window.I18N.apply(lang);
  try { localStorage.setItem("pdfscan.lang", lang); } catch { /* preference only */ }
  if (state.job) renderAll();
  setTab(state.tab, false);
}
$("lang").addEventListener("click", () => applyLanguage(window.I18N.lang === "zh" ? "en" : "zh"));
function applyTheme(theme) {
  if (theme) document.documentElement.dataset.theme = theme; else delete document.documentElement.dataset.theme;
}
$("theme").addEventListener("click", () => {
  const dark = document.documentElement.dataset.theme ? document.documentElement.dataset.theme === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
  const next = dark ? "light" : "dark";
  applyTheme(next);
  try { localStorage.setItem("pdfscan.theme", next); } catch { /* preference only */ }
});

/* ---------- Boot ---------- */
(async () => {
  let lang = null, theme = null;
  try { lang = localStorage.getItem("pdfscan.lang"); theme = localStorage.getItem("pdfscan.theme"); } catch { /* defaults */ }
  applyTheme(theme);
  window.I18N.apply(lang || (/^zh/i.test(navigator.language) ? "zh" : "en"));
  setTab("find", false);
  try {
    const c = await request("/api/config");
    state.token = c.token;
    state.models = c.models;
    state.ocrEngine = c.ocrEngine;
    // PP-OCR reads English and Chinese together, so the language choice only matters for the fallback.
    $("languages").closest(".field").hidden = c.ocrEngine === "ppocr";
    if (c.ocrEngine !== "ppocr") note(t(c.models.includes("eng") ? "err.ppocr" : "err.models"), !c.models.includes("eng"));
    await restore();
  } catch (err) { note(t("err.connect", {message: err.message}), true); }
  buttons();
  document.body.classList.add("ready");
  if (new URLSearchParams(location.search).has("demo") && !state.job) openDemo();
})();
