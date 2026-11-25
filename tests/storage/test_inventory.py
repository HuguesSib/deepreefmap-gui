"""What one drive holds, described rather than discovered.

``classify_mount`` takes the facts as arguments, so these name a mount table and
a listing instead of building a filesystem.
"""

from __future__ import annotations

import uuid
from pathlib import Path

from deepreefmap_gui.storage.inventory import (
    KIND_ABORTED_RUN,
    KIND_DB_PARTIAL,
    KIND_DB_SET_ASIDE,
    KIND_ORPHAN_DIR,
    KIND_SCENE_TMP,
    KIND_UNKNOWN_FILE,
    classify_mount,
    clips_on,
)
from deepreefmap_gui.survey.catalogue import (
    LINK_LINKED,
    LINK_MISSING,
    RunEntry,
    VideoLibraryEntry,
)
from deepreefmap_gui.survey.models import RunRecord, TransectPass, VideoAsset

GB = 1024**3
MOUNTS = {"/", "/media/card"}
OUT_ROOT = Path("/home/evan/DeepReefMap")


def ismount(path: str) -> bool:
    return path in MOUNTS


def run_entry(dir_name: str, **overrides) -> RunEntry:
    fields = {
        "run_dir": OUT_ROOT / dir_name,
        "dir_name": dir_name,
        "manifest": {},
        "display_name": dir_name,
        "sort_key": 0.0,
        "video_hashes": [],
        "video_name": None,
        "begin_s": None,
        "end_s": None,
        "duration_s": None,
        "points": None,
        "manifest_run_id": None,
        "manifest_pass_id": None,
        "manifest_transect_id": None,
        "manifest_transect_name": None,
        "manifest_direction": None,
    }
    fields.update(overrides)
    return RunEntry(**fields)


def clip(path: str, *, succeeded: int = 1, passes: int = 1, link: str = LINK_LINKED):
    video = VideoAsset(file_name=Path(path).name, path=path, size_bytes=4 * GB)
    cut = [TransectPass(transect_id=None, video_id=video.id, begin_s=0.0, end_s=10.0) for _ in range(passes)]
    runs = [
        RunRecord(pass_id=cut[index].id, run_dir_name=f"r{index}", status="succeeded")
        for index in range(succeeded)
    ]
    return VideoLibraryEntry(
        video=video, pass_count=passes, run_count=len(runs),
        passes=cut, runs=runs, link_state=link,
    )


def test_only_the_clips_on_this_drive_are_listed() -> None:
    here = clip("/media/card/GX010042.MP4")
    elsewhere = clip("/home/evan/other.MP4")

    found = clips_on("/media/card", [here, elsewhere], ismount=ismount)
    assert [c.file_name for c in found] == ["GX010042.MP4"]


def test_a_clip_with_a_finished_run_may_go_and_one_without_may_not() -> None:
    (done,) = clips_on("/", [clip("/a.MP4", succeeded=1)], ismount=ismount)
    (owing,) = clips_on("/", [clip("/b.MP4", succeeded=0)], ismount=ismount)

    assert done.deletable
    assert not owing.deletable


def test_a_clip_on_an_unplugged_drive_is_never_offered() -> None:
    (gone,) = clips_on("/", [clip("/a.MP4", link=LINK_MISSING)], ismount=ismount)
    assert not gone.deletable


def test_a_rerun_does_not_read_as_more_sections_than_there_are() -> None:
    """run_count counts chapters and reruns, so it would say 5 of 4."""
    entry = clip("/a.MP4", succeeded=1, passes=2)
    entry.runs.append(
        RunRecord(pass_id=entry.passes[0].id, run_dir_name="rerun", status="succeeded")
    )
    (found,) = clips_on("/", [entry], ismount=ismount)

    assert (found.succeeded_passes, found.pass_count) == (1, 2)


def test_only_the_drive_holding_the_output_root_lists_runs() -> None:
    listing = [("20260520-155637", True, 4096)]
    card = classify_mount(
        "/media/card", OUT_ROOT, entries=[run_entry("20260520-155637")], clips=[],
        children=listing, breakdowns={}, ismount=ismount,
    )
    assert not card.holds_out_root
    assert card.runs == ()

    home = classify_mount(
        "/", OUT_ROOT, entries=[run_entry("20260520-155637")], clips=[],
        children=listing, breakdowns={}, ismount=ismount,
    )
    assert [r.dir_name for r in home.runs] == ["20260520-155637"]


