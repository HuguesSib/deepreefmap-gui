"""Survey data writers: transect CSV, the collated cover CSV, and the JSON document."""

from __future__ import annotations

import csv
import json
import uuid
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from deepreefmap_gui.survey.models.transect import Transect

if TYPE_CHECKING:
    from deepreefmap_gui.survey.analysis import LongCoverRow

TRANSECT_CSV_COLUMNS = [
    "name",
    "site",
    "start_lat",
    "start_lon",
    "start_accuracy_m",
    "end_lat",
    "end_lon",
    "end_accuracy_m",
    "length_m",
    "depth_m",
    "description",
    "id",
]


def _blank(value: float | None) -> float | str:
    return "" if value is None else value


def save_transects_csv(
    path: Path,
    transects: Iterable[Transect],
    site_names: Mapping[uuid.UUID, str] | None = None,
) -> None:
    """CSV in the shape import_transects_csv reads back, ids and site names included."""
    names = site_names or {}
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(TRANSECT_CSV_COLUMNS)
        for t in transects:
            writer.writerow([
                t.name,
                names.get(t.site_id, "") if t.site_id else "",
                _blank(t.start_lat),
                _blank(t.start_lon),
                _blank(t.start_accuracy_m),
                _blank(t.end_lat),
                _blank(t.end_lon),
                _blank(t.end_accuracy_m),
                _blank(t.length_m),
                _blank(t.depth_m),
                t.description,
                str(t.id),
            ])


def save_repeatability_csv(
    path: Path,
    labels: list[str],
    stats: Mapping[str, Mapping[str, float]],
    covers: list[Any],
) -> None:
    """Per-class repeatability stats plus one fraction column per pass."""
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(
            ["class", "mean_fraction", "std", "cv", "range"]
            + [c.run_dir_name for c in covers]
        )
        for label in labels:
            entry = stats.get(label, {})
            writer.writerow(
                [
                    label,
                    f"{entry.get('mean', 0.0):.6f}",
                    f"{entry.get('std', 0.0):.6f}",
                    f"{entry.get('cv', 0.0):.4f}",
                    f"{entry.get('range', 0.0):.6f}",
                ]
                + [f"{c.cover.get(label, 0.0):.6f}" for c in covers]
            )


_LONG_PRECISION = {"fraction": 6, "count": 4, "denominator": 4}


def _format_long_value(name: str, value: Any) -> str:
    """Blank for missing, fixed precision for the measured columns, str otherwise."""
    if value is None:
        return ""
    if name in _LONG_PRECISION and isinstance(value, (int, float)):
        return f"{float(value):.{_LONG_PRECISION[name]}f}"
    return str(value)


def save_long_format_csv(path: Path, rows: Sequence[LongCoverRow]) -> None:
    """Collated long-format cover: one row per transect/pass/class/level.

    Column order is the dataclass field order, so a header is always written
    even for an empty survey. Pooled estimate rows carry the same schema with
    ``estimator=pooled`` and the contributing/expected pass counts.
    """
    from deepreefmap_gui.survey.analysis import LONG_COVER_COLUMNS

    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(LONG_COVER_COLUMNS)
        for row in rows:
            writer.writerow(
                [_format_long_value(name, getattr(row, name)) for name in LONG_COVER_COLUMNS]
            )


def save_survey_json(path: Path, document: Mapping[str, Any]) -> None:
    path.write_text(json.dumps(document, indent=2), encoding="utf-8")


def load_survey_json(path: Path) -> dict[str, Any]:
    doc = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(doc, dict):
        raise ValueError("Survey JSON must be a single object.")
    return doc
