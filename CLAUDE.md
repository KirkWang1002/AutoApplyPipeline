# CLAUDE.md

Guidance for Claude Code sessions (local or cloud) working in this repo.

## Project

Human-in-the-loop job application pipeline: discover -> score -> tailor -> prefill -> track.
Read README.md for the stage overview and docs/ROADMAP.md for which milestone is next.

## Stack

- Python 3.11+, package in `src/autoapply/`, CLI entry point `autoapply` (Typer).
- HTTP: `httpx`. Config: YAML via `pyyaml`. Data models: `pydantic`.
- LLM calls: official `anthropic` SDK, model id from env `AUTOAPPLY_MODEL` (default `claude-sonnet-5-5`).
- Browser prefill: `playwright` (only runs on the owner's machine, never in cloud).
- Tests: `pytest`, in `tests/`. Mock all network and LLM calls in tests.

## Rules

- NEVER write code that clicks a final Submit / Apply button. Prefill must stop and hand control to the human.
- Only public endpoints (e.g. `boards-api.greenhouse.io`, `api.lever.co`, `api.ashbyhq.com` posting APIs). No logged-in scraping, no CAPTCHA solving.
- Never commit `.env`, `config/profile.yaml`, `config/companies.yaml`, `resume/`, or `data/`. Use the `*.example.*` files for samples and fake data in tests.
- Keep each stage a separate module with a pure-ish core function that tests can call without network.
- Run `pytest` before committing. Keep commits small with clear messages.

## Working in a cloud session

- The cloud VM has no access to the owner's browser, accounts, or local files; only this repo.
- No real `.env` there: code must not crash at import time without `ANTHROPIC_API_KEY`; tests must pass without it.
- When a milestone is done, tick it in docs/ROADMAP.md, commit, push the branch, and open a PR.
