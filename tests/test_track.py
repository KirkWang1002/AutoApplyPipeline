import csv
from datetime import date
from pathlib import Path

import pytest
from typer.testing import CliRunner

from autoapply import paths
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
    monkeypatch.setattr(paths, "JOBS_FILE", jobs_f)
    monkeypatch.setattr(paths, "SCORES_FILE", scores_f)
    monkeypatch.setattr(paths, "APPLICATIONS_FILE", apps_f)
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


# ---------------------------------------------------------------- Excel-edited CSV


HEADER = ",".join(ApplicationRecord.model_fields)


def write_raw(path: Path, lines: list[str], bom: bool = True) -> None:
    path.write_bytes((("﻿" if bom else "") + "\r\n".join(lines) + "\r\n").encode("utf-8"))


def test_load_excel_saved_csv(tmp_path: Path):
    p = tmp_path / "applications.csv"
    write_raw(p, [
        HEADER,
        "a:1,Acme,Intern 1,https://x/1, Applied ,4,11/5/2026,",
        "a:2,Acme,Intern 2,https://x/2,SCORED,,3/9/26,note",
        "a:3,Acme,Intern 3,https://x/3,found,,2026-03-01,",
        ",,,,,,,",  # blank row Excel leaves behind
    ])
    recs = track.load_applications(p)
    assert set(recs) == {"a:1", "a:2", "a:3"}
    assert (recs["a:1"].status, recs["a:1"].score, recs["a:1"].updated) == (S.applied, 4, date(2026, 11, 5))
    assert (recs["a:2"].status, recs["a:2"].score, recs["a:2"].updated) == (S.scored, None, date(2026, 3, 9))
    assert recs["a:3"].updated == date(2026, 3, 1)


def test_invalid_rows_warn_skip_and_survive_save(tmp_path: Path, capsys):
    p = tmp_path / "applications.csv"
    write_raw(p, [
        HEADER,
        "a:1,Acme,Intern 1,https://x/1,applied,4,2026-03-01,",
        "a:2,Acme,Intern 2,https://x/2,ghosted,,2026-03-01,typo",
        "a:3,Acme,Intern 3,https://x/3,found,high,someday,",
    ])
    recs = track.load_applications(p)
    assert set(recs) == {"a:1"}
    err = capsys.readouterr().err
    assert "row 3" in err and "ghosted" in err and "row 4" in err

    # Saving must not silently delete the rows the human still has to fix.
    track.set_status(recs, "a:1", S.interview, today=TODAY)
    track.save_applications(recs, p)
    lines = p.read_text(encoding="utf-8").splitlines()
    assert lines[0] == HEADER
    assert lines[1].startswith("a:1,") and ",interview," in lines[1]
    assert "a:2,Acme,Intern 2,https://x/2,ghosted,,2026-03-01,typo" in lines
    assert "a:3,Acme,Intern 3,https://x/3,found,high,someday," in lines


@pytest.mark.parametrize(
    "value",
    ['=HYPERLINK("http://evil")', "+1", "-2", "@SUM(A1)", "\tx", "\rx", "'=already", "''+x",
     "'plain quote", "normal"],
)
def test_formula_cells_escaped_and_roundtrip(tmp_path: Path, value: str):
    p = tmp_path / "applications.csv"
    r = rec(1, S.found, notes=value)
    r.title = value
    r.company = value
    track.save_applications({r.job_id: r}, p)
    with p.open(newline="", encoding="utf-8") as f:
        row = next(csv.DictReader(f))
    dangerous = value.lstrip("'")[:1] in ("=", "+", "-", "@", "\t", "\r")
    for field in ("title", "company", "notes"):
        assert row[field] == ("'" + value if dangerous else value)
    assert track.load_applications(p) == {r.job_id: r}


def _locked_replace(src, dst):
    raise PermissionError(13, "The process cannot access the file")


def test_save_permission_error_is_friendly(tmp_path: Path, monkeypatch):
    p = tmp_path / "applications.csv"
    monkeypatch.setattr(track.os, "replace", _locked_replace)
    with pytest.raises(track.ApplicationsFileLocked, match="close applications.csv in Excel"):
        track.save_applications({job(1).id: rec(1, S.found)}, p)
    assert list(tmp_path.iterdir()) == []  # temp file cleaned up


# ---------------------------------------------------------------- mark_drafted


def test_mark_drafted_only_upgrades_found_and_scored():
    old = date(2026, 1, 1)
    records = {
        r.job_id: r
        for r in [rec(1, S.found, updated=old), rec(2, S.scored, score=4, updated=old),
                  rec(3, S.applied, updated=old), rec(4, S.interview, updated=old),
                  rec(5, S.drafted, updated=old)]
    }
    ids = [job(n).id for n in range(1, 6)] + ["unknown:1"]
    assert track.mark_drafted(records, ids, today=TODAY) == [job(1).id, job(2).id]
    statuses = [records[job(n).id].status for n in range(1, 6)]
    assert statuses == [S.drafted, S.drafted, S.applied, S.interview, S.drafted]
    assert records[job(1).id].updated == TODAY
    assert records[job(3).id].updated == old


# ---------------------------------------------------------------- CLI: no needless writes


def test_report_does_not_rewrite_unchanged_file(files: Path, monkeypatch):
    runner = CliRunner()
    assert runner.invoke(app, ["report"]).exit_code == 0  # first run creates the file
    assert files.exists()
    monkeypatch.setattr(track, "save_applications", lambda *a, **k: pytest.fail("rewrote file"))
    result = runner.invoke(app, ["report"])
    assert result.exit_code == 0, result.output


def test_report_reads_excel_file_without_rewriting(files: Path, monkeypatch):
    write_raw(files, [
        HEADER,
        f"{job(1).id},Acme,Hardware Intern 1,https://example.com/jobs/1,Applied,,3/1/2026,",
        f"{job(2).id},Acme,Hardware Intern 2,https://example.com/jobs/2,scored,5,3/1/2026,",
        "junk,Acme,X,https://x,ghosted,,3/1/2026,",
    ])
    before = files.read_bytes()
    monkeypatch.setattr(track, "save_applications", lambda *a, **k: pytest.fail("rewrote file"))
    result = CliRunner().invoke(app, ["report"])
    assert result.exit_code == 0, result.output
    assert "skipping invalid row 4" in result.output
    assert job(1).id in result.output[result.output.index("Pending"):]
    assert files.read_bytes() == before


@pytest.mark.parametrize("args", [["report"], ["status", job(1).id, "applied"]])
def test_cli_locked_file_message(files: Path, monkeypatch, args):
    monkeypatch.setattr(track.os, "replace", _locked_replace)
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 1
    assert "close applications.csv in Excel and retry" in result.output
    assert "Traceback" not in result.output
    assert isinstance(result.exception, SystemExit)
