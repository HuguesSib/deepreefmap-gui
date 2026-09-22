"""Comparable run observations and distribution summaries."""

from __future__ import annotations

import json
import math
import uuid
from collections import defaultdict

PARAMETERS = {
    "resolution": ("processing_width", "processing_height"),
    "fps": ("fps",),
    "batch": ("preprocess_batch_size",),
    "models": ("mapping_backend", "segmentation_model", "model_revisions"),
}
SETTING_KEYS = (
    "processing_width",
    "processing_height",
    "fps",
    "preprocess_batch_size",
    "mapping_backend",
    "segmentation_model",
    "mode",
    "mapping_options",
    "enable_tsdf",
    "grid_bins",
    "replacement_radius_factor",
    "replacement_radius_estimation_frames",
    "replacement_radius_override",
    "refine_intrinsics_from_mapper",
    "camera_profile",
    "deepreefmap_version",
    "model_revisions",
    "taxonomy_hash",
    "camera_calibration_id",
)


def observation(manifest: dict, completed: bool) -> dict:
    """Return the run-time settings, hardware and timing eligibility."""
    profile = manifest.get("system_profile") or {}
    gpu = profile.get("gpu") or {}
    provenance = (manifest.get("survey") or {}).get("provenance") or {}
    config = provenance.get("config") or {}
    settings = {key: (manifest.get("performance_settings") or {}).get(key, manifest.get(key)) for key in SETTING_KEYS}
    settings["taxonomy_hash"] = provenance.get("taxonomy_hash", settings["taxonomy_hash"])
    settings["model_revisions"] = provenance.get("model_versions", settings["model_revisions"])
    settings["preset_name"] = config.get("preset_name") or (manifest.get("survey") or {}).get("preset_name")
    settings["preset_version"] = config.get("preset_version")
    settings["preset_hash"] = config.get("preset_hash")
    return {
        "version": 1,
        "id": (manifest.get("performance_observation") or {}).get("id") or str(uuid.uuid4()),
        "settings": settings,
        "hardware": {
            **{key: profile.get(key) for key in ("cpu_logical", "cpu_physical", "total_ram_bytes", "total_swap_bytes")},
            "gpu_name": gpu.get("name"),
            "gpu_kind": gpu.get("kind"),
            "total_vram_bytes": gpu.get("total_vram_bytes"),
        },
        "basis": "process",
        "frames": manifest.get("frames_processed"),
        "timing_complete": completed and not bool(manifest.get("resumed_stages")),
        "status": "completed" if completed else "failed",
        "duration_s": manifest.get("run_duration_s"),
        "recorded_at": manifest.get("run_timestamp"),
    }


def settings_known(meta: dict) -> bool:
    """Return whether the observation establishes its core configuration."""
    settings = meta.get("settings") or {}
    sizes = ("processing_width", "processing_height", "fps", "preprocess_batch_size")
    names = ("mapping_backend", "segmentation_model", "mode")
    total = (meta.get("hardware") or {}).get("total_ram_bytes")
    return (
        meta.get("version") == 1
        and meta.get("basis") in ("process", "machine")
        and all(
            isinstance(settings.get(key), int) and not isinstance(settings[key], bool) and settings[key] > 0
            for key in sizes
        )
        and all(isinstance(settings.get(key), str) and settings[key] for key in names)
        and isinstance(total, int)
        and total > 0
    )


def distribution(values: list) -> dict:
    """Return linearly interpolated quartiles and observed extrema."""
    values = sorted(
        float(v)
        for v in values
        if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v >= 0
    )
    if not values:
        return {"n": 0, "min": None, "q1": None, "median": None, "q3": None, "max": None}

    def percentile(fraction: float) -> float:
        position = (len(values) - 1) * fraction
        lower = int(position)
        upper = min(lower + 1, len(values) - 1)
        return values[lower] + (values[upper] - values[lower]) * (position - lower)

    return {
        "n": len(values),
        "min": values[0],
        "q1": percentile(0.25),
        "median": percentile(0.5),
        "q3": percentile(0.75),
        "max": values[-1],
    }


def signature(row: dict) -> str:
    """Return the identity shared by comparable observations."""
    return json.dumps([row["settings"], row["hardware"], row["basis"], row["known"]], sort_keys=True)


def comparable(left: dict, right: dict, parameter: str) -> bool:
    """Return whether configurations differ only in the selected parameter."""
    if left["basis"] != right["basis"] or left["hardware"] != right["hardware"]:
        return False
    if not left["known"] or not right["known"]:
        return False
    excluded = PARAMETERS[parameter]
    if any(
        left["settings"].get(key) is None or right["settings"].get(key) is None
        for key in excluded
        if key != "model_revisions"
    ):
        return False
    return {k: v for k, v in left["settings"].items() if k not in excluded} == {
        k: v for k, v in right["settings"].items() if k not in excluded
    }


