/* ==========================================================================
   Fig Foundry -- canvas composer
   The atom is data. Drag signals onto a canvas to make plots; move/resize/snap
   them; style each plot's axes, ticks, title, colors, and artists. The preview
   is the generated code (executed server-side), so it is WYSIWYG by construction.
   ========================================================================== */
"use strict";

const $ = (s) => document.querySelector(s);
const figImg = $("#figImg"), overlay = $("#overlay"), guides = $("#guides"), stage = $("#stage"), stageInner = $("#stageInner");
const SNAP_PX = 8;

const state = { slug: null, name: null, spec: null, render: null, signals: [], selected: null, selection: new Set(), zoom: 1, pan: { x: 0, y: 0 }, gridCols: 3, nudging: false, history: [], future: [], sliderSnapshot: null, signalSel: new Set() };

/* ---------------- api ---------------- */
async function api(path, opts) {
  const r = await fetch(path, opts);
  if (!r.ok) { let d = ""; try { d = (await r.json()).detail || ""; } catch (e) {} throw new Error(d || r.statusText); }
  return r.json();
}
const get = (p) => api(p);
const post = (p, o) => api(p, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(o) });

/* ---------------- spec helpers ---------------- */
const clone = (o) => JSON.parse(JSON.stringify(o));
function getPath(o, path) { return path.reduce((a, k) => (a == null ? a : a[k]), o); }
function setPath(o, path, val) { let a = o; for (let i = 0; i < path.length - 1; i++) a = a[path[i]]; a[path[path.length - 1]] = val; }
const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
const r4 = (x) => Math.round(x * 10000) / 10000;

/* ---------------- coordinate model: rect=[left,bottom,w,h] (figure frac, bottom-origin) ---------------- */
function rectToCss(rect) {
  return { left: (rect[0] * 100) + "%", top: ((1 - (rect[1] + rect[3])) * 100) + "%", width: (rect[2] * 100) + "%", height: (rect[3] * 100) + "%" };
}
function toFig(clientX, clientY) { const r = figImg.getBoundingClientRect(); return { fx: (clientX - r.left) / r.width, fyTop: (clientY - r.top) / r.height }; }
// Instant client-side tracking during a nudge. Plots are repositioned absolutely (the box
// IS the rect); movable elements (titles/texts/labels) are shifted RELATIVELY by the delta,
// because repositioning them to their anchor point (vs the server's text bbox) causes a jump.
function trackOverlays(ids, dx, dy) {
  const plotIds = new Set(ids.filter((id) => id.startsWith("plot:")).map((id) => id.slice(5)));
  overlay.querySelectorAll(".plotbox").forEach((d) => { if (plotIds.has(d.dataset.pid)) { const p = state.spec.plots.find((x) => x.id === d.dataset.pid); if (p) Object.assign(d.style, rectToCss(p.rect)); } });
  for (const id of ids) {
    if (id.startsWith("plot:")) continue;
    const div = document.querySelector(`.mov[data-id="${id}"]`);
    if (!div) continue;
    div.style.left = (parseFloat(div.style.left) + dx * 100) + "%";
    div.style.top = (parseFloat(div.style.top) - dy * 100) + "%";
  }
}
function offCanvasCount() {
  let n = 0;
  for (const p of state.spec.plots) { const r = p.rect; if (r[0] < -0.001 || r[1] < -0.001 || r[0] + r[2] > 1.001 || r[1] + r[3] > 1.001) n++; }
  for (const f of state.spec.figure_artists) { const x = f.loc ? f.loc[0] : f.x, y = f.loc ? f.loc[1] : f.y; if ((x !== undefined && (x < -0.001 || x > 1.001)) || (y !== undefined && (y < -0.001 || y > 1.001))) n++; }
  return n;
}

/* ---------------- render pipeline ---------------- */
let draftTimer = null, fullTimer = null, renderSeq = 0, renderInFlight = false, renderPending = false, renderPendingDraft = false;
async function doRender(draft) {
  // Cap at one in-flight render so long nudges don't pile requests onto the server.
  // A request arriving mid-render is marked pending and fired (with the latest spec)
  // as soon as the current one completes.
  if (renderInFlight) { renderPending = true; renderPendingDraft = draft; return; }
  renderInFlight = true;
  const seq = ++renderSeq;
  try {
    const r = await post(`/api/canvases/${state.slug}/render`, { spec: state.spec, draft });
    if (seq === renderSeq) { state.render = r; applyRender(); }
  } catch (e) { if (seq === renderSeq) toast("Render failed: " + e.message, true); }
  finally {
    renderInFlight = false;
    if (renderPending) { const d = renderPendingDraft; renderPending = false; doRender(d); }
  }
}
// fast low-res render during interaction; full-res when things settle
function scheduleDraft() { clearTimeout(fullTimer); fullTimer = null; if (draftTimer) return; draftTimer = setTimeout(() => { draftTimer = null; doRender(true); }, 50); }
function scheduleFull() { clearTimeout(draftTimer); draftTimer = null; if (fullTimer) return; fullTimer = setTimeout(() => { fullTimer = null; doRender(false); }, 250); }

function applyRender() {
  const r = state.render; if (!r) return;
  try {
    figImg.src = "data:" + r.mime + ";base64," + r.img;
    figImg.style.width = r.img_w + "px";
    figImg.style.height = r.img_h + "px";  // natural size; the stage-inner transform handles zoom/pan
    applyTransform();
    if (r.draft) return;  // draft: update the image only; overlays are tracked client-side during interaction
    buildOverlays();
    buildLayers();
    const oc = offCanvasCount();
    const hint = $("#canvasHint");
    if (hint) hint.innerHTML = oc
      ? `<b style="color:var(--danger)">${oc} element${oc > 1 ? "s" : ""} off the canvas</b> &mdash; select it in Layers and drag it back, or enlarge the canvas`
      : "Drag a signal to add a plot &nbsp;&middot;&nbsp; Drag to move, handles to resize (snaps) &nbsp;&middot;&nbsp; Shift+click to multi-select &nbsp;&middot;&nbsp; Arrows to nudge (Shift = bigger) &nbsp;&middot;&nbsp; Drag empty space / space+drag to pan";
    const ae = document.activeElement;
    const editing = ae && (ae.tagName === "INPUT" && ae.type === "text") || ae && ae.tagName === "SELECT";
    if (!editing) buildInspector();
  } catch (err) {
    console.error("applyRender:", err);
    showErrBanner(err);
  }
}
function showErrBanner(err) {
  let b = document.getElementById("errBanner");
  if (!b) { b = document.createElement("div"); b.id = "errBanner"; document.body.appendChild(b); }
  b.className = "err-banner";
  const stack = err && err.stack ? err.stack.split("\n").slice(1, 4).join("  ↲  ") : "";
  b.innerHTML = `<b>⚠ ${err && err.message ? err.message : err}</b>${stack ? `<span class="eb-stack">${stack}</span>` : ""}<span class="eb-x" title="Dismiss">×</span>`;
  b.querySelector(".eb-x").onclick = () => b.remove();
}

/* ---------------- snapping ---------------- */
function snapRect(rect, others, imgW, imgH) {
  const thrX = SNAP_PX / imgW, thrY = SNAP_PX / imgH;
  const tx = [0, 0.25, 0.5, 0.75, 1], ty = [0, 0.25, 0.5, 0.75, 1];
  for (const o of others) { tx.push(o[0], o[0] + o[2] / 2, o[0] + o[2]); ty.push(o[1], o[1] + o[3] / 2, o[1] + o[3]); }
  const g = [];
  const edgesX = [[rect, 0, v => rect[0] = v], [null, 1, v => rect[0] = v - rect[2] / 2], [null, 2, v => rect[0] = v - rect[2]]];
  const valsX = [rect[0], rect[0] + rect[2] / 2, rect[0] + rect[2]];
  edgesX.forEach((e, i) => { for (const t of tx) { if (Math.abs(valsX[i] - t) < thrX) { e[2](t); g.push({ axis: "v", f: t }); break; } } });
  const valsY = [rect[1], rect[1] + rect[3] / 2, rect[1] + rect[3]];
  const edgesY = [[0, v => rect[1] = v], [1, v => rect[1] = v - rect[3] / 2], [2, v => rect[1] = v - rect[3]]];
  edgesY.forEach((e, i) => { for (const t of ty) { if (Math.abs(valsY[i] - t) < thrY) { e[1](t); g.push({ axis: "h", f: t }); break; } } });
  rect[0] = clamp(rect[0], 0, 1); rect[1] = clamp(rect[1], 0, 1);
  rect[2] = clamp(rect[2], 0.03, 1 - rect[0]); rect[3] = clamp(rect[3], 0.03, 1 - rect[1]);
  return g;
}
function drawGuides(gs) {
  guides.innerHTML = "";
  const W = stageInner.offsetWidth, H = stageInner.offsetHeight;
  for (const gd of gs) {
    const el = document.createElement("div");
    el.className = "guide " + (gd.axis === "v" ? "v" : "h");
    if (gd.axis === "v") { el.style.left = (gd.f * W) + "px"; el.style.top = "0"; el.style.height = H + "px"; }
    else { el.style.top = ((1 - gd.f) * H) + "px"; el.style.left = "0"; el.style.width = W + "px"; }
    guides.appendChild(el);
  }
}

/* ---------------- overlays ---------------- */
function buildOverlays() {
  overlay.innerHTML = "";
  const r = state.render;
  for (let i = 0; i < state.spec.plots.length; i++) {
    const p = state.spec.plots[i];
    const box = document.createElement("div");
    box.className = "plotbox"; box.dataset.pid = p.id;
    Object.assign(box.style, rectToCss(p.rect));
    for (const h of ["nw", "n", "ne", "e", "se", "s", "sw", "w"]) { const d = document.createElement("div"); d.className = "rh " + h; d.dataset.h = h; box.appendChild(d); }
    makeMovable(box, "plot:" + p.id, "box");
    if (is3D(p)) { box.title = "Double-click to adjust the 3-D view"; box.addEventListener("dblclick", (e) => { e.stopPropagation(); openView3D(p.id); }); }
    overlay.appendChild(box);
  }
  for (const h of r.hotspots) {
    if (h.id.startsWith("plot:")) continue;
    const div = document.createElement("div");
    div.className = "mov" + (isCanvasMovable(h.id) ? "" : " inspect-only");
    div.dataset.id = h.id;
    div.style.left = (h.x / r.img_w * 100) + "%"; div.style.top = (h.top / r.img_h * 100) + "%";
    div.style.width = (h.w / r.img_w * 100) + "%"; div.style.height = (h.h / r.img_h * 100) + "%";
    makeMovable(div, h.id, "mov");
    overlay.appendChild(div);
  }
  refreshSelection();
}
function isCanvasMovable(id) {
  if (id.startsWith("title:")) return true;
  if (id.startsWith("figartist:")) return true;
  if (id.startsWith("artist:")) {
    const parts = id.split(":"); const pid = parts[1], j = +parts[2];
    const p = state.spec.plots.find((x) => x.id === pid);
    const art = p && p.artists[j];
    return art && art.type === "text" && art.axes_frac;
  }
  return false;
}

function refreshSelection() {
  overlay.querySelectorAll(".plotbox").forEach((d) => d.classList.toggle("selected", state.selection.has("plot:" + d.dataset.pid)));
  overlay.querySelectorAll(".mov").forEach((d) => d.classList.toggle("selected", state.selection.has(d.dataset.id)));
  document.querySelectorAll(".layer").forEach((l) => l.classList.toggle("active", l.dataset.id && state.selection.has(l.dataset.id)));
}

