#!/usr/bin/env python3
import base64, difflib, glob, json, math, os, re, struct, sys, threading, uuid
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, unquote
import numpy as np
import pymupdf

ROOT = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(ROOT, "static")
SESSIONS = {}
SESS_DIR = os.environ.get("PDFED_SESSIONS", "/tmp/pdfed-sessions" if os.environ.get("VERCEL") else "/private/tmp/claude-501/pdfed-sessions")  # docs survive a server restart
os.makedirs(SESS_DIR, exist_ok=True)


def persist(sid, s):
    with open(os.path.join(SESS_DIR, sid + ".pdf"), "wb") as f:
        f.write(s.doc.tobytes())
    with open(os.path.join(SESS_DIR, sid + ".name"), "w") as f:
        f.write(s.name)
LOCK = threading.Lock()  # MuPDF objects aren't thread-safe
UNDO_LIMIT = 40
FONT_DIRS = [os.path.join(ROOT, "fonts"), "/System/Library/Fonts", "/Library/Fonts", os.path.expanduser("~/Library/Fonts"),
             "/usr/share/fonts", "/usr/local/share/fonts", os.path.expanduser("~/.fonts"), "C:/Windows/Fonts"]
B14 = {"he": ["helv", "heit", "hebo", "hebi"], "ti": ["tiro", "tiit", "tibo", "tibi"], "co": ["cour", "coit", "cobo", "cobi"]}


B14_NAMES = {"helv": "Helvetica", "hebo": "Helvetica Bold", "heit": "Helvetica Oblique", "hebi": "Helvetica Bold Oblique",
             "tiro": "Times", "tibo": "Times Bold", "tiit": "Times Italic", "tibi": "Times Bold Italic",
             "cour": "Courier", "cobo": "Courier Bold", "coit": "Courier Oblique", "cobi": "Courier Bold Oblique"}


def ps_name(buf):
    """PostScript name (name ID 6) from a TrueType/OpenType font; MuPDF reports text under this name."""
    try:
        n = struct.unpack(">H", buf[4:6])[0]
        for i in range(n):
            tag, _, off, _ = struct.unpack(">4sIII", buf[12 + 16 * i:28 + 16 * i])
            if tag == b"name":
                count, strings = struct.unpack(">HH", buf[off + 2:off + 6])
                for j in range(count):
                    plat, _, _, nid, ln, so = struct.unpack(">6H", buf[off + 6 + 12 * j:off + 18 + 12 * j])
                    if nid == 6:
                        raw = buf[off + strings + so:off + strings + so + ln]
                        return raw.decode("utf-16-be" if plat in (0, 3) else "latin-1")
    except (struct.error, UnicodeDecodeError):
        pass
    return None


def strip_subset(name):
    return name.split("+", 1)[-1]


def font_key(name):
    parts = re.split(r"[-,]", strip_subset(name), maxsplit=1)
    parts = [re.sub(r"(PSMT|MT|PS)$", "", p) for p in parts]
    return re.sub(r"[^a-z0-9]", "", "".join(parts).lower()).replace("regular", "")


_sysfonts = None


def sysfont(name):
    # shortcut: filename-based match, skips .ttc collections; use fontconfig if matches are poor.
    global _sysfonts
    if _sysfonts is None:
        _sysfonts = {}
        for d in FONT_DIRS:
            for p in glob.glob(os.path.join(d, "**", "*.[OoTt][Tt][Ff]"), recursive=True):
                _sysfonts.setdefault(font_key(os.path.splitext(os.path.basename(p))[0]), p)
    return _sysfonts.get(font_key(name))


DOC_FONTS = ["Arial", "Times New Roman", "Calibri", "Carlito", "Cambria", "Caladea", "Georgia", "Verdana", "Tahoma", "Trebuchet MS", "Garamond",
             "EB Garamond", "Book Antiqua", "Palatino Linotype", "Century Gothic", "Gill Sans MT", "Futura", "Avenir", "Roboto",
             "Open Sans", "Lato", "Montserrat", "Segoe UI", "Courier New", "Arial Narrow", "Arial Black", "Comic Sans MS",
             "Impact", "Franklin Gothic Medium", "Rockwell", "Bookman Old Style", "Brush Script MT", "Bradley Hand"]


def doc_fonts():
    # shortcut: only standalone .ttf/.otf files count (macOS .ttc families like Avenir/Futura are skipped).
    return [n for n in DOC_FONTS if sysfont(n) or sysfont(n + " Regular") or sysfont(n + " Bold")]


def family_file(label, bold, italic):
    style = (" Bold" if bold else "") + (" Italic" if italic else "")
    for name in ([label + style, label + style.replace("Italic", "Oblique")] if style else []) + [label, label + " Regular", label + " Bold"]:
        path = sysfont(name)
        if path:
            return path
    return None


def base14(name, bold, italic, family=None):
    n = name.lower()
    if not family:
        if "courier" in n or "mono" in n: family = "co"
        elif "times" in n or ("serif" in n and "sans" not in n): family = "ti"
        else: family = "he"
    return B14[family][bool(bold) * 2 + bool(italic)]


def style_of(name, flags):
    n = name.lower()
    bold = flags & 16 or any(w in n for w in ("bold", "black", "heavy", "semibold"))
    italic = flags & 2 or "italic" in n or "oblique" in n
    if not any(w in n for w in ("courier", "mono", "times", "serif")):
        n += " mono" if flags & 8 else (" times" if flags & 4 else "")
    return n, bold, italic


def hex2rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))


def _u16(h):
    try:
        return bytes.fromhex(h).decode("utf-16-be")
    except ValueError:
        return ""


def parse_tounicode(data):
    """Map unicode text -> hex char code, from a ToUnicode CMap."""
    m = {}
    for blk in re.findall(r"beginbfchar(.*?)endbfchar", data, re.S):
        for src, dst in re.findall(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]*)>", blk):
            m.setdefault(_u16(dst), src.upper())
    for blk in re.findall(r"beginbfrange(.*?)endbfrange", data, re.S):
        for lo, hi, dst in re.findall(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*(<[0-9A-Fa-f]*>|\[[^\]]*\])", blk):
            a, w = int(lo, 16), len(lo)
            if dst[0] == "<":
                base = _u16(dst[1:-1])
                for i in range(int(hi, 16) - a + 1):
                    if base:
                        m.setdefault(base[:-1] + chr(ord(base[-1]) + i), format(a + i, f"0{w}X"))
            else:
                for i, d in enumerate(re.findall(r"<([0-9A-Fa-f]*)>", dst)):
                    m.setdefault(_u16(d), format(a + i, f"0{w}X"))
    m.pop("", None)
    return m


def encode_text(cmap, text, space_gap=250):
    """TJ array body for text using only codes the font has; None if a glyph is missing."""
    out, i = [], 0
    while i < len(text):
        for n in (1, 3, 2):
            if text[i:i + n] in cmap and len(text[i:i + n]) == n:
                out.append(f"<{cmap[text[i:i + n]]}>")
                i += n
                break
        else:
            if text[i] != " ":
                return None
            out.append(str(-space_gap))  # font has no space glyph: shift instead
            i += 1
    return "[" + " ".join(out) + "]"


def append_content(page, data):
    doc = page.parent
    page.wrap_contents()
    x = doc.get_new_xref()
    doc.update_object(x, "<<>>")
    doc.update_stream(x, data)
    doc.xref_set_key(page.xref, "Contents", "[%s]" % " ".join(f"{r} 0 R" for r in page.get_contents() + [x]))


_cands = None


def font_candidates():
    global _cands
    if _cands is None:
        _cands = [(B14_NAMES[k], pymupdf.Font(k), k) for k in B14_NAMES]
        sysfont("")
        for path in sorted(set(_sysfonts.values())):
            try:
                f = pymupdf.Font(fontfile=path)
                if f.has_glyph(ord("a")) and f.has_glyph(ord("A")):
                    _cands.append((os.path.splitext(os.path.basename(path))[0], f, path))
            except Exception:
                pass
    return _cands


def gray(pix):
    return np.frombuffer(pix.samples, np.uint8).reshape(pix.height, pix.stride)[:, :pix.width].astype(np.float32)


def ink_crop(a):
    ys, xs = np.nonzero(a > 0.3 * a.max()) if a.max() > 0 else ((), ())
    return a[ys.min():ys.max() + 1, xs.min():xs.max() + 1] if len(ys) else None


def fit(a, shape):
    h, w = shape
    a = a[np.linspace(0, a.shape[0] - 1, h).astype(int)][:, np.linspace(0, a.shape[1] - 1, w).astype(int)]
    p = np.pad(a, 1, mode="edge")  # 3x3 blur absorbs JPEG noise and anti-aliasing differences
    return sum(p[i:i + h, j:j + w] for i in range(3) for j in range(3)) / 9


def ncc(a, b):
    a, b = a - a.mean(), b - b.mean()
    d = np.sqrt((a * a).sum() * (b * b).sum())
    return float((a * b).sum() / d) if d else -1.0


