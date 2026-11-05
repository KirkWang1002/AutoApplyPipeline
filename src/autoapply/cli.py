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


if __name__ == "__main__":
    app()
