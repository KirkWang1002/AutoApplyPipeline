import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from autoapply import paths
from autoapply import discover as disc
from autoapply.cli import app
from autoapply.discover import (
    dedupe,
    discover,
    fetch_ashby,
    fetch_greenhouse,
    fetch_lever,
    filter_jobs,
    html_to_text,
    load_jobs,
    location_matches,
    parse_ashby,
    parse_greenhouse,
    parse_lever,
    save_jobs,
    title_matches,
)
from autoapply.models import Companies, CompanyBoard, Job, Profile, Target

FIXTURES = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)
TARGET = Target(
    titles=["Hardware Engineer Intern", "Embedded Software Intern", "Electrical Engineering Intern"],
    locations=["Remote", "NC", "CA", "TX"],
    exclude_title_words=["Senior", "Staff", "Principal", "Manager", "Director"],
    posted_within_days=14,
)


def fixture(name: str):
    return json.loads((FIXTURES / name).read_text())


def job(**kw) -> Job:
    base = dict(id="greenhouse:acme:1", source="greenhouse", company="acme", title="Hardware Engineer Intern",
                url="https://example.com/jobs/1", location="Austin, TX")
    base.update(kw)
    return Job(**base)


def mock_client(routes: dict[str, httpx.Response | Exception]) -> httpx.Client:
    """Client whose responses are keyed by host; unknown hosts get a 404."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        r = routes.get(request.url.host, httpx.Response(404))
        if isinstance(r, Exception):
            raise r
        return r

    client = httpx.Client(transport=httpx.MockTransport(handler))
    client.seen = seen  # type: ignore[attr-defined]
    return client


def all_fixture_routes() -> dict[str, httpx.Response]:
    return {
        "boards-api.greenhouse.io": httpx.Response(200, json=fixture("greenhouse.json")),
        "api.lever.co": httpx.Response(200, json=fixture("lever.json")),
        "api.ashbyhq.com": httpx.Response(200, json=fixture("ashby.json")),
    }


# ---------------------------------------------------------------- parsers


def test_parse_greenhouse():
    jobs = parse_greenhouse(fixture("greenhouse.json"), "acme-robotics")
    assert len(jobs) == 4
    j = jobs[0]
    assert j.id == "greenhouse:acme-robotics:1001"
    assert j.source == "greenhouse" and j.company == "Acme Robotics"  # display name, slug stays in id
    assert j.url == "https://boards.greenhouse.io/acme-robotics/jobs/1001"
    assert j.location == "Austin, TX" and not j.remote
    assert j.posted_at == datetime(2026, 11, 1, 13, 30, tzinfo=timezone.utc)
    assert "<" not in j.description and "&lt;" not in j.description
    assert "Help us build robot boards." in j.description
    assert "- PCB bring-up & test" in j.description
    assert jobs[1].remote  # "Remote - US"


def test_parse_lever():
    jobs = parse_lever(fixture("lever.json"), "widgetco")
    assert [j.id for j in jobs][:2] == ["lever:widgetco:aaaa-1111", "lever:widgetco:aaaa-2222"]
    assert jobs[0].title == "Embedded Software Intern"
    assert jobs[0].url == "https://jobs.lever.co/widgetco/aaaa-1111"
    assert jobs[0].location == "San Francisco, CA; Austin, TX" and not jobs[0].remote  # + allLocations, deduped
    assert jobs[0].posted_at == datetime(2026, 11, 3, tzinfo=timezone.utc)
    assert jobs[0].description == (
        "Write firmware for tiny widgets.\n\n"
        "What you'll do\n- Write C for ARM Cortex-M parts\n- Bring up new boards\n\n"
        "Requirements\n- Pursuing a B.S. in EE or CS\n- Comfort with an oscilloscope & logic analyzer\n\n"
        "Widgetco is an equal opportunity employer."
    )
    assert jobs[0].company == "Widgetco"
    assert jobs[1].location == "Anywhere"
    assert jobs[1].remote  # workplaceType == "remote"
    assert jobs[2].description == "Make posters."  # no lists / additional


def test_parse_lever_falls_back_to_html_and_tolerates_odd_lists():
    payload = [{
        "id": "x1", "text": "T", "hostedUrl": "https://jobs.lever.co/w/x1",
        "categories": {"allLocations": ["Toronto, ON", "", None]},
        "description": "<p>Intro <b>html</b></p>",
        "lists": [{"text": "Bonus", "content": ""}, "garbage", {"content": "<li>only bullets</li>"}],
        "additional": "<p>Closing html</p>",
    }]
    j = parse_lever(payload, "w")[0]
    assert j.location == "Toronto, ON"
    assert j.description == "Intro html\n\nBonus\n\n- only bullets\n\nClosing html"


def test_parse_ashby_secondary_locations_both_shapes():
    jobs = parse_ashby(fixture("ashby.json"), "sparkhw")
    assert jobs[0].location == "Durham, NC; Raleigh, NC"  # object + duplicate string
    assert jobs[2].location == "United States"  # empty secondaryLocations
    payload = {"jobs": [{"id": "1", "title": "T", "jobUrl": "u", "location": "London",
                         "secondaryLocations": ["Austin, TX", {"location": "Remote"}, {"address": {}}, 7]}]}
    j = parse_ashby(payload, "x")[0]
    assert j.location == "London; Austin, TX; Remote" and j.remote
    assert filter_jobs([j], Target(locations=["TX"], posted_within_days=10_000), NOW) == [j]


def test_company_display_name():
    assert CompanyBoard(board="example-company").display_name == "Example Company"
    assert CompanyBoard(board="spark_hw").display_name == "Spark Hw"
    assert CompanyBoard(board="anthropic", name="Anthropic, PBC").display_name == "Anthropic, PBC"
    assert CompanyBoard.model_validate({"board": "acme"}).name is None  # old configs still load
    assert parse_greenhouse(fixture("greenhouse.json"), "acme", "ACME Corp")[0].company == "ACME Corp"


def test_parse_ashby():
    jobs = parse_ashby(fixture("ashby.json"), "sparkhw")
    assert [j.id for j in jobs] == ["ashby:sparkhw:0f1e2d3c-0001", "ashby:sparkhw:0f1e2d3c-0002",
                                    "ashby:sparkhw:0f1e2d3c-0003"]  # unlisted 0004 skipped
    assert jobs[0].posted_at == datetime(2026, 10, 30, 15, tzinfo=timezone.utc)
    assert jobs[2].remote and jobs[2].posted_at is None
    assert jobs[2].description == "Fully remote internship."  # falls back to descriptionHtml


def test_parsers_tolerate_empty_and_malformed():
    assert parse_greenhouse({}, "x") == []
    assert parse_lever([], "x") == []
    assert parse_ashby({"jobs": None}, "x") == []
    assert parse_greenhouse({"jobs": [{"id": 1, "title": "No URL"}]}, "x") == []
    bad_date = parse_ashby({"jobs": [{"id": "1", "title": "T", "jobUrl": "u", "publishedAt": "nope"}]}, "x")
    assert bad_date[0].posted_at is None


def test_html_to_text():
    assert html_to_text("") == ""
    assert html_to_text("<p>a&nbsp;b</p><script>evil()</script><p>c</p>") == "a b\n\nc"
    assert html_to_text("<p>Intro</p><ul><li>a</li><li>b</li></ul><p>End</p>") == "Intro\n\n- a\n- b\n\nEnd"


# ---------------------------------------------------------------- filters


@pytest.mark.parametrize(
    "title,target,expected",
    [
        ("Hardware Engineer Intern", "Hardware Engineer Intern", True),
        ("Intern, Hardware Engineering (Summer 2027)", "Hardware Engineer Intern", True),
        ("Hardware Engineering Internship", "Hardware Engineer Intern", True),
        ("Electrical Engineer Intern", "Electrical Engineering Intern", True),
        ("Software Engineer Intern", "Embedded Software Intern", False),
        ("Internal Tools Hardware Engineer", "Hardware Engineer Intern", False),
        ("Hardware Engineer", "Hardware Engineer Intern", False),
        ("Anything", "of the", False),
    ],
)
def test_title_matches(title, target, expected):
    assert title_matches(title, target) is expected


def test_filter_jobs_on_fixtures():
    jobs = (
        parse_greenhouse(fixture("greenhouse.json"), "acme-robotics")
        + parse_lever(fixture("lever.json"), "widgetco")
        + parse_ashby(fixture("ashby.json"), "sparkhw")
    )
    kept = {j.id for j in filter_jobs(jobs, TARGET, NOW)}
    assert kept == {
        "greenhouse:acme-robotics:1001",  # TX, recent
        "lever:widgetco:aaaa-1111",  # CA
        "lever:widgetco:aaaa-2222",  # remote via workplaceType, "Internship"
        "ashby:sparkhw:0f1e2d3c-0001",  # NC
        "ashby:sparkhw:0f1e2d3c-0003",  # remote, unknown date kept
    }


def test_filter_exclude_words_are_whole_word_case_insensitive():
    t = Target(titles=["Engineer Intern"], exclude_title_words=["Senior", "Sr"])
    assert filter_jobs([job(title="SENIOR Engineer Intern")], t, NOW) == []
    assert filter_jobs([job(title="Sr. Engineer Intern")], t, NOW) == []
    assert len(filter_jobs([job(title="Engineer Intern, Srinagar")], t, NOW)) == 1


def test_filter_location_and_dates():
    t = Target(titles=[], locations=["NC", "Remote"], posted_within_days=7)
    assert filter_jobs([job(location="Lincoln, NE")], t, NOW) == []  # "nc" inside a word does not match
    assert len(filter_jobs([job(location="Raleigh, NC")], t, NOW)) == 1
    assert len(filter_jobs([job(location="Anywhere", remote=True)], t, NOW)) == 1
    assert len(filter_jobs([job(location="")], t, NOW)) == 1  # unknown location kept
    old = datetime(2026, 10, 1, tzinfo=timezone.utc)
    assert filter_jobs([job(location="Raleigh, NC", posted_at=old)], t, NOW) == []
    # naive datetimes are treated as UTC rather than crashing
    assert len(filter_jobs([job(location="NC", posted_at=datetime(2026, 11, 4))], t, NOW.replace(tzinfo=None))) == 1
    # Remote not listed: remote flag alone is not enough
    assert filter_jobs([job(location="Anywhere", remote=True)], Target(locations=["TX"]), NOW) == []


@pytest.mark.parametrize(
    "location,targets,expected",
    [
        ("San Francisco, California", ["CA"], True),
        ("San Francisco, CA", ["California"], True),
        ("Raleigh, North Carolina", ["nc"], True),
        ("Austin, TX", ["Texas"], True),
        ("Portland, Oregon", ["OR"], True),
        ("Remote or Hybrid", ["Oregon"], False),  # full name -> code only matches an upper-case code
        ("Indianapolis", ["IN"], False),  # whole words only
        ("Carolina Beach", ["NC"], False),  # "North Carolina" required, not just "Carolina"
        ("New York, NY", ["CA", "TX"], False),
    ],
)
def test_location_state_aliases(location, targets, expected):
    assert location_matches(job(location=location), targets) is expected


def test_empty_target_keeps_everything():
    assert len(filter_jobs([job(title="Chef")], Target(posted_within_days=10_000), NOW)) == 1


# ---------------------------------------------------------------- dedupe / storage


def test_dedupe_by_url():
    a = job(id="greenhouse:acme:1", url="https://example.com/jobs/1")
    b = job(id="lever:acme:x", url="https://EXAMPLE.com/jobs/1/")
    c = job(id="greenhouse:acme:2", url="https://example.com/jobs/2")
    assert [j.id for j in dedupe([a, b, c])] == ["greenhouse:acme:1", "greenhouse:acme:2"]


def test_save_and_load_jobs_appends_only_new(tmp_path: Path):
    path = tmp_path / "data" / "jobs.jsonl"
    assert load_jobs(path) == []
    a, b = job(id="a", url="https://x/a"), job(id="b", url="https://x/b")
    assert save_jobs([a], path) == [a]
    assert save_jobs([a, b, b], path) == [b]
    assert save_jobs([a, b], path) == []
    assert load_jobs(path) == [a, b]
    assert len(path.read_text().splitlines()) == 2


# ---------------------------------------------------------------- fetchers


def test_fetchers_hit_public_endpoints():
    client = mock_client(all_fixture_routes())
    assert len(fetch_greenhouse(client, "acme")) == 4
    assert len(fetch_lever(client, "acme")) == 4
    assert len(fetch_ashby(client, "acme")) == 3
    urls = [str(r.url) for r in client.seen]
    assert urls == [
        "https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=true",
        "https://api.lever.co/v0/postings/acme?mode=json",
        "https://api.ashbyhq.com/posting-api/job-board/acme?includeCompensation=false",
    ]


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(404),
        httpx.Response(500),
        httpx.Response(200, text="<html>not json</html>"),
        httpx.ReadTimeout("timed out"),
        httpx.ConnectError("no route"),
    ],
)
def test_failing_board_is_skipped_with_warning(response, caplog):
    client = mock_client({"boards-api.greenhouse.io": response})
    with caplog.at_level(logging.WARNING, logger="autoapply.discover"):
        assert fetch_greenhouse(client, "broken") == []
    assert "broken" in caplog.text


# ---------------------------------------------------------------- orchestrator + CLI


def companies() -> Companies:
    return Companies(
        greenhouse=[CompanyBoard(board="acme-robotics"), CompanyBoard(board="gone")],
        lever=[CompanyBoard(board="widgetco")],
        ashby=[CompanyBoard(board="sparkhw")],
    )


def profile() -> Profile:
    return Profile(name="Test Person", email="test@example.com", target=TARGET)


def test_discover_end_to_end_skips_failing_board_and_known_jobs():
    routes = all_fixture_routes()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/v1/boards/gone/"):
            return httpx.Response(404)
        return routes[request.url.host]

    client = httpx.Client(transport=httpx.MockTransport(handler))
    found = discover(companies(), profile(), client, NOW)
    assert len(found) == 5
    again = discover(companies(), profile(), client, NOW, known=found[:2])
    assert {j.id for j in again} == {j.id for j in found[2:]}
    assert len(discover(companies(), profile(), client, NOW, apply_filters=False)) == 11


def test_cli_discover(tmp_path: Path, monkeypatch):
    out = tmp_path / "data" / "jobs.jsonl"
    monkeypatch.setattr(paths, "JOBS_FILE", out)
    monkeypatch.setattr(disc, "make_client", lambda: mock_client(all_fixture_routes()))
    runner = CliRunner()

    dry = runner.invoke(app, ["discover", "--all", "--dry-run"])
    assert dry.exit_code == 0, dry.output
    assert "11 new job(s) (dry run" in dry.output and not out.exists()

    # --all is preview-only even without --dry-run: unfiltered postings are never saved.
    everything = runner.invoke(app, ["discover", "--all"])
    assert everything.exit_code == 0, everything.output
    assert "11 new job(s) (dry run, not saved)" in everything.output
    assert "--all is preview-only" in everything.output and not out.exists()

    # Without --all, only filtered jobs are saved. The CLI uses the real clock, so only assert
    # date-independent facts: the remote posting with no date always passes, the excluded ones never do.
    first = runner.invoke(app, ["discover"])
    assert first.exit_code == 0, first.output
    saved = load_jobs(out)
    assert f"{len(saved)} new job(s) saved" in first.output
    ids = {j.id for j in saved}
    assert "ashby:example-company:0f1e2d3c-0003" in ids
    assert not ids & {"lever:example-company:aaaa-3333", "ashby:example-company:0f1e2d3c-0002"}
    names = {j.id.split(":")[0]: j.company for j in saved}
    assert names["ashby"] == "Example Company"  # derived from the slug (example config has no name)

    second = runner.invoke(app, ["discover"])
    assert "0 new job(s)" in second.output
    assert len(load_jobs(out)) == len(saved)