/* ---------------- move / resize / drag ---------------- */
function makeMovable(el, id, mode) {
  el.addEventListener("pointerdown", (e) => {
    if (e.button !== 0) return;
    e.preventDefault(); e.stopPropagation();
    const shift = e.shiftKey;
    const handle = (e.target.dataset && e.target.dataset.h) || null;
    const startF = toFig(e.clientX, e.clientY);
    let moved = false, onMove, onUp, sx = 0, sy = 0;
    if (mode === "box") {
      const p = state.spec.plots.find((x) => x.id === id.slice(5));
      const rect0 = p.rect.slice();
      const dragSet = (state.selection.size > 1 && state.selection.has(id) && handle === null)
        ? state.spec.plots.filter((x) => state.selection.has("plot:" + x.id)).map((x) => ({ p: x, r0: x.rect.slice() }))
        : [{ p, r0: rect0 }];
      const others = state.spec.plots.filter((x) => x.id !== p.id).map((x) => x.rect);
      const r = state.render;
      onMove = (ev) => {
        const F = toFig(ev.clientX, ev.clientY);
        const dxF = F.fx - startF.fx, dyBF = startF.fyTop - F.fyTop;
        if (handle === null) {
          const rect = rect0.slice(); rect[0] += dxF; rect[1] += dyBF;
          const guides = snapRect(rect, others, r.img_w, r.img_h);
          const adx = rect[0] - rect0[0], ady = rect[1] - rect0[1];
          for (const d of dragSet) d.p.rect = [clamp(r4(d.r0[0] + adx), 0, 1), clamp(r4(d.r0[1] + ady), 0, 1), d.r0[2], d.r0[3]];
          dragSet.forEach((d) => { const b2 = document.querySelector(`.plotbox[data-pid="${d.p.id}"]`); if (b2) Object.assign(b2.style, rectToCss(d.p.rect)); });
          drawGuides(guides);
        } else {
          const rect = rect0.slice();
          let l = rect0[0], b = rect0[1], rr = rect0[0] + rect0[2], t = rect0[1] + rect0[3];
          const cx = F.fx, cb = 1 - F.fyTop;
          if (handle.includes("w")) l = cx; if (handle.includes("e")) rr = cx;
          if (handle.includes("n")) t = cb; if (handle.includes("s")) b = cb;
          rect[0] = Math.min(l, rr); rect[2] = Math.abs(rr - l);
          rect[1] = Math.min(b, t); rect[3] = Math.abs(t - b);
          p.rect = rect;
          drawGuides(snapRect(rect, others, r.img_w, r.img_h));  // snaps mutate rect (=== p.rect) in place
          Object.assign(el.style, rectToCss(rect));
        }
        scheduleDraft();
        moved = true;
      };
    } else {
      sx = e.clientX; sy = e.clientY;
      onMove = (ev) => {
        const dx = ev.clientX - sx, dy = ev.clientY - sy;
        if (dx || dy) { moved = true; el.style.transform = `translate(${dx}px,${dy}px)`; scheduleDraft(); }
      };
    }
    onUp = (ev) => {
      document.removeEventListener("pointermove", onMove);
      document.removeEventListener("pointerup", onUp);
      drawGuides([]);
      el.style.transform = "";
      if (!moved) { select(id, shift); return; }
      if (mode !== "box") commitArtDrag(id, ev.clientX - sx, ev.clientY - sy);
      commit();
    };
    document.addEventListener("pointermove", onMove);
    document.addEventListener("pointerup", onUp);
  });
}

function commitArtDrag(id, dxPx, dyPx) {
  // Apply the drag DELTA (in figure fractions) to the element's own coordinates, so the
  // element lands exactly where the user dropped it (the grab offset is preserved).
  const rect = figImg.getBoundingClientRect();
  const dxF = dxPx / rect.width, dyF = -dyPx / rect.height;
  const r = state.spec;
  if (id.startsWith("title:")) {
    const pid = id.slice(6); const p = r.plots.find((x) => x.id === pid);
    p.title.x = r4(clamp(p.title.x + dxF / p.rect[2], -0.6, 1.6));
    p.title.y = r4(clamp(p.title.y + dyF / p.rect[3], -0.6, 1.6));
    p.title.visible = true;
  } else if (id.startsWith("artist:")) {
    const parts = id.split(":"); const pid = parts[1], j = +parts[2];
    const p = r.plots.find((x) => x.id === pid); const a = p && p.artists[j];
    if (a && a.type === "text" && a.axes_frac) {
      a.x = r4(clamp(a.x + dxF / p.rect[2], -0.6, 1.6));
      a.y = r4(clamp(a.y + dyF / p.rect[3], -0.6, 1.6));
    }
  } else if (id.startsWith("figartist:")) {
    const j = +id.slice(10); const fa = r.figure_artists[j];
    if (fa.loc) { fa.loc[0] = r4(clamp(fa.loc[0] + dxF, 0, 1.2)); fa.loc[1] = r4(clamp(fa.loc[1] + dyF, -0.2, 1.2)); }
    else { fa.x = r4(clamp((fa.x || 0) + dxF, -0.1, 1.1)); fa.y = r4(clamp((fa.y || 0) + dyF, -0.1, 1.1)); }
  }
}

/* ---------------- selection / history / persist ---------------- */
function select(id, additive) {
  if (additive) {
    if (state.selection.has(id)) state.selection.delete(id); else state.selection.add(id);
    state.selected = state.selection.has(id) ? id : (state.selection.size ? [...state.selection].pop() : null);
  } else {
    state.selection = id ? new Set([id]) : new Set();
    state.selected = id;
  }
  refreshSelection();
  buildInspector();
}
function pushHist() { state.history.push(clone(state.spec)); if (state.history.length > 60) state.history.shift(); state.future = []; updateHist(); }
function undo() { if (!state.history.length) return; state.future.push(clone(state.spec)); state.spec = state.history.pop(); state.sliderSnapshot = null; updateHist(); commit(); }
function redo() { if (!state.future.length) return; state.history.push(clone(state.spec)); state.spec = state.future.pop(); state.sliderSnapshot = null; updateHist(); commit(); }
function updateHist() { $("#undoBtn").disabled = !state.history.length; $("#redoBtn").disabled = !state.future.length; }
async function commit() {
  try { await api(`/api/canvases/${state.slug}`, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ spec: state.spec }) }); }
  catch (e) { toast("Save failed: " + e.message, true); }  // a failed save must not skip the full render
  await doRender(false);
}

let nudgePushed = false, nudgeTimer = null;
function nudgeSelected(dx, dy) {
  const ids = state.selection.size ? [...state.selection] : (state.selected ? [state.selected] : []);
  if (!ids.length) return;
  if (!nudgePushed) { pushHist(); nudgePushed = true; }
  for (const id of ids) nudgeOne(id, dx, dy);
  trackOverlays(ids, dx, dy);  // instant client-side tracking; content follows via the draft render
  scheduleDraft();
  clearTimeout(nudgeTimer);
  nudgeTimer = setTimeout(() => { nudgePushed = false; commit(); }, 350);
}
function nudgeOne(id, dx, dy) {
  const [kind, pid, j] = id.split(":");
  if (kind === "plot") { const p = state.spec.plots.find((x) => x.id === pid); if (p) { p.rect[0] = clamp(r4(p.rect[0] + dx), 0, 1); p.rect[1] = clamp(r4(p.rect[1] + dy), 0, 1); } }
  else if (kind === "title") { const p = state.spec.plots.find((x) => x.id === pid); const t = p && p.title; if (t) { t.x = r4(clamp(t.x + dx / p.rect[2], -0.6, 1.6)); t.y = r4(clamp(t.y + dy / p.rect[3], -0.6, 1.6)); t.visible = true; } }
  else if (kind === "figartist") { const fa = state.spec.figure_artists[+pid]; if (fa) { if (fa.loc) { fa.loc[0] = r4(clamp(fa.loc[0] + dx, 0, 1.2)); fa.loc[1] = r4(clamp(fa.loc[1] + dy, -0.2, 1.2)); } else { fa.x = r4(clamp((fa.x || 0) + dx, -0.1, 1.1)); fa.y = r4(clamp((fa.y || 0) + dy, -0.1, 1.1)); } } }
  else if (kind === "artist") { const p = state.spec.plots.find((x) => x.id === pid); const a = p && p.artists[j]; if (a) { if (a.type === "text" && a.axes_frac) { a.x = r4(clamp(a.x + dx / p.rect[2], -0.6, 1.6)); a.y = r4(clamp(a.y + dy / p.rect[3], -0.6, 1.6)); } else if (a.type === "vline") a.x = r4(a.x + dx * 8); else if (a.type === "hline") a.y = r4(a.y + dy * 8); else if (a.type === "circle" || a.type === "ellipse") { a.x = r4(a.x + dx * 8); a.y = r4(a.y + dy * 8); } } }
}

/* ---------------- signal drag -> create plot ---------------- */
// The backend lists signals as {id, name, length, dtype}. Normalize to the shape
// the rest of the UI expects ({name, ref, label, ...}) so nothing here assumes a
// particular dataset. Any signal can be an x- or y-axis; there is no fixed kind.
function normalizeSignals(raw) {
  return (raw || []).map((s) => ({ name: s.name, ref: "@" + s.name, label: s.name, length: s.length,
    dtype: s.dtype, shape: s.shape || [s.length], ndim: s.ndim || 1 }));
}
function defaultXRef() { return state.signals.length ? state.signals[0].ref : null; }
// Choose an x-axis for a 1-D signal: a same-length 1-D signal, preferring one named "x".
// Returns null if none (the plot then runs against its own index).
function defaultXFor(signal) {
  if (!signal || signal.ndim > 1) return null;
  const cand = state.signals.filter((s) => s.ndim === 1 && s.length === signal.length);
  const pick = cand.find((s) => s.name === "x") || cand.find((s) => s.ref !== signal.ref) || null;
  return pick ? pick.ref : null;
}
// Build a sensible plot for a dragged signal: 2-D -> image, 1-D -> line.
function plotForSignal(signal, rect) {
  const id = nextPlotId();
  if (signal && signal.ndim > 1) {
    const p = _blankPlot(id, rect, null, null);
    p.type = "imshow"; p.data.field = signal.ref;
    return p;
  }
  const p = _blankPlot(id, rect, defaultXFor(signal), signal ? signal.ref : null);
  p.type = "line";
  return p;
}

