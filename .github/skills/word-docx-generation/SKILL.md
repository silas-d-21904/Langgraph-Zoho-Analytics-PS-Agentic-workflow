---
name: docx-python
description: "Use this skill whenever the user wants to create, read, edit, or manipulate Word documents (.docx files) using Python. Triggers include: any mention of 'Word doc', 'word document', '.docx', or requests to produce professional documents with formatting like tables of contents, headings, page numbers, tables, images, or letterheads. Also use when extracting or reorganizing content from .docx files, inserting or replacing images, performing find-and-replace, working with tracked changes or comments, or converting content into a polished Word document. Do NOT use for PDFs, spreadsheets, or Google Docs."
license: Community reference. python-docx is BSD-licensed.
---

# DOCX creation, editing, and analysis with Python (python-docx)

A `.docx` is a ZIP archive of XML files. This skill uses [`python-docx`](https://python-docx.readthedocs.io) for creation and editing, and `pandoc` / LibreOffice for reading and conversion. Choose the approach by task:

| Task | Approach |
|---|---|
| **Create** a new document | `python-docx` — `Document()`, add content, `.save()` |
| **Edit** an existing document | `python-docx` — `Document("in.docx")` opens existing files directly (a key advantage over the JS `docx` library) |
| **Deep XML edits** (tracked changes, comments, shading) | Reach into `python-docx`'s underlying `oxml` layer, or `unzip` → edit `word/document.xml` → `zip` |
| **Read** content | `pandoc -t markdown file.docx` (fast) or iterate paragraphs/tables in `python-docx` |

## Required workflow (do not skip)

Whenever this skill is used to **create or edit** a `.docx`, the final step is mandatory:

1. Save the document.
2. Run `python scripts/validate.py <file> --render` from the skill directory.
3. If the result is not `RESULT: PASS`, fix the document and re-run. Do **not** report the task as complete until validation passes.

Before a find-and-replace on an existing document, first run `python scripts/merge_runs.py <file>` so the target text is contiguous and matchable.

## Install

```bash
pip install python-docx        # imports as `docx`, NOT `pip install docx`
```

`import docx` then `docx.Document()`. The package name on PyPI is `python-docx`; the import name is `docx`. Installing the unrelated `docx` package is a common mistake — it will break your imports.

Standard imports used throughout this skill:

```python
from docx import Document
from docx.shared import Inches, Pt, Cm, RGBColor, Twips, Emu
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_TAB_ALIGNMENT, WD_TAB_LEADER, WD_BREAK, WD_LINE_SPACING
from docx.enum.section import WD_ORIENT, WD_SECTION
from docx.enum.table import WD_TABLE_ALIGNMENT, WD_ALIGN_VERTICAL
from docx.oxml.ns import qn
from docx.oxml import OxmlElement
```

## Units

python-docx uses EMU internally but gives you helpers so you rarely touch raw numbers:

- `Inches(1)`, `Cm(2.54)`, `Pt(12)` — convert to EMU automatically.
- `Twips(n)` — twentieths of a point (DXA); `1440 Twips = 1 inch`. Useful when matching XML values.
- Read back with `.inches`, `.pt`, `.twips`, e.g. `section.page_width.inches`.

## Creating — the gotchas

The API is discoverable; these are the footguns that fail silently or render wrong.

### Page size — python-docx defaults to US Letter

Unlike the JS `docx` library (which defaults to A4), the stock `python-docx` template is already **US Letter (8.5″ × 11″)**. Set it explicitly only if you need to be certain or want A4:

```python
sec = doc.sections[0]
sec.page_width  = Inches(8.5)      # US Letter
sec.page_height = Inches(11)
# A4: Cm(21) x Cm(29.7)
```

### Landscape — you must swap width/height yourself

python-docx does **not** swap dimensions when you change orientation (the JS library does). Set the orientation flag *and* the dimensions:

```python
sec.orientation = WD_ORIENT.LANDSCAPE
sec.page_width, sec.page_height = Inches(11), Inches(8.5)   # wide first
```

### Margins

```python
sec.top_margin = sec.bottom_margin = Inches(1)
sec.left_margin = sec.right_margin = Inches(1)
```

### Headings — required for a working Table of Contents

Use the built-in heading styles so Word's TOC field can find them. `add_heading(text, level)` maps to the built-in `Heading N` styles:

```python
doc.add_heading("Document Title", level=0)   # level 0 = Title style
doc.add_heading("Section One", level=1)
doc.add_heading("Subsection", level=2)
```

If you make a *custom* heading style, it must carry an `outlineLevel` or the TOC won't pick it up — prefer the built-ins.

### Table of Contents — inject the field manually

python-docx has no native TOC. Insert a `TOC` field; Word/LibreOffice populates it when the field updates (right-click → Update Field, or it updates on open in many viewers). Verified working:

```python
def add_toc(doc):
    para = doc.add_paragraph()
    run = para.add_run()
    begin = OxmlElement("w:fldChar"); begin.set(qn("w:fldCharType"), "begin")
    instr = OxmlElement("w:instrText"); instr.set(qn("xml:space"), "preserve")
    instr.text = r'TOC \o "1-3" \h \z \u'   # levels 1-3, hyperlinked, hide page nums in web view
    sep = OxmlElement("w:fldChar"); sep.set(qn("w:fldCharType"), "separate")
    placeholder = OxmlElement("w:t"); placeholder.text = "Right-click and select Update Field."
    end = OxmlElement("w:fldChar"); end.set(qn("w:fldCharType"), "end")
    for el in (begin, instr, sep, placeholder, end):
        run._r.append(el)
```

To force the field to refresh on open, set `<w:updateFields w:val="true"/>` in `settings.xml`:

```python
def force_update_fields(doc):
    settings = doc.settings.element
    el = OxmlElement("w:updateFields"); el.set(qn("w:val"), "true")
    settings.append(el)
```

**Pitfall — the "Table of Contents" page label must NOT be a real heading.** The `TOC \o "1-3"`
switch scans the whole document for paragraphs carrying the built-in `Heading 1-3` styles (by
outline level). If you render the "Table of Contents" title on the TOC page itself using
`doc.add_heading(...)`, it becomes Heading-1-styled text and the TOC field will list itself as its
own first entry when Word updates the field. Give that page's title matching visual styling
(bold, large font, accent color) via a plain `doc.add_paragraph()` + manual run formatting instead
of a real heading style — never `add_heading()` — for that one label only:

```python
def add_toc_page_title(doc, text):
    p = doc.add_paragraph()
    run = p.add_run(text)
    run.font.bold = True
    run.font.size = Pt(16)
    return p
```

Verify by opening the generated docx with python-docx and confirming the "Table of Contents"
paragraph's `p.style.name` is `Normal`, not `Heading 1` (all real section headings should still be
`Heading 1`/`Heading 2`).

