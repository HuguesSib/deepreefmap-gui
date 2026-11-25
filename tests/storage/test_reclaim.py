"""Deleting, and refusing to.

Every refusal is checked twice over: that it raises, and that nothing on disk
moved. A guard that raises after the unlink is not a guard.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from _factories import write_run_tree

from deepreefmap_gui.storage.inventory import (
    KIND_ABORTED_RUN,
    KIND_DB_PARTIAL,
    KIND_SCENE_TMP,
    MountClip,
    MountItem,
)
from deepreefmap_gui.storage.reclaim import (
    ReclaimError,
    delete_input_clip,
    delete_other,
    delete_run_folder,
    delete_tier,
)
from deepreefmap_gui.storage.tiers import (
    TIER_CACHE,
    TIER_KEEP,
    TIER_RESULTS,
    TIER_UNKNOWN,
    TIER_WORKING,
    measure_run,
)
from deepreefmap_gui.survey.models import VideoAsset
from deepreefmap_gui.survey.store import SurveyStore


@pytest.fixture
def out_root(tmp_path) -> Path:
    root = tmp_path / "DeepReefMap"
    root.mkdir()
    return root


def test_deleting_the_cache_leaves_the_run_openable(out_root) -> None:
    run_dir = write_run_tree(out_root)
    breakdown = measure_run(run_dir)

    freed = delete_tier(out_root, run_dir, TIER_CACHE, breakdown)

    assert freed.freed_bytes > 0
    assert measure_run(run_dir).openable
    assert not list(run_dir.glob("*.scene.zarr.zip"))


def test_deleting_the_working_data_leaves_the_manifest(out_root) -> None:
    run_dir = write_run_tree(out_root)

    delete_tier(out_root, run_dir, TIER_WORKING, measure_run(run_dir))

    after = measure_run(run_dir)
    assert not after.openable and not after.resumable
    assert (run_dir / "run_manifest.json").exists()
    assert after.tier_bytes(TIER_RESULTS) > 0


def test_the_whole_folder_goes_and_the_record_is_not_this_module_s_business(out_root) -> None:
    run_dir = write_run_tree(out_root)

    freed = delete_run_folder(out_root, run_dir)

    assert not run_dir.exists()
    assert freed.freed_bytes > 0


@pytest.mark.parametrize("tier", [TIER_KEEP, TIER_UNKNOWN, "made-up"])
def test_a_tier_that_is_not_offered_has_no_path_to_a_delete(out_root, tier) -> None:
    run_dir = write_run_tree(out_root)
    breakdown = measure_run(run_dir)

    with pytest.raises(ReclaimError):
        delete_tier(out_root, run_dir, tier, breakdown)
    assert (run_dir / "run_manifest.json").exists()


def test_a_run_outside_the_output_root_is_refused(out_root, tmp_path) -> None:
    stranger = write_run_tree(tmp_path, "somebody-elses")

    with pytest.raises(ValueError):
        delete_tier(out_root, stranger, TIER_CACHE, measure_run(stranger))
    assert stranger.exists()


def test_deleting_the_same_tier_twice_frees_nothing_and_raises_nothing(out_root) -> None:
    run_dir = write_run_tree(out_root)
    breakdown = measure_run(run_dir)

    delete_tier(out_root, run_dir, TIER_CACHE, breakdown)
    again = delete_tier(out_root, run_dir, TIER_CACHE, breakdown)

    assert again.freed_bytes == 0
    assert again.failures == ()


def test_a_file_written_after_the_scan_is_not_part_of_what_was_armed(out_root) -> None:
    run_dir = write_run_tree(out_root)
    breakdown = measure_run(run_dir)
    late = run_dir / "later.scene.zarr.zip"
    late.write_bytes(b"z")

    delete_tier(out_root, run_dir, TIER_CACHE, breakdown)

    assert late.exists()


def test_a_symlink_inside_a_run_is_unlinked_and_never_followed(out_root, tmp_path) -> None:
    keep = tmp_path / "keep"
    keep.mkdir()
    (keep / "precious.bin").write_bytes(b"x" * 4096)
    run_dir = write_run_tree(out_root)
    (run_dir / "videos").symlink_to(keep, target_is_directory=True)

    breakdown = measure_run(run_dir)
    # A symlinked entry is unknown, so no tier can reach it in the first place.
    assert "videos" in breakdown.tier_entries(TIER_UNKNOWN)
    delete_tier(out_root, run_dir, TIER_RESULTS, breakdown)
    assert (keep / "precious.bin").exists()


def test_a_scene_temp_being_written_right_now_is_refused(out_root, monkeypatch) -> None:
    tmp = out_root / "dive.scene.zarr.zip.tmp"
    tmp.write_bytes(b"z")
    item = MountItem(kind=KIND_SCENE_TMP, label=tmp.name, path=tmp, size_bytes=1)
    monkeypatch.setattr(
        "deepreefmap_gui.storage.reclaim.tmp_write_in_progress", lambda _: True
    )

    with pytest.raises(ReclaimError):
        delete_other(out_root, item)
    assert tmp.exists()


def test_an_abandoned_scene_temp_goes(out_root, monkeypatch) -> None:
    tmp = out_root / "dive.scene.zarr.zip.tmp"
    tmp.write_bytes(b"z")
    item = MountItem(kind=KIND_SCENE_TMP, label=tmp.name, path=tmp, size_bytes=1)
    monkeypatch.setattr(
        "deepreefmap_gui.storage.reclaim.tmp_write_in_progress", lambda _: False
    )

    assert delete_other(out_root, item).items == 1
    assert not tmp.exists()


def test_residue_outside_the_output_root_is_refused(out_root, tmp_path) -> None:
    stranger = tmp_path / "survey.db.v6.bak.partial"
    stranger.write_bytes(b"x")
    item = MountItem(kind=KIND_DB_PARTIAL, label=stranger.name, path=stranger, size_bytes=1)

    with pytest.raises(ReclaimError):
        delete_other(out_root, item)
    assert stranger.exists()


def test_an_aborted_run_folder_goes_through_the_run_guard(out_root, tmp_path) -> None:
    aborted = out_root / "20260528-134250"
    (aborted / "frames").mkdir(parents=True)
    (aborted / "frames" / "00000000.png").write_bytes(b"x" * 1024)
    item = MountItem(kind=KIND_ABORTED_RUN, label=aborted.name, path=aborted, size_bytes=4096)

    freed = delete_other(out_root, item)

    assert not aborted.exists()
    assert freed.freed_bytes > 0


# --- footage -----------------------------------------------------------------


def store_with(tmp_path, path: str) -> tuple[SurveyStore, VideoAsset]:
    store = SurveyStore(tmp_path / "survey.db")
    asset = VideoAsset(file_name=Path(path).name, path=path, size_bytes=8, hash="abc")
    store.upsert_video(asset)
    return store, asset


def mount_clip(asset: VideoAsset) -> MountClip:
    return MountClip(
        video_id=asset.id, file_name=asset.file_name, path=asset.path,
        size_bytes=asset.size_bytes, link_state="linked",
        pass_count=1, succeeded_passes=1,
    )


def test_the_file_goes_and_the_record_keeps_everything(tmp_path) -> None:
    footage = tmp_path / "GX010042.MP4"
    footage.write_bytes(b"x" * 8)
    store, asset = store_with(tmp_path, str(footage))

    freed = delete_input_clip(mount_clip(asset), store, confirmed_name="GX010042.MP4")

    assert not footage.exists()
    assert freed.freed_bytes == 8
    kept = store.get_video(asset.id)
    assert kept is not None
    assert (kept.hash, kept.size_bytes) == ("abc", 8)


def test_the_name_the_row_showed_has_to_be_repeated_back(tmp_path) -> None:
    footage = tmp_path / "GX010042.MP4"
    footage.write_bytes(b"x" * 8)
    store, asset = store_with(tmp_path, str(footage))

    with pytest.raises(ReclaimError):
        delete_input_clip(mount_clip(asset), store, confirmed_name="GX010099.MP4")
    assert footage.exists()


def test_a_clip_that_moved_since_the_page_was_drawn_is_refused(tmp_path) -> None:
    footage = tmp_path / "GX010042.MP4"
    footage.write_bytes(b"x" * 8)
    store, asset = store_with(tmp_path, str(footage))
    stale = MountClip(
        video_id=asset.id, file_name=asset.file_name, path=str(tmp_path / "elsewhere.MP4"),
        size_bytes=8, link_state="linked", pass_count=1, succeeded_passes=1,
    )

    with pytest.raises(ReclaimError):
        delete_input_clip(stale, store, confirmed_name="elsewhere.MP4")
    assert footage.exists()


def test_a_clip_the_library_no_longer_holds_is_refused(tmp_path) -> None:
    footage = tmp_path / "GX010042.MP4"
    footage.write_bytes(b"x" * 8)
    store, asset = store_with(tmp_path, str(footage))
    ghost = MountClip(
        video_id=uuid.uuid4(), file_name=asset.file_name, path=str(footage),
        size_bytes=8, link_state="linked", pass_count=1, succeeded_passes=1,
    )

    with pytest.raises(ReclaimError):
        delete_input_clip(ghost, store, confirmed_name="GX010042.MP4")
    assert footage.exists()


def test_a_symlinked_clip_is_refused(tmp_path) -> None:
    real = tmp_path / "real.MP4"
    real.write_bytes(b"x" * 8)
    link = tmp_path / "GX010042.MP4"
    link.symlink_to(real)
    store, asset = store_with(tmp_path, str(link))

    with pytest.raises(ReclaimError):
        delete_input_clip(mount_clip(asset), store, confirmed_name="GX010042.MP4")
    assert real.exists() and link.exists()


def test_a_directory_offered_as_a_clip_is_refused(tmp_path) -> None:
    folder = tmp_path / "GX010042.MP4"
    folder.mkdir()
    store, asset = store_with(tmp_path, str(folder))

    with pytest.raises(ReclaimError):
        delete_input_clip(mount_clip(asset), store, confirmed_name="GX010042.MP4")
    assert folder.is_dir()


def test_a_file_that_is_already_gone_is_refused_rather_than_reported_as_freed(tmp_path) -> None:
    store, asset = store_with(tmp_path, str(tmp_path / "GX010042.MP4"))

    with pytest.raises(ReclaimError):
        delete_input_clip(mount_clip(asset), store, confirmed_name="GX010042.MP4")


# --- an expedition folder as the output root --------------------------------


def _expedition(out_root: Path) -> tuple[Path, Path]:
    """A transect with footage, and a finished run in its pass's RESULTS."""
    transect = out_root / "VC_6m_01"
    raw = transect / "pass_01_control" / "bwd" / "RAW"
    raw.mkdir(parents=True)
    (raw / "GX010099.MP4").write_bytes(b"v" * 2048)
    run = write_run_tree(transect / "pass_01_control" / "bwd" / "RESULTS", "Cam1__1a2b3c4d")
    (run / "run_manifest.json").write_text("{}")
    return transect, run


