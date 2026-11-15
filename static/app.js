'use strict';
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const h = UI.h;
const TEXT_DEFAULTS = () => ({
  font: 'Arial', size: 16, color: '#111827', bold: false, italic: false,
  underline: { on: false, style: 'solid', color: null, width: null, offset: null, skipInk: false },
  strike: { on: false, style: 'solid', color: null, width: null, position: null },
  overline: { on: false, style: 'solid', color: null, width: null, offset: null },
  marker: null, bg: null, border: { style: 'solid', color: null, width: 1, sides: 'trbl' }, radius: 0, padding: 4,
  boxShadow: { on: false, color: '#0000003d', blur: 10, x: 0, y: 4 },
  shadow: { on: false, color: '#00000066', blur: 2, x: 1, y: 1 }, outline: { on: false, color: '#000000', width: 1 },
  spacing: 0, wordSpacing: 0, lineHeight: 1.2, paraBefore: 0, paraAfter: 0, indent: 0, firstIndent: 0, list: null,
  align: 'left', valign: 'top', script: null, case: null, smallCaps: false, opacity: 1, rot: 0, link: '', fit: 'grow',
});
const STYLE_KEYS = Object.keys(TEXT_DEFAULTS());
// Older boxes (made before a property existed) get the defaults filled in.
function normText(o) {
  const d = TEXT_DEFAULTS();
  for (const k of STYLE_KEYS) {
    if (o[k] === undefined) o[k] = d[k];
    else if (d[k] && typeof d[k] === 'object' && o[k] && typeof o[k] === 'object') for (const kk in d[k]) if (o[k][kk] === undefined) o[k][kk] = d[k][kk];
  }
  return o;
}
const S = {
  id: null, name: '', pages: [], zoom: 1.25, tool: 'select', ver: 0, pending: 0,
  editing: null,  // inline retype of existing PDF text
  objs: [], sel: null, editObj: null, img: null, fonts: [], spanFonts: new Set(), hist: [], fut: [], seq: 1,
  defaults: {
    text: TEXT_DEFAULTS(),
    shape: { kind: 'rect', stroke: '#6366f1', fill: null, width: 2, opacity: 1, rot: 0 },
    ink: { stroke: '#111827', width: 2.5, opacity: 1 },
    highlight: { fill: '#facc15', opacity: 0.45 },
    whiteout: { fill: '#ffffff' },
    stamp: { kind: 'check', stroke: '#16a34a', width: 2.5 },
    note: { fill: '#facc15' },
  },
};
const api = (p, q = {}) => `/api/${p}?` + new URLSearchParams({ id: S.id, ...q });
const viewer = $('#viewer'), pagesBox = $('#pages'), pbar = $('#pbar');
const isMobile = () => matchMedia('(max-width: 760px)').matches;
const clone = o => JSON.parse(JSON.stringify(o));
const snap = () => JSON.stringify(S.objs);

const TOASTS = new Map();  // one toast per message, with a repeat count
function toast(msg, err) {
  let t = TOASTS.get(msg);
  if (t) { t.n++; t.el.dataset.n = '×' + t.n; clearTimeout(t.a); clearTimeout(t.b); t.el.classList.remove('out'); }
  else { t = { n: 1, el: h('div', 'toast' + (err ? ' err' : ''), { textContent: msg }) }; $('#toasts').append(t.el); TOASTS.set(msg, t); }
  t.a = setTimeout(() => t.el.classList.add('out'), err ? 5600 : 3600);
  t.b = setTimeout(() => { t.el.remove(); TOASTS.delete(msg); }, err ? 6000 : 4000);
}

async function call(url, opts, retried) {
  S.pending++; document.body.classList.add('busy');
  try {
    const r = await fetch(url, opts);
    const ct = r.headers.get('Content-Type') || '';
    const body = ct.includes('json') ? await r.json() : await r.blob();
    if (!r.ok) throw new Error(body.error || r.statusText);
    return body;
  } catch (e) {
    // the server lost the document: re-upload it, replay the edits, retry once, quietly
    if (!retried && /not loaded/i.test(e.message) && S.file) {
      const old = S.id;
      if (await recover()) return call(url.replace('id=' + old, 'id=' + S.id), opts, true);
    }
    toast(e.message, true); return null;
  }
  finally { if (!--S.pending) document.body.classList.remove('busy'); }
}

// ---------- history: object edits and server ops share one timeline ----------
function record(before, tag) {
  const now = snap();
  if (before === now) return;
  const top = S.hist[S.hist.length - 1];
  if (tag && top && top.tag === tag && Date.now() - top.t < 1500) { top.after = now; top.t = Date.now(); }
  else S.hist.push({ before, after: now, tag, t: Date.now() });
  S.fut = []; syncUndo();
}
function syncUndo() { $('#undo').disabled = !S.hist.length; $('#redo').disabled = !S.fut.length; }

async function recover() {
  try {
    const r = await fetch('/api/open', { method: 'POST', headers: { 'X-Filename': encodeURIComponent(S.name) }, body: S.file });
    if (!r.ok) return false;
    S.id = (await r.json()).id;
    for (const [name, args] of S.srvLog) await fetch(api('op'), { method: 'POST', body: JSON.stringify({ op: name, ...args }) });
    return true;
  } catch (e) { return false; }
}
async function rawOp(name, args = {}) {
  const j = await call(api('op'), { method: 'POST', body: JSON.stringify({ op: name, ...args }) });
  if (!j) return null;
  S.srvLog.push([name, args]);
  (j.notes || []).forEach(n => toast(n));
  const structural = j.dirty === null || j.pages.length !== S.pages.length;
  S.pages = j.pages;
  if (structural) build(); else await Promise.all(j.dirty.map(reload));
  return j;
}
async function op(name, args = {}, remap) {
  const before = snap();
  const j = await rawOp(name, args);
  if (!j) return null;
  if (remap) { remap(); renderAllObjs(); }
  S.hist.push({ srv: true, before, after: snap(), t: Date.now() });
  S.fut = []; syncUndo();
  return j;
}
async function undo() {
  await finishEdits(true);
  const e = S.hist.pop(); if (!e) return;
  if (e.srv) await rawOp('undo');
  S.objs = JSON.parse(e.before); S.fut.push(e); select(null); renderAllObjs(); syncUndo();
}
async function redo() {
  await finishEdits(true);
  const e = S.fut.pop(); if (!e) return;
  if (e.srv) await rawOp('redo');
  S.objs = JSON.parse(e.after); S.hist.push(e); select(null); renderAllObjs(); syncUndo();
}

// ---------- fonts ----------
fetch('/api/fontlist').then(r => r.json()).then(list => {
  S.fonts = list;
  const url = (f, b, i) => `url(/api/sysfont?${new URLSearchParams({ f: f.label, b: b ? 1 : 0, i: i ? 1 : 0 })})`;
  const css = list.map(f => [
    `@font-face{font-family:'F_${f.label}';src:${url(f, 0, 0)};font-weight:400;font-style:normal}`,
    f.b && `@font-face{font-family:'F_${f.label}';src:${url(f, 1, 0)};font-weight:700;font-style:normal}`,
    f.i && `@font-face{font-family:'F_${f.label}';src:${url(f, 0, 1)};font-weight:400;font-style:italic}`,
    f.bi && `@font-face{font-family:'F_${f.label}';src:${url(f, 1, 1)};font-weight:700;font-style:italic}`,
  ].filter(Boolean).join('')).join('');
  document.head.append(h('style', '', { textContent: css }));
}).catch(() => {});

// ---------- open / save ----------
async function openFile(file) {
  if (!file) return;
  const j = await call('/api/open', { method: 'POST', headers: { 'X-Filename': encodeURIComponent(file.name) }, body: file });
  if (!j) return;
  Object.assign(S, { file, srvLog: [], id: j.id, name: file.name, pages: j.pages, spanFonts: new Set(), objs: [], hist: [], fut: [], sel: null, editObj: null });
  document.body.classList.remove('empty');
  document.title = file.name + ' — Folio';
  $('#docname').textContent = file.name;
  syncUndo();
  viewer.scrollTop = 0;
  fitWidth();
  if (j.scanned) {
    toast(`${j.scanned} scanned page${j.scanned > 1 ? 's' : ''} found — recognising text…`);
    runOcr();
  }
}
async function runOcr() {
  const j = await op('ocr');
  if (j) toast(j.count ? `Text recognised on ${j.count} page${j.count > 1 ? 's' : ''} — click any line to edit` : 'No scanned pages without text');
}
const readB64 = file => new Promise(res => { const r = new FileReader(); r.onload = () => res(r.result.split(',')[1]); r.readAsDataURL(file); });
const readURL = file => new Promise(res => { const r = new FileReader(); r.onload = () => res(r.result); r.readAsDataURL(file); });

// Line breaks exactly as the browser laid them out, so the PDF wraps the same way.
function textLines(o) {
  const el = objEl(o.id);
  return el ? measure(o, el).map(l => [l.text, l.hard]) : null;
}

async function download() {
  await finishEdits();
  await document.fonts.ready;
  const objs = S.objs.map(o => o.type === 'text' ? { ...o, lines: textLines(o) } : o);
  const blob = await call(api('export'), { method: 'POST', body: JSON.stringify({ objs }) });
  if (!blob) return;
  const fname = S.name.replace(/\.pdf$/i, '') + '-edited.pdf';
  const iOS = /iP(hone|ad|od)/.test(navigator.userAgent) || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
  if (iOS) {
    // iOS: the share sheet saves to Files reliably; fall back to opening the PDF
    const f = new File([blob], fname, { type: 'application/pdf' });
    try { if (navigator.canShare?.({ files: [f] })) { await navigator.share({ files: [f], title: fname }); return; } } catch (err) { if (err.name === 'AbortError') return; }
    const u = URL.createObjectURL(blob); if (!window.open(u, '_blank')) location.href = u; return;
  }
  const a = h('a', '', { href: URL.createObjectURL(blob), download: fname });
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 5000);
  toast('Downloaded ' + a.download);
}

// ---------- page rendering ----------
const io = new IntersectionObserver(es => es.forEach(e => {
  if (e.isIntersecting && !e.target.dataset.loaded) reload(+e.target.dataset.n);
}), { root: viewer, rootMargin: '1200px 0px' });

const ICO = {
  rotl: '<svg viewBox="0 0 24 24"><path d="M4.5 9A8 8 0 1112 20M4.5 4v5h5"/></svg>',
  rotr: '<svg viewBox="0 0 24 24"><path d="M19.5 9A8 8 0 1012 20M19.5 4v5h-5"/></svg>',
  dup: '<svg viewBox="0 0 24 24"><rect x="8" y="8" width="12" height="12" rx="2"/><path d="M16 8V6a2 2 0 00-2-2H6a2 2 0 00-2 2v8a2 2 0 002 2h2"/></svg>',
  del: '<svg viewBox="0 0 24 24"><path d="M5 7h14M10 7V5h4v2M7 7l1 12h8l1-12"/></svg>',
  plus: '<svg viewBox="0 0 24 24"><path d="M12 5v14M5 12h14"/></svg>',
};

function build() {
  const ratio = viewer.scrollTop / (viewer.scrollHeight || 1);
  io.disconnect();
  pagesBox.innerHTML = '';
  $('#thumbs').innerHTML = '';
  S.editing = null; S.editObj = null;
  S.pages.forEach((p, n) => {
    const el = h('div', 'page');
    el.dataset.n = n;
    el.style.width = p.w * S.zoom + 'px'; el.style.height = p.h * S.zoom + 'px';
    el.innerHTML = `<img alt="Page ${n + 1}" draggable="false"><div class="layer"></div><div class="objs"></div><svg class="draw"></svg><div class="pnum">${n + 1}</div>`;
    pagesBox.append(el);
    io.observe(el);
    const t = h('div', 'thumb');
    t.dataset.n = n;
    t.innerHTML = `<div class="tim"><img loading="lazy" alt="" style="aspect-ratio:${p.w}/${p.h}" src="${api('page', { n, s: 0.3, v: S.ver })}">
      <div class="tact"><button data-p="rotate" data-deg="90" title="Rotate">${ICO.rotr}</button><button data-p="dup" title="Duplicate">${ICO.dup}</button>
      <button data-p="blank" title="Insert blank page after">${ICO.plus}</button><button data-p="delete" title="Delete" ${S.pages.length > 1 ? '' : 'disabled'}>${ICO.del}</button></div></div><span>${n + 1}</span>`;
    $('#thumbs').append(t);
  });
  renderAllObjs();
  viewer.scrollTop = ratio * viewer.scrollHeight;
  $('#zoom').textContent = Math.round(S.zoom * 100) + '%';
}

async function reload(n) {
  const el = $(`.page[data-n="${n}"]`);
  if (!el) return;
  el.dataset.loaded = 1;
  const v = ++S.ver;
  // iOS can't hold very large images: keep the render under ~4096px wide.
  const pw = (el.offsetWidth || 600) / (S.zoom || 1);
  const sc = Math.min(S.zoom * devicePixelRatio, 4096 / Math.max(pw, 1));
  const src = api('page', { n, s: sc.toFixed(2), v });
  const pre = new Image(); pre.src = src;
  const [spans, ok] = await Promise.all([call(api('spans', { n })), pre.decode().then(() => true, () => false)]);
  if (!el.isConnected) return;
  // Keep the previous render if the sharper one failed to load.
  if (ok) $('img', el).src = src;
  const th = $(`.thumb[data-n="${n}"] img`);
  if (th) th.src = api('page', { n, s: 0.3, v });
  if (spans) renderSpans(el, n, spans);
}

function fontStack(sp) {
  const fams = sp.keys.map(k => {
    const fam = 'pf_' + S.id.slice(0, 8) + '_' + k.replace(/\W/g, '_');
    if (!S.spanFonts.has(fam)) {
      S.spanFonts.add(fam);
      new FontFace(fam, `url(${api('font', { k })})`).load().then(f => document.fonts.add(f)).catch(() => {});
    }
    return `"${fam}"`;
  });
  const n = sp.font.toLowerCase();
  fams.push(sp.flags & 8 || /mono|courier/.test(n) ? 'monospace' : sp.flags & 4 || /times|serif/.test(n) && !/sans/.test(n) ? 'serif' : 'sans-serif');
  return fams.join(',');
}

