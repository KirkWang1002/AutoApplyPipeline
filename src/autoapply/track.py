"""M5 Track + report: keep data/applications.csv in sync and print a status report.

Core functions (`sync`, `set_status`, `build_report`) are pure and take plain data;
file I/O lives in `load_*` / `save_applications` / `sync_from_files`.
"""
from __future__ import annotations

import csv
import os
import re
import sys
import tempfile
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from typing import Iterable

from pydantic import ValidationError

from autoapply.models import ApplicationRecord, ApplicationStatus, Job, ScoreResult
from autoapply import paths, storage

FIELDS = list(ApplicationRecord.model_fields)
TOP_N = 15

# Progression order. Anything at or beyond `drafted` is never touched by `sync`.
_RANK = {
    ApplicationStatus.found: 0,
    ApplicationStatus.scored: 1,
    ApplicationStatus.drafted: 2,
    ApplicationStatus.applied: 3,
    ApplicationStatus.interview: 4,
    ApplicationStatus.rejected: 4,
    ApplicationStatus.offer: 5,
}
NOT_APPLIED = {ApplicationStatus.found, ApplicationStatus.scored, ApplicationStatus.drafted}
PENDING = {ApplicationStatus.drafted, ApplicationStatus.applied}


# ---------------------------------------------------------------- file I/O


# The owner edits applications.csv in Excel, which adds a UTF-8 BOM, rewrites dates as
# 11/5/2026, and lets a human type "Applied ". Reading tolerates all of that.
_DATE_FORMATS = ("%m/%d/%Y", "%m/%d/%y")

# CSV formula injection: titles/notes come from untrusted postings, and Excel runs a
# cell starting with = + - @ (or tab/CR) as a formula. Such cells are written with a
# leading ' and read back without it. A value that already starts with quote(s) and
# then one of those characters gets one more quote, so the round-trip is lossless.
_FORMULA_LIKE = re.compile(r"^'*[=+\-@\t\r]")


class ApplicationsFileLocked(RuntimeError):
    """applications.csv could not be written (on Windows: usually open in Excel)."""


def _escape_cell(value: str) -> str:
    return "'" + value if _FORMULA_LIKE.match(value) else value


def _unescape_cell(value: str) -> str:
    return value[1:] if value.startswith("'") and _FORMULA_LIKE.match(value) else value


def _parse_date(value: str) -> date | str:
    try:
        return date.fromisoformat(value)
    except ValueError:
        pass
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            pass
    return value  # let pydantic reject it with a readable message


def _parse_row(data: dict[str, str]) -> ApplicationRecord:
    """Validate one (already unescaped) CSV row. Raises ValueError on bad data."""
    data = dict(data)
    for key in ("job_id", "status", "score", "updated"):
        data[key] = data.get(key, "").strip()
    if not data["job_id"]:
        raise ValueError("job_id is empty")
    data["status"] = data["status"].lower()
    if not data["status"]:
        data.pop("status")  # model default: found
    data["score"] = data["score"] or None
    if data["updated"]:
        data["updated"] = _parse_date(data["updated"])
    else:
        data.pop("updated")  # model default: today
    return ApplicationRecord.model_validate(data)


def _describe(err: ValueError) -> str:
    if isinstance(err, ValidationError):
        return "; ".join(
            f"{'.'.join(map(str, e['loc']))}: {e['msg']} (got {e.get('input')!r})"
            for e in err.errors()
        )
    return str(err)


