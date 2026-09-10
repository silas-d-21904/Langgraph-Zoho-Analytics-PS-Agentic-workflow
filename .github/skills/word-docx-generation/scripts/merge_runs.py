#!/usr/bin/env python3
"""merge_runs.py — coalesce adjacent identically-formatted runs in a .docx.

Word splits a visible phrase across many <w:r> runs (revision ids, spell-check
markers, etc.), so a phrase you can see in the document often does not exist as
a contiguous string in word/document.xml. That breaks naive find-and-replace.

This script merges consecutive <w:r> elements inside each paragraph when they
share identical run properties (<w:rPr>) AND contain only plain text (<w:t>),
concatenating their text into the first run. Runs containing breaks, tabs,
drawings, fields, footnote refs, etc. are treated as barriers and never merged,
so nothing is lost or reordered. Content and rendering are byte-for-byte
unchanged in the rendered document — only the run boundaries move. It never
inserts or deletes characters, so it will not add spaces between runs that were
already adjacent (two runs "Before"+"break" become one run "Beforebreak", which
is exactly how it already rendered).

Usage:
    python merge_runs.py in.docx -o out.docx      # rewrite a .docx
    python merge_runs.py in.docx                  # in place (overwrites)
    python merge_runs.py unpacked/                # rewrite word/document.xml in an unpacked dir

Requires: lxml (pulled in by python-docx).
"""
import argparse
import os
import shutil
import sys
import tempfile
import zipfile

from lxml import etree

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
NS = {"w": W}


def _q(tag):
    return f"{{{W}}}{tag}"


def _rpr_key(run):
    """Serialized <w:rPr> used as the equality key. None-safe."""
    rpr = run.find(_q("rPr"))
    if rpr is None:
        return b""
    # Canonical-ish: c14n keeps attribute ordering stable across identical props.
    return etree.tostring(rpr, method="c14n")


def _is_plain_text_run(run):
    """True if the run's only content children are <w:t> (optionally with rPr).

    Any break/tab/drawing/field/etc. makes the run a merge barrier."""
    for child in run:
        tag = etree.QName(child).localname
        if tag == "rPr":
            continue
        if tag == "t":
            continue
        return False
    return True


def _merge_paragraph(p):
    """Merge eligible consecutive runs within one <w:p>. Returns count merged."""
    merged = 0
    runs = p.findall(_q("r"))
    prev = None
    prev_key = None
    for run in runs:
        if not _is_plain_text_run(run):
            prev = None
            prev_key = None
            continue
        key = _rpr_key(run)
        if prev is not None and key == prev_key:
            # Append this run's text to prev's <w:t>, then drop this run.
            prev_t = prev.find(_q("t"))
            cur_t = run.find(_q("t"))
            if prev_t is None:
                # prev had no text element yet; adopt current text into a new <w:t>
                prev_t = etree.SubElement(prev, _q("t"))
                prev_t.text = ""
            prev_text = prev_t.text or ""
            cur_text = (cur_t.text if cur_t is not None else "") or ""
            prev_t.text = prev_text + cur_text
            prev_t.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
            run.getparent().remove(run)
            merged += 1
        else:
            prev = run
            prev_key = key
    return merged


def merge_document_xml(xml_bytes):
    """Return (new_xml_bytes, total_merged)."""
    parser = etree.XMLParser(remove_blank_text=False)
    root = etree.fromstring(xml_bytes, parser)
    total = 0
    for p in root.iter(_q("p")):
        total += _merge_paragraph(p)
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True), total


def process_dir(unpacked_dir):
    doc_path = os.path.join(unpacked_dir, "word", "document.xml")
    if not os.path.isfile(doc_path):
        sys.exit(f"error: {doc_path} not found — is this an unpacked .docx?")
    with open(doc_path, "rb") as f:
        new_xml, total = merge_document_xml(f.read())
    with open(doc_path, "wb") as f:
        f.write(new_xml)
    return total


def process_docx(in_path, out_path):
    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".docx")
    os.close(tmp_fd)
    total = 0
    with zipfile.ZipFile(in_path, "r") as zin, zipfile.ZipFile(
        tmp_path, "w", zipfile.ZIP_DEFLATED
    ) as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename == "word/document.xml":
                data, total = merge_document_xml(data)
            zout.writestr(item, data)
    shutil.move(tmp_path, out_path)
    return total


def main():
    ap = argparse.ArgumentParser(description="Coalesce adjacent identically-formatted runs in a .docx.")
    ap.add_argument("input", help="path to a .docx file OR an unpacked directory")
    ap.add_argument("-o", "--output", help="output .docx (default: overwrite input; ignored for dir mode)")
    args = ap.parse_args()

    if os.path.isdir(args.input):
        total = process_dir(args.input)
        print(f"merged {total} run(s) in {os.path.join(args.input, 'word/document.xml')}")
    elif os.path.isfile(args.input):
        out = args.output or args.input
        total = process_docx(args.input, out)
        print(f"merged {total} run(s) -> {out}")
    else:
        sys.exit(f"error: {args.input} not found")


if __name__ == "__main__":
    main()