function renderSpans(el, n, spans) {
  const layer = $('.layer', el), z = S.zoom;
  layer.innerHTML = '';
  for (const sp of spans) {
    const [x0, y0, x1, y1] = sp.bbox, d = h('div', 'span', { textContent: sp.text });
    d._sp = sp; d._n = n;
    d.style.cssText = `left:${x0 * z}px;top:${y0 * z}px;min-width:${(x1 - x0) * z}px;height:${(y1 - y0) * z}px;line-height:${(y1 - y0) * z}px;` +
      `font-size:${sp.size * z}px;font-family:${fontStack(sp)};font-weight:${sp.flags & 16 || /bold|black|heavy/i.test(sp.font) ? 700 : 400};font-style:${sp.flags & 2 || /italic|oblique/i.test(sp.font) ? 'italic' : 'normal'}`;
    layer.append(d);
  }
}

function sampleBg(el, bbox) {
  const img = $('img', el);
  if (!img.naturalWidth) return '#ffffff';
  const c = document.createElement('canvas'), k = img.naturalWidth / el.clientWidth * S.zoom;
  c.width = img.naturalWidth; c.height = img.naturalHeight;
  const g = c.getContext('2d', { willReadFrequently: true });
  g.drawImage(img, 0, 0);
  const [x0, y0, x1, y1] = bbox, counts = {};
  for (const [x, y] of [[x0 - 2, y0], [x1 + 2, y0], [x0 - 2, y1], [x1 + 2, y1], [(x0 + x1) / 2, y0 - 1]]) {
    const [r, gg, b] = g.getImageData(Math.max(0, x * k), Math.max(0, y * k), 1, 1).data;
    const key = `rgb(${r},${gg},${b})`; counts[key] = (counts[key] || 0) + 1;
  }
  return Object.entries(counts).sort((a, b) => b[1] - a[1])[0][0];
}

// ---------- retyping existing PDF text ----------
const B14_CSS = { he: 'Helvetica,Arial,sans-serif', ti: '"Times New Roman",Times,serif', co: '"Courier New",monospace' };
const famCss = v => !v ? null : v.startsWith('f:') ? `'F_${v.slice(2)}',sans-serif` : B14_CSS[v];

function caretAt(x, y) {
  const r = document.caretRangeFromPoint?.(x, y);
  if (r) { const s = getSelection(); s.removeAllRanges(); s.addRange(r); }
}

async function startScannedEdit(d, ev) {
  const sp = d._sp, n = d._n, { clientX, clientY } = ev;
  if (sp.text.trim().split(/\s+/).length < 2) return startEdit(d, ev);
  d.classList.add('working');
  const box = d.closest('.page').getBoundingClientRect();
  const j = await op('split', { page: n, bbox: sp.bbox, text: sp.text, x: (clientX - box.left) / S.zoom });
  d.classList.remove('working');
  if (!j) return;
  const hit = document.elementFromPoint(clientX, clientY)?.closest('.span');
  if (hit) startEdit(hit, { clientX, clientY });
}

function startEdit(d, ev) {
  select(null);
  const sp = d._sp, el = d.closest('.page');
  S.editing = { el: d, n: d._n, sp, orig: sp.text, family: '', bold: !!(sp.flags & 16), italic: !!(sp.flags & 2), size: sp.size, color: sp.color };
  d.contentEditable = 'plaintext-only';
  d.classList.add('editing');
  d.style.color = sp.color;
  d.style.background = sampleBg(el, sp.bbox);
  d.focus();
  caretAt(ev.clientX, ev.clientY);
  renderBar();
  if (sp.ocr) previewScanned(d, sp);
}

async function previewScanned(d, sp) {
  d.style.color = '#444';
  d.classList.add('working');
  const m = await call(api('match', { n: d._n, bbox: sp.bbox.join(','), text: sp.text }));
  d.classList.remove('working');
  if (!m || S.editing?.el !== d) return;
  S.editing.alts = m.alts;
  Object.assign(d.style, { color: m.ink, background: m.bg });
  S.editing.color = sp.color = m.ink;
  showMatch(d, sp, 0);
  toast(m.confident === false ? `Best guess: ${m.label}. Check the font menu — if the exact font can't be pinned down, nothing will be changed.`
    : `Matched font: ${m.label} — other close matches are in the font menu`, m.confident === false);
  renderBar();
}

function showMatch(d, sp, i) {
  const a = S.editing.alts[i], z = S.zoom;
  const fam = a.key ? fontStack({ keys: [a.key], font: a.label, flags: 0 }) : B14_CSS[a.b14.slice(0, 2)];
  Object.assign(d.style, { fontFamily: fam, fontSize: a.size * z + 'px',
    fontWeight: a.b14 && /bo|bi/.test(a.b14) ? 700 : 400, fontStyle: a.b14 && /it|bi|oi/.test(a.b14) ? 'italic' : 'normal' });
  S.editing.match = i; S.editing.size = sp.size = a.size;
}

async function commitSpan(cancel) {
  const e = S.editing;
  if (!e) return;
  S.editing = null;
  const d = e.el, text = d.innerText.replace(/\n+$/, '');
  d.contentEditable = 'false';
  d.classList.remove('editing');
  renderBar();
  const sp = e.sp;
  const changed = text !== e.orig || e.size !== sp.size || e.color !== sp.color || e.family || e.bold !== !!(sp.flags & 16) || e.italic !== !!(sp.flags & 2);
  if (cancel || !changed) { d.textContent = e.orig; d.style.color = d.style.background = ''; return; }
  d.classList.add('working');
  const exact = sp.ocr && e.alts && !e.family ? { match_label: e.alts[e.match || 0].label } : {};
  const j = await op('edit', { page: e.n, items: [{ ...sp, ...exact, text: text.replace(/\n/g, ' '), orig: e.orig, size: e.size, color: e.color, family: e.family || null,
    bold: e.bold, italic: e.italic, size_changed: e.size !== sp.size, color_changed: e.color !== sp.color, match: e.match || 0 }] });
  if (!j) { d.classList.remove('working'); d.textContent = e.orig; d.style.color = d.style.background = ''; }
}

function spanProp(k, v) {
  const e = S.editing; if (!e) return;
  e[k] = v;
  const st = e.el.style, z = S.zoom;
  if (k === 'size') st.fontSize = v * z + 'px';
  if (k === 'color') st.color = v;
  if (k === 'bold') st.fontWeight = v ? 700 : 400;
  if (k === 'italic') st.fontStyle = v ? 'italic' : 'normal';
  if (k === 'family') st.fontFamily = v ? famCss(v) : '';
  e.el.focus();
}

const FONT_GUESS = n => {
  n = n.toLowerCase();
  const hit = S.fonts.find(f => n.replace(/\s/g, '').includes(f.label.toLowerCase().replace(/\s/g, '')));
  return hit ? hit.label : /courier|mono/.test(n) ? 'Courier New' : /times|serif|roman|garamond|minion|cambria/.test(n) && !/sans/.test(n) ? 'Times New Roman' : 'Arial';
};

// Styling the PDF can't express on the original glyphs: lift the text into a fully editable text box.
async function liftSpan(patch) {
  const e = S.editing; if (!e) return;
  const sp = e.sp, text = e.el.innerText.replace(/\n+$/, ''), n = e.n;
  const [x0, y0, x1, y1] = sp.bbox;
  S.editing = null; e.el.contentEditable = 'false'; e.el.classList.remove('editing');
  const before = snap();
  const j = await rawOp('erase_text', { page: n, rect: sp.bbox });
  if (!j) return;
  const size = e.size, fam = e.family?.startsWith('f:') ? e.family.slice(2) : FONT_GUESS(sp.font);
  // Stays inline: no padding, no box, hugging the text and sitting on the original baseline.
  const F = FM(fam), lh = size * 1.2, base = sp.origin ? sp.origin[1] : y1 + F.desc * size;
  const o = newObj('text', n, {
    ...TEXT_DEFAULTS(), font: fam, size, color: e.color, bold: e.bold, italic: e.italic, text, inline: true, padding: 0, fit: 'hug',
    x: x0, y: base - F.asc * size - (lh - (F.asc - F.desc) * size) / 2, w: x1 - x0, h: lh, ...patch,
  });
  fitText(o);
  S.hist.push({ srv: true, before, after: snap(), t: Date.now() }); S.fut = []; syncUndo();
  select(o.id); editText(o.id);
}

// ---------- objects ----------
const STAMPS = { check: [[[0.12, 0.55], [0.4, 0.84], [0.9, 0.16]]], cross: [[[0.16, 0.16], [0.84, 0.84]], [[0.84, 0.16], [0.16, 0.84]]] };
function newObj(type, page, props) {
  const o = { id: 'o' + S.seq++, type, page, rot: 0, opacity: 1, ...clone(props) };
  S.objs.push(o); renderObj(o);
  return o;
}
const objById = id => S.objs.find(o => o.id === id);
const objEl = id => $(`.obj[data-id="${id}"]`);
const pageEl = n => $(`.page[data-n="${n}"]`);

function renderAllObjs() {
  $$('.objs').forEach(l => l.innerHTML = '');
  S.objs.forEach(renderObj);
  if (S.sel && !objById(S.sel)) S.sel = null;
  decorateSel(); renderBar();
}

function renderObj(o) {
  const pg = pageEl(o.page); if (!pg) return;
  let el = objEl(o.id);
  const layer = $('.objs', pg);
  if (!el || el.parentNode !== layer) {
    el?.remove();
    el = h('div', 'obj ' + o.type + (o.inline ? ' inline' : '')); el.dataset.id = o.id;
    layer.append(el);
  }
  [...layer.children].forEach(c => c.style.zIndex = S.objs.findIndex(x => x.id === c.dataset.id) + 1);
  const z = S.zoom;
  Object.assign(el.style, { left: o.x * z + 'px', top: o.y * z + 'px', width: o.w * z + 'px', height: o.h * z + 'px',
    transform: o.rot ? `rotate(${o.rot}deg)` : '', opacity: o.type === 'highlight' ? '' : o.opacity ?? 1 });
  const W = o.w * z, H = o.h * z;
  if (o.type === 'text') return paintText(o, el);
  if (o.type === 'highlight') { el.style.background = o.fill; el.style.opacity = o.opacity; return; }
  if (o.type === 'whiteout') { el.style.background = o.fill || '#fff'; return; }
  if (o.type === 'image') { if (!$('img', el)) el.append(h('img', '', { src: o.data, draggable: false })); return; }
  if (o.type === 'note') { el.innerHTML = `<svg viewBox="0 0 24 24"><path d="M4 4h16v12l-4 4H4z" fill="${o.fill}"/><path d="M16 20v-4h4" fill="none"/></svg>`; el.title = o.text || 'Note'; return; }
  const sw = (o.width || 2) * z, col = o.stroke || 'none', fill = o.fill || 'none';
  let body = '';
  if (o.type === 'rect') body = `<rect x="0" y="0" width="${W}" height="${H}" stroke="${col}" fill="${fill}" stroke-width="${sw}"/>`;
  if (o.type === 'ellipse') body = `<ellipse cx="${W / 2}" cy="${H / 2}" rx="${W / 2}" ry="${H / 2}" stroke="${col}" fill="${fill}" stroke-width="${sw}"/>`;
  if (o.type === 'line' || o.type === 'arrow') {
    const [[ax, ay], [bx, by]] = o.pts.map(([px, py]) => [px * W, py * H]);
    body = `<line x1="${ax}" y1="${ay}" x2="${bx}" y2="${by}" stroke="${col}" stroke-width="${sw}"/>`;
    if (o.type === 'arrow') {
      const a = Math.atan2(by - ay, bx - ax), L = Math.max(8, (o.width || 2) * 4) * z;
      const p = [[bx, by], ...[0.45, -0.45].map(d => [bx - L * Math.cos(a + d), by - L * Math.sin(a + d)])];
      body += `<polygon points="${p.map(q => q.join(',')).join(' ')}" fill="${col}" stroke="${col}" stroke-width="${sw}"/>`;
    }
  }
  if (o.paths) body = o.paths.map(pth => `<polyline points="${pth.map(([px, py]) => px * W + ',' + py * H).join(' ')}" stroke="${col}" fill="none" stroke-width="${sw}"/>`).join('');
  el.innerHTML = `<svg width="${W}" height="${H}" overflow="visible" stroke-linecap="round" stroke-linejoin="round">${body}</svg>`;
  decorateSel();
}

const FM = (label, b, i) => { const f = S.fonts.find(x => x.label === label); return f?.m?.[`${b ? 1 : 0}${i ? 1 : 0}`] || f || { asc: 0.9, desc: -0.21, strike: 0.3, strikeSize: 0.05, ul: 0.1, ulSize: 0.05 }; };
const INK = new Map();
// x-runs (em from the glyph origin) where a glyph has ink inside a band below the baseline; same method as server.glyph_ink
function glyphInk(o, c, top, bot) {
  const font = `${o.italic ? 'italic ' : ''}${o.bold ? 700 : 400} 200px 'F_${o.font}'`, key = [font, c, top.toFixed(3), bot.toFixed(3)].join('|');
  if (INK.has(key)) return INK.get(key);
  const E = 200, cv = document.createElement('canvas'); cv.width = E * 3; cv.height = E * 3;
  const g = cv.getContext('2d', { willReadFrequently: true }); g.font = font; g.fillText(c, E, E * 1.5);
  const y0 = Math.floor(E * 1.5 + top * E), rows = Math.max(1, Math.floor(E * 1.5 + bot * E) + 1 - y0);
  const d = g.getImageData(0, y0, E * 3, rows).data, runs = [];
  let start = null;
  for (let x = 0; x <= E * 3; x++) {
    let ink = false;
    if (x < E * 3) for (let y = 0; y < rows && !ink; y++) ink = d[(y * E * 3 + x) * 4 + 3] > 115;
    if (ink && start === null) start = x;
    if (!ink && start !== null) { runs.push([(start - E) / E, (x - E) / E]); start = null; }
  }
  if (document.fonts.check(font)) INK.set(key, runs);
  return runs;
}
const DESC = new Set('gjpqyQJ,;()[]{}|/_@$');
const decoW = (spec, fs, F, kind) => spec.width || (kind === 's' ? Math.max(0.75, fs * F.strikeSize) : Math.max(0.5, fs * F.ulSize));

