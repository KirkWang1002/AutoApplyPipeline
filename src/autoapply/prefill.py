"""M6 Prefill: open a job's application form in a visible browser, fill the basics, STOP.

SAFETY CONTRACT (tested in tests/test_prefill.py):
- This module never clicks, presses keys, checks boxes, picks dropdowns or submits.
  It only types into empty text fields and attaches files to file inputs.
- It never overwrites a field that already has a value.
- The human reviews everything and clicks Submit themselves.

The field-matching core (`classify_field`, `plan_fills`) is pure and runs without a browser.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from autoapply.models import Job, Profile

# --------------------------------------------------------------------------- URLs


def application_url(job: Job) -> str:
    """URL of the page that holds the application form for `job`."""
    url = job.url.split("#")[0].rstrip("/")
    if job.source == "lever" and not url.endswith("/apply"):
        return url + "/apply"
    if job.source == "ashby" and not url.endswith("/application"):
        return url + "/application"
    return url  # Greenhouse shows the form on the posting page itself


# --------------------------------------------------------------------------- values


def split_name(full: str) -> tuple[str, str]:
    """("Jane Q. Doe") -> ("Jane", "Q. Doe"). A single word is a first name only."""
    parts = full.split()
    if not parts:
        return "", ""
    return parts[0], " ".join(parts[1:])


def _link(links: dict[str, str], *keys: str) -> str:
    lowered = {k.lower(): v for k, v in links.items()}
    for k in keys:
        if lowered.get(k):
            return lowered[k]
    return ""


def profile_values(
    profile: Profile,
    resume_file: Path | None = None,
    cover_letter_file: Path | None = None,
    cover_letter_text: str = "",
) -> dict[str, str]:
    """Map of field key -> value to type (or file path to attach). Empty values are dropped."""
    first, last = split_name(profile.name)
    edu = profile.education[0] if profile.education else None
    values = {
        "first_name": first,
        "last_name": last,
        "full_name": profile.name.strip(),
        "email": profile.email,
        "phone": profile.phone,
        "location": profile.location,
        "linkedin": _link(profile.links, "linkedin"),
        "github": _link(profile.links, "github"),
        "website": _link(profile.links, "website", "portfolio", "personal", "site"),
        "school": edu.school if edu else "",
        "degree": edu.degree if edu else "",
        "resume_file": str(resume_file) if resume_file else "",
        "cover_letter_file": str(cover_letter_file) if cover_letter_file else "",
        "cover_letter_text": cover_letter_text.strip(),
    }
    return {k: v for k, v in values.items() if v}


# --------------------------------------------------------------------------- matching


@dataclass
class FieldInfo:
    """One input/textarea found on the page (as reported by COLLECT_JS)."""

    idx: int
    tag: str = "input"  # "input" | "textarea"
    type: str = "text"
    label: str = ""
    aria: str = ""
    placeholder: str = ""
    name: str = ""
    id: str = ""
    value: str = ""
    visible: bool = True
    disabled: bool = False


@dataclass
class Fill:
    idx: int
    key: str
    value: str
    frame: int = 0
    label: str = field(default="", compare=False)

    @property
    def is_file(self) -> bool:
        return self.key.endswith("_file")


TEXT_TYPES = {"text", "email", "tel", "url", "search", ""}

# Order matters: the first pattern that matches a field's text wins.
TEXT_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("first_name", re.compile(r"\bfirst name\b|\bgiven name\b|\bfirstname\b|^first$|\bfname\b")),
    ("last_name", re.compile(r"\blast name\b|\bsurname\b|\bfamily name\b|\blastname\b|^last$|\blname\b")),
    ("email", re.compile(r"\be ?mail\b|\bemail address\b")),
    ("phone", re.compile(r"\bphone\b|\bmobile\b|\btelephone\b|^tel$|\bcell\b")),
    ("linkedin", re.compile(r"\blinked ?in\b")),
    ("github", re.compile(r"\bgit ?hub\b")),
    ("website", re.compile(r"\bwebsite\b|\bportfolio\b|\bpersonal (site|url|page)\b")),
    ("full_name", re.compile(r"^(full |legal |your )?name$|\bfull name\b|\bfull legal name\b|^systemfield name$")),
    ("location", re.compile(r"^(current )?location$|\bcurrent location\b|^city$|\bcity of residence\b|\bwhere are you (currently )?(based|located)\b")),
    ("school", re.compile(r"\bschool\b|\buniversity\b|\bcollege\b")),
    ("degree", re.compile(r"^degree$|\bdegree\b(?!.*\b(required|completed|expected)\b)")),
]
FILE_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("cover_letter_file", re.compile(r"\bcover ?letter\b")),
    ("resume_file", re.compile(r"\bresume\b|\bcv\b|\brésumé\b|\bcurriculum vitae\b")),
]
TEXTAREA_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("cover_letter_text", re.compile(r"\bcover ?letter\b|^additional information$|\badditional information\b")),
]


def normalize(text: str) -> str:
    """Lowercase words only: "urls[LinkedIn]" -> "urls linkedin", "First Name*" -> "first name"."""
    text = re.sub(r"([a-z])([A-Z])", r"\1 \2", text or "")  # firstName -> first Name
    return " ".join(re.sub(r"[^\w]+|_", " ", text.lower()).split())


def _sources(f: FieldInfo) -> list[str]:
    """Descriptive strings for a field, most trustworthy first."""
    return [s for s in (normalize(f.label), normalize(f.aria), normalize(f.placeholder),
                        normalize(f.name), normalize(f.id)) if s]


def classify_field(f: FieldInfo) -> str | None:
    """Which profile value belongs in this field, or None to leave it for the human."""
    if f.disabled:
        return None
    if f.tag == "textarea":
        patterns = TEXTAREA_PATTERNS
    elif f.type == "file":
        patterns = FILE_PATTERNS
    elif f.type in TEXT_TYPES:
        patterns = TEXT_PATTERNS
    else:
        return None  # checkbox, radio, hidden, submit, password, date, ...
    if f.type != "file" and not f.visible:
        return None  # hidden text fields are often honeypots
    for text in _sources(f):
        for key, pattern in patterns:
            if pattern.search(text):
                return key
    return None


def plan_fills(fields: list[FieldInfo], values: dict[str, str], frame: int = 0) -> list[Fill]:
    """Decide what to put where. Each value is used at most once; filled fields are skipped."""
    fills: list[Fill] = []
    used: set[str] = set()
    for f in fields:
        key = classify_field(f)
        if key is None or key in used or not values.get(key):
            continue
        if f.type != "file" and f.value.strip():
            continue  # never overwrite what is already there
        fills.append(Fill(idx=f.idx, key=key, value=values[key], frame=frame, label=f.label or f.name or f.id))
        used.add(key)
    # A form with separate first/last fields should not also get the full name elsewhere.
    if {"first_name", "last_name"} <= used:
        fills = [x for x in fills if x.key != "full_name"]
    return fills


# --------------------------------------------------------------------------- browser

ATTR = "data-autoapply-idx"

# Read-only: tags each input/textarea with an index and describes it. No events fired.
COLLECT_JS = """
(attr) => {
  const out = [];
  const text = (el) => (el ? (el.innerText || el.textContent || '') : '').trim();
  const labelOf = (el) => {
    if (el.labels && el.labels.length) return text(el.labels[0]);
    const wrap = el.closest('label');
    if (wrap) return text(wrap);
    const by = el.getAttribute('aria-labelledby');
    if (by) return by.split(/\\s+/).map((id) => text(document.getElementById(id))).join(' ');
    return '';
  };
  document.querySelectorAll('input, textarea').forEach((el, i) => {
    el.setAttribute(attr, String(i));
    const r = el.getBoundingClientRect();
    const st = getComputedStyle(el);
    out.push({
      idx: i,
      tag: el.tagName.toLowerCase(),
      type: (el.getAttribute('type') || (el.tagName === 'TEXTAREA' ? 'textarea' : 'text')).toLowerCase(),
      label: labelOf(el).slice(0, 200),
      aria: (el.getAttribute('aria-label') || '').slice(0, 200),
      placeholder: (el.getAttribute('placeholder') || '').slice(0, 200),
      name: el.getAttribute('name') || '',
      id: el.id || '',
      value: el.type === 'file' ? '' : (el.value || ''),
      visible: r.width > 0 && r.height > 0 && st.visibility !== 'hidden' && st.display !== 'none',
      disabled: !!(el.disabled || el.readOnly),
    });
  });
  return out;
}
"""

HIGHLIGHT_JS = """
([attr, idx]) => {
  const el = document.querySelector(`[${attr}="${idx}"]`);
  if (el) { el.style.outline = '2px solid #f5a623'; el.style.outlineOffset = '1px'; }
}
"""

BANNER_JS = """
(msg) => {
  const b = document.createElement('div');
  b.id = 'autoapply-banner';
  b.textContent = msg;
  // In normal flow at the top of the page, so it never covers a form field.
  b.style.cssText = 'position:relative;z-index:2147483647;padding:8px 12px;margin:0 0 8px;'
    + 'background:#f5a623;color:#111;font:600 14px/1.3 system-ui,sans-serif;text-align:center;';
  (document.body || document.documentElement).prepend(b);
}
"""

BANNER = ("autoapply prefilled the orange-outlined fields. Check every field, answer the rest, "
          "and click Submit yourself.")


def _field_infos(raw: list[dict[str, Any]]) -> list[FieldInfo]:
    known = set(FieldInfo.__dataclass_fields__)
    return [FieldInfo(**{k: v for k, v in r.items() if k in known}) for r in raw]


def prefill_page(page: Any, values: dict[str, str], banner: bool = True) -> list[Fill]:
    """Fill every frame of an already-loaded `page`. Returns what was filled.

    Uses only `Locator.fill` (text) and `Locator.set_input_files` (files). Fields that
    error (detached, read-only widget, ...) are skipped; the human fills them.
    """
    done: list[Fill] = []
    for frame_no, frame in enumerate(page.frames):
        try:
            fields = _field_infos(frame.evaluate(COLLECT_JS, ATTR))
        except Exception:
            continue  # cross-origin frame that went away, about:blank, ...
        remaining = {k: v for k, v in values.items() if k not in {d.key for d in done}}
        for fill in plan_fills(fields, remaining, frame=frame_no):
            loc = frame.locator(f'[{ATTR}="{fill.idx}"]')
            try:
                if fill.is_file:
                    loc.set_input_files(fill.value, timeout=5_000)
                else:
                    loc.fill(fill.value, timeout=5_000)
                frame.evaluate(HIGHLIGHT_JS, [ATTR, fill.idx])
            except Exception:
                continue
            done.append(fill)
    if banner:
        try:
            page.evaluate(BANNER_JS, BANNER)
        except Exception:
            pass
    return done


def wait_for_form(page: Any, timeout_ms: int = 15_000) -> None:
    """Let client-rendered forms (Ashby, embedded Greenhouse iframes) appear."""
    try:
        page.wait_for_load_state("networkidle", timeout=timeout_ms)
    except Exception:
        pass  # pages with long-polling never go idle; fill whatever is there


@dataclass
class PrefillFiles:
    resume_pdf: Path | None = None
    cover_letter_pdf: Path | None = None
    cover_letter_text: str = ""
    notes: list[str] = field(default_factory=list)


def find_files(dest: Path | None) -> PrefillFiles:
    """Tailored PDFs / cover letter text in `dest` (an out/<company>-<role>/ folder)."""
    from autoapply.tailor import COVER_LETTER_FILE

    files = PrefillFiles()
    if dest is None or not Path(dest).is_dir():
        files.notes.append("No tailored folder for this job: only contact details will be filled.")
        return files
    dest = Path(dest)
    if (p := dest / "resume.pdf").is_file():
        files.resume_pdf = p
    else:
        files.notes.append(f"No {dest / 'resume.pdf'}: attach your resume yourself.")
    if (p := dest / "cover_letter.pdf").is_file():
        files.cover_letter_pdf = p
    if (p := dest / COVER_LETTER_FILE).is_file():
        files.cover_letter_text = p.read_text(encoding="utf-8")
    return files


def run_prefill(
    job: Job,
    profile: Profile,
    dest: Path | None,
    url: str | None = None,
    headless: bool = False,
    wait: bool = True,
    echo: Any = print,
) -> list[Fill]:
    """Render PDFs if needed, open the form, fill it, then (if `wait`) block until the
    human closes the browser window. Never submits. Returns the fills made."""
    from autoapply import browser as br
    from autoapply import render

    pw = br.start_playwright()
    try:
        if dest is not None and Path(dest).is_dir():
            printer = br.launch_chromium(pw, headless=True)
            try:
                for pdf in render.render_folder(Path(dest), printer):
                    echo(f"rendered: {pdf}")
            finally:
                printer.close()
        files = find_files(dest)
        for note in files.notes:
            echo(f"note: {note}")
        values = profile_values(profile, files.resume_pdf, files.cover_letter_pdf, files.cover_letter_text)

        browser = br.launch_chromium(pw, headless=headless)
        page = browser.new_page()
        target = url or application_url(job)
        echo(f"opening: {target}")
        page.goto(target, wait_until="domcontentloaded", timeout=60_000)
        wait_for_form(page)
        filled = prefill_page(page, values)
        for f in filled:
            shown = Path(f.value).name if f.is_file else (f.value[:40] + ("..." if len(f.value) > 40 else ""))
            echo(f"  filled {f.key:<18} <- {shown}")
        missing = [k for k in ("resume_file",) if k in values and k not in {f.key for f in filled}]
        if not filled:
            echo("No fields recognized. The form may be behind an 'Apply' button: open it and fill by hand.")
        for k in missing:
            echo(f"  could not find a field for {k}: attach it yourself")
        if wait:
            echo("Review every field in the browser, answer the remaining questions, and click Submit "
                 "yourself. Close the browser window when you are done.")
            try:
                page.wait_for_event("close", timeout=0)
            except Exception:
                pass  # whole browser closed
        browser.close()
        return filled
    finally:
        pw.stop()