def test_a_transect_folder_is_never_deletable_as_a_run(out_root) -> None:
    """Under an expedition root a transect is a direct child: it is the footage."""
    transect, _run = _expedition(out_root)
    item = MountItem(kind=KIND_ABORTED_RUN, label=transect.name, path=transect, size_bytes=4096)

    with pytest.raises(ValueError, match="footage"):
        delete_other(out_root, item)

    assert (transect / "pass_01_control" / "bwd" / "RAW" / "GX010099.MP4").exists()


def test_a_run_in_a_results_folder_can_be_removed(out_root) -> None:
    _transect, run = _expedition(out_root)

    freed = delete_run_folder(out_root, run)

    assert not run.exists() and freed.items == 1
    assert (out_root / "VC_6m_01" / "pass_01_control" / "bwd" / "RAW" / "GX010099.MP4").exists()


def test_a_plain_folder_in_results_is_not_a_run(out_root) -> None:
    """Somebody's own folder beside the runs carries no run files, so it stays."""
    _transect, _run = _expedition(out_root)
    notes = out_root / "VC_6m_01" / "pass_01_control" / "bwd" / "RESULTS" / "notes"
    notes.mkdir()

    with pytest.raises(ValueError):
        delete_run_folder(out_root, notes)

    assert notes.exists()


def test_deleting_a_nested_run_takes_its_record(out_root) -> None:
    from _factories import seed_pass

    from deepreefmap_gui.survey import catalogue
    from deepreefmap_gui.survey.models import RunRecord

    _transect, run = _expedition(out_root)
    store = SurveyStore(out_root / "survey.db")
    _t, _v, pass_ = seed_pass(store)
    record = RunRecord(pass_id=pass_.id, run_dir_name="VC_6m_01/pass_01_control/bwd/RESULTS/Cam1__1a2b3c4d")
    store.add_run(record)

    catalogue.delete_run_dir(out_root, run, store)

    assert store.run_by_dir_name(record.run_dir_name) is None