### Lists — use built-in styles, never a literal bullet

Do not type `•` or `1.` into the text. Use the built-in list styles so numbering is real and continues correctly:

```python
doc.add_paragraph("First bullet",  style="List Bullet")
doc.add_paragraph("Second bullet", style="List Bullet")
doc.add_paragraph("First step",    style="List Number")
doc.add_paragraph("Second step",   style="List Number")
```

Nested levels use `List Bullet 2`, `List Bullet 3`, `List Number 2`, etc. For fully custom numbering (restart-at, custom formats) you must edit `word/numbering.xml` — see "Deep XML edits."

### Newlines — separate paragraphs or explicit breaks

Never put `\n` in run text; it is ignored or mangled. Use a new paragraph for a new block, or an explicit line break within a paragraph:

```python
doc.add_paragraph("Line one.")
doc.add_paragraph("Line two.")               # separate blocks
p = doc.add_paragraph("Same paragraph,")
p.add_run().add_break(WD_BREAK.LINE)         # soft line break
p.add_run("continued on next line.")
```

### Page breaks

```python
doc.add_page_break()                          # simplest
# or inside an existing paragraph:
p.add_run().add_break(WD_BREAK.PAGE)
```

### Tables — set width on the table AND every cell, in fixed units

Percentage widths render inconsistently (notably in Google Docs). Use fixed widths and disable autofit, then set the width on **every cell** in a column — setting only `column.width` is unreliable because Word tracks width per-cell:

```python
table = doc.add_table(rows=3, cols=3)
table.style = "Table Grid"                    # gives visible borders
table.alignment = WD_TABLE_ALIGNMENT.CENTER
table.autofit = False
table.allow_autofit = False

col_widths = (Inches(2), Inches(2.5), Inches(2))   # should sum to usable page width
for row in table.rows:
    for idx, cell in enumerate(row.cells):
        cell.width = col_widths[idx]

# Fill cells
table.rows[0].cells[0].text = "Name"
```

Column widths should sum to the printable width (page width − left − right margins), e.g. 6.5″ for Letter with 1″ margins.

### Table cell shading — no native API; add `<w:shd>`

python-docx has no cell-fill method. Use `clear` for the pattern value (never `solid`, which renders as a black fill in some viewers) and set the color via the `fill` attribute. Verified working:

```python
def shade_cell(cell, hex_fill):               # hex_fill like "D9E2F3", no leading #
    tcPr = cell._tc.get_or_add_tcPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"),  "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"),  hex_fill)
    tcPr.append(shd)

shade_cell(table.rows[0].cells[0], "D9E2F3")  # light blue header
```

Merge cells with `a.merge(b)`. Vertical alignment: `cell.vertical_alignment = WD_ALIGN_VERTICAL.CENTER`.

### Horizontal rule — a paragraph bottom border, not a table

Don't fake a rule with a 1-row table. Add a bottom border to an empty paragraph. Verified working:

```python
def add_horizontal_rule(paragraph):
    pPr = paragraph._p.get_or_add_pPr()
    pbdr = OxmlElement("w:pBdr")
    bottom = OxmlElement("w:bottom")
    bottom.set(qn("w:val"), "single")
    bottom.set(qn("w:sz"), "6")               # eighths of a point
    bottom.set(qn("w:space"), "1")
    bottom.set(qn("w:color"), "auto")
    pbdr.append(bottom)
    pPr.append(pbdr)

add_horizontal_rule(doc.add_paragraph())
```

### Dot leaders / right-aligned-on-same-line — use a tab stop

For "Chapter 1 .......... 3" style lines, don't pad with dots or spaces. Add a right-aligned tab stop with a dot leader and press Tab between the runs. Verified working:

```python
p = doc.add_paragraph()
p.paragraph_format.tab_stops.add_tab_stop(
    Inches(6.5), WD_TAB_ALIGNMENT.RIGHT, WD_TAB_LEADER.DOTS)
p.add_run("Chapter 1")
p.add_run("\t")            # a literal tab is correct here; it triggers the tab stop
p.add_run("3")
```

`\t` (tab) is fine and expected; `\n` (newline) is the one to avoid.

### Images — `add_picture` with an explicit size

```python
doc.add_picture("logo.png", width=Inches(2))   # aspect ratio preserved if only one dim given
# center it:
doc.paragraphs[-1].alignment = WD_ALIGN_PARAGRAPH.CENTER
```

Give width **or** height (not both) to preserve aspect ratio. Supported formats include PNG, JPEG, GIF, BMP, TIFF. To place an image inside a table cell, add it to a paragraph within the cell:

```python
run = table.rows[0].cells[0].paragraphs[0].add_run()
run.add_picture("logo.png", width=Inches(1))
```

### Text formatting

```python
p = doc.add_paragraph()
r = p.add_run("Bold blue 14pt")
r.bold = True
r.font.size = Pt(14)
r.font.color.rgb = RGBColor(0x1F, 0x4E, 0x79)
r.font.name = "Calibri"
p.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
p.paragraph_format.space_after = Pt(6)
p.paragraph_format.line_spacing_rule = WD_LINE_SPACING.ONE_POINT_FIVE
```

### Headers and footers (incl. page numbers)