function setParas(flow, text) {
  flow.replaceChildren(...text.split('\n').map(t => {
    const p = h('div', 'p'); const u = h('span', 'tu'); const s = h('span', 'ts', { textContent: t });
    u.append(s); p.append(u); return p;
  }));
}
function flowText(flow) {
  const kids = [...flow.childNodes];
  if (kids.length && kids.every(k => k.classList?.contains('p'))) return kids.map(k => k.textContent).join('\n');
  return flow.innerText.replace(/\n$/, '');
}
function flowClean(flow) {
  return [...flow.childNodes].every(k => k.classList?.contains('p') && k.childNodes.length === 1 && k.firstChild.classList?.contains('tu')
    && k.firstChild.childNodes.length === 1 && k.firstChild.firstChild.classList?.contains('ts') && !k.textContent.includes('\n'));
}
function getCaret(flow) {
  const sel = getSelection(); if (!sel.rangeCount || !flow.contains(sel.anchorNode)) return null;
  const r = document.createRange(); r.selectNodeContents(flow); r.setEnd(sel.anchorNode, sel.anchorOffset);
  const tmp = h('div'); tmp.append(r.cloneContents());
  return [...tmp.childNodes].map(n => n.textContent).join('\n').length;
}
function setCaret(flow, off) {
  for (const p of flow.children) {
    const len = p.textContent.length;
    if (off <= len) {
      const ts = $('.ts', p), node = ts.firstChild || ts;
      const r = document.createRange(); r.setStart(node, node.nodeType === 3 ? off : 0); r.collapse(true);
      getSelection().removeAllRanges(); getSelection().addRange(r); return;
    }
    off -= len + 1;
  }
}

function paintText(o, el) {
  normText(o);
  const z = S.zoom, F = FM(o.font, o.bold, o.italic);
  let tx = $('.tx', el);
  if (!tx) {
    el.innerHTML = '<div class="tx"><svg class="dk dku"></svg><div class="flow"></div><svg class="dk dko"></svg></div>';
    tx = $('.tx', el);
  }
  const flow = $('.flow', tx);
  if (S.editObj !== o.id && flowText(flow) !== o.text) setParas(flow, o.text);
  const fs = o.size * (o.script ? 0.65 : 1), shift = { sup: -0.35, sub: 0.15 }[o.script] || 0, lh = o.size * o.lineHeight;
  const bs = o.boxShadow, sh = o.shadow, ol = o.outline;
  Object.assign(tx.style, {
    padding: o.padding * z + 'px', background: o.bg || 'transparent', borderRadius: o.radius * z + 'px',
    boxShadow: bs.on ? `${bs.x * z}px ${bs.y * z}px ${bs.blur * z}px ${bs.color}` : 'none',
    justifyContent: { middle: 'center', bottom: 'flex-end' }[o.valign] || 'flex-start',
  });
  Object.assign(flow.style, {
    fontFamily: `'F_${o.font}', sans-serif`, fontSize: fs * z + 'px', lineHeight: lh * z + 'px', letterSpacing: o.spacing * z + 'px',
    wordSpacing: o.wordSpacing * z + 'px', color: o.color, fontWeight: o.bold ? 700 : 400, fontStyle: o.italic ? 'italic' : 'normal',
    textTransform: { upper: 'uppercase', lower: 'lowercase', title: 'capitalize' }[o.case] || 'none', fontVariantCaps: o.smallCaps ? 'small-caps' : 'normal',
    textShadow: sh.on ? `${sh.x * z}px ${sh.y * z}px ${sh.blur * z}px ${sh.color}` : 'none',
    webkitTextStroke: ol.on ? `${ol.width * z}px ${ol.color}` : '0', textAlign: o.align,
  });
  const li = o.list ? 18 : 0;
  [...flow.children].forEach((p, i) => {
    Object.assign(p.style, { paddingTop: o.paraBefore * z + 'px', paddingBottom: o.paraAfter * z + 'px', paddingLeft: (o.indent + li) * z + 'px',
      textIndent: o.firstIndent * z + 'px', minHeight: lh * z + 'px' });
    p.style.setProperty('--mx', o.indent * z + 'px');
    if (o.list) p.dataset.m = o.list === 'bullet' ? '•' : `${i + 1}.`; else delete p.dataset.m;
    const u = $('.tu', p); if (u) u.style.top = shift * o.size * z + 'px';
  });
  el.title = o.link || '';
  drawDeco(o, el);
  decorateSel();
}

// Measure laid-out lines (box-local px, rotation removed): text, hard break, extent, baseline top, glyph rects.
function measure(o, el) {
  const tx = $('.tx', el), flow = $('.flow', tx), tf = el.style.transform;
  el.style.transform = 'none';
  const T = tx.getBoundingClientRect(), out = [], r = document.createRange();
  for (const p of flow.children) {
    const start = out.length;
    const walk = document.createTreeWalker(p, NodeFilter.SHOW_TEXT);
    let cur = null;
    for (let node; (node = walk.nextNode());) {
      const t = node.textContent;
      for (let i = 0; i < t.length; i++) {
        r.setStart(node, i); r.setEnd(node, i + 1);
        const rc = r.getClientRects()[0];
        if (!rc) { if (cur) cur.text += t[i]; continue; }
        if (!cur || rc.top > cur.top + T.top + rc.height * 0.5) { cur = { text: '', hard: false, top: rc.top - T.top, xs: rc.left - T.left, xe: rc.left - T.left, chars: [] }; out.push(cur); }
        cur.text += t[i];
        if (t[i].trim()) cur.xe = rc.right - T.left;
        cur.xeAll = rc.right - T.left;
        cur.chars.push({ c: t[i], x: rc.left - T.left, w: rc.width });
      }
    }
    if (out.length === start) out.push({ text: '', hard: true, top: 0, xs: 0, xe: 0, chars: [] });
    // A paragraph's last line keeps its typed trailing spaces, like the export.
    const last = out[out.length - 1]; last.hard = true;
    if (last.xeAll > last.xe) last.xe = last.xeAll;
  }
  el.style.transform = tf;
  return out;
}

function lineSvg(x0, x1, y, style, w, color) {
  if (x1 <= x0) return '';
  const dash = { dotted: [`0 ${2 * w}`, 'round'], dashed: [`${3 * w} ${2 * w}`, 'butt'], longdash: [`${7 * w} ${3 * w}`, 'butt'], dashdot: [`${5 * w} ${2 * w} 0 ${2 * w}`, 'round'] }[style];
  return (style === 'double' ? [y - w, y + w] : [y]).map(yy => {
    if (style === 'wavy') {
      const amp = Math.max(1, w * 1.2), L = Math.max(4, w * 5), n = Math.max(2, Math.floor((x1 - x0) / (L / 8)) + 1);
      const pts = Array.from({ length: n }, (_, i) => { const x = x0 + (x1 - x0) * i / (n - 1); return `${x},${yy + amp * Math.sin(2 * Math.PI * (x - x0) / L)}`; });
      return `<polyline points="${pts.join(' ')}" fill="none" stroke="${color}" stroke-width="${w}" stroke-linecap="round" stroke-linejoin="round"/>`;
    }
    return `<line x1="${x0}" y1="${yy}" x2="${x1}" y2="${yy}" stroke="${color}" stroke-width="${w}" ${dash ? `stroke-dasharray="${dash[0]}" stroke-linecap="${dash[1]}"` : ''}/>`;
  }).join('');
}

// Marker, underline/overline, border under the text; strike over it. Same geometry as server.draw_text_obj.
function drawDeco(o, el) {
  const z = S.zoom, F = FM(o.font, o.bold, o.italic), fs = o.size * (o.script ? 0.65 : 1), fp = fs * z;
  const lines = measure(o, el), tx = $('.tx', el);
  let under = '', over = '';
  const base = l => l.top + F.asc * fp;
  if (o.marker) lines.forEach(l => { if (l.xe > l.xs) under += `<rect x="${l.xs}" y="${base(l) - F.asc * fp}" width="${l.xe - l.xs}" height="${(F.asc - F.desc) * fp}" fill="${o.marker}" stroke="none"/>`; });
  for (const [spec, kind] of [[o.underline, 'u'], [o.overline, 'o'], [o.strike, 's']]) {
    if (!spec.on) continue;
    const w = decoW(spec, fs, F, kind) * z, col = spec.color || o.color;
    for (const l of lines) {
      if (l.xe <= l.xs) continue;
      const b = base(l);
      const y = kind === 'u' ? b + (spec.offset ?? fs * F.ul) * z + w / 2 : kind === 'o' ? b - F.asc * fp + w / 2 - (spec.offset || 0) * z : b - (spec.position ?? F.strike) * fp;
      let segs = [[l.xs, l.xe]];
      if (kind === 'u' && spec.skipInk) {
        const gaps = [], gsz = fs * (o.smallCaps ? 1 : 1);
        for (const c of l.chars) {
          if (!c.c.trim()) continue;
          const ch = o.case === 'upper' ? c.c.toUpperCase() : o.case === 'lower' ? c.c.toLowerCase() : c.c;
          const gs = o.smallCaps && ch !== ch.toUpperCase() ? fs * 0.7 : gsz, gch = gs !== gsz ? ch.toUpperCase() : ch;
          for (const [a, bb] of glyphInk(o, gch, (y - w / 2 - b) / (gs * z), (y + w / 2 - b) / (gs * z))) {
            const m = Math.max(w * 0.8, gs * z * 0.03);
            gaps.push([c.x + a * gs * z - m, c.x + bb * gs * z + m]);
          }
        }
        segs = []; let cur = l.xs;
        for (const [a, bb] of gaps.sort((p, q) => p[0] - q[0])) { if (a > cur) segs.push([cur, Math.min(a, l.xe)]); cur = Math.max(cur, bb); }
        if (l.xe > cur) segs.push([cur, l.xe]);
        segs = segs.filter(([a, bb]) => bb - a > w * 0.6);
      }
      const svg = segs.map(([a, bb]) => lineSvg(a, Math.min(bb, l.xe), y, spec.style, w, col)).join('');
      if (kind === 's') over += svg; else under += svg;
    }
  }
  const bd = o.border;
  if (bd.color && bd.width) {
    const bw = bd.width * z, W = tx.offsetWidth, H = tx.offsetHeight, i = bw / 2;
    const dash = { dotted: `stroke-dasharray="0 ${2 * bw}" stroke-linecap="round"`, dashed: `stroke-dasharray="${3 * bw} ${2 * bw}"` }[bd.style] || '';
    if (bd.sides === 'trbl') under += `<rect x="${i}" y="${i}" width="${W - bw}" height="${H - bw}" rx="${o.radius * z}" fill="none" stroke="${bd.color}" stroke-width="${bw}" ${dash}/>`;
    else under += [...bd.sides].map(k => { const [a, b] = { t: [[0, i], [W, i]], b: [[0, H - i], [W, H - i]], l: [[i, 0], [i, H]], r: [[W - i, 0], [W - i, H]] }[k]; return `<line x1="${a[0]}" y1="${a[1]}" x2="${b[0]}" y2="${b[1]}" stroke="${bd.color}" stroke-width="${bw}" ${dash}/>`; }).join('');
  }
  $('.dku', tx).innerHTML = under; $('.dko', tx).innerHTML = over;
}

// Grow a text box to fit its content while typing.
function fitText(o) {
  const el = objEl(o.id), tx = el && $('.tx', el); if (!tx || o.fit === 'fixed') return;
  const flow = $('.flow', tx), z = S.zoom;
  if (o.fit === 'hug') {
    flow.style.width = 'max-content';
    const w = (flow.scrollWidth + 2 * o.padding * z + 2) / z;
    flow.style.width = '';
    if (Math.abs(w - o.w) > 0.5) { o.w = w; el.style.width = o.w * z + 'px'; }
  }
  const need = (flow.scrollHeight + 2 * o.padding * z) / z;
  if (o.fit === 'hug' ? Math.abs(need - o.h) > 0.5 : need > o.h + 0.5) { o.h = need; el.style.height = o.h * z + 'px'; }
  drawDeco(o, el);
}

function decorateSel() {
  $$('.obj.selected').forEach(e => { if (e.dataset.id !== S.sel) { e.classList.remove('selected'); $('.selbox', e)?.remove(); } });
  const o = S.sel && objById(S.sel), el = o && objEl(o.id);
  if (!el) return;
  el.classList.add('selected');
  if ($('.selbox', el)) return;
  const box = h('div', 'selbox');
  if (o.inline) { el.append(box); return; }
  if (o.type === 'line' || o.type === 'arrow') {
    o.pts.forEach((_, i) => { const k = h('i', 'hd end'); k.dataset.end = i; box.append(k); });
    placeEnds(o, box);
  } else {
    ['nw', 'n', 'ne', 'e', 'se', 's', 'sw', 'w'].forEach(d => { const k = h('i', 'hd ' + d); k.dataset.h = d; box.append(k); });
    if (o.type !== 'note') { const r = h('i', 'hd rot'); r.dataset.h = 'rot'; box.append(r); }
  }
  el.append(box);
}
function placeEnds(o, box) {
  $$('.end', box).forEach(k => { const [px, py] = o.pts[k.dataset.end]; Object.assign(k.style, { left: px * 100 + '%', top: py * 100 + '%' }); });
}

function select(id) {
  if (S.editObj && S.editObj !== id) endTextEdit();
  S.sel = id; decorateSel(); renderBar();
}

