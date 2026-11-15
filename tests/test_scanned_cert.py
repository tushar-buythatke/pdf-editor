"""Scanned-certificate regression suite.

Fixture: tests/make_fixture.py builds an image-only, JPEG'd, slightly skewed form with a serif-italic
name between printed labels, sitting on an underlined blank. The ground truth is a second build of
the same fixture that contains the new name natively, so "perfect" is measurable: the edited region
is compared against a document that was *born* with that text.

    python tests/test_scanned_cert.py
"""
import os
import sys

import numpy as np
import pymupdf

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from server import Session  # noqa: E402
from make_fixture import build  # noqa: E402
from simcheck import compare  # noqa: E402

OLD = "Avery Stone"


def edited(old=OLD, new="Amara Stone", **kw):
    """Build the fixture, OCR it, rename, and return (edited doc, native-reference doc, name rect)."""
    src, meta = build(old, **kw)
    ref, _ = build(new, **kw)
    s = Session(src, "fx.pdf")
    s.apply("ocr", {})
    run = next(sp for sp in s.spans(0) if old.split()[0] in sp["text"])
    assert run["ocr"], "fixture must OCR into an invisible text layer"
    notes = s.apply("edit", {"page": 0, "items": [dict(run, orig=run["text"], text=run["text"].replace(old, new))]})
    r = meta["name_rect"]
    return pymupdf.open("pdf", s.export()), pymupdf.open("pdf", ref), meta, notes, pymupdf.Rect(r)


def page_ink(doc, rect, dpi=220):
    # Store shrink first: MuPDF keeps decoded JPEGs in a process-wide store, so a render after other
    # renders can differ by resampling noise even when the file is byte-identical.
    pymupdf.TOOLS.store_shrink(100)
    pg = doc[0]
    pix = pg.get_pixmap(dpi=dpi, clip=pymupdf.Rect(rect), colorspace=pymupdf.csGRAY)
    return np.frombuffer(pix.samples, np.uint8).reshape(pix.height, pix.stride)[:, :pix.width]


def test_name_edit_matches_a_natively_printed_name():
    """The whole point: a renamed field must look like the document always said that."""
    doc, ref, meta, notes, r = edited()
    assert "Georgia Italic" in " ".join(notes["notes"]), notes  # the serif italic was recognised
    m = compare(doc, ref, (r.x0 - 2, r.y0 - 2, r.x1 + 14, r.y1 + 2))
    assert m["ncc"] > 0.93, m
    assert abs(m["shift"][0]) <= 0.5 and abs(m["shift"][1]) <= 0.5, m
    assert abs(m["scale"] - 1) <= 0.03, m


def rule_coverage(doc, r):
    """Widest ink coverage of any row just under the name: a form underline spans the whole field."""
    band = pymupdf.Rect(r.x0 + 1, r.y1 - 2.5, r.x1 + 8, r.y1 + 2.5)
    return float((page_ink(doc, band) < 150).mean(1).max())


def test_underline_under_the_edited_words_survives():
    """Erasing the old name must not take the form's underline with it."""
    doc, ref, meta, notes, r = edited()
    got, want = rule_coverage(doc, r), rule_coverage(ref, r)
    assert got > 0.55, f"underline erased: widest row only {got:.2f} inked"
    assert abs(got - want) < 0.2, (got, want)


def test_untouched_words_keep_their_original_pixels():
    """Only the edited words may change; the rest of the form stays byte-identical."""
    src, meta = build(OLD)
    s = Session(src, "fx.pdf")
    s.apply("ocr", {})
    run = next(sp for sp in s.spans(0) if OLD.split()[0] in sp["text"])
    r = pymupdf.Rect(meta["name_rect"])
    keep = pymupdf.Rect(r.x0 - 45, r.y1 + 7, r.x1 + 130, r.y1 + 24)  # the line below the name field
    before = page_ink(s.doc, keep).copy()
    s.apply("edit", {"page": 0, "items": [dict(run, orig=run["text"], text=run["text"].replace(OLD, "Amara Stone"))]})
    assert (page_ink(pymupdf.open("pdf", s.export()), keep) == before).all()


