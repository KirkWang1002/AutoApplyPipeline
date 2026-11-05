"""M4 Tailor: produce a job-specific resume and cover letter from the master resume.

The LLM may only reorder, rephrase and select content from the master resume. It must
never invent experience. Everything here is network-free except the client passed in.
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from autoapply import storage
from autoapply.llm import complete_text
from autoapply.models import Job, Profile, ScoreResult

RESUME_FILE = "resume.md"
COVER_LETTER_FILE = "cover_letter.md"
JOB_FILE = "job.json"

MASTER_CANDIDATES = ("master.md", "master.tex")
SLUG_MAX_LEN = 60

# Truncate very long job descriptions so prompts stay a sane size.
MAX_DESCRIPTION_CHARS = 12_000

NO_INVENTION_RULES = """\
STRICT TRUTHFULNESS RULES (these override everything else):
1. The MASTER RESUME is the only source of truth about the candidate's experience.
   You may ONLY reorder, rephrase, and select content that is already in it.
2. NEVER invent or add employers, job titles, dates, degrees, schools, GPAs, metrics,
   numbers, projects, certifications, awards, skills, tools, or technologies that are
   not explicitly present in the master resume. Do not "round up" or exaggerate.
3. Keep every fact verbatim-accurate: names of employers, titles, dates, degrees,
   numbers and metrics must match the master resume exactly.
4. If the job asks for something the master resume does not show, leave it out.
   Do not claim it, imply it, or hint at it. Omitting is always better than inventing.
5. Use keywords from the job posting only where the master resume already supports them.
"""


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


def load_master_resume(resume_dir: Path) -> str:
    """Return the master resume text: `master.md` preferred, `master.tex` as fallback."""
    resume_dir = Path(resume_dir)
    for name in MASTER_CANDIDATES:
        path = resume_dir / name
        if path.is_file():
            text = path.read_text(encoding="utf-8")
            if not text.strip():
                raise ValueError(f"Master resume {path} is empty.")
            return text
    raise FileNotFoundError(
        f"No master resume found in {resume_dir}. Create {resume_dir / 'master.md'} "
        f"(or {resume_dir / 'master.tex'}) containing your full resume: every job, project, "
        "skill and metric you might want to use. The tailor stage only selects and rephrases "
        "from it. The resume/ folder is git-ignored."
    )


def load_jobs(path: Path) -> list[Job]:
    """Read JOBS_FILE (JSONL of Job). Missing file -> []."""
    return storage.load_jobs(path)


def load_scores(path: Path) -> list[ScoreResult]:
    """Read SCORES_FILE (JSONL of ScoreResult). Missing file -> []."""
    return list(storage.load_scores(path).values())


def select_jobs_to_tailor(
    jobs: Iterable[Job], scores: Iterable[ScoreResult], min_score: int
) -> list[Job]:
    """Jobs whose score is >= min_score, best first. Unscored jobs are skipped.

    If a job has several score rows, the last one wins. Ties keep the jobs' input order.
    """
    by_id: dict[str, int] = {}
    for s in scores:
        by_id[s.job_id] = s.score
    picked = [j for j in jobs if by_id.get(j.id, 0) >= min_score]
    # dedupe by id, keeping first occurrence
    seen: set[str] = set()
    unique = [j for j in picked if not (j.id in seen or seen.add(j.id))]
    return sorted(unique, key=lambda j: -by_id[j.id])


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


def slugify(text: str, max_len: int = SLUG_MAX_LEN) -> str:
    """Lowercase ASCII slug with hyphens, at most `max_len` chars."""
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-")
    return slug[:max_len].rstrip("-")


def output_dir(job: Job, out_dir: Path) -> Path:
    """`out/<company-slug>-<title-slug>/` for this job.

    If that folder already belongs to a *different* job (same company + title, e.g. two
    locations), a short hash of the job id is appended so outputs don't collide.
    """
    name = slugify(f"{job.company} {job.title}") or "job"
    path = Path(out_dir) / name
    existing = path / JOB_FILE
    if existing.is_file():
        try:
            other_id = json.loads(existing.read_text(encoding="utf-8")).get("id")
        except (OSError, ValueError):
            other_id = None
        if other_id and other_id != job.id:
            suffix = hashlib.sha1(job.id.encode("utf-8")).hexdigest()[:6]
            path = Path(out_dir) / f"{name}-{suffix}"
    return path


def is_tailored(dir_: Path) -> bool:
    return (dir_ / RESUME_FILE).is_file() and (dir_ / COVER_LETTER_FILE).is_file()


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------


def _job_block(job: Job) -> str:
    desc = job.description.strip()
    if len(desc) > MAX_DESCRIPTION_CHARS:
        desc = desc[:MAX_DESCRIPTION_CHARS] + "\n[...truncated]"
    loc = job.location + (" (remote)" if job.remote else "")
    return (
        f"Company: {job.company}\n"
        f"Title: {job.title}\n"
        f"Location: {loc or 'n/a'}\n"
        f"URL: {job.url}\n\n"
        f"Description:\n{desc or '(no description provided)'}"
    )


def _profile_block(profile: Profile) -> str:
    lines = [f"Name: {profile.name}", f"Email: {profile.email}"]
    if profile.phone:
        lines.append(f"Phone: {profile.phone}")
    if profile.location:
        lines.append(f"Location: {profile.location}")
    for label, url in profile.links.items():
        lines.append(f"{label}: {url}")
    for edu in profile.education:
        lines.append(f"Education: {edu.degree}, {edu.school}" + (f" ({edu.grad})" if edu.grad else ""))
    if profile.work_authorization:
        lines.append(f"Work authorization: {profile.work_authorization}")
    return "\n".join(lines)


def build_resume_prompt(master: str, job: Job, profile: Profile) -> tuple[str, str]:
    """Return (system, user) prompts for a tailored resume."""
    system = (
        "You tailor a candidate's resume to a specific job posting.\n\n"
        + NO_INVENTION_RULES
        + """
