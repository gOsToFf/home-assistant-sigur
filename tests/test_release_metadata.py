"""Tests for the release metadata derived from a pushed tag.

These guard the two ways a release can go wrong silently: shipping a beta as a
stable release, and shipping a version the manifest does not declare.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.release_metadata import (
    ReleaseError,
    build,
    changelog_section,
    is_prerelease,
    version_from_tag,
)

CHANGELOG = """# Changelog

Preamble that belongs to no release.

## [Unreleased]

## [0.2.0] - 2026-09-01

### Added

- Something new.

## [0.1.0] - 2026-08-28

First release.

### Added

- Everything else.

[Unreleased]: https://example.com/compare/v0.2.0...HEAD
[0.2.0]: https://example.com/releases/tag/v0.2.0
"""


@pytest.mark.parametrize(
    ("tag", "version"),
    [("v0.1.0", "0.1.0"), ("0.1.0", "0.1.0"), ("v1.2.3b1", "1.2.3b1")],
)
def test_version_from_tag(tag: str, version: str) -> None:
    """The leading ``v`` is optional and stripped."""
    assert version_from_tag(tag) == version


@pytest.mark.parametrize("version", ["0.1.0", "1.0.0", "2026.2.0", "10.20.30", "1.2"])
def test_stable_versions_are_not_prereleases(version: str) -> None:
    """A plain numeric version ships to everyone."""
    assert is_prerelease(version) is False


@pytest.mark.parametrize(
    "version",
    [
        "0.2.0b1",
        "0.2.0b0",
        "1.0.0rc2",
        "1.0.0a1",
        "1.0.0-beta.1",
        "1.0.0-rc.1",
        "1.0.0-alpha",
        "1.0.0.dev1",
        "1.0.0B1",
    ],
)
def test_prerelease_versions_are_detected(version: str) -> None:
    """Both the Home Assistant ``b1`` style and semver ``-beta.1`` count."""
    assert is_prerelease(version) is True


def test_changelog_section_is_extracted(tmp_path: Path) -> None:
    """Only the section for the released version ends up in the notes."""
    path = tmp_path / "CHANGELOG.md"
    path.write_text(CHANGELOG, encoding="utf-8")
    section = changelog_section("0.1.0", path)
    assert section is not None
    assert "First release." in section
    assert "Everything else." in section
    # Neither a neighbouring release nor the preamble leaks in.
    assert "Something new." not in section
    assert "Preamble" not in section


def test_changelog_link_definitions_are_stripped(tmp_path: Path) -> None:
    """Reference links at the foot of the file are not release notes."""
    path = tmp_path / "CHANGELOG.md"
    path.write_text(CHANGELOG, encoding="utf-8")
    section = changelog_section("0.2.0", path)
    assert section is not None
    assert "Something new." in section
    assert "https://example.com" not in section


def test_changelog_section_missing(tmp_path: Path) -> None:
    """An unreleased version has no section, and that is reported."""
    path = tmp_path / "CHANGELOG.md"
    path.write_text(CHANGELOG, encoding="utf-8")
    assert changelog_section("9.9.9", path) is None


def test_build_rejects_a_tag_the_manifest_does_not_declare() -> None:
    """A tag that disagrees with manifest.json fails the release."""
    with pytest.raises(ReleaseError, match=r"manifest\.json declares"):
        build("v99.0.0")


def test_build_accepts_the_declared_version() -> None:
    """The repository's own manifest and changelog agree with each other."""
    from scripts.release_metadata import manifest_version

    version = manifest_version()
    built_version, prerelease, notes = build(f"v{version}")
    assert built_version == version
    assert prerelease is is_prerelease(version)
    assert notes.strip()
