"""Main window assembly: creation, cache seeding from the form's settings,
updates tab, desktop entry controls."""

from __future__ import annotations

import sys

import pytest


def test_window_creates(window) -> None:
    assert window.windowTitle() == "DeepReefMap"


def test_a_pass_seeds_its_cache_from_a_matching_prior_run(window, tmp_path) -> None:
    """The batch worker seeds each pass from the settings the form holds, so a
    second attempt at the same clip reuses the frames the first one prepared."""
    from deepreefmap.pipeline import resume as resume_mod

    from deepreefmap_gui.camera.profiles import copy_profile_into_run
    from deepreefmap_gui.io.classes_default import resolve_classes_path
    from deepreefmap_gui.runs.seeding import seed_from_settings

    video = tmp_path / "clip.mp4"
    video.write_bytes(b"not really a video")
    window._fps_spin.setValue(3)

    prior = tmp_path / "runs" / "20260101-000000"
    for dirname in ("frames", "labels", "masks"):
        (prior / dirname).mkdir(parents=True)
        (prior / dirname / "000000.png").write_bytes(b"data")
    prep_key = resume_mod.preprocess_key(
        video_paths=[video],
        fps=3,
        begin_s=None,
        end_s=None,
        camera_profile_name=window._profile_combo.currentText(),
        segmentation_name=window._seg_combo.currentText(),
        classes_path=resolve_classes_path(window._classes_path),
        processing_width=window._proc_width_spin.value(),
        processing_height=window._proc_height_spin.value(),
    )
    resume_mod.write_sidecar(prior, resume_mod.STAGE_PREPROCESS, prep_key)
    copy_profile_into_run(window._profile_combo.currentText(), prior)

    out_dir = tmp_path / "runs" / "20260102-000000"
    out_dir.mkdir()
    seed_from_settings(
        out_dir, out_dir.parent, window._collect_run_settings(), [video], None, None
    )
    assert (out_dir / "frames" / "000000.png").exists()
    sidecar = resume_mod.read_sidecar(out_dir, resume_mod.STAGE_PREPROCESS)
    assert sidecar is not None and sidecar["key"] == prep_key


def test_updates_tab_dev_mode_vs_installed(window) -> None:
    releases = [{"tag_name": "v2.0.0", "assets": []}, {"tag_name": "v1.0.0", "assets": []}]

    # Dev mode (no installer binary): explain it, hide the install controls.
    # isHidden() reflects the widget's own flag (the window is never shown, so
    # isVisible() would be False regardless).
    window._apply_update_check("1.1.0", releases, None)
    assert "development mode" in window._update_status_label.text().lower()
    assert window._update_version_combo.isHidden()
    assert window._update_show_all.isHidden()

    # Installed binary: install controls + rollback checkbox appear.
    window._apply_update_check("1.1.0", releases, "/tmp/mock-pyapp")
    assert not window._update_version_combo.isHidden()
    assert not window._update_show_all.isHidden()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux-only feature")
def test_the_shortcut_row_adds_and_removes_the_entry(make_window, monkeypatch, tmp_path) -> None:
    """Scenario: a bare Linux binary registering itself in the applications menu.

    Expected behaviour: the readiness row carries the verb, and the entry it
    reports on is the one on disk.
    """
    from deepreefmap_gui.packaging.shortcuts import _linux, install_shortcut, remove_shortcut

    monkeypatch.setenv("DEEPREEFMAP_MOCK_PYAPP", "1")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setenv("DEEPREEFMAP_SHORTCUT_MANIFEST", str(tmp_path / "shortcut.json"))
    monkeypatch.setattr(_linux, "_refresh_desktop_caches", lambda: None)

    window = make_window()
    _icon, _detail, actions = window._setup_check_rows["shortcut"]
    assert actions[0].text() == "Add"

    install_shortcut()
    window._shortcut_status_cache = None
    window._refresh_readiness_view()
    # The row passes now, so its action is the glyph shape and carries the verb
    # as its name rather than its text.
    assert actions[0].accessibleName() == "Remove"
    assert not actions[0].isHidden()

    remove_shortcut()
    window._shortcut_status_cache = None
    window._refresh_readiness_view()
    assert actions[0].text() == "Add"


def test_the_shortcut_row_is_advisory(make_window, monkeypatch, tmp_path) -> None:
    """A missing applications-menu entry stops nothing, so it must not read as a
    missing requirement or hold the Setup summary back."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setenv("DEEPREEFMAP_SHORTCUT_MANIFEST", str(tmp_path / "shortcut.json"))
    monkeypatch.setenv("DEEPREEFMAP_MOCK_PYAPP", "1")
    window = make_window()
    checks = {c.key: c for c in window._current_setup_checks()}
    assert checks["shortcut"].advisory
    assert "requirement" not in checks["shortcut"].detail.lower()


def test_the_shortcut_row_offers_no_action_from_a_source_checkout(
    make_window, monkeypatch, tmp_path
) -> None:
    """Running from a checkout there is no installed program to point an entry
    at, so the row explains itself instead of showing a button that fails."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setenv("DEEPREEFMAP_SHORTCUT_MANIFEST", str(tmp_path / "shortcut.json"))
    monkeypatch.delenv("DEEPREEFMAP_MOCK_PYAPP", raising=False)
    monkeypatch.delenv("PYAPP", raising=False)

    window = make_window()
    window._refresh_readiness_view()

    check = {c.key: c for c in window._current_setup_checks()}["shortcut"]
    assert check.action_label == ""
    assert "source checkout" in check.detail

    _icon, _detail, actions = window._setup_check_rows["shortcut"]
    assert actions[0].isHidden()

