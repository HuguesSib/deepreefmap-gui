import csv
import uuid

import pytest

from deepreefmap_gui.survey.models import Transect
from deepreefmap_gui.survey.models.exporters import save_transects_csv
from deepreefmap_gui.survey.models.importers import (
    build_transect,
    import_transects_csv,
    import_transects_gpx,
    parse_latlon,
)

GPX = """<?xml version="1.0" encoding="UTF-8"?>
<gpx version="1.1" xmlns="http://www.topografix.com/GPX/1/1">
  <wpt lat="-17.51" lon="177.11"><name>T2 start</name></wpt>
  <wpt lat="-17.512" lon="177.112"></wpt>
  <trk>
    <name>Reef flat</name>
    <trkseg>
      <trkpt lat="-17.5" lon="177.1"></trkpt>
      <trkpt lat="-17.5002" lon="177.1002"></trkpt>
      <trkpt lat="-17.5005" lon="177.1005"></trkpt>
    </trkseg>
  </trk>
</gpx>
"""


def test_parse_latlon_accepts_space_and_comma():
    assert parse_latlon("-17.5 177.1") == (-17.5, 177.1)
    assert parse_latlon("-17.5, 177.1") == (-17.5, 177.1)
    assert parse_latlon("-17.5,177.1") == (-17.5, 177.1)


def test_build_transect_names_the_field_at_fault():
    with pytest.raises(ValueError, match="Missing end point"):
        build_transect("T9", "-17.5, 177.1", "")
    with pytest.raises(ValueError, match="Start point"):
        build_transect("T9", "not coords", "-17.5, 177.1")


def test_build_transect_reads_zero_length_and_depth_as_unset():
    transect = build_transect(
        " T9 ", "-17.5, 177.1", "-17.5005, 177.1005", length_m=0.0, depth_m=0.0
    )
    assert transect.name == "T9"
    assert transect.length_m is None
    assert transect.depth_m is None


def test_parse_latlon_rejects_garbage():
    with pytest.raises(ValueError):
        parse_latlon("-17.5")
    with pytest.raises(ValueError):
        parse_latlon("95 10")
    with pytest.raises(ValueError):
        parse_latlon("ten twenty")


def test_parse_latlon_accepts_degrees_decimal_minutes():
    lat, lon = parse_latlon("17°30.512'S 149°49.104'W")
    assert lat == pytest.approx(-(17 + 30.512 / 60))
    assert lon == pytest.approx(-(149 + 49.104 / 60))


def test_parse_latlon_accepts_space_separated_ddm():
    lat, lon = parse_latlon("17 30.512 S 149 49.104 W")
    assert lat == pytest.approx(-(17 + 30.512 / 60))
    assert lon == pytest.approx(-(149 + 49.104 / 60))


def test_parse_latlon_accepts_dms_with_seconds():
    lat, lon = parse_latlon("17°30'30\"S 149°49'6\"E")
    assert lat == pytest.approx(-(17 + 30 / 60 + 30 / 3600))
    assert lon == pytest.approx(149 + 49 / 60 + 6 / 3600)


def test_parse_latlon_hemisphere_order_independent():
    # Lon-first still assigns by hemisphere, so a swapped paste is not silently wrong.
    assert parse_latlon("149°49.104'W 17°30.512'S") == parse_latlon("17°30.512'S 149°49.104'W")


def test_parse_latlon_northern_hemisphere_stays_positive():
    lat, lon = parse_latlon("40°26.767'N 79°58.933'W")
    assert lat == pytest.approx(40 + 26.767 / 60)
    assert lon == pytest.approx(-(79 + 58.933 / 60))


def test_parse_latlon_rejects_two_of_the_same_hemisphere():
    with pytest.raises(ValueError):
        parse_latlon("17°30'S 18°10'S")


def test_parse_latlon_rejects_minutes_over_sixty():
    with pytest.raises(ValueError):
        parse_latlon("17°75.0'S 149°10.0'W")


def test_csv_import_case_insensitive_headers(tmp_path):
    path = tmp_path / "transects.csv"
    path.write_text(
        "Name,Start_Lat,Start_Lon,End_Lat,End_Lon,Length_M\n"
        "T1,-17.5,177.1,-17.5005,177.1005,50\n"
        ",,,,,\n"
    )
    transects = import_transects_csv(path)
    assert len(transects) == 1
    assert transects[0].name == "T1"
    assert transects[0].length_m == 50.0
    assert transects[0].depth_m is None