function editText(id) {
  const o = objById(id), el = objEl(id); if (!o || !el) return;
  S.editObj = id; S.editBefore = snap();
  el.classList.add('editing');
  const flow = $('.flow', el);
  flow.contentEditable = 'plaintext-only';
  flow.focus();
  setCaret(flow, o.text.length);
}
function endTextEdit() {
  const id = S.editObj; if (!id) return;
  S.editObj = null;
  const o = objById(id), el = objEl(id);
  if (el) { el.classList.remove('editing'); $('.flow', el).contentEditable = 'false'; }
  if (o && !o.text.trim()) { S.objs = S.objs.filter(x => x !== o); el?.remove(); if (S.sel === id) S.sel = null; }
  else if (o) paintText(o, el);
  record(S.editBefore);
  renderBar();
}
async function finishEdits(cancel) { endTextEdit(); await commitSpan(cancel); }

pagesBox.addEventListener('input', e => {
  const el = e.target.closest('.obj'); if (!el || S.editObj !== el.dataset.id) return;
  const o = objById(el.dataset.id), flow = $('.flow', el);
  o.text = flowText(flow);
  if (!flowClean(flow)) { const c = getCaret(flow); setParas(flow, o.text); if (c !== null) setCaret(flow, c); }
  paintText(o, el); fitText(o); positionBar();
});
flowPaste();
function flowPaste() {
  pagesBox.addEventListener('paste', e => {
    if (!e.target.closest('.obj.editing')) return;
    e.preventDefault();
    document.execCommand('insertText', false, e.clipboardData.getData('text/plain'));
  });
}

function mutate(o, patch, tag) {
  const before = snap();
  for (const [k, v] of Object.entries(patch)) o[k] = v && typeof v === 'object' && !Array.isArray(v) ? { ...o[k], ...v } : v;
  renderObj(o);
  if (o.type === 'text') fitText(o);
  record(before, tag && o.id + ':' + tag);
  positionBar();
}
function removeObj(o) { const b = snap(); S.objs = S.objs.filter(x => x !== o); objEl(o.id)?.remove(); S.sel = null; S.editObj = null; record(b); renderBar(); }
function reorder(o, front) { const b = snap(); S.objs = S.objs.filter(x => x !== o); front ? S.objs.push(o) : S.objs.unshift(o); renderAllObjs(); record(b); }
function duplicate(o) {
  const b = snap(); const c = newObj(o.type, o.page, { ...clone(o), x: o.x + 12, y: o.y + 12 }); delete c.lines;
  record(b); select(c.id);
}

// ---------- property bar ----------
const IC = {
  bold: '<b>B</b>', italic: '<i style="font-family:Georgia,serif">I</i>', under: '<u>U</u>', strike: '<s>S</s>',
  left: '<svg viewBox="0 0 24 24"><path d="M4 6h16M4 10h10M4 14h16M4 18h10"/></svg>',
  center: '<svg viewBox="0 0 24 24"><path d="M4 6h16M7 10h10M4 14h16M7 18h10"/></svg>',
  right: '<svg viewBox="0 0 24 24"><path d="M4 6h16M10 10h10M4 14h16M10 18h10"/></svg>',
  justify: '<svg viewBox="0 0 24 24"><path d="M4 6h16M4 10h16M4 14h16M4 18h16"/></svg>',
  more: '<svg viewBox="0 0 24 24"><path d="M4 7h10M18 7h2M4 17h4M12 17h8"/><circle cx="16" cy="7" r="2"/><circle cx="10" cy="17" r="2"/></svg>',
  deco: '<svg viewBox="0 0 24 24"><path d="M7 4v6a5 5 0 0010 0V4M5 20h14"/></svg>',
  front: '<svg viewBox="0 0 24 24"><rect x="9" y="9" width="11" height="11" rx="2" fill="currentColor" stroke="none" opacity=".9"/><path d="M15 5H6a2 2 0 00-2 2v8"/></svg>',
  back: '<svg viewBox="0 0 24 24"><rect x="4" y="4" width="11" height="11" rx="2" fill="currentColor" stroke="none" opacity=".35"/><path d="M9 19h9a2 2 0 002-2V8"/></svg>',
  marker: '<svg viewBox="0 0 24 24"><path d="M14 4.5l5.5 5.5-7.5 7.5H7.5L5 20l-.5-3.5 2.5-2.5z"/></svg>',
  para: '<svg viewBox="0 0 24 24"><path d="M13 4v16M17 4v16M19 4h-8.5a4 4 0 000 8H13"/></svg>',
  fx: '<svg viewBox="0 0 24 24"><path d="M12 3l1.8 4.7L18.5 9l-4.7 1.8L12 15.5l-1.8-4.7L5.5 9l4.7-1.3zM18 15l.9 2.1L21 18l-2.1.9L18 21l-.9-2.1L15 18l2.1-.9z"/></svg>',
  box: '<svg viewBox="0 0 24 24"><rect x="4" y="4" width="16" height="16" rx="4"/><path d="M8 9h8M8 13h5"/></svg>',
  brush: '<svg viewBox="0 0 24 24"><path d="M4 4h12v5H4zM16 6.5h3v5h-8v3M10 14.5h2V20h-2z"/></svg>',
  reset: '<svg viewBox="0 0 24 24"><path d="M4 7h10M9 7v12M14 15l6 6M20 15l-6 6"/></svg>',
  textA: '<span class="ta">A</span>', fill: '<svg viewBox="0 0 24 24"><path d="M6 13l6-8 6 8a6 6 0 01-12 0z"/></svg>',
  stroke: '<svg viewBox="0 0 24 24"><rect x="4" y="4" width="16" height="16" rx="3"/></svg>',
};
const sep = () => h('span', 'vsep');
const btn = (html, tip, fn, cls = '') => { const b = h('button', 'tg ' + cls, { type: 'button', innerHTML: html }); b.dataset.tip = tip; b.onclick = fn; return b; };
function field(label, ctl) { const w = h('label', 'field'); w.append(h('span', '', { textContent: label }), ctl); return w; }

// Previewed fonts repaint without touching history; the commit records one undo step from before the first preview.
function fontLive(o) {
  let before = null;
  return (v, ph) => {
    if (ph === 'preview' && before === null) before = snap();
    o.font = v; renderObj(o); fitText(o); positionBar();
    if (ph === 'commit') { record(before ?? snap()); before = null; }
    if (ph === 'revert') before = null;
  };
}

// One builder serves a selected object, the current tool's defaults, and lifted PDF text.
const UI_CHEV = '<svg class="cv" viewBox="0 0 24 24"><path d="M7 10l5 5 5-5"/></svg>';
const LSTYLES = [['solid', 'M3 12h18'], ['double', 'M3 9h18M3 15h18'], ['dotted', 'M4 12h.01M8 12h.01M12 12h.01M16 12h.01M20 12h.01'],
  ['dashed', 'M3 12h4M10 12h4M17 12h4'], ['longdash', 'M3 12h8M14 12h7'], ['dashdot', 'M3 12h6M12 12h.01M15 12h6'], ['wavy', 'M3 12c1.5-3 3-3 4.5 0s3 3 4.5 0 3-3 4.5 0 3 3 4.5 0']];
const lineSeg = (v, on, kinds = LSTYLES) => UI.seg(kinds.map(([k, d]) => ({ v: k, html: `<svg viewBox="0 0 24 24"><path d="${d}"/></svg>`, title: k })), v, on, 'lines');
const pt = (v, on, o = {}) => UI.stepper(+(+v).toFixed(2), { min: -50, max: 200, step: 0.5, suffix: ' pt', ...o }, on);
const sub = t => h('div', 'ptitle', { textContent: t });
// A popover whose body re-renders in place (tabs, toggles that reveal fields).
function panel(icon, tip, body, cls = '') {
  const b = btn(icon + UI_CHEV, tip, () => {}, 'wide ' + cls);
  UI.pop(b, p => { p.classList.add('form'); const draw = () => { p.replaceChildren(); body(p, draw); }; draw(); });
  b._onClose = () => renderBar();
  return b;
}

function textControls(o, set, live) {
  const bits = [], fs = () => o.size * (o.script ? 0.65 : 1), F = () => FM(o.font, o.bold, o.italic);
  bits.push(UI.fontPicker(S.fonts, o.font, live ? fontLive(o) : (v, ph) => ph === 'preview' ? (o.font = v) : set({ font: v })));
  bits.push(UI.stepper(o.size, { min: 4, max: 300, step: 1, title: 'Size (⌘[ ⌘])' }, v => set({ size: v }, 'size')));
  bits.push(sep());
  bits.push(UI.toggle(IC.bold, 'Bold (⌘B)', o.bold, v => set({ bold: v })));
  bits.push(UI.toggle(IC.italic, 'Italic (⌘I)', o.italic, v => set({ italic: v })));
  bits.push(UI.toggle(IC.under, 'Underline (⌘U)', o.underline?.on, v => set({ underline: { on: v } })));
  bits.push(UI.toggle(IC.strike, 'Strikethrough (⌘⇧X)', o.strike?.on, v => set({ strike: { on: v } })));
  let tab = 'underline';
  bits.push(panel(IC.deco, 'Underline, strikethrough & overline styles', (p, redraw) => {
    p.append(UI.seg([{ v: 'underline', html: 'Underline' }, { v: 'strike', html: 'Strike' }, { v: 'overline', html: 'Overline' }], tab, v => { tab = v; redraw(); }, 'tabs'));
    const spec = o[tab] || {}, kind = { underline: 'u', strike: 's', overline: 'o' }[tab];
    const put = (patch, tag) => set({ [tab]: { ...patch } }, tag);
    p.append(
      field('Show', UI.toggle(spec.on ? 'On' : 'Off', 'Show ' + tab, spec.on, v => { put({ on: v }); redraw(); })),
      field('Style', lineSeg(spec.style || 'solid', v => put({ style: v, on: true }))),
      field('Colour', UI.swatch({ value: spec.color, auto: 'Same as text', label: tab + ' colour', onChange: v => put({ color: v, on: true }) })),
      field('Thickness', pt(decoW(spec, fs(), F(), kind), v => put({ width: v, on: true }), { min: 0.25, max: 12, step: 0.25, title: 'Thickness' })));
    if (tab === 'strike') p.append(field('Height above baseline', pt(+((spec.position ?? F().strike) * fs()).toFixed(2), v => put({ position: v / fs(), on: true }), { min: -20, max: 60, step: 0.25 })));
    else p.append(field(tab === 'underline' ? 'Offset below baseline' : 'Offset above text', pt(spec.offset ?? (tab === 'underline' ? +(fs() * F().ul).toFixed(2) : 0), v => put({ offset: v, on: true }), { min: -20, max: 40, step: 0.25 })));
    if (tab === 'underline') p.append(field('Skip descenders', UI.toggle(spec.skipInk ? 'On' : 'Off', 'Gap the line where descenders cross it', !!spec.skipInk, v => { put({ skipInk: v }); redraw(); })));
  }));
  bits.push(sep());
  bits.push(UI.swatch({ value: o.color, label: 'Text colour', icon: IC.textA, onChange: v => set({ color: v }) }));
  bits.push(UI.swatch({ value: o.marker, allowNone: true, label: 'Highlight (marker)', icon: IC.marker, onChange: v => set({ marker: v }) }));
  bits.push(sep());
  bits.push(UI.seg(['left', 'center', 'right', 'justify'].map(v => ({ v, html: IC[v], title: 'Align ' + v })), o.align, v => set({ align: v })));
  bits.push(panel(IC.para, 'Paragraph: spacing, indents, lists, vertical align', p => p.append(
    sub('Spacing'),
    field('Line height', UI.stepper(o.lineHeight, { min: 0.6, max: 4, step: 0.05, suffix: '×' }, v => set({ lineHeight: v }, 'lh'))),
    field('Letter spacing', pt(o.spacing, v => set({ spacing: v }, 'sp'), { min: -5, max: 40, step: 0.25 })),
    field('Word spacing', pt(o.wordSpacing, v => set({ wordSpacing: v }, 'ws'), { min: -5, max: 60, step: 0.5 })),
    field('Space before', pt(o.paraBefore, v => set({ paraBefore: v }, 'pb'), { min: 0, max: 100 })),
    field('Space after', pt(o.paraAfter, v => set({ paraAfter: v }, 'pa'), { min: 0, max: 100 })),
    sub('Indent & lists'),
    field('Left indent', pt(o.indent, v => set({ indent: v }, 'ind'), { min: 0, max: 200, step: 1 })),
    field('First line', pt(o.firstIndent, v => set({ firstIndent: v }, 'fi'), { min: -100, max: 200, step: 1 })),
    field('List', UI.seg([{ v: null, html: 'None' }, { v: 'bullet', html: '• List' }, { v: 'number', html: '1. List' }], o.list, v => set({ list: v }))),
    field('Vertical align', UI.seg([{ v: 'top', html: 'Top' }, { v: 'middle', html: 'Middle' }, { v: 'bottom', html: 'Bottom' }], o.valign, v => set({ valign: v }))))));
  bits.push(panel(IC.fx, 'Effects: case, script, shadow, outline, opacity, rotation, link', (p, redraw) => p.append(
    field('Case', UI.seg([{ v: null, html: 'Aa', title: 'As typed' }, { v: 'upper', html: 'AA', title: 'UPPERCASE' }, { v: 'lower', html: 'aa', title: 'lowercase' }, { v: 'title', html: 'Tt', title: 'Title Case' }], o.case, v => set({ case: v }))),
    field('Small caps', UI.toggle('<span style="font-variant:small-caps">Sc</span>', 'Small caps', o.smallCaps, v => set({ smallCaps: v }))),
    field('Script', UI.seg([{ v: null, html: 'Abc', title: 'Normal' }, { v: 'sup', html: 'X<sup>2</sup>', title: 'Superscript' }, { v: 'sub', html: 'X<sub>2</sub>', title: 'Subscript' }], o.script || null, v => set({ script: v }))),
    sub('Text shadow'),
    field('Shadow', h('div', 'row', {}, )),
    sub('Outline'),
    field('Outline', h('div', 'row o2', {})),
    sub('Layout'),
    field('Opacity', UI.slider(o.opacity ?? 1, { min: 0.05, max: 1, step: 0.05, fmt: v => Math.round(v * 100) + '%' }, v => set({ opacity: v }, 'op'))),
    field('Rotation', UI.stepper(o.rot || 0, { min: -360, max: 360, step: 1, suffix: '°' }, v => set({ rot: v }, 'rot'))),
    field('Link', linkInput(o, set)),
  ) || (() => {
    const [r1, r2] = p.querySelectorAll('.row');
    r1.append(UI.toggle(o.shadow.on ? 'On' : 'Off', 'Text shadow', o.shadow.on, v => { set({ shadow: { on: v } }); redraw(); }),
      UI.swatch({ value: o.shadow.color, label: 'Shadow colour', onChange: v => set({ shadow: { color: v, on: true } }) }),
      mini('Blur', o.shadow.blur, v => set({ shadow: { blur: v, on: true } }, 'shb')), mini('X', o.shadow.x, v => set({ shadow: { x: v, on: true } }, 'shx')), mini('Y', o.shadow.y, v => set({ shadow: { y: v, on: true } }, 'shy')));
    r2.append(UI.toggle(o.outline.on ? 'On' : 'Off', 'Text outline', o.outline.on, v => { set({ outline: { on: v } }); redraw(); }),
      UI.swatch({ value: o.outline.color, label: 'Outline colour', onChange: v => set({ outline: { color: v, on: true } }) }),
      mini('Width', o.outline.width, v => set({ outline: { width: v, on: true } }, 'olw'), 0.25));
  })()));
  bits.push(panel(IC.box, 'Box: fill, border, corners, padding, shadow, sizing', (p, redraw) => {
    const bd = o.border, bs = o.boxShadow;
    const sides = h('div', 'seg');
    ['t', 'r', 'b', 'l'].forEach(k => { const b = h('button', bd.sides.includes(k) ? 'on' : '', { type: 'button', textContent: k.toUpperCase(), title: { t: 'Top', r: 'Right', b: 'Bottom', l: 'Left' }[k] });
      b.onclick = () => { const s2 = bd.sides.includes(k) ? bd.sides.replace(k, '') : 'trbl'.split('').filter(x => x === k || bd.sides.includes(x)).join(''); set({ border: { sides: s2 } }); redraw(); }; sides.append(b); });
    const shRow = h('div', 'row');
    shRow.append(UI.toggle(bs.on ? 'On' : 'Off', 'Box shadow', bs.on, v => { set({ boxShadow: { on: v } }); redraw(); }),
      UI.swatch({ value: bs.color, label: 'Shadow colour', onChange: v => set({ boxShadow: { color: v, on: true } }) }),
      mini('Blur', bs.blur, v => set({ boxShadow: { blur: v, on: true } }, 'bsb')), mini('Y', bs.y, v => set({ boxShadow: { y: v, on: true } }, 'bsy')));
    p.append(
      field('Fill', UI.swatch({ value: o.bg, allowNone: true, label: 'Box fill', icon: IC.fill, onChange: v => set({ bg: v }) })),
      sub('Border'),
      field('Colour', UI.swatch({ value: bd.color, allowNone: true, label: 'Border colour', icon: IC.stroke, onChange: v => set({ border: { color: v } }) })),
      field('Style', lineSeg(bd.style, v => set({ border: { style: v, color: bd.color || '#111827' } }), LSTYLES.filter(([k]) => /solid|dashed|dotted/.test(k)))),
      field('Width', pt(bd.width, v => set({ border: { width: v, color: bd.color || '#111827' } }, 'bw'), { min: 0.25, max: 20, step: 0.25 })),
      field('Sides', sides),
      sub('Shape'),
      field('Corner radius', pt(o.radius, v => set({ radius: v }, 'rad'), { min: 0, max: 100, step: 1 })),
      field('Padding', pt(o.padding, v => set({ padding: v }, 'pad'), { min: 0, max: 100, step: 1 })),
      field('Shadow', shRow),
      field('Sizing', UI.seg([{ v: 'grow', html: 'Auto height' }, { v: 'fixed', html: 'Fixed' }, { v: 'hug', html: 'Hug text' }], o.fit, v => set({ fit: v }))));
  }));
  if (live) bits.push(sep(), btn(IC.brush, 'Format painter: copy this style, then click another text box', () => startPainter(o), S.painter ? 'on' : ''),
    btn(IC.reset, 'Reset formatting', () => { const d = TEXT_DEFAULTS(); delete d.rot; set(d); renderBar(); }));
  return bits;
}
function mini(label, v, on, step = 1) { const w = h('span', 'mini'); w.append(h('span', '', { textContent: label }), UI.stepper(v, { min: -100, max: 200, step }, on)); return w; }
function linkInput(o, set) {
  const i = h('input', 'linkin', { value: o.link || '', placeholder: 'https://', spellcheck: false });
  i.onchange = () => { let v = i.value.trim(); if (v && !/^[a-z]+:/i.test(v)) v = 'https://' + v; set({ link: v }); };
  return i;
}
function startPainter(o) {
  S.painter = Object.fromEntries(STYLE_KEYS.filter(k => k !== 'rot').map(k => [k, clone(o[k])]));
  document.body.classList.add('painting'); renderBar();
  toast('Style copied — click another text box to apply it (Esc to cancel)');
}
function stopPainter() { S.painter = null; document.body.classList.remove('painting'); }

