"""Versión vigente de la ontología (leída de ontology/VERSION)."""

from __future__ import annotations

from pathlib import Path

_FALLBACK = "0.1.0"


def _read_version() -> str:
    for base in (Path(__file__).resolve().parents[2], Path.cwd()):
        candidate = base / "ontology" / "VERSION"
        if candidate.exists():
            return candidate.read_text(encoding="utf-8").strip()
    return _FALLBACK


ONTOLOGY_VERSION = _read_version()
