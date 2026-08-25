"""Cross-run benthic cover assembly and repeatability statistics for a transect.

The defensible transect cover figure is the count-weighted pool in
:func:`pooled_transect_cover`: it sums the observed counts over the summed
denominators, so a short pass cannot swing the estimate the way an unweighted
mean of per-pass fractions does. Reruns of one pass are collapsed to the latest
succeeded run before pooling (:func:`latest_run_per_pass`) so a rerun does not
count twice. :func:`repeatability_stats` still reports the spread between
passes, but that spread is not the cover estimate.
"""

from __future__ import annotations

import json
import logging
import math
import uuid
from dataclasses import dataclass, fields
from pathlib import Path

from deepreefmap.config.classes import ClassConfig

from deepreefmap_gui.cover import COVER_LEVELS, aggregate_cover, taxonomy_hash, taxonomy_version
from deepreefmap_gui.packaging.releases import current_version
from deepreefmap_gui.survey.store import SurveyStore

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class PassCover:
    """Benthic cover of one succeeded run, rolled up to a group level.

    ``counts`` and ``denominator`` are kept alongside ``cover`` (the fractions)
    so the pooled estimator can weight each pass by what it actually observed
    instead of averaging ratios. ``created_at`` orders reruns of one pass.
    """

    run_id: uuid.UUID
    pass_id: uuid.UUID
    run_dir_name: str
    direction: str
    video_hash: str | None
    begin_s: float
    end_s: float
    cover: dict[str, float]
    counts: dict[str, float]
    denominator: float
    created_at: str


@dataclass(slots=True)
class PooledCover:
    """Count-weighted transect cover: summed counts over summed denominators.

    This is the transect cover estimate. ``contributing_passes`` of
    ``expected_passes`` states how much of the transect the number rests on, so
    a figure built from two of five passes is never mistaken for the whole line.
    """

    cover: dict[str, float]
    counts: dict[str, float]
    denominator: float
    contributing_passes: int
    expected_passes: int