function shapeControls(o, set, isTool) {
  const bits = [];
  if (isTool) bits.push(UI.seg([
    { v: 'rect', html: '<svg viewBox="0 0 24 24"><rect x="4" y="6" width="16" height="12" rx="1.5"/></svg>', title: 'Rectangle' },
    { v: 'ellipse', html: '<svg viewBox="0 0 24 24"><ellipse cx="12" cy="12" rx="8.5" ry="6.5"/></svg>', title: 'Ellipse' },
    { v: 'line', html: '<svg viewBox="0 0 24 24"><path d="M5 19L19 5"/></svg>', title: 'Line' },
    { v: 'arrow', html: '<svg viewBox="0 0 24 24"><path d="M5 19L19 5M10 5h9v9"/></svg>', title: 'Arrow' }], o.kind, v => set({ kind: v })), sep());
  bits.push(UI.swatch({ value: o.stroke, allowNone: !/line|arrow/.test(o.kind || o.type), label: 'Stroke', icon: IC.stroke, onChange: v => set({ stroke: v }) }));
  if (!/line|arrow/.test(o.kind || o.type)) bits.push(UI.swatch({ value: o.fill, allowNone: true, label: 'Fill', icon: IC.fill, onChange: v => set({ fill: v }) }));
  bits.push(UI.stepper(o.width, { min: 0.5, max: 40, step: 0.5, title: 'Stroke width', suffix: ' pt' }, v => set({ width: v }, 'w')));
  bits.push(sep(), h('span', 'lbl', { textContent: 'Opacity' }), UI.slider(o.opacity ?? 1, { min: 0.05, max: 1, step: 0.05, fmt: v => Math.round(v * 100) + '%' }, v => set({ opacity: v }, 'op')));
  return bits;
}

function renderBar() {
  UI.closePop();
  pbar.replaceChildren();
  const o = S.sel && objById(S.sel), e = S.editing, d = S.defaults;
  let bits = [], title = '';
  if (e) {
    title = 'Text';
    const lift = patch => liftSpan(patch);
    const extras = (e.alts || []).map((a, i) => ({ label: (i ? '' : '★ ') + a.label, pick: () => { showMatch(e.el, e.sp, i); e.family = ''; e.el.focus(); } }));
    if (!extras.length) extras.push({ label: 'Original font', on: !e.family, pick: () => { e.family = ''; e.el.style.fontFamily = ''; } });
    const picked = e.family?.startsWith('f:') ? e.family.slice(2) : null;
    bits.push(UI.fontPicker(S.fonts, picked, v => spanProp('family', v ? 'f:' + v : ''), extras));
    bits.push(UI.stepper(+e.size.toFixed(1), { min: 4, max: 300, step: 0.5, title: 'Size' }, v => spanProp('size', v)));
    bits.push(sep(), UI.toggle(IC.bold, 'Bold', e.bold, v => spanProp('bold', v)), UI.toggle(IC.italic, 'Italic', e.italic, v => spanProp('italic', v)),
      UI.toggle(IC.under, 'Underline', false, () => lift({ underline: { on: true, color: null, width: 1 } })),
      UI.toggle(IC.strike, 'Strikethrough', false, () => lift({ strike: { on: true, color: null, width: 0 } })), sep(),
      UI.swatch({ value: e.color, label: 'Text colour', icon: IC.textA, onChange: v => spanProp('color', v) }),
      UI.swatch({ value: null, allowNone: true, label: 'Highlight (marker)', icon: IC.marker, onChange: v => v && lift({ marker: v }) }), sep(),
      UI.seg(['left', 'center', 'right', 'justify'].map(v => ({ v, html: IC[v], title: 'Align ' + v })), null, v => lift({ align: v })),
      btn(IC.more + '<span class="t">More styles</span>', 'Turn into a text box for spacing, case, border, rotation…', () => lift({}), 'wide'));
  } else if (o) {
    const set = (patch, tag) => mutate(o, patch, tag);
    title = { text: 'Text', rect: 'Rectangle', ellipse: 'Ellipse', line: 'Line', arrow: 'Arrow', ink: 'Drawing', highlight: 'Highlight',
      whiteout: 'Eraser', image: 'Image', note: 'Note', check: 'Check', cross: 'Cross' }[o.type];
    if (o.type === 'text') bits = textControls(o, set, true);
    else if (/rect|ellipse|line|arrow/.test(o.type)) bits = shapeControls(o, set);
    else if (o.type === 'ink' || o.type === 'check' || o.type === 'cross') bits = [UI.swatch({ value: o.stroke, label: 'Colour', icon: IC.stroke, onChange: v => set({ stroke: v }) }),
      UI.stepper(o.width, { min: 0.5, max: 30, step: 0.5, title: 'Width', suffix: ' pt' }, v => set({ width: v }, 'w')),
      sep(), UI.slider(o.opacity ?? 1, { min: 0.05, max: 1, step: 0.05, fmt: v => Math.round(v * 100) + '%' }, v => set({ opacity: v }, 'op'))];
    else if (o.type === 'highlight') bits = [UI.swatch({ value: o.fill, label: 'Colour', onChange: v => set({ fill: v }) }),
      UI.slider(o.opacity, { min: 0.1, max: 1, step: 0.05, fmt: v => Math.round(v * 100) + '%' }, v => set({ opacity: v }, 'op'))];
    else if (o.type === 'whiteout') bits = [UI.swatch({ value: o.fill, label: 'Cover colour', onChange: v => set({ fill: v }) })];
    else if (o.type === 'image') bits = [btn(ICO.rotr, 'Rotate 90°', () => set({ rot: ((o.rot || 0) + 90) % 360 }))];
    else if (o.type === 'note') {
      const ta = h('textarea', 'notetext', { value: o.text || '', placeholder: 'Write a note…', rows: 2 });
      ta.oninput = () => set({ text: ta.value }, 'nt');
      bits = [UI.swatch({ value: o.fill, label: 'Colour', onChange: v => set({ fill: v }) }), ta];
      setTimeout(() => { if (!o.text) ta.focus(); });
    }
    bits.push(sep(), btn(IC.front, 'Bring to front', () => reorder(o, true)), btn(IC.back, 'Send to back', () => reorder(o, false)),
      btn(ICO.dup, 'Duplicate (⌘D)', () => duplicate(o)), btn(ICO.del, 'Delete (⌫)', () => removeObj(o), 'danger'));
  } else {
    const t = S.tool, setD = k => (patch) => { for (const [a, v] of Object.entries(patch)) d[k][a] = v && typeof v === 'object' && !Array.isArray(v) ? { ...d[k][a], ...v } : v; if ('kind' in patch) renderBar(); };
    if (t === 'text') { title = 'New text'; bits = textControls(d.text, setD('text')); }
    else if (t === 'shape') { title = 'New shape'; bits = shapeControls({ ...d.shape }, setD('shape'), true); }
    else if (t === 'ink') { title = 'Pen'; bits = [UI.swatch({ value: d.ink.stroke, label: 'Colour', icon: IC.stroke, onChange: v => setD('ink')({ stroke: v }) }),
      UI.stepper(d.ink.width, { min: 0.5, max: 30, step: 0.5, title: 'Width', suffix: ' pt' }, v => setD('ink')({ width: v })),
      UI.slider(d.ink.opacity, { min: 0.05, max: 1, step: 0.05, fmt: v => Math.round(v * 100) + '%' }, v => { d.ink.opacity = v; })]; }
    else if (t === 'highlight') { title = 'Highlighter'; bits = [UI.swatch({ value: d.highlight.fill, label: 'Colour', onChange: v => setD('highlight')({ fill: v }) })]; }
    else if (t === 'whiteout') { title = 'Eraser'; bits = [UI.swatch({ value: d.whiteout.fill, label: 'Cover colour', onChange: v => setD('whiteout')({ fill: v }) }), h('span', 'lbl', { textContent: 'Drag over anything to remove it' })]; }
    else if (t === 'stamp') { title = 'Mark'; bits = [UI.seg([{ v: 'check', html: '<svg viewBox="0 0 24 24"><path d="M5 12.5l4 4 10-10"/></svg>', title: 'Check' },
      { v: 'cross', html: '<svg viewBox="0 0 24 24"><path d="M6 6l12 12M18 6L6 18"/></svg>', title: 'Cross' }], d.stamp.kind, v => setD('stamp')({ kind: v })),
      UI.swatch({ value: d.stamp.stroke, label: 'Colour', icon: IC.stroke, onChange: v => setD('stamp')({ stroke: v }) })]; }
    else if (t === 'note') { title = 'Note'; bits = [UI.swatch({ value: d.note.fill, label: 'Colour', onChange: v => setD('note')({ fill: v }) }), h('span', 'lbl', { textContent: 'Click to pin a note' })]; }
    else if (t === 'image' || t === 'sign') { title = S.img ? 'Place' : ''; if (S.img) bits = [h('span', 'lbl', { textContent: 'Click or drag on the page to place it' })]; }
  }
  if (!bits.length) { pbar.hidden = true; return; }
  if (title) pbar.append(h('span', 'ptag', { textContent: title }));
  pbar.append(...bits);
  pbar.hidden = false;
  positionBar();
}