def _read_rows(path: Path, warn: bool) -> tuple[dict[str, ApplicationRecord], list[dict[str, str]]]:
    """Return (valid records by job_id, raw rows that failed validation)."""
    records: dict[str, ApplicationRecord] = {}
    bad: list[dict[str, str]] = []
    if not path.exists():
        return records, bad
    with path.open(newline="", encoding="utf-8-sig") as f:
        # Row numbers match what Excel shows (header is row 1).
        for rownum, row in enumerate(csv.DictReader(f), 2):
            data = {
                k.strip(): _unescape_cell(v or "")
                for k, v in row.items()
                if isinstance(k, str) and k.strip() in FIELDS
            }
            if not any(v.strip() for v in data.values()):
                continue  # blank line Excel left behind
            try:
                rec = _parse_row(data)
            except ValueError as e:  # includes pydantic.ValidationError
                bad.append(data)
                if warn:
                    print(
                        f"warning: skipping invalid row {rownum} in {path}: {_describe(e)}",
                        file=sys.stderr,
                    )
                continue
            records[rec.job_id] = rec
    return records, bad


def load_applications(path: Path | None = None) -> dict[str, ApplicationRecord]:
    """Read applications.csv into {job_id: record}. Missing file -> empty dict.

    Rows that cannot be parsed are skipped with a warning on stderr. `save_applications`
    keeps them in the file verbatim, so a hand-typed typo is never silently lost.
    """
    path = Path(path) if path is not None else paths.APPLICATIONS_FILE
    return _read_rows(path, warn=True)[0]


def _to_row(rec: ApplicationRecord) -> dict[str, str]:
    return {
        "job_id": rec.job_id,
        "company": rec.company,
        "title": rec.title,
        "url": rec.url,
        "status": rec.status.value,
        "score": "" if rec.score is None else str(rec.score),
        "updated": rec.updated.isoformat(),
        "notes": rec.notes,
    }


def save_applications(records: dict[str, ApplicationRecord], path: Path | None = None) -> None:
    """Write records to CSV atomically (temp file in the same dir + os.replace).

    Rows of the existing file that fail validation are appended unchanged so the human
    can fix them. Raises ApplicationsFileLocked if the file cannot be written (on
    Windows, os.replace fails while Excel has it open).
    """
    path = Path(path) if path is not None else paths.APPLICATIONS_FILE
    try:
        bad_rows = _read_rows(path, warn=False)[1]
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=FIELDS)
                writer.writeheader()
                for row in [_to_row(rec) for rec in records.values()] + bad_rows:
                    writer.writerow({k: _escape_cell(row.get(k, "")) for k in FIELDS})
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
    except PermissionError as e:
        raise ApplicationsFileLocked(
            f"Could not write {path} (permission denied). "
            f"If it is open in Excel, close {path.name} in Excel and retry."
        ) from e


def load_jobs(path: Path | None = None) -> list[Job]:
    return storage.load_jobs(path)


def load_scores(path: Path | None = None) -> dict[str, ScoreResult]:
    """Scores keyed by job id; if a job id appears more than once, the last line wins."""
    return storage.load_scores(path)


# ---------------------------------------------------------------- core logic


def sync(
    jobs: Iterable[Job],
    scores: dict[str, ScoreResult],
    records: dict[str, ApplicationRecord],
    today: date | None = None,
) -> dict[str, ApplicationRecord]:
    """Merge discovered jobs and scores into records (returns a new dict).

    New jobs get a `found` record. A known score is copied onto the record and bumps
    `found` -> `scored`. Statuses further along are never downgraded.
    """
    today = today or date.today()
    out = {k: v.model_copy() for k, v in records.items()}
    for job in jobs:
        if job.id not in out:
            out[job.id] = ApplicationRecord(
                job_id=job.id, company=job.company, title=job.title, url=job.url, updated=today
            )
    for job_id, s in scores.items():
        rec = out.get(job_id)
        if rec is None:
            continue
        changed = rec.score != s.score
        rec.score = s.score
        if rec.status == ApplicationStatus.found:
            rec.status = ApplicationStatus.scored
            changed = True
        if changed:
            rec.updated = today
    return out