/* ---------------- Load data: pick an existing dataset or upload a new one ---------------- */
function closeLoadMenu() { document.querySelectorAll(".load-menu").forEach((m) => m.remove()); }
async function loadDataMenu(x, y) {
  closeLoadMenu();
  const menu = document.createElement("div"); menu.className = "overlay-menu load-menu";
  menu.style.left = Math.min(x, window.innerWidth - 280) + "px";
  menu.style.top = Math.min(y, window.innerHeight - 320) + "px";
  document.body.appendChild(menu);
  let datasets = [];
  try { ({ datasets } = await get("/api/datasets")); } catch (e) {}
  if (!datasets.length) {
    const it = document.createElement("button"); it.className = "om-item"; it.disabled = true;
    it.innerHTML = "No datasets yet<span class=\"k\">upload one below</span>";
    menu.appendChild(it);
  }
  for (const d of datasets) {
    const b = document.createElement("button"); b.className = "om-item";
    b.innerHTML = `${d.name}<span class="k">${d.kind} · ${d.n_signals} signal${d.n_signals === 1 ? "" : "s"}</span>`;
    b.onclick = async (ev) => {
      ev.stopPropagation(); closeLoadMenu();
      pushHist();
      state.spec.dataset = d.name; state.dataset = d.name;
      const sr = await get(`/api/datasets/${d.name}/signals`);
      state.signals = normalizeSignals(sr.signals);
      buildSignals();
      await commit();
      toast(`Loaded ${d.name}`);
    };
    menu.appendChild(b);
  }
  const sep = document.createElement("div"); sep.className = "om-sep"; menu.appendChild(sep);
  const up = document.createElement("button"); up.className = "om-item";
  up.innerHTML = "Upload new file&hellip;<span class=\"k\">.npz / .pkl / .csv</span>";
  up.onclick = (ev) => { ev.stopPropagation(); closeLoadMenu(); uploadDataset(); };
  menu.appendChild(up);
  setTimeout(() => document.addEventListener("pointerdown", function outside(ev) { if (!menu.contains(ev.target)) { closeLoadMenu(); document.removeEventListener("pointerdown", outside); } }), 0);
}
function uploadDataset() {
  const inp = document.createElement("input"); inp.type = "file"; inp.accept = ".npz,.pkl,.pickle,.csv";
  inp.onchange = async () => {
    const f = inp.files[0]; if (!f) return;
    const fd = new FormData(); fd.append("file", f);
    const r = await fetch("/api/datasets", { method: "POST", body: fd });
    const d = await r.json();
    if (!r.ok) { toast("Upload failed: " + (d.detail || ""), true); return; }
    pushHist();
    state.spec.dataset = d.name; state.dataset = d.name; state.signals = normalizeSignals(d.signals);
    buildSignals();
    await commit();
    toast(`Loaded ${d.name} (${d.n_signals} signals)`);
  };
  inp.click();
}
function buildSignals() {
  const list = $("#signalList"); list.innerHTML = "";
  state.signalSel.clear();  // refs from a previous dataset are invalid for the new signal list
  for (const s of state.signals) {
    const el = document.createElement("div"); el.className = "signal" + (state.signalSel.has(s.ref) ? " sel" : "");
    const cb = document.createElement("input"); cb.type = "checkbox"; cb.className = "sig-check"; cb.checked = state.signalSel.has(s.ref);
    cb.addEventListener("pointerdown", (e) => e.stopPropagation());
    cb.addEventListener("click", (e) => { e.stopPropagation(); if (cb.checked) state.signalSel.add(s.ref); else state.signalSel.delete(s.ref); el.classList.toggle("sel", cb.checked); updateAddSel(); });
    const chip = document.createElement("span"); chip.className = "chip"; chip.textContent = s.ndim > 1 ? "▦" : "∿"; chip.title = s.shape.join("×") + " · " + s.dtype;
    const lab = document.createElement("span"); lab.className = "slabel"; lab.textContent = s.label;
    el.append(cb, chip, lab);
    el.addEventListener("pointerdown", (e) => { if (e.target === cb) return; startSignalDrag(e, s); });
    list.appendChild(el);
  }
  if (!state.signals.length) {
    const n = document.createElement("div"); n.className = "hint";
    n.innerHTML = "No signals yet. Click <b>Load data&hellip;</b> to pick a dataset.";
    list.appendChild(n);
  }
  const bar = document.createElement("div"); bar.className = "sig-bar";
  const btn = document.createElement("button"); btn.className = "pill"; btn.id = "addSel";
  btn.onclick = addSelectedSignals;
  bar.appendChild(btn); list.appendChild(bar);
  updateAddSel();
}
function updateAddSel() { const b = $("#addSel"); if (!b) return; b.textContent = state.signalSel.size ? `Add ${state.signalSel.size} selected` : "Add selected"; b.disabled = !state.signalSel.size; }
function addPlotAtCenter(signal) {
  state.spec.plots.push(plotForSignal(signal, [0.35, 0.35, 0.3, 0.3]));
}
function addSelectedSignals() {
  if (!state.signalSel.size) return;
  pushHist();
  [...state.signalSel].forEach((ref) => addPlotAtCenter(state.signals.find((s) => s.ref === ref)));
  arrangeGrid(Math.ceil(Math.sqrt(state.spec.plots.length)));
  state.signalSel.clear();
  commit();
  buildSignals();
}
function startSignalDrag(e, s) {
  e.preventDefault();
  const ghost = document.createElement("div"); ghost.className = "ghost"; ghost.textContent = s.label;
  document.body.appendChild(ghost);
  const mv = (ev) => { ghost.style.left = ev.clientX + "px"; ghost.style.top = ev.clientY + "px"; };
  mv(e);
  const up = (ev) => {
    document.removeEventListener("pointermove", mv); ghost.remove();
    const r = figImg.getBoundingClientRect();
    if (ev.clientX >= r.left && ev.clientX <= r.right && ev.clientY >= r.top && ev.clientY <= r.bottom) {
      const fx = (ev.clientX - r.left) / r.width, fyB = 1 - (ev.clientY - r.top) / r.height;
      const target = plotAt(fx, fyB);
      if (target) showOverlayMenu(target.id, ev.clientX, ev.clientY, s);
      else addPlotAt(fx, fyB, s);
    }
  };
  document.addEventListener("pointermove", mv);
  document.addEventListener("pointerup", up, { once: true });
}

function plotAt(fx, fyB) {
  for (const p of state.spec.plots) { const [l, b, w, h] = p.rect; if (fx >= l && fx <= l + w && fyB >= b && fyB <= b + h) return p; }
  return null;
}
function closeOverlayMenu() { document.querySelectorAll(".overlay-menu").forEach((m) => m.remove()); }
function showOverlayMenu(pid, clientX, clientY, signal) {
  closeOverlayMenu();
  const menu = document.createElement("div"); menu.className = "overlay-menu";
  menu.style.left = Math.min(clientX, window.innerWidth - 210) + "px";
  menu.style.top = Math.min(clientY, window.innerHeight - 190) + "px";
  const get = () => state.spec.plots.find((x) => x.id === pid);
  const opts = [
    ["＋ line · overlay", "add a 2nd series on top", () => { pushHist(); const p = get(); p.extra_lines = p.extra_lines || []; p.extra_lines.push({ ref: signal.ref, color: "#2f6690", lw: 1.5, marker: "", ms: 0, alpha: 1.0, label: "" }); select("plot:" + pid); commit(); }],
    ["± band · shaded", "fill y ± this signal", () => { pushHist(); get().data.band = signal.ref; select("plot:" + pid); commit(); }],
    ["± error bars", "whiskers from this signal", () => { pushHist(); const p = get(); p.data.errorbar = { ref: signal.ref, color: p.style.color, lw: 1.0 }; select("plot:" + pid); commit(); }],
    ["⟳ replace", "swap the primary series", () => { pushHist(); get().data.y = signal.ref; select("plot:" + pid); commit(); }],
  ];
  for (const [label, sub, fn] of opts) {
    const b = document.createElement("button"); b.className = "om-item";
    b.innerHTML = `${label}<span class="k">${sub}</span>`;
    b.onclick = (e) => { e.stopPropagation(); closeOverlayMenu(); fn(); };
    menu.appendChild(b);
  }
  document.body.appendChild(menu);
  setTimeout(() => document.addEventListener("pointerdown", function outside(e) { if (!menu.contains(e.target)) { closeOverlayMenu(); document.removeEventListener("pointerdown", outside); } }), 0);
}
function addPlotAt(fx, fyB, signal) {
  pushHist();
  const w = 0.3, h = 0.3;
  const rect = [r4(clamp(fx - w / 2, 0, 1 - w)), r4(clamp(fyB - h / 2, 0, 1 - h)), w, h];
  const p = plotForSignal(signal, rect);
  state.spec.plots.push(p);
  select("plot:" + p.id);
  commit();
}
function _blankPlot(pid, rect, xref, yref) {
  return {
    id: pid, rect, data: { x: xref, y: yref, z: null, field: null, band: null, errorbar: null }, type: "line", extra_lines: [],
    style: { color: "#b03a2e", lw: 1.6, marker: "o", ms: 2.4, alpha: 1.0, band_alpha: 0.13 },
    axes: { xticks: "auto", xticklabels: null, yticks: "auto", yticklabels: null, xlabel: "", ylabel: "", xlabel_visible: true, ylabel_visible: true, xlim: null, ylim: null, xscale: "linear", yscale: "linear", spines: { top: false, right: false, bottom: true, left: true }, tick_labelsize: 12, labelbottom: true, labelleft: true },
    title: { text: "", x: 0.0, y: 1.0, pad: 1.0, fontsize: 11, weight: "normal", color: "#000000", visible: false },
    artists: [],
  };
}

/* ---------------- layers ---------------- */
function buildLayers() {
  const list = $("#layersList"); list.innerHTML = "";
  state.spec.plots.forEach((p, i) => {
    const r = p.rect; const off = r[0] < -0.001 || r[1] < -0.001 || r[0] + r[2] > 1.001 || r[1] + r[3] > 1.001;
    addLayer(list, { icon: "▣", swatch: p.style.color, name: (off ? "⚠ " : "") + `Plot ${i + 1} · ${sigLabel(p.data.y)}`, id: "plot:" + p.id });
    if (p.title.visible) addLayer(list, { name: "Title", id: "title:" + p.id, sub: true });
    (p.extra_lines || []).forEach((el, j) => addLayer(list, { name: "line · " + sigLabel(el.ref), id: `line:${p.id}:${j}`, sub: true }));
    if (p.data.band) addLayer(list, { name: "band · " + sigLabel(p.data.band), id: "plot:" + p.id, sub: true });
    if (p.data.errorbar) addLayer(list, { name: "error bars", id: "plot:" + p.id, sub: true });
    p.artists.forEach((a, j) => addLayer(list, { name: a.type, id: `artist:${p.id}:${j}`, sub: true }));
  });
  if (state.spec.figure_artists.length) {
    addGroup(list, "Figure");
    state.spec.figure_artists.forEach((fa, j) => addLayer(list, { name: fa.type + (fa.text ? " · " + fa.text : ""), id: "figartist:" + j }));
  }
  if (!list.children.length) { const n = document.createElement("div"); n.className = "empty-note"; n.textContent = "Drag a signal onto the canvas to start."; list.appendChild(n); }
}
const sigLabel = (ref) => { const s = state.signals.find((x) => x.ref === ref); return s ? s.label : ref; };
function addGroup(list, name) { const g = document.createElement("div"); g.className = "layer-group"; g.textContent = name; list.appendChild(g); }
function addLayer(list, { icon, swatch, name, id, sub }) {
  const el = document.createElement("div"); el.className = "layer" + (sub ? " sub" : ""); el.dataset.id = id;
  const ico = document.createElement("span"); ico.className = "ico"; ico.textContent = icon || ""; el.appendChild(ico);
  if (swatch) { const s = document.createElement("span"); s.className = "swatch"; s.style.background = swatch; el.appendChild(s); }
  const nm = document.createElement("span"); nm.className = "lname"; nm.textContent = name; el.appendChild(nm);
  el.onclick = (e) => select(id, e.shiftKey);
  list.appendChild(el);
}

/* ---------------- inspector ---------------- */
function buildInspector() {
  const body = $("#inspectorBody");
  if (!body) return;
  body.innerHTML = "";
  const titleEl = $("#inspTitle");
  // clear a selection that points at a deleted element so we never read a missing plot
  let id = state.selected;
  if (id) {
    const [kind, pid] = id.split(":");
    const plotExists = state.spec.plots.some((p) => p.id === pid);
    if ((kind === "plot" || kind === "title" || kind === "artist" || kind === "line") && !plotExists) id = state.selected = null;
    if (kind === "figartist" && !state.spec.figure_artists[+pid]) id = state.selected = null;
  }
  if (!id) { if (titleEl) titleEl.textContent = "Canvas"; buildCanvasInspector(body); return; }
  const [kind, pid, j] = id.split(":");
  const pl = state.spec.plots.find((p) => p.id === pid);
  try {
    if (kind === "plot" && pl) { if (titleEl) titleEl.innerHTML = "Plot " + (state.spec.plots.findIndex((p) => p.id === pid) + 1); buildPlotInspector(body, pid); }
    else if (kind === "title" && pl) { if (titleEl) titleEl.innerHTML = "Title"; buildTitleInspector(body, pid); }
    else if (kind === "artist" && pl && pl.artists[j]) { if (titleEl) titleEl.innerHTML = "Artist · " + pl.artists[j].type; buildArtistInspector(body, pid, j); }
    else if (kind === "line" && pl && pl.extra_lines && pl.extra_lines[j]) { if (titleEl) titleEl.innerHTML = "Overlaid line"; buildLineInspector(body, pid, j); }
    else if (kind === "figartist" && state.spec.figure_artists[+pid]) { if (titleEl) titleEl.innerHTML = "Figure · " + state.spec.figure_artists[+pid].type; buildFigartistInspector(body, +pid); }
    else { if (titleEl) titleEl.textContent = "Canvas"; buildCanvasInspector(body); }
  } catch (err) {
    console.error("buildInspector:", err);
    if (titleEl) titleEl.textContent = "Canvas";
    body.innerHTML = "";
  }
}

function buildCanvasInspector(body) {
  const c = state.spec.canvas;
  body.appendChild(ctlHead("Canvas"));
  body.appendChild(slider("Width (in)", ["canvas", "width"], c.width, 3, 14, 0.05));
  body.appendChild(slider("Height (in)", ["canvas", "height"], c.height, 1, 12, 0.05));
  body.appendChild(slider("Export DPI", ["canvas", "dpi"], c.dpi, 100, 400, 10));
  body.appendChild(color("Background", ["canvas", "bg"], c.bg));
  body.appendChild(buildLayoutSection());
  body.appendChild(buildSyncAxesSection());
  body.appendChild(buildFigureElementsSection());
  body.appendChild(ctlHead(""));
  const hint = document.createElement("div"); hint.className = "empty-note";
  hint.innerHTML = "Select a plot to edit its data, axes, title, colors and artists. Drag signals from the left to add plots.";
  body.appendChild(hint);
}