// Float the bar above the selection; dock it under the toolbar otherwise.
function positionBar() {
  if (pbar.hidden) return;
  if (isMobile()) { pbar.style.left = pbar.style.top = ''; return; }
  const el = (S.sel && objEl(S.sel)) || S.editing?.el, vr = viewer.getBoundingClientRect();
  const bw = pbar.offsetWidth, bh = pbar.offsetHeight;
  let x = vr.left + vr.width / 2 - bw / 2, y = vr.top + 12;
  if (el) {
    const r = el.getBoundingClientRect();
    x = r.left + r.width / 2 - bw / 2;
    y = r.top - bh - 14;
    if (y < vr.top + 8) y = r.bottom + 14;
    if (y + bh > innerHeight - 8) y = vr.top + 12;
  }
  pbar.style.left = Math.max(8, Math.min(innerWidth - bw - 8, x)) + 'px';
  pbar.style.top = y + 'px';
  pbar.classList.toggle('docked', !el);
}
viewer.addEventListener('scroll', () => { positionBar(); UI.closePop(); }, { passive: true });
addEventListener('resize', positionBar);
pbar.addEventListener('pointerdown', e => { if (!e.target.closest('input, textarea')) e.preventDefault(); });

// ---------- pointer: create, select, move, resize, rotate ----------
const CREATE = { text: 1, shape: 1, ink: 1, highlight: 1, whiteout: 1, image: 1, sign: 1, note: 1, stamp: 1 };
document.addEventListener('pointerdown', e => {
  if (e.target.closest('#pbar, .pop, dialog')) return;
  if (S.editing && !S.editing.el.contains(e.target)) { commitSpan(); if (e.target.closest('.page')) e.preventDefault(); }
  if (S.editObj && !objEl(S.editObj)?.contains(e.target)) endTextEdit();
}, true);

pagesBox.addEventListener('pointerdown', e => {
  const pg = e.target.closest('.page');
  if (!pg || e.button !== 0 || e.target.closest('.span.editing') || e.target.closest('.obj.editing .flow')) return;
  const n = +pg.dataset.n, box = pg.getBoundingClientRect(), z = S.zoom;
  const pt = ev => [(ev.clientX - box.left) / z, (ev.clientY - box.top) / z];
  const hd = e.target.closest('.hd'), oel = e.target.closest('.obj');
  const tool = S.tool;
  if (hd) return handleDrag(e, objById(hd.closest('.obj').dataset.id), hd, pt);
  if (oel && (tool === 'select' || (tool === 'text' && oel.classList.contains('text')))) {
    const o = objById(oel.dataset.id);
    e.preventDefault();
    if (S.painter && o.type === 'text') { mutate(o, clone(S.painter)); stopPainter(); select(o.id); return; }
    const wasSel = S.sel === o.id;
    select(o.id);
    longPress(e, o);
    const edit = () => { if (o.type === 'text') { editText(o.id); caretAt(e.clientX, e.clientY); } };
    if (o.type === 'text' && (tool === 'text' || e.detail > 1)) return edit();
    return moveDrag(e, o, pg, pt, wasSel ? edit : null);
  }
  if (tool === 'select') {
    const d = e.target.closest('.span');
    select(null);
    if (d) { e.preventDefault(); d._sp.ocr ? startScannedEdit(d, e) : startEdit(d, e); }
    return;
  }
  if (!CREATE[tool]) return;
  e.preventDefault();
  select(null);
  const start = pt(e), d = S.defaults;
  if ((tool === 'image' || tool === 'sign') && !S.img) return tool === 'image' ? $('#imgfile').click() : openSign();
  if (tool === 'note') { const o = newObj('note', n, { x: start[0] - 11, y: start[1] - 11, w: 22, h: 22, text: '', fill: d.note.fill }); record(JSON.stringify(S.objs.slice(0, -1))); setTool('select'); return select(o.id); }
  if (tool === 'stamp') { const k = d.stamp.kind, o = newObj(k, n, { x: start[0] - 12, y: start[1] - 12, w: 24, h: 24, paths: STAMPS[k], stroke: d.stamp.stroke, width: d.stamp.width }); record(JSON.stringify(S.objs.slice(0, -1))); return select(o.id); }
  pg.setPointerCapture(e.pointerId);
  const svg = $('.draw', pg);
  let move, done;
  if (tool === 'ink') {
    const pts = [start];
    const line = document.createElementNS('http://www.w3.org/2000/svg', 'polyline');
    line.setAttribute('style', `stroke:${d.ink.stroke};stroke-width:${d.ink.width * z};fill:none;stroke-linecap:round;stroke-linejoin:round;opacity:${d.ink.opacity}`);
    svg.append(line);
    move = ev => { pts.push(pt(ev)); line.setAttribute('points', pts.map(p => p.map(v => v * z).join(',')).join(' ')); };
    done = () => {
      line.remove(); if (pts.length < 2) return;
      const xs = pts.map(p => p[0]), ys = pts.map(p => p[1]);
      const x = Math.min(...xs), y = Math.min(...ys), w = Math.max(1, Math.max(...xs) - x), hh = Math.max(1, Math.max(...ys) - y);
      const b = snap();
      newObj('ink', n, { x, y, w, h: hh, paths: [pts.map(([px, py]) => [(px - x) / w, (py - y) / hh])], stroke: d.ink.stroke, width: d.ink.width, opacity: d.ink.opacity });
      record(b);
    };
  } else {
    const kind = tool === 'shape' ? d.shape.kind : tool;
    const lineish = kind === 'line' || kind === 'arrow';
    const band = h('div', 'band ' + kind);
    const ghost = document.createElementNS('http://www.w3.org/2000/svg', 'line');
    if (lineish) { ghost.setAttribute('style', `stroke:${d.shape.stroke};stroke-width:${d.shape.width * z}`); svg.append(ghost); } else pg.append(band);
    let end = start;
    move = ev => {
      end = pt(ev);
      if (ev.shiftKey && !lineish) { const s = Math.max(Math.abs(end[0] - start[0]), Math.abs(end[1] - start[1])); end = [start[0] + Math.sign(end[0] - start[0]) * s, start[1] + Math.sign(end[1] - start[1]) * s]; }
      if (lineish) { ghost.setAttribute('x1', start[0] * z); ghost.setAttribute('y1', start[1] * z); ghost.setAttribute('x2', end[0] * z); ghost.setAttribute('y2', end[1] * z); return; }
      const [x0, y0, x1, y1] = [Math.min(start[0], end[0]), Math.min(start[1], end[1]), Math.max(start[0], end[0]), Math.max(start[1], end[1])];
      Object.assign(band.style, { left: x0 * z + 'px', top: y0 * z + 'px', width: (x1 - x0) * z + 'px', height: (y1 - y0) * z + 'px' });
    };
    done = () => {
      band.remove(); ghost.remove();
      let [x0, y0, x1, y1] = [Math.min(start[0], end[0]), Math.min(start[1], end[1]), Math.max(start[0], end[0]), Math.max(start[1], end[1])];
      const tiny = x1 - x0 < 4 && y1 - y0 < 4;
      const b = snap();
      let o;
      if (tool === 'text') {
        const st = d.text, w = tiny ? 220 : Math.max(40, x1 - x0);
        o = newObj('text', n, { ...clone(st), text: '', x: start[0] - (tiny ? 4 : 0), y: start[1] - (tiny ? st.size * st.lineHeight / 2 + 4 : 0), w, h: st.size * st.lineHeight + 8 });
        setTool('select'); select(o.id); editText(o.id); return;
      }
      if (tool === 'image' || tool === 'sign') {
        const { w, h: hh } = S.img;
        const r = tiny ? [start[0] - w / 2, start[1] - hh / 2, w, hh] : [x0, y0, x1 - x0, y1 - y0];
        o = newObj('image', n, { x: r[0], y: r[1], w: r[2], h: r[3], data: S.img.data });
        S.img = null; record(b); setTool('select'); return select(o.id);
      }
      if (lineish) {
        if (tiny) [x1, y1] = [x0 + 120, y0];
        const x = Math.min(start[0], end[0]), y = Math.min(start[1], end[1]);
        const w = Math.max(1, Math.abs(end[0] - start[0])), hh = Math.max(1, Math.abs(end[1] - start[1]));
        const pts = tiny ? [[0, 0], [1, 0]] : [start, end].map(([px, py]) => [(px - x) / w, (py - y) / hh]);
        o = newObj(kind, n, { x, y, w: tiny ? 120 : w, h: tiny ? 1 : hh, pts, stroke: d.shape.stroke, width: d.shape.width, opacity: d.shape.opacity });
      } else {
        if (tiny && tool !== 'shape') return;
        if (tiny) [x1, y1] = [x0 + 120, y0 + 80];
        const props = { x: x0, y: y0, w: x1 - x0, h: y1 - y0 };
        if (tool === 'shape') o = newObj(kind, n, { ...props, stroke: d.shape.stroke, fill: d.shape.fill, width: d.shape.width, opacity: d.shape.opacity });
        else if (tool === 'highlight') o = newObj('highlight', n, { ...props, fill: d.highlight.fill, opacity: d.highlight.opacity });
        else o = newObj('whiteout', n, { ...props, fill: d.whiteout.fill });
      }
      record(b);
      if (tool === 'shape') { setTool('select'); select(o.id); }
    };
  }
  const up = () => { pg.removeEventListener('pointermove', move); done(); };
  pg.addEventListener('pointermove', move);
  pg.addEventListener('pointerup', up, { once: true });
});

pagesBox.addEventListener('dblclick', e => {
  const oel = e.target.closest('.obj.text');
  if (oel && S.editObj !== oel.dataset.id) { select(oel.dataset.id); editText(oel.dataset.id); caretAt(e.clientX, e.clientY); }
});

function moveDrag(e, o, pg, pt, onTap) {
  const b = snap(), start = pt(e), ox = o.x, oy = o.y, el = objEl(o.id);
  pg.setPointerCapture(e.pointerId);
  let moved = false;
  const move = ev => {
    const [x, y] = pt(ev);
    if (!moved && Math.hypot(x - start[0], y - start[1]) * S.zoom < 3) return;
    moved = true; el.classList.add('dragging');
    o.x = ox + x - start[0]; o.y = oy + y - start[1];
    el.style.left = o.x * S.zoom + 'px'; el.style.top = o.y * S.zoom + 'px'; positionBar();
  };
  pg.addEventListener('pointermove', move);
  pg.addEventListener('pointerup', () => {
    pg.removeEventListener('pointermove', move); el.classList.remove('dragging');
    if (moved) record(b); else if (onTap && !S.ctx) onTap();
  }, { once: true });
}

function handleDrag(e, o, hd, pt) {
  e.preventDefault(); e.stopPropagation();
  const pg = pageEl(o.page), b = snap(), z = S.zoom;
  pg.setPointerCapture(e.pointerId);
  const o0 = clone(o), cx = o.x + o.w / 2, cy = o.y + o.h / 2, rad = (o.rot || 0) * Math.PI / 180;
  const start = pt(e), kind = hd.dataset.h;
  const move = ev => {
    const [x, y] = pt(ev);
    if (hd.dataset.end !== undefined) {
      // endpoint drag: rebuild the line's box from its two absolute ends
      const abs = o0.pts.map(([px, py]) => [o0.x + px * o0.w, o0.y + py * o0.h]);
      abs[hd.dataset.end] = [x, y];
      const nx = Math.min(abs[0][0], abs[1][0]), ny = Math.min(abs[0][1], abs[1][1]);
      const w = Math.max(1, Math.abs(abs[0][0] - abs[1][0])), hh = Math.max(1, Math.abs(abs[0][1] - abs[1][1]));
      Object.assign(o, { x: nx, y: ny, w, h: hh, pts: abs.map(([px, py]) => [(px - nx) / w, (py - ny) / hh]) });
    } else if (kind === 'rot') {
      let a = Math.atan2(y - cy, x - cx) * 180 / Math.PI + 90;
      if (ev.shiftKey) a = Math.round(a / 15) * 15;
      o.rot = Math.round(((a % 360) + 360) % 360);
    } else {
      const dx = x - start[0], dy = y - start[1];
      const lx = dx * Math.cos(-rad) - dy * Math.sin(-rad), ly = dx * Math.sin(-rad) + dy * Math.cos(-rad);
      const sx = kind.includes('e') ? 1 : kind.includes('w') ? -1 : 0, sy = kind.includes('s') ? 1 : kind.includes('n') ? -1 : 0;
      let w = Math.max(6, o0.w + sx * lx), hh = Math.max(6, o0.h + sy * ly);
      if (ev.shiftKey && sx && sy || o.type === 'image' && sx && sy && !ev.altKey) { const k = Math.max(w / o0.w, hh / o0.h); w = o0.w * k; hh = o0.h * k; }
      const ccx = sx * (w - o0.w) / 2, ccy = sy * (hh - o0.h) / 2;
      const wx = ccx * Math.cos(rad) - ccy * Math.sin(rad), wy = ccx * Math.sin(rad) + ccy * Math.cos(rad);
      Object.assign(o, { w, h: hh, x: cx + wx - w / 2, y: cy + wy - hh / 2 });
    }
    renderObj(o);
    const sb = $('.selbox', objEl(o.id)); if (sb && o.pts) placeEnds(o, sb);
    if (o.type === 'text') fitText(o);
    positionBar();
  };
  pg.addEventListener('pointermove', move);
  pg.addEventListener('pointerup', () => { pg.removeEventListener('pointermove', move); record(b); }, { once: true });
}