def set_status(
    records: dict[str, ApplicationRecord],
    job_id: str,
    status: ApplicationStatus | str,
    notes: str | None = None,
    today: date | None = None,
) -> ApplicationRecord:
    """Set a record's status (in place) and stamp `updated`. Unknown id -> KeyError."""
    if job_id not in records:
        hint = ""
        close = [k for k in records if job_id.lower() in k.lower()]
        if close:
            hint = f" Did you mean: {', '.join(sorted(close)[:5])}?"
        raise KeyError(
            f"Unknown job id {job_id!r}.{hint} Run `autoapply report` to see tracked ids."
        )
    rec = records[job_id]
    rec.status = ApplicationStatus(status)
    if notes is not None:
        rec.notes = notes
    rec.updated = today or date.today()
    return rec


def mark_drafted(
    records: dict[str, ApplicationRecord],
    job_ids: Iterable[str],
    today: date | None = None,
) -> list[str]:
    """Set `drafted` (in place) on records still `found`/`scored`; never downgrades.

    Unknown ids are ignored. Returns the ids that changed.
    """
    today = today or date.today()
    changed = []
    for job_id in job_ids:
        rec = records.get(job_id)
        if rec is not None and rec.status in (ApplicationStatus.found, ApplicationStatus.scored):
            rec.status = ApplicationStatus.drafted
            rec.updated = today
            changed.append(job_id)
    return changed


def _table(headers: list[str], rows: list[list[str]]) -> str:
    widths = [len(h) for h in headers]
    for row in rows:
        widths = [max(w, len(c)) for w, c in zip(widths, row)]
    fmt = lambda cells: "  ".join(c.ljust(w) for c, w in zip(cells, widths)).rstrip()  # noqa: E731
    lines = [fmt(headers), fmt(["-" * w for w in widths])]
    lines += [fmt(r) for r in rows]
    return "\n".join(lines)


def _clip(s: str, n: int = 40) -> str:
    return s if len(s) <= n else s[: n - 1] + "~"


def build_report(records: dict[str, ApplicationRecord], today: date, top_n: int = TOP_N) -> str:
    recs = list(records.values())
    out: list[str] = [f"Application report - {today.isoformat()}", ""]

    counts = Counter(r.status for r in recs)
    summary = ", ".join(f"{s.value}={counts[s]}" for s in ApplicationStatus if counts[s])
    out.append(f"Total: {len(recs)}" + (f" ({summary})" if summary else ""))
    out.append("")

    top = [r for r in recs if r.status in NOT_APPLIED]
    top.sort(key=lambda r: (r.score is None, -(r.score or 0), r.company.lower(), r.title.lower()))
    top = top[:top_n]
    out.append(f"Top scored jobs not yet applied (top {top_n})")
    if top:
        rows = [
            ["-" if r.score is None else str(r.score), r.status.value, _clip(r.company, 20),
             _clip(r.title), r.job_id]
            for r in top
        ]
        out.append(_table(["Score", "Status", "Company", "Title", "Job id"], rows))
    else:
        out.append("(none)")
    out.append("")

    pending = [r for r in recs if r.status in PENDING]
    pending.sort(key=lambda r: (_RANK[r.status], r.updated, r.job_id))
    out.append("Pending (drafted, or applied awaiting response)")
    if pending:
        rows = [
            [r.status.value, str((today - r.updated).days), _clip(r.company, 20),
             _clip(r.title), r.job_id]
            for r in pending
        ]
        out.append(_table(["Status", "Days", "Company", "Title", "Job id"], rows))
    else:
        out.append("(none)")
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------- glue for the CLI


def sync_from_files(
    today: date | None = None,
    drafted: Iterable[str] = (),
    save: bool = True,
) -> dict[str, ApplicationRecord]:
    """Load jobs/scores/applications from the default paths, sync, mark the `drafted`
    ids (see `mark_drafted`), and return the records.

    The CSV is only rewritten when something actually changed, so `report` leaves a
    file the human has open in Excel alone. save=False: the caller saves later.
    """
    before = load_applications()
    records = sync(load_jobs(), load_scores(), before, today=today)
    mark_drafted(records, drafted, today=today)
    if save and records != before:
        save_applications(records)
    return records
