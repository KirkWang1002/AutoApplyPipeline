"""Core data models shared by every pipeline stage."""
from __future__ import annotations

from datetime import date, datetime
from enum import Enum

from pydantic import BaseModel, Field


class Job(BaseModel):
    """A single job posting, normalized across ATS sources."""

    id: str  # stable id, e.g. "greenhouse:anthropic:12345"
    source: str  # "greenhouse" | "lever" | "ashby"
    company: str
    title: str
    url: str
    location: str = ""
    remote: bool = False
    posted_at: datetime | None = None
    description: str = ""


class Education(BaseModel):
    school: str
    degree: str
    grad: str = ""


class Target(BaseModel):
    titles: list[str] = Field(default_factory=list)
    locations: list[str] = Field(default_factory=list)
    exclude_title_words: list[str] = Field(default_factory=list)
    posted_within_days: int = 14


class Profile(BaseModel):
    """The candidate. Loaded from config/profile.yaml."""

    name: str
    email: str
    phone: str = ""
    location: str = ""
    links: dict[str, str] = Field(default_factory=dict)
    education: list[Education] = Field(default_factory=list)
    work_authorization: str = ""
    target: Target = Field(default_factory=Target)
    skills: list[str] = Field(default_factory=list)
    min_score_to_tailor: int = Field(default=4, ge=1, le=5)


class ScoreResult(BaseModel):
    """Claude's fit assessment for one job."""

    job_id: str
    score: int = Field(ge=1, le=5)
    matched_skills: list[str] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
    reason: str = ""


class ApplicationStatus(str, Enum):
    found = "found"
    scored = "scored"
    drafted = "drafted"
    applied = "applied"
    rejected = "rejected"
    interview = "interview"
    offer = "offer"


class ApplicationRecord(BaseModel):
    """One row of data/applications.csv."""

    job_id: str
    company: str
    title: str
    url: str
    status: ApplicationStatus = ApplicationStatus.found
    score: int | None = None
    updated: date = Field(default_factory=date.today)
    notes: str = ""


def board_display_name(board: str) -> str:
    """Human-readable company name derived from an ATS board slug ("example-company" -> "Example Company")."""
    return board.replace("-", " ").replace("_", " ").title()


class CompanyBoard(BaseModel):
    board: str  # ATS slug, used in API URLs and Job.id
    name: str | None = None  # display name for cover letters etc.; derived from `board` if unset

    @property
    def display_name(self) -> str:
        return (self.name or "").strip() or board_display_name(self.board)


class Companies(BaseModel):
    """Loaded from config/companies.yaml: ATS board slugs to watch."""

    greenhouse: list[CompanyBoard] = Field(default_factory=list)
    lever: list[CompanyBoard] = Field(default_factory=list)
    ashby: list[CompanyBoard] = Field(default_factory=list)
