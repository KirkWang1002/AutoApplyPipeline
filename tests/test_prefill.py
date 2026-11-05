"""M6 Prefill tests. Local fixture forms only: no network, no real job sites.

Browser tests run headless and are skipped when Playwright / Chromium are unavailable.
Set AUTOAPPLY_CHROMIUM to a Chromium binary to use a preinstalled browser.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest
from typer.testing import CliRunner

from autoapply import paths
from autoapply import prefill as pf
from autoapply.cli import app
from autoapply.models import Education, Job, Profile

FORMS = Path(__file__).parent / "fixtures" / "forms"

PROFILE = Profile(
    name="Jane Q. Doe",
    email="jane@example.com",
    phone="555-000-0000",
    location="Raleigh, NC",
    links={"LinkedIn": "https://www.linkedin.com/in/example", "github": "https://github.com/example",
           "portfolio": "https://example.com"},
    education=[Education(school="Example University", degree="B.S. Electrical Engineering")],
)


def job(source: str = "greenhouse", url: str = "https://boards.greenhouse.io/acme/jobs/1") -> Job:
    return Job(id=f"{source}:acme:1", source=source, company="Acme", title="Firmware Intern", url=url)


# --------------------------------------------------------------------------- pure


@pytest.mark.parametrize("source,url,expected", [
    ("greenhouse", "https://boards.greenhouse.io/acme/jobs/1", "https://boards.greenhouse.io/acme/jobs/1"),
    ("lever", "https://jobs.lever.co/acme/abc", "https://jobs.lever.co/acme/abc/apply"),
    ("lever", "https://jobs.lever.co/acme/abc/apply/", "https://jobs.lever.co/acme/abc/apply"),
    ("ashby", "https://jobs.ashbyhq.com/acme/abc#top", "https://jobs.ashbyhq.com/acme/abc/application"),
])
def test_application_url(source, url, expected):
    assert pf.application_url(job(source, url)) == expected


def test_split_name():
    assert pf.split_name("Jane Q. Doe") == ("Jane", "Q. Doe")
    assert pf.split_name("Cher") == ("Cher", "")
    assert pf.split_name("  ") == ("", "")


def test_profile_values_drops_empty_and_finds_links_case_insensitively(tmp_path):
    v = pf.profile_values(PROFILE, resume_file=tmp_path / "resume.pdf")
    assert v["first_name"] == "Jane" and v["last_name"] == "Q. Doe"
    assert v["linkedin"].endswith("/example") and v["website"] == "https://example.com"
    assert v["resume_file"].endswith("resume.pdf")
    assert "cover_letter_file" not in v and "cover_letter_text" not in v
    bare = pf.profile_values(Profile(name="Jo", email="jo@example.com"))
    assert set(bare) == {"first_name", "full_name", "email"}


def test_normalize():
    assert pf.normalize("urls[LinkedIn]") == "urls linked in"
    assert pf.normalize("First Name *") == "first name"
    assert pf.normalize("job_application[first_name]") == "job application first name"
    assert pf.normalize("firstName") == "first name"


@pytest.mark.parametrize("info,key", [
    (pf.FieldInfo(0, label="First Name *"), "first_name"),
    (pf.FieldInfo(0, name="job_application[last_name]"), "last_name"),
    (pf.FieldInfo(0, label="Full name✱", name="name"), "full_name"),
    (pf.FieldInfo(0, label="Name"), "full_name"),
    (pf.FieldInfo(0, label="Company name"), None),
    (pf.FieldInfo(0, label="Current company", name="org"), None),
    (pf.FieldInfo(0, type="email", label="Email address"), "email"),
    (pf.FieldInfo(0, type="tel", placeholder="Mobile number"), "phone"),
    (pf.FieldInfo(0, label="LinkedIn Profile"), "linkedin"),
    (pf.FieldInfo(0, name="urls[GitHub]"), "github"),
    (pf.FieldInfo(0, label="Portfolio URL"), "website"),
    (pf.FieldInfo(0, label="Where are you currently located?"), "location"),
    (pf.FieldInfo(0, label="School"), "school"),
    (pf.FieldInfo(0, type="file", label="Resume/CV"), "resume_file"),
    (pf.FieldInfo(0, type="file", id="cover_letter"), "cover_letter_file"),
    (pf.FieldInfo(0, type="file", label="Resume", visible=False), "resume_file"),  # styled uploads hide the input
    (pf.FieldInfo(0, tag="textarea", type="textarea", label="Cover Letter"), "cover_letter_text"),
    (pf.FieldInfo(0, tag="textarea", type="textarea", label="Additional information"), "cover_letter_text"),
    (pf.FieldInfo(0, tag="textarea", type="textarea", label="Why do you want to work here?"), None),
    (pf.FieldInfo(0, label="Email", visible=False), None),  # honeypot
    (pf.FieldInfo(0, label="Email", disabled=True), None),
    (pf.FieldInfo(0, type="checkbox", label="Email me about new jobs"), None),
    (pf.FieldInfo(0, type="hidden", name="email"), None),
    (pf.FieldInfo(0, type="password", label="Email password"), None),
    (pf.FieldInfo(0, type="submit", label="Submit"), None),
    (pf.FieldInfo(0, label="Why us?"), None),
])
def test_classify_field(info, key):
    assert pf.classify_field(info) == key


def test_plan_fills_skips_filled_fields_uses_each_value_once_and_drops_full_name():
    fields = [
        pf.FieldInfo(0, label="First name"),
        pf.FieldInfo(1, label="Last name"),
        pf.FieldInfo(2, label="Email", value="typed@by.human"),
        pf.FieldInfo(3, label="Email (confirm)"),
        pf.FieldInfo(4, label="LinkedIn"),
        pf.FieldInfo(5, label="LinkedIn (again)"),
        pf.FieldInfo(6, label="Full name"),
        pf.FieldInfo(7, label="Twitter"),
    ]
    fills = pf.plan_fills(fields, pf.profile_values(PROFILE))
    assert [(f.idx, f.key) for f in fills] == [(0, "first_name"), (1, "last_name"), (3, "email"), (4, "linkedin")]


def test_plan_fills_without_values_fills_nothing():
    assert pf.plan_fills([pf.FieldInfo(0, label="Phone")], {"email": "x@y.z"}) == []


def test_find_files(tmp_path):
    assert pf.find_files(None).notes
    (tmp_path / "resume.pdf").write_bytes(b"%PDF-1.4")
    (tmp_path / "cover_letter.md").write_text("Dear team,\n", encoding="utf-8")
    files = pf.find_files(tmp_path)
    assert files.resume_pdf == tmp_path / "resume.pdf"
    assert files.cover_letter_pdf is None and files.cover_letter_text == "Dear team,\n"


# --------------------------------------------------------------------------- safety

FORBIDDEN_CALLS = {"click", "dblclick", "tap", "press", "press_sequentially", "type", "check", "uncheck",
                   "set_checked", "select_option", "dispatch_event", "submit", "request_submit"}


def test_prefill_module_never_clicks_presses_or_submits():
    """Static guard for the human-in-the-loop rule: prefill may only fill and attach files."""
    src = Path(pf.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    called = {n.func.attr for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert not called & FORBIDDEN_CALLS, f"prefill.py must not call {called & FORBIDDEN_CALLS}"
    for js in (pf.COLLECT_JS, pf.HIGHLIGHT_JS, pf.BANNER_JS):
        for word in (".click(", ".submit(", "requestSubmit", "dispatchEvent", "KeyboardEvent"):
            assert word not in js


# --------------------------------------------------------------------------- browser


@pytest.fixture(scope="module")
def browser():
    from autoapply import browser as br

    try:
        pw = br.start_playwright()
    except br.BrowserUnavailable:
        pytest.skip("playwright not installed")
    try:
        b = br.launch_chromium(pw, headless=True)
    except br.BrowserUnavailable as e:
        pw.stop()
        pytest.skip(str(e))
    yield b
    b.close()
    pw.stop()


@pytest.fixture
def resume_pdf(tmp_path):
    p = tmp_path / "resume.pdf"
    p.write_bytes(b"%PDF-1.4\n% fake\n")
    return p


def _open(browser, name):
    page = browser.new_page()
    page.goto((FORMS / name).as_uri())
    pf.wait_for_form(page, timeout_ms=5_000)
    return page


def _files(page, selector):
    return page.eval_on_selector(selector, "el => Array.from(el.files).map(f => f.name)")


def test_greenhouse_form(browser, resume_pdf, tmp_path):
    cover = tmp_path / "cover_letter.pdf"
    cover.write_bytes(b"%PDF-1.4\n")
    page = _open(browser, "greenhouse.html")
    url_before = page.url
    filled = pf.prefill_page(page, pf.profile_values(PROFILE, resume_pdf, cover, "Dear team"))

    assert {f.key for f in filled} == {"first_name", "last_name", "email", "resume_file",
                                       "cover_letter_file", "linkedin", "school"}
    assert page.input_value("#first_name") == "Jane"
    assert page.input_value("#last_name") == "Q. Doe"
    assert page.input_value("#email") == "jane@example.com"
    assert page.input_value("#phone") == "already typed"  # never overwritten
    assert page.input_value("#li") == "https://www.linkedin.com/in/example"
    assert page.input_value("#hp") == ""  # hidden honeypot untouched
    assert _files(page, "#resume") == ["resume.pdf"]
    assert _files(page, "#cover_letter") == ["cover_letter.pdf"]
    assert page.input_value("#auth") == "" and not page.is_checked("#consent")
    assert page.evaluate("window.__submitted") is False and page.url == url_before
    assert page.locator("#autoapply-banner").count() == 1
    page.close()


def test_lever_form(browser, resume_pdf):
    page = _open(browser, "lever.html")
    filled = pf.prefill_page(page, pf.profile_values(PROFILE, resume_pdf, None, "Dear Acme team,\n\nHello."))

    assert {f.key for f in filled} == {"resume_file", "full_name", "email", "phone", "linkedin",
                                       "github", "website", "cover_letter_text"}
    assert page.input_value("[name=name]") == "Jane Q. Doe"
    assert page.input_value("[name=org]") == ""
    assert page.input_value("[name='urls[GitHub]']") == "https://github.com/example"
    assert page.input_value("[name=comments]").startswith("Dear Acme team")
    assert page.evaluate("window.__submitted") is False
    page.close()


def test_client_rendered_form_and_input_events(browser, resume_pdf):
    page = _open(browser, "ashby.html")
    page.wait_for_selector("form")
    filled = pf.prefill_page(page, pf.profile_values(PROFILE, resume_pdf))

    assert {f.key for f in filled} == {"full_name", "email", "resume_file", "location"}
    assert page.input_value("#q1") == "Raleigh, NC"
    assert page.input_value("#q2") == ""  # free-text question left for the human
    assert page.evaluate("window.__inputEvents") >= 3  # React-style apps see real input events
    assert page.evaluate("window.__submitted") is False
    page.close()


# --------------------------------------------------------------------------- CLI


def _write_jobs(tmp_path, monkeypatch, jobs):
    monkeypatch.setattr(paths, "JOBS_FILE", tmp_path / "jobs.jsonl")
    monkeypatch.setattr(paths, "OUT_DIR", tmp_path / "out")
    monkeypatch.setattr(paths, "SCORES_FILE", tmp_path / "scores.jsonl")
    monkeypatch.setattr(paths, "APPLICATIONS_FILE", tmp_path / "applications.csv")
    (tmp_path / "jobs.jsonl").write_text("".join(j.model_dump_json() + "\n" for j in jobs), encoding="utf-8")


def test_cli_prefill_unknown_job(tmp_path, monkeypatch):
    _write_jobs(tmp_path, monkeypatch, [])
    result = CliRunner().invoke(app, ["prefill", "greenhouse:nope:1"])
    assert result.exit_code == 1 and "not found" in result.output


@pytest.mark.parametrize("answer,status", [("y\n", "applied"), ("n\n", "found")])
def test_cli_prefill_marks_applied_only_when_confirmed(tmp_path, monkeypatch, answer, status):
    from autoapply import track

    j = job()
    _write_jobs(tmp_path, monkeypatch, [j])
    calls = []
    monkeypatch.setattr(pf, "run_prefill", lambda *a, **kw: calls.append((a, kw)) or [])
    result = CliRunner().invoke(app, ["prefill", j.id], input=answer)
    assert result.exit_code == 0, result.output
    assert calls and calls[0][0][2] is None  # not tailored -> no folder passed
    assert "not tailored yet" in result.output
    records = track.load_applications(tmp_path / "applications.csv")
    if status == "applied":
        assert records[j.id].status.value == "applied"
    else:
        assert j.id not in records or records[j.id].status.value != "applied"
