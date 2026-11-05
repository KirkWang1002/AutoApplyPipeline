"""Stage 1 - Discover: fetch postings from public ATS job-board APIs, filter, dedupe, save.

Only public, unauthenticated posting endpoints are used:

- Greenhouse: https://boards-api.greenhouse.io/v1/boards/{board}/jobs?content=true
- Lever:      https://api.lever.co/v0/postings/{board}?mode=json
- Ashby:      https://api.ashbyhq.com/posting-api/job-board/{board}?includeCompensation=false

The parsers and filters are pure functions (no network) so tests can call them directly
with recorded JSON fixtures. Fetchers take an ``httpx.Client`` so tests can inject an
``httpx.MockTransport``.
"""
from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta, timezone
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import httpx

from autoapply.models import Companies, Job, Profile, Target, board_display_name
from autoapply import paths, storage

log = logging.getLogger(__name__)

GREENHOUSE_URL = "https://boards-api.greenhouse.io/v1/boards/{board}/jobs?content=true"
LEVER_URL = "https://api.lever.co/v0/postings/{board}?mode=json"
ASHBY_URL = "https://api.ashbyhq.com/posting-api/job-board/{board}?includeCompensation=false"

USER_AGENT = "autoapply/0.1 (personal job search; human-in-the-loop)"


# --------------------------------------------------------------------------- helpers