```python
sec = doc.sections[0]
sec.header.paragraphs[0].text = "Company Confidential"

# Page-number field in the footer:
footer_p = sec.footer.paragraphs[0]
footer_p.alignment = WD_ALIGN_PARAGRAPH.CENTER
run = footer_p.add_run()
for typ, txt in (("begin", None), (None, "PAGE"), ("end", None)):
    if typ:
        fld = OxmlElement("w:fldChar"); fld.set(qn("w:fldCharType"), typ); run._r.append(fld)
    else:
        instr = OxmlElement("w:instrText"); instr.set(qn("xml:space"), "preserve")
        instr.text = txt; run._r.append(instr)
```

Set `sec.different_first_page_header_footer = True` to give the first page its own header/footer.

### House-style logo (cover, header, footer) — default for all Discovery Agent documents

The canonical logo asset lives at `.github/skills/word-docx-generation/assets/logo.png`. Copy it
into the project's own deliverables folder (e.g. `deliverables/<project-slug>/logo.png`) before
running the generation script, and reference it via a `LOGO_PATH` constant. Apply it in three
places on every generated document (internal guide and customer doc alike):

1. **Cover page** — a picture run at the very top, before the eyebrow label:
   ```python
   p = doc.add_paragraph()
   p.alignment = WD_ALIGN_PARAGRAPH.LEFT
   run = p.add_run()
   run.add_picture(LOGO_PATH, height=Inches(0.55))
   p.paragraph_format.space_after = Pt(28)
   ```
2. **Header (logo only, no title text)** — on every page *after* the cover. Use
   `different_first_page_header_footer` so the cover page's own header stays blank, and add a thin
   bottom rule for a subtle separator:
   ```python
   sec.different_first_page_header_footer = True
   sec.first_page_header.paragraphs[0].text = ""     # blank on the cover page
   hp = sec.header.paragraphs[0]
   hp.alignment = WD_ALIGN_PARAGRAPH.LEFT
   run = hp.add_run()
   run.add_picture(LOGO_PATH, height=Inches(0.28))
   pPr = hp._p.get_or_add_pPr()
   pBdr = OxmlElement("w:pBdr")
   bottom = OxmlElement("w:bottom")
   bottom.set(qn("w:val"), "single"); bottom.set(qn("w:sz"), "4")
   bottom.set(qn("w:space"), "1"); bottom.set(qn("w:color"), "D9D9D9")
   pBdr.append(bottom); pPr.append(pBdr)
   ```
3. **Footer (logo + tagline, left) + page number (right)** — blank the first-page footer the same
   way, then on the main footer put the logo and a small gray tagline (e.g. "Zoho Analytics") on
   the left, with the `PAGE` field right-aligned via a tab stop:
   ```python
   sec.first_page_footer.paragraphs[0].text = ""
   fp = sec.footer.paragraphs[0]
   run_logo = fp.add_run()
   run_logo.add_picture(LOGO_PATH, height=Inches(0.16))
   tag = fp.add_run(f"  {tagline}\t")
   tag.font.size = Pt(8); tag.font.color.rgb = GRAY
   # ...then append the PAGE field run as shown above, with a right-aligned tab stop at the margin
   ```

**Pitfall:** don't forget `different_first_page_header_footer = True` — without it the cover page
inherits the same header/footer as every other page, duplicating the logo awkwardly next to the
cover's own logo/design.

## Verify the output

After saving, render it and actually look at it — silent layout bugs (a table overflowing the margin, a broken leader) only show up visually:

```bash
python scripts/validate.py output.docx --render --keep-pdf   # validates AND renders
pdftoppm -jpeg -r 100 output.pdf page                        # then open/Read the images
ls page-*.jpg
# (validate.py calls `soffice --headless --convert-to pdf` internally)
```

`pdftoppm` zero-pads page numbers to the width of the page count (`page-01.jpg` … `page-12.jpg`).

## Reading content

Fastest is pandoc:

```bash
pandoc -t markdown file.docx        # or -t plain
```

In Python, walk the document body. Note that `doc.paragraphs` and `doc.tables` are separate collections and don't preserve interleaved order; if order matters, iterate the body XML:

```python
from docx.document import Document as _Doc
from docx.oxml.table import CT_Tbl
from docx.oxml.text.paragraph import CT_P
from docx.table import Table
from docx.text.paragraph import Paragraph

def iter_block_items(parent):
    """Yield Paragraphs and Tables in document order."""
    body = parent.element.body
    for child in body.iterchildren():
        if isinstance(child, CT_P):
            yield Paragraph(child, parent)
        elif isinstance(child, CT_Tbl):
            yield Table(child, parent)

doc = Document("file.docx")
for block in iter_block_items(doc):
    if isinstance(block, Paragraph):
        print("P:", block.text)
    else:
        print("TABLE:", [[c.text for c in row.cells] for row in block.rows])
```

## Editing existing documents

python-docx opens existing files directly — no unzip needed for content-level edits:

```python
doc = Document("in.docx")
for p in doc.paragraphs:
    if "OLD" in p.text:
        # Replacing p.text drops formatting/runs. To keep formatting, edit runs:
        for run in p.runs:
            run.text = run.text.replace("OLD", "NEW")
doc.save("out.docx")
```

**Find-and-replace caveat:** Word splits a visible phrase across many `<w:r>` runs (revision ids, spell-check markers), so your search string often doesn't exist in any single run. Options: (a) match within `p.text` and rebuild the paragraph, or (b) drop to the XML and coalesce runs first (see below). Legacy `.doc` files must be converted first: `soffice --headless --convert-to docx file.doc`.

## Deep XML edits (unzip route)

For tracked changes, custom numbering, coalescing runs, or anything the object API doesn't expose, edit the XML directly:

```bash
unzip -q in.docx -d unpacked/
find unpacked -type l -delete        # strip symlink entries — external docx is untrusted
# edit unpacked/word/document.xml in place — do NOT reformat or pretty-print (whitespace is significant)
(cd unpacked && rm -f ../out.docx && zip -Xr ../out.docx .)
```

To make a searchable phrase contiguous before find-and-replace, merge adjacent identically-formatted runs. Minimal approach: for each `<w:p>`, collapse consecutive `<w:r>` elements that share the same `<w:rPr>` into one, concatenating their `<w:t>` text (set `xml:space="preserve"` on the merged `<w:t>`).

### Tracked changes (redlining)

Wrap inserted runs in `<w:ins>` and deleted runs in `<w:del>`, each with `w:id`, `w:author`, `w:date`:

- Inside `<w:del>`, the text element is `<w:delText>`, not `<w:t>`.
- A deleted paragraph mark is `<w:pPr><w:rPr><w:del w:id=".." w:author=".." w:date=".."/></w:rPr></w:pPr>` and means "merge this paragraph into the next." Deleting a paragraph outright = that mark **plus** a `<w:del>` around every run.
- The `<w:del/>` inside `rPr` must come **before** the other `rPr` children; child order is schema-enforced.

Accepting a deleted paragraph mark should join the paragraph to the one below. `pandoc --track-changes=accept` never joins them; LibreOffice joins correctly except when a deleted paragraph is followed by an empty spacer paragraph. A stray empty bullet in an "accepted" view is usually a view artifact — verify paragraph deletions in the XML, not just the rendered copy.

To produce a clean accepted copy: `pandoc --track-changes=accept in.docx -o out.docx` (with the join caveat above), or open+resave via LibreOffice.

### Comments

python-docx 1.1.0+ exposes a comments API:

```python
doc = Document("in.docx")
para = doc.paragraphs[0]
# Comment anchored to specific runs:
doc.add_comment(runs=para.runs, text="This cap is too low", author="Reviewer", initials="RV")
doc.save("annotated.docx")
```

If your version lacks it, comments require six cross-linked parts (`comments.xml`, `commentsExtended.xml`, `commentsIds.xml`, `commentsExtensible.xml`, the relationship entry, and content-type overrides) plus `<w:commentRangeStart/>`, `<w:commentRangeEnd/>`, and a `<w:commentReference/>` placed in `document.xml` where the comment anchors. Without the range markers the comment exists but is invisible.

## Full minimal example

