"""Per-machine timing profile used to seed remaining-time estimates."""

from __future__ import annotations

import json
import logging
import os
import statistics
from dataclasses import dataclass
from pathlib import Path

from deepreefmap_gui.io.atomic import atomic_write_json
from deepreefmap_gui.paths import run_timings_path as timings_path
from deepreefmap_gui.profiling.eta import STAGES

logger = logging.getLogger(__name__)

# Parsed profile, keyed by the file's path, mtime and size. See _load_all.
_LOADED: dict[tuple[str, int, int], dict[str, list[dict]]] = {}

# One cold-cache or thermally throttled run should not skew the profile, so we
# keep a short rolling window and fit with the median rather than the mean.
_MAX_RUNS_PER_KEY = 10

# Stamped on each entry so a future schema change can tell old (peak-less) runs
# from new ones. Bump when the entry shape changes incompatibly.
#
# 2: stage peaks are the run's own process rather than the whole machine. A
#    version 1 peak carries the desktop's RAM and whatever was in swap before the
#    run began, which on a busy machine is tens of gigabytes that were never the
#    run's, so those peaks are read as absent rather than believed.
_ENTRY_VERSION = 2
_PROCESS_PEAK_VERSION = 2


def history_key(mapping_backend: str, seg_model: str, proc_w: int, proc_h: int, fps: int) -> str:
    """Profile key grouping runs comparable enough to seed each other's estimate."""
    # fps belongs in the key because it changes the memory regime: 5fps can thrash
    # RAM where 3fps does not, which changes the per-frame time. Other params stay
    # out so the short rolling history is not fragmented into singletons.
    return f"{mapping_backend}|{seg_model}|{proc_w}x{proc_h}|{fps}fps"


def seg_key(segmentation_name: str | None, skip_segmentation: bool) -> str:
    """How a run's segmentation model is spelled in the history key.

    Skipping segmentation is its own identity, not a missing model, and is what
    a finished run is recorded under (instrumentation.py). Predicting under the
    combo's model name instead prices a geometry-only pass with priors that
    include segmentation, and files its own runs under a key nothing reads.
    """
    return "__skip__" if skip_segmentation else str(segmentation_name or "")


def load_expected_points(key: str, path: Path | None = None) -> int | None:
    """Median final point count over stored runs for `key`, or None if unseen."""
    points = [int(r["points"]) for r in _load_all(path or timings_path()).get(key, []) if r.get("points")]
    return int(statistics.median(points)) if points else None


@dataclass(frozen=True)
class ProfileEntry:
    """One recorded configuration, and what it costs per unit of its drivers.

    A key on its own does not say what shape a run was; this pairs the learned
    per-stage rates with the resolution and frame rate they were learned at, so a
    pass at another size can be predicted by scaling from the nearest one.
    """

    key: str
    mapping_backend: str
    seg_model: str
    width: int
    height: int
    fps: int
    priors: dict[str, float]
    frames: int | None
    points: int | None

    @property
    def pixels(self) -> int:
        return max(1, self.width * self.height)


def _shape_from_key(key: str) -> tuple[str, str, int, int, int] | None:
    """Unpack a `backend|seg|WxH|Nfps` key, for entries recorded without params."""
    parts = key.split("|")
    if len(parts) != 4 or not parts[3].endswith("fps"):
        return None
    size = parts[2].split("x")
    if len(size) != 2:
        return None
    try:
        return parts[0], parts[1], int(size[0]), int(size[1]), int(parts[3][:-3])
    except ValueError:
        return None


def load_profile_entries(path: Path | None = None) -> list[ProfileEntry]:
    """Every configuration this machine has learned timings for."""
    target = path or timings_path()
    entries: list[ProfileEntry] = []
    for key, runs in _load_all(target).items():
        priors = load_priors(key, target)
        if not priors:
            continue
        params = next((r.get("params") for r in runs if r.get("params")), None) or {}
        shape = _shape_from_key(key)
        backend = params.get("mapping_backend") or (shape[0] if shape else "")
        seg = params.get("segmentation_model") or (shape[1] if shape else "")
        width = int(params.get("processing_width") or (shape[2] if shape else 0))
        height = int(params.get("processing_height") or (shape[3] if shape else 0))
        fps = int(params.get("fps") or (shape[4] if shape else 0))
        if not (backend and width and height and fps):
            continue
        entries.append(
            ProfileEntry(
                key=key,
                mapping_backend=backend,
                seg_model=seg,
                width=width,
                height=height,
                fps=fps,
                priors=priors,
                frames=load_expected_frames(key, target),
                points=load_expected_points(key, target),
            )
        )
    return entries


