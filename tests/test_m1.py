from pathlib import Path

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from autoapply.cli import app
from autoapply.config import load_companies, load_profile
from autoapply.models import ApplicationRecord, ApplicationStatus, Job, ScoreResult


def test_example_profile_loads():
    p = load_profile()
    assert p.name
    assert p.target.posted_within_days > 0
    assert "Senior" in p.target.exclude_title_words


def test_example_companies_load():
    c = load_companies()
    assert c.greenhouse[0].board == "anthropic"


def test_real_file_preferred_over_example(tmp_path: Path):
    (tmp_path / "profile.example.yaml").write_text("name: Example\nemail: e@x.com\n")
    (tmp_path / "profile.yaml").write_text("name: Real\nemail: r@x.com\n")
    assert load_profile(tmp_path).name == "Real"


def test_missing_config_raises(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        load_profile(tmp_path)


def test_empty_ats_section(tmp_path: Path):
    (tmp_path / "companies.yaml").write_text("greenhouse:\nlever:\n  - board: foo\n")
    c = load_companies(tmp_path)
    assert c.greenhouse == [] and c.lever[0].board == "foo"


def test_score_bounds():
    ScoreResult(job_id="x", score=5)
    with pytest.raises(ValidationError):
        ScoreResult(job_id="x", score=6)


def test_job_and_record_roundtrip():
    job = Job(id="greenhouse:acme:1", source="greenhouse", company="acme", title="EE Intern", url="https://x")
    assert Job.model_validate_json(job.model_dump_json()) == job
    rec = ApplicationRecord(job_id=job.id, company=job.company, title=job.title, url=job.url)
    assert rec.status is ApplicationStatus.found


def test_cli_help_and_config():
    runner = CliRunner()
    assert runner.invoke(app, ["--help"]).exit_code == 0
    result = runner.invoke(app, ["config"])
    assert result.exit_code == 0, result.output
    assert "greenhouse: anthropic" in result.output
