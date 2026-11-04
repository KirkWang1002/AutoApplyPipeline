import typer

app = typer.Typer(help="Job application pipeline: discover -> score -> tailor -> prefill -> track.")


@app.command()
def discover() -> None:
    """Fetch new postings from configured job boards (milestone M2)."""
    typer.echo("discover: not implemented yet, see docs/ROADMAP.md M2")


if __name__ == "__main__":
    app()