TASK:
- Select the master-resume content most relevant to the job and order it so the strongest
  matches come first (sections, bullets, and skills).
- You may tighten and rephrase bullets for clarity and to mirror the job's wording, but the
  underlying facts must stay exactly as in the master resume.
- You may drop less relevant items to keep it to roughly one page.
- The CONTACT INFO block may be used only for the resume header.

OUTPUT FORMAT:
- Output the resume in Markdown only. No preamble, no explanation, no notes, no commentary,
  no code fences. Start directly with the candidate's name as a heading.
"""
    )
    user = (
        "<master_resume>\n"
        f"{master.strip()}\n"
        "</master_resume>\n\n"
        "<contact_info>\n"
        f"{_profile_block(profile)}\n"
        "</contact_info>\n\n"
        "<job_posting>\n"
        f"{_job_block(job)}\n"
        "</job_posting>\n\n"
        "Write the tailored resume now, following every rule in the system prompt."
    )
    return system, user


def build_cover_letter_prompt(master: str, job: Job, profile: Profile) -> tuple[str, str]:
    """Return (system, user) prompts for a cover letter."""
    system = (
        "You write a concise, specific cover letter for a candidate applying to a job.\n\n"
        + NO_INVENTION_RULES
        + f"""
6. For the cover letter, the only allowed facts about the candidate are those in the MASTER
   RESUME and the CANDIDATE PROFILE. Do not invent motivations tied to fabricated
   experiences, and do not claim knowledge of the company beyond the job posting.

TASK:
- At most about 300 words.
- Address it to the hiring team at {job.company} (e.g. "Dear {job.company} Hiring Team,").
- Connect two or three concrete, relevant items from the master resume to the role.
- Close with the candidate's name from the profile.
- Never use placeholders such as [Company], [Hiring Manager], [Your Name], or [Date].
  Use the real company name, role title, and candidate name given below.

OUTPUT FORMAT:
- Output the cover letter in Markdown only. No preamble, no explanation, no notes,
  no commentary, no code fences.
"""
    )
    user = (
        "<master_resume>\n"
        f"{master.strip()}\n"
        "</master_resume>\n\n"
        "<candidate_profile>\n"
        f"{_profile_block(profile)}\n"
        "</candidate_profile>\n\n"
        "<job_posting>\n"
        f"{_job_block(job)}\n"
        "</job_posting>\n\n"
        f"Write the cover letter to the {job.company} hiring team for the {job.title} role now, "
        "following every rule in the system prompt."
    )
    return system, user


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------

_FENCE_RE = re.compile(r"^\s*```[\w-]*[ \t]*\n(.*?)\n?```\s*$", re.DOTALL)


def strip_fences(text: str) -> str:
    """Remove a ``` / ```markdown fence wrapping the whole output, if present."""
    m = _FENCE_RE.match(text)
    body = m.group(1) if m else text
    return body.strip() + "\n"


def tailor_job(job: Job, profile: Profile, master: str, client: Any, out_dir: Path) -> Path:
    """Generate resume.md + cover_letter.md (+ job.json) for `job`; return the folder.

    Files are only written after both LLM calls succeed, so a failure leaves no half output.
    """
    dest = output_dir(job, out_dir)

    system, user = build_resume_prompt(master, job, profile)
    resume = strip_fences(complete_text(client, system, user, max_tokens=4096))

    system, user = build_cover_letter_prompt(master, job, profile)
    letter = strip_fences(complete_text(client, system, user, max_tokens=1500))

    if not resume.strip() or not letter.strip():
        raise RuntimeError(f"Empty model output while tailoring {job.id}")

    dest.mkdir(parents=True, exist_ok=True)
    (dest / RESUME_FILE).write_text(resume, encoding="utf-8")
    (dest / COVER_LETTER_FILE).write_text(letter, encoding="utf-8")
    (dest / JOB_FILE).write_text(job.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return dest
