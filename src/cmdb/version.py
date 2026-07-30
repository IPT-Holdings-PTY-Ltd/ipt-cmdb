"""Validated application release metadata shared by API and worker processes."""

from __future__ import annotations

import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
VERSION_FILE = ROOT / "VERSION"
_SEMVER = re.compile(
    r"^(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
_COMMIT = re.compile(r"^[0-9a-fA-F]{7,64}$")
_IMAGE_DIGEST = re.compile(r"^sha256:[0-9a-fA-F]{64}$")


def _packaged_version() -> str:
    """Return the checked-in semantic version without trusting arbitrary file content."""

    try:
        candidate = VERSION_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return "0.0.0"
    return candidate if _SEMVER.fullmatch(candidate) else "0.0.0"


APPLICATION_VERSION = _packaged_version()


@dataclass(frozen=True)
class ReleaseMetadata:
    """Describe the running artifact using bounded, publicly safe identifiers."""

    application_version: str
    source_commit: str
    image_digest: str

    def public(self) -> dict[str, str]:
        """Return the API-safe camel-case representation."""

        values = asdict(self)
        return {
            "applicationVersion": values["application_version"],
            "sourceCommit": values["source_commit"],
            "imageDigest": values["image_digest"],
        }


def release_metadata() -> ReleaseMetadata:
    """Read validated build/deployment metadata, falling back to packaged values."""

    configured_version = os.getenv("CMDB_VERSION", "").strip()
    version = (
        configured_version
        if _SEMVER.fullmatch(configured_version) and len(configured_version) <= 80
        else APPLICATION_VERSION
    )
    configured_commit = os.getenv("CMDB_SOURCE_COMMIT", "").strip()
    source_commit = (
        configured_commit.lower()
        if _COMMIT.fullmatch(configured_commit) and len(configured_commit) <= 64
        else "unknown"
    )
    configured_digest = os.getenv("CMDB_IMAGE_DIGEST", "").strip()
    image_digest = (
        configured_digest.lower() if _IMAGE_DIGEST.fullmatch(configured_digest) else "unknown"
    )
    return ReleaseMetadata(version, source_commit, image_digest)
