from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

from gemini_translator.version import APP_VERSION, __version__


RELEASE_NOTES_PATTERN = re.compile(r"^RELEASE_NOTES_v(?P<version>\d+\.\d+\.\d+)\.md$")
RELEASE_VERSION_PATTERN = re.compile(r"^\d+\.\d+\.\d+$")


def _version_tuple(value: str) -> tuple[int, ...]:
    if value.strip().lower() == "dev":
        return (999, 999, 999)
    base_version = str(value).strip().removeprefix("v").split("-", 1)[0]
    parts = base_version.split(".")
    if len(parts) != 3:
        raise ValueError(f"Expected semantic version with three parts, got: {value!r}")
    return tuple(int(part) for part in parts)


def _release_note_versions(project_root: Path) -> list[tuple[int, ...]]:
    versions = []
    for path in project_root.glob("RELEASE_NOTES_v*.md"):
        match = RELEASE_NOTES_PATTERN.match(path.name)
        if match:
            versions.append(_version_tuple(match.group("version")))
    return sorted(versions)


def _is_release_mode_env_enabled() -> bool:
    value = (
        os.environ.get("GT_RELEASE_METADATA_MODE")
        or os.environ.get("GT_RELEASE_METADATA_STRICT")
        or ""
    ).strip().lower()
    return value in {"1", "true", "yes", "release", "strict"}


def check_release_metadata(project_root: Path | None = None, *, release: bool = False) -> list[str]:
    root = project_root or Path(__file__).resolve().parents[2]
    errors = []

    if APP_VERSION != f"V {__version__}":
        errors.append(
            f"APP_VERSION mismatch: expected 'V {__version__}', got {APP_VERSION!r}"
        )

    release_version = RELEASE_VERSION_PATTERN.fullmatch(str(__version__).strip()) is not None
    current_version = _version_tuple(__version__)

    release_versions = _release_note_versions(root)
    if not release_versions:
        errors.append("No RELEASE_NOTES_v*.md files found.")
        return errors

    latest_release_version = max(release_versions)
    if current_version < latest_release_version:
        latest = ".".join(str(part) for part in latest_release_version)
        errors.append(
            f"Application version {__version__!r} is older than latest release notes v{latest}."
        )

    if release:
        if not release_version:
            errors.append(
                f"Release mode requires a final semantic version, got {__version__!r}."
            )
        if current_version not in release_versions:
            current = ".".join(str(part) for part in current_version)
            errors.append(f"Missing release notes for release version v{current}.")

    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate release/version metadata.")
    parser.add_argument(
        "--release",
        action="store_true",
        help="Enable strict release checks: final semver and matching release notes.",
    )
    args = parser.parse_args(argv)

    release = args.release or _is_release_mode_env_enabled()
    errors = check_release_metadata(release=release)
    if errors:
        for error in errors:
            print(f"[release-metadata] {error}", file=sys.stderr)
        return 1
    mode = "release" if release else "dev"
    print(f"[release-metadata] OK ({mode}): {APP_VERSION}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
