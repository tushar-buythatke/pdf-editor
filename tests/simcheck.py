#!/usr/bin/env python3
"""How close is an edited region to the same document natively containing that text?

    python tests/simcheck.py edited.pdf reference.pdf 'x0,y0,x1,y1'
    from simcheck import compare
    compare(docA, docB, rect) -> dict(ncc, shift, scale, mean_abs)

ncc    correlation of the ink maps after the best sub-pixel shift (1.0 = identical)
shift  (dx, dy) in points, how far the edit sits from the reference
scale  width ratio of the ink mass (1.0 = right size, 0.97 = 3% too small)
"""
import sys

import numpy as np
import pymupdf


def _ink(page, rect, dpi):
    pix = page.get_pixmap(dpi=dpi, clip=pymupdf.Rect(rect), colorspace=pymupdf.csGRAY)
    a = np.frombuffer(pix.samples, np.uint8).reshape(pix.height, pix.stride)[:, :pix.width]
    return 1.0 - a.astype(np.float32) / 255.0


def _body(a, keep=0.86):
    """Ink map with trailing rules/underlines masked out, so only glyphs are compared."""
    a = a.copy()
    a[int(a.shape[0] * keep):, :] = np.where(a[int(a.shape[0] * keep):, :] > 0.55 * a.max(), 0, a[int(a.shape[0] * keep):, :])
    return a


def compare(doc_a, doc_b, rect, dpi=300, pad=2.0):
    r = pymupdf.Rect(rect) + (-pad, -pad, pad, pad)
    A = _body(_ink(doc_a[0], r, dpi))
    B = _body(_ink(doc_b[0], r, dpi))
    best = (-2.0, 0, 0)
    for dy in range(-8, 9):
        for dx in range(-8, 9):
            a = np.roll(np.roll(A, dy, 0), dx, 1)
            n = float((a * B).sum() / np.sqrt((a * a).sum() * (B * B).sum() + 1e-9))
            if n > best[0]:
                best = (n, dx, dy)
    n, dx, dy = best
    px = 72.0 / dpi
    ma, mb = A.sum(), B.sum()
    ca = (A.sum(0) * np.arange(A.shape[1])).sum() / max(ma, 1e-6)
    cb = (B.sum(0) * np.arange(B.shape[1])).sum() / max(mb, 1e-6)
    return {"ncc": round(n, 4), "shift": (round(dx * px, 2), round(dy * px, 2)),
            "scale": round(float(np.sqrt(ma / max(mb, 1e-6))), 3),
            "centroid_dx": round(float((ca - cb) * px), 2),
            "mean_abs": round(float(np.abs(A - B).mean() * 255), 2)}


if __name__ == "__main__":
    a, b = pymupdf.open(sys.argv[1]), pymupdf.open(sys.argv[2])
    rect = [float(v) for v in sys.argv[3].split(",")]
    print(compare(a, b, rect))
