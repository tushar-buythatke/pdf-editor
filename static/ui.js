'use strict';
// Small control kit for the property bar: popovers, swatches, segmented, steppers, sliders, font picker.
const UI = (() => {
  const h = (tag, cls, props = {}) => Object.assign(document.createElement(tag), cls ? { className: cls } : {}, props);
  const ICON = {
    chev: '<svg viewBox="0 0 24 24"><path d="M7 10l5 5 5-5"/></svg>',
    none: '<svg viewBox="0 0 24 24"><path d="M5 19L19 5"/></svg>',
  };
  const PALETTE = ['#111827', '#4b5563', '#9ca3af', '#ffffff', '#ef4444', '#f97316', '#f59e0b', '#facc15',
    '#22c55e', '#14b8a6', '#06b6d4', '#3b82f6', '#6366f1', '#8b5cf6', '#ec4899', '#a16207'];

  let openPop = null;
  const closePop = () => { if (openPop) { const o = openPop; openPop = null; o.pop.remove(); o.btn.classList.remove('open'); o.pop._close?.(); o.btn._onClose?.(); } };
  document.addEventListener('pointerdown', e => {
    if (openPop && !openPop.pop.contains(e.target) && !openPop.btn.contains(e.target)) closePop();
  }, true);
  document.addEventListener('keydown', e => {
    if (!openPop) return;
    if (openPop.pop._key?.(e)) { e.preventDefault(); e.stopPropagation(); return; }
    if (e.key === 'Escape') { closePop(); e.stopPropagation(); }
  }, true);

  // A button that opens a floating panel built on demand.
  function pop(btn, build, cls = '') {
    btn.addEventListener('click', () => {
      if (openPop?.btn === btn) return closePop();
      closePop();
      const p = h('div', 'pop ' + cls);
      build(p, closePop);
      document.body.append(p);
      if (matchMedia('(max-width: 760px)').matches) { btn.classList.add('open'); openPop = { btn, pop: p }; return; }  // CSS makes it a sheet
      const r = btn.getBoundingClientRect(), pr = p.getBoundingClientRect();
      let x = r.left + r.width / 2 - pr.width / 2, y = r.bottom + 8;
      if (y + pr.height > innerHeight - 8) y = r.top - pr.height - 8;
      p.style.left = Math.max(8, Math.min(innerWidth - pr.width - 8, x)) + 'px';
      p.style.top = Math.max(8, y) + 'px';
      btn.classList.add('open');
      openPop = { btn, pop: p };
    });
    return btn;
  }

  // Colour picker: palette, recents, custom + hex (#rrggbb[aa]), eyedropper, opacity. null = none / auto.
  const store = (k, v) => { try { if (v === undefined) return JSON.parse(localStorage.getItem(k) || '[]'); localStorage.setItem(k, JSON.stringify(v)); } catch (e) { return []; } };
  const hexA = (hex, a) => hex.slice(0, 7) + (a >= 1 ? '' : Math.round(a * 255).toString(16).padStart(2, '0'));
  const alphaOf = v => v && v.length === 9 ? parseInt(v.slice(7), 16) / 255 : 1;
  function swatch({ value, allowNone = false, auto = '', label = 'Colour', icon = '', onChange }) {
    const b = h('button', 'swb', { type: 'button', title: label });
    b.dataset.tip = label;
    const paint = v => { b.innerHTML = `${icon}<i class="chip ${v ? '' : auto ? 'auto' : 'none'}" style="${v ? `background:${v}` : ''}"></i>`; };
    paint(value);
    pop(b, (p, close) => {
      p.classList.add('pal');
      const pick = (v, keep) => {
        value = v; paint(v); onChange(v); mark();
        if (v && !keep) store('recentColors', [v.slice(0, 7), ...store('recentColors').filter(c => c !== v.slice(0, 7))].slice(0, 8));
      };
      const cell = (c, cls = '') => { const cb = h('button', 'cell ' + cls, { type: 'button', title: c || label }); if (c) cb.style.setProperty('--c', c); cb.dataset.c = c || ''; cb.onclick = () => pick(c ? hexA(c, alpha.value / 100) : null); return cb; };
      const grid = h('div', 'grid');
      if (auto) { const n = h('button', 'autocell', { type: 'button', textContent: auto }); n.onclick = () => pick(null); p.append(n); }
      if (allowNone) { const n = cell(null, 'none'); n.innerHTML = ICON.none; n.title = 'None'; grid.append(n); }
      PALETTE.forEach(c => grid.append(cell(c)));
      const rec = store('recentColors');
      const recent = h('div', 'grid recent');
      rec.forEach(c => recent.append(cell(c)));
      const row = h('div', 'hexrow');
      const native = h('input', '', { type: 'color', value: (value || '#000000').slice(0, 7), title: 'Custom colour' });
      const hex = h('input', 'hex', { value: value || '', placeholder: allowNone ? 'none' : 'hex', maxLength: 9, spellcheck: false });
      native.oninput = () => pick(hexA(native.value, alpha.value / 100), true);
      native.onchange = () => pick(hexA(native.value, alpha.value / 100));
      hex.onkeydown = e => {
        if (e.key !== 'Enter') return;
        e.preventDefault();
        let v = hex.value.trim().toLowerCase().replace(/^#?/, '#');
        if (/^#[0-9a-f]{3}$/.test(v)) v = '#' + [...v.slice(1)].map(x => x + x).join('');
        if (/^#([0-9a-f]{6}|[0-9a-f]{8})$/.test(v)) { alpha.value = Math.round(alphaOf(v) * 100); pick(v); } else if (allowNone && !hex.value.trim()) pick(null);
      };
      row.append(native, hex);
      if (window.EyeDropper) {
        const eye = h('button', 'eye', { type: 'button', title: 'Pick from screen', innerHTML: '<svg viewBox="0 0 24 24"><path d="M14 6l4 4M4.5 19.5l1-3.5 9-9 2.5 2.5-9 9zM15 5l1.5-1.5a2.1 2.1 0 013 3L18 8"/></svg>' });
        eye.onclick = async () => { try { const r = await new EyeDropper().open(); pick(hexA(r.sRGBHex, alpha.value / 100)); } catch (e) {} };
        row.append(eye);
      }
      const alpha = h('input', '', { type: 'range', min: 0, max: 100, step: 1, value: Math.round(alphaOf(value) * 100) });
      const aout = h('span', '', { textContent: alpha.value + '%' });
      alpha.oninput = () => { aout.textContent = alpha.value + '%'; if (value) pick(hexA(value, alpha.value / 100), true); };
      const arow = h('label', 'alpharow'); arow.append(h('span', '', { textContent: 'Opacity' }), alpha, aout);
      const mark = () => { hex.value = value || ''; p.querySelectorAll('.cell').forEach(c => c.classList.toggle('sel', (c.dataset.c || null) === (value ? value.slice(0, 7) : null))); p.querySelector('.autocell')?.classList.toggle('sel', !value); };
      p.append(h('div', 'ptitle', { textContent: label }), grid);
      if (rec.length) p.append(h('div', 'ptitle', { textContent: 'Recent' }), recent);
      p.append(row, arow);
      [grid, recent].forEach(g => g.addEventListener('keydown', e => {
        const cells = [...g.querySelectorAll('.cell')], i = cells.indexOf(document.activeElement);
        const d = { ArrowRight: 1, ArrowLeft: -1, ArrowDown: 8, ArrowUp: -8 }[e.key];
        if (d === undefined || i < 0) return;
        e.preventDefault(); cells[Math.max(0, Math.min(cells.length - 1, i + d))].focus();
      }));
      requestAnimationFrame(() => (grid.querySelector('.cell.sel') || grid.querySelector('.cell')).focus());
      mark();
    });
    b.set = v => { value = v; paint(v); };
    return b;
  }

  function seg(options, value, onChange, cls = '') {
    const w = h('div', 'seg ' + cls);
    options.forEach(o => {
      const b = h('button', '', { type: 'button', innerHTML: o.html, title: o.title || '' });
      if (o.title) b.dataset.tip = o.title;
      b.classList.toggle('on', o.v === value);
      b.onclick = () => {
        const v = o.toggle && o.v === value ? null : o.v;
        value = v; w.querySelectorAll('button').forEach((x, i) => x.classList.toggle('on', options[i].v === v)); onChange(v);
      };
      w.append(b);
    });
    w.addEventListener('keydown', e => {
      const d = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 }[e.key]; if (!d) return;
      e.preventDefault();
      const bs = [...w.querySelectorAll('button')], i = Math.max(0, options.findIndex(o => o.v === value));
      const nb = bs[(i + d + bs.length) % bs.length]; nb.click(); nb.focus();
    });
    return w;
  }

  function toggle(html, title, on, onChange) {
    const b = h('button', 'tg', { type: 'button', innerHTML: html, title });
    b.dataset.tip = title;
    b.classList.toggle('on', !!on);
    b.onclick = () => { on = !on; b.classList.toggle('on', on); onChange(on); };
    return b;
  }

  function stepper(value, { min = 0, max = 999, step = 1, title = '', suffix = '' }, onChange) {
    const w = h('div', 'stepper'); w.dataset.tip = title;
    const dec = h('button', '', { type: 'button', textContent: '−' }), inc = h('button', '', { type: 'button', textContent: '+' });
    const inp = h('input', '', { value, inputMode: 'decimal', title });
    const set = v => {
      v = Math.min(max, Math.max(min, Math.round(v / step) * step));
      if (!isFinite(v)) { inp.value = value; return; }
      value = +v.toFixed(2); inp.value = value + suffix; onChange(value);
    };
    inp.value = value + suffix;
    dec.onclick = () => set(value - step); inc.onclick = () => set(value + step);
    inp.onfocus = () => { inp.value = value; inp.select(); };
    inp.onblur = () => { inp.value = value + suffix; };
    inp.onkeydown = e => {
      if (e.key === 'Enter') { e.preventDefault(); set(parseFloat(inp.value)); inp.blur(); }
      if (e.key === 'ArrowUp') { e.preventDefault(); set(value + step * (e.shiftKey ? 10 : 1)); inp.value = value; }
      if (e.key === 'ArrowDown') { e.preventDefault(); set(value - step * (e.shiftKey ? 10 : 1)); inp.value = value; }
    };
    inp.onchange = () => set(parseFloat(inp.value));
    w.append(dec, inp, inc);
    return w;
  }

  function slider(value, { min = 0, max = 1, step = 0.01, fmt = v => v }, onChange) {
    const w = h('label', 'slider');
    const r = h('input', '', { type: 'range', min, max, step, value });
    const out = h('span', '', { textContent: fmt(value) });
    r.oninput = () => { out.textContent = fmt(+r.value); onChange(+r.value); };
    w.append(r, out);
    return w;
  }

  // Font picker: grouped by category, each name in its own face. Arrows live-preview, Enter commits, Esc reverts.
  function fontPicker(fonts, value, onChange, extras = []) {
    const b = h('button', 'fontb', { type: 'button' });
    b.dataset.tip = 'Font (↑/↓ to cycle)';
    const paint = v => { b.innerHTML = `<span style="font-family:${v ? `'F_${v}'` : 'inherit'}">${v || 'Original font'}</span>${ICON.chev}`; };
    paint(value);
    const labels = fonts.map(f => f.label);
    b.addEventListener('keydown', e => {
      if (openPop?.btn === b) return;
      const d = { ArrowDown: 1, ArrowUp: -1 }[e.key]; if (!d) return;
      e.preventDefault();
      const i = labels.indexOf(value);
      value = labels[i < 0 ? (d > 0 ? 0 : labels.length - 1) : (i + d + labels.length) % labels.length];
      paint(value); onChange(value, 'commit');
    });
    pop(b, (p, close) => {
      p.classList.add('fonts'); p.tabIndex = -1;
      const orig = value, items = [];
      const q = h('input', 'fsearch', { placeholder: 'Search fonts', spellcheck: false });
      p.append(q);
      let act = -1, done = false, typed = '', typedAt = 0;
      const add = (label, css, on, pick) => {
        const o = h('button', 'fo' + (on ? ' sel' : ''), { type: 'button', textContent: label, tabIndex: -1 });
        o.style.fontFamily = css; o._pick = pick; o._label = label; items.push(o); p.append(o); return o;
      };
      if (extras.length) {
        p.append(h('div', 'ptitle', { textContent: 'This document' }));
        extras.forEach(x => add(x.label, x.css || 'inherit', x.on, () => { x.pick(); paint(null); b.querySelector('span').textContent = x.label; }));
      }
      const recent = store('recentFonts').filter(l => labels.includes(l));
      if (recent.length) {
        p.append(h('div', 'ptitle', { textContent: 'Recent' }));
        recent.forEach(l => add(l, `'F_${l}'`, false, ph => { value = l; paint(l); onChange(l, ph); if (ph === 'commit') remember(l); }));
      }
      [...new Set(fonts.map(f => f.cat))].forEach(c => {
        p.append(h('div', 'ptitle', { textContent: c }));
        fonts.filter(f => f.cat === c).forEach(f => add(f.label, `'F_${f.label}'`, f.label === value, ph => { value = f.label; paint(f.label); onChange(f.label, ph); if (ph === 'commit') remember(f.label); }));
      });
      const vis = () => items.filter(it => !it.hidden);
      q.oninput = () => {
        const t = q.value.trim().toLowerCase();
        items.forEach(it => { it.hidden = !!t && !it._label.toLowerCase().includes(t); });
        p.querySelectorAll('.ptitle').forEach(pt => { pt.hidden = !!t; });
        const v = vis(); if (v.length) highlight(items.indexOf(v[0]), true);
      };
      const highlight = (i, preview) => {
        items[act]?.classList.remove('act'); act = i; const it = items[i]; if (!it) return;
        it.classList.add('act');
        if (it.offsetTop < p.scrollTop + 24) p.scrollTop = it.offsetTop - 30;
        else if (it.offsetTop + it.offsetHeight > p.scrollTop + p.clientHeight - 6) p.scrollTop = it.offsetTop + it.offsetHeight - p.clientHeight + 6;
        if (preview) it._pick('preview');
      };
      const commit = it => { done = true; it._pick('commit'); close(); };
      p.addEventListener('click', e => { const it = e.target.closest('.fo'); if (it) commit(it); });
      p.addEventListener('pointermove', e => { const it = e.target.closest('.fo'); if (it) highlight(items.indexOf(it), false); });
      p._key = e => {
        const k = e.key, n = items.length;
        const v = vis(), at = v.indexOf(items[act]), go = j => highlight(items.indexOf(v[j]), true);
        if (!v.length) return k !== 'Escape' && k !== 'Enter' ? false : (close(), true);
        if (k === 'ArrowDown' || k === 'ArrowUp') { go(((at < 0 ? 0 : at + (k === 'ArrowDown' ? 1 : -1)) + v.length) % v.length); return true; }
        if (k === 'Home' || k === 'End') { go(k === 'Home' ? 0 : v.length - 1); return true; }
        if (k === 'PageDown' || k === 'PageUp') { go(Math.max(0, Math.min(v.length - 1, at + (k === 'PageDown' ? 8 : -8)))); return true; }
        if (k === 'Enter' || k === 'Tab') { if (items[act]) commit(items[act]); else close(); return true; }
        if (k === 'Escape') { close(); return true; }
        if (document.activeElement === q) return false;  // typing filters the list
        if (k.length === 1 && /\S/.test(k)) {
          typed = (Date.now() - typedAt < 700 ? typed : '') + k.toLowerCase(); typedAt = Date.now();
          const j = items.findIndex(it => it._label.replace(/^★\s*/, '').toLowerCase().startsWith(typed));
          if (j >= 0) highlight(j, true);
          return true;
        }
        return false;
      };
      // closing without a commit puts the original font back
      p._close = () => { if (!done && act >= 0 && orig !== value) { value = orig; paint(orig); onChange(orig, 'revert'); } };
      const remember = l => store('recentFonts', [l, ...store('recentFonts').filter(x => x !== l)].slice(0, 5));
      requestAnimationFrame(() => { q.focus({ preventScroll: true }); highlight(Math.max(0, items.findIndex(it => it.classList.contains('sel'))), false); });
    });
    return b;
  }

  return { store, h, pop, closePop, swatch, seg, toggle, stepper, slider, fontPicker, PALETTE };
})();
