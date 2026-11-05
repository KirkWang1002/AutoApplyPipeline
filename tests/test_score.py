import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from autoapply import llm, paths
from autoapply import score as scoring
from autoapply.cli import app
from autoapply.models import Education, Job, Profile, ScoreResult, Target


class FakeMessages:
    def __init__(self, replies):
        self.replies = replies  # job title -> reply text, or Exception to raise
        self.calls = []

    def create(self, **kw):
        self.calls.append(kw)
        prompt = kw["messages"][0]["content"]
        for title, reply in self.replies.items():
            if f"Title: {title}\n" in prompt:
                if isinstance(reply, Exception):
                    raise reply
                return SimpleNamespace(content=[SimpleNamespace(type="text", text=reply)])
        raise AssertionError("unexpected prompt")


class FakeClient:
    def __init__(self, replies):
        self.messages = FakeMessages(replies)


def reply(score, reason="ok"):
    return json.dumps({"score": score, "matched_skills": ["Python"], "gaps": [], "reason": reason})


@pytest.fixture
def profile():
    return Profile(
        name="Test Person",
        email="t@example.com",
        location="Austin, TX",
        work_authorization="US citizen",
        education=[Education(school="State U", degree="BS EE", grad="2027-05")],
        target=Target(titles=["Hardware Engineer Intern"], locations=["Remote"]),
        skills=["Python", "Verilog", "KiCad"],
    )


def make_job(n, title=None, company="Acme"):
    return Job(
        id=f"greenhouse:acme:{n}",
        source="greenhouse",
        company=company,
        title=title or f"Role {n}",
        url=f"https://example.com/{n}",
        description="Build things with Python.",
    )


def test_prompt_contains_profile_and_job(profile):
    system, user = scoring.build_prompt(make_job(1, title="FPGA Intern"), profile)
    assert "JSON" in system
    for s in ("Python", "Verilog", "KiCad", "FPGA Intern", "US citizen", "2027-05", "Hardware Engineer Intern"):
        assert s in user


def test_prompt_truncates_long_description(profile):
    job = make_job(1).model_copy(update={"description": "x" * 50_000})
    _, user = scoring.build_prompt(job, profile)
    assert len(user) < 12_000
    assert "truncated" in user


def test_parse_fenced_json_with_prose():
    text = 'Sure! Here is my rating:\n```json\n{"score": 4, "matched_skills": ["Python"], "gaps": ["Go"], "reason": "solid"}\n```\nThanks'
    r = scoring.parse_score(text, "job-1")
    assert r == ScoreResult(job_id="job-1", score=4, matched_skills=["Python"], gaps=["Go"], reason="solid")


def test_parse_coerces_numeric_string():
    assert scoring.parse_score('{"score": "5", "reason": "x"}', "j").score == 5


@pytest.mark.parametrize("text", ["no json here", "", "{not json}", '{"reason": "missing score"}', "[1, 2]"])
def test_parse_invalid_raises(text):
    with pytest.raises(ValueError):
        scoring.parse_score(text, "j")


@pytest.mark.parametrize("bad", [0, 6, 4.5, "high", True])
def test_parse_out_of_range_rejected(bad):
    with pytest.raises(ValueError):
        scoring.parse_score(json.dumps({"score": bad}), "j")


def test_score_job_uses_client(profile):
    client = FakeClient({"Role 1": reply(5, "great")})
    r = scoring.score_job(make_job(1), profile, client)
    assert r.job_id == "greenhouse:acme:1" and r.score == 5 and r.reason == "great"
    assert client.messages.calls[0]["model"] == llm.model_id()


def test_cache_prevents_second_call(profile, tmp_path: Path):
    cache = tmp_path / "scores.jsonl"
    jobs = [make_job(1), make_job(2)]
    client = FakeClient({"Role 1": reply(4), "Role 2": reply(2)})
    first = scoring.score_jobs(jobs, profile, client, cache)
    assert len(client.messages.calls) == 2
    assert set(scoring.load_cache(cache)) == {j.id for j in jobs}

    second = scoring.score_jobs(jobs, profile, client, cache)
    assert len(client.messages.calls) == 2  # no new calls
    assert second == first


def test_one_failing_job_does_not_stop_others(profile, tmp_path: Path, capsys):
    cache = tmp_path / "scores.jsonl"
    jobs = [make_job(1), make_job(2), make_job(3)]
    client = FakeClient({"Role 1": reply(3), "Role 2": RuntimeError("boom"), "Role 3": "garbage"})
    results = scoring.score_jobs(jobs, profile, client, cache)
    assert [r.job_id for r in results] == ["greenhouse:acme:1"]
    err = capsys.readouterr().err
    assert "greenhouse:acme:2" in err and "boom" in err and "greenhouse:acme:3" in err
    # Failures aren't cached, so they get retried next run.
    assert set(scoring.load_cache(cache)) == {"greenhouse:acme:1"}


def test_load_cache_skips_bad_lines(tmp_path: Path):
    cache = tmp_path / "scores.jsonl"
    scoring.append_cache(ScoreResult(job_id="a", score=3), cache)
    with cache.open("a") as f:
        f.write("not json\n\n")
    scoring.append_cache(ScoreResult(job_id="a", score=5), cache)
    assert scoring.load_cache(cache)["a"].score == 5
    assert scoring.load_cache(tmp_path / "missing.jsonl") == {}


def _setup_cli(monkeypatch, tmp_path, jobs):
    jobs_file = tmp_path / "jobs.jsonl"
    jobs_file.write_text("".join(j.model_dump_json() + "\n" for j in jobs))
    monkeypatch.setattr(paths, "JOBS_FILE", jobs_file)
    monkeypatch.setattr(paths, "SCORES_FILE", tmp_path / "scores.jsonl")


def test_cli_score(monkeypatch, tmp_path: Path):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    jobs = [make_job(1, "Role 1", "Zeta"), make_job(2, "Role 2", "Beta"), make_job(3, "Role 3", "Alpha")]
    _setup_cli(monkeypatch, tmp_path, jobs)
    client = FakeClient({"Role 1": reply(5, "top pick"), "Role 2": reply(4, "good"), "Role 3": reply(2, "meh")})
    created = []
    monkeypatch.setattr(llm, "get_client", lambda: created.append(1) or client)

    result = CliRunner().invoke(app, ["score", "--min", "4"])
    assert result.exit_code == 0, result.output
    out = result.stdout
    assert "Zeta" in out and "Beta" in out and "Alpha" not in out
    assert out.index("Zeta") < out.index("Beta")  # sorted by score desc
    assert "top pick" in out and "https://example.com/1" in out
    assert len(created) == 1

    # Second run: everything cached, so no client is created at all.
    monkeypatch.setattr(llm, "get_client", lambda: pytest.fail("client should not be created"))
    result = CliRunner().invoke(app, ["score", "--min", "2"])
    assert result.exit_code == 0, result.output
    assert "Alpha" in result.stdout
    assert len(client.messages.calls) == 3


def test_cli_score_no_jobs(monkeypatch, tmp_path: Path):
    _setup_cli(monkeypatch, tmp_path, [])
    result = CliRunner().invoke(app, ["score"])
    assert result.exit_code == 0
    assert "No jobs" in result.output
