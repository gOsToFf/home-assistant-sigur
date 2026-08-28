#!/usr/bin/env python3
"""Derive the release metadata for a pushed tag.

HACS reads GitHub *releases*, not tags, and it treats a release flagged as a
pre-release as a beta that only users who opted in ever see. Getting that flag
wrong publishes a beta to everyone, so it is decided here from the version
itself rather than being set by hand.

Run from the repository root; with ``--github-output`` it writes the values the
release workflow consumes.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import sys

MANIFEST = Path("custom_components/sigur/manifest.json")
CHANGELOG = Path("CHANGELOG.md")

#: A version is a pre-release when a marker follows the numeric core, in either
#: the ``1.2.0b1`` style Home Assistant itself uses or the ``1.2.0-beta.1``
#: style of semantic versioning.
_PRERELEASE = re.compile(r"\d[-._]?(?:a|b|c|rc|alpha|beta|dev|pre)\d*", re.IGNORECASE)


class ReleaseError(Exception):
    """The tag and the repository disagree about what is being released."""


def version_from_tag(tag: str) -> str:
    """Strip the leading ``v`` from a release tag."""
    return tag[1:] if tag.startswith("v") else tag


def is_prerelease(version: str) -> bool:
    """Whether ``version`` names a pre-release."""
    return bool(_PRERELEASE.search(version))


def manifest_version(manifest: Path = MANIFEST) -> str:
    """Read the integration version out of ``manifest.json``."""
    return str(json.loads(manifest.read_text(encoding="utf-8"))["version"])


def changelog_section(version: str, changelog: Path = CHANGELOG) -> str | None:
    """Return the changelog entry for ``version``.

    Returns:
        The section body without its heading, or ``None`` if the changelog has
        no entry for this version.

    """
    text = changelog.read_text(encoding="utf-8")
    pattern = re.compile(
        rf"^## \[{re.escape(version)}\].*?$(?P<body>.*?)(?=^## \[|\Z)",
        re.MULTILINE | re.DOTALL,
    )
    match = pattern.search(text)
    if match is None:
        return None
    # Trailing link definitions belong to the document, not to this release.
    body = re.sub(r"^\[[^\]]+\]:.*$", "", match.group("body"), flags=re.MULTILINE)
    return body.strip() or None


def build(tag: str) -> tuple[str, bool, str]:
    """Validate ``tag`` and build the release notes.

    Returns:
        ``(version, prerelease, notes)``.

    Raises:
        ReleaseError: if the tag disagrees with ``manifest.json``, or the
            changelog has no entry for the version.

    """
    version = version_from_tag(tag)
    declared = manifest_version()
    if version != declared:
        raise ReleaseError(
            f"tag {tag!r} releases version {version!r}, but "
            f"manifest.json declares {declared!r}"
        )
    notes = changelog_section(version)
    if notes is None:
        raise ReleaseError(f"CHANGELOG.md has no '## [{version}]' section")
    return version, is_prerelease(version), notes


def _write_output(path: Path, version: str, prerelease: bool, notes: str) -> None:
    """Append the values to a GitHub Actions output file."""
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"version={version}\n")
        handle.write(f"prerelease={'true' if prerelease else 'false'}\n")
        # Multi-line values need a delimiter that cannot occur in the body.
        handle.write("notes<<RELEASE_NOTES_EOF\n")
        handle.write(notes.rstrip() + "\n")
        handle.write("RELEASE_NOTES_EOF\n")


def main() -> int:
    """Entry point."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tag", help="the release tag, for example v0.1.0")
    parser.add_argument(
        "--github-output",
        action="store_true",
        help="append the values to $GITHUB_OUTPUT",
    )
    args = parser.parse_args()

    try:
        version, prerelease, notes = build(args.tag)
    except ReleaseError as err:
        print(f"error: {err}", file=sys.stderr)
        return 1

    print(f"version:    {version}")
    print(f"prerelease: {prerelease}")
    print("notes:")
    print(notes)

    if args.github_output:
        output = os.environ.get("GITHUB_OUTPUT")
        if not output:
            print("error: GITHUB_OUTPUT is not set", file=sys.stderr)
            return 1
        _write_output(Path(output), version, prerelease, notes)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
