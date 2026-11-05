"""End-to-end prefill against a local fixture form (headless, no human wait).

Kept apart from test_prefill.py: run_prefill starts its own Playwright, which cannot
nest inside that module's shared browser fixture.
"""
from __future__ import annotations

import pytest

from autoapply import browser as br
from autoapply import prefill as pf
from test_prefill import FORMS, PROFILE, job  # shared fake data

pytest.importorskip("playwright")
pytest.importorskip("markdown")


@pytest.fixture(autouse=True)
def _need_chromium():
    try:
        pw = br.start_playwright()
        try:
            br.launch_chromium(pw, headless=True).close()
        finally:
            pw.stop()
    except br.BrowserUnavailable as e:
        pytest.skip(str(e))


def test_run_prefill_end_to_end_headless(tmp_path):
    """Renders the tailored Markdown to PDF and attaches it, without waiting for a human."""
    dest = tmp_path / "acme-firmware-intern"
    dest.mkdir()
    (dest / "resume.md").write_text("# Jane Doe\n\n- Wrote C drivers\n", encoding="utf-8")
    (dest / "cover_letter.md").write_text("Dear Acme team,\n", encoding="utf-8")
    lines: list[str] = []
    filled = pf.run_prefill(job(), PROFILE, dest, url=(FORMS / "greenhouse.html").as_uri(),
                            headless=True, wait=False, echo=lines.append)
    assert (dest / "resume.pdf").read_bytes().startswith(b"%PDF")
    assert {"resume_file", "cover_letter_file", "email"} <= {f.key for f in filled}
    assert any(line.startswith("rendered:") for line in lines)
