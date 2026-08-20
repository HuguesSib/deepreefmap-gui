"""Transect imports: quick coordinate text entry, CSV, and GPX."""

from __future__ import annotations

import csv
import re
import uuid
from pathlib import Path
from xml.etree import ElementTree

from deepreefmap_gui.survey.models.transect import Transect

_CSV_REQUIRED = {"name", "start_lat", "start_lon", "end_lat", "end_lon"}

_HEMISPHERE_SIGN = {"N": 1.0, "S": -1.0, "E": 1.0, "W": -1.0}
# One coordinate ending in a hemisphere letter: digits and the usual degree,
# minute and second punctuation, then N/S/E/W. Lazy, so it stops at the first
# hemisphere letter and never swallows the second coordinate.
_COMPONENT_RE = re.compile(r"(\d[\d\s.,°'\"]*?)\s*([NSEW])", re.IGNORECASE)
_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")


def parse_latlon(text: str) -> tuple[float, float]:
    """Parse a coordinate pair off a GPS, pasted or typed.

    Accepts decimal degrees ("lat lon" or "lat, lon") and hemisphere forms in
    degrees decimal minutes or degrees/minutes/seconds, such as
    "17°30.512'S 149°49.104'W" or "17 30.512 S 149 49.104 W".
    """
    pair = _parse_hemisphere_pair(text)
    lat, lon = pair if pair is not None else _parse_decimal_pair(text)
    if not -90.0 <= lat <= 90.0:
        raise ValueError(f"Latitude out of range: {lat}")
    if not -180.0 <= lon <= 180.0:
        raise ValueError(f"Longitude out of range: {lon}")
    return lat, lon


def build_transect(
    name: str,
    start_text: str,
    end_text: str,
    length_m: float | None = None,
    depth_m: float | None = None,
    description: str = "",
    site_id: uuid.UUID | None = None,
) -> Transect:
    """Validate and build a transect from typed fields, naming the field at fault.

    Shared by the Transects form and the new-transect dialog so their
    validation cannot drift. Both endpoints are required: inventing 0,0 would
    file the transect in the Gulf of Guinea.
    """
    coords: list[float] = []
    for which, raw in (("start", start_text), ("end", end_text)):
        cleaned = raw.strip()
        if not cleaned:
            raise ValueError(f"Missing {which} point")
        try:
            coords.extend(parse_latlon(cleaned))
        except ValueError as exc:
            raise ValueError(f"{which.capitalize()} point: {exc}") from None
    return Transect(
        name=name.strip(),
        start_lat=coords[0],
        start_lon=coords[1],
        end_lat=coords[2],
        end_lon=coords[3],
        length_m=length_m or None,
        depth_m=depth_m or None,
        description=description,
        site_id=site_id,
    )


def _parse_decimal_pair(text: str) -> tuple[float, float]:
    parts = text.replace(",", " ").split()
    if len(parts) != 2:
        raise ValueError(f"Expected 'lat lon', got: {text!r}")
    return float(parts[0]), float(parts[1])


def _parse_hemisphere_pair(text: str) -> tuple[float, float] | None:
    """A lat/lon pair with N/S/E/W markers, or None when the text carries none.

    Returning None lets the decimal parser handle plain (and negative) degrees;
    this path only claims text that actually names its hemispheres.
    """
    matches = _COMPONENT_RE.findall(text)
    if len(matches) != 2:
        return None
    lat: float | None = None
    lon: float | None = None
    for numbers, hemisphere in matches:
        value = _dms_to_degrees(numbers, hemisphere.upper())
        if hemisphere.upper() in ("N", "S"):
            if lat is not None:
                raise ValueError(f"Two latitude values in: {text!r}")
            lat = value
        elif lon is not None:
            raise ValueError(f"Two longitude values in: {text!r}")
        else:
            lon = value
    if lat is None or lon is None:
        raise ValueError(f"Expected one N/S and one E/W value in: {text!r}")
    return lat, lon


def _dms_to_degrees(numbers: str, hemisphere: str) -> float:
    """Fold degrees, optional minutes and optional seconds into signed degrees."""
    parts = _NUMBER_RE.findall(numbers)
    if not parts:
        raise ValueError(f"No digits in coordinate: {numbers!r}")
    degrees = float(parts[0])
    minutes = float(parts[1]) if len(parts) > 1 else 0.0
    seconds = float(parts[2]) if len(parts) > 2 else 0.0
    if minutes >= 60.0 or seconds >= 60.0:
        raise ValueError(f"Minutes and seconds must be under 60: {numbers!r}")
    return _HEMISPHERE_SIGN[hemisphere] * (degrees + minutes / 60.0 + seconds / 3600.0)


