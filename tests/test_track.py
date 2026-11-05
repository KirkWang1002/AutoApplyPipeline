from datetime import date
from pathlib import Path

import pytest
from typer.testing import CliRunner

from autoapply import track
from autoapply.cli import app
from autoapply.models import ApplicationRecord, ApplicationStatus as S, Job, ScoreResult

TODAY = date(2026, 3, 10)


def job(n: int, company: str = "Acme") -> Job:
    return Job(
        id=f"greenhouse:{company.lower()}:{n}",
        source="greenhouse",
        company=company,
        title=f"Hardware Intern {n}",
        url=f"https://example.com/jobs/{n}",
    )


def rec(n: int, status: S, score: int | None = None, updated: date = TODAY, notes: str = "") -> ApplicationRecord:
    j = job(n)
    return ApplicationRecord(
        job_id=j.id, company=j.company, title=j.title, url=j.url,
        status=status, score=score, updated=updated, notes=notes,
    )


# ---------------------------------------------------------------- csv


def test_csv_roundtrip(tmp_path: Path):
    path = tmp_path / "nested" / "applications.csv"
    records = {
        r.job_id: r
        for r in [
            rec(1, S.found),
            rec(2, S.applied, score=5, updated=date(2026, 1, 2), notes='said "hi", follow up\nnext week'),
        ]
    }
    track.save_applications(records, path)
    assert path.exists()
    assert path.read_text().splitlines()[0] == ",".join(ApplicationRecord.model_fields)
    assert list(tmp_path.joinpath("nested").iterdir()) == [path]  # no temp file left behind
    assert track.load_applications(path) == records


def test_load_missing_file_is_empty(tmp_path: Path):
    assert track.load_applications(tmp_path / "nope.csv") == {}


def test_load_scores_last_wins(tmp_path: Path):
    p = tmp_path / "scores.jsonl"
    p.write_text(
        ScoreResult(job_id="a", score=2).model_dump_json() + "\n\n"
        + ScoreResult(job_id="a", score=4).model_dump_json() + "\n"
    )
    assert track.load_scores(p)["a"].score == 4


# ---------------------------------------------------------------- sync


def test_sync_adds_new_jobs_as_found():
    out = track.sync([job(1), job(2)], {}, {}, today=TODAY)
    assert set(out) == {job(1).id, job(2).id}
    assert all(r.status == S.found and r.score is None and r.updated == TODAY for r in out.values())


def test_sync_scores_bump_found_to_scored():
    records = {job(1).id: rec(1, S.found, updated=date(2026, 1, 1))}
    out = track.sync([job(1)], {job(1).id: ScoreResult(job_id=job(1).id, score=4)}, records, today=TODAY)
    assert out[job(1).id].status == S.scored
    assert out[job(1).id].score == 4
    assert out[job(1).id].updated == TODAY
    assert records[job(1).id].status == S.found  # input not mutated


@pytest.mark.parametrize("status", [S.drafted, S.applied, S.interview, S.rejected, S.offer])
def test_sync_never_downgrades(status):
    old = date(2026, 1, 1)
    records = {job(1).id: rec(1, status, score=3, updated=old, notes="keep me")}
    out = track.sync([job(1)], {job(1).id: ScoreResult(job_id=job(1).id, score=3)}, records, today=TODAY)
    r = out[job(1).id]
    assert r.status == status
    assert r.notes == "keep me"
    assert r.updated == old  # nothing changed


def test_sync_keeps_records_whose_job_vanished_and_ignores_orphan_scores():
    records = {job(9).id: rec(9, S.applied)}
    out = track.sync([], {"unknown": ScoreResult(job_id="unknown", score=5)}, records, today=TODAY)
    assert set(out) == {job(9).id}


# ---------------------------------------------------------------- set_status


def test_set_status_updates_record():
    records = {job(1).id: rec(1, S.scored, updated=date(2026, 1, 1))}
    track.set_status(records, job(1).id, "applied", notes="via referral", today=TODAY)
    r = records[job(1).id]
    assert (r.status, r.notes, r.updated) == (S.applied, "via referral", TODAY)


