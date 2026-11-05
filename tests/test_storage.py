from autoapply import paths, storage
from autoapply.models import Job


def _job(i: int) -> Job:
    return Job(id=f"greenhouse:acme:{i}", source="greenhouse", company="acme", title="EE Intern", url=f"https://x/{i}")


def test_truncated_line_is_skipped(tmp_path, capsys):
    f = tmp_path / "jobs.jsonl"
    storage.append_jsonl([_job(1), _job(2)], f)
    with f.open("a", encoding="utf-8") as fh:
        fh.write('{"id": "greenhouse:acme:3", "sour')  # Ctrl+C mid-append
    assert [j.id for j in storage.load_jobs(f)] == ["greenhouse:acme:1", "greenhouse:acme:2"]
    assert "skipping invalid line 3" in capsys.readouterr().err


def test_default_path_read_at_call_time(tmp_path, monkeypatch):
    f = tmp_path / "jobs.jsonl"
    storage.append_jsonl([_job(1)], f)
    monkeypatch.setattr(paths, "JOBS_FILE", f)
    assert len(storage.load_jobs()) == 1
