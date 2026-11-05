"""M3 Score: ask Claude to rate how well a job fits the candidate, with a JSONL cache."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Iterable

from pydantic import ValidationError

from autoapply import llm
from autoapply.models import Job, Profile, ScoreResult
from autoapply import paths, storage

MAX_DESCRIPTION_CHARS = 8000

SYSTEM_PROMPT = (
    "You are a careful technical recruiter screening job postings for one specific candidate. "
    "Judge fit honestly: do not inflate scores. You respond with ONLY a JSON object and no other text."
)

_INSTRUCTIONS = """Rate how well this job fits the candidate on a 1-5 scale:
5 = excellent fit, apply now; 4 = good fit; 3 = possible stretch; 2 = weak fit; 1 = not a fit.

Consider: target titles, skills overlap, education and graduation date (e.g. new-grad/intern
eligibility), location and remote policy, work authorization / sponsorship requirements, and
seniority (years of experience required vs. the candidate's level).

Respond with ONLY a JSON object, no markdown, no commentary, exactly in this shape:
{"score": <int 1-5>, "matched_skills": [<candidate skills the job asks for>], "gaps": [<requirements the candidate lacks>], "reason": "<one line>"}"""


def _truncate(text: str, limit: int = MAX_DESCRIPTION_CHARS) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[:limit] + "\n[... description truncated ...]"


def _profile_block(profile: Profile) -> str:
    edu = "; ".join(
        f"{e.degree}, {e.school}" + (f" (grad {e.grad})" if e.grad else "") for e in profile.education
    )
    t = profile.target
    lines = [
        f"Location: {profile.location or '-'}",
        f"Work authorization: {profile.work_authorization or '-'}",
        f"Education: {edu or '-'}",
        f"Target titles: {', '.join(t.titles) or '-'}",
        f"Preferred locations: {', '.join(t.locations) or '-'}",
        f"Avoid titles containing: {', '.join(t.exclude_title_words) or '-'}",
        f"Skills: {', '.join(profile.skills) or '-'}",
    ]
    return "\n".join(lines)


def _job_block(job: Job) -> str:
    lines = [
        f"Company: {job.company}",
        f"Title: {job.title}",
        f"Location: {job.location or '-'}" + (" (remote)" if job.remote else ""),
        f"URL: {job.url}",
        "Description:",
        _truncate(job.description) or "(no description provided)",
    ]
    return "\n".join(lines)


def build_prompt(job: Job, profile: Profile) -> tuple[str, str]:
    """Return (system, user) prompts for scoring `job` against `profile`."""
    user = (
        f"<candidate>\n{_profile_block(profile)}\n</candidate>\n\n"
        f"<job>\n{_job_block(job)}\n</job>\n\n"
        f"{_INSTRUCTIONS}"
    )
    return SYSTEM_PROMPT, user


def _first_json_object(text: str) -> dict[str, Any]:
    """Find the first decodable {...} object in text (handles fences and surrounding prose)."""
    decoder = json.JSONDecoder()
    idx = text.find("{")
    while idx != -1:
        try:
            obj, _ = decoder.raw_decode(text, idx)
        except json.JSONDecodeError:
            idx = text.find("{", idx + 1)
            continue
        if isinstance(obj, dict):
            return obj
        idx = text.find("{", idx + 1)
    raise ValueError(f"No JSON object found in model output: {text[:200]!r}")


def _coerce_score(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError(f"score must be an integer 1-5, got {value!r}")
    if isinstance(value, str):
        value = value.strip()
        try:
            value = float(value)
        except ValueError:
            raise ValueError(f"score must be an integer 1-5, got {value!r}") from None
    if isinstance(value, float):
        if not value.is_integer():
            raise ValueError(f"score must be an integer 1-5, got {value!r}")
        value = int(value)
    if not isinstance(value, int):
        raise ValueError(f"score must be an integer 1-5, got {value!r}")
    if not 1 <= value <= 5:
        raise ValueError(f"score must be between 1 and 5, got {value}")
    return value


def _str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, list):
        return [str(v) for v in value if v is not None and str(v).strip()]
    raise ValueError(f"expected a list of strings, got {value!r}")


def parse_score(text: str, job_id: str) -> ScoreResult:
    """Parse Claude's reply into a ScoreResult. Raises ValueError on unusable output."""
    data = _first_json_object(text or "")
    if "score" not in data:
        raise ValueError(f"Model output has no 'score' field: {data!r}")
    try:
        return ScoreResult(
            job_id=job_id,
            score=_coerce_score(data["score"]),
            matched_skills=_str_list(data.get("matched_skills")),
            gaps=_str_list(data.get("gaps")),
            reason=str(data.get("reason") or "").strip(),
        )
    except ValidationError as e:
        raise ValueError(f"Invalid score output for {job_id}: {e}") from e


def score_job(job: Job, profile: Profile, client: Any) -> ScoreResult:
    system, user = build_prompt(job, profile)
    text = llm.complete_text(client, system, user, max_tokens=1024)
    return parse_score(text, job.id)


# --- cache -----------------------------------------------------------------


def load_cache(path: Path | None = None) -> dict[str, ScoreResult]:
    """Read cached scores keyed by job id. Later lines win; malformed lines are skipped."""
    return storage.load_scores(path)


def append_cache(result: ScoreResult, path: Path | None = None) -> None:
    path = Path(path) if path is not None else paths.SCORES_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(result.model_dump_json() + "\n")


def read_jobs(path: Path | None = None) -> list[Job]:
    """Read jobs JSONL. Missing file -> []; bad lines skipped."""
    return storage.load_jobs(path)


def uncached(jobs: Iterable[Job], cache: dict[str, ScoreResult]) -> list[Job]:
    return [j for j in jobs if j.id not in cache]


def score_jobs(
    jobs: Iterable[Job], profile: Profile, client: Any, cache_path: Path | None = None
) -> list[ScoreResult]:
    """Score each job, reusing cached results. One failing job is logged and skipped."""
    cache = load_cache(cache_path)
    results: list[ScoreResult] = []
    for job in jobs:
        if job.id in cache:
            results.append(cache[job.id])
            continue
        try:
            result = score_job(job, profile, client)
        except Exception as e:  # noqa: BLE001 - keep going on any per-job failure
            print(f"error scoring {job.id} ({job.company} - {job.title}): {e}", file=sys.stderr)
            continue
        append_cache(result, cache_path)
        cache[job.id] = result
        results.append(result)
    return results
