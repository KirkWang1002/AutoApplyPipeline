"""One tolerant JSONL reader shared by every stage (jobs.jsonl, scores.jsonl)."""
from __future__ import annotations

import sys
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from autoapply import paths
from autoapply.models import Job, ScoreResult

M = TypeVar("M", bound=BaseModel)


def read_jsonl(path: Path, model: type[M]) -> list[M]:
    """Parse one model per line. Missing file -> []. Bad lines (e.g. a truncated last
    line after Ctrl+C) are skipped with a warning instead of crashing the run."""
    path = Path(path)
    if not path.exists():
        return []
    items: list[M] = []
    with path.open(encoding="utf-8-sig") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                items.append(model.model_validate_json(line))
            except ValidationError:
                print(f"warning: skipping invalid line {lineno} in {path}", file=sys.stderr)
    return items


def append_jsonl(items: list[BaseModel], path: Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        for item in items:
            f.write(item.model_dump_json() + "\n")


def load_jobs(path: Path | None = None) -> list[Job]:
    return read_jsonl(path if path is not None else paths.JOBS_FILE, Job)


def load_scores(path: Path | None = None) -> dict[str, ScoreResult]:
    """Scores keyed by job id; if a job id appears more than once, the last line wins."""
    results = read_jsonl(path if path is not None else paths.SCORES_FILE, ScoreResult)
    return {s.job_id: s for s in results}