def test_font_identity_and_size_are_reported_for_the_edit():
    """Re-editing the result must show the same family and size the user just saw."""
    doc, ref, meta, notes, r = edited()
    spans = Session(doc.tobytes(), "x.pdf").spans(0)
    got = [sp for sp in spans if "Amara" in sp["text"]]
    assert got, [sp["text"] for sp in spans]
    sp = got[0]
    assert "Georgia" in sp["font"] and "Italic" in sp["font"].replace("-", " "), sp["font"]
    assert abs(sp["size"] - meta["size"]) <= max(0.6, meta["size"] * 0.06), (sp["size"], meta["size"])


def test_wider_and_narrower_names_still_look_native():
    """Longer and shorter replacements: same font, same baseline, rule intact, nothing collides."""
    for new in ("Amara Stone", "Priya Stone", "Eve Stone"):
        doc, ref, meta, notes, r = edited(new=new)
        m = compare(doc, ref, (r.x0 - 2, r.y0 - 2, r.x1 + 20, r.y1 + 2))
        assert m["ncc"] > 0.85, (new, m)
        assert abs(m["scale"] - 1) <= 0.04, (new, m)
        assert rule_coverage(doc, r) > 0.55, ("underline lost", new)


def test_faded_scan_keeps_the_serif_font():
    """A washed-out toner copy must still resolve to the right family, not to Helvetica."""
    doc, ref, meta, notes, r = edited(fade=0.35)
    assert "Georgia Italic" in " ".join(notes["notes"]), notes
    assert compare(doc, ref, (r.x0 - 2, r.y0 - 2, r.x1 + 14, r.y1 + 2))["ncc"] > 0.88


def test_cursive_name_matches_the_script_face():
    """A name set in a script/calligraphic face must resolve to that face, not to a bold sans.

    Two installed faces cannot be rendered through TextWriter, which the matcher used to do
    silently; a cursive name therefore never matched anything and a heavy sans was substituted.
    Rendering through embedded fonts fixed matching here outright (0.93 vs 0.39 for the runner-up).
    """
    from server import sysfont
    script = sysfont("Brush Script MT") or sysfont("Bradley Hand")
    if not script:
        print("   (skipped: no script font installed)")
        return
    src, meta = build(OLD, name_font=script)
    ref, _ = build("Amara Stone", name_font=script)
    s = Session(src, "fx.pdf")
    s.apply("ocr", {})
    run = next(sp for sp in s.spans(0) if OLD.split()[0] in sp["text"])
    notes = s.apply("edit", {"page": 0, "items": [dict(run, orig=run["text"], text=run["text"].replace(OLD, "Amara Stone"))]})
    joined = " ".join(notes["notes"])
    assert "Couldn't identify" not in joined, notes
    assert "Script" in joined or "script" in joined.lower(), notes
    r = pymupdf.Rect(meta["name_rect"])
    m = compare(pymupdf.open("pdf", s.export()), pymupdf.open("pdf", ref), (r.x0 - 2, r.y0 - 2, r.x1 + 16, r.y1 + 2))
    assert m["ncc"] > 0.8, m


def test_low_confidence_guard_leaves_the_page_untouched():
    """When no font fits, the edit must do nothing — a wrong face baked into a scan is unrecoverable.

    Forced here rather than found in the wild: the guard is the safety net behind the matcher.
    """
    src, meta = build(OLD)
    s = Session(src, "fx.pdf")
    s.apply("ocr", {})
    run = next(sp for sp in s.spans(0) if OLD.split()[0] in sp["text"])
    real = Session.analyze_scan

    def poor(self, n, bbox, text, also=None):
        out = real(self, n, bbox, text, also)
        out = dict(out)
        out["alts"] = [(0.11, "Helvetica", "helv", 11.0)] + list(out["alts"][1:])
        return out

    Session.analyze_scan = poor
    try:
        keep = pymupdf.Rect(meta["name_rect"])
        before = page_ink(s.doc, keep).copy()
        notes = s.apply("edit", {"page": 0, "items": [dict(run, orig=run["text"], text=run["text"].replace(OLD, "Amara Stone"))]})
        assert any("Couldn't identify this font" in n for n in notes["notes"]), notes
        assert (page_ink(pymupdf.open("pdf", s.export()), keep) == before).all()
    finally:
        Session.analyze_scan = real


if __name__ == "__main__":
    import time
    fails = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            t = time.time()
            try:
                fn()
                print("ok   %-58s %4.1fs" % (name, time.time() - t))
            except AssertionError as e:
                fails += 1
                print("FAIL %-58s %s" % (name, e))
    sys.exit(1 if fails else 0)