def test_a_run_whose_directory_is_gone_is_not_listed_here() -> None:
    """Browse already carries data-removed records; this page lists what exists."""
    found = classify_mount(
        "/", OUT_ROOT, entries=[run_entry("gone", data_missing=True)], clips=[],
        children=[], breakdowns={}, ismount=ismount,
    )
    assert found.runs == ()


def test_residue_is_named_by_what_left_it_behind() -> None:
    children = [
        ("dive.scene.zarr.zip.tmp", False, 1024),
        ("survey.db.v6.bak.partial", False, 2048),
        ("survey.db.schema-v6", False, 4096),
        ("notes.txt", False, 12),
    ]
    found = classify_mount(
        "/", OUT_ROOT, entries=[], clips=[], children=children, breakdowns={}, ismount=ismount,
    )
    assert [item.kind for item in found.others] == [
        KIND_SCENE_TMP, KIND_DB_PARTIAL, KIND_DB_SET_ASIDE, KIND_UNKNOWN_FILE,
    ]


def test_the_database_and_its_completed_backups_are_never_offered() -> None:
    """The .bak set is the only way back from a bad upgrade, and is tiny."""
    children = [
        ("survey.db", False, 110_000),
        ("survey.db-wal", False, 4096),
        ("survey.db-shm", False, 32768),
        ("survey.db.v6.bak", False, 94_000),
    ]
    found = classify_mount(
        "/", OUT_ROOT, entries=[], clips=[], children=children, breakdowns={}, ismount=ismount,
    )
    assert found.others == ()


def test_a_folder_with_no_manifest_and_no_resume_key_is_an_aborted_run(tmp_path) -> None:
    aborted = tmp_path / "20260528-134250"
    (aborted / "frames").mkdir(parents=True)
    found = classify_mount(
        str(tmp_path), tmp_path, entries=[], clips=[],
        children=[("20260528-134250", True, 4096)], breakdowns={},
        ismount=lambda p: p == str(tmp_path),
    )
    assert [item.kind for item in found.others] == [KIND_ABORTED_RUN]


def test_a_folder_that_kept_its_resume_key_is_a_run_somebody_can_finish(tmp_path) -> None:
    crashed = tmp_path / "20260528-134808"
    (crashed / ".cache").mkdir(parents=True)
    (crashed / ".cache" / "preprocess.json").write_text("{}")
    found = classify_mount(
        str(tmp_path), tmp_path, entries=[], clips=[],
        children=[("20260528-134808", True, 4096)], breakdowns={},
        ismount=lambda p: p == str(tmp_path),
    )
    assert [item.kind for item in found.others] == [KIND_ORPHAN_DIR]


def test_a_path_whose_volume_cannot_be_told_is_dropped() -> None:
    assert clips_on("/", [clip("")], ismount=ismount) == ()


def test_an_unreadable_output_root_reports_nothing_and_raises_nothing(tmp_path) -> None:
    from deepreefmap_gui.storage.inventory import scan_children

    children, unmeasured = scan_children(tmp_path / "not-here")
    assert (children, unmeasured) == ([], 0)


def test_video_ids_survive_the_trip() -> None:
    entry = clip("/a.MP4")
    (found,) = clips_on("/", [entry], ismount=ismount)
    assert isinstance(found.video_id, uuid.UUID)
    assert found.video_id == entry.video.id


def test_an_expedition_root_lists_its_runs_and_never_its_transects(tmp_path) -> None:
    raw = tmp_path / "VC_6m_01" / "pass_01_control" / "bwd" / "RAW"
    raw.mkdir(parents=True)
    (raw / "GX010099.MP4").write_bytes(b"v")
    nested = "VC_6m_01/pass_01_control/bwd/RESULTS/Cam1__1a2b3c4d"
    (tmp_path / nested).mkdir(parents=True)
    (tmp_path / nested / "run_manifest.json").write_text("{}")
    entry = run_entry(nested, run_dir=tmp_path / nested)

    found = classify_mount(
        str(tmp_path), tmp_path, entries=[entry], clips=[],
        children=[("VC_6m_01", True, 4096)], breakdowns={},
        ismount=lambda p: p == str(tmp_path),
    )

    assert [run.dir_name for run in found.runs] == [nested]
    assert found.others == ()
