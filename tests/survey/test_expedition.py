"""An expedition folder as a workspace: reading the tree, filing it, placing results."""

import json
import uuid
from pathlib import Path

import pytest
from _factories import make_transect, make_video, write_run

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

    pid = uuid.UUID("1a2b3c4d" + "0" * 24)
    assert results_dir_for(tree, named, pid) == "VC_6m_01/pass_01_control/bwd/RESULTS/Cam2__1a2b3c4d"
    assert results_dir_for(tree, raw, pid) == "VC_6m_01/pass_02_angle/fwd/RESULTS/GX010100__1a2b3c4d"
    assert results_dir_for(tree, loose, pid) is None
    assert results_dir_for(tree.parent / "elsewhere", named, pid) is None


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
    assert not any(c.endswith("_Cam2_GX010051.MP4") for c in cameras)
    # Named by where they were swum, so same-named clips can be told apart.
    assert "VC_6m_01 · pass_01_control · forward · GX010099.MP4" in cameras


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

    tag = pass_.id.hex[:8]
    assert first == f"VC_6m_01/pass_01_control/bwd/RESULTS/Cam2__{tag}"
    assert second == f"VC_6m_01/pass_01_control/bwd/RESULTS/Cam2__{tag}__r02"


def test_two_passes_from_one_camera_never_share_a_run_folder(tree, store):
    """Two windows cut from one recording: each counts its own attempts."""
    from types import SimpleNamespace

    from deepreefmap_gui.simple.batch import SimpleBatchMixin
    from deepreefmap_gui.survey.models import TransectPass

    import_expedition(store, scan_expedition(tree), probe=_probe)
    named = next(v for v in store.list_videos() if v.camera_label == "Cam2")
    whole = store.list_passes(video_id=named.id)[0]
    second_window = TransectPass(
        transect_id=whole.transect_id, video_id=named.id, begin_s=50.0, end_s=90.0, direction="reverse"
    )
    store.add_pass(second_window)
    window = SimpleNamespace(
        _out_root_input=SimpleNamespace(text=lambda: str(tree)),
        _sanitize_run_name=lambda name: name,
    )

    names = {SimpleBatchMixin._pass_dir_name(window, p, None, store) for p in (whole, second_window)}

    assert len(names) == 2


def test_a_pass_with_flat_runs_keeps_its_runs_together(tree, store):
    """Runs made before the import stay where their first attempt was."""
    from types import SimpleNamespace

    from deepreefmap_gui.simple.batch import SimpleBatchMixin
    from deepreefmap_gui.survey.models import RunRecord

    import_expedition(store, scan_expedition(tree), probe=_probe)
    pass_ = store.list_passes()[0]
    store.add_run(RunRecord(pass_id=pass_.id, run_dir_name="VC_6m_01__p01__045876c3", status="succeeded"))
    window = SimpleNamespace(
        _out_root_input=SimpleNamespace(text=lambda: str(tree)),
        _sanitize_run_name=lambda name: name,
    )

    assert SimpleBatchMixin._pass_dir_name(window, pass_, None, store) == "VC_6m_01__p01__045876c3__r02"


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


def test_an_old_run_copied_into_results_joins_the_imported_records(tree, store):
    """The drive's case: runs made before the import, copied into RESULTS, carry
    their own ids for the campaign, transect and pass. Rebuilding must file them
    under the imported records rather than fail on the names they share."""
    from deepreefmap_gui.survey.models import RunRecord, TransectPass
    from deepreefmap_gui.survey.models.campaign import Campaign
    from deepreefmap_gui.survey.models.convert import survey_manifest_block

    import_expedition(store, scan_expedition(tree), probe=_probe)
    clip = next(v for v in store.list_videos() if v.camera_label == "Cam2")
    imported = store.list_passes(video_id=clip.id)[0]

    old_campaign = Campaign(name="2026_11_jo_expe")  # same expedition, other id and case
    old_transect = make_transect("VC_6m_01")
    old_pass = TransectPass(
        transect_id=old_transect.id, video_id=clip.id, begin_s=12.0, end_s=80.0,
        direction="reverse", campaign_id=old_campaign.id,
    )
    old_run = RunRecord(pass_id=old_pass.id, run_dir_name="VC_6m_01__p01__045876c3", status="succeeded")
    nested = "VC_6m_01/pass_01_control/bwd/RESULTS/VC_6m_01__p01__045876c3"
    write_run(
        tree, nested,
        input_videos=[clip.path], video_hashes=[clip.hash], begin_s=12.0, end_s=80.0,
        survey=survey_manifest_block(old_run, old_pass, old_transect, None, campaign=old_campaign),
    )

    report = store.rebuild_from_scan(tree)

    assert report.runs == 1 and not report.skipped
    assert len(store.list_campaigns()) == 1
    assert sorted(t.name for t in store.list_transects()) == ["VC_12m_01", "VC_6m_01"]
    passes = store.list_passes(video_id=clip.id)
    assert [p.id for p in passes] == [imported.id]  # no second pass beside it
    assert (passes[0].begin_s, passes[0].end_s) == (12.0, 80.0)  # the run's trim
    assert [r.run_dir_name for r in store.runs_for_pass(imported.id)] == [nested]
    assert store.rebuild_from_scan(tree).runs == 0  # and once only


