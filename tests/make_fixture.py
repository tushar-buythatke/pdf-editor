#!/usr/bin/env python3
"""Synthetic image-only certificate fixture for the PDF editor's scanned-text path.

Everything here is invented: a made-up institute, a made-up holder, a made-up roll number.
The technical profile matches a hard real-world case: a single JPEG, no text layer, no fonts,
a slight rotation, a mixed-script header, a coloured border, and a serif-italic name sitting on
an underlined blank between two printed labels.

    python tests/make_fixture.py /tmp/fixture.pdf            # -> Avery Stone
    python tests/make_fixture.py /tmp/ref.pdf --name "Amara Stone"

`build()` returns (pdf_bytes, meta). meta["name_rect"] is where the name lands on the final
page, so tests can compare an edited copy against a natively-built reference of the same text.
"""
import argparse
import subprocess
import sys
import os
import tempfile

import numpy as np
import pymupdf

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SERIF = "/System/Library/Fonts/Supplemental/Georgia.ttf"
SERIF_I = "/System/Library/Fonts/Supplemental/Georgia Italic.ttf"
SERIF_B = "/System/Library/Fonts/Supplemental/Georgia Bold.ttf"

INSTITUTE = "NORTHWIND INSTITUTE OF TECHNOLOGY"
HINDI = "उदाहरण प्रौद्योगिकी संस्थान"
DEGREE = "BACHELOR OF TECHNOLOGY"
COLLEGE = "Example College of Engineering, Testville"
ROLL = "TEST-99887766"
FOOTER = "Synthetic test fixture - generated, not a real document"

PAPER = (0.996, 0.992, 0.976)
INK = (0.12, 0.11, 0.14)
ORANGE = (0.91, 0.55, 0.09)
MAROON = (0.42, 0.10, 0.20)
FADE = (0.30, 0.30, 0.34)


def _deva_png(text, size):
    """Shaped Devanagari via AppKit (MuPDF does not reorder matras)."""
    out = os.path.join(tempfile.mkdtemp(prefix="fx_deva_"), "deva.png")
    subprocess.run(["swift", os.path.join(HERE, "deva_png.swift"), text, "DevanagariSangamMN",
                    str(size), out, str(MAROON[0]), str(MAROON[1]), str(MAROON[2])],
                   check=True, capture_output=True)
    return out


def _warp(a, deg, shear):
    """Rotate+shear about the page centre, keeping the frame page-sized. Returns (array, A).

    out_px = A @ [src_x, src_y, 1]. Keeping the frame identical to the source means the fixture's
    geometry is the page's geometry: an 11.5pt name in the source is 11.5pt in the scan, so tests can
    assert measured sizes against the real ones.
    """
    h, w = a.shape[:2]
    c = np.array([w / 2.0, h / 2.0])
    t = np.deg2rad(deg)
    R = np.array([[np.cos(t), -np.sin(t)], [np.sin(t), np.cos(t)]])
    S = np.array([[1.0, shear], [0.0, 1.0]])
    L = S @ R
    off = c - L @ c
    A = np.array([[L[0, 0], L[0, 1], off[0]], [L[1, 0], L[1, 1], off[1]], [0, 0, 1]])
    Ai = np.linalg.inv(A)
    yy, xx = np.mgrid[0:h, 0:w]
    sxy = Ai @ np.stack([xx.ravel(), yy.ravel(), np.ones(h * w)])
    sx, sy = sxy[0], sxy[1]
    ok = (sx >= 0) & (sx <= w - 1) & (sy >= 0) & (sy <= h - 1)
    x0, y0 = np.clip(sx.astype(int), 0, w - 2), np.clip(sy.astype(int), 0, h - 2)
    fx, fy = np.clip(sx - x0, 0, 1)[:, None], np.clip(sy - y0, 0, 1)[:, None]  # bilinear: a real
    a00, a10 = a[y0, x0], a[y0, x0 + 1]                                        # scan resamples
    a01, a11 = a[y0 + 1, x0], a[y0 + 1, x0 + 1]                                # optically, never
    out = (a00 * (1 - fx) * (1 - fy) + a10 * fx * (1 - fy)                 # with nearest-neighbour
           + a01 * (1 - fx) * fy + a11 * fx * fy)
    out[~ok] = 255.0  # corners exposed by the rotation are blank paper, not black
    return np.clip(out, 0, 255).reshape(h, w, 3).astype(np.uint8), A