def test_set_status_unknown_id_raises():
    records = {job(1).id: rec(1, S.scored)}
    with pytest.raises(KeyError, match="Unknown job id"):
        track.set_status(records, "lever:nope:1", S.applied)


# ---------------------------------------------------------------- report


def test_report_sections_and_ordering():
    records = {
        r.job_id: r
        for r in [
            rec(1, S.scored, score=3),
            rec(2, S.scored, score=5),
            rec(3, S.found),
            rec(4, S.drafted, score=4, updated=date(2026, 3, 8)),
            rec(5, S.applied, score=5, updated=date(2026, 3, 1)),
            rec(6, S.rejected, score=5),
            rec(7, S.interview, score=4),
        ]
    }
    text = track.build_report(records, TODAY)
    top_header = text.index("Top scored jobs not yet applied")
    pending_header = text.index("Pending")
    top, pending = text[top_header:pending_header], text[pending_header:]

    assert "Total: 7" in text and "applied=1" in text and "scored=2" in text

    order = [job(n).id for n in (2, 4, 1, 3)]
    positions = [top.index(i) for i in order]
    assert positions == sorted(positions)
    for n in (5, 6, 7):
        assert job(n).id not in top

    assert job(4).id in pending and job(5).id in pending
    for n in (1, 2, 3, 6, 7):
        assert job(n).id not in pending
    line5 = next(line for line in pending.splitlines() if job(5).id in line)
    assert line5.split()[:2] == ["applied", "9"]
    line4 = next(line for line in pending.splitlines() if job(4).id in line)
    assert line4.split()[:2] == ["drafted", "2"]


def test_report_top_n_limit_and_empty():
    records = {job(n).id: rec(n, S.scored, score=1 + n % 5) for n in range(20)}
    text = track.build_report(records, TODAY)
    top = text[text.index("Top scored"): text.index("Pending")]
    assert sum(1 for n in range(20) if job(n).id in top) == track.TOP_N
    empty = track.build_report({}, TODAY)
    assert empty.count("(none)") == 2


# ---------------------------------------------------------------- CLI


@pytest.fixture
def files(tmp_path: Path, monkeypatch):
    jobs_f = tmp_path / "data" / "jobs.jsonl"
    scores_f = tmp_path / "data" / "scores.jsonl"
    apps_f = tmp_path / "data" / "applications.csv"
    jobs_f.parent.mkdir(parents=True)
    jobs_f.write_text("".join(job(n).model_dump_json() + "\n" for n in (1, 2)))
    scores_f.write_text(ScoreResult(job_id=job(2).id, score=5).model_dump_json() + "\n")
    monkeypatch.setattr(track, "JOBS_FILE", jobs_f)
    monkeypatch.setattr(track, "SCORES_FILE", scores_f)
    monkeypatch.setattr(track, "APPLICATIONS_FILE", apps_f)
    return apps_f


def test_cli_status_syncs_then_sets(files: Path):
    result = CliRunner().invoke(app, ["status", job(1).id, "applied", "--notes", "sent"])
    assert result.exit_code == 0, result.output
    records = track.load_applications(files)
    assert records[job(1).id].status == S.applied
    assert records[job(1).id].notes == "sent"
    assert records[job(2).id].status == S.scored and records[job(2).id].score == 5


def test_cli_status_rejects_bad_status_and_unknown_id(files: Path):
    runner = CliRunner()
    bad = runner.invoke(app, ["status", job(1).id, "ghosted"])
    assert bad.exit_code == 2
    assert "Invalid status" in bad.output
    unknown = runner.invoke(app, ["status", "lever:nope:1", "applied"])
    assert unknown.exit_code == 1
    assert "Unknown job id" in unknown.output


def test_cli_report(files: Path):
    runner = CliRunner()
    runner.invoke(app, ["status", job(1).id, "applied"])
    result = runner.invoke(app, ["report"])
    assert result.exit_code == 0, result.output
    top = result.output[result.output.index("Top scored"): result.output.index("Pending")]
    pending = result.output[result.output.index("Pending"):]
    assert job(2).id in top and job(1).id not in top
    assert job(1).id in pending
    assert files.exists()