def test_an_old_run_with_a_site_joins_the_imported_site_less_transect(tree, store):
    """The import makes transects without a site; a run that knew one fills it in."""
    from deepreefmap_gui.survey.models import RunRecord, Site, TransectPass
    from deepreefmap_gui.survey.models.campaign import Campaign
    from deepreefmap_gui.survey.models.convert import survey_manifest_block

    import_expedition(store, scan_expedition(tree), probe=_probe)
    clip = next(v for v in store.list_videos() if v.camera_label == "Cam2")
    imported = store.list_passes(video_id=clip.id)[0]
    site = Site(name="Aqaba", country="Jordan")
    old_transect = make_transect("VC_6m_01", site_id=site.id)
    old_pass = TransectPass(
        transect_id=old_transect.id, video_id=clip.id, begin_s=10.0, end_s=70.0, direction="reverse"
    )
    old_run = RunRecord(pass_id=old_pass.id, run_dir_name="x", status="succeeded")
    write_run(
        tree, "VC_6m_01/pass_01_control/bwd/RESULTS/old",
        input_videos=[clip.path], video_hashes=[clip.hash],
        survey=survey_manifest_block(
            old_run, old_pass, old_transect, None, site=site, campaign=Campaign(name="2026_11_JO_EXPE")
        ),
    )

    store.rebuild_from_scan(tree)

    six = [t for t in store.list_transects() if t.name == "VC_6m_01"]
    assert len(six) == 1 and six[0].site_id == site.id
    assert [p.id for p in store.list_passes(video_id=clip.id)] == [imported.id]


def test_a_note_in_brackets_written_by_a_person_is_not_taken_for_a_tag(tree, store):
    noted = store.upsert_video(make_video(
        "b1" * 16, file_name="GX010092.MP4", path=str(tree / "VC_6m_01" / "context" / "GX010092.MP4"),
        notes="[blurry]",
    ))

    report = import_expedition(store, scan_expedition(tree), probe=_probe)

    assert report.removed_other == 0
    assert noted.id in {v.id for v in store.list_videos()}


def test_a_transect_this_expedition_already_uses_is_preferred(tree, store):
    """T names repeat across trips: the line this expedition files under wins."""
    from deepreefmap_gui.survey.models import Site, TransectPass
    from deepreefmap_gui.survey.models.campaign import Campaign

    import_expedition(store, scan_expedition(tree), probe=_probe)
    ours = next(t for t in store.list_transects() if t.name == "VC_12m_01")
    other_trip = Campaign(name="2025_10_eritrea")
    store.add_campaign(other_trip)
    elsewhere = Site(name="Massawa", country="Eritrea")
    store.add_site(elsewhere)
    theirs = make_transect("VC_12m_01", site_id=elsewhere.id)
    store.add_transect(theirs)
    clip = store.upsert_video(make_video("c2" * 16, path="/elsewhere/X.MP4"))
    store.add_pass(
        TransectPass(transect_id=theirs.id, video_id=clip.id, begin_s=0, end_s=10, campaign_id=other_trip.id)
    )
    (tree / "VC_12m_01" / "pass_02_new" / "fwd").mkdir(parents=True)
    _clip(tree / "VC_12m_01" / "pass_02_new" / "fwd" / "GX010300.MP4")

    report = import_expedition(store, scan_expedition(tree), probe=_probe)

    new_pass = next(p for p in store.list_passes() if p.label == "pass_02_new")
    assert new_pass.transect_id == ours.id
    assert report.shared_transects == []


def test_a_name_another_expedition_holds_without_a_site_is_shared_and_said(tree, store):
    from deepreefmap_gui.survey.models import TransectPass
    from deepreefmap_gui.survey.models.campaign import Campaign

    other_trip = Campaign(name="2025_10_eritrea")
    store.add_campaign(other_trip)
    theirs = make_transect("VC_12m_01")
    store.add_transect(theirs)
    clip = store.upsert_video(make_video("c3" * 16, path="/elsewhere/Y.MP4"))
    store.add_pass(
        TransectPass(transect_id=theirs.id, video_id=clip.id, begin_s=0, end_s=10, campaign_id=other_trip.id)
    )

    report = import_expedition(store, scan_expedition(tree), probe=_probe)

    assert report.shared_transects == ["VC_12m_01"]
    assert "shared with another expedition" in report.summary()


def test_a_clip_first_added_through_a_symlink_is_not_its_own_duplicate(tree, store, tmp_path):
    link = tmp_path / "links" / tree.name  # same expedition, reached another way
    link.parent.mkdir()
    link.symlink_to(tree)
    import_expedition(store, scan_expedition(link), probe=_probe)

    report = import_expedition(store, scan_expedition(tree), probe=_probe)

    assert report.duplicates == []
    assert report.passes_created == 0