def load_expected_frames(key: str, path: Path | None = None) -> int | None:
    """Median frame count over stored runs for `key`, or None if unseen."""
    frames = [
        int(r["frames"]) for r in _load_all(path or timings_path()).get(key, []) if r.get("frames")
    ]
    return int(statistics.median(frames)) if frames else None


def _strip_fps(key: str) -> str:
    """Drop the trailing `|Nfps` segment so keys group by resolution, not fps."""
    return key.rsplit("|", 1)[0] if key.endswith("fps") else key


def load_expected_peaks(
    key: str,
    path: Path | None = None,
    *,
    gpu_name: str | None = None,
    batch_size: int | None = None,
) -> dict | None:
    """Worst recent peak as `{ram_bytes, frames, vram_bytes, vram_frames}`.

    The RAM pair pools across every fps at this backend/model/resolution (unlike
    the ETA priors, keyed per fps) because peak memory tracks the frame count the
    caller scales by. Worst run rather than median: this is a crash predictor,
    and the high-water run says what happens on a busier machine.

    The VRAM pair is qualified far more narrowly, because the caller uses it to
    move a fixed term rather than to lift a baseline. It is offered only when the
    caller names the card, and only from runs on a card of that name at the same
    preprocessing batch size, which changes device memory and is not in the key.
    Without a gpu_name there is no VRAM pair at all: a peak from another
    machine's card would rewrite this one's constant.

    VRAM is carried with its own run's frame count, not the worst-RAM run's. The
    caller subtracts a per-frame slope from it, and the wrong length there moves
    the intercept by exactly that error.
    """
    prefix = _strip_fps(key)
    runs = [
        r
        for k, entries in _load_all(path or timings_path()).items()
        if _strip_fps(k) == prefix
        for r in entries
        if r.get("stage_peaks") and r.get("frames")
    ]
    if not runs:
        return None
    worst_committed = 0
    worst_frames = 0
    worst_vram = 0
    worst_vram_frames = 0
    for run in runs:
        stages = list(run["stage_peaks"].values())
        # Peak committed memory per stage = RAM plus the swap it spilled into, then
        # the worst stage. A thrashing run pins RAM near 100% and shows its real
        # demand as swap, so RAM alone would understate the true peak.
        #
        # Only from entries that measured this run's own process. An older one
        # measured the whole machine, so its figure carries the desktop's RAM and
        # whatever was in swap before the run began -- tens of gigabytes that were
        # never the run's, applied to a fixed term the user cannot argue with. The
        # VRAM half of the same entry is still read: the card was always sampled
        # device-wide, deliberately, and that has not changed.
        per_stage = (
            [s["ram_bytes"] + (s.get("swap_bytes") or 0) for s in stages if s.get("ram_bytes")]
            if int(run.get("version") or 1) >= _PROCESS_PEAK_VERSION
            else []
        )
        if per_stage:
            committed = max(per_stage)
            if committed > worst_committed:
                worst_committed = committed
                worst_frames = int(run["frames"])
        if gpu_name is None:
            continue
        recorded_gpu = (run.get("system_profile") or {}).get("gpu") or {}
        if recorded_gpu.get("name") != gpu_name:
            continue
        params = run.get("params") or {}
        recorded_batch = params.get("preprocess_batch_size")
        # Absent counts as matching: batch size was not recorded before this was
        # introduced, and discarding every older entry would cost the whole
        # history to guard against a mismatch that is usually not one.
        if batch_size is not None and recorded_batch not in (None, batch_size):
            continue
        vram = [s["vram_bytes"] for s in stages if s.get("vram_bytes")]
        if vram and max(vram) > worst_vram:
            worst_vram = max(vram)
            worst_vram_frames = int(run["frames"])
    if not worst_committed and not worst_vram:
        return None
    return {
        "ram_bytes": worst_committed or None,
        "frames": worst_frames,
        "vram_bytes": worst_vram or None,
        "vram_frames": worst_vram_frames if worst_vram else None,
    }


