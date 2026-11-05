# AutoApplyPipeline

A personal, human-in-the-loop job application pipeline.

It finds postings, scores how well each one fits my background, drafts a tailored
resume and cover letter, and pre-fills the application form. **I review and click
Submit myself** — the pipeline never submits on its own.

## Pipeline stages

```
1. discover  ->  2. score  ->  3. tailor  ->  4. prefill  ->  (me: review + submit)  ->  5. track
```

| Stage | What it does | Where it runs |
|-------|--------------|---------------|
| discover | Pull postings from public ATS job-board APIs (Greenhouse, Lever, Ashby) for companies in `config/companies.yaml` | anywhere (cloud OK) |
| score | Ask Claude to rate fit 1-5 against `config/profile.yaml`, with reasons | anywhere (cloud OK) |
| tailor | Generate a tailored resume (from my master resume) + cover letter for jobs scored >= threshold | anywhere (cloud OK) |
| prefill | Open the application page in a browser (Playwright) and fill fields, then **stop** | **my own computer only** |
| track | Record status in `data/applications.csv` (found / scored / drafted / applied / rejected / interview) | anywhere |

See [docs/ROADMAP.md](docs/ROADMAP.md) for the build plan, one milestone per Claude Code session.

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -e ".[dev]"
copy config\profile.example.yaml config\profile.yaml
copy config\companies.example.yaml config\companies.yaml
```

Put your Anthropic API key in `.env` (see `.env.example`). `.env`, `config/profile.yaml`,
`resume/` and `data/` are git-ignored so personal data never reaches GitHub.

Put your master resume at `resume/master.md` (or `master.tex`). Tailoring only reorders,
rephrases and selects from it.

## Daily use

```bash
autoapply config                 # check profile + companies loaded
autoapply discover               # new matching postings -> data/jobs.jsonl
autoapply discover --all         # preview everything, unfiltered (not saved)
autoapply score --min 4          # Claude rates fit 1-5 (cached; re-runs are free)
autoapply tailor                 # resume.md + cover_letter.md per good job -> out/<company>-<role>/
autoapply report                 # top jobs not applied yet + pending
autoapply status <job_id> applied --notes "referral from X"
```

Read every file in `out/` before using it, and check `REVIEW_FLAGS.txt` if one was
written: it lists numbers / names in the draft that are not in your master resume.

Run tests with `python -m pytest`.

## Rules

- Never auto-submit. Prefill stops before the final Submit button.
- Only use public job-board APIs or pages; no scraping behind logins, no CAPTCHA bypassing.
- Never commit personal data, resumes, or API keys.


Owner: Kehan