function buildLayoutSection() {
  const wrap = document.createElement("div");
  const L = state.spec.layout || { margin: 0.05, gap: 0.04 };
  wrap.appendChild(ctlHead("Layout (all plots)"));
  const grow = document.createElement("div"); grow.className = "ctl";
  const ghead = document.createElement("div"); ghead.className = "ctl-head";
  const glab = document.createElement("label"); glab.textContent = "Arrange in grid";
  const gcols = document.createElement("input"); gcols.type = "number"; gcols.min = 1; gcols.max = 6;
  gcols.value = Math.ceil(Math.sqrt(state.spec.plots.length || 1)); gcols.style.cssText = "width:52px;padding:6px 8px;border:1px solid var(--line);border-radius:8px";
  ghead.append(glab, gcols); grow.appendChild(ghead);
  const gbtn = document.createElement("button"); gbtn.className = "pill"; gbtn.textContent = "Make grid"; gbtn.style.width = "100%";
  gbtn.onclick = () => { state.gridCols = parseInt(gcols.value, 10) || 3; layoutAll("grid", state.gridCols); };
  grow.appendChild(gbtn); wrap.appendChild(grow);
  wrap.appendChild(slider("Margin (fraction of canvas)", ["layout", "margin"], L.margin, 0, 0.2, 0.005, () => arrangeGrid(state.gridCols)));
  wrap.appendChild(slider("Gap between plots (fraction)", ["layout", "gap"], L.gap, 0, 0.2, 0.005, () => arrangeGrid(state.gridCols)));
  wrap.appendChild(ctlHead("Align"));
  const arow = document.createElement("div"); arow.className = "add-row";
  for (const [l, op, t] of [["L", "align:left", "Align left"], ["Cx", "align:cx", "Center horizontally"], ["R", "align:right", "Align right"], ["T", "align:top", "Align top"], ["Cy", "align:cy", "Center vertically"], ["B", "align:bottom", "Align bottom"]]) {
    const b = document.createElement("button"); b.className = "pill"; b.textContent = l; b.title = t; b.onclick = () => layoutAll(op); arow.appendChild(b);
  }
  wrap.appendChild(arow);
  wrap.appendChild(ctlHead("Distribute"));
  const drow = document.createElement("div"); drow.className = "add-row";
  for (const [l, op] of [["Horizontal", "distH"], ["Vertical", "distV"]]) {
    const b = document.createElement("button"); b.className = "pill"; b.textContent = l; b.onclick = () => layoutAll(op); drow.appendChild(b);
  }
  wrap.appendChild(drow);
  return wrap;
}

function nextPlotNum() { let mx = 0; for (const p of state.spec.plots) { const m = p.id.match(/^p(\d+)$/); if (m) mx = Math.max(mx, +m[1] + 1); } return mx; }
function nextPlotId() { return "p" + nextPlotNum(); }
function arrangeGrid(cols) {
  const plots = state.spec.plots; if (!plots.length) return;
  const L = state.spec.layout || { margin: 0.05, gap: 0.04 };
  const n = plots.length, rows = Math.ceil(n / cols), ml = L.margin, gap = L.gap;
  const cw = (1 - 2 * ml - (cols - 1) * gap) / cols, ch = (1 - 2 * ml - (rows - 1) * gap) / rows;
  const rects = plots.map((p) => p.rect);
  const cy = (i) => rects[i][1] + rects[i][3] / 2, cx = (i) => rects[i][0] + rects[i][2] / 2;
  const order = plots.map((p, i) => i).sort((a, b) => (cy(b) - cy(a)) || (cx(a) - cx(b))); // top-to-bottom, then left-to-right
  order.forEach((pi, idx) => {
    const rr = Math.floor(idx / cols), cc = idx % cols;
    plots[pi].rect = [r4(ml + cc * (cw + gap)), r4(ml + (rows - 1 - rr) * (ch + gap)), r4(cw), r4(ch)];
    plots[pi].axes.labelleft = (cc === 0);        // only the left column shows y tick labels
    plots[pi].axes.labelbottom = (rr === rows - 1); // only the bottom row shows x tick labels
  });
}

function buildSyncAxesSection() {
  const wrap = document.createElement("div");
  wrap.appendChild(ctlHead("Sync axis ranges (all plots)"));
  const mk = (axisLabel, key) => {
    const row = document.createElement("div"); row.className = "num-row";
    const lab = document.createElement("label"); lab.textContent = axisLabel; lab.style.cssText = "font-size:12px;color:var(--muted);flex:0 0 auto";
    const a = document.createElement("input"); a.type = "text"; a.placeholder = "min";
    const b = document.createElement("input"); b.type = "text"; b.placeholder = "max";
    const btn = document.createElement("button"); btn.className = "pill"; btn.textContent = "all";
    btn.onclick = () => {
      const lo = parseFloat(a.value), hi = parseFloat(b.value);
      if (isNaN(lo) || isNaN(hi)) { toast("Enter both min and max", true); return; }
      pushHist(); state.spec.plots.forEach((p) => { p.axes[key] = [lo, hi]; }); commit();
    };
    row.append(lab, a, b, btn); return row;
  };
  wrap.appendChild(mk("X", "xlim"));
  wrap.appendChild(mk("Y", "ylim"));
  wrap.appendChild(hint("Set one range and apply to every plot so the axes line up (e.g. X 0 to 16 centers 0 on all panels)."));
  return wrap;
}

function buildFigureElementsSection() {
  const wrap = document.createElement("div");
  wrap.appendChild(ctlHead("Figure elements"));
  const frow = document.createElement("div"); frow.className = "add-row";
  for (const [label, mk] of [
    ["+ X label", () => ({ type: "text", x: 0.5, y: 0.02, text: "x-axis", ha: "center", va: "bottom", fontsize: 14, color: "#000" })],
    ["+ Y label", () => ({ type: "text", x: 0.03, y: 0.5, text: "y-axis", ha: "center", va: "center", rotation: 90, fontsize: 14, color: "#000" })],
    ["+ Legend", () => ({ type: "legend", handles: [{ color: "#b03a2e", lw: 1.6, marker: "o", ms: 3 }], labels: ["series"], loc: [0.5, 1.0], ncol: 2, frameon: false, fontsize: 12 })],
    ["+ Suptitle", () => ({ type: "suptitle", text: "Title", x: 0.5, y: 1.0, fontsize: 16, color: "#000" })],
    ["+ Text", () => ({ type: "text", x: 0.5, y: 0.5, text: "note", ha: "center", va: "center", fontsize: 12, color: "#333" })],
  ]) {
    const b = document.createElement("button"); b.className = "pill"; b.textContent = label;
    b.onclick = () => { pushHist(); state.spec.figure_artists.push(mk()); select("figartist:" + (state.spec.figure_artists.length - 1)); commit(); };
    frow.appendChild(b);
  }
  wrap.appendChild(frow);
  return wrap;
}

function layoutAll(op, param) {
  const plots = state.spec.plots; if (!plots.length) return;
  const L = state.spec.layout || { margin: 0.05, gap: 0.04 };
  const rects = plots.map((p) => p.rect);
  pushHist();
  if (op === "grid") {
    arrangeGrid(param || 3);
  } else if (op.indexOf("align:") === 0) {
    const w = op.slice(6);
    if (w === "left") { const v = Math.min(...rects.map((r) => r[0])); plots.forEach((p) => p.rect[0] = r4(v)); }
    else if (w === "right") { const v = Math.max(...rects.map((r) => r[0] + r[2])); plots.forEach((p) => p.rect[0] = r4(v - p.rect[2])); }
    else if (w === "cx") { const v = (Math.min(...rects.map((r) => r[0])) + Math.max(...rects.map((r) => r[0] + r[2]))) / 2; plots.forEach((p) => p.rect[0] = r4(v - p.rect[2] / 2)); }
    else if (w === "top") { const v = Math.max(...rects.map((r) => r[1] + r[3])); plots.forEach((p) => p.rect[1] = r4(v - p.rect[3])); }
    else if (w === "bottom") { const v = Math.min(...rects.map((r) => r[1])); plots.forEach((p) => p.rect[1] = r4(v)); }
    else if (w === "cy") { const v = (Math.min(...rects.map((r) => r[1])) + Math.max(...rects.map((r) => r[1] + r[3]))) / 2; plots.forEach((p) => p.rect[1] = r4(v - p.rect[3] / 2)); }
  } else if (op === "distH") {
    const s = plots.map((p, i) => [i, p.rect[0] + p.rect[2] / 2]).sort((a, b) => a[1] - b[1]);
    const lo = Math.min(...rects.map((r) => r[0])), hi = Math.max(...rects.map((r) => r[0] + r[2]));
    const totalW = rects.reduce((a, r) => a + r[2], 0), gap = (hi - lo - totalW) / Math.max(1, plots.length - 1);
    let x = lo; s.forEach(([i]) => { plots[i].rect[0] = r4(x); x += plots[i].rect[2] + gap; });
  } else if (op === "distV") {
    const s = plots.map((p, i) => [i, p.rect[1] + p.rect[3] / 2]).sort((a, b) => a[1] - b[1]);
    const lo = Math.min(...rects.map((r) => r[1])), hi = Math.max(...rects.map((r) => r[1] + r[3]));
    const totalH = rects.reduce((a, r) => a + r[3], 0), gap = (hi - lo - totalH) / Math.max(1, plots.length - 1);
    let y = lo; s.forEach(([i]) => { plots[i].rect[1] = r4(y); y += plots[i].rect[3] + gap; });
  }
  commit();
}

const CMAPS = [["viridis", "Viridis"], ["plasma", "Plasma"], ["magma", "Magma"], ["inferno", "Inferno"],
  ["cividis", "Cividis"], ["coolwarm", "Cool warm"], ["RdYlBu", "Red-Yellow-Blue"], ["YlOrRd", "Yellow-Orange-Red"],
  ["Blues", "Blues"], ["Greys", "Greys"]];

function paletteBtn(pid) {
  const b = document.createElement("button"); b.className = "pill"; b.style.width = "100%";
  b.textContent = "Suggest accessible colors"; b.title = "Assign a colorblind-safe palette (Okabe-Ito) to this plot's series";
  b.onclick = async () => {
    const { palettes } = await get("/api/palettes");
    const pal = palettes["Okabe-Ito (colorblind-safe)"] || Object.values(palettes)[0];
    pushHist();
    const p = state.spec.plots.find((x) => x.id === pid);
    p.style.color = pal[0];
    (p.extra_lines || []).forEach((el, i) => { el.color = pal[(i + 1) % pal.length]; });
    commit();
    toast("Applied Okabe-Ito palette");
  };
  return b;
}

const PLOT_TYPES = {
  "line": {label:"Line", group:"Lines", data:["x","y"]},
  "step": {label:"Step", group:"Lines", data:["x","y"]},
  "scatter": {label:"Scatter", group:"Points", data:["x","y"]},
  "scatter-grouped": {label:"Scatter (grouped)", group:"Points", data:["x","y","z"]},
  "bar": {label:"Bar", group:"Bars", data:["x","y"]},
  "barh": {label:"Bar (horizontal)", group:"Bars", data:["x","y"]},
  "histogram": {label:"Histogram", group:"Distributions", data:["y"]},
  "boxplot": {label:"Box plot", group:"Distributions", data:["x","y"]},
  "violinplot": {label:"Violin plot", group:"Distributions", data:["x","y"]},
  "hist2d": {label:"2-D histogram", group:"Density", data:["x","y"]},
  "hexbin": {label:"Hexbin", group:"Density", data:["x","y"]},
  "imshow": {label:"Image (2-D)", group:"Fields", data:["field"]},
  "contourf": {label:"Contour (filled)", group:"Fields", data:["field"]},
  "contour": {label:"Contour (lines)", group:"Fields", data:["field"]},
  "pcolormesh": {label:"Pcolormesh", group:"Fields", data:["field"]},
  "quiver": {label:"Vector field", group:"Vector", data:["x","y"]},
  "streamplot": {label:"Streamplot", group:"Vector", data:["x","y"]},
  "decision-boundary": {label:"Decision boundary", group:"Composite", data:["field","x","y","z"]},
  "pie": {label:"Pie", group:"Other", data:["z"]},
  "polar": {label:"Polar", group:"Other", data:["x","y"]},
  "scatter-3d": {label:"3-D scatter", group:"3-D", data:["x","y","z"]},
  "surface": {label:"Surface (3-D)", group:"3-D", data:["field"]},
  "wireframe": {label:"Wireframe (3-D)", group:"3-D", data:["field"]},
};
const COLORMAP_TYPES = new Set(["imshow","contourf","contour","pcolormesh","hist2d","hexbin","scatter-grouped","decision-boundary"]);
const FIELD_LABELS = {
  x: {default:"X signal", quiver:"U field (2-D)", streamplot:"U field (2-D)", polar:"Theta", barh:"Values", boxplot:"Groups", violinplot:"Groups"},
  y: {default:"Y signal", histogram:"Values", polar:"Radius", barh:"Categories", boxplot:"Values", violinplot:"Values", quiver:"V field (2-D)", streamplot:"V field (2-D)"},
  z: {default:"Group / class", pie:"Values (slices)", "scatter-3d":"Z signal"},
  field: {default:"Matrix (2-D)", "decision-boundary":"Decision surface (2-D)", surface:"Height field (2-D)", wireframe:"Height field (2-D)"},
};
const fieldLabel = (name, t) => (FIELD_LABELS[name] && (FIELD_LABELS[name][t] || FIELD_LABELS[name].default)) || name;

