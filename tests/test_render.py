"""PDF rendering tests (M4 optional). Skipped without Playwright / Chromium."""
from __future__ import annotations

import os
import time

import pytest

from autoapply import render


def test_markdown_to_html_escapes_title_and_renders_lists():
    pytest.importorskip("markdown")
    doc = render.markdown_to_html("# Jane\n\n- C\n- Python\n", title="<x>")
    assert "<h1>Jane</h1>" in doc and "<li>C</li>" in doc and "<title>&lt;x&gt;</title>" in doc


def test_needs_render(tmp_path):
    md = tmp_path / "resume.md"
    assert not render.needs_render(md)
    md.write_text("x", encoding="utf-8")
    assert render.needs_render(md)
    pdf = tmp_path / "resume.pdf"
    pdf.write_bytes(b"%PDF")
    os.utime(md, (time.time() - 60, time.time() - 60))
    assert not render.needs_render(md)


def test_render_folder_offline_and_no_scripts(tmp_path):
    pytest.importorskip("markdown")
    from autoapply import browser as br

    try:
        pw = br.start_playwright()
    except br.BrowserUnavailable:
        pytest.skip("playwright not installed")
    try:
        try:
            b = br.launch_chromium(pw, headless=True)
        except br.BrowserUnavailable as e:
            pytest.skip(str(e))
        (tmp_path / "resume.md").write_text(
            "# Jane\n\n<script>document.body.innerHTML='PWNED'</script>\n\n![x](https://example.invalid/x.png)\n",
            encoding="utf-8")
        (tmp_path / "cover_letter.md").write_text("Dear team,\n", encoding="utf-8")
        written = render.render_folder(tmp_path, b)
        assert sorted(p.name for p in written) == ["cover_letter.pdf", "resume.pdf"]
        assert all(p.read_bytes().startswith(b"%PDF") for p in written)
        assert render.render_folder(tmp_path, b) == []  # up to date -> skipped
        b.close()
    finally:
        pw.stop()
