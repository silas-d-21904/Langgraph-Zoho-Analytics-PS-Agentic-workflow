#!/usr/bin/env python3
"""validate.py — pragmatic validity checks for a .docx.

python-docx has no bundled XSD validator, and full OOXML schema validation is
heavy. This wrapper runs the checks that actually catch the failures you hit in
practice, in increasing order of cost:

  1. ZIP integrity        — the file is a readable archive.
  2. Required parts        — [Content_Types].xml and word/document.xml exist.
  3. XML well-formedness   — every .xml/.rels part parses (lxml).
  4. python-docx open      — the object model can load it.
  5. Render round-trip     — LibreOffice converts it to PDF (optional, --render).

Exit code is 0 only if all *attempted* checks pass. Use --render in a verify
step; skip it for a fast structural gate.

Usage:
    python validate.py out.docx
    python validate.py out.docx --render          # also confirm it renders
    python validate.py out.docx --render --keep-pdf
    python validate.py out.docx --render --render-timeout 240

Requires: lxml + python-docx. --render additionally needs LibreOffice (soffice).
"""
import argparse
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import zipfile

from lxml import etree

REQUIRED_PARTS = ("[Content_Types].xml", "word/document.xml")


class Reporter:
    def __init__(self):
        self.failed = False

    def ok(self, msg):
        print(f"  [ok]   {msg}")

    def warn(self, msg):
        print(f"  [warn] {msg}")

    def fail(self, msg):
        print(f"  [FAIL] {msg}")
        self.failed = True


def check_zip(path, rep):
    if not zipfile.is_zipfile(path):
        rep.fail(f"{path} is not a valid ZIP archive")
        return None
    zf = zipfile.ZipFile(path, "r")
    bad = zf.testzip()
    if bad is not None:
        rep.fail(f"corrupt entry in archive: {bad}")
        return None
    rep.ok("archive is a readable ZIP")
    return zf


def check_required_parts(zf, rep):
    names = set(zf.namelist())
    for part in REQUIRED_PARTS:
        if part in names:
            rep.ok(f"present: {part}")
        else:
            rep.fail(f"missing required part: {part}")


def check_xml_wellformed(zf, rep):
    xml_parts = [n for n in zf.namelist() if n.endswith(".xml") or n.endswith(".rels")]
    for name in xml_parts:
        try:
            etree.fromstring(zf.read(name))
        except etree.XMLSyntaxError as e:
            rep.fail(f"malformed XML in {name}: {e}")
    if not rep.failed:
        rep.ok(f"all {len(xml_parts)} XML part(s) well-formed")


def check_python_docx(path, rep):
    try:
        import docx
    except ImportError:
        rep.warn("python-docx not installed; skipping object-model open")
        return
    try:
        d = docx.Document(path)
        n_par = len(d.paragraphs)
        n_tbl = len(d.tables)
        rep.ok(f"python-docx opened it ({n_par} paragraphs, {n_tbl} tables)")
    except Exception as e:
        rep.fail(f"python-docx could not open it: {type(e).__name__}: {e}")


def find_soffice():
    for cand in ("soffice", "libreoffice"):
        from shutil import which
        p = which(cand)
        if p:
            return p
    # common script wrapper used by the JS docx skill, if present
    return None


def run_soffice(command, timeout):
    proc = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
        return proc.returncode, stdout, stderr
    except subprocess.TimeoutExpired:
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGTERM)
        else:
            proc.terminate()
        try:
            proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            if os.name == "posix":
                os.killpg(proc.pid, signal.SIGKILL)
            else:
                proc.kill()
            proc.communicate()
        raise


def check_render(path, rep, keep_pdf, render_timeout):
    soffice = find_soffice()
    if not soffice:
        rep.warn("LibreOffice (soffice) not found; skipping render check")
        return
    source_path = Path(path).resolve()
    pdf_name = source_path.with_suffix(".pdf").name
    with tempfile.TemporaryDirectory(prefix="docx-validate-profile-") as profile_dir:
        with tempfile.TemporaryDirectory(prefix="docx-validate-output-") as temp_outdir:
            outdir = Path.cwd() if keep_pdf else Path(temp_outdir)
            pdf_path = outdir / pdf_name
            if pdf_path.exists():
                pdf_path.unlink()
            command = [
                soffice,
                f"-env:UserInstallation={Path(profile_dir).resolve().as_uri()}",
                "--headless",
                "--convert-to",
                "pdf",
                "--outdir",
                str(outdir.resolve()),
                str(source_path),
            ]
            try:
                returncode, stdout, stderr = run_soffice(command, render_timeout)
            except subprocess.TimeoutExpired:
                rep.fail(
                    f"LibreOffice render timed out ({render_timeout}s). On macOS in VS Code, "
                    "rerun outside the terminal sandbox; blocked macOS services can prevent "
                    "LibreOffice from starting."
                )
                return
            if pdf_path.is_file() and pdf_path.stat().st_size > 0:
                rep.ok(f"LibreOffice rendered it to PDF ({pdf_path.stat().st_size} bytes)")
                if keep_pdf:
                    print(f"         kept: {pdf_path}")
                return
            details = (stderr or stdout).strip()[:500]
            rep.fail(f"LibreOffice produced no PDF (exit {returncode}). output: {details}")


def main():
    ap = argparse.ArgumentParser(description="Pragmatic .docx validity checks.")
    ap.add_argument("docx", help="path to the .docx to validate")
    ap.add_argument("--render", action="store_true", help="also confirm it renders via LibreOffice")
    ap.add_argument("--keep-pdf", action="store_true", help="with --render, keep the PDF in the cwd")
    ap.add_argument(
        "--render-timeout",
        type=int,
        default=240,
        help="maximum seconds for LibreOffice rendering (default: 240)",
    )
    args = ap.parse_args()

    if not os.path.isfile(args.docx):
        sys.exit(f"error: {args.docx} not found")

    print(f"Validating {args.docx}")
    rep = Reporter()

    zf = check_zip(args.docx, rep)
    if zf is not None:
        check_required_parts(zf, rep)
        check_xml_wellformed(zf, rep)
        zf.close()
    check_python_docx(args.docx, rep)
    if args.render:
        check_render(args.docx, rep, args.keep_pdf, args.render_timeout)

    print()
    if rep.failed:
        print("RESULT: FAIL")
        sys.exit(1)
    print("RESULT: PASS")
    sys.exit(0)


if __name__ == "__main__":
    main()
