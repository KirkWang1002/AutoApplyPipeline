"""M5 Track + report: keep data/applications.csv in sync and print a status report.

Core functions (`sync`, `set_status`, `build_report`) are pure and take plain data;
file I/O lives in `load_*` / `save_applications` / `sync_from_files`.
"""
from __future__ import annotations

import csv
import os
import tempfile
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Iterable


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


def load_applications(path: Path | None = None) -> dict[str, ApplicationRecord]:
    """Read applications.csv into {job_id: record}. Missing file -> empty dict."""
    path = Path(path) if path is not None else paths.APPLICATIONS_FILE
    if not path.exists():
        return {}
    records: dict[str, ApplicationRecord] = {}
    with path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            data = {k: v for k, v in row.items() if k in FIELDS}
            if data.get("score") in ("", None):
                data["score"] = None
            if not data.get("updated"):
                data.pop("updated", None)
            data["notes"] = data.get("notes") or ""
            rec = ApplicationRecord.model_validate(data)
            records[rec.job_id] = rec
    return records


def save_applications(records: dict[str, ApplicationRecord], path: Path | None = None) -> None:
    """Write records to CSV atomically (temp file in the same dir + os.replace)."""
    path = Path(path) if path is not None else paths.APPLICATIONS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=FIELDS)
            writer.writeheader()
            for rec in records.values():
                writer.writerow(
                    {
                        "job_id": rec.job_id,
                        "company": rec.company,
                        "title": rec.title,
                        "url": rec.url,
                        "status": rec.status.value,
                        "score": "" if rec.score is None else rec.score,
                        "updated": rec.updated.isoformat(),
                        "notes": rec.notes,
                    }
                )
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


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


def sync_from_files(today: date | None = None) -> dict[str, ApplicationRecord]:
    """Load jobs/scores/applications from the default paths, sync, save, return records."""
    records = sync(load_jobs(), load_scores(), load_applications(), today=today)
    save_applications(records)
    return records
