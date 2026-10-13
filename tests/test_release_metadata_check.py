from pathlib import Path

from gemini_translator.scripts import check_release_metadata as release_metadata
from gemini_translator.scripts.check_release_metadata import check_release_metadata


def test_release_metadata_check_accepts_current_project():
    assert check_release_metadata() == []


def test_release_metadata_check_reports_missing_release_notes(tmp_path: Path):
    errors = check_release_metadata(tmp_path)

    assert errors == ["No RELEASE_NOTES_v*.md files found."]


def test_release_metadata_release_mode_rejects_dev_version(monkeypatch, tmp_path: Path):
    (tmp_path / "RELEASE_NOTES_v1.2.3.md").write_text("# v1.2.3\n", encoding="utf-8")
    monkeypatch.setattr(release_metadata, "__version__", "1.2.3-dev")
    monkeypatch.setattr(release_metadata, "APP_VERSION", "V 1.2.3-dev")

    errors = check_release_metadata(tmp_path, release=True)

    assert "Release mode requires a final semantic version, got '1.2.3-dev'." in errors


def test_release_metadata_release_mode_requires_matching_release_notes(monkeypatch, tmp_path: Path):
    (tmp_path / "RELEASE_NOTES_v1.2.2.md").write_text("# v1.2.2\n", encoding="utf-8")
    monkeypatch.setattr(release_metadata, "__version__", "1.2.3")
    monkeypatch.setattr(release_metadata, "APP_VERSION", "V 1.2.3")

    errors = check_release_metadata(tmp_path, release=True)

    assert errors == ["Missing release notes for release version v1.2.3."]