def test_csv_import_reports_row_number_on_error(tmp_path):
    path = tmp_path / "transects.csv"
    path.write_text(
        "name,start_lat,start_lon,end_lat,end_lon\n"
        "T1,-17.5,177.1,-17.5005,177.1005\n"
        "T2,not-a-number,177.1,-17.5,177.1\n"
    )
    with pytest.raises(ValueError, match="Row 3"):
        import_transects_csv(path)


def test_csv_import_rejects_a_missing_name_column(tmp_path):
    path = tmp_path / "transects.csv"
    path.write_text("start_lat,start_lon\n-17.5,177.1\n")
    with pytest.raises(ValueError, match="name"):
        import_transects_csv(path)


def test_csv_import_rejects_half_an_end_point(tmp_path):
    path = tmp_path / "transects.csv"
    path.write_text("name,start_lat\nT1,-17.5\n")
    with pytest.raises(ValueError, match="Row 2.*both a latitude"):
        import_transects_csv(path)


def test_csv_import_takes_a_line_with_no_end_points(tmp_path):
    path = tmp_path / "transects.csv"
    path.write_text("name,length_m\nT1,50\n")
    [transect] = import_transects_csv(path)
    assert (transect.name, transect.length_m, transect.end_points()) == ("T1", 50.0, None)


def test_csv_round_trip_preserves_ids(tmp_path):
    original = Transect(
        name="T1",
        start_lat=-17.5,
        start_lon=177.1,
        end_lat=-17.5005,
        end_lon=177.1005,
        depth_m=8.0,
        description="north edge",
    )
    path = tmp_path / "transects.csv"
    save_transects_csv(path, [original])
    imported = import_transects_csv(path)[0]
    assert imported.id == original.id
    assert imported.name == original.name
    assert imported.length_m is None
    assert imported.depth_m == 8.0
    assert imported.description == "north edge"


def test_gpx_import_reads_tracks_and_waypoint_pairs(tmp_path):
    path = tmp_path / "site.gpx"
    path.write_text(GPX)
    transects = import_transects_gpx(path)
    by_name = {t.name: t for t in transects}
    track = by_name["Reef flat"]
    assert (track.start_lat, track.start_lon) == (-17.5, 177.1)
    assert (track.end_lat, track.end_lon) == (-17.5005, 177.1005)
    pair = by_name["T2 start"]
    assert (pair.end_lat, pair.end_lon) == (-17.512, 177.112)
    assert all(isinstance(t.id, uuid.UUID) for t in transects)


def test_gpx_import_rejects_non_gpx(tmp_path):
    path = tmp_path / "junk.gpx"
    path.write_text("not xml at all")
    with pytest.raises(ValueError):
        import_transects_gpx(path)


def test_repeatability_csv_carries_stats_then_one_column_per_pass(tmp_path):
    """The analysis export: stats first, then the raw fraction behind each number.

    Reviewers read this to see whether a wide range is one outlier pass or real
    spread, so the per-pass columns have to line up with the covers in order.
    """
    from types import SimpleNamespace

    from deepreefmap_gui.survey.models.exporters import save_repeatability_csv

    covers = [
        SimpleNamespace(run_dir_name="t1__p01", cover={"coral": 0.30, "sand": 0.70}),
        SimpleNamespace(run_dir_name="t1__p02", cover={"coral": 0.50}),  # sand unmeasured
    ]
    stats = {"coral": {"mean": 0.4, "std": 0.1414, "cv": 0.3536, "range": 0.2}}

    path = tmp_path / "repeatability.csv"
    save_repeatability_csv(path, ["coral", "sand"], stats, covers)

    rows = list(csv.DictReader(path.open()))
    assert list(rows[0]) == ["class", "mean_fraction", "std", "cv", "range", "t1__p01", "t1__p02"]

    coral = rows[0]
    assert coral["class"] == "coral"
    assert coral["mean_fraction"] == "0.400000"
    assert coral["cv"] == "0.3536"          # cv is 4dp, the fractions are 6dp
    assert (coral["t1__p01"], coral["t1__p02"]) == ("0.300000", "0.500000")

    # A label with no stats entry and a pass that never saw it both read as zero
    # rather than blank, so the column stays numeric.
    sand = rows[1]
    assert sand["mean_fraction"] == "0.000000"
    assert (sand["t1__p01"], sand["t1__p02"]) == ("0.700000", "0.000000")