class _TextExtractor(HTMLParser):
    _BLOCK = {"p", "div", "br", "li", "ul", "ol", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section"}
    _SKIP = {"script", "style"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._SKIP:
            self._skip_depth += 1
        elif tag == "li":
            self.parts.append("\n- ")
        elif tag in self._BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag in self._BLOCK and tag != "li":  # the next "<li>" adds its own newline
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    """Convert an HTML fragment (possibly entity-escaped, as Greenhouse sends it) to plain text."""
    if not html:
        return ""
    # Greenhouse double-encodes: the content field is escaped HTML ("&lt;p&gt;...").
    if "<" not in html and "&lt;" in html:
        html = unescape(html)
    parser = _TextExtractor()
    parser.feed(html)
    parser.close()
    text = "".join(parser.parts).replace("\xa0", " ")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    out: list[str] = []
    for line in lines:
        if line or (out and out[-1]):
            out.append(line)
    return "\n".join(out).strip()


def _parse_iso(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _parse_epoch_ms(value: Any) -> datetime | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    try:
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _looks_remote(location: str) -> bool:
    return bool(re.search(r"\bremote\b", location or "", re.IGNORECASE))


def _location_strings(value: Any) -> list[str]:
    """Pull location names out of a string, a ``{"location": ...}``/``{"name": ...}`` object, or a list of those."""
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, dict):
        return _location_strings(value.get("location") or value.get("name") or value.get("locationName"))
    if isinstance(value, (list, tuple)):
        return [s for v in value for s in _location_strings(v)]
    return []


def _join_locations(*values: Any) -> str:
    """Join every location found in ``values`` with "; ", dropping case-insensitive duplicates."""
    seen: set[str] = set()
    out: list[str] = []
    for loc in _location_strings(list(values)):
        if loc.lower() not in seen:
            seen.add(loc.lower())
            out.append(loc)
    return "; ".join(out)


def _lever_description(item: dict) -> str:
    """Lever splits a posting into intro, ``lists`` (headed bullet sections) and closing text."""
    parts: list[str] = []
    intro = item.get("descriptionPlain") or html_to_text(item.get("description") or "")
    if intro.strip():
        parts.append(intro.strip())
    for section in item.get("lists") or []:
        if not isinstance(section, dict):
            continue
        heading = (section.get("text") or "").strip()
        body = html_to_text(section.get("content") or "")
        block = "\n".join(p for p in (heading, body) if p)
        if block:
            parts.append(block)
    closing = item.get("additionalPlain") or html_to_text(item.get("additional") or "")
    if closing.strip():
        parts.append(closing.strip())
    return "\n\n".join(parts)


# --------------------------------------------------------------------------- parsers (pure)


def parse_greenhouse(payload: dict, board: str, company: str | None = None) -> list[Job]:
    """Parse a Greenhouse ``/v1/boards/{board}/jobs?content=true`` response.

    ``company`` is the display name for ``Job.company``; it defaults to one derived from ``board``.
    """
    company = company or board_display_name(board)
    jobs: list[Job] = []
    for item in (payload or {}).get("jobs") or []:
        if not item.get("id") or not item.get("title") or not item.get("absolute_url"):
            continue
        location = ((item.get("location") or {}).get("name") or "").strip()
        posted = _parse_iso(item.get("first_published")) or _parse_iso(item.get("updated_at"))
        jobs.append(
            Job(
                id=f"greenhouse:{board}:{item['id']}",
                source="greenhouse",
                company=company,
                title=item["title"].strip(),
                url=item["absolute_url"],
                location=location,
                remote=_looks_remote(location),
                posted_at=posted,
                description=html_to_text(item.get("content") or ""),
            )
        )
    return jobs


def parse_lever(payload: list, board: str, company: str | None = None) -> list[Job]:
    """Parse a Lever ``/v0/postings/{board}?mode=json`` response (a JSON list).

    The description joins ``descriptionPlain``, each of ``lists`` (heading + bullets) and
    ``additionalPlain``; the location joins ``categories.location`` and ``categories.allLocations``.
    """
    company = company or board_display_name(board)
    jobs: list[Job] = []
    for item in payload or []:
        if not item.get("id") or not item.get("text") or not item.get("hostedUrl"):
            continue
        categories = item.get("categories") or {}
        location = _join_locations(categories.get("location"), categories.get("allLocations"))
        workplace = (item.get("workplaceType") or "").lower()
        description = _lever_description(item)
        jobs.append(
            Job(
                id=f"lever:{board}:{item['id']}",
                source="lever",
                company=company,
                title=item["text"].strip(),
                url=item["hostedUrl"],
                location=location,
                remote=workplace == "remote" or _looks_remote(location),
                posted_at=_parse_epoch_ms(item.get("createdAt")),
                description=description.strip(),
            )
        )
    return jobs


def parse_ashby(payload: dict, board: str, company: str | None = None) -> list[Job]:
    """Parse an Ashby ``/posting-api/job-board/{board}`` response. Unlisted postings are skipped.

    The location joins ``location`` and ``secondaryLocations`` (objects or plain strings).
    """
    company = company or board_display_name(board)
    jobs: list[Job] = []
    for item in (payload or {}).get("jobs") or []:
        if not item.get("id") or not item.get("title") or not item.get("jobUrl"):
            continue
        if item.get("isListed") is False:
            continue
        location = _join_locations(item.get("location"), item.get("secondaryLocations"))
        description = item.get("descriptionPlain") or html_to_text(item.get("descriptionHtml") or "")
        jobs.append(
            Job(
                id=f"ashby:{board}:{item['id']}",
                source="ashby",
                company=company,
                title=item["title"].strip(),
                url=item["jobUrl"],
                location=location,
                remote=bool(item.get("isRemote")) or _looks_remote(location),
                posted_at=_parse_iso(item.get("publishedAt")),
                description=description.strip(),
            )
        )
    return jobs


# --------------------------------------------------------------------------- fetchers


def make_client() -> httpx.Client:
    """The HTTP client the CLI uses. Tests monkeypatch this to inject a MockTransport."""
    return httpx.Client(timeout=20.0, follow_redirects=True, headers={"User-Agent": USER_AGENT})


def _fetch(
    client: httpx.Client,
    source: str,
    board: str,
    url: str,
    parse: Callable[[Any, str, str | None], list[Job]],
    company: str | None = None,
) -> list[Job]:
    """GET ``url`` and parse it. Any HTTP/network/JSON error logs a warning and returns []."""
    try:
        resp = client.get(url)
        resp.raise_for_status()
        return parse(resp.json(), board, company)
    except (httpx.HTTPError, ValueError, AttributeError, TypeError) as exc:
        log.warning("skipping %s board %r: %s", source, board, exc)
        return []


def fetch_greenhouse(client: httpx.Client, board: str, company: str | None = None) -> list[Job]:
    return _fetch(client, "greenhouse", board, GREENHOUSE_URL.format(board=board), parse_greenhouse, company)


def fetch_lever(client: httpx.Client, board: str, company: str | None = None) -> list[Job]:
    return _fetch(client, "lever", board, LEVER_URL.format(board=board), parse_lever, company)


def fetch_ashby(client: httpx.Client, board: str, company: str | None = None) -> list[Job]:
    return _fetch(client, "ashby", board, ASHBY_URL.format(board=board), parse_ashby, company)


FETCHERS: dict[str, Callable[[httpx.Client, str, str | None], list[Job]]] = {
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "ashby": fetch_ashby,
}


def fetch_all(companies: Companies, client: httpx.Client) -> list[Job]:
    jobs: list[Job] = []
    for source, fetch in FETCHERS.items():
        for company in getattr(companies, source):
            jobs.extend(fetch(client, company.board, company.display_name))
    return jobs


# --------------------------------------------------------------------------- filters (pure)

_STOPWORDS = {"a", "an", "and", "the", "of", "for", "in", "to", "at", "on", "or", "with"}
# Suffixes a job-title word may add to a target word and still count as the same word,
# e.g. "Intern" -> "Interns"/"Internship", "Engineer" -> "Engineering"/"Engineers".
_OK_SUFFIXES = ("", "s", "es", "ing", "ings", "ship", "ships", "er", "ers")


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9+#]+", text.lower())


def _stem(word: str) -> str:
    for suffix in ("ings", "ing", "s"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            return word[: -len(suffix)]
    return word


def title_matches(title: str, target_title: str) -> bool:
    """True if every significant word of ``target_title`` appears in ``title`` (any order).

    Words are compared case-insensitively after a light stem, and a job-title word may add a
    common suffix, so "Hardware Engineer Intern" matches "Intern, Hardware Engineering" and
    "Hardware Engineering Internship", but "Intern" does not match "Internal". Stopwords
    ("of", "and", ...) are ignored. A target with no significant words matches nothing.
    """
    wanted = [_stem(w) for w in _words(target_title) if w not in _STOPWORDS]
    if not wanted:
        return False
    have = _words(title)
    return all(any(h == s + suf for h in have for suf in _OK_SUFFIXES) for s in wanted)


def _contains_word(text: str, word: str) -> bool:
    return bool(re.search(rf"(?<!\w){re.escape(word.strip())}(?!\w)", text or "", re.IGNORECASE))


US_STATES: dict[str, str] = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California",
    "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware", "FL": "Florida", "GA": "Georgia",
    "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa",
    "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
    "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi", "MO": "Missouri",
    "MT": "Montana", "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey",
    "NM": "New Mexico", "NY": "New York", "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio",
    "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina",
    "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont",
    "VA": "Virginia", "WA": "Washington", "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
    "DC": "District of Columbia",
}
_STATE_CODES: dict[str, str] = {name.lower(): code for code, name in US_STATES.items()}


def _location_hit(text: str, loc: str) -> bool:
    """Whole-word match of ``loc`` in ``text``, also trying the US state alias of ``loc``.

    A 2-letter state code also matches the full state name ("CA" -> "California", any case).
    A full state name also matches its code, but only written in upper case ("Oregon" -> "OR"),
    so a target like "Oregon" does not match the word "or" in "Remote or Hybrid".
    """
    loc = loc.strip()
    if not loc:
        return False
    if _contains_word(text, loc):
        return True
    name = US_STATES.get(loc.upper()) if len(loc) == 2 else None
    if name and _contains_word(text, name):
        return True
    code = _STATE_CODES.get(re.sub(r"\s+", " ", loc.lower()))
    return bool(code and re.search(rf"(?<!\w){code}(?!\w)", text or ""))


def location_matches(job: Job, locations: list[str]) -> bool:
    """True if the job's location contains any target location as a whole word (case-insensitive).

    US states match by code or full name in either direction (see :func:`_location_hit`).
    "Remote" in ``locations`` also accepts any job flagged ``remote``. Jobs with no location
    text are kept (unknown, let the human decide). An empty ``locations`` list accepts all.
    """
    if not locations or not job.location.strip():
        return True
    for loc in locations:
        if loc.strip().lower() == "remote" and job.remote:
            return True
        if _location_hit(job.location, loc):
            return True
    return False


def filter_jobs(jobs: Iterable[Job], target: Target, now: datetime) -> list[Job]:
    """Keep jobs that fit ``target``.

    - Title: matches at least one of ``target.titles`` via :func:`title_matches` (each target
      title is treated as a bag of significant words that must all appear in the job title).
      An empty ``titles`` list accepts every title.
    - Exclusions: drop titles containing any ``exclude_title_words`` (whole word, case-insensitive).
    - Location: see :func:`location_matches`.
    - Recency: ``posted_at`` within ``posted_within_days`` of ``now``; unknown dates are kept.
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    cutoff = now - timedelta(days=target.posted_within_days)
    kept: list[Job] = []
    for job in jobs:
        if target.titles and not any(title_matches(job.title, t) for t in target.titles):
            continue
        if any(_contains_word(job.title, w) for w in target.exclude_title_words if w.strip()):
            continue
        if not location_matches(job, target.locations):
            continue
        if job.posted_at is not None:
            posted = job.posted_at if job.posted_at.tzinfo else job.posted_at.replace(tzinfo=timezone.utc)
            if posted < cutoff:
                continue
        kept.append(job)
    return kept


def _norm_url(url: str) -> str:
    return url.strip().rstrip("/").lower()


def dedupe(jobs: Iterable[Job]) -> list[Job]:
    """Drop jobs whose URL (trimmed, case-insensitive, no trailing slash) was already seen."""
    seen: set[str] = set()
    out: list[Job] = []
    for job in jobs:
        key = _norm_url(job.url)
        if key in seen:
            continue
        seen.add(key)
        out.append(job)
    return out


# --------------------------------------------------------------------------- storage


def load_jobs(path: Path | None = None) -> list[Job]:
    """Read jobs from a JSONL file; a missing file means no jobs, bad lines are skipped."""
    return storage.load_jobs(path)


def save_jobs(jobs: Iterable[Job], path: Path | None = None) -> list[Job]:
    """Append jobs whose id is not already in ``path``. Returns the jobs actually written."""
    path = Path(path) if path is not None else paths.JOBS_FILE
    known = {j.id for j in load_jobs(path)}
    new: list[Job] = []
    for job in jobs:
        if job.id not in known:
            known.add(job.id)
            new.append(job)
    if new:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            for job in new:
                fh.write(job.model_dump_json() + "\n")
    return new


# --------------------------------------------------------------------------- orchestrator


def discover(
    companies: Companies,
    profile: Profile,
    client: httpx.Client,
    now: datetime,
    *,
    known: Iterable[Job] = (),
    apply_filters: bool = True,
) -> list[Job]:
    """Fetch every configured board, dedupe, filter, and return jobs not already in ``known``.

    Does not write anything; the caller persists the result with :func:`save_jobs`.
    """
    jobs = dedupe(fetch_all(companies, client))
    if apply_filters:
        jobs = filter_jobs(jobs, profile.target, now)
    known = list(known)
    known_ids = {j.id for j in known}
    known_urls = {_norm_url(j.url) for j in known}
    return [j for j in jobs if j.id not in known_ids and _norm_url(j.url) not in known_urls]
