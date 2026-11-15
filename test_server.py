import numpy as np
import pymupdf
from server import Session

GEORGIA = "/System/Library/Fonts/Supplemental/Georgia.ttf"


def make_pdf(rotation=0):
    d = pymupdf.open()
    p = d.new_page()
    p.insert_font(fontname="F0", fontfile=GEORGIA)
    p.insert_text((72, 100), "Hello Georgia world", fontname="F0", fontsize=14, color=(0.2, 0, 0.6))
    p.insert_text((72, 140), "Base helv line", fontname="helv", fontsize=12)
    p.set_rotation(rotation)
    d.subset_fonts()
    return d.tobytes()


def spans_by_text(s, n=0):
    return {sp["text"]: sp for sp in s.spans(n)}


def test_exact_font_edit():
    s = Session(make_pdf(), "t.pdf")
    sp = spans_by_text(s)["Hello Georgia world"]
    assert any(k.startswith("x") for k in sp["keys"])
    res = s.apply("edit", {"page": 0, "items": [dict(sp, text="Hello Georgia")]})  # glyphs all in subset
    assert res["notes"] == []
    new = spans_by_text(s)
    assert "Hello Georgia" in new and new["Hello Georgia"]["font"] == "Georgia Regular"
    assert abs(new["Hello Georgia"]["origin"][1] - 100) < 0.5 and new["Hello Georgia"]["color"] == sp["color"]
    assert "Base helv line" in new  # neighbour untouched


def test_missing_glyph_uses_installed_font():
    s = Session(make_pdf(), "t.pdf")
    sp = spans_by_text(s)["Hello Georgia world"]
    res = s.apply("edit", {"page": 0, "items": [dict(sp, text="Jumpy Zebra-quiz")]})
    assert res["notes"] and "installed copy" in res["notes"][0]
    assert spans_by_text(s)["Jumpy Zebra-quiz"]["font"].startswith("Georgia")  # text reads back exactly


def test_edit_reflows_rest_of_line():
    s = Session(make_pdf(), "t.pdf")
    p = s.doc[0]
    p.insert_text((200, 140), "tail", fontname="helv", fontsize=12)
    before = spans_by_text(s)
    old_x = before["tail"]["origin"][0]
    s.apply("edit", {"page": 0, "items": [dict(before["Base helv line"], text="Base helv line-much-longer")]})
    after = spans_by_text(s)
    assert after["tail"]["origin"][0] > old_x + 20 and "Base helv line-much-longer" in after


def ink_at(page, rect):
    pix = page.get_pixmap(clip=rect, alpha=False)
    return min(pix.samples) < 128


def test_rotated_page_edit_keeps_position():
    s = Session(make_pdf(rotation=90), "t.pdf")
    s.apply("add_text", {"page": 0, "x": 100, "y": 300, "text": "Upright note", "size": 20, "color": "#000000"})
    sp = spans_by_text(s)["Upright note"]
    assert abs(sp["origin"][0] - 100) < 1 and abs(sp["origin"][1] - 300) < 1
    s.apply("edit", {"page": 0, "items": [dict(sp, text="Upright edited")]})
    new = spans_by_text(s)
    assert "Upright note" not in new and max(abs(a - b) for a, b in zip(new["Upright edited"]["origin"], sp["origin"])) < 1
    assert ink_at(s.doc[0], pymupdf.Rect(100, 288, 160, 300))
    s.apply("whiteout", {"page": 0, "rect": new["Upright edited"]["bbox"]})
    assert "Upright edited" not in spans_by_text(s)
    s.apply("highlight", {"page": 0, "rect": (300, 100, 400, 130), "color": "#ffff00"})
    page = s.doc[0]
    got = page.first_annot.rect * page.rotation_matrix
    assert got.contains(pymupdf.Rect(300, 100, 400, 130)) and got.width < 120


def test_replace_undo_redo_and_export():
    s = Session(make_pdf(), "t.pdf")
    assert s.apply("replace", {"find": "Base", "repl": "Bas"})["count"] == 1
    assert "Bas helv line" in spans_by_text(s)
    s.apply("blank_page", {"page": 0})
    s.apply("move_page", {"page": 1, "page_to": 0})
    assert len(s.doc) == 2 and not s.spans(0)
    s.apply("undo", {}); s.apply("undo", {}); s.apply("undo", {})
    assert "Base helv line" in spans_by_text(s) and len(s.doc) == 1
    s.apply("redo", {})
    assert "Bas helv line" in spans_by_text(s)
    out = pymupdf.open("pdf", s.export())
    assert "Bas helv line" in out[0].get_text()


def test_scanned_page_ocr_and_font_match():
    src = pymupdf.open()
    p = src.new_page()
    p.insert_font(fontname="F0", fontfile=GEORGIA)
    p.insert_text((72, 120), "Quarterly report for Northwind", fontname="F0", fontsize=20, color=(0.1, 0.2, 0.6))
    scan = pymupdf.open()
    scan.new_page().insert_image(scan[0].rect, stream=p.get_pixmap(dpi=200).tobytes("jpeg"))
    s = Session(scan.tobytes(), "scan.pdf")
    assert s.apply("ocr", {})["count"] == 1
    sp = next(x for x in s.spans(0) if x["ocr"])
    assert sp["text"] == "Quarterly report for Northwind"
    keep = pymupdf.Rect(sp["bbox"]); keep.x1 = keep.x0 + keep.width * 0.55  # "Quarterly report for" region
    def keep_px():
        px = s.doc[0].get_pixmap(dpi=144)
        a = np.frombuffer(px.samples, np.uint8).reshape(px.height, px.width, 3)
        return a[int(keep.y0 * 2):int(keep.y1 * 2), int(keep.x0 * 2):int(keep.x1 * 2)].copy()
    before = keep_px()
    res = s.apply("edit", {"page": 0, "items": [dict(sp, orig=sp["text"], text="Quarterly report for Northport")]})
    assert res["notes"] == ["Scanned text matched to Georgia"], res
    assert (keep_px() == before).all()  # untouched words keep original pixels
    spans = {x["text"]: x for x in s.spans(0)}
    new = spans["Northport"]
    # edited scan text is pixels + an invisible text layer, so it still reports the matched font
    assert new["font"].startswith("Georgia") and abs(new["size"] - 20) < 1.5 and new["ocr"]
    r, g, b = (int(new["color"][i:i + 2], 16) for i in (1, 3, 5))
    assert b > r + 40  # kept the blue ink
    rest = spans["Quarterly report for"]
    assert rest["ocr"]
    res = s.apply("edit", {"page": 0, "items": [dict(rest, orig=rest["text"], text="Annual report for")]})
    assert res["notes"] == ["Scanned text matched to Georgia"], res
    assert {"Annual", "Northport"} <= {x["text"] for x in s.spans(0)}


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn(); print("ok", name)