def test_repeatability_csv_with_no_passes_still_writes_a_header(tmp_path):
    from deepreefmap_gui.survey.models.exporters import save_repeatability_csv

    path = tmp_path / "repeatability.csv"
    save_repeatability_csv(path, [], {}, [])
    assert path.read_text().strip() == "class,mean_fraction,std,cv,range"


def test_long_format_csv_writes_per_pass_and_pooled_rows(tmp_path):
    """The collated export is a published artefact: precision and blanks are contract."""
    from deepreefmap_gui.survey.analysis import LONG_COVER_COLUMNS, LongCoverRow
    from deepreefmap_gui.survey.models.exporters import save_long_format_csv

    common = {
        "transect_name": "T1",
        "transect_id": "tid",
        "level": "fine",
        "group": "coral alive",
        "contributing_passes": 2,
        "expected_passes": 3,
        "gui_version": "0.1.0",
        "taxonomy_version": 1,
        "taxonomy_hash": "abc123",
    }
    per_pass = LongCoverRow(
        **common, estimator="per_pass", fraction=0.9, count=90.0, denominator=100.0,
        pass_id="pid", direction="forward", begin_s=0.0, end_s=60.0,
        run_id="rid", run_dir_name="run_a", deepreefmap_version="2.0.0",
        segmentation_model="segformer-b2", mapping_backend="scsfmlearner",
    )
    pooled = LongCoverRow(
        **common, estimator="pooled", fraction=0.0909, count=100.0, denominator=1100.0,
        pass_id="", direction="", begin_s=None, end_s=None,
        run_id="", run_dir_name="", deepreefmap_version="",
        segmentation_model="", mapping_backend="",
    )

    path = tmp_path / "long.csv"
    save_long_format_csv(path, [per_pass, pooled])
    rows = list(csv.DictReader(path.open()))

    assert list(rows[0]) == LONG_COVER_COLUMNS
    assert rows[0]["estimator"] == "per_pass"
    assert rows[0]["fraction"] == "0.900000"       # 6dp
    assert rows[0]["count"] == "90.0000"           # 4dp
    assert rows[0]["expected_passes"] == "3"
    # Pooled row leaves the pass-only columns blank rather than zero.
    assert rows[1]["estimator"] == "pooled"
    assert rows[1]["begin_s"] == ""
    assert rows[1]["pass_id"] == ""


def test_long_format_csv_with_no_rows_still_writes_a_header(tmp_path):
    from deepreefmap_gui.survey.analysis import LONG_COVER_COLUMNS
    from deepreefmap_gui.survey.models.exporters import save_long_format_csv

    path = tmp_path / "long.csv"
    save_long_format_csv(path, [])
    assert path.read_text().strip() == ",".join(LONG_COVER_COLUMNS)


def test_build_transect_fills_a_blank_depth_from_the_ends():
    transect = build_transect("T1", "", "", start_depth_m=5.0, end_depth_m=11.0)
    assert transect.start_depth_m == 5.0
    assert transect.end_depth_m == 11.0
    assert transect.depth_m == 8.0  # (5 + 11) / 2


def test_build_transect_keeps_a_typed_depth_over_the_ends():
    transect = build_transect("T1", "", "", depth_m=7.0, start_depth_m=5.0, end_depth_m=11.0)
    assert transect.depth_m == 7.0


def test_build_transect_leaves_depth_unset_with_one_end():
    transect = build_transect("T1", "", "", start_depth_m=5.0)
    assert transect.depth_m is None
    assert transect.end_depth_m is None


def test_transect_csv_round_trips_the_end_depths(tmp_path):
    original = Transect(name="T2", depth_m=8.0, start_depth_m=5.0, end_depth_m=11.0)
    path = tmp_path / "transects.csv"
    save_transects_csv(path, [original])
    imported = import_transects_csv(path)[0]
    assert imported.start_depth_m == 5.0
    assert imported.end_depth_m == 11.0
    assert imported.depth_m == 8.0