function plotTypeSelect(pid) {
  const p = state.spec.plots[idx(pid)];
  const w = document.createElement("div"); w.className = "ctl";
  const head = document.createElement("div"); head.className = "ctl-head"; const lab = document.createElement("label"); lab.textContent = "Plot type"; head.appendChild(lab); w.appendChild(head);
  const sel = document.createElement("select"); sel.className = "ctl-sel";
  const groups = {};
  for (const [k, v] of Object.entries(PLOT_TYPES)) (groups[v.group] = groups[v.group] || []).push([k, v.label]);
  for (const gname of Object.keys(groups)) {
    const og = document.createElement("optgroup"); og.label = gname;
    for (const [k, lbl] of groups[gname]) { const o = document.createElement("option"); o.value = k; o.textContent = lbl; if (k === p.type) o.selected = true; og.appendChild(o); }
    sel.appendChild(og);
  }
  sel.addEventListener("change", () => { pushHist(); p.type = sel.value; commit(); });
  w.appendChild(sel); return w;
}

function buildPlotInspector(body, pid) {
  const p = state.spec.plots.find((x) => x.id === pid);
  body.appendChild(ctlHead("Data"));
  body.appendChild(plotTypeSelect(pid));
  const t = p.type;
  const fields = (PLOT_TYPES[t] && PLOT_TYPES[t].data) || ["x","y"];
  for (const f of fields) {
    const allowNone = (f === "z") || (f === "x" && ["line", "step", "scatter"].includes(t));
    body.appendChild(sigSelect(fieldLabel(f, t), ["plots", idx(pid), "data", f], p.data[f], allowNone));
  }
  if (t === "line" || t === "scatter") {
    body.appendChild(sigSelect("Band (± sigma)", ["plots", idx(pid), "data", "band"], p.data.band, true));
    body.appendChild(errorbarSelect(p));
    buildExtraLines(body, pid);
  }
  body.appendChild(ctlHead("Style"));
  body.appendChild(color("Color", ["plots", idx(pid), "style", "color"], p.style.color));
  body.appendChild(paletteBtn(pid));
  if (COLORMAP_TYPES.has(t)) {
    body.appendChild(selControl("Colormap", ["plots", idx(pid), "style", "cmap"], p.style.cmap || "viridis", CMAPS));
    body.appendChild(check("Show colorbar", ["plots", idx(pid), "style", "colorbar"], !!p.style.colorbar));
  }
  if (t === "line" || t === "step") body.appendChild(slider("Line width", ["plots", idx(pid), "style", "lw"], p.style.lw, 0.2, 5, 0.1));
  if (t === "scatter" || t === "scatter-grouped" || t === "decision-boundary") body.appendChild(slider("Marker size", ["plots", idx(pid), "style", "ms"], p.style.ms, 0, 8, 0.2));
  if (t === "bar" || t === "barh") body.appendChild(slider("Bar width", ["plots", idx(pid), "style", "width"], p.style.width ?? 0.6, 0.05, 1, 0.05));
  body.appendChild(slider("Opacity", ["plots", idx(pid), "style", "alpha"], p.style.alpha, 0, 1, 0.05));
  if (["histogram","hist2d","hexbin"].includes(t)) body.appendChild(slider("Bins", ["plots", idx(pid), "style", "bins"], p.style.bins ?? 20, 2, 100, 1));
  if (["scatter-3d","surface","wireframe"].includes(t)) {
    body.appendChild(slider("View elevation (deg)", ["plots", idx(pid), "style", "elev"], p.style.elev ?? 30, -90, 90, 1));
    body.appendChild(slider("View azimuth (deg)", ["plots", idx(pid), "style", "azim"], p.style.azim ?? -60, -180, 180, 1));
  }
  if (p.data.band) body.appendChild(slider("Band opacity", ["plots", idx(pid), "style", "band_alpha"], p.style.band_alpha, 0, 0.6, 0.01));
  body.appendChild(ctlHead("Axes"));
  body.appendChild(ticksText("X ticks (blank = auto)", ["plots", idx(pid), "axes", "xticks"], p.axes.xticks));
  body.appendChild(ticksText("Y ticks (blank = auto)", ["plots", idx(pid), "axes", "yticks"], p.axes.yticks));
  body.appendChild(text("X label", ["plots", idx(pid), "axes", "xlabel"], p.axes.xlabel));
  body.appendChild(check("Show X label", ["plots", idx(pid), "axes", "xlabel_visible"], p.axes.xlabel_visible));
  body.appendChild(text("Y label", ["plots", idx(pid), "axes", "ylabel"], p.axes.ylabel));
  body.appendChild(check("Show Y label", ["plots", idx(pid), "axes", "ylabel_visible"], p.axes.ylabel_visible));
  body.appendChild(hint("Labels and titles support LaTeX mathtext, e.g. <code>$x_0$</code>."));
  body.appendChild(num2("X range (blank = auto)", ["plots", idx(pid), "axes", "xlim"], p.axes.xlim));
  body.appendChild(num2("Y range (blank = auto)", ["plots", idx(pid), "axes", "ylim"], p.axes.ylim));
  body.appendChild(slider("Tick label size", ["plots", idx(pid), "axes", "tick_labelsize"], p.axes.tick_labelsize, 6, 24, 0.5));
  body.appendChild(ctlHead("Spines"));
  const sp = p.axes.spines;
  const row = document.createElement("div"); row.className = "two";
  row.append(checkInline("top", ["plots", idx(pid), "axes", "spines", "top"], sp.top));
  row.append(checkInline("right", ["plots", idx(pid), "axes", "spines", "right"], sp.right));
  row.append(checkInline("bottom", ["plots", idx(pid), "axes", "spines", "bottom"], sp.bottom));
  row.append(checkInline("left", ["plots", idx(pid), "axes", "spines", "left"], sp.left));
  body.appendChild(row);
  body.appendChild(check("Show x tick labels", ["plots", idx(pid), "axes", "labelbottom"], p.axes.labelbottom));
  body.appendChild(check("Show y tick labels", ["plots", idx(pid), "axes", "labelleft"], p.axes.labelleft));
  body.appendChild(ctlHead("Title"));
  body.appendChild(check("Show title", ["plots", idx(pid), "title", "visible"], p.title.visible));
  body.appendChild(text("Title text", ["plots", idx(pid), "title", "text"], p.title.text));
  body.appendChild(slider("Title size", ["plots", idx(pid), "title", "fontsize"], p.title.fontsize, 6, 24, 0.5));
  body.appendChild(selControl("Title weight", ["plots", idx(pid), "title", "weight"], p.title.weight, [["normal", "Normal"], ["bold", "Bold"]]));
  body.appendChild(ctlHead("Artists"));
  buildArtistList(body, pid);
  body.appendChild(ctlHead(""));
  const del = document.createElement("button"); del.className = "pill danger"; del.textContent = "Delete plot";
  del.onclick = () => { pushHist(); state.spec.plots = state.spec.plots.filter((x) => x.id !== pid); select(null); commit(); };
  const wrap = document.createElement("div"); wrap.appendChild(del); body.appendChild(wrap);
}
const idx = (pid) => state.spec.plots.findIndex((p) => p.id === pid);

function buildTitleInspector(body, pid) {
  const t = state.spec.plots.find((x) => x.id === pid).title;
  body.appendChild(text("Title text", ["plots", idx(pid), "title", "text"], t.text));
  body.appendChild(slider("X (of panel)", ["plots", idx(pid), "title", "x"], t.x, -0.6, 1.6, 0.02));
  body.appendChild(slider("Y (of panel)", ["plots", idx(pid), "title", "y"], t.y, -0.6, 1.6, 0.02));
  body.appendChild(slider("Size (pt)", ["plots", idx(pid), "title", "fontsize"], t.fontsize, 6, 24, 0.5));
  body.appendChild(selControl("Weight", ["plots", idx(pid), "title", "weight"], t.weight, [["normal", "Normal"], ["bold", "Bold"]]));
  body.appendChild(color("Color", ["plots", idx(pid), "title", "color"], t.color));
  body.appendChild(hint("Drag the title on the canvas to reposition it freely."));
}

function buildArtistInspector(body, pid, j) {
  const a = state.spec.plots.find((x) => x.id === pid).artists[j];
  const P = (k, v, mn, mx, st) => ["plots", idx(pid), "artists", j, k];
  if (a.type === "vline") { body.appendChild(slider("X value", P("x", a.x), 0, 20, 0.5)); body.appendChild(color("Color", P("color"), a.color)); body.appendChild(selControl("Style", P("ls"), a.ls || "-", [["-", "Solid"], ["dotted", "Dotted"], ["dashed", "Dashed"], ["dashdot", "Dash dot"]])); body.appendChild(slider("Width", P("lw"), a.lw ?? 1, 0.2, 4, 0.1)); body.appendChild(slider("Opacity", P("alpha"), a.alpha ?? 1, 0, 1, 0.05)); }
  else if (a.type === "hline") { body.appendChild(slider("Y value", P("y", a.y), -1, 1, 0.05)); body.appendChild(color("Color", P("color"), a.color)); body.appendChild(selControl("Style", P("ls"), a.ls || "-", [["-", "Solid"], ["dotted", "Dotted"], ["dashed", "Dashed"]])); body.appendChild(slider("Width", P("lw"), a.lw ?? 1, 0.2, 4, 0.1)); }
  else if (a.type === "text") { body.appendChild(text("Text", P("text"), a.text)); body.appendChild(slider("X", P("x"), a.x, -1, 2, 0.02)); body.appendChild(slider("Y", P("y"), a.y, -1, 2, 0.02)); body.appendChild(check("Use panel fraction", P("axes_frac"), !!a.axes_frac)); body.appendChild(slider("Size", P("fontsize"), a.fontsize ?? 10, 6, 24, 0.5)); body.appendChild(color("Color", P("color"), a.color || "#000")); }
  else if (a.type === "annotation") {
    body.appendChild(text("Text", P("text"), a.text));
    body.appendChild(num2("Arrow points to (data x, y)", P("xy"), a.xy));
    body.appendChild(num2("Label at (panel x, y)", P("xytext"), a.xytext));
    body.appendChild(slider("Label size", P("fontsize"), a.fontsize ?? 10, 6, 20, 0.5));
    body.appendChild(slider("Arrow width", P("lw"), a.lw ?? 1.2, 0.3, 4, 0.1));
    body.appendChild(color("Color", P("color"), a.color || "#000"));
  }
  else if (a.type === "box") {
    body.appendChild(slider("X (0-1 of panel)", P("x0"), a.x0, 0, 1, 0.01));
    body.appendChild(slider("Y (0-1 of panel)", P("y0"), a.y0, 0, 1, 0.01));
    body.appendChild(slider("Width", P("w"), a.w, 0.02, 1, 0.01));
    body.appendChild(slider("Height", P("h"), a.h, 0.02, 1, 0.01));
    body.appendChild(color("Fill", P("fc"), a.fc || "#ffd166"));
    body.appendChild(slider("Fill opacity (0 = none)", P("alpha"), a.alpha ?? 0.15, 0, 1, 0.02));
    body.appendChild(color("Edge", P("ec"), a.ec || "#000"));
    body.appendChild(slider("Edge width", P("lw"), a.lw ?? 1, 0, 4, 0.1));
  }
  else if (a.type === "vspan") {
    body.appendChild(slider("X start (data)", P("x0"), a.x0, 0, 20, 0.5));
    body.appendChild(slider("X end (data)", P("x1"), a.x1, 0, 20, 0.5));
    body.appendChild(color("Color", P("color"), a.color || "#b03a2e"));
    body.appendChild(slider("Opacity", P("alpha"), a.alpha ?? 0.12, 0, 0.6, 0.01));
  }
  else if (a.type === "circle") {
    body.appendChild(slider("Center X (data)", P("x"), a.x, -10, 20, 0.2));
    body.appendChild(slider("Center Y (data)", P("y"), a.y, -10, 10, 0.2));
    body.appendChild(slider("Radius", P("r"), a.r ?? 1, 0.05, 10, 0.1));
    body.appendChild(check("Filled", P("fill"), !!a.fill));
    body.appendChild(color("Color", P("color"), a.color || "#000"));
    body.appendChild(slider("Width", P("lw"), a.lw ?? 1, 0.2, 4, 0.1));
  }
  else if (a.type === "ellipse") {
    body.appendChild(slider("Center X (data)", P("x"), a.x, -10, 20, 0.2));
    body.appendChild(slider("Center Y (data)", P("y"), a.y, -10, 10, 0.2));
    body.appendChild(slider("Width", P("w"), a.w ?? 2, 0.1, 10, 0.1));
    body.appendChild(slider("Height", P("h"), a.h ?? 1, 0.1, 10, 0.1));
    body.appendChild(slider("Angle (deg)", P("angle"), a.angle ?? 0, -180, 180, 5));
    body.appendChild(check("Filled", P("fill"), !!a.fill));
    body.appendChild(color("Color", P("color"), a.color || "#000"));
    body.appendChild(slider("Line width", P("lw"), a.lw ?? 1, 0.2, 4, 0.1));
  }
  else if (a.type === "polygon") {
    body.appendChild(check("Filled", P("fill"), !!a.fill));
    body.appendChild(color("Color", P("color"), a.color || "#000"));
    body.appendChild(slider("Width", P("lw"), a.lw ?? 1, 0.2, 4, 0.1));
    body.appendChild(hint("Default 3-point triangle; edit points in the spec for custom shapes."));
  }
  else if (a.type === "arrow") {
    body.appendChild(slider("From X", P("x0"), a.x0, -10, 20, 0.2));
    body.appendChild(slider("From Y", P("y0"), a.y0, -10, 10, 0.2));
    body.appendChild(slider("To X", P("x1"), a.x1, -10, 20, 0.2));
    body.appendChild(slider("To Y", P("y1"), a.y1, -10, 10, 0.2));
    body.appendChild(color("Color", P("color"), a.color || "#000"));
    body.appendChild(slider("Width", P("lw"), a.lw ?? 1.5, 0.3, 4, 0.1));
  }
  body.appendChild(ctlHead(""));
  const del = document.createElement("button"); del.className = "pill danger"; del.textContent = "Remove artist";
  del.onclick = () => { pushHist(); const pl = state.spec.plots.find((x) => x.id === pid); pl.artists.splice(j, 1); select("plot:" + pid); commit(); };
  const w = document.createElement("div"); w.appendChild(del); body.appendChild(w);
}

