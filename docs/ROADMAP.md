# Roadmap

Each milestone is sized for one Claude Code session. Paste the **Prompt** line into a
new cloud session (or local one), review the PR it opens, merge, then move to the next.

## M1 — Project skeleton
- [x] `pyproject.toml` deps filled in, `autoapply --help` works, `pytest` runs green.
- [x] Pydantic models: `Job`, `Profile`, `ScoreResult`, `ApplicationRecord`.
- [x] Config loader for `config/profile.yaml` + `config/companies.yaml` (fall back to the `.example` files).

**Prompt:** "Implement milestone M1 in docs/ROADMAP.md. Follow CLAUDE.md. Add tests, make them pass, tick the boxes, and open a PR."

## M2 — Discover
- [x] Fetchers for Greenhouse, Lever, Ashby public job-board APIs, each returning `list[Job]`.
- [x] Filters: title keywords, location / remote, posted-within-N-days, exclude seniority words (Senior, Staff, Principal...).
- [x] Dedupe by URL; save to `data/jobs.jsonl`. CLI: `autoapply discover`.
- [x] Tests use recorded JSON fixtures in `tests/fixtures/`, no live network.

**Prompt:** "Implement milestone M2 in docs/ROADMAP.md ..."

## M3 — Score
- [x] `score_job(job, profile) -> ScoreResult` using Claude with a structured (JSON) output: score 1-5, matched skills, gaps, one-line reason.
- [x] Cache results by job id so re-runs don't re-spend tokens. CLI: `autoapply score --min 4`.
- [x] Tests mock the Anthropic client.

## M4 — Tailor
- [x] Read master resume from `resume/master.md` (or .tex). Produce `out/<company>-<role>/resume.md` and `cover_letter.md`.
- [x] Never invent experience: the prompt may only reorder, rephrase and select from the master resume.
- [ ] Optional: render to PDF.

## M5 — Track + report
- [x] `data/applications.csv` with status column; CLI `autoapply status <job_id> applied`.
- [x] `autoapply report` prints a table of today's top-scored jobs and what's pending.

## M6 — Prefill (local only)
- [ ] Playwright script: open job URL, fill name / email / phone / links / upload resume from profile + `out/` folder.
- [ ] Stops before Submit, leaves the browser open for me to review. Greenhouse + Lever forms first.
