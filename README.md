# PDF editor

A local PDF text editor: click any text to retype it, in the original font, colour and position.
Scanned (image-only) pages are handled too: `OCR scans` adds a text layer, and editing there
re-typesets the words into the scan itself.

    ./run.sh            # http://localhost:8765

## Editing inside a scan

A scanned page has no fonts to inherit, so a replacement word has to be *rebuilt* to sit in the
scan convincingly. For each edit (`Session.edit_scanned` in `server.py`):

1. **Find the ink, not the box.** OCR word boxes are padded; the printed ink is the truth. The
   baseline is measured from the scan and the replacement is placed on the *pen* position the old
   word started at (`ink_span`, `line_baseline`, `_side_bearing`) — aligning ink edges instead would
   shift the line whenever the new first letter has a different side bearing.
2. **Pick the font by eye.** Every installed font is rendered and correlated with the scan
   (`match_font`), with fonts already confident elsewhere on the page as a tie-break (`page_fonts`).
3. **Pick the size by eye too.** Fitting the advance width reads a few percent small on blurred ink,
   so the glyph shapes are correlated over a coarse-to-fine sweep, and softness is then chosen from a
   narrow scan-relative band (`fit_scan_text`). Softness scales with the scan's dpi; wider sweeps
   measurably make the result softer than the ink around it rather than closer to it.
4. **Erase only the ink.** `clean_patch` rebuilds the paper under the old word from the scan's own
   pixels: rules, underlines, grain and shading are kept byte-identical, so erasing a name cannot
   leave a white box or eat the line it was written on.
5. **Draw scan-like pixels, not vector glyphs.** Razor-sharp vector text on a JPEG scan is the
   giveaway. The replacement is rasterised above the scan's dpi (an embedded raster gets resampled
   again by every viewer, which double-softens it) and softened to match (`glyph_png`), then an
   invisible text layer carries the same font, size and colour so the word stays selectable and
   re-editable (`render_mode=3`, colour remembered in `Session.painted`).
6. **Leave the rest alone.** Untouched words keep their pixels — including their original spacing,
   which is why the tail moves by *advance* deltas rather than ink-edge deltas.

`PDF_REDACT_IMAGE_PIXELS` is deliberately not used: it makes MuPDF re-encode the whole scan, so
every untouched pixel shifts. The opaque patch covers the old ink instead.

## Tests

    .venv/bin/python test_server.py             # core editor: fonts, reflow, rotation, undo, export
    .venv/bin/python tests/test_scanned_cert.py # scanned editing against a synthetic scan
    .venv/bin/python tests/lint_names.py server.py # used-but-undefined names

`tests/make_fixture.py` builds an image-only certificate-like form (invented institute and names,
JPEG'd, slightly skewed, scanner-softened, mixed-script header, serif-italic name on an underlined
blank). It resamples bilinearly like a real capture: a nearest-neighbour skew gives the raster
artificial aliasing that no scanner produces, and that aliasing otherwise caps how well any edit can
match. Each test
builds the same fixture **with the new text already in it** and compares the edited region against
that native reference — so "looks right" is a number:

    .venv/bin/python tests/simcheck.py edited.pdf reference.pdf 'x0,y0,x1,y1'
    -> ncc 0.96, shift (0.0, 0.0), scale 0.99

`ncc` is the ink correlation after the best shift (1.0 = identical), `shift` is how far the edit sits
from where the document would have printed it, `scale` is the ink-mass ratio (1.0 = right size).

Note: an edited scan word ends up as pixels plus an invisible text layer, so it extracts as OCR text
(`ocr: true` in `/api/spans`). Untouched words in the same run still report the fallback Helvetica
name in that layer — a known cosmetic gap, not a rendering one.
