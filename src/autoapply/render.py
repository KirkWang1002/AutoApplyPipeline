"""M4 (optional): render tailored resume.md / cover_letter.md to PDF with headless Chromium.

ATS upload fields want PDF or DOCX, not Markdown. Rendering runs with JavaScript off and
all network requests blocked, so model-written Markdown cannot run scripts or fetch URLs.
"""
from __future__ import annotations

import html
from pathlib import Path
from typing import Any

PDF_CSS = """
@page { size: Letter; margin: 0.6in 0.7in; }
body { font-family: "Helvetica Neue", Arial, sans-serif; font-size: 10.5pt; line-height: 1.35; color: #111; }
h1 { font-size: 20pt; margin: 0 0 4pt; }
h2 { font-size: 12pt; text-transform: uppercase; letter-spacing: .04em; border-bottom: 1px solid #999;
     margin: 12pt 0 4pt; padding-bottom: 2pt; }
h3 { font-size: 11pt; margin: 8pt 0 2pt; }
p { margin: 0 0 6pt; }
ul { margin: 2pt 0 6pt; padding-left: 16pt; }
li { margin: 0 0 2pt; }
a { color: #111; text-decoration: none; }
code { font-family: inherit; }
"""


def markdown_to_html(md_text: str, title: str = "") -> str:
    """Full HTML document for `md_text`, styled for a one-page Letter print."""
    try:
        import markdown
    except ImportError as e:
        raise RuntimeError('The `markdown` package is missing. Run: pip install -e ".[prefill]"') from e
    body = markdown.markdown(md_text, extensions=["sane_lists"])
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>{html.escape(title)}</title><style>{PDF_CSS}</style></head>"
        f"<body>{body}</body></html>"
    )


def render_pdf(md_path: Path, browser: Any, pdf_path: Path | None = None) -> Path:
    """Render one Markdown file to PDF next to it (or at `pdf_path`). Returns the PDF path."""
    md_path = Path(md_path)
    pdf_path = Path(pdf_path) if pdf_path else md_path.with_suffix(".pdf")
    doc = markdown_to_html(md_path.read_text(encoding="utf-8"), title=md_path.stem)
    context = browser.new_context(java_script_enabled=False)
    try:
        page = context.new_page()
        page.route("**/*", lambda route: route.abort())  # no network: offline render only
        page.set_content(doc, wait_until="domcontentloaded")
        page.pdf(path=str(pdf_path), format="Letter", print_background=True,
                 margin={"top": "0", "bottom": "0", "left": "0", "right": "0"})
    finally:
        context.close()
    return pdf_path


def needs_render(md_path: Path) -> bool:
    """True if the PDF next to `md_path` is missing or older than the Markdown."""
    pdf = md_path.with_suffix(".pdf")
    return md_path.is_file() and (not pdf.is_file() or pdf.stat().st_mtime < md_path.stat().st_mtime)


def render_folder(dest: Path, browser: Any, force: bool = False) -> list[Path]:
    """Render resume.md + cover_letter.md in a tailored folder. Returns PDFs written."""
    from autoapply.tailor import COVER_LETTER_FILE, RESUME_FILE

    written = []
    for name in (RESUME_FILE, COVER_LETTER_FILE):
        md = Path(dest) / name
        if md.is_file() and (force or needs_render(md)):
            written.append(render_pdf(md, browser))
    return written