def test_a_transect_folder_renamed_on_disk_takes_its_passes_and_details(tree, store):
    import_expedition(store, scan_expedition(tree), probe=_probe)
    old = next(t for t in store.list_transects() if t.name == "VC_12m_01")
    old.start_lat, old.start_lon, old.end_lat, old.end_lon, old.length_m = 29.1, 34.9, 29.2, 34.8, 50.0
    store.update_transect(old)
    (tree / "VC_12m_01").rename(tree / "VCX_12m_01")
    # A blank same-named line an earlier import left behind goes too.
    store.add_transect(make_transect("VCX_12m_01", start_lat=None, start_lon=None, end_lat=None, end_lon=None,
                                     length_m=None))

    report = import_expedition(store, scan_expedition(tree), probe=_probe)

    names = sorted(t.name for t in store.list_transects())
    assert names == ["VCX_12m_01", "VC_6m_01"]
    new = next(t for t in store.list_transects() if t.name == "VCX_12m_01")
    assert (new.start_lat, new.end_lon, new.length_m) == (29.1, 34.8, 50.0)
    assert len(store.list_passes(transect_id=new.id)) == 1
    assert report.passes_created == 0
    assert report.transects_renamed == ["VC_12m_01 → VCX_12m_01"]


def test_a_down_looking_clip_owes_details_but_no_trim_or_run(tree, store):
    import_expedition(store, scan_expedition(tree), probe=_probe)
    pass_ = next(p for p in store.list_passes() if store.get_video(p.video_id).file_name == "GX010200.MP4")
    video = store.get_video(pass_.video_id)
    video.view = "down"
    store.update_video(video)

    owed = {(g.record_id, g.item) for g in missing_data(store, pass_.campaign_id)}

    assert store.pass_looks_down(pass_.id)
    assert (pass_.id, "quality") in owed
    assert (video.id, "camera") in owed
    assert not {item for rid, item in owed if rid == pass_.id} & {
        "start and end of the swim (not trimmed)", "not processed yet"
    }
    assert (video.id, "rig position") not in owed
    # And it survives a reopen, through the migration's column.
    assert SurveyStore(tree / "survey.db").get_video(video.id).view == "down"


def test_the_queue_refuses_a_pass_shot_looking_down(tree, store):
    from types import SimpleNamespace

    from deepreefmap_gui.simple.batch import SimpleBatchMixin

    import_expedition(store, scan_expedition(tree), probe=_probe)
    down, oblique = store.list_passes()[:2]
    video = store.get_video(down.video_id)
    video.view = "down"
    store.update_video(video)
    carted = []
    window = SimpleNamespace(
        _survey_store=lambda: store,
        _ensure_cart_batch=lambda: SimpleNamespace(id=None),
    )
    store.add_batch_item = carted.append  # only membership is under test

    assert SimpleBatchMixin._cart_add(window, down.id) is False
    assert carted == []
    assert store.pass_looks_down(oblique.id) is False


def test_a_down_clip_is_not_listed_as_to_process():
    from deepreefmap_gui.survey.catalogue import VideoLibraryEntry
    from deepreefmap_gui.survey.jobs import clip_summary, job_filters
    from deepreefmap_gui.survey.models import TransectPass
    from deepreefmap_gui.survey.statuses import CLIP_LOOKS_DOWN, clip_spec

    video = make_video("d" * 32, view="down")
    pass_ = TransectPass(transect_id=uuid.uuid4(), video_id=video.id, begin_s=0.0, end_s=10.0)
    entry = VideoLibraryEntry(video=video, pass_count=1, run_count=0, passes=[pass_])

    assert job_filters(entry) == {"all"}
    assert entry.outcome == CLIP_LOOKS_DOWN
    assert clip_spec(entry.outcome).label == "Looks down (not processed)"
    assert clip_summary(entry) == "Looks down · not processed"
    video.view = "oblique"
    assert "process" in job_filters(entry)


def test_clips_swapped_between_pass_folders_take_their_passes_along(tree, store):
    """Two swims filed in the wrong pass, put right on disk: the next import follows."""
    import_expedition(store, scan_expedition(tree), probe=_probe)
    t6 = tree / "VC_6m_01"
    a = t6 / "pass_01_control" / "fwd" / "GX010050.MP4"
    b = t6 / "pass_02_angle" / "fwd" / "RAW" / "GX010100.MP4"
    a.rename(t6 / "pass_02_angle" / "fwd" / "RAW" / a.name)
    b.rename(t6 / "pass_01_control" / "fwd" / b.name)

    report = import_expedition(store, scan_expedition(tree), probe=_probe)

    label = {store.get_video(p.video_id).file_name: p.label for p in store.list_passes()}
    assert label["GX010050.MP4"] == "pass_02_angle"
    assert label["GX010100.MP4"] == "pass_01_control"
    assert report.passes_refiled == 2 and report.passes_created == 0
    assert all(Path(v.path).exists() for v in store.list_videos())