function buildFigartistInspector(body, j) {
  const fa = state.spec.figure_artists[j];
  const P = (...k) => ["figure_artists", j, ...k];
  if (fa.type === "text") {
    body.appendChild(text("Text", P("text"), fa.text));
    body.appendChild(slider("X", P("x"), fa.x, 0, 1, 0.01));
    body.appendChild(slider("Y", P("y"), fa.y, 0, 1, 0.01));
    body.appendChild(slider("Size (pt)", P("fontsize"), fa.fontsize ?? 12, 6, 26, 0.5));
    body.appendChild(color("Color", P("color"), fa.color || "#000"));
  } else if (fa.type === "legend") {
    body.appendChild(slider("Size (pt)", P("fontsize"), fa.fontsize ?? 10, 6, 20, 0.5));
    body.appendChild(slider("Columns", P("ncol"), fa.ncol ?? 1, 1, 4, 1));
    body.appendChild(slider("Position X", P("loc", 0), fa.loc ? fa.loc[0] : 0.5, 0, 1.2, 0.01));
    body.appendChild(slider("Position Y", P("loc", 1), fa.loc ? fa.loc[1] : 1.0, -0.2, 1.2, 0.01));
    body.appendChild(ctlHead("Entries"));
    const elist = document.createElement("div"); elist.className = "art-list";
    (fa.labels || []).forEach((lb, i) => {
      const row = document.createElement("div"); row.className = "num-row";
      const tin = document.createElement("input"); tin.type = "text"; tin.value = lb; tin.style.flex = "2";
      tin.addEventListener("input", () => { if (state.sliderSnapshot === null) state.sliderSnapshot = clone(state.spec); fa.labels[i] = tin.value; scheduleDraft(); });
      tin.addEventListener("change", () => { if (state.sliderSnapshot !== null) { state.history.push(state.sliderSnapshot); state.future = []; state.sliderSnapshot = null; updateHist(); } commit(); });
      const mk = document.createElement("select"); mk.className = "ctl-sel"; mk.style.cssText = "width:auto;flex:0 0 auto;padding:4px 6px";
      for (const [v, l] of [["", "— line"], ["o", "o dot"], ["^", "^ tri"], ["s", "s sq"], ["D", "D dia"]]) { const o = document.createElement("option"); o.value = v; o.textContent = l; mk.appendChild(o); }
      mk.value = (fa.handles[i] && fa.handles[i].marker) || "";
      mk.addEventListener("change", () => { pushHist(); fa.handles = fa.handles || []; fa.handles[i] = fa.handles[i] || {}; fa.handles[i].marker = mk.value; fa.handles[i].ms = mk.value ? 3 : 0; commit(); });
      const ls = document.createElement("select"); ls.className = "ctl-sel"; ls.style.cssText = "width:auto;flex:0 0 auto;padding:4px 6px";
      for (const [v, l] of [["-", "solid"], ["dotted", "dotted"], ["dashed", "dashed"]]) { const o = document.createElement("option"); o.value = v; o.textContent = l; ls.appendChild(o); }
      ls.value = (fa.handles[i] && fa.handles[i].ls) || "-";
      ls.addEventListener("change", () => { pushHist(); fa.handles = fa.handles || []; fa.handles[i] = fa.handles[i] || {}; fa.handles[i].ls = ls.value; commit(); });
      const rm = document.createElement("span"); rm.textContent = "×"; rm.style.cssText = "cursor:pointer;color:var(--faint);padding:0 6px;font-size:15px";
      rm.onclick = () => { pushHist(); fa.labels.splice(i, 1); (fa.handles || []).splice(i, 1); select("figartist:" + j); commit(); };
      row.append(tin, mk, ls, rm); elist.appendChild(row);
    });
    body.appendChild(elist);
    const addE = document.createElement("button"); addE.className = "pill"; addE.textContent = "+ Entry";
    addE.onclick = () => { pushHist(); fa.labels = fa.labels || []; fa.handles = fa.handles || []; fa.labels.push("entry"); fa.handles.push({ color: "#b03a2e", lw: 1.5 }); select("figartist:" + j); commit(); };
    body.appendChild(addE);
  } else if (fa.type === "suptitle") {
    body.appendChild(text("Text", P("text"), fa.text));
    body.appendChild(slider("Size (pt)", P("fontsize"), fa.fontsize ?? 14, 6, 26, 0.5));
    body.appendChild(color("Color", P("color"), fa.color || "#000"));
  }
  body.appendChild(ctlHead(""));
  const del = document.createElement("button"); del.className = "pill danger"; del.textContent = "Remove";
  del.onclick = () => { pushHist(); state.spec.figure_artists.splice(j, 1); select(null); commit(); };
  const w = document.createElement("div"); w.appendChild(del); body.appendChild(w);
}

function errorbarSelect(p) {
  const w = document.createElement("div"); w.className = "ctl"; w.appendChild(ctlHead("Error bars (±)"));
  const sel = document.createElement("select"); sel.className = "ctl-sel";
  const none = document.createElement("option"); none.value = ""; none.textContent = "(none)"; sel.appendChild(none);
  for (const s of state.signals) { const o = document.createElement("option"); o.value = s.ref; o.textContent = s.label; sel.appendChild(o); }
  sel.value = p.data.errorbar ? p.data.errorbar.ref : "";
  sel.addEventListener("change", () => { pushHist(); p.data.errorbar = sel.value ? { ref: sel.value, color: p.style.color, lw: 1.0 } : null; commit(); });
  w.appendChild(sel); return w;
}

function buildExtraLines(body, pid) {
  const p = state.spec.plots.find((x) => x.id === pid);
  body.appendChild(ctlHead("Overlaid lines"));
  const list = document.createElement("div"); list.className = "art-list";
  (p.extra_lines || []).forEach((el, j) => {
    const it = document.createElement("div"); it.className = "art-item"; it.dataset.id = `line:${pid}:${j}`;
    it.innerHTML = `<span><span class="swatch" style="background:${el.color};display:inline-block;width:9px;height:9px;border-radius:2px;margin-right:5px"></span>${sigLabel(el.ref)}</span><span class="rm" data-rm="${j}">&times;</span>`;
    it.onclick = (e) => { if (e.target.dataset.rm !== undefined) return; select(`line:${pid}:${j}`); };
    it.querySelector(".rm").onclick = (e) => { e.stopPropagation(); pushHist(); p.extra_lines.splice(j, 1); select("plot:" + pid); commit(); };
    list.appendChild(it);
  });
  if (!(p.extra_lines || []).length) { const n = document.createElement("div"); n.className = "hint"; n.textContent = "None. Drag a signal onto this plot to overlay one."; list.appendChild(n); }
  body.appendChild(list);
  const add = document.createElement("button"); add.className = "pill"; add.textContent = "+ Add line";
  add.onclick = () => { pushHist(); p.extra_lines.push({ ref: defaultXRef(), color: "#2f6690", lw: 1.5, marker: "", ms: 0, alpha: 1.0, label: "" }); select("plot:" + pid); commit(); };
  body.appendChild(add);
}

function buildLineInspector(body, pid, j) {
  const el = state.spec.plots.find((x) => x.id === pid).extra_lines[j];
  const P = (k) => ["plots", idx(pid), "extra_lines", j, k];
  body.appendChild(ctlHead("Overlaid line"));
  body.appendChild(sigSelect("Signal", P("ref"), el.ref));
  body.appendChild(color("Color", P("color"), el.color));
  body.appendChild(slider("Width", P("lw"), el.lw ?? 1.5, 0.2, 5, 0.1));
  body.appendChild(slider("Opacity", P("alpha"), el.alpha ?? 1, 0, 1, 0.05));
  body.appendChild(ctlHead(""));
  const del = document.createElement("button"); del.className = "pill danger"; del.textContent = "Remove line";
  del.onclick = () => { pushHist(); state.spec.plots.find((x) => x.id === pid).extra_lines.splice(j, 1); select("plot:" + pid); commit(); };
  const w = document.createElement("div"); w.appendChild(del); body.appendChild(w);
}

function buildArtistList(body, pid) {
  const p = state.spec.plots.find((x) => x.id === pid);
  const list = document.createElement("div"); list.className = "art-list";
  p.artists.forEach((a, j) => {
    const it = document.createElement("div"); it.className = "art-item"; it.dataset.id = `artist:${pid}:${j}`;
    it.innerHTML = `<span>${a.type}${a.text ? " · " + a.text : ""}</span><span class="rm" data-rm="${j}">&times;</span>`;
    it.onclick = (e) => { if (e.target.dataset.rm !== undefined) return; select(`artist:${pid}:${j}`); };
    it.querySelector(".rm").onclick = (e) => { e.stopPropagation(); pushHist(); p.artists.splice(j, 1); select("plot:" + pid); commit(); };
    list.appendChild(it);
  });
  if (!p.artists.length) { const n = document.createElement("div"); n.className = "hint"; n.textContent = "No artists yet."; list.appendChild(n); }
  body.appendChild(list);
  const add = document.createElement("div"); add.className = "add-row";
  for (const [label, mk] of [["+ vline", () => ({ type: "vline", x: 4, color: "#c0392b", ls: "dotted", lw: 0.8, alpha: 0.6, zorder: 0 })], ["+ hline", () => ({ type: "hline", y: 0, color: "#bbbbbb", lw: 0.6, zorder: 0 })], ["+ text", () => ({ type: "text", x: 0.5, y: 0.5, text: "label", ha: "center", va: "center", fontsize: 10, color: "#555", axes_frac: true })], ["+ annotate", () => ({ type: "annotation", text: "note", xy: [8, 0.3], xytext: [0.3, 0.9], color: "#555555", fontsize: 10, lw: 1.2 })], ["+ arrow", () => ({ type: "arrow", x0: 2, y0: 0, x1: 6, y1: 0, color: "#333333", lw: 1.5 })], ["+ box", () => ({ type: "box", x0: 0.1, y0: 0.2, w: 0.4, h: 0.5, fc: "#ffd166", alpha: 0.2, ec: "#b03a2e", lw: 1.0 })], ["+ circle", () => ({ type: "circle", x: 5, y: 0, r: 1, fill: false, color: "#c0392b", lw: 1.2, alpha: 1, zorder: 3 })], ["+ ellipse", () => ({ type: "ellipse", x: 5, y: 0, w: 2, h: 1, angle: 0, fill: false, color: "#2f6690", lw: 1.2, alpha: 1, zorder: 3 })], ["+ polygon", () => ({ type: "polygon", points: [[4, 0], [6, 0], [5, 1]], fill: false, color: "#009E73", lw: 1.2, alpha: 1, zorder: 3 })], ["+ span", () => ({ type: "vspan", x0: 1, x1: 4, color: "#b03a2e", alpha: 0.12, zorder: 0 })], ["+ legend", () => ({ type: "legend", handles: [{ color: p.style.color, lw: 1.6 }], labels: ["series"], loc: "best", ncol: 1, frameon: true, fontsize: 9 })]]) {
    const b = document.createElement("button"); b.className = "pill"; b.textContent = label;
    b.onclick = () => { pushHist(); p.artists.push(mk()); select(`artist:${pid}:${p.artists.length - 1}`); commit(); };
    add.appendChild(b);
  }
  body.appendChild(add);
}