pagesBox.addEventListener('keydown', e => {
  if (S.editing && e.target.classList.contains('editing')) {
    if (e.key === 'Escape') { e.preventDefault(); commitSpan(true); }
    else if (e.key === 'Enter') { e.preventDefault(); commitSpan(); }
  }
  if (S.editObj && e.key === 'Escape') { e.preventDefault(); endTextEdit(); }
  if (S.editObj && e.key === 'Enter' && !e.isComposing) {
    // split paragraphs ourselves; the browser's own newline handling adds stray blank lines
    e.preventDefault();
    const el = objEl(S.editObj), flow = $('.flow', el), o = objById(S.editObj);
    if (!getSelection().isCollapsed) { document.execCommand('delete'); o.text = flowText(flow); }
    const c = getCaret(flow) ?? o.text.length;
    o.text = o.text.slice(0, c) + '\n' + o.text.slice(c);
    setParas(flow, o.text); paintText(o, el); setCaret(flow, c + 1); fitText(o); positionBar();
  }
});

$('#imgfile').onchange = async e => {
  const f = e.target.files[0]; e.target.value = '';
  if (!f) return setTool('select');
  const data = await readURL(f), im = new Image();
  im.src = data; await im.decode();
  const k = Math.min(1, 220 / Math.max(im.width, im.height));
  S.img = { data, w: im.width * k, h: im.height * k };
  renderBar();
  toast('Click or drag on the page to place the image');
};

// ---------- signature ----------
const dlg = $('#signdlg'), cv = $('#sd-canvas'), g = cv.getContext('2d');
let sigMode = 'draw', sigColor = '#111827', sigFont = null, sigDirty = false;
function openSign() {
  g.clearRect(0, 0, cv.width, cv.height); sigDirty = false;
  const box = $('#sd-colors'); box.replaceChildren();
  ['#111827', '#1d4ed8', '#b91c1c'].forEach(c => { const b = h('button', 'dot' + (c === sigColor ? ' on' : ''), { type: 'button' }); b.style.background = c; b.onclick = () => { sigColor = c; $$('.dot', box).forEach(x => x.classList.toggle('on', x === b)); }; box.append(b); });
  const fonts = S.fonts.filter(f => f.cat === 'Handwriting');
  sigFont = sigFont || fonts[0]?.label;
  const fl = $('#sd-fonts'); fl.replaceChildren();
  fonts.forEach(f => { const b = h('button', 'sf' + (f.label === sigFont ? ' on' : ''), { type: 'button', textContent: $('#sd-name').value || 'Your signature' }); b.style.fontFamily = `'F_${f.label}'`; b.onclick = () => { sigFont = f.label; $$('.sf', fl).forEach(x => x.classList.toggle('on', x === b)); }; fl.append(b); });
  dlg.showModal();
}
$('#sd-name').oninput = () => $$('.sf').forEach(b => b.textContent = $('#sd-name').value || 'Your signature');
$$('#sd-mode button').forEach(b => b.onclick = () => {
  sigMode = b.dataset.m; $$('#sd-mode button').forEach(x => x.classList.toggle('on', x === b));
  $('.sd-draw').hidden = sigMode !== 'draw'; $('.sd-type').hidden = sigMode !== 'type';
  if (sigMode === 'type') $('#sd-name').focus();
});
cv.addEventListener('pointerdown', e => {
  const r = cv.getBoundingClientRect(), k = cv.width / r.width;
  const p = ev => [(ev.clientX - r.left) * k, (ev.clientY - r.top) * k];
  cv.setPointerCapture(e.pointerId);
  g.strokeStyle = sigColor; g.lineWidth = 5; g.lineCap = g.lineJoin = 'round';
  g.beginPath(); g.moveTo(...p(e)); sigDirty = true;
  const mv = ev => { g.lineTo(...p(ev)); g.stroke(); };
  cv.addEventListener('pointermove', mv);
  cv.addEventListener('pointerup', () => cv.removeEventListener('pointermove', mv), { once: true });
});
$('#sd-clear').onclick = () => { g.clearRect(0, 0, cv.width, cv.height); sigDirty = false; };
function trimmed(c) {
  const d = c.getContext('2d').getImageData(0, 0, c.width, c.height).data;
  let x0 = c.width, y0 = c.height, x1 = 0, y1 = 0;
  for (let y = 0; y < c.height; y++) for (let x = 0; x < c.width; x++) if (d[(y * c.width + x) * 4 + 3] > 8) { x0 = Math.min(x0, x); x1 = Math.max(x1, x); y0 = Math.min(y0, y); y1 = Math.max(y1, y); }
  if (x1 <= x0) return null;
  const out = document.createElement('canvas'); out.width = x1 - x0 + 16; out.height = y1 - y0 + 16;
  out.getContext('2d').drawImage(c, x0 - 8, y0 - 8, out.width, out.height, 0, 0, out.width, out.height);
  return out;
}
dlg.addEventListener('close', async () => {
  if (dlg.returnValue !== 'ok') return setTool('select');
  let c;
  if (sigMode === 'draw') { if (!sigDirty) return setTool('select'); c = trimmed(cv); }
  else {
    const name = $('#sd-name').value.trim(); if (!name) return setTool('select');
    await document.fonts.load(`96px 'F_${sigFont}'`);
    const t = document.createElement('canvas'); t.width = 1600; t.height = 320;
    const tg = t.getContext('2d'); tg.font = `96px 'F_${sigFont}'`; tg.fillStyle = sigColor; tg.textBaseline = 'middle'; tg.fillText(name, 40, 160);
    c = trimmed(t);
  }
  if (!c) return setTool('select');
  const k = 160 / c.width;
  S.img = { data: c.toDataURL('image/png'), w: 160, h: c.height * k };
  renderBar();
  toast('Click on the page to place your signature');
});

// ---------- pages ----------
function remapPages(fn) { S.objs = S.objs.flatMap(fn); }
function pageOp(a, n, extra = {}) {
  const W = S.pages[n]?.w, H = S.pages[n]?.h;
  if (a === 'rotate') return op('rotate', { page: n, deg: extra.deg }, () => remapPages(o => {
    if (o.page !== n) return [o];
    const cx = o.x + o.w / 2, cy = o.y + o.h / 2, [nx, ny] = extra.deg > 0 ? [H - cy, cx] : [cy, W - cx];
    return [{ ...o, x: nx - o.w / 2, y: ny - o.h / 2, rot: ((o.rot || 0) + extra.deg + 360) % 360 }];
  }));
  if (a === 'delete') return op('delete_page', { page: n }, () => remapPages(o => o.page === n ? [] : [{ ...o, page: o.page > n ? o.page - 1 : o.page }]));
  if (a === 'blank') return op('blank_page', { page: n }, () => remapPages(o => [{ ...o, page: o.page > n ? o.page + 1 : o.page }]));
  if (a === 'dup') return op('duplicate_page', { page: n }, () => remapPages(o => o.page === n ? [o, { ...clone(o), id: 'o' + S.seq++, page: n + 1 }] : [{ ...o, page: o.page > n ? o.page + 1 : o.page }]));
  if (a === 'move') {
    const to = extra.to;
    return op('move_page', { page: n, page_to: to }, () => remapPages(o => {
      const order = [...S.pages.keys()]; const [m] = order.splice(n, 1); order.splice(to, 0, m);
      return [{ ...o, page: order.indexOf(o.page) }];
    }));
  }
}
const thumbs = $('#thumbs');
thumbs.addEventListener('click', e => {
  const b = e.target.closest('.tact button'), t = e.target.closest('.thumb');
  if (!t) return;
  const n = +t.dataset.n;
  if (b) return pageOp(b.dataset.p, n, { deg: +b.dataset.deg });
  if (t._dragged) return;
  pageEl(n)?.scrollIntoView({ behavior: 'smooth', block: 'start' });
  document.body.classList.remove('pages-open');
});
// Reorder by pointer drag (mouse right away, touch after a short hold so the list still scrolls).
thumbs.addEventListener('pointerdown', e => {
  const t = e.target.closest('.thumb'); if (!t || e.target.closest('.tact')) return;
  const from = +t.dataset.n, sx = e.clientX, sy = e.clientY;
  let ghost = null, over = null, timer = null;
  const begin = () => {
    ghost = t.cloneNode(true); ghost.classList.add('ghost'); document.body.append(ghost); t.classList.add('lifting');
    try { thumbs.setPointerCapture(e.pointerId); } catch (_) {}
    navigator.vibrate?.(10);
  };
  if (e.pointerType === 'touch') timer = setTimeout(begin, 280);
  const move = ev => {
    if (!ghost) { if (e.pointerType !== 'touch' && Math.hypot(ev.clientX - sx, ev.clientY - sy) > 6) begin(); else { if (Math.hypot(ev.clientX - sx, ev.clientY - sy) > 8) clearTimeout(timer); return; } }
    ev.preventDefault();
    Object.assign(ghost.style, { left: ev.clientX - 48 + 'px', top: ev.clientY - 40 + 'px' });
    const hit = document.elementFromPoint(ev.clientX, ev.clientY)?.closest('.thumb:not(.ghost)');
    if (hit !== over) { over?.classList.remove('over'); over = hit; over?.classList.add('over'); }
  };
  const up = () => {
    clearTimeout(timer); removeEventListener('pointermove', move); removeEventListener('pointerup', up); removeEventListener('pointercancel', up);
    if (!ghost) return;
    ghost.remove(); t.classList.remove('lifting'); over?.classList.remove('over');
    t._dragged = true; setTimeout(() => { t._dragged = false; });
    if (over && +over.dataset.n !== from) pageOp('move', from, { to: +over.dataset.n });
  };
  addEventListener('pointermove', move, { passive: false }); addEventListener('pointerup', up); addEventListener('pointercancel', up);
});
thumbs.addEventListener('touchmove', e => { if ($('.thumb.ghost')) e.preventDefault(); }, { passive: false });
$('#addpage').onclick = () => pageOp('blank', S.pages.length - 1);
viewer.addEventListener('scroll', () => {
  const mid = viewer.getBoundingClientRect().top + viewer.clientHeight / 3;
  const cur = $$('.page').find(p => p.getBoundingClientRect().bottom > mid);
  $$('.thumb.current').forEach(t => t.classList.remove('current'));
  if (cur) $(`.thumb[data-n="${cur.dataset.n}"]`)?.classList.add('current');
}, { passive: true });

// ---------- toolbar ----------
function setTool(t) {
  if (t !== S.tool) { endTextEdit(); commitSpan(); }
  if (t !== 'image' && t !== 'sign') S.img = null;
  S.tool = t; document.body.dataset.tool = t;
  $$('.tools button').forEach(b => b.classList.toggle('active', b.dataset.tool === t));
  if (t !== 'select') select(null); else renderBar();
  if (t === 'image' && !S.img) $('#imgfile').click();
  if (t === 'sign' && !S.img) openSign();
}
$$('.tools button').forEach(b => b.onclick = () => setTool(b.dataset.tool));

function setZoom(z) {
  S.zoom = Math.min(4, Math.max(0.25, z));
  endTextEdit(); commitSpan(); build();
}
const fitWidth = () => setZoom(Math.min(1.5, (viewer.clientWidth - 96) / Math.max(...S.pages.map(p => p.w))));
$('#zin').onclick = () => setZoom(S.zoom * 1.2);
$('#zout').onclick = () => setZoom(S.zoom / 1.2);
$('#zoom').onclick = fitWidth;
$('#undo').onclick = undo;
$('#redo').onclick = redo;
$('#save').onclick = download;
$('#open').onclick = $('#open2').onclick = () => $('#file').click();
$('#merge').onclick = () => $('#mergefile').click();
$('#ocr').onclick = runOcr;
$('#file').onchange = e => { openFile(e.target.files[0]); e.target.value = ''; };
$('#mergefile').onchange = async e => {
  const f = e.target.files[0]; e.target.value = '';
  if (f && await op('merge', { data: await readB64(f) })) toast(`Appended ${f.name}`);
};
$('#find').onclick = () => { $('#findbar').hidden = false; $('#f-find').focus(); };
$('#f-close').onclick = () => { $('#findbar').hidden = true; };
$('#f-go').onclick = async () => {
  const j = await op('replace', { find: $('#f-find').value, repl: $('#f-repl').value });
  if (j) toast(j.count ? `Replaced ${j.count} occurrence${j.count > 1 ? 's' : ''}` : 'No matches');
};
$('#findbar').addEventListener('keydown', e => { if (e.key === 'Enter') $('#f-go').click(); if (e.key === 'Escape') $('#f-close').click(); });

(function theme() {
  const root = document.documentElement, KEY = 'pdfedit-theme';
  $('#theme').onclick = () => { const t = root.dataset.theme === 'dark' ? 'light' : 'dark'; root.dataset.theme = t; try { localStorage.setItem(KEY, t); } catch (e) {} };
})();

