"""Clip paths inside the database's folder are stored relative to it."""

import sqlite3
from pathlib import Path, PureWindowsPath

from _factories import make_video

from deepreefmap_gui.survey.store import SurveyStore, full_clip_path, stored_clip_path


def _raw_path(db: Path, video_id) -> str:
    with sqlite3.connect(db) as conn:
        return conn.execute("SELECT path FROM video_asset WHERE id = ?", (str(video_id),)).fetchone()[0]


def test_a_clip_inside_the_folder_is_stored_relative_and_read_back_whole(tmp_path):
    root = tmp_path / "2026_11_JO_EXPE"
    store = SurveyStore(root / "survey.db")
    clip = root / "VC_6m_01" / "pass_01_control" / "fwd" / "GX010099.MP4"
    video = store.upsert_video(make_video("a" * 32, path=str(clip)))

    assert _raw_path(root / "survey.db", video.id) == "VC_6m_01/pass_01_control/fwd/GX010099.MP4"
    assert store.get_video(video.id).path == str(clip)


def test_the_folder_can_move_and_the_clips_follow(tmp_path):
    root = tmp_path / "drive_a" / "EXPE"
    store = SurveyStore(root / "survey.db")
    video = store.upsert_video(make_video("b" * 32, path=str(root / "T1" / "x.MP4")))
    store.close()

    moved = tmp_path / "drive_b" / "EXPE"
    moved.parent.mkdir()
    root.rename(moved)

    assert SurveyStore(moved / "survey.db").get_video(video.id).path == str(moved / "T1" / "x.MP4")


def test_a_clip_outside_the_folder_keeps_its_full_path(tmp_path):
    store = SurveyStore(tmp_path / "out" / "survey.db")
    elsewhere = str(tmp_path / "cards" / "GX010001.MP4")
    video = store.upsert_video(make_video("c" * 32, path=elsewhere))

    assert _raw_path(tmp_path / "out" / "survey.db", video.id) == elsewhere
    assert store.get_video(video.id).path == elsewhere


def test_full_paths_an_older_build_wrote_are_rewritten_at_open(tmp_path):
    root = tmp_path / "EXPE"
    store = SurveyStore(root / "survey.db")
    video = store.upsert_video(make_video("d" * 32, path=str(root / "T1" / "y.MP4")))
    with sqlite3.connect(root / "survey.db") as conn:
        conn.execute("UPDATE video_asset SET path = ? WHERE id = ?", (str(root / "T1" / "y.MP4"), str(video.id)))
    store.close()

    SurveyStore(root / "survey.db")

    assert _raw_path(root / "survey.db", video.id) == "T1/y.MP4"


def test_a_windows_path_is_never_taken_for_a_relative_one():
    root = Path("/Volumes/EXPE")
    windows = str(PureWindowsPath(r"E:\2026_11_JO_EXPE\VC_6m_01\GX010099.MP4"))

    assert stored_clip_path(root, windows) == windows
    assert full_clip_path(root, windows) == windows
    assert full_clip_path(root, "VC_6m_01/GX010099.MP4") == str(root / "VC_6m_01" / "GX010099.MP4")
