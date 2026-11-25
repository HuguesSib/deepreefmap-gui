"""An expedition folder as a workspace: reading the tree, filing it, placing results."""

import json
from pathlib import Path

import pytest
from _factories import make_video, write_run

from deepreefmap_gui.survey import catalogue
from deepreefmap_gui.survey.completeness import CLIP, EXPEDITION, PASS, TRANSECT, capture_span, missing_data
from deepreefmap_gui.survey.expedition import (
    camera_from_name,
    depth_from_name,
    import_expedition,
    is_expedition_root,
    results_dir_for,
    scan_expedition,
)
from deepreefmap_gui.survey.run_dirs import manifest_paths, run_dir_candidates
from deepreefmap_gui.survey.store import SurveyStore

CLIP_SECONDS = 100.0


def _clip(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Distinct bytes per file: clips are identified by content hash.
    path.write_bytes(path.as_posix().encode() * 64)
    return path


def _probe(_path: Path) -> tuple[float, float]:
    return CLIP_SECONDS, 30.0


@pytest.fixture
def tree(tmp_path) -> Path:
    """Anders's layout, shrunk: two transects, passes with fwd/bwd, chapters, extras."""
    root = tmp_path / "2026_11_JO_EXPE"
    t6 = root / "VC_6m_01"
    _clip(t6 / "pass_01_control" / "fwd" / "GX010099.MP4")
    _clip(t6 / "pass_01_control" / "fwd" / "GX020099.MP4")  # second chapter
    _clip(t6 / "pass_01_control" / "fwd" / "GX010050.MP4")  # another camera
    _clip(t6 / "pass_01_control" / "bwd" / "VC_6m_01_pass01_bwd_Cam2_GX010051.MP4")
    _clip(t6 / "pass_02_angle" / "fwd" / "RAW" / "GX010100.MP4")
    _clip(t6 / "colour_charts" / "GX010090.MP4")
    (t6 / "pass_03_empty").mkdir()
    _clip(root / "VC_12m_01" / "pass_01_control" / "bwd" / "GX010200.MP4")
    _clip(root / "VC_tests_Hugues" / "GX010028.MP4")
    (root / "context").mkdir()
    return root


@pytest.fixture
def store(tree) -> SurveyStore:
    return SurveyStore(tree / "survey.db")


def test_names_carry_depth_and_camera():
    assert depth_from_name("VC_6m_01") == 6.0
    assert depth_from_name("VC_12m_01") == 12.0
    assert depth_from_name("VC_tests_Hugues") is None
    assert camera_from_name("VC_6m_01_pass01_bwd_Cam2_GX010051.MP4") == "Cam2"
    assert camera_from_name("GX010099.MP4") is None


def test_the_tree_is_read_without_touching_a_clip(tree):
    layout = scan_expedition(tree)

    assert layout.name == "2026_11_JO_EXPE"
    assert [t.name for t in layout.transects] == ["VC_12m_01", "VC_6m_01"]
    six = next(t for t in layout.transects if t.name == "VC_6m_01")
    assert six.depth_m == 6.0
    shapes = sorted((r.pass_dir, r.direction, tuple(p.name for p in r.clips)) for r in six.recordings)
    assert shapes == [
        ("pass_01_control", "forward", ("GX010050.MP4",)),
        ("pass_01_control", "forward", ("GX010099.MP4", "GX020099.MP4")),
        ("pass_01_control", "reverse", ("VC_6m_01_pass01_bwd_Cam2_GX010051.MP4",)),
        ("pass_02_angle", "forward", ("GX010100.MP4",)),
    ]
    assert list(six.other) == ["colour_charts"]
    assert "VC_tests_Hugues" in layout.loose
    assert any("pass_03_empty" in w for w in layout.warnings)
    assert is_expedition_root(tree)


def test_importing_files_the_expedition(tree, store):
    report = import_expedition(store, scan_expedition(tree), probe=_probe)

    assert report.campaign_created
    assert sorted(report.transects_created) == ["VC_12m_01", "VC_6m_01"]
    assert report.passes_created == 5
    campaign = store.list_campaigns()[0]
    assert campaign.name == "2026_11_JO_EXPE"
    assert store.default_campaign_id() == campaign.id
    transect = next(t for t in store.list_transects() if t.name == "VC_6m_01")
    assert transect.depth_m == 6.0

    passes = store.list_passes(transect_id=transect.id)
    chaptered = next(p for p in passes if p.extra_video_ids)
    assert chaptered.label == "pass_01_control"
    assert chaptered.direction == "forward"
    assert chaptered.campaign_id == campaign.id
    assert (chaptered.begin_s, chaptered.end_s) == (0.0, 2 * CLIP_SECONDS)
    named = next(p for p in passes if p.direction == "reverse")
    assert store.get_video(named.video_id).camera_label == "Cam2"
    # The folder above a clip is never taken for its camera.
    assert store.get_video(chaptered.video_id).camera_label is None
    # Only pass footage enters the library: colour charts and loose clips stay out.
    names = {v.file_name for v in store.list_videos()}
    assert "GX010090.MP4" not in names
    assert "GX010028.MP4" not in names
    assert report.skipped_other == 2


def test_importing_again_changes_nothing_and_keeps_a_trim(tree, store):
    layout = scan_expedition(tree)
    import_expedition(store, layout, probe=_probe)
    trimmed = store.list_passes()[0]
    trimmed.begin_s, trimmed.end_s = 12.0, 80.0
    store.update_pass(trimmed)

    again = import_expedition(store, layout, probe=_probe)

    assert not again.campaign_created
    assert again.transects_created == []
    assert again.clips_added == 0
    assert again.passes_created == 0
    assert again.passes_kept == 5
    assert len(store.list_passes()) == 5
    kept = store.get_pass(trimmed.id)
    assert (kept.begin_s, kept.end_s) == (12.0, 80.0)


def test_an_unreadable_chapter_files_no_half_pass(tree, store):
    def probe(path: Path):
        return None if path.name == "GX020099.MP4" else (CLIP_SECONDS, 30.0)

    report = import_expedition(store, scan_expedition(tree), probe=probe)

    assert report.passes_created == 4
    assert any(u.endswith("GX020099.MP4") for u in report.unreadable)


def test_results_go_beside_the_clips(tree, store):
    import_expedition(store, scan_expedition(tree), probe=_probe)
    named = next(v for v in store.list_videos() if v.camera_label == "Cam2")
    raw = next(v for v in store.list_videos() if v.file_name == "GX010100.MP4")
    loose = make_video("e" * 32, file_name="GX010028.MP4", path=str(tree / "VC_tests_Hugues" / "GX010028.MP4"))

    assert results_dir_for(tree, named, 1) == "VC_6m_01/pass_01_control/bwd/RESULTS/Cam2__r01"
    assert results_dir_for(tree, raw, 2) == "VC_6m_01/pass_02_angle/fwd/RESULTS/GX010100__r02"
    assert results_dir_for(tree, loose, 1) is None
    assert results_dir_for(tree.parent / "elsewhere", named, 1) is None


def test_runs_in_results_folders_are_found_and_named_by_their_path(tmp_path):
    root = tmp_path / "root"
    write_run(root, "flat__p01__abcd1234")
    nested = "VC_6m_01/pass_01_control/fwd/RESULTS/Cam1__r01"
    write_run(root, nested)
    (root / "VC_6m_01" / "pass_01_control" / "fwd" / "GX010099.MP4").write_bytes(b"x")

    names = sorted(entry.dir_name for entry in catalogue.scan_out_root(root))

    assert names == ["VC_6m_01/pass_01_control/fwd/RESULTS/Cam1__r01", "flat__p01__abcd1234"]
    assert len(manifest_paths(root)) == 2
    assert root / nested in run_dir_candidates(root)
    assert json.loads((root / nested / "run_manifest.json").read_text())["begin_s"] == 0.0


def test_missing_data_lists_what_the_folders_cannot_say(tree, store):
    import_expedition(store, scan_expedition(tree), probe=_probe)
    campaign = store.list_campaigns()[0]

    gaps = missing_data(store, campaign.id)

    by_level = {level: [g for g in gaps if g.level == level] for level in (EXPEDITION, TRANSECT, PASS, CLIP)}
    assert [g.item for g in by_level[EXPEDITION]] == ["start and end dates"]
    six = [g.item for g in by_level[TRANSECT] if g.subject == "VC_6m_01"]
    assert six == ["start and end coordinates", "tape length", "site"]  # depth came from the name
    assert sum("not trimmed" in g.item for g in by_level[PASS]) == 5
    assert sum(g.item == "not processed yet" for g in by_level[PASS]) == 5
    cameras = [g.subject for g in by_level[CLIP] if g.item == "camera"]
    assert "VC_6m_01_pass01_bwd_Cam2_GX010051.MP4" not in cameras
    assert "GX010099.MP4" in cameras


def test_the_expedition_dates_can_come_from_the_clips(tree, store):
    import_expedition(store, scan_expedition(tree), probe=_probe)
    campaign = store.list_campaigns()[0]
    in_passes = [store.get_video(p.video_id) for p in store.list_passes(campaign_id=campaign.id)[:2]]
    for day, video in zip(("2026-11-23", "2026-11-21"), in_passes, strict=True):
        video.captured_at = f"{day}T07:00:00+00:00"
        video.captured_source = "container"
        store.update_video(video)

    assert capture_span(store, campaign.id) == ("2026-11-21", "2026-11-23")


def test_the_batch_names_an_expedition_run_after_its_results_folder(tree, store):
    from types import SimpleNamespace

    from deepreefmap_gui.simple.batch import SimpleBatchMixin
    from deepreefmap_gui.survey.models import RunRecord

    import_expedition(store, scan_expedition(tree), probe=_probe)
    named = next(v for v in store.list_videos() if v.camera_label == "Cam2")
    pass_ = store.list_passes(video_id=named.id)[0]
    window = SimpleNamespace(
        _out_root_input=SimpleNamespace(text=lambda: str(tree)),
        _sanitize_run_name=lambda name: name,
    )

    first = SimpleBatchMixin._pass_dir_name(window, pass_, None, store)
    store.add_run(RunRecord(pass_id=pass_.id, run_dir_name=first, status="failed"))
    second = SimpleBatchMixin._pass_dir_name(window, pass_, None, store)

    assert first == "VC_6m_01/pass_01_control/bwd/RESULTS/Cam2__r01"
    assert second == "VC_6m_01/pass_01_control/bwd/RESULTS/Cam2__r02"


def test_non_pass_clips_an_earlier_import_added_are_taken_out_while_unused(tree, store):
    """Older builds imported colour charts and loose clips with a [role] tag."""
    chart = store.upsert_video(make_video(
        "c" * 32, file_name="GX010090.MP4", path=str(tree / "VC_6m_01" / "colour_charts" / "GX010090.MP4"),
        notes="[colour_charts]",
    ))
    loose = store.upsert_video(make_video(
        "d" * 32, file_name="GX010028.MP4", path=str(tree / "VC_tests_Hugues" / "GX010028.MP4"),
        notes="[VC_tests_Hugues]",
    ))
    # Someone has written about this one, so it is theirs now and stays.
    kept = store.upsert_video(make_video(
        "f" * 32, file_name="GX010091.MP4", path=str(tree / "VC_6m_01" / "context" / "GX010091.MP4"),
        notes="[context] drone shot of the reef edge",
    ))
    # Outside this expedition: not the import's to remove.
    elsewhere = store.upsert_video(make_video(
        "a1" * 16, file_name="X.MP4", path=str(tree.parent / "other" / "X.MP4"), notes="[context]",
    ))

    report = import_expedition(store, scan_expedition(tree), probe=_probe)

    assert report.removed_other == 2
    live = {v.id for v in store.list_videos()}
    assert chart.id not in live and loose.id not in live
    assert kept.id in live and elsewhere.id in live
    assert (tree / "VC_6m_01" / "colour_charts" / "GX010090.MP4").exists()  # files untouched
    assert report.passes_created == 5


def test_a_copy_in_a_second_pass_folder_is_named_not_filed(tree, store):
    original = tree / "VC_6m_01" / "pass_02_angle" / "fwd" / "RAW" / "GX010100.MP4"
    copy = tree / "VC_12m_01" / "pass_01_control" / "bwd" / "GX010100.MP4"
    copy.write_bytes(original.read_bytes())

    first = import_expedition(store, scan_expedition(tree), probe=_probe)
    clip = next(v for v in store.list_videos() if v.file_name == "GX010100.MP4")
    kept_at = clip.path
    again = import_expedition(store, scan_expedition(tree), probe=_probe)

    # A fresh import cannot know which file is the original: the first one read
    # is filed and the other named, and from then on the filed one stays put.
    pair = {str(copy), str(original)}
    for report in (first, again):
        assert len(report.duplicates) == 1
        assert set(report.duplicates[0]) == pair
        assert report.duplicates[0][1] == kept_at
        assert report.clips_relinked == 0
    assert store.get_video(clip.id).path == kept_at
    assert len(store.list_passes(video_id=clip.id)) == 1


def test_a_clip_that_really_moved_is_relinked(tree, store):
    import_expedition(store, scan_expedition(tree), probe=_probe)
    old = tree / "VC_6m_01" / "pass_02_angle" / "fwd" / "RAW" / "GX010100.MP4"
    new = tree / "VC_6m_01" / "pass_02_angle" / "fwd" / "GX010100.MP4"
    old.rename(new)

    report = import_expedition(store, scan_expedition(tree), probe=_probe)

    assert report.clips_relinked == 1
    assert report.duplicates == []
    clip = next(v for v in store.list_videos() if v.file_name == "GX010100.MP4")
    assert clip.path == str(new)