document.addEventListener('keydown', e => {
  const mod = e.metaKey || e.ctrlKey, k = e.key.toLowerCase();
  const typing = e.target.closest('input, select, textarea, [contenteditable=true], [contenteditable=plaintext-only]');
  if (mod && k === 'o') { e.preventDefault(); $('#file').click(); return; }
  if (!S.id || dlg.open) return;
  if (mod && k === 's') { e.preventDefault(); download(); return; }
  if (mod && k === 'f') { e.preventDefault(); $('#find').click(); return; }
  const tsel = S.sel && objById(S.sel);
  if (mod && tsel?.type === 'text') {
    const sc = { b: () => mutate(tsel, { bold: !tsel.bold }), i: () => mutate(tsel, { italic: !tsel.italic }), u: () => mutate(tsel, { underline: { on: !tsel.underline.on } }),
      '[': () => mutate(tsel, { size: Math.max(4, tsel.size - (e.shiftKey ? 10 : 1)) }, 'size'), ']': () => mutate(tsel, { size: tsel.size + (e.shiftKey ? 10 : 1) }, 'size') };
    if (e.shiftKey && k === 'x') sc.x = () => mutate(tsel, { strike: { on: !tsel.strike.on } });
    if (sc[k]) { e.preventDefault(); sc[k](); renderBar(); return; }
  }
  if (mod && S.editing && (k === 'b' || k === 'i')) { e.preventDefault(); spanProp(k === 'b' ? 'bold' : 'italic', !S.editing[k === 'b' ? 'bold' : 'italic']); renderBar(); return; }
  if (typing) return;
  if (mod && k === 'z') { e.preventDefault(); e.shiftKey ? redo() : undo(); return; }
  if (mod && k === 'y') { e.preventDefault(); redo(); return; }
  const o = S.sel && objById(S.sel);
  if (o && mod && k === 'd') { e.preventDefault(); duplicate(o); return; }
  if (mod) return;
  if (o && (e.key === 'Delete' || e.key === 'Backspace')) { e.preventDefault(); removeObj(o); return; }
  if (o && e.key.startsWith('Arrow')) {
    e.preventDefault(); const s = e.shiftKey ? 10 : 1;
    mutate(o, { x: o.x + (e.key === 'ArrowLeft' ? -s : e.key === 'ArrowRight' ? s : 0), y: o.y + (e.key === 'ArrowUp' ? -s : e.key === 'ArrowDown' ? s : 0) }, 'nudge');
    return;
  }
  if (o && e.key === 'Enter' && o.type === 'text') { e.preventDefault(); editText(o.id); return; }
  if (e.key === 'Escape') { if (S.painter) stopPainter(); else if (S.sel) select(null); else setTool('select'); return; }
  const t = { v: 'select', t: 'text', i: 'image', r: 'shape', d: 'ink', h: 'highlight', w: 'whiteout', s: 'sign', n: 'note', k: 'stamp' }[k];
  if (t) setTool(t);
  if (e.key === '+' || e.key === '=') setZoom(S.zoom * 1.2);
  if (e.key === '-') setZoom(S.zoom / 1.2);
});

// ---------- tooltips ----------
(function tips() {
  const tip = $('#tip'); let timer;
  document.addEventListener('pointerover', e => {
    const t = e.target.closest('[data-tip]'); clearTimeout(timer); tip.classList.remove('on');
    if (!t || t.classList.contains('open') || e.pointerType !== 'mouse') return;
    timer = setTimeout(() => {
      tip.textContent = t.dataset.tip; const r = t.getBoundingClientRect();
      tip.style.left = Math.max(6, Math.min(innerWidth - tip.offsetWidth - 6, r.left + r.width / 2 - tip.offsetWidth / 2)) + 'px';
      tip.style.top = (r.bottom + 8 + 30 > innerHeight ? r.top - 34 : r.bottom + 8) + 'px';
      tip.classList.add('on');
    }, 380);
  });
  document.addEventListener('pointerdown', () => { clearTimeout(timer); tip.classList.remove('on'); }, true);
})();

// ---------- drag & drop a file ----------
let dragDepth = 0;
addEventListener('dragenter', e => { if (e.dataTransfer.types.includes('Files')) { dragDepth++; document.body.classList.add('dragging'); } });
addEventListener('dragleave', () => { if (--dragDepth <= 0) { dragDepth = 0; document.body.classList.remove('dragging'); } });
addEventListener('dragover', e => { if (e.dataTransfer.types.includes('Files')) e.preventDefault(); });
addEventListener('drop', e => {
  dragDepth = 0; document.body.classList.remove('dragging');
  const files = [...(e.dataTransfer.files || [])];
  const pdf = files.find(f => /pdf$/i.test(f.type) || /\.pdf$/i.test(f.name));
  const img = files.find(f => /^image\//.test(f.type));
  if (pdf) { e.preventDefault(); return openFile(pdf); }
  if (img && S.id) { e.preventDefault(); S.img = null; S.tool = 'image'; document.body.dataset.tool = 'image'; $('#imgfile').onchange({ target: { files: [img], value: '' } }); }
});
addEventListener('beforeunload', e => { if (S.hist.length) e.preventDefault(); });
$('.cover').addEventListener('click', e => { if (!e.target.closest('button')) $('#file').click(); });
dotMatrix($('#dots'), 'folio');
// Spotlight follows the pointer on .spot surfaces; .comet tiles tilt toward it.
document.addEventListener('pointermove', e => {
  const el = e.target.closest?.('.spot'); if (!el) return;
  const r = el.getBoundingClientRect();
  el.style.setProperty('--mx', e.clientX - r.left + 'px'); el.style.setProperty('--my', e.clientY - r.top + 'px');
  if (el.classList.contains('comet')) el.style.transform = `perspective(1000px) rotateX(${((e.clientY - r.top) / r.height - 0.5) * -12}deg) rotateY(${((e.clientX - r.left) / r.width - 0.5) * 12}deg)`;
}, { passive: true });
$$('.comet').forEach(t => t.addEventListener('pointerleave', () => { t.style.transform = ''; }));

// Port of the dashboard's DotMatrix: a dot field spelling `text`; dots ripple and swell toward the cursor.
function dotMatrix(canvas, text, gap = 11) {
  const ctx = canvas.getContext('2d'), reduce = matchMedia('(prefers-reduced-motion: reduce)').matches;
  let w = 0, hgt = 0, dpr = 1, cols = 0, rows = 0, mask = new Uint8Array(0), raf = 0;
  const mouse = { x: -9999, y: -9999 };
  const colors = () => { const cs = getComputedStyle(document.documentElement); return { on: cs.getPropertyValue('--accent').trim(), off: cs.getPropertyValue('--line-2').trim() }; };
  let col = colors();
  const build = () => {
    const r = canvas.getBoundingClientRect(); dpr = Math.min(devicePixelRatio || 1, 2);
    w = r.width; hgt = r.height; canvas.width = w * dpr; canvas.height = hgt * dpr;
    cols = Math.floor(w / gap); rows = Math.floor(hgt / gap);
    if (cols < 1 || rows < 1) { mask = new Uint8Array(0); cols = rows = 0; return; }
    const off = document.createElement('canvas'); off.width = cols; off.height = rows;
    const o = off.getContext('2d');
    let size = rows * 0.95;
    o.font = `600 ${size}px 'Clash Display', 'Satoshi', sans-serif`;
    const tw = o.measureText(text).width; if (tw > cols * 0.9) size *= cols * 0.9 / tw;
    o.font = `600 ${size}px 'Clash Display', 'Satoshi', sans-serif`; o.textAlign = 'center'; o.textBaseline = 'middle'; o.fillStyle = '#000';
    o.fillText(text, cols / 2, rows / 2 + size * 0.04);
    const data = o.getImageData(0, 0, cols, rows).data;
    mask = new Uint8Array(cols * rows); for (let i = 0; i < cols * rows; i++) mask[i] = data[i * 4 + 3] > 110 ? 1 : 0;
  };
  const draw = t => {
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0); ctx.clearRect(0, 0, w, hgt);
    const ox = (w - cols * gap) / 2 + gap / 2, oy = (hgt - rows * gap) / 2 + gap / 2;
    for (let y = 0; y < rows; y++) for (let x = 0; x < cols; x++) {
      const px = ox + x * gap, py = oy + y * gap, on = mask[y * cols + x] === 1;
      const wave = reduce ? 0.5 : (Math.sin(x * 0.22 - t * 0.0022 + y * 0.08) + 1) / 2;
      const near = Math.max(0, 1 - Math.hypot(px - mouse.x, py - mouse.y) / 120);
      const r = (on ? gap * (0.2 + wave * 0.16) : gap * 0.09) + near * gap * (on ? 0.14 : 0.18);
      ctx.globalAlpha = on ? 0.55 + wave * 0.45 : 0.55 + near * 0.45;
      ctx.fillStyle = on || near > 0.35 ? col.on : col.off;
      ctx.beginPath(); ctx.arc(px, py, r, 0, Math.PI * 2); ctx.fill();
    }
    ctx.globalAlpha = 1;
    if (!reduce && document.body.classList.contains('empty')) raf = requestAnimationFrame(draw); else raf = 0;
  };
  canvas.addEventListener('pointermove', e => { const r = canvas.getBoundingClientRect(); mouse.x = e.clientX - r.left; mouse.y = e.clientY - r.top; });
  canvas.addEventListener('pointerleave', () => { mouse.x = mouse.y = -9999; });
  new ResizeObserver(() => { build(); if (reduce) draw(0); }).observe(canvas);
  new MutationObserver(() => { col = colors(); if (!raf && document.body.classList.contains('empty')) raf = requestAnimationFrame(draw); })
    .observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });
  new MutationObserver(() => { if (!raf && document.body.classList.contains('empty')) raf = requestAnimationFrame(draw); }).observe(document.body, { attributes: true, attributeFilter: ['class'] });
  document.fonts.load("600 40px 'Clash Display'").then(build).catch(() => {});
  build(); raf = requestAnimationFrame(draw);
}
setTool('select');


// ---------- touch: pinch zoom, long-press menu, sheets, drawer, keyboard ----------
(function pinch() {
  let d0 = 0, z0 = 1, mid = null, live = 1;
  const dist = t => Math.hypot(t[0].clientX - t[1].clientX, t[0].clientY - t[1].clientY);
  viewer.addEventListener('touchstart', e => {
    if (e.touches.length !== 2 || !S.id) return;
    d0 = dist(e.touches); z0 = S.zoom; live = 1;
    const r = viewer.getBoundingClientRect();
    mid = { x: (e.touches[0].clientX + e.touches[1].clientX) / 2 - r.left, y: (e.touches[0].clientY + e.touches[1].clientY) / 2 - r.top };
    pagesBox.style.transformOrigin = `${mid.x + viewer.scrollLeft}px ${mid.y + viewer.scrollTop}px`;
  }, { passive: true });
  viewer.addEventListener('touchmove', e => {
    if (e.touches.length !== 2 || !d0) return;
    live = Math.min(4 / z0, Math.max(0.25 / z0, dist(e.touches) / d0));
    pagesBox.style.transform = `scale(${live})`;
  }, { passive: true });
  viewer.addEventListener('touchend', e => {
    if (!d0 || e.touches.length) return;
    d0 = 0; pagesBox.style.transform = '';
    if (Math.abs(live - 1) < 0.02) return;
    const ax = (viewer.scrollLeft + mid.x) * live - mid.x, ay = (viewer.scrollTop + mid.y) * live - mid.y;
    setZoom(z0 * live);
    viewer.scrollLeft = ax; viewer.scrollTop = ay;
  });
  // iOS Safari: keep its own page zoom out of the way
  document.addEventListener('gesturestart', e => e.preventDefault());
})();

function ctxMenu(o, x, y) {
  UI.closePop();
  const m = h('div', 'pop ctxmenu');
  const item = (label, fn, cls = '') => { const b = h('button', cls, { type: 'button', textContent: label }); b.onclick = () => { m.remove(); S.ctx = null; fn(); }; m.append(b); };
  if (o.type === 'text') item('Edit text', () => editText(o.id));
  item('Duplicate', () => duplicate(o));
  item('Bring to front', () => reorder(o, true));
  item('Send to back', () => reorder(o, false));
  item('Delete', () => removeObj(o), 'danger');
  document.body.append(m);
  if (!isMobile()) { m.style.left = Math.min(x, innerWidth - m.offsetWidth - 8) + 'px'; m.style.top = Math.min(y, innerHeight - m.offsetHeight - 8) + 'px'; }
  S.ctx = m;
  setTimeout(() => addEventListener('pointerdown', function off(ev) { if (!m.contains(ev.target)) { m.remove(); S.ctx = null; removeEventListener('pointerdown', off, true); } }, true));
}
function longPress(e, o) {
  if (e.pointerType !== 'touch') return;
  const sx = e.clientX, sy = e.clientY;
  const timer = setTimeout(() => { navigator.vibrate?.(12); ctxMenu(o, sx, sy); }, 520);
  const cancel = ev => { if (ev.type !== 'pointermove' || Math.hypot(ev.clientX - sx, ev.clientY - sy) > 8) { clearTimeout(timer); off(); } };
  const off = () => { removeEventListener('pointermove', cancel); removeEventListener('pointerup', cancel); removeEventListener('pointercancel', cancel); };
  addEventListener('pointermove', cancel); addEventListener('pointerup', cancel); addEventListener('pointercancel', cancel);
}
pagesBox.addEventListener('contextmenu', e => {
  const oel = e.target.closest('.obj'); if (!oel) return;
  e.preventDefault(); const o = objById(oel.dataset.id); select(o.id); ctxMenu(o, e.clientX, e.clientY);
});

// pages drawer (the backdrop lives inside the framed shell so the drawer can sit above it)
$('.shell').append($('#scrim'));
const togglePages = open => document.body.classList.toggle('pages-open', open);
$('#pagesbtn').onclick = () => togglePages(!document.body.classList.contains('pages-open'));
$('#scrim').onclick = () => togglePages(false);
$$('[data-proxy]').forEach(b => b.onclick = () => { togglePages(false); $(b.dataset.proxy).click(); });

// swipe a sheet down to dismiss it
function swipeDismiss(el, handle, onClose) {
  el.addEventListener('pointerdown', e => {
    if (!isMobile() || e.pointerType === 'mouse' || !handle(e)) return;
    const sy = e.clientY; let dy = 0;
    const mv = ev => { dy = Math.max(0, ev.clientY - sy); el.style.transform = `translateY(${dy}px)`; };
    const up = () => { removeEventListener('pointermove', mv); removeEventListener('pointerup', up); removeEventListener('pointercancel', up); el.style.transform = ''; if (dy > 70) onClose(); };
    addEventListener('pointermove', mv); addEventListener('pointerup', up); addEventListener('pointercancel', up);
  });
}
swipeDismiss(pbar, e => e.clientY - pbar.getBoundingClientRect().top < 30 || e.target.closest('.ptag'), () => { if (S.sel) select(null); else pbar.hidden = true; });
swipeDismiss($('#rail'), e => !!e.target.closest('.sheet-head'), () => togglePages(false));

// keep the text being typed above the on-screen keyboard
window.visualViewport?.addEventListener('resize', () => {
  const el = S.editObj ? objEl(S.editObj) : S.editing?.el; if (!el) return;
  const r = el.getBoundingClientRect(), vv = visualViewport;
  if (r.bottom > vv.offsetTop + vv.height - 12 || r.top < vv.offsetTop) el.scrollIntoView({ block: 'center', behavior: 'smooth' });
});
