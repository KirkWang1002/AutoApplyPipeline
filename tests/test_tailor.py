"""M4 Tailor tests. Fake data and a fake Anthropic client only: no network, no API key."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from autoapply import llm, paths
from autoapply import tailor as t
from autoapply.cli import app
from autoapply.models import Job, Profile, ScoreResult

MASTER = """\
# Jane Doe
## Experience
### Firmware Intern, Acme Widgets (May 2025 - Aug 2025)
- Wrote C drivers for an STM32 sensor board, cutting boot time by 40%.
## Skills
C, Python, KiCad
"""

RESUME_OUT = "# Jane Doe\n\n- Wrote C drivers for an STM32 sensor board."
LETTER_OUT = "Dear Initech Hiring Team,\n\nI am applying...\n\nJane Doe"


class FakeMessages:
    def __init__(self, outputs: list[str] | None = None):
        self.calls: list[dict] = []
        self.outputs = outputs

    def create(self, **kw):
        self.calls.append(kw)
        if self.outputs is not None:
            text = self.outputs[len(self.calls) - 1]
        else:
            text = LETTER_OUT if "cover letter" in kw["system"] else RESUME_OUT
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)])


class FakeClient:
    def __init__(self, outputs: list[str] | None = None):
        self.messages = FakeMessages(outputs)


@pytest.fixture(autouse=True)
def _no_api_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


def make_job(i: int = 1, company: str = "Initech", title: str = "Embedded Software Intern") -> Job:
    return Job(
        id=f"greenhouse:initech:{i}",
        source="greenhouse",
        company=company,
        title=title,
        url=f"https://boards.greenhouse.io/initech/jobs/{i}",
        location="Austin, TX",
        description="Write firmware in C for our sensor products.",
    )


PROFILE = Profile(name="Jane Doe", email="jane@example.com", skills=["C"], min_score_to_tailor=4)


# --- master resume ---------------------------------------------------------


def test_master_md_preferred(tmp_path: Path):
    (tmp_path / "master.md").write_text("MD resume")
    (tmp_path / "master.tex").write_text("TEX resume")
    assert t.load_master_resume(tmp_path) == "MD resume"


def test_master_tex_fallback(tmp_path: Path):
    (tmp_path / "master.tex").write_text("\\section{Experience}")
    assert t.load_master_resume(tmp_path) == "\\section{Experience}"


def test_master_missing_raises_helpful_error(tmp_path: Path):
    with pytest.raises(FileNotFoundError, match="master.md"):
        t.load_master_resume(tmp_path)


# --- slugs / paths ---------------------------------------------------------


def test_slugify():
    assert t.slugify("Embedded Software Intern (Summer 2026)") == "embedded-software-intern-summer-2026"
    assert t.slugify("Café Société / R&D") == "cafe-societe-r-d"
    long = t.slugify("x" * 50 + " " + "y" * 50)
    assert len(long) <= 60 and not long.endswith("-")


def test_output_dir(tmp_path: Path):
    assert t.output_dir(make_job(), tmp_path) == tmp_path / "initech-embedded-software-intern"


def test_output_dir_collision_gets_suffix(tmp_path: Path):
    first = make_job(1)
    d = t.output_dir(first, tmp_path)
    d.mkdir()
    (d / "job.json").write_text(first.model_dump_json())
    assert t.output_dir(first, tmp_path) == d
    other = t.output_dir(make_job(2), tmp_path)
    assert other != d and other.name.startswith(d.name + "-")


# --- prompts ---------------------------------------------------------------


def test_resume_prompt_enforces_no_invention_and_includes_master():
    system, user = t.build_resume_prompt(MASTER, make_job(), PROFILE)
    assert "NEVER invent" in system
    assert "only reorder, rephrase, and select" in system.lower()
    for word in ("employers", "titles", "dates", "degrees", "metrics", "skills", "tools"):
        assert word in system
    assert "Markdown only" in system
    assert MASTER.strip() in user
    assert "Write firmware in C" in user


def test_cover_letter_prompt():
    system, user = t.build_cover_letter_prompt(MASTER, make_job(), PROFILE)
    assert "NEVER invent" in system
    assert "300 words" in system
    assert "Initech Hiring Team" in system
    assert "[Company]" in system  # named as a forbidden placeholder
    assert MASTER.strip() in user
    assert "Jane Doe" in user


# --- generation ------------------------------------------------------------


def test_tailor_job_writes_files(tmp_path: Path):
    client = FakeClient()
    job = make_job()
    dest = t.tailor_job(job, PROFILE, MASTER, client, tmp_path)
    assert dest == tmp_path / "initech-embedded-software-intern"
    assert (dest / "resume.md").read_text().strip() == RESUME_OUT
    assert (dest / "cover_letter.md").read_text().strip() == LETTER_OUT
    assert Job.model_validate_json((dest / "job.json").read_text()) == job
    assert len(client.messages.calls) == 2
    for call in client.messages.calls:
        assert "NEVER invent" in call["system"]
        assert MASTER.strip() in call["messages"][0]["content"]
        assert call["model"] == llm.model_id()


def test_tailor_job_strips_fences(tmp_path: Path):
    client = FakeClient(["```markdown\n# Jane Doe\n- item\n```\n", "```\nDear team,\n```"])
    dest = t.tailor_job(make_job(), PROFILE, MASTER, client, tmp_path)
    assert (dest / "resume.md").read_text() == "# Jane Doe\n- item\n"
    assert (dest / "cover_letter.md").read_text() == "Dear team,\n"


def test_strip_fences_leaves_plain_text():
    assert t.strip_fences("# Title\n\nUse `code` here.\n") == "# Title\n\nUse `code` here.\n"


# --- selection -------------------------------------------------------------


def test_select_jobs_to_tailor():
    jobs = [make_job(i) for i in range(1, 5)]
    scores = [
        ScoreResult(job_id=jobs[0].id, score=3),
        ScoreResult(job_id=jobs[1].id, score=4),
        ScoreResult(job_id=jobs[2].id, score=5),
        # jobs[3] unscored
    ]
    picked = t.select_jobs_to_tailor(jobs, scores, 4)
    assert [j.id for j in picked] == [jobs[2].id, jobs[1].id]
    assert len(t.select_jobs_to_tailor(jobs, scores, 1)) == 3


def test_select_last_score_wins():
    job = make_job()
    scores = [ScoreResult(job_id=job.id, score=5), ScoreResult(job_id=job.id, score=2)]
    assert t.select_jobs_to_tailor([job], scores, 4) == []


# --- CLI -------------------------------------------------------------------


@pytest.fixture
def cli_env(tmp_path: Path, monkeypatch):
    data, out, resume = tmp_path / "data", tmp_path / "out", tmp_path / "resume"
    data.mkdir()
    resume.mkdir()
    (resume / "master.md").write_text(MASTER)
    jobs = [make_job(1, title="Firmware Intern"), make_job(2, title="Test Intern"), make_job(3, title="PCB Intern")]
    (data / "jobs.jsonl").write_text("\n".join(j.model_dump_json() for j in jobs) + "\n")
    scores = [ScoreResult(job_id=jobs[0].id, score=5), ScoreResult(job_id=jobs[1].id, score=2)]
    (data / "scores.jsonl").write_text("\n".join(s.model_dump_json() for s in scores) + "\n")
    monkeypatch.setattr(paths, "JOBS_FILE", data / "jobs.jsonl")
    monkeypatch.setattr(paths, "SCORES_FILE", data / "scores.jsonl")
    monkeypatch.setattr(paths, "OUT_DIR", out)
    monkeypatch.setattr(paths, "RESUME_DIR", resume)
    client = FakeClient()
    created = []

    def fake_get_client():
        created.append(1)
        return client

    monkeypatch.setattr(llm, "get_client", fake_get_client)
    return SimpleNamespace(out=out, resume=resume, client=client, created=created, jobs=jobs)


def test_cli_tailor_happy_path(cli_env):
    runner = CliRunner()
    result = runner.invoke(app, ["tailor"])  # default --min from example profile (4)
    assert result.exit_code == 0, result.output
    dirs = sorted(p.name for p in cli_env.out.iterdir())
    assert dirs == ["initech-firmware-intern"]
    assert (cli_env.out / "initech-firmware-intern" / "resume.md").exists()
    assert (cli_env.out / "initech-firmware-intern" / "cover_letter.md").exists()
    assert len(cli_env.client.messages.calls) == 2

    # Second run: already tailored -> skipped, no client created.
    cli_env.created.clear()
    result = runner.invoke(app, ["tailor"])
    assert result.exit_code == 0, result.output
    assert "skip" in result.output and cli_env.created == []

    # --force regenerates.
    result = runner.invoke(app, ["tailor", "--force"])
    assert result.exit_code == 0, result.output
    assert len(cli_env.client.messages.calls) == 4


def test_cli_tailor_min_option(cli_env):
    result = CliRunner().invoke(app, ["tailor", "--min", "2"])
    assert result.exit_code == 0, result.output
    assert len(list(cli_env.out.iterdir())) == 2


def test_cli_tailor_job_id(cli_env):
    job = cli_env.jobs[2]  # unscored, but explicitly requested
    result = CliRunner().invoke(app, ["tailor", "--job-id", job.id])
    assert result.exit_code == 0, result.output
    dest = cli_env.out / "initech-pcb-intern"
    assert json.loads((dest / "job.json").read_text())["id"] == job.id


def test_cli_tailor_unknown_job_id(cli_env):
    result = CliRunner().invoke(app, ["tailor", "--job-id", "nope"])
    assert result.exit_code == 1
    assert cli_env.created == []


def test_cli_nothing_to_do_needs_no_client_or_resume(cli_env, tmp_path):
    (cli_env.resume / "master.md").unlink()
    paths.SCORES_FILE.write_text("")  # nothing scored
    result = CliRunner().invoke(app, ["tailor"])
    assert result.exit_code == 0, result.output
    assert "Nothing to tailor" in result.output
    assert cli_env.created == []


def test_cli_missing_master_resume(cli_env):
    (cli_env.resume / "master.md").unlink()
    result = CliRunner().invoke(app, ["tailor"])  # one job qualifies, but resume missing
    assert result.exit_code == 1
    assert "master.md" in result.output
    assert cli_env.created == []


def test_cli_without_api_key_fails_cleanly(cli_env, monkeypatch):
    monkeypatch.setattr(llm, "get_client", lambda: (_ for _ in ()).throw(RuntimeError("ANTHROPIC_API_KEY is not set")))
    result = CliRunner().invoke(app, ["tailor"])
    assert result.exit_code == 1
    assert "ANTHROPIC_API_KEY" in result.output