def scan_colors(page, r):
    pix = page.get_pixmap(matrix=pymupdf.Matrix(3, 3), clip=r, alpha=False)
    px = np.frombuffer(pix.samples, np.uint8).reshape(pix.height, pix.stride)[:, :pix.width * 3].reshape(pix.height, pix.width, 3)
    border = np.concatenate([px[0], px[-1], px[:, 0], px[:, -1]]).astype(np.float32)
    bg = np.median(border, axis=0)
    w = max(px.shape[1] // 6, 1)
    paper = lambda cols: np.median(cols.reshape(-1, 3)[np.abs(cols.reshape(-1, 3).astype(np.float32) - bg).sum(1) < 60], axis=0) \
        if (np.abs(cols.reshape(-1, 3).astype(np.float32) - bg).sum(1) < 60).any() else bg
    scan_colors.edges = (paper(px[:, :w]) / 255, paper(px[:, -w:]) / 255)
    flat = px.reshape(-1, 3).astype(np.float32)
    dist = np.abs(flat - bg).sum(1)
    ink = np.median(flat[dist >= np.percentile(dist, 92)], axis=0)
    return tuple(float(v) for v in bg / 255), tuple(float(v) for v in ink / 255)


def scan_baseline(page, r):
    k = 4
    a = 255 - gray(page.get_pixmap(matrix=pymupdf.Matrix(k, k), clip=r, colorspace=pymupdf.csGRAY))
    for y in rule_rows(a, k):  # a dotted leader or underline is not the baseline
        a[max(y - 2, 0):y + 3] = 0
    rows = a.sum(1)
    dense = np.nonzero(rows > 0.3 * max(rows.max(), 1))[0]
    return float(r.y0 + (dense[-1] + 1) / k) if len(dense) else r.y1


SPLIT_GAIN = 0.2
# Below this, the best-correlating font is a guess: substituting it is how a cursive name comes back
# in a heavy sans. Refusing to retype is recoverable; baking in the wrong face is not.
SCAN_MIN_MATCH = 0.45
RARE = re.compile(r"Math|Nerd|Symbol|Emoji|LastResort|Wingding|Webding|Braille|Ornament|Dingbat|LGL?DZ|LGM|LGS", re.I)


def line_baseline(page, r):
    """Baseline = where ink density falls off hardest below the x-height band (robust to descenders)."""
    k = 4
    a = 255 - gray(page.get_pixmap(matrix=pymupdf.Matrix(k, k), clip=r, colorspace=pymupdf.csGRAY))
    for y in rule_rows(a, k):
        a[max(y - 2, 0):y + 3] = 0
    rows = np.convolve(a.sum(1), np.ones(3) / 3, mode="same")
    if rows.max() <= 0:
        return r.y1
    drop = rows[:-1] - rows[1:]
    lo = len(rows) // 3
    i = lo + int(np.argmax(drop[lo:]))
    return float(r.y0 + (i + 1) / k)


def match_font(page, r, text, baseline, only=None, wide=True):
    """Render the line in candidate fonts and keep the one that looks most like the scan.

    Rendering all ~270 installed fonts costs seconds per word, so a metric-only prefilter (glyph
    proportions, no rasterising) narrows the field first. If that shortlist is unconvincing the full
    list is swept, so an unusual font still gets found.
    """
    k = 48 / r.height
    target = 255 - gray(page.get_pixmap(matrix=pymupdf.Matrix(k, k), clip=r, colorspace=pymupdf.csGRAY))
    for y in rule_rows(target, k):  # drop form underlines / dotted leaders, incl. their anti-aliased edges
        target[max(y - 2, 0):y + 3] = 0
    target = ink_crop(target)
    ranked = [(-2.0, "Helvetica", "helv", r.height * 0.8)]
    if target is None:
        return ranked
    target = fit(target, target.shape)
    chars = {ord(c) for c in text if not c.isspace()}
    cands = [c for c in font_candidates()
             if (only is None or c[0] in only) and all(c[1].has_glyph(ch) for ch in chars) and c[1].text_length(text, 1) > 0]
    if not cands:
        return ranked
    # proportions from font metrics alone: cheap, and a font that is far off in width-to-height
    # cannot win on shape, because the score already penalises |log(ratio)|
    want = target.shape[1] / target.shape[0]
    def off(c):
        return abs(np.log((c[1].text_length(text, 1) / max(c[1].ascender - c[1].descender, 1e-6)) / want))
    short = sorted(cands, key=off)[:80]
    top = _rank_fonts(target, short, text, r, k)
    if wide and (not top or top[0][0] < 0.75):  # nothing convincing in the shortlist: sweep everything
        top_wide = _rank_fonts(target, cands, text, r, k)
        if top_wide:
            top = top_wide
    ranked += top
    ranked.sort(key=lambda x: -x[0])
    seen, out = set(), []
    for c in ranked:  # best per family name, so the alternatives aren't five weights of one font
        fam = re.split(r"[ -]", c[1])[0]
        if fam not in seen:
            seen.add(fam)
            out.append(c)
    return out


def _rank_fonts(target, cands, text, r, k):
    """Score candidate fonts by rendering the text and correlating it with the scan."""
    out = []
    for label, font, ref in cands:
        try:
            length = font.text_length(text, 1)
            fs = r.width / length
            pg = pymupdf.open().new_page(width=r.width * 1.5 + 20, height=r.height * 3)
            # Render through an embedded font rather than TextWriter: a couple of installed faces
            # (script faces among them) refuse TextWriter and used to be skipped in silence, so a
            # cursive name could never be matched and a bold sans got substituted instead.
            if ref in B14_NAMES:
                pg.insert_text((10, r.height * 2), text, fontname=ref, fontsize=fs)
            else:
                pg.insert_font(fontname="F0", fontfile=ref)
                pg.insert_text((10, r.height * 2), text, fontname="F0", fontsize=fs)
            got = ink_crop(255 - gray(pg.get_pixmap(matrix=pymupdf.Matrix(k, k), colorspace=pymupdf.csGRAY)))
        except Exception:
            continue
        if got is None:
            continue
        ratio = (got.shape[1] / got.shape[0]) / (target.shape[1] / target.shape[0])
        score = ncc(target, fit(got, target.shape)) - abs(np.log(ratio))
        if RARE.search(label):
            score -= 0.05  # look-alike math / terminal / symbol fonts rarely appear in documents
        out.append((score, label, ref, float(fs * target.shape[1] / got.shape[1])))
    return out


def rule_rows(a, k):
    """Rows of a horizontal rule under/over text: solid lines anywhere, dotted leaders near the bottom.
    k = pixels per point of `a`."""
    cov = (a > 0.4 * max(a.max(), 1)).mean(1)
    n, rows, i = len(cov), [], 0
    low = np.arange(n) > 0.65 * n
    hit = (cov > 0.85) | (low & (cov > 0.2))
    while i < n:
        if hit[i]:
            j = i
            while j + 1 < n and hit[j + 1]:
                j += 1
            band = cov[i:j + 1]
            solid = band.max() > 0.85 and j - i + 1 <= max(3, 0.12 * n)
            gap_above = i > 0 and cov[max(i - max(1, int(k)), 0):i].min() < 0.08  # leaders sit below a blank gap
            dotted = low[i] and gap_above and j - i + 1 <= max(2, 1.6 * k) and band.max() < 0.85
            if solid or dotted:
                rows += range(i, j + 1)
            i = j + 1
        else:
            i += 1
    return np.array(rows, int)


def scan_dpi(page):
    """Dots per point of the page's background raster: the scan's own resolution."""
    for img in sorted(page.get_images(full=True), key=lambda i: -(i[2] * i[3]))[:1]:
        rects = page.get_image_rects(img[0])
        if rects and rects[0].width > 0 and img[2]:
            return min(max(img[2] / (rects[0].width / 72.0), 72.0), 1200.0)
    return 300.0


def blur(a, sigma):
    if sigma <= 0.05:
        return a
    rad = max(1, int(np.ceil(sigma * 3)))
    xs = np.arange(-rad, rad + 1)
    ker = np.exp(-(xs ** 2) / (2 * sigma * sigma))
    ker /= ker.sum()
    p = np.pad(a, ((rad, rad), (0, 0)), mode="edge")
    a = sum(ker[i] * p[i:i + a.shape[0]] for i in range(len(ker)))
    p = np.pad(a, ((0, 0), (rad, rad)), mode="edge")
    return sum(ker[i] * p[:, i:i + a.shape[1]] for i in range(len(ker)))


def clean_patch(page, cover):
    """(png, rect) of the paper under `cover` with its printed ink erased.

    Rules, underlines, grain and shading stay byte-identical, so erasing a word cannot leave a
    white box or eat the dotted leader the word sat on. The rect is snapped to whole pixels of the
    scan so the paste is seam-free at native resolution.
    """
    k = scan_dpi(page) / 72.0
    cover = pymupdf.Rect(np.floor(cover.x0 * k) / k, np.floor(cover.y0 * k) / k,
                         np.ceil(cover.x1 * k) / k, np.ceil(cover.y1 * k) / k)
    pix = page.get_pixmap(matrix=pymupdf.Matrix(k, k), clip=cover, alpha=False)
    a = np.frombuffer(pix.samples, np.uint8).reshape(pix.height, pix.stride)[:, :pix.width * 3]
    a = a[:, :pix.width * 3].reshape(pix.height, pix.width, 3).astype(np.float32)
    if not a.size or a.shape[1] < 4:
        return None, cover
    lum = a.mean(2)
    bg = float(np.median(np.concatenate([lum[0], lum[-1], lum[:, 0], lum[:, -1]])))
    ink = lum < bg - 40
    if not ink.any():
        return None, cover
    keep = np.zeros_like(ink)  # long horizontal runs are form rules: keep those pixels exactly
    w = ink.shape[1]
    for y in range(ink.shape[0]):
        row, cov = ink[y], ink[y].sum()
        if cov < 0.4 * w:
            continue
        edges = np.nonzero(np.diff(np.concatenate(([0], row.view(np.int8), [0]))))[0]
        for s, e in zip(edges[::2], edges[1::2]):
            if e - s >= max(6, int(0.3 * w)):
                keep[y, s:e] = True
    erase = ink & ~keep
    grow = erase.copy()
    for dy in (-1, 0, 1):  # swallow the anti-aliased halo around erased glyphs
        for dx in (-1, 0, 1):
            grow |= np.roll(np.roll(erase, dy, axis=0), dx, axis=1)
    erase = grow & ~keep
    if not erase.any():
        return None, cover
    paper = np.zeros((a.shape[1], 3), np.float32)  # per column, so gradients survive
    free = ~ink
    for x in range(a.shape[1]):
        col = a[free[:, x], x]
        paper[x] = np.median(col, axis=0) if len(col) else bg
    out = a.copy()
    yi, xi = np.nonzero(erase)
    out[yi, xi] = np.clip(paper[xi] + np.random.default_rng(11).normal(0, 2.2, (len(yi), 3)), 0, 255)
    png = pymupdf.Pixmap(pymupdf.csRGB, pix.width, pix.height, out.astype(np.uint8).tobytes(), False).tobytes("png")
    return png, cover


GLYPH_HI = 4.0  # glyph rasters are drawn at this multiple of the scan's dpi: embedded at scan
                # resolution they would be resampled again by every viewer and double-softened


def glyph_png(text, font, fs, color, dpi, sx=1.0, sigma=None, gain=1.0, hi=4.0):
    """RGBA PNG of `text` at the scan's resolution, softened like the scan's own ink.

    Vector glyphs dropped into a JPEG scan are the giveaway (razor edges, no grain), so replacement
    text is rasterised at the scan's dpi and blurred to match. `rect` is relative to the text origin;
    it also carries the ink's own left offset, so callers can line the new ink up with the old.
    `sigma` is the blur in pixels and `gain` scales ink strength: both are measured off the scan by
    fit_scan_text, so the new words carry the same weight as the ones around them.
    """
    # Rendered well above the scan's dpi: a raster embedded at scan resolution gets resampled again
    # by every viewer, which double-softens it against the vector-origin glyphs around it.
    k = max(dpi, 150.0) * max(hi, 1.0) / 72.0
    adv = font.text_length(text, fs)
    asc, desc = font.ascender * fs, font.descender * fs
    m = 2.0
    W, H = max(adv + 2 * m, 6), max(asc - desc + 2 * m, 6)
    tmp = pymupdf.open().new_page(width=W, height=H)
    tw = pymupdf.TextWriter(tmp.rect)
    tw.append((m, m + asc), text, font=font, fontsize=fs)
    tw.write_text(tmp)
    pix = tmp.get_pixmap(matrix=pymupdf.Matrix(k, k), alpha=True, colorspace=pymupdf.csRGB)
    px = np.frombuffer(pix.samples, np.uint8).reshape(pix.height, pix.stride)[:, :pix.width * 4]
    alpha = px.reshape(pix.height, pix.width, 4)[:, :, 3].astype(np.float32)
    alpha = blur(alpha, max(0.45, 0.13 * k) if sigma is None else sigma * max(hi, 1.0)) * float(gain)
    rgba = np.dstack([np.full_like(alpha, c * 255.0) for c in color] + [alpha])
    png = pymupdf.Pixmap(pymupdf.csRGB, pix.width, pix.height,
                         np.clip(rgba, 0, 255).astype(np.uint8).tobytes(), True).tobytes("png")
    # left side bearing and ink width of this very string, so ink (not the advance box) aligns
    cols = np.nonzero((alpha > 12).sum(0))[0]
    lsb = (int(cols[0]) / k - m) if len(cols) else 0.0
    ink_w = ((int(cols[-1]) - int(cols[0]) + 1) / k) if len(cols) else 0.0
    rect = pymupdf.Rect(-m, -m - asc, -m + 2 * m + adv * sx, -m - asc + H)
    return png, rect, lsb, ink_w


def _side_bearing(text, font, fs, color, page, sigma=None, gain=1.0):
    """Distance from the text origin to where this string's ink starts, in points."""
    try:
        return glyph_png(text, font, fs, color, scan_dpi(page), 1.0, sigma, gain)[2]
    except Exception:
        return 0.0


def ink_span(page, r):
    """(left, right) of the actual printed ink in `r`, ignoring rules and dotted leaders."""
    k = max(scan_dpi(page), 150.0) / 72.0
    a = 255 - gray(page.get_pixmap(matrix=pymupdf.Matrix(k, k), clip=r, colorspace=pymupdf.csGRAY))
    if not a.size:
        return r.x0, r.x1
    for y in rule_rows(a, k):
        a[max(y - 1, 0):y + 2] = 0
    cols = np.nonzero((a > max(0.3 * a.max(), 40)).sum(0) > 0)[0]
    if not len(cols):
        return r.x0, r.x1
    return float(r.x0 + (int(cols[0]) - 0.5) / k), float(r.x0 + (int(cols[-1]) + 0.5) / k)


def glyph_alpha(png, down=1):
    """Alpha channel of a glyph PNG (box-filtered down by `down`), plus where its ink starts."""
    pix = pymupdf.Pixmap(png)
    px = np.frombuffer(pix.samples, np.uint8).reshape(pix.height, pix.stride)[:, :pix.width * 4]
    a = px.reshape(pix.height, pix.width, 4)[:, :, 3].astype(np.float32)
    if down > 1:  # back to the scan's pixel grid, so comparisons are apples to apples
        h, w = (a.shape[0] // down) * down, (a.shape[1] // down) * down
        a = a[:h, :w].reshape(h // down, down, w // down, down).mean(axis=(1, 3))
    ys, xs = np.nonzero(a > 12)
    return a, (int(ys.min()), int(xs.min())) if len(ys) else (0, 0)


def fit_scan_text(page, r, text, font, color, bg, fs, base, sx=1.0, span=0.09, steps=5, refine=True):
    """Fontsize and softness that make a rendering sit in the scan like its own ink.

    Size comes from *shape*: fitting the advance width reads a few percent small on blurred ink
    (JPEG halos count as width), while correlating the glyphs themselves picks what the eye expects.
    Softness is scaled to the scan's resolution and then nudged within a narrow band — measurably
    wider sweeps make the result softer than the ink around it, not closer to it. Ink is drawn at
    full strength; the sampled ink colour already carries the scan's fade.
    Returns (fontsize, sigma_px, gain).
    """
    k = max(scan_dpi(page), 150.0) / 72.0
    ink_l, _ = ink_span(page, r)
    rr = pymupdf.Rect(np.floor(r.x0 * k) / k, np.floor(r.y0 * k) / k, np.ceil(r.x1 * k) / k, np.ceil(r.y1 * k) / k)
    pix = page.get_pixmap(matrix=pymupdf.Matrix(k, k), clip=rr, colorspace=pymupdf.csGRAY)
    tgt = 1.0 - np.frombuffer(pix.samples, np.uint8).reshape(pix.height, pix.stride)[:, :pix.width].astype(np.float32) / 255.0
    for y in rule_rows(tgt, k):  # the underline is not part of the glyphs
        tgt[max(y - 1, 0):y + 2] = 0.0
    sig0 = 0.13 * k  # scan-relative default: stroke softness scales with the raster's resolution
    if not tgt.any():
        return fs, sig0, 1.0
    if refine:
        pick = _fit_shapes(tgt, rr, text, font, color, fs * np.linspace(1 - span, 1 + span, steps), [sig0], k, base, ink_l, sx)
        if pick is None:
            return fs, sig0, 1.0
        f0, _ = pick
        # a denser second pass: near the optimum this score is steep, so 0.5% steps are worth it
        pick = _fit_shapes(tgt, rr, text, font, color, f0 * np.linspace(0.975, 1.025, 11), [sig0], k, base, ink_l, sx)
        fs = pick[0] if pick else f0
    f, sig = fs, sig0
    pick = _fit_shapes(tgt, rr, text, font, color, [fs], sig0 * np.array([1.0, 1.15, 1.3]), k, base, ink_l, sx)
    if pick:
        f, sig = pick
    # light ink gain: real ink spreads as it hits paper, so the strokes read a touch heavier than a
    # clean raster of the same shape. Measured on the fixture: it moves both match and ink weight up.
    return float(f), float(sig), 1.10


def _place(tgt, rr, text, font, color, f, sigma, k, base, ink_l, sx):
    """Rendering of `text` laid onto a canvas the size of the scan crop, at its measured position.

    Glyphs are drawn at GLYPH_HI times the scan's dpi, so the alpha is box-filtered back down to the
    scan's pixel grid before anything is compared with the scan.
    """
    try:
        png, rel, lsb, _ = glyph_png(text, font, f, color, k * 72.0, sx, sigma, 1.0, GLYPH_HI)
        a, (itop, ileft) = glyph_alpha(png, int(GLYPH_HI))
    except Exception:
        return None
    row_base = (2.0 + font.ascender * f) * k  # baseline row inside the glyph image
    y0 = int(round((base - rr.y0) * k + (itop - row_base)))  # ink top sits (row_base - itop) up
    x0 = int(round((ink_l - rr.x0) * k - ileft))
    canvas = np.zeros_like(tgt)
    sub = a[itop:, ileft:] / 255.0
    ys, xs = max(y0, 0), max(x0, 0)
    ye, xe = min(y0 + sub.shape[0], canvas.shape[0]), min(x0 + sub.shape[1], canvas.shape[1])
    if ye <= ys or xe <= xs:
        return None
    canvas[ys:ye, xs:xe] = sub[ys - y0:ye - y0, xs - x0:xe - x0]
    return canvas if canvas.any() else None


def _fit_shapes(tgt, rr, text, font, color, sizes, sigmas, k, base, ink_l, sx):
    """Best (size, sigma) by correlation with the scan, over a small alignment jitter."""
    best, best_pick = -9.0, None
    for f in sizes:
        for sigma in sigmas:
            canvas = _place(tgt, rr, text, font, color, f, sigma, k, base, ink_l, sx)
            if canvas is None:
                continue
            for dy in range(-3, 4):  # analytic placement is good to a couple of pixels; jitter finds the rest
                for dx in range(-3, 4):
                    c = np.roll(np.roll(canvas, dy, axis=0), dx, axis=1)
                    m = (tgt > 0.2) | (c > 0.2)
                    if m.sum() < 40:
                        continue
                    score = float(np.corrcoef(tgt[m], c[m])[0, 1])
                    if score > best:
                        best, best_pick = score, (float(f), float(sigma))
    return best_pick


LEADERS = re.compile(r"^[_„‚·…→«»|]+|[_·…→|]+$|\.{2,}$")


def page_rgb(page, dpi=150):
    pix = page.get_pixmap(dpi=dpi, alpha=False)
    return np.frombuffer(pix.samples, np.uint8).reshape(pix.height, pix.stride)[:, :pix.width * 3].reshape(pix.height, pix.width, 3), dpi / 72


def word_style(img, k, r):
    """(ink rgb, ink height) of one word, used to split printed labels from handwritten fills."""
    crop = img[max(int(r.y0 * k), 0):int(r.y1 * k) + 1, max(int(r.x0 * k), 0):int(r.x1 * k) + 1].reshape(-1, 3).astype(np.float32)
    if not len(crop):
        return None
    lum = crop.mean(1)
    dark = lum <= np.percentile(lum, 15)
    rows = (img[max(int(r.y0 * k), 0):int(r.y1 * k) + 1, max(int(r.x0 * k), 0):int(r.x1 * k) + 1].mean(2) < lum.mean() - 40).any(1)
    return np.median(crop[dark], axis=0), max(rows.sum(), 1)


def same_style(a, b):
    # Compare ink hue only (black print vs blue pen); lightness and height vary word to word.
    if a is None or b is None:
        return True
    return np.abs((a[0] - a[0].mean()) - (b[0] - b[0].mean())).sum() < 45


def split_runs(words, styles=None):
    """[(text, Rect)] words of one line -> runs; split on wide gaps (new field) or ink/size change (new font)."""
    runs, last = [], None
    for i, (text, r) in enumerate(words):
        text = LEADERS.sub("", text)
        if not text or (len(text) == 1 and text in "!|'`\"" and i in (0, len(words) - 1)):
            continue  # leader dots/underscores misread as stray punctuation
        st = styles[i] if styles else None
        if runs and r.x0 - runs[-1][1].x1 < 0.55 * max(r.height, runs[-1][1].height) and same_style(st, last):
            runs[-1] = (runs[-1][0] + " " + text, runs[-1][1] | r)
        else:
            runs.append((text, pymupdf.Rect(r)))
        last = st if st is not None else last
    runs = [(re.sub(r"['`\"]+$", "", t) if j == len(runs) - 1 else t, r) for j, (t, r) in enumerate(runs)]  # OCR-invented quote at line end
    return [(t, r) for t, r in runs if t and r.height <= 1.5 * r.width]  # drop vertical text


def vision_lines(page, clip=None):
    """[[(word, Rect)]] per recognised line, in display coordinates."""
    import Vision
    from Foundation import NSData, NSMakeRange
    clip = pymupdf.Rect(clip or page.rect)
    png = page.get_pixmap(dpi=300, clip=clip).tobytes("png")
    handler = Vision.VNImageRequestHandler.alloc().initWithData_options_(NSData.dataWithBytes_length_(png, len(png)), None)
    req = Vision.VNRecognizeTextRequest.alloc().init()
    req.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
    req.setUsesLanguageCorrection_(True)
    handler.performRequests_error_([req], None)
    W, H = clip.width, clip.height
    lines = []
    for obs in req.results() or []:
        cand = obs.topCandidates_(1)[0]
        if cand.confidence() < 0.35:
            continue  # signatures, stamps, noise
        line, words = cand.string(), []
        for m in re.finditer(r"\S+", line):
            box, _ = cand.boundingBoxForRange_error_(NSMakeRange(m.start(), m.end() - m.start()), None)
            b = box.boundingBox()
            words.append((m.group(), pymupdf.Rect(clip.x0 + b.origin.x * W, clip.y0 + (1 - b.origin.y - b.size.height) * H,
                                                  clip.x0 + (b.origin.x + b.size.width) * W, clip.y0 + (1 - b.origin.y) * H)))
        lines.append(words)
    return lines


def ocr_vision(page):
    img, k = page_rgb(page)
    return [run for words in vision_lines(page) for run in split_runs(words, [word_style(img, k, r) for _, r in words])]


def word_boxes(page, r, text):
    """Tight per-word boxes inside one run, or None if they can't be aligned with the run's words."""
    want = text.split()
    try:
        words = sorted((w for line in vision_lines(page, r + (-2, -3, 2, 3)) for w in line), key=lambda w: w[1].x0)
    except ImportError:
        words = []
    if len(words) == len(want):
        return [w[1] & r for w in words]
    # fallback: split the run box in proportion to character counts
    total, x, out = sum(len(w) + 1 for w in want) - 1, r.x0, []
    for w in want:
        x1 = x + r.width * len(w) / total
        out.append(pymupdf.Rect(x, r.y0, x1, r.y1))
        x = x1 + r.width / total
    return out


def ocr_tesseract(page, lang):
    tp = page.get_textpage_ocr(dpi=300, full=True, language=lang)
    img, k = page_rgb(page)
    out = []
    for blk in page.get_text("dict", textpage=tp)["blocks"]:
        for line in blk.get("lines", []):
            words = [(w["text"].strip(), pymupdf.Rect(w["bbox"]) * page.rotation_matrix) for w in line["spans"] if w["text"].strip()]
            out += split_runs(words, [word_style(img, k, r) for _, r in words])
    return out


def ocr_runs(page, lang):
    """Text runs in display coordinates: Apple Vision on macOS (better on mixed/handwritten fonts), else Tesseract."""
    if sys.platform == "darwin":
        try:
            return ocr_vision(page)
        except ImportError:
            pass
    return ocr_tesseract(page, lang)


def unrot(page, obj):
    return obj * page.derotation_matrix if page.rotation else obj


# Fonts offered for new/restyled text: label -> category. Files come from the system or ./fonts.
FONT_LIST = [("Arial", "Sans"), ("Verdana", "Sans"), ("Trebuchet MS", "Sans"), ("Tahoma", "Sans"), ("Carlito", "Sans"),
             ("Georgia", "Serif"), ("Times New Roman", "Serif"), ("Caladea", "Serif"),
             ("Courier New", "Mono"), ("Andale Mono", "Mono"),
             ("Arial Black", "Display"), ("Impact", "Display"), ("Skia", "Display"), ("Herculanum", "Display"),
             ("Trattatello", "Display"), ("Luminari", "Display"), ("Chalkduster", "Display"),
             ("Lucida Handwriting", "Handwriting"), ("Courgette", "Handwriting"), ("Kalam", "Handwriting"),
             ("Comic Sans MS", "Handwriting"), ("Bradley Hand", "Handwriting"), ("Brush Script MT", "Handwriting"),
             ("Apple Chancery", "Handwriting"), ("Zapfino", "Handwriting")]


def font_list():
    out = []
    for label, cat in FONT_LIST:
        reg = family_file(label, False, False)
        if reg:
            b, i, bi = family_file(label, True, False), family_file(label, False, True), family_file(label, True, True)
            out.append({"label": label, "cat": cat, "b": b != reg, "i": i != reg, "bi": bi not in (None, reg, b, i), **font_metrics(reg),
                        "m": {f"{bb}{ii}": font_metrics(family_file(label, bb, ii) or reg) for bb in (0, 1) for ii in (0, 1)}})
    return out


def os2_strike(path):
    # (strikeout centre, thickness) in em from the OS/2 + head tables; fallback is typical Latin metrics.
    try:
        buf = open(path, "rb").read()
        n = struct.unpack(">H", buf[4:6])[0]
        tabs = {buf[12 + 16 * i:16 + 16 * i]: struct.unpack(">I", buf[20 + 16 * i:24 + 16 * i])[0] for i in range(n)}
        upm = struct.unpack(">H", buf[tabs[b"head"] + 18:tabs[b"head"] + 20])[0]
        size, pos = struct.unpack(">hh", buf[tabs[b"OS/2"] + 26:tabs[b"OS/2"] + 30])
        return (pos + size / 2) / upm, size / upm
    except Exception:
        return 0.3, 0.05


def _rgb(h):
    return hex2rgb(h) if h else None


def _box_matrix(page, o):
    # displayed-page coords -> rotated about the box centre -> unrotated page space (what Shape draws in)
    cx, cy = o["x"] + o["w"] / 2, o["y"] + o["h"] / 2
    m = pymupdf.Matrix(1, 0, 0, 1, -cx, -cy) * pymupdf.Matrix(o.get("rot", 0) or 0) * pymupdf.Matrix(1, 0, 0, 1, cx, cy)
    return m * page.derotation_matrix


def _text_matrix(page, tw, m):
    # TextWriter emits PDF-space text; map it through m (displayed->unrotated) and undo write_text's own shift.
    cb = page.cropbox_position
    delta = page.rect.height - page.rect.width if page.rotation in (90, 270) else 0
    shift = pymupdf.Matrix(1, 0, 0, 1, cb.x, cb.y + page.mediabox.y0 - delta)
    return ~tw.ictm * m * ~page.transformation_matrix * ~shift


def _rgba(h):
    # '#rrggbb' or '#rrggbbaa' -> ((r, g, b), alpha); None stays None
    if not h:
        return None, 1.0
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)), (int(h[6:8], 16) / 255 if len(h) >= 8 else 1.0)


def post_underline(path):
    # (top of underline below baseline, thickness) in em from the 'post' + 'head' tables
    try:
        buf = open(path, "rb").read()
        n = struct.unpack(">H", buf[4:6])[0]
        tabs = {buf[12 + 16 * i:16 + 16 * i]: struct.unpack(">I", buf[20 + 16 * i:24 + 16 * i])[0] for i in range(n)}
        upm = struct.unpack(">H", buf[tabs[b"head"] + 18:tabs[b"head"] + 20])[0]
        pos, size = struct.unpack(">hh", buf[tabs[b"post"] + 8:tabs[b"post"] + 12])
        return -pos / upm, max(size, 1) / upm
    except Exception:
        return 0.1, 0.05


_metrics = {}


def font_metrics(path):
    if path not in _metrics:
        f = pymupdf.Font(fontfile=path)
        pos, size = os2_strike(path)
        ul, uls = post_underline(path)
        _metrics[path] = {"asc": f.ascender, "desc": f.descender, "strike": pos, "strikeSize": size, "ul": ul, "ulSize": uls}
    return _metrics[path]


_ink = {}


def glyph_ink(path, c, top, bot):
    # x-runs (em, from the glyph origin) where the glyph has ink between top..bot em below the baseline
    key = (path, c, round(top, 3), round(bot, 3))
    if key not in _ink:
        E = 200.0
        doc = pymupdf.open(); pg = doc.new_page(width=E * 3, height=E * 3)
        tw = pymupdf.TextWriter(pg.rect); tw.append((E, E * 1.5), c, font=pymupdf.Font(fontfile=path), fontsize=E); tw.write_text(pg)
        y0, y1 = int(E * 1.5 + top * E), int(E * 1.5 + bot * E) + 1
        pix = pg.get_pixmap(clip=pymupdf.IRect(0, y0, int(E * 3), max(y1, y0 + 1)), colorspace=pymupdf.csGRAY, alpha=False)
        a = np.frombuffer(pix.samples, np.uint8).reshape(pix.h, pix.w)
        cols = np.where(a.min(axis=0) < 140)[0]
        runs, start = [], None
        for i, x in enumerate(cols):
            if start is None: start = x
            if i == len(cols) - 1 or cols[i + 1] != x + 1:
                runs.append(((start - E) / E, (x + 1 - E) / E)); start = None
        _ink[key] = runs
    return _ink[key]


def line_style(style, w):
    # dash pattern + cap shared with the browser preview (app.js lineDash)
    return {"dotted": ("[0 %g] 0" % (2 * w), 1), "dashed": ("[%g %g] 0" % (3 * w, 2 * w), 0), "longdash": ("[%g %g] 0" % (7 * w, 3 * w), 0),
            "dashdot": ("[%g %g 0 %g] 0" % (5 * w, 2 * w, 2 * w), 1)}.get(style, (None, 0))


def wave(x0, x1, y, w):
    amp, L = max(1.0, w * 1.2), max(4.0, w * 5)
    n = max(2, int((x1 - x0) / (L / 8)) + 1)
    return [(x0 + (x1 - x0) * i / (n - 1), y + amp * math.sin(2 * math.pi * ((x1 - x0) * i / (n - 1)) / L)) for i in range(n)]


def deco_line(sh, M, x0, x1, y, style, w, rgb, alpha):
    if x1 <= x0:
        return
    ys = [y - w, y + w] if style == "double" else [y]
    for yy in ys:
        if style == "wavy":
            sh.draw_polyline([pymupdf.Point(p) * M for p in wave(x0, x1, yy, w)])
            sh.finish(color=rgb, width=w, stroke_opacity=alpha, closePath=False, lineCap=1, lineJoin=1)
            continue
        dashes, cap = line_style(style, w)
        sh.draw_line(pymupdf.Point(x0, yy) * M, pymupdf.Point(x1, yy) * M)
        sh.finish(color=rgb, width=w, stroke_opacity=alpha, dashes=dashes, lineCap=cap, closePath=False)


def _restroke(page, width, rgb):
    # TextWriter strokes at 5% of the font size in the fill colour; rewrite its last stream with ours.
    doc, x = page.parent, page.get_contents()[-1]
    st = doc.xref_stream(x).decode("latin-1")
    st = re.sub(r"[\d.]+ w\n", "%g w\n" % width, st)
    st = re.sub(r"[\d.]+ [\d.]+ [\d.]+ RG", "%g %g %g RG" % rgb, st)
    doc.update_stream(x, st.encode("latin-1"))


def skip_ink(xs, xe, placed, path, yy, w, base):
    # gaps where glyph ink crosses the underline band, with a small margin (mirrored in app.js)
    gaps = []
    for c, gx, gs in placed:
        if not c.strip():
            continue
        for a, b in glyph_ink(path, c, (yy - w / 2 - base) / gs, (yy + w / 2 - base) / gs):
            m = max(w * 0.8, gs * 0.03)
            gaps.append((gx + a * gs - m, gx + b * gs + m))
    out, cur = [], xs
    for a, b in sorted(gaps):
        if a > cur: out.append((cur, min(a, xe)))
        cur = max(cur, b)
    if xe > cur: out.append((cur, xe))
    return [(a, b) for a, b in out if b - a > w * 0.6]


def wrap_lines(text, font, fs, spacing, width):
    lines = []
    for para in text.split("\n"):
        cur = ""
        for w in para.split(" "):
            cand = w if not cur else cur + " " + w
            if cur and font.text_length(cand, fs) + spacing * len(cand) > width:
                lines.append([cur, False]); cur = w
            else:
                cur = cand
        lines.append([cur, True])
    return lines


def _cased(line, case):
    if case == "upper": return line.upper()
    if case == "lower": return line.lower()
    if case == "title": return re.sub(r"(^|\s)(\S)", lambda m: m.group(1) + m.group(2).upper(), line)
    return line


def draw_text_obj(page, o):
    bold, italic = bool(o.get("bold")), bool(o.get("italic"))
    label = o.get("font") or "Arial"
    reg = family_file(label, False, False) or family_file("Arial", False, False)
    path = family_file(label, bold, italic) or reg
    # missing bold/italic files are synthesised, as the browser does
    fake_b = bold and family_file(label, True, italic) in (None, family_file(label, False, italic))
    fake_i = italic and family_file(label, bold, True) in (None, family_file(label, bold, False))
    font = pymupdf.Font(fontfile=path)
    met = font_metrics(path)
    size = float(o.get("size", 12))
    script = o.get("script")
    fs = size * (0.65 if script else 1)
    shift = {"sup": -0.35 * size, "sub": 0.15 * size}.get(script, 0)
    lh = size * float(o.get("lineHeight", 1.2))
    sp, wsp = float(o.get("spacing", 0)), float(o.get("wordSpacing", 0))
    x0, y0, w, h = o["x"], o["y"], o["w"], o["h"]
    M = _box_matrix(page, o)
    op = float(o.get("opacity", 1))
    bd = o.get("border") or {}
    bw = float(bd.get("width", 1)) if bd.get("color") else 0
    sides = bd.get("sides", "trbl") if bw else ""
    pad = float(o.get("padding", 4))
    rad = float(o.get("radius", 0))
    box = pymupdf.Rect(x0, y0, x0 + w, y0 + h)
    # box: shadow, fill, border (rounded boxes are drawn axis-aligned then turned about the centre)
    cen = pymupdf.Point(x0 + w / 2, y0 + h / 2) * page.derotation_matrix
    morph = (cen, pymupdf.Matrix(o.get("rot", 0) or 0))
    def rect_path(sh, r):
        rr = (r * page.derotation_matrix).normalize()
        sh.draw_rect(rr, radius=(min(0.5, rad / rr.width), min(0.5, rad / rr.height)) if rad else None)
    bs = o.get("boxShadow") or {}
    sh = page.new_shape()
    if bs.get("on"):
        c, a = _rgba(bs.get("color") or "#00000040")
        rect_path(sh, box + (bs.get("x", 0), bs.get("y", 4), bs.get("x", 0), bs.get("y", 4)))
        sh.finish(color=None, fill=c, fill_opacity=a * op, morph=morph)
    bg, bga = _rgba(o.get("bg"))
    if bg:
        rect_path(sh, box)
        sh.finish(color=None, fill=bg, fill_opacity=bga * op, morph=morph)
    if bw:
        bc, bca = _rgba(bd.get("color"))
        dashes, cap = line_style(bd.get("style", "solid"), bw)
        if sides == "trbl":
            rect_path(sh, box + (bw / 2, bw / 2, -bw / 2, -bw / 2))
            sh.finish(color=bc, width=bw, stroke_opacity=bca * op, dashes=dashes, lineCap=cap, closePath=True, morph=morph)
        else:
            i = bw / 2
            seg = {"t": ((x0, y0 + i), (x0 + w, y0 + i)), "b": ((x0, y0 + h - i), (x0 + w, y0 + h - i)),
                   "l": ((x0 + i, y0), (x0 + i, y0 + h)), "r": ((x0 + w - i, y0), (x0 + w - i, y0 + h))}
            for k in sides:
                a_, b_ = seg[k]
                sh.draw_line(pymupdf.Point(a_) * M, pymupdf.Point(b_) * M)
                sh.finish(color=bc, width=bw, stroke_opacity=bca * op, dashes=dashes, lineCap=cap, closePath=False)
    sh.commit(overlay=True)

    color, ca = _rgba(o.get("color") or "#000000")
    case, small = o.get("case"), bool(o.get("smallCaps"))
    lines = o.get("lines") or wrap_lines(o.get("text", ""), font, fs, sp, w - 2 * pad)
    indent, first = float(o.get("indent", 0)), float(o.get("firstIndent", 0))
    lst = o.get("list")
    li = 18 if lst else 0
    pb, pa = float(o.get("paraBefore", 0)), float(o.get("paraAfter", 0))
    inner_w = w - 2 * pad
    # vertical layout: paragraphs are runs of lines ending in a hard break
    nparas = sum(1 for _, hard in lines if hard)
    total = len(lines) * lh + nparas * (pb + pa)
    vy = {"middle": (h - 2 * pad - total) / 2, "bottom": h - 2 * pad - total}.get(o.get("valign"), 0)
    y = y0 + pad + max(0, vy) if o.get("valign") in ("middle", "bottom") else y0 + pad
    ul, st, ov = o.get("underline") or {}, o.get("strike") or {}, o.get("overline") or {}
    shd, out = o.get("shadow") or {}, o.get("outline") or {}
    marker, mka = _rgba(o.get("marker"))
    laid = []  # (x_start, x_end, base, glyph list)
    para_first, num = True, 0
    for line, hard in lines:
        if para_first:
            y += pb; num += 1
        # Typed trailing spaces stay (so underline/strike/marker cover them); soft-wrap spaces go.
        line = _cased(line if hard else line.rstrip(" "), case)
        glyphs, cx = [], 0.0
        for c in line:
            gs = fs
            if small and c.islower():
                c, gs = c.upper(), fs * 0.7
            adv = font.text_length(c, gs) + sp + (wsp if c == " " else 0)
            glyphs.append((c, cx, gs)); cx += adv
        total_w = cx
        lead = indent + li + (first if para_first else 0)
        avail = inner_w - lead
        align = o.get("align", "left")
        xs = x0 + pad + lead + {"center": (avail - total_w) / 2, "right": avail - total_w}.get(align, 0)
        gap = (avail - total_w) / line.count(" ") if align == "justify" and not hard and line.count(" ") else 0
        k, placed = 0.0, []
        for c, gx, gs in glyphs:
            placed.append((c, xs + gx + k, gs))
            if c == " ": k += gap
        base = y + (lh - (met["asc"] - met["desc"]) * fs) / 2 + met["asc"] * fs + shift
        laid.append((xs, xs + total_w + k, base, placed, para_first and lst, num))
        y += lh
        para_first = hard
        if hard:
            y += pa
    def write(dx, dy, rgb, alpha, mode=0, stroke=None):
        tw = pymupdf.TextWriter(page.rect)
        for xs, xe, base, placed, mark, n in laid:
            for c, gx, gs in placed:
                if c.strip():
                    tw.append((gx + dx, base + dy), c, font=font, fontsize=gs)
            if mark:
                tw.append((x0 + pad + indent + dx, base + dy), "•" if lst == "bullet" else f"{n}.", font=font, fontsize=fs)
        mm = M
        if fake_i:  # synthetic oblique, sheared about the box's first baseline like the browser
            b0 = laid[0][2] if laid else y0
            mm = pymupdf.Matrix(1, 0, 0, 1, -x0, -b0) * pymupdf.Matrix(1, 0, -0.2, 1, 0, 0) * pymupdf.Matrix(1, 0, 0, 1, x0, b0) * M
        if not laid:
            return
        tw.write_text(page, color=rgb, opacity=alpha, matrix=_text_matrix(page, tw, mm), render_mode=mode)
        if stroke:
            _restroke(page, *stroke)
    sh = page.new_shape()
    if marker:  # marker highlight behind each line's text
        for xs, xe, base, placed, *_ in laid:
            if xe > xs:
                sh.draw_quad(pymupdf.Rect(xs, base - met["asc"] * fs, xe, base - met["desc"] * fs).quad * M)
                sh.finish(color=None, fill=marker, fill_opacity=mka * op)
    sh.commit(overlay=True)
    if shd.get("on"):
        c, a = _rgba(shd.get("color") or "#00000066")
        write(float(shd.get("x", 1)), float(shd.get("y", 1)), c, a * op)
    sh = page.new_shape()
    for spec, kind in ((ul, "u"), (ov, "o")):
        if not spec.get("on"):
            continue
        dw = float(spec.get("width") or max(0.5, fs * met["ulSize"]))
        rgb, a = _rgba(spec.get("color")) if spec.get("color") else (color, ca)
        off = spec.get("offset")
        for xs, xe, base, placed, *_ in laid:
            yy = base + (fs * met["ul"] if off is None else float(off)) + dw / 2 if kind == "u" else base - met["asc"] * fs + dw / 2 - float(off or 0)
            for a0, a1 in skip_ink(xs, xe, placed, path, yy, dw, base) if kind == "u" and spec.get("skipInk") else [(xs, xe)]:
                deco_line(sh, M, a0, a1, yy, spec.get("style", "solid"), dw, rgb, a * op)
    sh.commit(overlay=True)
    if out.get("on"):
        oc, _ = _rgba(out.get("color") or "#000000")
        write(0, 0, color, ca * op, 2 if not fake_b else 2, (float(out.get("width", 1)), oc))
    else:
        write(0, 0, color, ca * op, 2 if fake_b else 0)
    sh = page.new_shape()
    if st.get("on"):
        dw = float(st.get("width") or max(0.75, fs * met["strikeSize"]))
        rgb, a = _rgba(st.get("color")) if st.get("color") else (color, ca)
        pos = met["strike"] if st.get("position") is None else float(st["position"])
        for xs, xe, base, *_ in laid:
            deco_line(sh, M, xs, xe, base - pos * fs, st.get("style", "solid"), dw, rgb, a * op)
    sh.commit(overlay=True)
    if o.get("link"):
        page.insert_link({"kind": pymupdf.LINK_URI, "from": (box.quad * M).rect, "uri": o["link"]})


def _pts(o, key="pts"):
    return [(o["x"] + px * o["w"], o["y"] + py * o["h"]) for px, py in o[key]]


def draw_obj(page, o):
    t = o["type"]
    if t == "text":
        return draw_text_obj(page, o)
    M = _box_matrix(page, o)
    P = lambda x, y: pymupdf.Point(o["x"] + x * o["w"], o["y"] + y * o["h"]) * M
    r = pymupdf.Rect(o["x"], o["y"], o["x"] + o["w"], o["y"] + o["h"])
    quad = r.quad * M
    op = float(o.get("opacity", 1))
    (stroke, sa), (fill, fa) = _rgba(o.get("stroke")), _rgba(o.get("fill"))
    width = float(o.get("width", 2))
    if t == "whiteout":  # really remove what's underneath, then paint the cover colour
        page.add_redact_annot(quad.rect, fill=False)
        page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_PIXELS, graphics=pymupdf.PDF_REDACT_LINE_ART_NONE)
    if t == "highlight":
        a = page.add_highlight_annot(quad)
        a.set_colors(stroke=fill or (1, 0.88, 0.3)); a.set_opacity(op if op < 1 else 0.5); a.update()
        return
    if t == "note":
        a = page.add_text_annot(pymupdf.Point(o["x"], o["y"]) * page.derotation_matrix, o.get("text", ""), icon="Note")
        a.set_colors(stroke=fill or (1, 0.8, 0.2)); a.update()
        return
    if t == "image":
        # shortcut: images rotate in 90-degree steps only (insert_image limit)
        q = round((o.get("rot") or 0) / 90) * 90
        page.insert_image((r * page.derotation_matrix).normalize(), stream=base64.b64decode(o["data"].split(",")[-1]),
                          keep_proportion=False, rotate=(-q - page.rotation) % 360)
        return
    sh = page.new_shape()
    kw = dict(width=width, stroke_opacity=op * sa, fill_opacity=op * fa, lineCap=1, lineJoin=1)
    if t == "whiteout":
        sh.draw_quad(quad); sh.finish(color=None, fill=fill or (1, 1, 1), **kw)
    elif t == "rect":
        sh.draw_quad(quad); sh.finish(color=stroke, fill=fill, **kw)
    elif t == "ellipse":
        sh.draw_oval(quad); sh.finish(color=stroke, fill=fill, **kw)
    elif t in ("line", "arrow"):
        (ax, ay), (bx, by) = o["pts"]
        A, B = P(ax, ay), P(bx, by)
        sh.draw_line(A, B); sh.finish(color=stroke, closePath=False, **kw)
        if t == "arrow":
            ang, L = math.atan2(B.y - A.y, B.x - A.x), max(8, width * 4)
            sh.draw_polyline([B] + [(B.x - L * math.cos(ang + d), B.y - L * math.sin(ang + d)) for d in (0.45, -0.45)])
            sh.finish(color=stroke, fill=stroke, closePath=True, **kw)
    elif t in ("ink", "check", "cross"):
        for path in o["paths"]:
            sh.draw_polyline([P(px, py) for px, py in path])
            sh.finish(color=stroke, closePath=False, **kw)
    sh.commit(overlay=True)


