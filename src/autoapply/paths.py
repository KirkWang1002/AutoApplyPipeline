"""Filesystem locations shared by all stages. All are git-ignored except CONFIG_DIR."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"
OUT_DIR = ROOT / "out"
RESUME_DIR = ROOT / "resume"

JOBS_FILE = DATA_DIR / "jobs.jsonl"
SCORES_FILE = DATA_DIR / "scores.jsonl"
APPLICATIONS_FILE = DATA_DIR / "applications.csv"
