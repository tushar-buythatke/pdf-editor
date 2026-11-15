import base64, math
import pymupdf
from server import render_objects, font_list

PNG = base64.b64encode(pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 4, 4), False).tobytes("png")).decode()


def blank(rotation=0, text=None):
    d = pymupdf.open()
    p = d.new_page()
    if text:
        p.insert_text((72, 300), text, fontsize=12)
    p.set_rotation(rotation)
    return d.tobytes()


def txt(**kw):
    o = dict(type="text", page=0, x=60, y=60, w=300, h=80, text="Hello world", font="Georgia", size=20, color="#cc0000")
    o.update(kw)
    return o


def spans(page):
    return [s for b in page.get_text("dict")["blocks"] for l in b.get("lines", []) for s in l["spans"]]


def near(c, rgb, tol=0.02):
    return c is not None and all(abs(a - b) < tol for a, b in zip(c, rgb))


def test_text_full_styling():
    o = txt(bold=True, case="upper", spacing=1, lineHeight=1.5, align="center", bg="#ffff00", border={"color": "#ff00ff", "width": 2},
            underline={"on": True, "color": "#00aa00", "width": 2}, strike={"on": True, "color": "#0000ff"})
    p = render_objects(blank(), [o])[0]
    sp = spans(p)
    assert "".join(s["text"] for s in sp).replace(" ", "") == "HELLOWORLD"
    assert all("Georgia" in s["font"] and "Bold" in s["font"] and s["color"] == 0xCC0000 and abs(s["size"] - 20) < 0.01 for s in sp)
    x0, x1 = min(s["bbox"][0] for s in sp), max(s["bbox"][2] for s in sp)
    assert abs((x0 + x1) / 2 - 210) < 3  # centred in the box
    dr = p.get_drawings()
    assert any(near(d.get("fill"), (1, 1, 0)) for d in dr) and any(near(d.get("color"), (1, 0, 1)) and abs(d["width"] - 2) < 0.01 for d in dr)
    ul = [d for d in dr if near(d.get("color"), (0, 0.667, 0)) and abs(d["width"] - 2) < 0.01]
    stk = [d for d in dr if near(d.get("color"), (0, 0, 1))]
    base = sp[0]["origin"][1]
    assert ul and ul[0]["rect"].y0 > base and stk and stk[0]["rect"].y0 < base  # under vs through the text


def test_fonts_synthetic_and_opacity_rotation_script():
    p = render_objects(blank(), [txt(font="Impact", italic=True, opacity=0.5, script="sup", rot=90, y=300, text="Sup")])[0]
    tr = p.get_texttrace()
    assert tr and abs(tr[0]["opacity"] - 0.5) < 0.01 and "Impact" in tr[0]["font"]
    ln = [l for b in p.get_text("dict")["blocks"] for l in b.get("lines", [])][0]
    assert abs(ln["spans"][0]["size"] - 13) < 0.01 and abs(ln["dir"][1]) > 0.99  # 0.65em, rotated a quarter turn


def test_justify_and_wrap():
    o = txt(text="one two three four five six seven eight nine ten", align="justify", w=200, h=200, font="Arial", size=12)
    p = render_objects(blank(), [o])[0]
    lines = {}
    for s in spans(p):
        lines.setdefault(round(s["origin"][1]), []).append(s)
    rows = sorted(lines)
    assert len(rows) >= 2
    rights = [max(s["bbox"][2] for s in lines[r]) for r in rows[:-1]]
    assert all(abs(r - rights[0]) < 1.5 for r in rights)  # every wrapped line ends flush


def test_shapes_annots_image_whiteout():
    objs = [
        dict(type="rect", page=0, x=50, y=400, w=80, h=40, stroke="#ff0000", fill="#00ff00", width=3),
        dict(type="ellipse", page=0, x=150, y=400, w=80, h=40, stroke="#0000ff", width=2),
        dict(type="arrow", page=0, x=250, y=400, w=80, h=40, stroke="#123456", width=2, pts=[[0, 0], [1, 1]]),
        dict(type="ink", page=0, x=350, y=400, w=60, h=40, stroke="#654321", width=2, paths=[[[0, 0], [0.5, 1], [1, 0]]]),
        dict(type="highlight", page=0, x=50, y=500, w=100, h=20, fill="#ffe14d"),
        dict(type="note", page=0, x=200, y=500, w=24, h=24, text="check this"),
        dict(type="image", page=0, x=300, y=500, w=50, h=50, data=PNG),
        dict(type="whiteout", page=0, x=60, y=285, w=200, h=20),
    ]
    p = render_objects(blank(text="secret words"), objs)[0]
    dr = p.get_drawings()
    assert any(near(d.get("fill"), (0, 1, 0)) for d in dr) and any(near(d.get("color"), (1, 0, 0)) and abs(d["width"] - 3) < 0.01 for d in dr)
    assert any(near(d.get("color"), (0, 0, 1)) and any(i[0] == "c" for i in d["items"]) for d in dr)  # ellipse = curves
    assert sum(1 for d in dr if near(d.get("color"), (0x12 / 255, 0x34 / 255, 0x56 / 255))) >= 2  # shaft + head
    assert any(near(d.get("color"), (0x65 / 255, 0x43 / 255, 0x21 / 255)) for d in dr)
    kinds = sorted(a.type[1] for a in p.annots())
    assert kinds == ["Highlight", "Text"]
    assert p.get_images() and "secret" not in p.get_text()