def render_objects(data, objs):
    doc = pymupdf.open("pdf", data)
    for o in objs:
        if 0 <= o.get("page", -1) < len(doc):
            draw_obj(doc[o["page"]], o)
    return doc


class Session:
    def __init__(self, data, name):
        self.name = name
        self.undo, self.redo = [], []
        self.load(data)
        self.seen = None
        self.font_files = {}  # PDF base font name -> installed file we embedded it from
        self.painted = {}  # (page, text, x, y) -> what we drew: invisible text loses its colour in MuPDF

    def load(self, data):
        doc = pymupdf.open("pdf", data)
        if doc.needs_pass and not doc.authenticate(""):
            raise ValueError("PDF is password-protected")
        self.doc = doc
        self.fonts = None  # stripped name -> [xref]
        self.cache = {}  # xref -> (buffer, Font)
        self.painted = {}

    def snapshot(self):
        self.undo.append(self.doc.tobytes())
        del self.undo[:-UNDO_LIMIT]
        self.redo.clear()

    def pages(self):
        return [{"w": p.rect.width, "h": p.rect.height} for p in self.doc]

    def embedded(self, name):
        if self.fonts is None:
            self.fonts = {}
            for i in range(len(self.doc)):
                for f in self.doc.get_page_fonts(i):
                    names = {strip_subset(f[3])}
                    if f[1] != "n/a":
                        names.add(ps_name(self.font_obj(f[0])[0]))
                    for nm in names - {None}:
                        xrefs = self.fonts.setdefault(nm, [])
                        if f[1] != "n/a" and f[0] not in xrefs:
                            xrefs.append(f[0])
        return self.fonts.get(name, [])

    def font_obj(self, xref):
        if xref not in self.cache:
            buf = self.doc.extract_font(xref)[3]
            try: self.cache[xref] = (buf, pymupdf.Font(fontbuffer=buf))
            except Exception: self.cache[xref] = (buf, None)
        return self.cache[xref]

    def cmap(self, xref):
        key = ("cmap", xref)
        if key not in self.cache:
            t, v = self.doc.xref_get_key(xref, "ToUnicode")
            self.cache[key] = parse_tounicode(self.doc.xref_stream(int(v.split()[0])).decode("latin-1")) if t == "xref" else {}
        return self.cache[key]

    def drawn_chars(self, name):
        # Subsets may keep a full ToUnicode but drop outlines, so trust only glyphs the original drew.
        # shortcut: keyed by font name, so same-named subsets share one set; key by xref if that bites.
        if self.seen is None:
            self.seen = {}
            for page in self.doc:
                for sp in page.get_texttrace():
                    self.seen.setdefault(sp["font"], set()).update(chr(c[0]) for c in sp["chars"])
        return self.seen.get(name, set())

    def write_with_original(self, page, it):
        """Write text with the PDF's own font object (exact font, even for cmap-less subsets)."""
        seen = self.drawn_chars(it["font"])
        for xref in self.embedded(it["font"]):
            tj = encode_text({k: v for k, v in self.cmap(xref).items() if k in seen}, it["text"])
            if tj is None:
                continue
            ref = next((f[4] for f in page.get_fonts(full=True) if f[0] == xref and f[5] == 0), None)
            if not ref:
                ref = f"PE{xref}"
                self.doc.xref_set_key(page.xref, f"Resources/Font/{ref}", f"{xref} 0 R")
            m = page.derotation_matrix * ~page.transformation_matrix
            o = pymupdf.Point(it["origin"])
            p0, ex, ey = o * m, (o + (1, 0)) * m, (o + (0, -1)) * m
            ex, ey = ex - p0, ey - p0
            r, g, b = hex2rgb(it["color"])
            append_content(page, (f"q BT /{ref} {it['size']:g} Tf {r:.4f} {g:.4f} {b:.4f} rg "
                                  f"{ex.x:.5f} {ex.y:.5f} {ey.x:.5f} {ey.y:.5f} {p0.x:.3f} {p0.y:.3f} Tm {tj} TJ ET Q").encode())
            return True
        return False

    def font_file(self, name):
        return self.font_files.get(name) or sysfont(name)

    def remember_file(self, page, ref, path):
        for f in page.get_fonts():
            if f[4] == ref:
                self.font_files[strip_subset(f[3])] = path
        name = ps_name(open(path, "rb").read())
        if name:
            self.font_files[name] = path
        self.fonts = None  # new embedded font: rebuild the name index

    def pick_font(self, page, name, flags, text, family=None, bold=None, italic=None):
        n, b, i = style_of(name, flags)
        if family and family.startswith("f:"):
            path = family_file(family[2:], bold, italic)
            if path:
                ref = "D" + font_key(os.path.basename(path))[:20]
                page.insert_font(fontname=ref, fontfile=path)
                self.remember_file(page, ref, path)
                return ref, "chosen"
            family = None
        if family:
            return base14(n, bold, italic, family), "fallback"
        chars = {ord(c) for c in text if c not in "\n\r"}
        for xref in self.embedded(name):
            buf, f = self.font_obj(xref)
            if f and all(f.has_glyph(c) for c in chars):
                try:
                    page.insert_font(fontname=f"E{xref}", fontbuffer=buf)
                    return f"E{xref}", "exact"
                except Exception:
                    pass
        path = self.font_file(name)
        if path:
            try:
                if all(pymupdf.Font(fontfile=path).has_glyph(c) for c in chars):
                    ref = "S" + font_key(name)[:20]
                    page.insert_font(fontname=ref, fontfile=path)
                    self.remember_file(page, ref, path)
                    return ref, "system"
            except Exception:
                pass
        return base14(n, b, i), "fallback"

    def spans(self, n):
        page = self.doc[n]
        out = []
        rm = page.rotation_matrix
        for blk in page.get_text("dict")["blocks"]:
            for line in blk.get("lines", []):
                d = pymupdf.Point(line["dir"]) * rm - pymupdf.Point(0, 0) * rm
                if abs(d.x - 1) > 1e-3:
                    continue  # shortcut: text not horizontal on screen isn't editable
                for sp in line["spans"]:
                    if not sp["text"].strip():
                        continue
                    name = sp["font"]
                    keys = []
                    for xref in self.embedded(name):
                        if self.doc.xref_get_key(xref, "Subtype")[1] in ("/TrueType", "/Type0", "/OpenType"):
                            keys.append(f"x{xref}")
                    if self.font_file(name):
                        keys.append("s" + name)
                    o = pymupdf.Point(sp["origin"]) * rm
                    col = self.painted.get((n, sp["text"], round(o.x, 1), round(o.y, 1)), "#%06x" % sp["color"])
                    out.append({"bbox": tuple(pymupdf.Rect(sp["bbox"]) * rm), "origin": tuple(o), "text": sp["text"],
                                "size": round(sp["size"], 2), "font": name, "flags": sp["flags"], "color": col,
                                "keys": keys, "ocr": sp.get("alpha", 255) == 0})
        return out

    # ---- ops ----
    def op_edit(self, page, items, reflow=True):
        p = self.doc[page]
        notes = set()
        for it in [it for it in items if it.get("ocr")]:
            self.edit_scanned(p, it, notes)
        items = [it for it in items if not it.get("ocr")]
        if not items:
            return sorted(notes)
        same_line = lambda sp, it: abs(sp["origin"][1] - it["origin"][1]) < 0.5
        # Single edits push the rest of the line along, like a word processor.
        tail = []
        if reflow and len(items) == 1:
            it = items[0]
            tail = [sp for sp in self.spans(page) if same_line(sp, it) and sp["bbox"][0] >= it["bbox"][2] - 1 and not sp["ocr"]]
        for it in items + tail:
            r = pymupdf.Rect(it["bbox"])
            h = r.height
            p.add_redact_annot(unrot(p, pymupdf.Rect(r.x0 + 0.5, r.y0 + h * 0.3, r.x1 - 0.5, r.y1 - h * 0.3)))
        p.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE, graphics=pymupdf.PDF_REDACT_LINE_ART_NONE)
        for it in items:
            self.put(p, it, notes)
        if tail:
            it = items[0]
            new = [sp for sp in self.spans(page) if same_line(sp, it) and abs(sp["origin"][0] - it["origin"][0]) < 0.5]
            dx = (new[0]["bbox"][2] if new else it["bbox"][0]) - it["bbox"][2]
            for sp in tail:
                self.put(p, dict(sp, origin=(sp["origin"][0] + dx, sp["origin"][1])), notes)
        return sorted(notes)

    def put(self, p, it, notes):
        if not it["text"]:
            return
        if not it.get("family") and "\n" not in it["text"] and self.write_with_original(p, it):
            return
        ref, kind = self.pick_font(p, it["font"], it["flags"], it["text"], it.get("family"), it.get("bold"), it.get("italic"))
        standard = it["font"].split("-")[0] in ("Helvetica", "Times", "Courier", "Symbol", "ZapfDingbats")
        if kind != "exact" and not it.get("family") and not standard:
            notes.add(f"{it['font']}: original font lacks some glyphs, used " + ("installed copy" if kind == "system" else ref))
        p.insert_text(unrot(p, pymupdf.Point(it["origin"])), it["text"], fontname=ref, fontsize=it["size"],
                      color=hex2rgb(it["color"]), rotate=p.rotation)
        self.fix_space_map(p, ref)

    def analyze_scan(self, n, bbox, text, also=None):
        key = ("scan", n, tuple(round(v, 1) for v in bbox), text, also)
        if key not in self.cache:
            p, r = self.doc[n], pymupdf.Rect(bbox)
            base = scan_baseline(p, r)
            bg, ink = scan_colors(p, r)
            alts = match_font(p, r, text, base)
            if also and also != text and len(also.split()) == len(text.split()):
                # the user's retyped words may fix an OCR misread; keep each font's better score
                best = {c[1]: c for c in alts}
                for c in match_font(p, r, also, base):
                    if c[0] > best.get(c[1], (-9,))[0]:
                        best[c[1]] = (c[0], c[1], c[2], best[c[1]][3] if c[1] in best else c[3])
                alts = sorted(best.values(), key=lambda c: -c[0])
            weak = len(text.replace(" ", "")) <= 8 or alts[0][0] < 0.7
            # Fonts confident elsewhere on the page break ties. Surveying them costs a matching pass
            # per sample, so it only runs when the word alone is weak or two families are close.
            margin = alts[0][0] - alts[1][0] if len(alts) > 1 else 1.0
            if weak or margin < 0.06:
                prior = self.page_fonts(n)
                alts = sorted(alts, key=lambda c: -(c[0] + (0.15 if weak else 0.03) * (c[1] in prior)))
            _, label, ref, fs = alts[0]
            self.cache[key] = {"bg": bg, "ink": ink, "base": base, "label": label, "ref": ref, "fs": fs, "alts": alts}
        return self.cache[key]

    def invisible(self, p, text, r):
        helv = pymupdf.Font("helv")
        fs = r.height / (helv.ascender - helv.descender)
        o = pymupdf.Point(r.x0, r.y1 + helv.descender * fs)
        p.insert_text(unrot(p, o), text, fontname="helv", fontsize=fs, render_mode=3, rotate=p.rotation,
                      morph=(unrot(p, o), pymupdf.Matrix(r.width / helv.text_length(text, fs), 1)))

    def page_fonts(self, n):
        key = ("pagefonts", n)
        if key not in self.cache:
            p, found = self.doc[n], set()
            # four samples are enough to know which fonts this page is set in, and each costs a
            # matching pass over the candidates
            longest = sorted((sp for sp in self.spans(n) if sp["ocr"]), key=lambda sp: -len(sp["text"]))[:3]
            for sp in longest:
                if len(sp["text"].replace(" ", "")) > 8:
                    r = pymupdf.Rect(sp["bbox"])
                    best = match_font(p, r, sp["text"], scan_baseline(p, r))[0]
                    if best[0] > 0.8:
                        found.add(best[1])
            self.cache[key] = found
        return self.cache[key]

    def op_split(self, n, bbox, text, x):
        """Split an OCR run where the font changes (e.g. printed label + pen fill); returns the piece at x."""
        p, run = self.doc[n], pymupdf.Rect(bbox)
        words = text.split()
        if len(words) < 2:
            return {"bbox": tuple(run), "text": text}
        boxes = word_boxes(p, run, text)
        base = line_baseline(p, run)
        top = {c[1] for c in match_font(p, run, text, base)[:15]}
        part = lambda i, j: (pymupdf.Rect(boxes[i].x0, run.y0, boxes[j - 1].x1, run.y1), " ".join(words[i:j]))
        def scores(i, j):
            r, t = part(i, j)
            return {c[1]: c[0] for c in match_font(p, r, t, base, only=top)}
        best_k, best_gain = None, 0.0
        enough = lambda ws: len(ws) >= 2 or len(re.sub(r"\W", "", "".join(ws))) >= 6  # one short word is too little evidence
        for k in range(1, len(words)):
            if not (enough(words[:k]) and enough(words[k:])):
                continue
            L, R = scores(0, k), scores(k, len(words))
            fl, fr = max(L, key=L.get), max(R, key=R.get)
            gain = min(L[fl] - L.get(fr, -1), R[fr] - R.get(fl, -1))  # how much each side prefers its own font
            if gain > best_gain:
                best_k, best_gain = k, gain
        pieces = [list(range(len(words)))]
        if best_k is not None and best_gain > SPLIT_GAIN:
            pieces = [list(range(best_k)), list(range(best_k, len(words)))]
        if len(pieces) == 1:
            return {"bbox": tuple(run), "text": text}
        p.add_redact_annot(unrot(p, run))
        p.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE, graphics=pymupdf.PDF_REDACT_LINE_ART_NONE)
        hit = None
        for idx in pieces:
            r = pymupdf.Rect(boxes[idx[0]].x0, run.y0, boxes[idx[-1]].x1, run.y1)
            t = " ".join(words[i] for i in idx)
            self.invisible(p, t, r)
            if hit is None or r.x0 <= x:
                hit = {"bbox": tuple(r), "text": t}
        return hit

    def edit_scanned(self, p, it, notes):
        run = pymupdf.Rect(it["bbox"])
        orig, new = it.get("orig") or it["text"], it["text"]
        ow, nw = orig.split(), new.split()
        # Untouched words keep their original pixels; only the changed middle is re-typeset.
        pre = 0
        while pre < min(len(ow), len(nw)) and ow[pre] == nw[pre]:
            pre += 1
        suf = 0
        while suf < min(len(ow), len(nw)) - pre and ow[-1 - suf] == nw[-1 - suf]:
            suf += 1
        if pre == len(ow) == len(nw):
            return
        boxes = word_boxes(p, run, orig) if len(ow) > 1 else [run]
        insert = pre == len(ow) - suf  # pure insertion: existing words stay untouched
        seg_n = " ".join(nw[pre:len(nw) - suf])
        if insert:
            near = pre - 1 if pre > 0 else pre  # match the font against the neighbouring word
            seg_o = ow[near]
            r = pymupdf.Rect(boxes[near])
        else:
            seg_o = " ".join(ow[pre:len(ow) - suf])
            r = pymupdf.Rect(boxes[pre])
            for bx in boxes[pre:len(ow) - suf]:
                r |= bx
        r.y0, r.y1 = run.y0, run.y1
        # Borrow the user's spelling only where it looks like a fix of an OCR misread (sailing -> Sailing).
        fixed = " ".join(n if difflib.SequenceMatcher(None, o.lower(), n.lower()).ratio() >= 0.75 else o
                         for o, n in zip(seg_o.split(), seg_n.split())) if len(seg_o.split()) == len(seg_n.split()) else None
        if it.get("match_label") or (it.get("family") or ""):
            bg, ink = scan_colors(p, r)
            a = {"alts": []}
        else:
            a = self.analyze_scan(p.number, tuple(r), seg_o, fixed)
            bg, ink = a["bg"], a["ink"]
            if a["alts"] and a["alts"][0][0] < SCAN_MIN_MATCH:
                notes.add(f"Couldn't identify this font ({a['alts'][0][1]} fits poorly) - nothing changed. "
                          f"Pick a font from the font menu to edit it anyway.")
                return
        if (it.get("family") or "").startswith("f:") and family_file(it["family"][2:], it.get("bold"), it.get("italic")):
            ref = family_file(it["family"][2:], it.get("bold"), it.get("italic"))
            label, fs = it["family"][2:], r.width / pymupdf.Font(fontfile=ref).text_length(seg_o, 1)
        elif it.get("family"):
            ref = base14("", it.get("bold"), it.get("italic"), it["family"])
            label, fs = B14_NAMES[ref], r.width / pymupdf.Font(ref).text_length(seg_o, 1)
        elif it.get("match_label") and any(c[0] == it["match_label"] for c in font_candidates()):
            # WYSIWYG: use exactly the font and size the edit box showed
            label, _, ref = next(c for c in font_candidates() if c[0] == it["match_label"])
            fs = float(it.get("size") or r.height)
        else:
            _, label, ref, fs = a["alts"][min(int(it.get("match") or 0), len(a["alts"]) - 1)]
        if it.get("size_changed"):
            fs = it["size"]
        base = line_baseline(p, run)
        sigma = gain = None
        f0 = None if ref in B14_NAMES else (ref if os.path.isfile(ref) else None)
        if ref in B14_NAMES or f0:
            try:
                mine = not (it.get("size_changed") or it.get("match_label") or it.get("family"))
                f_fit, sigma, gain = fit_scan_text(p, unrot(p, r) if p.rotation else r, seg_o,
                                                   pymupdf.Font(fontfile=f0) if f0 else pymupdf.Font(ref),
                                                   ink, bg, fs, base, refine=mine)
                if mine:
                    fs = f_fit
            except Exception:
                sigma = gain = None
        # Drop the run's whole invisible layer (pixels untouched), then erase only the changed words.
        p.add_redact_annot(unrot(p, run))
        p.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE, graphics=pymupdf.PDF_REDACT_LINE_ART_NONE)
        color = hex2rgb(it["color"]) if it.get("color_changed") else ink
        # OCR boxes are padded; the printed ink is the truth we line the replacement up with.
        old_l, old_r = ink_span(p, unrot(p, r) if p.rotation else r)
        fpath = None if ref in B14_NAMES else (ref if os.path.isfile(ref) else None)
        if ref not in B14_NAMES and not fpath:
            ref = "helv"
        fnt = pymupdf.Font(fontfile=fpath) if fpath else pymupdf.Font(ref)
        space = fnt.text_length(" ", fs)
        cover = None if insert else r + (-1.2, -1.2, 1.2, 1.2)
        if insert:  # new words go beside the neighbour; nothing is erased
            x_new = r.x1 + space if pre > 0 else r.x0
            r = pymupdf.Rect(x_new, r.y0, x_new, r.y1)
        advance = fnt.text_length(seg_n, fs) if seg_n else 0.0
        # Room on this line before the next field; condense the new words instead of colliding with it.
        line_right = min([sp["bbox"][0] for sp in self.spans(p.number)
                          if abs(sp["bbox"][1] - run.y0) < run.height * 0.6 and sp["bbox"][0] > run.x1 - 1] + [p.rect.width - 20])
        room = (old_r - old_l) + max(line_right - 3 - min(run.x1, boxes[-1].x1), 0)
        sx = min(1.0, room / advance) if advance > old_r - old_l else 1.0
        glyph = None
        if seg_n:
            try:
                # (png, rect, left side bearing, ink width) at the tone measured off the scan
                glyph = glyph_png(seg_n, fnt, fs, color, scan_dpi(p), sx, sigma, gain or 1.0)
            except Exception:
                glyph = None
        if insert:
            ox, new_r = r.x0, r.x0 + advance * sx
        elif glyph:
            # A natively printed line keeps the *pen* where the old word started; matching ink edges
            # instead shifts the text whenever the first letter has a different side bearing
            # (Avery -> Priya). Only fall back to ink edges if the old glyph cannot be measured.
            ox = old_l - _side_bearing(seg_o, fnt, fs, color, p, sigma, gain)
            new_r = ox + glyph[2] + glyph[3]
        else:
            ox, new_r = old_l, old_l + advance * sx
        # Untouched words after the edit move with their own pixels. Advance widths, not ink edges:
        # bearings cancel, so the original gap between the words is kept to a fraction of a point.
        old_adv = 0.0 if insert else fnt.text_length(seg_o, fs)
        dx = (advance * sx + (space if insert and pre == 0 else 0)) - old_adv if suf else 0.0
        tail = pymupdf.Rect(boxes[len(ow) - suf].x0 - 1, run.y0, min(run.x1, boxes[-1].x1) + 1, run.y1) if suf else None
        tail_png = p.get_pixmap(dpi=round(scan_dpi(p)), clip=tail).tobytes("png") if tail and abs(dx) > 0.5 else None
        if tail_png:
            cover = (tail + (-1.2, -1.2, 1.2, 1.2)) if cover is None else cover | (tail + (-1.2, -1.2, 1.2, 1.2))
        if cover is not None:
            patch, prect = clean_patch(p, cover)
            p.add_redact_annot(unrot(p, prect))
            # IMAGE_NONE on purpose: asking MuPDF to redact image pixels rewrites the *whole* scan
            # (every untouched pixel shifts slightly). The opaque patch covers the old ink instead.
            p.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE, graphics=pymupdf.PDF_REDACT_LINE_ART_NONE)
            if patch:
                p.insert_image(prect, stream=patch, rotate=p.rotation, keep_proportion=False)
        if tail_png:
            tail = tail + (dx, 0, dx, 0)
            p.insert_image(tail, stream=tail_png, rotate=p.rotation)
            run = pymupdf.Rect(run.x0, run.y0, run.x1 + dx, run.y1)
        if pre:
            self.invisible(p, " ".join(ow[:pre]), pymupdf.Rect(run.x0, run.y0, boxes[pre - 1].x1, run.y1))
        if suf:
            self.invisible(p, " ".join(ow[len(ow) - suf:]), pymupdf.Rect(boxes[len(ow) - suf].x0 + dx, run.y0, run.x1, run.y1))
        if not seg_n:
            return
        if ref not in B14_NAMES:
            fpath, ref = fpath, "M" + font_key(label)[:20]
            p.insert_font(fontname=ref, fontfile=fpath)
            self.remember_file(p, ref, fpath)
        o = unrot(p, pymupdf.Point(ox, base))
        morph = (o, pymupdf.Matrix(sx, 1)) if sx < 0.999 else None
        if glyph:  # scan-like pixels: vector glyphs in a JPEG scan are the giveaway
            png, rel = glyph[0], glyph[1]
            p.insert_image(pymupdf.Rect(ox + rel.x0, base + rel.y0, ox + rel.x1, base + rel.y1),
                           stream=png, rotate=p.rotation, keep_proportion=False)
        else:
            p.insert_text(o, seg_n, fontname=ref, fontsize=fs, color=color, rotate=p.rotation, morph=morph)
        # invisible twin keeps the text selectable and the font name visible to the editor
        p.insert_text(o, seg_n, fontname=ref, fontsize=fs, color=color, rotate=p.rotation, render_mode=3, morph=morph)
        self.painted[(p.number, seg_n, round(ox, 1), round(base, 1))] = "#%02x%02x%02x" % tuple(
            max(0, min(255, int(round(c * 255)))) for c in color)
        self.fix_space_map(p, ref)
        notes.add(f"Scanned text matched to {label}")

    def op_ocr(self, lang="eng"):
        """Add an invisible text layer to image-only pages so their text becomes editable."""
        done = 0
        for page in self.doc:
            if page.get_text().strip() or not page.get_images():
                continue
            for text, r in ocr_runs(page, lang):
                self.invisible(page, text, r)
            done += 1
        return done

    def fix_space_map(self, page, ref):
        # MuPDF maps shared glyphs to U+00A0/U+00AD, so extracted text gets NBSPs and soft hyphens.
        for f in page.get_fonts():
            t, v = self.doc.xref_get_key(f[0], "ToUnicode")
            if f[4] != ref or t != "xref" or ("spfix", f[0]) in self.cache:
                continue
            x = int(v.split()[0])
            def fix(block):  # only single-code bfchar entries; ranges must stay intact
                b = re.sub(rb"(?m)^(<[0-9a-fA-F]+>[ \t]+)<00[aA]0>[ \t]*$", rb"\1<0020>", block.group(0))
                return re.sub(rb"(?m)^(<[0-9a-fA-F]+>[ \t]+)<00[aA][dD]>[ \t]*$", rb"\1<002D>", b)
            data = re.sub(rb"beginbfchar.*?endbfchar", fix, self.doc.xref_stream(x), flags=re.S)
            self.doc.update_stream(x, re.sub(rb"(?m)^(<[0-9a-fA-F]+>[ \t]+)<2010>[ \t]*$", rb"\1<002D>", data))
            self.cache[("spfix", f[0])] = True

    def op_add_text(self, page, x, y, text, size, color, family, bold, italic):
        p = self.doc[page]
        ref = self.pick_font(p, "", 0, text, family, bold, italic)[0]
        p.insert_text(unrot(p, pymupdf.Point(x, y)), text, fontname=ref, fontsize=size, color=hex2rgb(color), rotate=p.rotation)
        self.fix_space_map(p, ref)

    def op_shape(self, page, kind, rect, color, width=2, data=None):
        p = self.doc[page]
        r = unrot(p, pymupdf.Rect(rect)).normalize()
        c = hex2rgb(color)
        if kind == "whiteout":
            p.add_redact_annot(r, fill=(1, 1, 1))
            p.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_PIXELS, graphics=pymupdf.PDF_REDACT_LINE_ART_NONE)
            p.draw_rect(r, color=None, fill=(1, 1, 1), overlay=True)
            return
        if kind == "image":
            p.insert_image(r, stream=base64.b64decode(data), keep_proportion=True, rotate=0)
            return
        a = p.add_highlight_annot(r) if kind == "highlight" else p.add_rect_annot(r)
        a.set_colors(stroke=c)
        if kind == "rect":
            a.set_border(width=width)
        a.update()

    def op_ink(self, page, paths, color, width):
        p = self.doc[page]
        a = p.add_ink_annot([[tuple(unrot(p, pymupdf.Point(pt))) for pt in path] for path in paths])
        a.set_colors(stroke=hex2rgb(color))
        a.set_border(width=width)
        a.update()

    def op_replace(self, find, repl):
        count = 0
        notes = set()
        for n in range(len(self.doc)):
            hits = [s for s in self.spans(n) if find in s["text"]]
            if hits:
                count += sum(s["text"].count(find) for s in hits)
                notes.update(self.op_edit(n, [dict(s, text=s["text"].replace(find, repl), orig=s["text"]) for s in hits], reflow=False))
        return count, sorted(notes)

    def apply(self, op, a):
        d = self.doc
        if op in ("undo", "redo"):
            src, dst = (self.undo, self.redo) if op == "undo" else (self.redo, self.undo)
            if not src:
                return {"dirty": []}
            dst.append(d.tobytes())
            self.load(src.pop())
            return {"dirty": None}
        if op == "replace" and not a["find"]:
            raise ValueError("Nothing to find")
        self.drawn_chars("")  # index glyphs before this op removes any text
        self.snapshot()
        try:
            res = {"dirty": [a["page"]] if "page" in a else None}
            if op == "edit":
                res["notes"] = self.op_edit(a["page"], a["items"])
            elif op == "add_text":
                self.op_add_text(a["page"], a["x"], a["y"], a["text"], a["size"], a["color"], a.get("family") or "he", a.get("bold"), a.get("italic"))
            elif op in ("whiteout", "highlight", "rect", "image"):
                self.op_shape(a["page"], op, a["rect"], a.get("color", "#000000"), a.get("width", 2), a.get("data"))
            elif op == "ink":
                self.op_ink(a["page"], a["paths"], a["color"], a.get("width", 2))
            elif op == "replace":
                res["count"], res["notes"] = self.op_replace(a["find"], a["repl"])
                res["dirty"] = None
            elif op == "split":
                res["piece"] = self.op_split(a["page"], a["bbox"], a["text"], a["x"])
            elif op == "ocr":
                res["count"], res["dirty"] = self.op_ocr(a.get("lang") or "eng"), None
            elif op == "rotate":
                pg = d[a.pop("page")]
                pg.set_rotation((pg.rotation + a["deg"]) % 360)
            elif op == "delete_page":
                if len(d) == 1:
                    raise ValueError("Can't delete the only page")
                d.delete_page(a.pop("page"))
            elif op == "blank_page":
                n = a.pop("page")
                r = d[n].rect
                d.new_page(n + 1, width=r.width, height=r.height)
            elif op == "duplicate_page":
                n = a.pop("page")
                d.fullcopy_page(n, n + 1 if n + 1 < len(d) else -1)
            elif op == "erase_text":
                pg = d[a["page"]]
                r = unrot(pg, pymupdf.Rect(a["rect"])).normalize()
                pg.add_redact_annot(r + (0.5, 1, -0.5, -1), fill=False)
                pg.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE, graphics=pymupdf.PDF_REDACT_LINE_ART_NONE)
                # the original's own underline/strike rules (fully inside the text band) go too, or they'd double up
                pad = r.height * 0.35
                pg.add_redact_annot(r + (-1, -1, 1, pad), fill=False)
                pg.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE, graphics=pymupdf.PDF_REDACT_LINE_ART_REMOVE_IF_COVERED, text=pymupdf.PDF_REDACT_TEXT_NONE)
            elif op == "move_page":
                n, to = a.pop("page"), a["page_to"]
                if 0 <= to < len(d) and to != n:
                    d.move_page(n, to if to < n else to + 1)
            elif op == "merge":
                d.insert_pdf(pymupdf.open("pdf", base64.b64decode(a["data"])))
                self.seen = None
            else:
                raise ValueError(f"Unknown op {op}")
        except Exception:
            self.doc = pymupdf.open("pdf", self.undo.pop())
            self.fonts, self.cache = None, {}
            raise
        if res["dirty"] is None:
            self.fonts, self.cache = None, {}
        return res

    def export(self):
        out = pymupdf.open("pdf", self.doc.tobytes())
        try:
            out.subset_fonts()
        except Exception:
            pass
        return out.tobytes(garbage=3, deflate=True, deflate_fonts=True)


