"""Extract bounded text locally from text, PDF and DOCX attachments.

Queue items receive text, never a document to execute. Optional python-docx is
loaded only when a DOCX needs extraction; PDFs use a bounded pdftotext process.
"""

import importlib
import os
import subprocess

DOC_TEXT_CHARS = 6000  # max extracted chars per document

TEXT_EXTS = (
    ".txt",
    ".md",
    ".markdown",
    ".csv",
    ".json",
    ".jsonl",
    ".log",
    ".py",
    ".js",
    ".ts",
    ".tsx",
    ".jsx",
    ".rs",
    ".go",
    ".java",
    ".c",
    ".cpp",
    ".h",
    ".hpp",
    ".cs",
    ".rb",
    ".sh",
    ".yaml",
    ".yml",
    ".toml",
    ".xml",
    ".html",
    ".css",
    ".sql",
)

PDF_EXTS = (".pdf",)

DOCX_EXTS = (".docx",)

PDFTOTEXT_TIMEOUT = 30


def _read_pdf_text(path, filename):
    try:
        proc = subprocess.run(
            ["pdftotext", "-layout", path, "-"],
            capture_output=True,
            text=True,
            timeout=PDFTOTEXT_TIMEOUT,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout
        print(f"pdftotext found no text in {filename}", flush=True)
        return None
    except Exception as e:
        print(f"pdftotext failed ({filename}): {e}", flush=True)
        return None


def _read_docx_text(path, filename):
    try:
        docx = importlib.import_module("docx")

        doc = docx.Document(path)
        text = "\n".join(p.text for p in doc.paragraphs)
        return text
    except Exception as e:
        print(f"docx read failed ({filename}): {e}", flush=True)
        return None


def extract_document_text(path, content_type, filename):
    """Extract readable text from a downloaded document. Returns the text
    (truncated) or None when nothing usable could be extracted. Runs only
    local subprocess/library extraction: never executes the document."""
    ctype = (content_type or "").lower()
    ext = os.path.splitext(filename.lower())[1]

    def _truncate(t):
        t = (t or "").strip()
        if len(t) > DOC_TEXT_CHARS:
            t = t[:DOC_TEXT_CHARS] + "\n[...truncated...]"
        return t or None

    # Plain text and friends: read directly.
    if (
        ctype.startswith("text/")
        or ext in TEXT_EXTS
        or ctype.endswith("+json")
        or ctype in ("application/json", "application/x-sh")
    ):
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                return _truncate(f.read())
        except Exception as e:
            print(f"Text read failed ({filename}): {e}", flush=True)
            return None

    if ctype == "application/pdf" or ext in PDF_EXTS:
        return _truncate(_read_pdf_text(path, filename))

    # DOCX: python-docx paragraph text.
    if (
        ctype == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        or ext in DOCX_EXTS
    ):
        return _truncate(_read_docx_text(path, filename))

    return None