def summarise_recorded_runs(path: Path | None = None) -> list[dict]:
    """One row per recorded run that captured peaks, for the system panel."""
    rows: list[dict] = []
    for key, entries in _load_all(path or timings_path()).items():
        for entry in entries:
            peaks = entry.get("stage_peaks")
            if not peaks:
                continue
            rams = [s["ram_bytes"] for s in peaks.values() if s.get("ram_bytes")]
            swaps = [s["swap_bytes"] for s in peaks.values() if s.get("swap_bytes")]
            vrams = [s["vram_bytes"] for s in peaks.values() if s.get("vram_bytes")]
            profile = entry.get("system_profile") or {}
            gpu = profile.get("gpu") or {}
            durations = entry.get("stage_durations") or {}
            # Sum of the timed stages = wall-clock cost of the run, the figure the
            # the system panel reports and normalises per frame across configs.
            run_seconds = sum(float(v) for v in durations.values()) if durations else None
            rows.append(
                {
                    "key": key,
                    "params": entry.get("params") or {},
                    "frames": entry.get("frames"),
                    "points": entry.get("points"),
                    "run_seconds": run_seconds,
                    "peak_ram_bytes": max(rams) if rams else None,
                    "peak_swap_bytes": max(swaps) if swaps else 0,
                    # Distinguish "measured 0 swap" from "never measured it", so
                    # the UI can show "not recorded" rather than a misleading 0%.
                    # A null is the second case: entries predating swap capture
                    # carry no key, and a machine that cannot read per-process
                    # swap carries the key with nothing in it.
                    "swap_recorded": any(s.get("swap_bytes") is not None for s in peaks.values()),
                    "peak_vram_bytes": max(vrams) if vrams else None,
                    # Whether the RAM and swap figures are the run's own or the
                    # whole machine's. Old entries measured the machine, and a
                    # panel that shows both without saying which is which invites
                    # the reader to compare two different quantities.
                    "machine_basis": int(entry.get("version") or 1) < _PROCESS_PEAK_VERSION,
                    "total_ram_bytes": profile.get("total_ram_bytes"),
                    "total_swap_bytes": profile.get("total_swap_bytes") or 0,
                    "gpu_name": gpu.get("name"),
                    "gpu_total_vram_bytes": gpu.get("total_vram_bytes"),
                }
            )
    # Newest-first within each key only. Entries carry no timestamp, so runs under
    # different keys cannot be ordered against each other.
    rows.reverse()
    return rows


def group_recorded_runs(path: Path | None = None) -> list[dict]:
    """Collapse repeat runs of the same config into one median-averaged entry.

    The typical-cost view for the system panel. The pre-run check instead reasons from
    the worst recent run (load_expected_peaks), which is the crash predictor.
    """
    groups: dict[tuple, list[dict]] = {}
    for row in summarise_recorded_runs(path):
        p = row["params"]
        signature = (
            p.get("mapping_backend"), p.get("segmentation_model"),
            p.get("processing_width"), p.get("processing_height"),
            p.get("fps"), row["frames"],
        )
        groups.setdefault(signature, []).append(row)

    grouped: list[dict] = []
    for members in groups.values():
        # The signature carries no machine identity, so the newest member's totals
        # stand in for the group: a re-run after a RAM upgrade reports the new total.
        rep = members[0]
        rams = [m["peak_ram_bytes"] for m in members if m["peak_ram_bytes"]]
        swaps = [m["peak_swap_bytes"] for m in members if m.get("swap_recorded")]
        vrams = [m["peak_vram_bytes"] for m in members if m["peak_vram_bytes"]]
        secs = [m["run_seconds"] for m in members if m.get("run_seconds")]
        # A group is one exact config (fps and frame count are both in the signature),
        # so this time is never pooled across fps, whose memory regime differs.
        run_seconds = int(statistics.median(secs)) if secs else None
        frames = rep["frames"]
        grouped.append(
            {
                "params": rep["params"],
                "frames": frames,
                "count": len(members),
                "run_seconds": run_seconds,
                # A per-frame throughput hint for eyeballing between cards. Rough
                # only: fps and scene complexity change per-frame cost, so it is not
                # an apples-to-apples figure across different fps.
                "seconds_per_frame": (run_seconds / frames) if run_seconds and frames else None,
                "peak_ram_bytes": int(statistics.median(rams)) if rams else None,
                "peak_swap_bytes": int(statistics.median(swaps)) if swaps else 0,
                "swap_recorded": any(m.get("swap_recorded") for m in members),
                "peak_vram_bytes": int(statistics.median(vrams)) if vrams else None,
                # One old member is enough to qualify the group: the median it
                # sits in is no longer purely the run's own.
                "machine_basis": any(m.get("machine_basis") for m in members),
                "total_ram_bytes": rep["total_ram_bytes"],
                "total_swap_bytes": rep["total_swap_bytes"],
                "gpu_name": rep["gpu_name"],
                "gpu_total_vram_bytes": rep["gpu_total_vram_bytes"],
            }
        )
    return grouped


def distinct_model_combinations(rows: list[dict]) -> list[tuple[str, str]]:
    """Unique (mapping_backend, segmentation_model) pairs feeding the system filter."""
    seen: dict[tuple[str, str], None] = {}
    for row in rows:
        p = row["params"]
        seen.setdefault((p.get("mapping_backend"), p.get("segmentation_model")), None)
    return list(seen)