def summarize(rows: list[dict], minimum: int = 0, maximum: int = 0) -> list[dict]:
    """Return configuration groups over the selected workload range."""
    buckets = defaultdict(list)
    for row in rows:
        frames = row.get("frames")
        if (minimum or maximum) and (not frames or frames < minimum or (maximum and frames > maximum)):
            continue
        buckets[signature(row)].append(row)
    groups = []
    for key, members in buckets.items():
        latest = max(members, key=lambda row: row.get("recorded_at") or "")
        stats = {
            metric: distribution([row.get(metric) for row in members if row["status"] == "completed"])
            for metric in ("ram", "swap", "vram", "seconds_per_frame")
        }
        groups.append(
            {
                **latest,
                "key": key,
                "stats": stats,
                "runs": members,
                "count": len(members),
                "completed": sum(row["status"] == "completed" for row in members),
                "failed": sum(row["status"] == "failed" for row in members),
                "workload": distribution([row.get("frames") for row in members]),
            }
        )
    return sorted(groups, key=lambda group: group.get("recorded_at") or "", reverse=True)


def timing_note(meta: dict, frames, duration) -> str:
    """Return why a per-frame timing observation is available or excluded."""
    if not meta:
        return "Eligibility not recorded"
    if meta.get("status") != "completed":
        return "Incomplete run"
    if not meta.get("timing_complete"):
        return "Cached or partial execution"
    if not frames:
        return "Frame count not recorded"
    return "Full run" if duration is not None else "Duration not recorded"


def history_observations(path=None) -> list[dict]:
    """Return local history observations, preserving unknown legacy metadata."""
    from deepreefmap_gui.profiling.run_history import _load_all, timings_path

    rows = []
    target = path or timings_path()
    if path is None:
        from deepreefmap_gui.profiling.performance_journal import import_legacy, observations

        import_legacy(target)
        histories = [
            [
                {
                    "performance_observation": payload["observation"],
                    "stage_peaks": payload["stage_peaks"],
                }
                for payload in observations()
            ]
        ]
    else:
        history = dict(_load_all(target))
        for key, entries in _load_all(target.with_name("performance_runs.json")).items():
            legacy_entries = [entry for entry in history.get(key, []) if not entry.get("performance_observation")]
            history[key] = legacy_entries + entries
        histories = list(history.values())
    seen = set()
    for entries in histories:
        for entry in entries:
            meta = entry.get("performance_observation") or {}
            if meta.get("id") in seen:
                continue
            if meta.get("id"):
                seen.add(meta["id"])
            legacy = observation({"system_profile": entry.get("system_profile")}, False)
            settings = meta.get("settings") or entry.get("params") or {}
            peaks = entry.get("stage_peaks") or {}
            if not peaks and not meta:
                continue
            frames = meta.get("frames", entry.get("frames"))
            duration = meta.get("duration_s")
            rate = (
                duration / frames
                if meta.get("timing_complete") and duration is not None and frames and frames > 0
                else None
            )
            rows.append(
                {
                    "settings": settings,
                    "hardware": meta.get("hardware", legacy["hardware"]),
                    "basis": meta.get("basis", "process" if entry.get("version", 1) >= 2 else "machine"),
                    "known": settings_known(meta),
                    "frames": frames,
                    "duration_s": duration,
                    "seconds_per_frame": rate,
                    "timing_note": timing_note(meta, frames, duration),
                    "status": meta.get("status", "completed"),
                    "recorded_at": meta.get("recorded_at"),
                    **{
                        metric: distribution(
                            [stage.get(metric + "_bytes") for stage in peaks.values() if isinstance(stage, dict)]
                        )["max"]
                        for metric in ("ram", "swap", "vram")
                    },
                }
            )
    return rows


def record_observation(manifest: dict, path=None) -> None:
    """Retain a performance observation independently of survey run storage."""
    from deepreefmap_gui.io.atomic import atomic_write_json
    from deepreefmap_gui.profiling.run_history import _load_all, history_key, timings_path

    meta = manifest.get("performance_observation")
    if not meta:
        return
    if path is None:
        from deepreefmap_gui.profiling.performance_journal import store

        run_id = (manifest.get("survey") or {}).get("run_id")
        store(
            {
                "id": meta["id"],
                "run_id": run_id,
                "observation": meta,
                "stage_peaks": manifest.get("stage_peaks") or {},
                "source": "device",
            }
        )
        return
    target = (path or timings_path()).with_name("performance_runs.json")
    settings = meta["settings"]
    key = history_key(
        settings.get("mapping_backend"),
        settings.get("segmentation_model"),
        settings.get("processing_width"),
        settings.get("processing_height"),
        settings.get("fps"),
    )
    history = dict(_load_all(target))
    entries = [entry for entry in history.get(key, []) if entry["performance_observation"]["id"] != meta["id"]]
    entries.append({"performance_observation": meta, "stage_peaks": manifest.get("stage_peaks") or {}})
    history[key] = entries
    try:
        atomic_write_json(target, history)
    except OSError:
        import logging

        logging.getLogger(__name__).warning("Could not retain performance observations", exc_info=True)