def import_transects_csv(path: Path) -> list[Transect]:
    """Read transects from a CSV with case-insensitive headers.

    Required columns: name, start_lat, start_lon, end_lat, end_lon.
    Optional: length_m, depth_m, description, id (a UUID kept for round-trips).
    """
    with path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None:
            raise ValueError("CSV has no header row.")
        norm = {fn.strip().lower(): fn for fn in reader.fieldnames}
        missing = _CSV_REQUIRED - set(norm)
        if missing:
            raise ValueError(f"CSV is missing required columns: {', '.join(sorted(missing))}")
        transects = []
        for n, row in enumerate(reader, start=2):
            name = _cell(row, norm, "name")
            if not name:
                continue
            try:
                transects.append(_transect_from_csv_row(row, norm, name))
            except ValueError as exc:
                raise ValueError(f"Row {n}: {exc}") from exc
    if not transects:
        raise ValueError("No usable rows in CSV.")
    return transects


def _cell(row: dict[str, str], norm: dict[str, str], key: str) -> str:
    if key not in norm:
        return ""
    return (row.get(norm[key], "") or "").strip()


def _optional_float(raw: str) -> float | None:
    return float(raw) if raw else None


def _transect_from_csv_row(row: dict[str, str], norm: dict[str, str], name: str) -> Transect:
    transect = Transect(
        name=name,
        start_lat=float(_cell(row, norm, "start_lat")),
        start_lon=float(_cell(row, norm, "start_lon")),
        end_lat=float(_cell(row, norm, "end_lat")),
        end_lon=float(_cell(row, norm, "end_lon")),
        length_m=_optional_float(_cell(row, norm, "length_m")),
        depth_m=_optional_float(_cell(row, norm, "depth_m")),
        description=_cell(row, norm, "description"),
    )
    raw_id = _cell(row, norm, "id")
    if raw_id:
        transect.id = uuid.UUID(raw_id)
    return transect


def import_transects_gpx(path: Path) -> list[Transect]:
    """Read transects from a GPX file.

    Each track or route becomes a transect from its first to its last point;
    bare waypoints pair up in file order (start, end, start, end, ...).
    """
    try:
        root = ElementTree.parse(path).getroot()
    except ElementTree.ParseError as exc:
        raise ValueError(f"Not a valid GPX file: {exc}") from exc
    transects = []
    for segment_tag, point_tag in (("trk", "trkpt"), ("rte", "rtept")):
        for i, segment in enumerate(root.findall(f".//{{*}}{segment_tag}"), start=1):
            points = segment.findall(f".//{{*}}{point_tag}")
            if len(points) < 2:
                continue
            name = _gpx_name(segment) or f"{path.stem} {segment_tag} {i}"
            transects.append(_transect_from_gpx_points(name, points[0], points[-1]))
    waypoints = root.findall(".//{*}wpt")
    for i in range(0, len(waypoints) - 1, 2):
        name = _gpx_name(waypoints[i]) or f"{path.stem} pair {i // 2 + 1}"
        transects.append(_transect_from_gpx_points(name, waypoints[i], waypoints[i + 1]))
    if not transects:
        raise ValueError("No tracks, routes, or waypoint pairs in GPX file.")
    return transects


def _gpx_name(element: ElementTree.Element) -> str:
    node = element.find("{*}name")
    return (node.text or "").strip() if node is not None else ""


def _transect_from_gpx_points(
    name: str, start: ElementTree.Element, end: ElementTree.Element
) -> Transect:
    def coords(point: ElementTree.Element) -> tuple[float, float]:
        try:
            return float(point.attrib["lat"]), float(point.attrib["lon"])
        except (KeyError, ValueError) as exc:
            raise ValueError(f"GPX point in {name!r} has no valid lat/lon") from exc

    start_lat, start_lon = coords(start)
    end_lat, end_lon = coords(end)
    return Transect(
        name=name,
        start_lat=start_lat,
        start_lon=start_lon,
        end_lat=end_lat,
        end_lon=end_lon,
    )