```python
from docx import Document
from docx.shared import Inches, Pt, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from docx.oxml import OxmlElement

doc = Document()
sec = doc.sections[0]
sec.page_width, sec.page_height = Inches(8.5), Inches(11)     # US Letter
sec.top_margin = sec.bottom_margin = Inches(1)
sec.left_margin = sec.right_margin = Inches(1)

doc.add_heading("Quarterly Report", level=0)
doc.add_heading("Summary", level=1)
doc.add_paragraph("This quarter showed steady growth across all regions.")

doc.add_paragraph("Revenue up 12%",  style="List Bullet")
doc.add_paragraph("Costs down 3%",   style="List Bullet")

table = doc.add_table(rows=2, cols=2)
table.style = "Table Grid"
table.autofit = False; table.allow_autofit = False
for row in table.rows:
    for idx, cell in enumerate(row.cells):
        cell.width = Inches(3.25)
table.rows[0].cells[0].text = "Metric"
table.rows[0].cells[1].text = "Value"
table.rows[1].cells[0].text = "Revenue"
table.rows[1].cells[1].text = "$1.2M"

def shade_cell(cell, hex_fill):
    tcPr = cell._tc.get_or_add_tcPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear"); shd.set(qn("w:color"), "auto"); shd.set(qn("w:fill"), hex_fill)
    tcPr.append(shd)
for c in table.rows[0].cells:
    shade_cell(c, "D9E2F3")

doc.save("report.docx")
```

## Dependency map (JS skill → Python skill)

| JS `docx` skill piece | Python equivalent |
|---|---|
| `docx` (npm), `require('docx')` | `python-docx` (`import docx`) — **also opens existing files** |
| docx-js cannot edit existing files | `Document("in.docx")` works directly |
| `pandoc -t markdown` (read) | same, or `iter_block_items` in Python |
| `soffice.py` / `pdftoppm` (verify) | same tools, unchanged |
| `validate.py` (XSD checks) | **bundled** `validate.py` — pragmatic checks (ZIP, required parts, XML well-formedness, python-docx open, optional LibreOffice render) |
| `merge_runs.py` | **bundled** `merge_runs.py` — coalesces adjacent same-formatted plain-text runs |
| `accept_changes.py` | `pandoc --track-changes=accept` or LibreOffice resave |
| `comment.py` | `doc.add_comment(...)` (python-docx ≥ 1.1.0) or manual XML parts |

## Bundled helper scripts

Two standalone scripts ship with this skill so it is fully self-contained (no dependency on the JS skill's scripts). Both use only `lxml` + `python-docx` (plus LibreOffice for the optional render check).

### `merge_runs.py` — make text findable before find-and-replace

Coalesces consecutive `<w:r>` runs that share identical `<w:rPr>` and contain only `<w:t>` text, so a visible phrase becomes a contiguous string. Runs holding breaks, tabs, drawings, or fields are treated as barriers and never merged; no characters are inserted or removed, so the rendered document is unchanged.

```bash
python merge_runs.py in.docx -o out.docx      # rewrite a .docx
python merge_runs.py in.docx                  # in place
python merge_runs.py unpacked/                # rewrite word/document.xml in an unpacked dir
```

### `validate.py` — confirm the output isn't quietly broken

Runs checks in increasing cost order: ZIP integrity → required parts present → every XML part well-formed → python-docx can open it → (optional) LibreOffice renders it to PDF. Exits non-zero if any attempted check fails, so it works as a CI/verify gate.

```bash
python validate.py out.docx                   # fast structural gate
python validate.py out.docx --render          # also confirm it renders
python validate.py out.docx --render --keep-pdf
```

`validate.py` does not do full OOXML XSD validation (that needs the schema files and is rarely worth it); the render round-trip catches the practical failures Word/LibreOffice would choke on.

## Dependencies

`python-docx` (`pip install python-docx`) · `lxml` (pulled in by python-docx; used by both helper scripts) · `pandoc` · LibreOffice (`soffice`) · `pdftoppm` (Poppler). Everything after `lxml` is only needed for reading-via-pandoc, verification rendering, and legacy `.doc` conversion.