/* ---------------- control builders ---------------- */
function ctlHead(t) { const h = document.createElement("div"); h.className = "section"; h.textContent = t; return h; }
function hint(t) { const h = document.createElement("div"); h.className = "hint"; h.innerHTML = t; return h; }
function slider(label, path, val, min, max, step, afterChange) {
  const w = document.createElement("div"); w.className = "ctl";
  const head = document.createElement("div"); head.className = "ctl-head";
  const lab = document.createElement("label"); lab.textContent = label; const vv = document.createElement("span"); vv.className = "val"; vv.textContent = fmt(val, step);
  head.append(lab, vv); w.appendChild(head);
  const inp = document.createElement("input"); inp.type = "range"; inp.min = min; inp.max = max; inp.step = step; inp.value = val;
  inp.addEventListener("input", () => { if (state.sliderSnapshot === null) state.sliderSnapshot = clone(state.spec); setPath(state.spec, path, parseFloat(inp.value)); vv.textContent = fmt(parseFloat(inp.value), step); scheduleDraft(); });
  inp.addEventListener("change", () => { if (state.sliderSnapshot !== null) { state.history.push(state.sliderSnapshot); state.future = []; state.sliderSnapshot = null; updateHist(); } if (afterChange) afterChange(); commit(); });
  w.appendChild(inp); return w;
}
function text(label, path, val) {
  const w = document.createElement("div"); w.className = "ctl"; const head = document.createElement("div"); head.className = "ctl-head"; const lab = document.createElement("label"); lab.textContent = label; head.appendChild(lab); w.appendChild(head);
  const inp = document.createElement("input"); inp.type = "text"; inp.value = val == null ? "" : val;
  inp.addEventListener("input", () => { if (state.sliderSnapshot === null) state.sliderSnapshot = clone(state.spec); setPath(state.spec, path, inp.value); scheduleDraft(); });
  inp.addEventListener("change", () => { if (state.sliderSnapshot !== null) { state.history.push(state.sliderSnapshot); state.future = []; state.sliderSnapshot = null; updateHist(); } commit(); });
  w.appendChild(inp); return w;
}
function ticksText(label, path, val) {
  const w = document.createElement("div"); w.className = "ctl"; const head = document.createElement("div"); head.className = "ctl-head"; const lab = document.createElement("label"); lab.textContent = label; head.appendChild(lab); w.appendChild(head);
  const inp = document.createElement("input"); inp.type = "text"; inp.value = Array.isArray(val) ? val.join(", ") : "";
  const parse = () => { const t = inp.value.trim(); return t ? t.split(",").map((s) => parseFloat(s.trim())).filter((n) => !isNaN(n)) : "auto"; };
  inp.addEventListener("input", () => { if (state.sliderSnapshot === null) state.sliderSnapshot = clone(state.spec); setPath(state.spec, path, parse()); scheduleDraft(); });
  inp.addEventListener("change", () => { if (state.sliderSnapshot !== null) { state.history.push(state.sliderSnapshot); state.future = []; state.sliderSnapshot = null; updateHist(); } commit(); });
  w.appendChild(inp); return w;
}
function num2(label, path, val) {
  const w = document.createElement("div"); w.className = "ctl"; const head = document.createElement("div"); head.className = "ctl-head"; const lab = document.createElement("label"); lab.textContent = label; head.appendChild(lab); w.appendChild(head);
  const row = document.createElement("div"); row.className = "num-row";
  const a = document.createElement("input"), b = document.createElement("input"), sep = document.createElement("span"); sep.className = "sep"; sep.textContent = "–"; a.type = b.type = "text";
  a.value = val ? val[0] : ""; b.value = val ? val[1] : "";
  const parse = () => { const x = parseFloat(a.value), y = parseFloat(b.value); return (a.value && b.value && !isNaN(x) && !isNaN(y)) ? [x, y] : null; };
  a.addEventListener("input", () => { if (state.sliderSnapshot === null) state.sliderSnapshot = clone(state.spec); setPath(state.spec, path, parse()); scheduleDraft(); }); b.addEventListener("input", () => { if (state.sliderSnapshot === null) state.sliderSnapshot = clone(state.spec); setPath(state.spec, path, parse()); scheduleDraft(); });
  a.addEventListener("change", () => { if (state.sliderSnapshot !== null) { state.history.push(state.sliderSnapshot); state.future = []; state.sliderSnapshot = null; updateHist(); } commit(); }); b.addEventListener("change", () => { if (state.sliderSnapshot !== null) { state.history.push(state.sliderSnapshot); state.future = []; state.sliderSnapshot = null; updateHist(); } commit(); });
  row.append(a, sep, b); w.appendChild(row); return w;
}
function color(label, path, val) {
  const w = document.createElement("div"); w.className = "ctl"; const head = document.createElement("div"); head.className = "ctl-head"; const lab = document.createElement("label"); lab.textContent = label; head.appendChild(lab); w.appendChild(head);
  const row = document.createElement("div"); row.className = "color-row";
  const inp = document.createElement("input"); inp.type = "color"; inp.value = val || "#000000";
  const hex = document.createElement("span"); hex.className = "color-hex"; hex.textContent = val || "#000000";
  inp.addEventListener("input", () => { if (state.sliderSnapshot === null) state.sliderSnapshot = clone(state.spec); setPath(state.spec, path, inp.value); hex.textContent = inp.value; scheduleDraft(); });
  inp.addEventListener("change", () => { if (state.sliderSnapshot !== null) { state.history.push(state.sliderSnapshot); state.future = []; state.sliderSnapshot = null; updateHist(); } commit(); });
  row.append(inp, hex); w.appendChild(row); return w;
}
function check(label, path, val) {
  const w = document.createElement("div"); w.className = "ctl"; const row = document.createElement("label"); row.className = "check-row";
  const inp = document.createElement("input"); inp.type = "checkbox"; inp.checked = !!val; const span = document.createElement("span"); span.textContent = label;
  inp.addEventListener("change", () => { pushHist(); setPath(state.spec, path, inp.checked); commit(); });
  row.append(inp, span); w.appendChild(row); return w;
}
function checkInline(label, path, val) {
  const row = document.createElement("label"); row.className = "check-row";
  const inp = document.createElement("input"); inp.type = "checkbox"; inp.checked = !!val; const span = document.createElement("span"); span.textContent = label;
  inp.addEventListener("change", () => { pushHist(); setPath(state.spec, path, inp.checked); commit(); });
  row.append(inp, span); return row;
}
function selControl(label, path, val, options) {
  const w = document.createElement("div"); w.className = "ctl"; const head = document.createElement("div"); head.className = "ctl-head"; const lab = document.createElement("label"); lab.textContent = label; head.appendChild(lab); w.appendChild(head);
  const sel = document.createElement("select"); sel.className = "ctl-sel";
  for (const [v, l] of options) { const o = document.createElement("option"); o.value = v; o.textContent = l; if (v === val) o.selected = true; sel.appendChild(o); }
  sel.addEventListener("change", () => { pushHist(); setPath(state.spec, path, sel.value); commit(); });
  w.appendChild(sel); return w;
}
function sigSelect(label, path, val, allowNone) {
  const w = document.createElement("div"); w.className = "ctl"; const head = document.createElement("div"); head.className = "ctl-head"; const lab = document.createElement("label"); lab.textContent = label; head.appendChild(lab); w.appendChild(head);
  const sel = document.createElement("select"); sel.className = "ctl-sel";
  if (allowNone) { const o = document.createElement("option"); o.value = ""; o.textContent = "(none)"; if (!val) o.selected = true; sel.appendChild(o); }
  for (const s of state.signals) { const o = document.createElement("option"); o.value = s.ref; o.textContent = s.label + (s.ndim > 1 ? " (" + s.shape.join("×") + ")" : ""); if (s.ref === val) o.selected = true; sel.appendChild(o); }
  sel.addEventListener("change", () => { pushHist(); setPath(state.spec, path, sel.value || null); commit(); });
  w.appendChild(sel); return w;
}
function fmt(v, step) { if (v == null) return "(default)"; return step < 1 ? String(Math.round(v * 100) / 100) : String(Math.round(v)); }

/* ---------------- zoom ---------------- */
function applyTransform() {
  if (!stageInner || !state.render) return;
  const imgW = state.render.img_w, imgH = state.render.img_h;
  // place the image's CENTER at (stage center + pan); transform-origin is the image center
  stageInner.style.transform = `translate(${stage.clientWidth / 2 + state.pan.x - imgW / 2}px, ${stage.clientHeight / 2 + state.pan.y - imgH / 2}px) scale(${state.zoom})`;
  setZoomLabel();
}
function setZoom(nz, cx, cy) {
  if (!state.render) { state.zoom = clamp(nz, 0.1, 8); applyTransform(); return; }
  const old = state.zoom;
  nz = clamp(nz, 0.1, 8);
  const sr = stage.getBoundingClientRect();
  if (cx === undefined) { cx = sr.left + sr.width / 2; cy = sr.top + sr.height / 2; }
  const Cx = sr.width / 2 + state.pan.x, Cy = sr.height / 2 + state.pan.y; // figure center (stage coords)
  const mx = cx - sr.left, my = cy - sr.top;                              // cursor (stage coords)
  const k = nz / old;
  state.pan.x = mx - (mx - Cx) * k - sr.width / 2;
  state.pan.y = my - (my - Cy) * k - sr.height / 2;
  state.zoom = nz;
  applyTransform();
}
function setZoomLabel() { $("#zoomLabel").textContent = Math.round(state.zoom * 100) + "%"; }
function fitZoom() {
  if (!state.render) return;
  const pad = 80;
  state.zoom = clamp(Math.min((stage.clientWidth - pad) / state.render.img_w, (stage.clientHeight - pad) / state.render.img_h, 1), 0.1, 8);
  state.pan = { x: 0, y: 0 };
  applyTransform();
}

/* ---------------- export ---------------- */
async function openExport() {
  const { code } = await get(`/api/canvases/${state.slug}/code`);
  $("#codeBody").textContent = code; $("#codePath").textContent = (state.name || "canvas") + ".py";
  $("#exportModal").classList.remove("hidden");
}

/* ---------------- 3-D view finder (double-click a 3-D plot) ---------------- */
let v3d = null, v3dBusy = false, v3dAgain = false, v3dWired = false;
const is3D = (p) => p && ["scatter-3d", "surface", "wireframe"].includes(p.type);
const v3dPref = (k) => { try { return localStorage.getItem("figfoundry.invert3d." + k) === "1"; } catch (e) { return false; } };
const v3dSetPref = (k, v) => { try { localStorage.setItem("figfoundry.invert3d." + k, v ? "1" : "0"); } catch (e) {} };
function v3dVals() {
  if (!v3d) return;
  const e = $("#v3eVal"), a = $("#v3aVal"), z = $("#v3dVal");
  if (e) e.textContent = Math.round(v3d.elev) + "°";
  if (a) a.textContent = Math.round(v3d.azim) + "°";
  if (z) z.textContent = (Math.round(v3d.dist * 10) / 10);
}

