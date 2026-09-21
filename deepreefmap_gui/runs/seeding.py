"""Seed a fresh run dir from a matching prior run's preprocess/mapping cache.

Every GUI run gets its own timestamped output directory, so the library's
always-on resume cache never hits on its own. This links (or copies) the cached
frames/labels/masks plus the preprocess and mapping sidecars from the newest
sibling whose preprocess key matches, letting the orchestrator resume.
"""

from __future__ import annotations

import logging
import os
import shutil
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from deepreefmap.pipeline.resume import (
    CACHE_DIR_NAME,
    STAGE_MAPPING,
    STAGE_PREPROCESS,
    _sidecar_path,
    read_sidecar,
)

logger = logging.getLogger(__name__)

_SEED_DIRS = ("frames", "labels", "masks")


def preprocess_key_for_settings(
    settings: Mapping[str, Any],
    video_paths: list[Path],
    begin_s: float | None,
    end_s: float | None,
) -> str:
    """The library's preprocess cache key for a run built from the run form.

    Takes the ``_collect_run_settings()`` dict rather than reading the widgets,
    so the survey batch worker can key its own seeding from its own thread.
    """
    from deepreefmap.pipeline import resume as resume_mod

    from deepreefmap_gui.io.classes_default import resolve_classes_path

    return resume_mod.preprocess_key(
        video_paths=video_paths,
        fps=settings["fps"],
        begin_s=begin_s,
        end_s=end_s,
        camera_profile_name=settings["camera_profile_name"],
        # Skipping segmentation is its own cache identity, not a missing model.
        segmentation_name=(
            "__skip__" if settings["skip_segmentation"] else settings["segmentation_name"]
        ),
        classes_path=resolve_classes_path(settings["classes_path"]),
        processing_width=settings["processing_width"],
        processing_height=settings["processing_height"],
    )


def seed_from_settings(
    output_dir: Path,
    search_root: Path,
    settings: Mapping[str, Any],
    video_paths: list[Path],
    begin_s: float | None,
    end_s: float | None,
) -> Path | None:
    """Seed ``output_dir`` from a sibling run of the same clips and settings.

    Never raises: a failed seed only costs the preprocess it would have skipped.
    """
    try:
        prep_key = preprocess_key_for_settings(settings, video_paths, begin_s, end_s)
        seeded = seed_run_dir_from_match(
            output_dir, search_root, prep_key, settings["camera_profile_name"]
        )
    except Exception:
        logger.warning("Cache seeding failed; running from scratch", exc_info=True)
        return None
    if seeded is not None:
        logger.info("Seeded cache from %s", seeded)
    return seeded


def seeded_stages(output_dir: Path, seeded: Path | None) -> frozenset[str]:
    """Coarse stages this run will read from cache rather than compute.

    Their durations measure a hard-link, not the work, so they must not be
    folded into the timing profile that seeds fresh runs. Preprocess always
    comes across on a hit; the mapping npz only when the source had a sidecar.
    """
    if seeded is None:
        return frozenset()
    stages = {"preprocess"}
    if (output_dir / "mapping_outputs.npz").is_file():
        stages.add("mapping")
    return frozenset(stages)


def _profile_document(name: str) -> dict | None:
    """The calibration this run will be rectified with, or None where it will not load.

    Read once per seeding: a pull may replace the file mid-scan.
    """
    from deepreefmap_gui.camera.profiles import load_profile, profile_payload

    try:
        return profile_payload(load_profile(name))
    except Exception:
        return None


def _same_calibration(cand: Path, expected: dict, name: str) -> bool:
    """Whether a candidate run was rectified with the calibration this run will use.

    Documents rather than digests: a pull and a run copy serialise the same
    measurement differently. A candidate that recorded none is refused.
    """
    from deepreefmap_gui.camera.profiles import run_profile_document

    held = run_profile_document(cand)
    if held is None:
        logger.debug("Not seeding from %s: it recorded no camera profile", cand.name)
        return False
    if held != expected:
        logger.info(
            "Not seeding from %s: it was rectified with a different %s calibration",
            cand.name,
            name,
        )
        return False
    return True


def _link_or_copy(src: Path, dst: Path) -> None:
    try:
        os.link(src, dst)
    except OSError:  # cross-device, or a filesystem without hard links
        shutil.copy2(src, dst)


def seed_run_dir_from_match(
    output_dir: Path, search_root: Path, prep_key: str, camera_profile_name: str
) -> Path | None:
    """Seed a fresh run dir from the newest sibling with a matching preprocess key.

    The key names the camera profile but not the calibration behind it, so the
    profile document is checked too.
    """
    if read_sidecar(output_dir, STAGE_PREPROCESS) is not None:
        return None
    expected = _profile_document(camera_profile_name)
    if expected is None:
        logger.warning("Not seeding: the camera profile %s did not load", camera_profile_name)
        return None
    try:
        candidates = [d for d in search_root.iterdir() if d.is_dir() and d != output_dir]
    except OSError:
        return None
    matches = []
    for cand in candidates:
        sidecar = read_sidecar(cand, STAGE_PREPROCESS)
        if sidecar is None or sidecar.get("key") != prep_key:
            continue
        if not (cand / "frames").is_dir():
            continue
        if not _same_calibration(cand, expected, camera_profile_name):
            continue
        matches.append((_sidecar_path(cand, STAGE_PREPROCESS).stat().st_mtime, cand))
    if not matches:
        return None
    _, source = max(matches)
    try:
        for dirname in _SEED_DIRS:
            src_dir = source / dirname
            if not src_dir.is_dir():
                continue
            dst_dir = output_dir / dirname
            dst_dir.mkdir(parents=True, exist_ok=True)
            for f in src_dir.iterdir():
                if f.is_file():
                    _link_or_copy(f, dst_dir / f.name)
        (output_dir / CACHE_DIR_NAME).mkdir(parents=True, exist_ok=True)
        _link_or_copy(_sidecar_path(source, STAGE_PREPROCESS), _sidecar_path(output_dir, STAGE_PREPROCESS))
        # The mapping key embeds backend/options/gravity and the orchestrator
        # validates it on load, so carrying the cache across is free.
        mapping_npz = source / "mapping_outputs.npz"
        if mapping_npz.is_file() and read_sidecar(source, STAGE_MAPPING) is not None:
            _link_or_copy(mapping_npz, output_dir / "mapping_outputs.npz")
            _link_or_copy(_sidecar_path(source, STAGE_MAPPING), _sidecar_path(output_dir, STAGE_MAPPING))
    except OSError:
        # Half-seeded dirs are safe: load_prepared_frames treats incomplete
        # artifacts as a miss and the orchestrator recomputes.
        logger.warning("Cache seeding from %s failed midway", source, exc_info=True)
        return None
    return source
