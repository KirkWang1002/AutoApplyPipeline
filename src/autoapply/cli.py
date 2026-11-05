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
def discover() -> None:
    """Fetch new postings from configured job boards (milestone M2)."""
    typer.echo("discover: not implemented yet, see docs/ROADMAP.md M2")


@app.command()
def score(
    min_score: int = typer.Option(4, "--min", min=1, max=5, help="Only show jobs scoring at least this."),
) -> None:
    """Score discovered jobs with Claude (cached) and list the good fits (milestone M3)."""
    from autoapply import llm, paths
    from autoapply import score as scoring

    jobs = scoring.read_jobs(paths.JOBS_FILE)
    if not jobs:
        typer.echo(f"No jobs found in {paths.JOBS_FILE}. Run `autoapply discover` first.")
        raise typer.Exit(0)
    profile = load_profile()
    todo = scoring.uncached(jobs, scoring.load_cache(paths.SCORES_FILE))
    client = llm.get_client() if todo else None
    typer.echo(f"{len(jobs)} jobs, {len(jobs) - len(todo)} cached, scoring {len(todo)}...", err=True)
    results = scoring.score_jobs(jobs, profile, client, paths.SCORES_FILE)

    by_id = {j.id: j for j in jobs}
    hits = [r for r in results if r.score >= min_score]
    hits.sort(key=lambda r: (-r.score, by_id[r.job_id].company.lower(), by_id[r.job_id].title.lower()))
    if not hits:
        typer.echo(f"No jobs scored >= {min_score}.")
        return
    for r in hits:
        j = by_id[r.job_id]
        typer.echo(f"[{r.score}] {j.company} | {j.title}")
        typer.echo(f"    {r.reason}")
        typer.echo(f"    {j.url}")


@app.command()
def status(
    job_id: str = typer.Argument(..., help="Job id, e.g. greenhouse:acme:123"),
    new_status: str = typer.Argument(..., metavar="STATUS", help="found|scored|drafted|applied|interview|rejected|offer"),
    notes: str | None = typer.Option(None, "--notes", help="Free-text note stored with the record."),
) -> None:
    """Set the status of a tracked application in data/applications.csv (M5)."""
    from autoapply import track
    from autoapply.models import ApplicationStatus

    try:
        parsed = ApplicationStatus(new_status.lower())
    except ValueError:
        choices = ", ".join(s.value for s in ApplicationStatus)
        typer.echo(f"Invalid status {new_status!r}. Choose one of: {choices}", err=True)
        raise typer.Exit(2)
    records = track.sync_from_files()
    try:
        rec = track.set_status(records, job_id, parsed, notes=notes)
    except KeyError as e:
        typer.echo(e.args[0], err=True)
        raise typer.Exit(1)
    track.save_applications(records)
    typer.echo(f"{rec.job_id}: {rec.status.value} ({rec.company} - {rec.title})")


@app.command()
def report() -> None:
    """Print top-scored jobs not yet applied and pending applications (M5)."""
    from datetime import date

    from autoapply import track

    records = track.sync_from_files()
    typer.echo(track.build_report(records, date.today()), nl=False)


@app.command()
def tailor(
    min_score: int = typer.Option(
        None, "--min", min=1, max=5, help="Minimum score to tailor (default: profile.min_score_to_tailor)."
    ),
    job_id: str = typer.Option(None, "--job-id", help="Tailor only this job id (ignores scores)."),
    force: bool = typer.Option(False, "--force", help="Regenerate even if output already exists."),
) -> None:
    """Write a tailored resume.md + cover_letter.md per job into out/ (milestone M4)."""
    from autoapply import llm, paths
    from autoapply import tailor as t

    profile = load_profile()
    jobs = t.load_jobs(paths.JOBS_FILE)
    if job_id:
        todo = [j for j in jobs if j.id == job_id][:1]
        if not todo:
            typer.echo(f"Job {job_id!r} not found in {paths.JOBS_FILE}", err=True)
            raise typer.Exit(1)
    else:
        threshold = min_score if min_score is not None else profile.min_score_to_tailor
        todo = t.select_jobs_to_tailor(jobs, t.load_scores(paths.SCORES_FILE), threshold)
        typer.echo(f"{len(todo)} job(s) scored >= {threshold}.")

    pending = []
    for job in todo:
        dest = t.output_dir(job, paths.OUT_DIR)
        if t.is_tailored(dest) and not force:
            typer.echo(f"skip (exists): {dest}")
        else:
            pending.append(job)
    if not pending:
        typer.echo("Nothing to tailor.")
        return

    try:
        master = t.load_master_resume(paths.RESUME_DIR)
        client = llm.get_client()
    except (FileNotFoundError, ValueError, RuntimeError) as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1)

    failed = 0
    for job in pending:
        try:
            dest = t.tailor_job(job, profile, master, client, paths.OUT_DIR)
            typer.echo(f"wrote: {dest}")
        except Exception as e:  # keep going; one bad job shouldn't stop the batch
            failed += 1
            typer.echo(f"failed: {job.id}: {e}", err=True)
    if failed:
        raise typer.Exit(1)


if __name__ == "__main__":
    app()