async function openView3D(pid) {
  const p = state.spec.plots.find((x) => x.id === pid);
  if (!is3D(p)) return;
  v3dWire();
  const cp = JSON.parse(JSON.stringify(p)); cp.rect = [0.08, 0.08, 0.84, 0.84];
  v3d = { pid, spec: Object.assign({}, state.spec, { plots: [cp] }),
          elev: p.style.elev ?? 30, azim: p.style.azim ?? -60, dist: p.style.dist ?? 10,
          invertR: v3dPref("rotate"), invertZ: v3dPref("zoom") };
  $("#v3e").value = v3d.elev; $("#v3a").value = v3d.azim; $("#v3d").value = v3d.dist;
  $("#v3invR").checked = v3d.invertR; $("#v3invZ").checked = v3d.invertZ;
  v3dVals();
  $("#view3dModal").classList.remove("hidden");
  v3dRender();
}
function v3dRender() {
  if (v3dBusy) { v3dAgain = true; return; }   // keep one draft render in flight
  v3dBusy = true;
  const cp = v3d.spec.plots[0];
  cp.style.elev = v3d.elev; cp.style.azim = v3d.azim; cp.style.dist = v3d.dist;
  fetch(`/api/canvases/${state.slug}/render`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ spec: v3d.spec, draft: true }) })
    .then((r) => (r.ok ? r.json() : null))
    .then((d) => { if (d) $("#view3dImg").src = `data:${d.mime};base64,${d.img}`; v3dBusy = false; if (v3dAgain) { v3dAgain = false; v3dRender(); } })
    .catch(() => { v3dBusy = false; });
}
function v3dSync() { $("#v3e").value = v3d.elev; $("#v3a").value = v3d.azim; $("#v3d").value = v3d.dist; v3dVals(); v3dRender(); }
function v3dWire() {
  if (v3dWired) return; v3dWired = true;
  $("#v3e").oninput = (e) => { v3d.elev = +e.target.value; v3dVals(); v3dRender(); };
  $("#v3a").oninput = (e) => { v3d.azim = +e.target.value; v3dVals(); v3dRender(); };
  $("#v3d").oninput = (e) => { v3d.dist = +e.target.value; v3dVals(); v3dRender(); };
  $("#v3invR").onchange = (e) => { if (v3d) v3d.invertR = e.target.checked; v3dSetPref("rotate", e.target.checked); };
  $("#v3invZ").onchange = (e) => { if (v3d) v3d.invertZ = e.target.checked; v3dSetPref("zoom", e.target.checked); };
  const img = $("#view3dImg"); let drag = null;
  img.addEventListener("pointerdown", (e) => { drag = { x: e.clientX, y: e.clientY, a: v3d.azim, el: v3d.elev }; img.setPointerCapture(e.pointerId); });
  img.addEventListener("pointermove", (e) => { if (!drag) return; const s = v3d.invertR ? -1 : 1; v3d.azim = clamp(drag.a + (e.clientX - drag.x) * 0.5 * s, -180, 180); v3d.elev = clamp(drag.el - (e.clientY - drag.y) * 0.5 * s, -90, 90); v3dSync(); });
  img.addEventListener("pointerup", () => { drag = null; });
  img.addEventListener("wheel", (e) => { e.preventDefault(); const s = v3d.invertZ ? -1 : 1; v3d.dist = clamp(v3d.dist + (e.deltaY > 0 ? 0.6 : -0.6) * s, 3, 22); v3dSync(); }, { passive: false });
  $("#v3dAccept").onclick = () => {
    pushHist();
    const p = state.spec.plots.find((x) => x.id === v3d.pid);
    p.style.elev = v3d.elev; p.style.azim = v3d.azim; p.style.dist = v3d.dist;
    $("#view3dModal").classList.add("hidden");
    commit();
  };
  const close = () => $("#view3dModal").classList.add("hidden");
  $("#v3dCancel").onclick = close; $("#view3dClose").onclick = close;
  $("#view3dModal").onclick = (e) => { if (e.target === $("#view3dModal")) close(); };
}

/* ---------------- toast ---------------- */
let toastTimer;
function toast(msg, isErr) { const t = $("#toast"); t.textContent = msg; t.style.background = isErr ? "var(--danger)" : "#1c2026"; t.classList.remove("hidden"); requestAnimationFrame(() => t.classList.add("show")); clearTimeout(toastTimer); toastTimer = setTimeout(() => { t.classList.remove("show"); setTimeout(() => t.classList.add("hidden"), 200); }, 2200); }

/* ---------------- boot ---------------- */
async function boot() {
  const slug = new URLSearchParams(location.search).get("slug");
  if (!slug) { location.href = "/"; return; }
  state.slug = slug;
  let doc;
  try {
    doc = await get(`/api/canvases/${slug}`);
  } catch (e) { return showLoadError(e); }
  state.name = doc.name;
  state.spec = doc.spec;
  state.dataset = doc.spec.dataset || null;
  let sigRes;
  try {
    sigRes = await get(`/api/canvases/${slug}/signals`);
  } catch (e) { return showLoadError(e); }
  state.signals = normalizeSignals(sigRes.signals);
  $("#figName").value = doc.name;
  buildSignals();
  await doRender();
  fitZoom();
  buildInspector();
  wireChrome();
}
function showLoadError(e) {
  const msg = "Could not load this figure (" + (e.message || e) + "). It may have been deleted.";
  showErrBanner(new Error(msg));
  toast("Figure not found", true);
  setTimeout(() => { location.href = "/"; }, 1500);
}
function wireChrome() {
  v3dWire();
  $("#backBtn").onclick = () => { location.href = "/"; };
  $("#figName").addEventListener("change", async () => {
    state.name = $("#figName").value.trim() || "Untitled";
    $("#figName").value = state.name;
    await api(`/api/canvases/${state.slug}`, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name: state.name }) });
    toast("Renamed to " + state.name);
  });
  $("#figName").addEventListener("keydown", (e) => { if (e.key === "Enter") e.target.blur(); });
  $("#duplicateBtn").onclick = async () => {
    const r = await api(`/api/canvases/${state.slug}/duplicate`, { method: "POST" });
    location.href = "/canvas?slug=" + r.slug;
  };
  $("#deleteBtn").onclick = async () => {
    if (!confirm('Delete "' + state.name + '"? This cannot be undone.')) return;
    await api(`/api/canvases/${state.slug}`, { method: "DELETE" });
    location.href = "/";
  };
  $("#undoBtn").onclick = undo; $("#redoBtn").onclick = redo;
  $("#zoomIn").onclick = () => setZoom(state.zoom * 1.15); $("#zoomOut").onclick = () => setZoom(state.zoom / 1.15); $("#zoomFit").onclick = fitZoom;
  $("#addPlotBtn").onclick = () => { pushHist(); const sg = state.signals.filter((s) => s.ndim === 1); const x = sg.find((s) => s.name === "x") || sg[0]; const y = sg.find((s) => s !== x) || sg[0]; const p = _blankPlot(nextPlotId(), [0.35, 0.35, 0.3, 0.3], x ? x.ref : null, y ? y.ref : null); p.type = "line"; state.spec.plots.push(p); select("plot:" + p.id); commit(); };
  $("#loadDataBtn").onclick = (e) => { e.stopPropagation(); loadDataMenu(e.clientX, e.clientY); };
  $("#exportBtn").onclick = openExport;
  $("#modalClose").onclick = () => $("#exportModal").classList.add("hidden");
  $("#exportModal").onclick = (e) => { if (e.target === $("#exportModal")) $("#exportModal").classList.add("hidden"); };
  $("#copyCode").onclick = async () => { try { await navigator.clipboard.writeText($("#codeBody").textContent); toast("Code copied"); } catch (e) { toast("Copy failed", true); } };
  $("#downloadCode").onclick = () => { location.href = `/api/canvases/${state.slug}/download`; toast("Downloading " + (state.name || "canvas") + ".py"); };
  $("#dlPng").onclick = () => { location.href = `/api/canvases/${state.slug}/figure?format=png`; };
  $("#dlSvg").onclick = () => { location.href = `/api/canvases/${state.slug}/figure?format=svg`; };
  $("#dlPdf").onclick = () => { location.href = `/api/canvases/${state.slug}/figure?format=pdf`; };
  let pan = null, spaceDown = false;
  stage.addEventListener("wheel", (e) => { if (!state.render) return; e.preventDefault(); setZoom(state.zoom * (e.deltaY < 0 ? 1.12 : 1 / 1.12), e.clientX, e.clientY); }, { passive: false });
  stage.addEventListener("pointerdown", (e) => {
    const empty = e.target === stage || e.target === stageInner || e.target === figImg || e.target === overlay;
    if (e.button === 1 || (e.button === 0 && (empty || spaceDown))) {
      e.preventDefault();
      pan = { x: e.clientX, y: e.clientY, px: state.pan.x, py: state.pan.y, clickEmpty: e.button === 0 && empty && !spaceDown };
    }
  });
  window.addEventListener("pointermove", (e) => { if (!pan) return; state.pan.x = pan.px + (e.clientX - pan.x); state.pan.y = pan.py + (e.clientY - pan.y); applyTransform(); });
  window.addEventListener("pointerup", (e) => { if (pan && pan.clickEmpty && Math.abs(e.clientX - pan.x) < 3 && Math.abs(e.clientY - pan.y) < 3) select(null); pan = null; });
  window.addEventListener("keydown", (e) => { if (e.code === "Space" && !/INPUT|SELECT|TEXTAREA/.test(e.target.tagName || "")) { spaceDown = true; stage.style.cursor = "grab"; } });
  window.addEventListener("keyup", (e) => { if (e.code === "Space") { spaceDown = false; stage.style.cursor = ""; } });
  document.addEventListener("keydown", (e) => {
    const tgt = e.target;
    if (tgt.isContentEditable || tgt.tagName === "INPUT" || tgt.tagName === "SELECT" || tgt.tagName === "TEXTAREA") { if (tgt.tagName === "INPUT" && tgt.type === "text" && e.key === "Escape") tgt.blur(); return; }
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "z") { e.preventDefault(); e.shiftKey ? redo() : undo(); }
    else if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "y") { e.preventDefault(); redo(); }
    else if (e.key === "Escape") { if (!$("#exportModal").classList.contains("hidden")) $("#exportModal").classList.add("hidden"); else select(null); }
    else if (e.key.startsWith("Arrow") && state.selected) { e.preventDefault(); const s = e.shiftKey ? 0.05 : 0.005; if (e.key === "ArrowLeft") nudgeSelected(-s, 0); else if (e.key === "ArrowRight") nudgeSelected(s, 0); else if (e.key === "ArrowUp") nudgeSelected(0, s); else if (e.key === "ArrowDown") nudgeSelected(0, -s); }
    else if ((e.key === "Delete" || e.key === "Backspace") && state.selected) {
      e.preventDefault(); pushHist();
      const ids = state.selection.size ? [...state.selection] : [state.selected];
      const plotIds = new Set(ids.filter((id) => id.startsWith("plot:")).map((id) => id.slice(5)));
      if (plotIds.size) state.spec.plots = state.spec.plots.filter((p) => !plotIds.has(p.id));
      const artDel = new Map(), lineDel = new Map(), figDel = [];
      for (const id of ids) {
        if (id.startsWith("artist:")) { const [, pid, j] = id.split(":"); if (!artDel.has(pid)) artDel.set(pid, []); artDel.get(pid).push(+j); }
        else if (id.startsWith("line:")) { const [, pid, j] = id.split(":"); if (!lineDel.has(pid)) lineDel.set(pid, []); lineDel.get(pid).push(+j); }
        else if (id.startsWith("figartist:")) figDel.push(+id.slice(10));
        else if (id.startsWith("title:")) { const p = state.spec.plots.find((x) => x.id === id.slice(6)); if (p) p.title.visible = false; }
      }
      // splice high-to-low so deleting several items at once doesn't shift the other indices
      for (const [pid, js] of artDel) { const p = state.spec.plots.find((x) => x.id === pid); if (p) js.sort((a, b) => b - a).forEach((j) => p.artists.splice(j, 1)); }
      for (const [pid, js] of lineDel) { const p = state.spec.plots.find((x) => x.id === pid); if (p && p.extra_lines) js.sort((a, b) => b - a).forEach((j) => p.extra_lines.splice(j, 1)); }
      figDel.sort((a, b) => b - a).forEach((j) => state.spec.figure_artists.splice(j, 1));
      select(null); commit();
    }
  });
  // settle (full-quality render + save) the instant an arrow key is released
  document.addEventListener("keyup", (e) => {
    if (e.key.startsWith("Arrow") && nudgePushed) { clearTimeout(nudgeTimer); nudgePushed = false; commit(); }
  });
}

window.addEventListener("error", (e) => { if (e.error) showErrBanner(e.error); });
boot();