def build(name="Avery Stone", father="Dana Stone", roll=ROLL, college=COLLEGE,
          skew=0.4, jpeg=72, dpi=280, fade=0.0, blur=0.7, name_font=None):
    """Return (pdf bytes, meta) for an image-only fixture holding `name` in the name field."""
    page = pymupdf.open().new_page(width=595, height=842)
    page.draw_rect(page.rect, color=None, fill=PAPER)
    for inset, col, wid in ((26, ORANGE, 7.0), (34, MAROON, 1.2)):
        page.draw_rect(pymupdf.Rect(inset, inset, 595 - inset, 842 - inset), color=col, width=wid,
                       radius=0.06, fill=None)
    for i in range(14):  # the little repeated band marks
        t = 0.02 + i * 0.072
        for x, y in ((30, 30 + t * 780), (565, 30 + t * 780)):
            page.insert_text((x - 6, y), "NWT", fontsize=7.5, fontname="hebo", color=ORANGE,
                             morph=None, rotate=90)
        page.insert_text((40 + t * 520, 36), "NWT", fontsize=7.5, fontname="hebo", color=ORANGE)
        page.insert_text((40 + t * 520, 816), "NWT", fontsize=7.5, fontname="hebo", color=ORANGE)

    deva = _deva_png(HINDI, 34)
    dr = pymupdf.Pixmap(deva)
    deva_h = 26.0
    deva_w = deva_h * dr.width / dr.height
    page.insert_image(pymupdf.Rect(297 - deva_w / 2, 96, 297 + deva_w / 2, 96 + deva_h), filename=deva)

    name_font = name_font or SERIF_I  # cursive/script names are a genuinely harder case
    page.insert_font(fontname="gs", fontfile=SERIF)
    page.insert_font(fontname="gsb", fontfile=SERIF_B)
    page.insert_font(fontname="gsi", fontfile=name_font)
    page.insert_text((297 - pymupdf.Font(fontfile=SERIF_B).text_length(INSTITUTE, 19) / 2, 158),
                     INSTITUTE, fontname="gsb", fontsize=19, color=INK)
    page.insert_text((297 - pymupdf.Font(fontfile=SERIF_I).text_length(DEGREE, 14) / 2, 196),
                     DEGREE, fontname="gsi", fontsize=14, color=MAROON)

    # registration line: right-aligned label over an underline
    lab = "Regn. cum Roll No."
    lw = pymupdf.Font(fontfile=SERIF_I).text_length(lab, 10.5)
    page.insert_text((430 - lw, 232), lab, fontname="gsi", fontsize=10.5, color=INK)
    page.insert_text((436, 232), roll, fontname="gs", fontsize=10.5, color=INK)
    page.draw_line((432, 235), (556, 235), color=FADE, width=0.7)

    fb = pymupdf.Font(fontfile=SERIF)             # body font
    fi = pymupdf.Font(fontfile=name_font)         # fill-in font (name field and friends)

    B, LAB = 11.5, 10.0
    drawn = {}

    class Flow:
        """Form layout: tokens flow left to right, wrapping at the right margin."""
        def __init__(self, y, x=52.0, right=543.0, lh=24.0):
            self.x, self.y, self.x0, self.right, self.lh = x, y, x, right, lh

        def put(self, text, italic=False, ul=0.0, key=None):
            size = B if italic else LAB
            f = fi if italic else fb
            w = f.text_length(text, size)
            if self.x + w + ul > self.right:
                self.y += self.lh
                self.x = self.x0
            page.insert_text((self.x, self.y), text, fontname="gsi" if italic else "gs",
                             fontsize=size, color=INK)
            if ul:
                page.draw_line((self.x + 1, self.y + 3.2), (self.x + w + ul, self.y + 3.2),
                               color=FADE, width=0.8)
            if key:
                drawn[key] = (self.x, self.x + w, self.y)
            self.x += w + ul + 12
            return self

    f = Flow(272)
    f.put("Mr./Ms.").put(name, True, 132, key="name")
    f.put("son/daughter of").put(father, True, 120)
    f.put("student of").put(college, True, 150)
    f.put("having completed the course of")
    f.put("studies approved by the Institute and having passed the prescribed examination held in")
    f.put("May-2019", True, 60)
    f.put("has been conferred the degree of Bachelor of Technology of this Institute in the discipline of")
    f.put("Electrical Engineering", True, 170)
    f.put("in").put("First Division", True, 120)

    seal = "Given under the seal of the Institute"
    page.insert_text((297 - fi.text_length(seal, 10) / 2, 452), seal, fontname="gsi", fontsize=10, color=INK)
    for x, cap in ((110, "Registrar"), (470, "Chancellor")):
        page.draw_line((x - 45, 500), (x + 45, 500), color=INK, width=0.9)
        page.insert_text((x - 45, 514), cap, fontname="gsi", fontsize=9.5, color=INK)
    page.insert_text((52, 540), FOOTER, fontname="gs", fontsize=6.5, color=(0.55, 0.55, 0.58))

    # --- the "scan": rasterise, rotate/shear, JPEG-encode -------------------------------
    zoom = dpi / 72.0
    pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
    arr = np.frombuffer(pix.samples, np.uint8).reshape(pix.height, pix.stride)[:, :pix.width * 3]
    arr = arr.reshape(pix.height, pix.width, 3).copy()
    warped, A = _warp(arr, skew, 0.0016)
    if blur:  # scanner MTF: a real capture is slightly soft before the JPEG encoder runs
        f = warped.astype(np.float32)
        rad = max(1, int(round(blur * 2)))
        xs = np.arange(-rad, rad + 1)
        ker = np.exp(-(xs ** 2) / (2 * blur * blur))
        ker /= ker.sum()
        f = sum(ker[i] * np.pad(f, ((rad, rad), (0, 0), (0, 0)), mode="edge")[i:i + f.shape[0]] for i in range(len(ker)))
        f = sum(ker[i] * np.pad(f, ((0, 0), (rad, rad), (0, 0)), mode="edge")[:, i:i + f.shape[1]] for i in range(len(ker)))
        warped = np.clip(f, 0, 255).astype(np.uint8)
    if fade:  # washed-out toner: lift the ink toward the paper
        warped = np.clip(warped.astype(np.float32) * (1 - fade) + 255 * fade, 0, 255).astype(np.uint8)
    warped = np.clip(warped.astype(np.float32) + np.random.default_rng(7).normal(0, 2.4, warped.shape), 0, 255).astype(np.uint8)

    doc_jpeg = os.path.join(tempfile.mkdtemp(prefix="fx_scan_"), "scan.jpg")
    pymupdf.Pixmap(pymupdf.csRGB, warped.shape[1], warped.shape[0], warped.tobytes(), False).save(doc_jpeg, jpg_quality=jpeg)

    out = pymupdf.open()
    op = out.new_page(width=595, height=842)
    iw, ih = warped.shape[1], warped.shape[0]
    # The frame is the page, so the image goes down at the page rect and the geometry is 1:1 with the
    # source. A nominal 11.5pt name really measures 11.5pt here, which is what the tests assert.
    place = pymupdf.Rect(0, 0, 595, 842)
    op.insert_image(place, filename=doc_jpeg)
    nx0, nx1, ybase = drawn["name"]
    r = pymupdf.Rect(nx0, ybase - 9.5, nx1, ybase + 3.5)
    s = 72.0 / dpi
    M = A @ np.array([[zoom, 0, 0], [0, zoom, 0], [0, 0, 1]])
    corners = (np.array([[r.x0, r.y0, 1], [r.x1, r.y0, 1], [r.x0, r.y1, 1], [r.x1, r.y1, 1]]) @ M.T)[:, :2] * s
    corners[:, 0] += place.x0
    corners[:, 1] += place.y0
    xs, ys = corners[:, 0], corners[:, 1]
    final = pymupdf.Rect(xs.min(), ys.min(), xs.max(), ys.max())
    meta = {"name": name, "fontfile": name_font, "size": B, "baseline": ybase,
            "name_rect": tuple(final), "source_rect": tuple(r), "page_rect": tuple(op.rect),
            "dpi": dpi, "skew": skew, "jpeg": jpeg, "roll": roll, "college": college}
    return out.tobytes(), meta


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("--name", default="Avery Stone")
    ap.add_argument("--father", default="Dana Stone")
    ap.add_argument("--skew", type=float, default=0.4)
    ap.add_argument("--jpeg", type=int, default=72)
    ap.add_argument("--fade", type=float, default=0.0)
    a = ap.parse_args()
    data, meta = build(a.name, a.father, skew=a.skew, jpeg=a.jpeg, fade=a.fade)
    open(a.out, "wb").write(data)
    print({k: (round(v, 1) if isinstance(v, float) else v) for k, v in meta.items()})