def _read_succeeded_covers(
    store: SurveyStore,
    out_root: Path,
    transect_id: uuid.UUID,
    classes_config: ClassConfig,
    level: str,
) -> list[PassCover]:
    """Every succeeded run's cover, one PassCover per run (reruns included)."""
    passes = {p.id: p for p in store.list_passes(transect_id=transect_id)}
    covers = []
    for run in store.runs_for_transect(transect_id):
        if run.status != "succeeded":
            continue
        pass_ = passes.get(run.pass_id)
        if pass_ is None:
            continue
        cover_path = out_root / run.run_dir_name / "benthic_cover.json"
        try:
            raw = json.loads(cover_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.warning("No readable benthic cover for %s", run.run_dir_name)
            continue
        grouped = aggregate_cover(raw, classes_config, level)
        denom = float(raw.get("denominator", 0.0)) if isinstance(raw, dict) else 0.0
        video = store.get_video(pass_.video_id)
        covers.append(PassCover(
            run_id=run.id,
            pass_id=pass_.id,
            run_dir_name=run.run_dir_name,
            direction=pass_.direction,
            video_hash=video.hash if video is not None else None,
            begin_s=pass_.begin_s,
            end_s=pass_.end_s,
            cover={name: float(entry["fraction"]) for name, entry in grouped.items()},
            counts={name: float(entry["count"]) for name, entry in grouped.items()},
            denominator=denom,
            created_at=run.created_at,
        ))
    return covers


def latest_run_per_pass(covers: list[PassCover]) -> list[PassCover]:
    """Collapse reruns to the latest succeeded run per pass, keeping pass order.

    Pooling over every succeeded run would let a re-processed pass count twice,
    inflating the transect estimate towards whichever pass was retried most.
    """
    latest: dict[uuid.UUID, PassCover] = {}
    order: list[uuid.UUID] = []
    for cover in covers:
        if cover.pass_id not in latest:
            order.append(cover.pass_id)
        current = latest.get(cover.pass_id)
        # created_at is ISO-8601 UTC, so a lexical compare is a time compare.
        # `>=` keeps the later-inserted run on a tie (runs_for_transect is
        # already ordered oldest-first).
        if current is None or cover.created_at >= current.created_at:
            latest[cover.pass_id] = cover
    return [latest[pass_id] for pass_id in order]


def assemble_transect_covers(
    store: SurveyStore,
    out_root: Path,
    transect_id: uuid.UUID,
    classes_config: ClassConfig,
    level: str = "intermediate",
    *,
    dedupe: bool = True,
) -> list[PassCover]:
    """Read benthic_cover.json from the transect's succeeded runs.

    With ``dedupe`` (the default) reruns collapse to one run per pass, which is
    the set the cover estimate is built from. Pass ``dedupe=False`` to keep every
    succeeded run, as :func:`reproducibility_groups` needs the reruns.
    """
    covers = _read_succeeded_covers(store, out_root, transect_id, classes_config, level)
    return latest_run_per_pass(covers) if dedupe else covers


def pooled_transect_cover(
    covers: list[PassCover], expected_passes: int | None = None
) -> PooledCover:
    """Count-weighted cover across passes: sum counts, sum denominators, divide.

    ``covers`` must already be deduped to one run per pass (see
    :func:`latest_run_per_pass`). ``expected_passes`` is how many passes the
    transect defines, so the caller can report "N of M passes". It defaults to
    the number that contributed when not supplied.
    """
    total_denom = sum(cover.denominator for cover in covers)
    counts: dict[str, float] = {}
    for cover in covers:
        for label, count in cover.counts.items():
            counts[label] = counts.get(label, 0.0) + count
    pooled = {
        label: (count / total_denom if total_denom > 0 else 0.0)
        for label, count in counts.items()
    }
    contributing = len(covers)
    return PooledCover(
        cover=pooled,
        counts=counts,
        denominator=total_denom,
        contributing_passes=contributing,
        expected_passes=contributing if expected_passes is None else expected_passes,
    )


def cover_labels(covers: list[PassCover], minimum_fraction: float = 0.0) -> list[str]:
    """Labels reaching ``minimum_fraction`` in any pass, largest mean cover first."""
    labels = {label for c in covers for label in c.cover}
    kept = []
    for label in labels:
        values = [c.cover.get(label, 0.0) for c in covers]
        if max(values) >= minimum_fraction:
            kept.append((sum(values) / len(values), label))
    return [label for _, label in sorted(kept, reverse=True)]


def repeatability_stats(covers: list[PassCover]) -> dict[str, dict[str, float]]:
    """Per label: mean, sample std, coefficient of variation, and value range.

    This is spread between passes, reported to judge how repeatable the survey
    is. It is not the transect cover estimate, which is the count-weighted pool.
    """
    stats: dict[str, dict[str, float]] = {}
    for label in {label for c in covers for label in c.cover}:
        values = [c.cover.get(label, 0.0) for c in covers]
        mean = sum(values) / len(values)
        variance = (
            sum((v - mean) ** 2 for v in values) / (len(values) - 1) if len(values) > 1 else 0.0
        )
        std = math.sqrt(variance)
        stats[label] = {
            "mean": mean,
            "std": std,
            "cv": std / mean if mean > 0 else 0.0,
            "range": max(values) - min(values),
            # The endpoints and the count behind them, so a chart can draw the
            # spread rather than only state its width.
            "min": min(values),
            "max": max(values),
            "n": float(len(values)),
        }
    return stats


@dataclass(frozen=True)
class AggregatedCover:
    """One transect's cover as a single estimate per class, with its spread.

    ``values`` is the count-weighted pool, ``spread`` the lowest and highest
    single-pass fraction behind it, and ``n`` how many passes that is. The two are
    different estimators: a weighted mean always falls inside the range, but the
    range is unweighted, so a reader has to be told which is which.
    """

    labels: list[str]
    values: dict[str, float]
    spread: dict[str, tuple[float, float]]
    n: int
    expected_passes: int


def aggregated_cover_chart(
    covers: list[PassCover],
    *,
    minimum_fraction: float = 0.0,
    expected_passes: int | None = None,
) -> AggregatedCover:
    """The transect estimate and its between-pass range, ready to plot.

    The spread is the observed range, not a standard deviation or a standard
    error: a transect has one to four passes, where a sample SD is unstable and a
    SEM asserts a normal approximation the data cannot support. At one pass there
    is no spread to report and ``spread`` is empty.
    """
    pooled = pooled_transect_cover(covers, expected_passes=expected_passes)
    labels = cover_labels(covers, minimum_fraction)
    stats = repeatability_stats(covers) if len(covers) > 1 else {}
    return AggregatedCover(
        labels=labels,
        values={label: pooled.cover.get(label, 0.0) for label in labels},
        spread={
            label: (stats[label]["min"], stats[label]["max"])
            for label in labels
            if label in stats
        },
        n=pooled.contributing_passes,
        expected_passes=pooled.expected_passes,
    )


def reproducibility_groups(covers: list[PassCover]) -> list[list[PassCover]]:
    """Runs over identical footage and trim: any spread here is pipeline variance.

    Feed this the un-deduped set (``assemble_transect_covers(..., dedupe=False)``),
    since the reruns it groups are exactly what deduping removes.
    """
    groups: dict[tuple[str, float, float], list[PassCover]] = {}
    for c in covers:
        if c.video_hash is None:
            continue
        key = (c.video_hash, round(c.begin_s, 2), round(c.end_s, 2))
        groups.setdefault(key, []).append(c)
    return [group for group in groups.values() if len(group) > 1]


# --- Provenance and collated long-format export ---


@dataclass(slots=True)
class LongCoverRow:
    """One row of the collated long-format cover export.

    ``estimator`` is ``per_pass`` for a single pass's observed cover or
    ``pooled`` for the count-weighted transect estimate. Pass-only columns are
    blank on pooled rows, and run-only provenance is blank where it does not apply.
    """

    transect_name: str
    transect_id: str
    level: str
    group: str
    estimator: str
    fraction: float
    count: float
    denominator: float
    contributing_passes: int
    expected_passes: int
    pass_id: str
    direction: str
    begin_s: float | None
    end_s: float | None
    run_id: str
    run_dir_name: str
    deepreefmap_version: str
    segmentation_model: str
    mapping_backend: str
    gui_version: str
    taxonomy_version: int
    taxonomy_hash: str


LONG_COVER_COLUMNS = [f.name for f in fields(LongCoverRow)]


def _run_manifest_provenance(out_root: Path, run_dir_name: str) -> dict[str, str]:
    """Model identity and library version the run recorded in its manifest.

    These sit at the manifest top level, not in benthic_cover.json, so a per-pass
    row can still name the models that produced it.
    """
    blank = {"deepreefmap_version": "", "segmentation_model": "", "mapping_backend": ""}
    path = out_root / run_dir_name / "run_manifest.json"
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return blank
    if not isinstance(manifest, dict):
        return blank
    return {
        "deepreefmap_version": str(manifest.get("deepreefmap_version") or ""),
        "segmentation_model": str(manifest.get("segmentation_model") or ""),
        "mapping_backend": str(manifest.get("mapping_backend") or ""),
    }


def collate_long_format(
    store: SurveyStore,
    out_root: Path,
    classes_config: ClassConfig,
    *,
    levels: tuple[str, ...] = COVER_LEVELS,
    transect_ids: list[uuid.UUID] | None = None,
) -> list[LongCoverRow]:
    """One long-format table across transects: per-pass rows plus the pooled estimate.

    Each per-pass row is one class-group's cover in one pass at one level. A
    trailing ``pooled`` row per transect and level carries the count-weighted
    estimate and its contributing/expected pass counts.
    """
    # The roll-up to groups happens here, at analysis time, so the taxonomy that
    # governs these grouped numbers is the one bundled now, not whatever was
    # current when the runs were processed. Stamp that identity on every row.
    gui_version = current_version()
    tax_version = taxonomy_version()
    tax_hash = taxonomy_hash()
    # Resolved rather than picked. A line the registry retired is not one anybody
    # may choose any more, but the passes swum on it before it went are still
    # results, and leaving them out drops them from the export and from the cover
    # the registry is sent.
    transects = store.list_transects_for_reference()
    if transect_ids is not None:
        wanted = set(transect_ids)
        transects = [t for t in transects if t.id in wanted]

    rows: list[LongCoverRow] = []
    for transect in transects:
        expected = len(store.list_passes(transect_id=transect.id))
        for level in levels:
            per_pass = assemble_transect_covers(
                store, out_root, transect.id, classes_config, level=level
            )
            pooled = pooled_transect_cover(per_pass, expected_passes=expected)
            tname, tid = transect.name, str(transect.id)
            for cover in per_pass:
                run_prov = _run_manifest_provenance(out_root, cover.run_dir_name)
                rows.extend(
                    LongCoverRow(
                        transect_name=tname,
                        transect_id=tid,
                        level=level,
                        group=group,
                        estimator="per_pass",
                        fraction=cover.cover.get(group, 0.0),
                        count=cover.counts.get(group, 0.0),
                        denominator=cover.denominator,
                        contributing_passes=pooled.contributing_passes,
                        expected_passes=pooled.expected_passes,
                        pass_id=str(cover.pass_id),
                        direction=cover.direction,
                        begin_s=cover.begin_s,
                        end_s=cover.end_s,
                        run_id=str(cover.run_id),
                        run_dir_name=cover.run_dir_name,
                        deepreefmap_version=run_prov["deepreefmap_version"],
                        segmentation_model=run_prov["segmentation_model"],
                        mapping_backend=run_prov["mapping_backend"],
                        gui_version=gui_version,
                        taxonomy_version=tax_version,
                        taxonomy_hash=tax_hash,
                    )
                    for group in sorted(cover.counts)
                )
            rows.extend(
                LongCoverRow(
                    transect_name=tname,
                    transect_id=tid,
                    level=level,
                    group=group,
                    estimator="pooled",
                    fraction=pooled.cover.get(group, 0.0),
                    count=pooled.counts.get(group, 0.0),
                    denominator=pooled.denominator,
                    contributing_passes=pooled.contributing_passes,
                    expected_passes=pooled.expected_passes,
                    pass_id="",
                    direction="",
                    begin_s=None,
                    end_s=None,
                    run_id="",
                    run_dir_name="",
                    deepreefmap_version="",
                    segmentation_model="",
                    mapping_backend="",
                    gui_version=gui_version,
                    taxonomy_version=tax_version,
                    taxonomy_hash=tax_hash,
                )
                for group in sorted(pooled.counts)
            )
    return rows