# Vercel rejects responses over ~4.5 MB and iOS drops huge canvases: cap pixels, fall back to JPEG.
def page_image(page, scale, max_px=16_000_000, max_bytes=3_500_000):
    w, h = page.rect.width * scale, page.rect.height * scale
    if w * h > max_px:
        scale *= (max_px / (w * h)) ** 0.5
    pix = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
    png = pix.tobytes("png")
    if len(png) <= max_bytes:
        return png, "image/png"
    for qual in (88, 78, 65):
        jpg = pix.tobytes("jpeg", jpg_quality=qual)
        if len(jpg) <= max_bytes:
            return jpg, "image/jpeg"
    small = page.get_pixmap(matrix=pymupdf.Matrix(scale * 0.7, scale * 0.7), alpha=False)
    return small.tobytes("jpeg", jpg_quality=70), "image/jpeg"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, body, ctype="application/json", extra=None):
        if not isinstance(body, (bytes, bytearray)):
            body = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def body(self):
        return self.rfile.read(int(self.headers.get("Content-Length", 0)))

    def sess(self, q):
        sid = q.get("id", [""])[0]
        s = SESSIONS.get(sid)
        path = os.path.join(SESS_DIR, re.sub(r"[^0-9a-f]", "", sid) + ".pdf")
        if not s and sid and os.path.isfile(path):
            s = SESSIONS[sid] = Session(open(path, "rb").read(), open(path[:-4] + ".name").read())
        if not s:
            raise KeyError("Document not loaded (server restarted?) — reopen the file")
        return s

    def handle_any(self, method):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        # Vercel rewrites every path to /api/index; the original rides along as __path.
        if "__path" in q:
            u = u._replace(path=q.pop("__path")[0] or "/")
        try:
            if method == "GET" and not u.path.startswith("/api/"):
                path = os.path.normpath(os.path.join(STATIC, (u.path.lstrip("/") or "index.html")))
                if not path.startswith(STATIC) or not os.path.isfile(path):
                    return self.send(404, {"error": "not found"})
                ext = os.path.splitext(path)[1]
                img = {".svg": "image/svg+xml", ".png": "image/png", ".ico": "image/png"}.get(ext)
                if img:
                    return self.send(200, open(path, "rb").read(), img, {"Cache-Control": "max-age=86400"})
                ctype = {".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".ttf": "font/ttf", ".webmanifest": "application/manifest+json"}.get(ext, "application/octet-stream")
                return self.send(200, open(path, "rb").read(), ctype + "; charset=utf-8", {"Cache-Control": "no-cache"})
            if u.path == "/api/fonts":
                return self.send(200, doc_fonts())
            if u.path == "/api/fontlist":
                return self.send(200, font_list())
            if u.path == "/api/sysfont":
                path = family_file(q["f"][0], q.get("b", ["0"])[0] == "1", q.get("i", ["0"])[0] == "1")
                if not path or q["f"][0] not in {f for f, _ in FONT_LIST}:
                    return self.send(404, {"error": "no such font"})
                return self.send(200, open(path, "rb").read(), "font/ttf", {"Cache-Control": "max-age=86400"})
            if u.path == "/api/open":
                sid = uuid.uuid4().hex
                SESSIONS[sid] = Session(self.body(), unquote(self.headers.get("X-Filename", "document.pdf")))
                persist(sid, SESSIONS[sid])
                doc = SESSIONS[sid].doc
                scanned = sum(1 for pg in doc if not pg.get_text().strip() and pg.get_images())
                return self.send(200, {"id": sid, "pages": SESSIONS[sid].pages(), "scanned": scanned})
            s = self.sess(q)
            if u.path == "/api/page":
                n, scale = int(q["n"][0]), min(float(q.get("s", ["1.5"])[0]), 6)
                img, ctype = page_image(s.doc[n], scale)
                return self.send(200, img, ctype, {"Cache-Control": "no-store"})
            if u.path == "/api/spans":
                return self.send(200, s.spans(int(q["n"][0])))
            if u.path == "/api/match":
                n, bbox, text = int(q["n"][0]), [float(v) for v in q["bbox"][0].split(",")], q["text"][0]
                a = s.analyze_scan(n, bbox, text)
                if len(text.split()) > 1:
                    # The edit matches one word at a time, so a line of label + fill reads badly as a
                    # whole. Preview the word that reads best, or the font shown while typing would
                    # disagree with the font the edit actually picks.
                    words = text.split()
                    boxes = word_boxes(s.doc[n], pymupdf.Rect(bbox), text)
                    for i in sorted(range(len(words)), key=lambda i: -len(words[i]))[:3]:
                        if len(words[i]) < 3 or i >= len(boxes):
                            continue
                        b = boxes[i]
                        cand = s.analyze_scan(n, (b.x0, bbox[1], b.x1, bbox[3]), words[i])
                        if cand["alts"] and cand["alts"][0][0] > a["alts"][0][0]:
                            a = cand
                hexc = lambda c: "#%02x%02x%02x" % tuple(round(v * 255) for v in c)
                alts = [{"label": lb, "key": None if rf in B14_NAMES else "s" + lb, "b14": rf if rf in B14_NAMES else None,
                         "size": float(fs), "score": round(float(sc), 3)}
                        for sc, lb, rf, fs in a["alts"][:6]]
                return self.send(200, {**alts[0], "alts": alts, "confident": bool(alts[0]["score"] >= SCAN_MIN_MATCH),
                                       "base": a["base"], "ink": hexc(a["ink"]), "bg": hexc(a["bg"])})
            if u.path == "/api/font":
                k = q["k"][0]
                if k[0] == "x":
                    buf = s.font_obj(int(k[1:]))[0]
                else:
                    p = s.font_file(k[1:])
                    buf = open(p, "rb").read() if p else b""
                return self.send(200, buf, "font/ttf", {"Cache-Control": "max-age=3600"})
            if u.path == "/api/op":
                a = json.loads(self.body())
                res = s.apply(a.pop("op"), a)
                persist(q["id"][0], s)
                res["pages"] = s.pages()
                res["canUndo"], res["canRedo"] = bool(s.undo), bool(s.redo)
                return self.send(200, res)
            if u.path == "/api/export":
                a = json.loads(self.body() or b"{}")
                doc = render_objects(s.export(), a.get("objs", []))
                name = re.sub(r"[^\w.\- ]", "_", os.path.splitext(s.name)[0]) + "-edited.pdf"
                return self.send(200, doc.tobytes(garbage=3, deflate=True), "application/pdf", {"Content-Disposition": f'attachment; filename="{name}"'})
            if u.path == "/api/download":
                name = re.sub(r"[^\w.\- ]", "_", os.path.splitext(s.name)[0]) + "-edited.pdf"
                return self.send(200, s.export(), "application/pdf", {"Content-Disposition": f'attachment; filename="{name}"'})
            self.send(404, {"error": "not found"})
        except Exception as e:
            self.send(400, {"error": str(e) or type(e).__name__})

    def do_GET(self):
        with LOCK:
            self.handle_any("GET")

    def do_POST(self):
        with LOCK:
            self.handle_any("POST")


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    print(f"PDF editor on http://localhost:{port}")
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
