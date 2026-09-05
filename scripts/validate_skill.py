#!/usr/bin/env python3
"""Small dependency-free validation for the distributable skill package."""

from __future__ import annotations

import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def frontmatter(markdown: str) -> dict[str, str]:
    """Return simple scalar YAML frontmatter used by SKILL.md."""
    if not markdown.startswith("---\n"):
        raise ValueError("SKILL.md must start with YAML frontmatter")
    try:
        raw = markdown.split("---\n", 2)[1]
    except IndexError as exc:
        raise ValueError("SKILL.md frontmatter is not closed") from exc
    values: dict[str, str] = {}
    for line in raw.splitlines():
        key, separator, value = line.partition(":")
        if separator:
            values[key.strip()] = value.strip()
    return values


def validate() -> list[str]:
    """Return human-readable validation errors; an empty list means valid."""
    errors: list[str] = []
    skill_path = ROOT / "SKILL.md"
    metadata_path = ROOT / "agents" / "openai.yaml"
    required = [skill_path, metadata_path, ROOT / "runjob.py", ROOT / "README.md"]
    for path in required:
        if not path.is_file():
            errors.append(f"missing required file: {path.relative_to(ROOT)}")

    if errors:
        return errors

    skill = skill_path.read_text(encoding="utf-8")
    try:
        metadata = frontmatter(skill)
    except ValueError as exc:
        return [str(exc)]

    if metadata.get("name") != "jobs":
        errors.append("frontmatter name must be 'jobs'")
    description = metadata.get("description", "")
    if not description:
        errors.append("frontmatter description is required")
    if len(description) > 1024:
        errors.append("frontmatter description is too long")
    if re.search(r"\b(TODO|PLACEHOLDER)\b", skill, re.IGNORECASE):
        errors.append("SKILL.md contains an unfinished placeholder")

    openai_yaml = metadata_path.read_text(encoding="utf-8")
    if "$jobs" not in openai_yaml:
        errors.append("agents/openai.yaml default_prompt must mention $jobs")
    return errors


def main() -> int:
    """Validate and print one stable result."""
    errors = validate()
    if errors:
        for error in errors:
            print(f"error: {error}", file=sys.stderr)
        return 1
    print("skill package valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
