"""Load config/profile.yaml and config/companies.yaml (falling back to the .example files)."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from autoapply.models import Companies, Profile

CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"


def _resolve(name: str, config_dir: Path) -> Path:
    real = config_dir / f"{name}.yaml"
    if real.exists():
        return real
    example = config_dir / f"{name}.example.yaml"
    if example.exists():
        return example
    raise FileNotFoundError(f"Neither {real} nor {example} exists")


def _load_yaml(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a YAML mapping at the top level")
    return data


def load_profile(config_dir: Path = CONFIG_DIR) -> Profile:
    return Profile.model_validate(_load_yaml(_resolve("profile", config_dir)))


def load_companies(config_dir: Path = CONFIG_DIR) -> Companies:
    data = _load_yaml(_resolve("companies", config_dir))
    # Allow `greenhouse:` with no entries (YAML null).
    return Companies.model_validate({k: v or [] for k, v in data.items()})
