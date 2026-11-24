"""Where run directories can be under an output root.

Two shapes, both relative to the root, which is what ``run_dir_name`` holds:
directly under it (``VC_6m_01__p01__045876c3``), as the app has always written
them, and inside an expedition tree (``VC_6m_01/pass_01_control/fwd/RESULTS/Cam1__r01``),
beside the clips they were made from. Only these two shapes are looked at, so a
scan never walks the raw footage.
"""

from __future__ import annotations

from pathlib import Path

MANIFEST = "run_manifest.json"
_NESTED = "*/*/*/RESULTS/*"


def run_dir_candidates(out_root: Path) -> list[Path]:
    """Every directory that may be a run, flat ones first."""
    if not out_root.is_dir():
        return []
    try:
        flat = [p for p in out_root.iterdir() if p.is_dir()]
        nested = [p for p in out_root.glob(_NESTED) if p.is_dir()]
    except OSError:
        return []
    return flat + sorted(nested)


def manifest_paths(out_root: Path) -> list[Path]:
    """Every run manifest in either shape."""
    if not out_root.is_dir():
        return []
    try:
        return sorted(out_root.glob(f"*/{MANIFEST}")) + sorted(out_root.glob(f"{_NESTED}/{MANIFEST}"))
    except OSError:
        return []


def run_dir_name(out_root: Path, run_dir: Path) -> str:
    """The name a run is recorded under: its path relative to the root, with ``/``."""
    try:
        return run_dir.relative_to(out_root).as_posix()
    except ValueError:
        return run_dir.name