def _load_all(path: Path) -> dict[str, list[dict]]:
    """Stored profile, or an empty one if there is nothing readable to load.

    A file that cannot be parsed is moved aside rather than silently overwritten
    by the next record_run, so the machine keeps its evidence of whatever went
    wrong. The single quarantine slot is deliberate: repeated corruption should
    not fill the config directory with copies.

    Memoised on the file's own identity: the batch prediction asks for priors,
    points and frames per queued pass and per recorded config, on every row
    mutation.
    """
    try:
        stat = path.stat()
    except OSError:
        return {}
    stamp = (str(path), stat.st_mtime_ns, stat.st_size)
    cached = _LOADED.get(stamp)
    if cached is not None:
        return cached
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        loaded = json.loads(raw)
    except ValueError:
        loaded = None
    if isinstance(loaded, dict):
        # One entry: older revisions can never be asked for again.
        _LOADED.clear()
        _LOADED[stamp] = loaded
        return loaded
    quarantine = path.with_name(path.name + ".corrupt")
    try:
        os.replace(path, quarantine)
    except OSError:
        logger.warning("Run timing profile at %s is unreadable", path, exc_info=True)
    else:
        logger.warning("Run timing profile at %s was unreadable; moved to %s", path, quarantine)
    return {}


def load_priors(key: str, path: Path | None = None) -> dict[str, float]:
    """Seconds-per-driver-unit per stage, median over stored runs for `key`."""
    runs = _load_all(path or timings_path()).get(key, [])
    if not runs:
        return {}
    from deepreefmap_gui.profiling.eta import driver_denominator

    priors: dict[str, float] = {}
    for spec in STAGES:
        ratios: list[float] = []
        for run in runs:
            duration = run.get("stage_durations", {}).get(spec.key)
            if duration is None:
                continue
            denom = driver_denominator(spec.driver, run.get("frames", 0), run.get("points"))
            if denom and denom > 0:
                ratios.append(duration / denom)
        if ratios:
            priors[spec.key] = statistics.median(ratios)
    return priors


def record_run(
    key: str,
    stage_durations: dict[str, float],
    frames: int,
    points: int | None,
    params: dict | None = None,
    stage_peaks: dict | None = None,
    system_profile: dict | None = None,
    path: Path | None = None,
) -> None:
    """Append one finished run to the profile, capped to the rolling window."""
    target = path or timings_path()
    all_runs = _load_all(target)
    entry: dict = {"version": _ENTRY_VERSION, "stage_durations": stage_durations, "frames": frames, "points": points}
    if params:
        entry["params"] = params
    if stage_peaks:
        entry["stage_peaks"] = stage_peaks
    if system_profile:
        entry["system_profile"] = system_profile
    all_runs.setdefault(key, []).append(entry)
    all_runs[key] = all_runs[key][-_MAX_RUNS_PER_KEY:]
    try:
        atomic_write_json(target, all_runs)
    except OSError:
        logger.warning("Could not write run timing profile to %s", target, exc_info=True)
    # For filesystems whose mtime granularity is coarser than the gap between
    # this write and the next read.
    _LOADED.clear()


def record_run_from_manifest(
    manifest: dict, cached_stages: "frozenset[str] | None" = None
) -> None:
    """Fold a finished run's manifest timings into the local timing profile.

    Stages named in `cached_stages` are dropped: their span measured a seeded
    hard-link rather than the work, and folding that in makes every fresh run
    predicted from this key too fast.
    """
    durations = manifest.get("stage_durations") or {}
    if cached_stages:
        durations = {k: v for k, v in durations.items() if k not in cached_stages}
    if not durations:
        return
    try:
        # `or 0` rather than a get() default: a manifest may carry an explicit
        # null for a field it did not measure, and the default only covers an
        # absent key. Getting this wrong loses the whole run's timings silently,
        # because the failure is swallowed below.
        key = history_key(
            str(manifest.get("mapping_backend") or ""),
            str(manifest.get("segmentation_model") or ""),
            int(manifest.get("processing_width") or 0),
            int(manifest.get("processing_height") or 0),
            int(manifest.get("fps") or 0),
        )
        params = {
            k: manifest.get(k)
            for k in (
                "fps", "mapping_backend", "segmentation_model", "processing_width",
                "processing_height", "mapping_options", "enable_tsdf", "grid_bins",
                # Recorded so a VRAM peak can be matched to the batch size it was
                # measured at; the segmentation term scales with it.
                "preprocess_batch_size",
            )
            if manifest.get(k) is not None
        }
        record_run(
            key,
            {k: float(v) for k, v in durations.items()},
            frames=int(manifest.get("frames_processed") or 0),
            points=manifest.get("metric_points"),
            params=params,
            stage_peaks=manifest.get("stage_peaks") or None,
            system_profile=manifest.get("system_profile") or None,
        )
    except Exception:
        logger.warning("Could not record run timings", exc_info=True)