def test_rotated_page_keeps_displayed_position():
    data = blank(rotation=90)
    p = render_objects(data, [txt(x=100, y=100, w=200, h=40, text="Turned", size=12, font="Arial")])[0]
    s = spans(p)[0]
    o = pymupdf.Point(s["origin"]) * p.rotation_matrix
    assert abs(o.x - 104) < 2 and 100 < o.y < 120


def test_font_list_has_every_category():
    cats = {f["cat"] for f in font_list()}
    assert {"Sans", "Serif", "Mono", "Display", "Handwriting"} <= cats




def test_decoration_styles_and_box():
    o = txt(text="Deco line", font="Arial", size=20, color="#222222",
            underline={"on": True, "style": "dashed", "color": "#ff0000", "width": 1.5},
            strike={"on": True},  # default thickness must be > 0
            overline={"on": True, "style": "double", "color": "#0000ff", "width": 1},
            marker="#ffe14d", border={"color": "#00aa00", "width": 2, "style": "dotted"}, radius=8,
            outline={"on": True, "color": "#ff00ff", "width": 0.8}, link="https://example.com")
    p = render_objects(blank(), [o])[0]
    dr = p.get_drawings()
    ul = [d for d in dr if near(d.get("color"), (1, 0, 0))]
    assert ul and ul[0].get("dashes") and "4.5" in ul[0]["dashes"] and abs(ul[0]["width"] - 1.5) < 0.01
    stk = [d for d in dr if near(d.get("color"), (0x22 / 255,) * 3) and d["type"] == "s"]
    assert stk and stk[0]["width"] >= 0.75
    assert len([d for d in dr if near(d.get("color"), (0, 0, 1))]) == 2  # double overline
    assert any(near(d.get("fill"), (1, 0xe1 / 255, 0x4d / 255)) for d in dr)  # marker
    brd = [d for d in dr if near(d.get("color"), (0, 0.667, 0))]
    assert brd and brd[0].get("dashes") and any(i[0] == "c" for i in brd[0]["items"])  # dotted + rounded
    assert "ff00ff" not in "" and p.get_links()[0]["uri"] == "https://example.com"
    assert "1 0 1 RG" in "".join(p.parent.xref_stream(x).decode("latin-1") for x in p.get_contents())  # outline colour


def test_wavy_lists_case_spacing_valign():
    o = txt(text="alpha beta\ngamma", font="Arial", size=12, w=300, h=200, list="number", case="title", smallCaps=False,
            underline={"on": True, "style": "wavy", "color": "#123456", "width": 1}, paraBefore=6, paraAfter=6,
            valign="bottom", wordSpacing=5)
    p = render_objects(blank(), [o])[0]
    txt_ = p.get_text()
    assert "Alpha Beta" in txt_ and "1." in txt_ and "2." in txt_
    wav = [d for d in p.get_drawings() if near(d.get("color"), (0x12 / 255, 0x34 / 255, 0x56 / 255))]
    assert wav and len(wav[0]["items"]) > 5  # a wave is a polyline, not one segment
    ys = sorted({round(s["origin"][1]) for s in spans(p)})
    assert ys[-1] > 60 + 200 - 40  # bottom-aligned
    sc = render_objects(blank(), [txt(text="Ab", smallCaps=True, size=20, font="Arial")])[0]
    sizes = sorted({round(s["size"]) for s in spans(sc)})
    assert sizes == [14, 20]


def test_plain_text_has_no_box():
    # new text boxes and retyped (inline) text export as bare glyphs: no rectangle, stroke or fill
    from server import Session
    default = txt(text="Plain", color="#000000", border={"style": "solid", "color": None, "width": 1, "sides": "trbl"}, bg=None, padding=4)
    inline = txt(text="Invoice total 1234", inline=True, padding=0, fit="hug", border={"color": None, "width": 1})
    p = render_objects(blank(), [default])[0]
    assert p.get_drawings() == [] and "Plain" in p.get_text()
    s = Session(blank(text="Invoice total 1234"), "t.pdf")
    s.apply("erase_text", {"page": 0, "rect": [72, 288, 182, 303]})
    p = render_objects(s.export(), [dict(inline, x=72, y=286, w=110, h=16.8, size=14, font="Arial")])[0]
    assert p.get_drawings() == [] and p.get_text().count("Invoice total 1234") == 1


def test_white_highlight_is_fill_only():
    p = render_objects(blank(), [txt(text="Marked", marker="#ffffff", bg="#ffffff")])[0]
    dr = p.get_drawings()
    assert dr and all(d["type"] == "f" and d.get("color") is None for d in dr)  # fills only, never a stroked rect
    assert render_objects(blank(), [txt(text="Bare", marker=None, bg=None)])[0].get_drawings() == []


def test_underline_single_clean_rule():
    o = txt(text="May-2", font="Times New Roman", bold=True, italic=True, size=14, color="#000000", underline={"on": True})
    p = render_objects(blank(), [o])[0]
    dr = [d for d in p.get_drawings() if d["type"] == "s"]
    assert len(dr) == 1 and len(dr[0]["items"]) == 1  # one straight segment, drawn once
    sp = spans(p)[0]
    assert dr[0]["rect"].y0 > sp["origin"][1]  # below the baseline, from the font's post table
    on = render_objects(blank(), [dict(o, underline={"on": True, "skipInk": True})])[0]
    segs = [d for d in on.get_drawings() if d["type"] == "s"]
    assert len(segs) == 2  # one clean gap at the y descender, no stray dots


if __name__ == "__main__":  # run every test_* in order
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn(); print("ok", name)
