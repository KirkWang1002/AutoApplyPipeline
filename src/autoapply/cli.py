"""Human-in-the-loop job application pipeline."""
import typer

from autoapply.config import load_companies, load_profile

app = typer.Typer(
    help="Job application pipeline: discover -> score -> tailor -> prefill -> track.",
    no_args_is_help=True,
)


@app.command()
def config() -> None:
    """Show the loaded profile and watched companies."""
    profile = load_profile()
    companies = load_companies()
    typer.echo(f"Profile: {profile.name} <{profile.email}>")
    typer.echo(f"Target titles: {', '.join(profile.target.titles)}")
    for source in ("greenhouse", "lever", "ashby"):
        boards = [c.board for c in getattr(companies, source)]
        typer.echo(f"{source}: {', '.join(boards) or '-'}")


@app.command()
def discover(
    all_jobs: bool = typer.Option(False, "--all", help="Skip title/location/date filters."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print new jobs without saving them."),
) -> None:
    """Fetch new postings from configured job boards and append them to data/jobs.jsonl."""
    import logging
    from datetime import datetime, timezone

    from autoapply import discover as disc

    logging.basicConfig(level=logging.WARNING, format="warning: %(message)s")
    path = disc.JOBS_FILE
    with disc.make_client() as client:
        jobs = disc.discover(
            load_companies(), load_profile(), client, datetime.now(timezone.utc),
            known=disc.load_jobs(path), apply_filters=not all_jobs,
        )
    if not dry_run:
        jobs = disc.save_jobs(jobs, path)
    for job in jobs:
        typer.echo(f"{job.company:<16} {job.title[:60]:<60} {job.location[:30]:<30} {job.url}")
    typer.echo(f"{len(jobs)} new job(s)" + (" (dry run, not saved)" if dry_run else f" saved to {path}"))


if __name__ == "__main__":
    app()
