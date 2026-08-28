from __future__ import annotations

import importlib.util
import os
from types import SimpleNamespace

import pytest


def require_torch() -> None:
    """Skip locally when torch is absent, but fail where it must be installed.

    Building a window pulls in torch, so a torchless machine would skip most of
    this directory. That is a reasonable local convenience and a useless CI run,
    so DEEPREEFMAP_REQUIRE_TORCH turns the skip into a failure.
    """
    if importlib.util.find_spec("torch") is not None:
        return
    message = "torch not loadable on this machine"
    if os.environ.get("DEEPREEFMAP_REQUIRE_TORCH"):
        raise AssertionError(
            f"{message}, and DEEPREEFMAP_REQUIRE_TORCH is set: the GUI suite would "
            "have been skipped rather than run"
        )
    pytest.skip(message, allow_module_level=True)


@pytest.fixture(autouse=True)
def _reset_setup_complete(qapp):
    """Keep the persisted readiness acknowledgement from leaking between tests.

    It decides whether a window opens on Setup or straight on Plan, so a
    test that reaches ready would otherwise change where the next one starts.
    """
    from PySide6.QtCore import QSettings

    settings = QSettings("ECEO", "deepreefmap")
    settings.remove("setup_complete")
    yield
    settings.remove("setup_complete")


@pytest.fixture
def out_root(tmp_path):
    """The one output root every window in the suite writes under.

    A subdirectory rather than tmp_path itself, so the input files a test writes
    into tmp_path (videos, CSVs, preset files) are not inside the archive the run
    scanner walks.
    """
    return tmp_path / "out"


@pytest.fixture(autouse=True)
def _tmp_output_root(qapp, out_root):
    """Point the persisted output root at the temp dir: a window creates
    survey.db under its root at construction, so this must be set before one is
    built."""
    from PySide6.QtCore import QSettings

    settings = QSettings("ECEO", "deepreefmap")
    old = settings.value("output_root_dir")
    settings.setValue("output_root_dir", str(out_root))
    yield
    if old is None:
        settings.remove("output_root_dir")
    else:
        settings.setValue("output_root_dir", old)


def _machine_preset_path(tmp_path):
    return tmp_path / "machine-settings" / "survey_preset.yaml"


@pytest.fixture(autouse=True)
def _isolate_survey_preset(tmp_path, monkeypatch):
    """Keep GUI tests off the developer's real survey settings.

    A window loads the settings at construction, and _adopt_form_as_preset
    writes the machine override back when the settings dialog is accepted, so an
    unguarded test would both read and overwrite the file at
    ~/.local/share/deepreefmap/survey_preset.yaml. Point it at a tmp path that
    does not exist and clear the admin override so the bundled preset loads.
    Follows the pattern in tests/survey/conftest.py.
    """
    monkeypatch.delenv("DEEPREEFMAP_SURVEY_PRESET", raising=False)
    monkeypatch.setattr(
        "deepreefmap_gui.survey.preset.survey_preset_path",
        lambda: _machine_preset_path(tmp_path),
    )


@pytest.fixture
def machine_preset_path(tmp_path):
    """Where the isolation fixture sends this machine's override."""
    return _machine_preset_path(tmp_path)


@pytest.fixture(autouse=True)
def _isolate_sync_credentials(tmp_path, monkeypatch):
    """Keep the Server page off this machine's real device token.

    The page reads the credential every time it is shown, and a developer with a
    laptop enrolled against a real registry would otherwise have the suite paint
    their own server, with a Sync now button wired to it.
    """
    monkeypatch.setenv("DEEPREEFMAP_SYNC_DEVICE", str(tmp_path / "sync_device.json"))
    monkeypatch.setenv("DEEPREEFMAP_SYNC_TOKEN", str(tmp_path / "sync_token.json"))
    monkeypatch.setattr("deepreefmap_gui.sync.credentials._keyring", lambda: None)


@pytest.fixture(autouse=True)
def _offline_tiles(qapp):
    """Map widgets must never fetch tiles during tests."""
    from deepreefmap_gui.map.tile_cache import shared_tile_cache

    cache = shared_tile_cache()
    cache.network_enabled = False
    yield


@pytest.fixture
def tile_cache_root(tmp_path, monkeypatch):
    """The tile cache directory, redirected into tmp_path. Created by the first tile."""
    from deepreefmap_gui.map import tile_cache as tile_cache_mod

    root = tmp_path / "tiles"
    monkeypatch.setattr(tile_cache_mod, "tile_cache_dir", lambda: root)
    return root


@pytest.fixture
def tile_cache(qapp, tile_cache_root):
    """A TileCache of its own, so a test may prune and fail tiles freely.

    Not named `cache`: that is a pytest builtin, and overriding it here would
    shadow it for every test in this directory.
    """
    from deepreefmap_gui.map.layers import OSM_LAYER
    from deepreefmap_gui.map.tile_cache import TileCache

    return TileCache(OSM_LAYER)


@pytest.fixture(autouse=True)
def _assume_gpu(monkeypatch):
    """Assume a graphics card so the run gate is deterministic off CI hardware.

    The bundled preset defaults to a GPU-only mapper, so on a CPU-only runner the
    gate blocks and every ready-state test fails. Tests for the no-GPU path
    override _gpu_available or _gpu_only_mapper themselves.
    """
    from deepreefmap_gui.form.panel import FormPanelMixin

    monkeypatch.setattr(FormPanelMixin, "_gpu_available", lambda self: True)


@pytest.fixture
def make_window(qapp):
    """Factory building a fresh DeepReefMapWindow with the built-in classes.

    A factory rather than an instance so tests can set env vars (mock PyApp,
    timing-profile path) before construction.

    Every window it builds is drained, stopped and destroyed on teardown, in that
    order. Some work is queued rather than armed: deleting a run directory fires
    QFileSystemWatcher.directoryChanged asynchronously, and a batch worker emits
    its done signal across threads. Neither has been delivered by teardown, so
    stopping timers first misses them entirely (the delivery happens later and
    arms a timer on a window nothing is watching any more). Draining first lets
    them land, then the stop catches what they armed.

    Windows are then destroyed: each installs event filters on its widgets, and a
    leaked window makes a later global setStyleSheet re-polish it through those
    filters, which hangs the suite. deleteLater rather than close() so the
    quit-confirmation modal never fires.
    """
    from PySide6.QtCore import QEvent

    require_torch()
    created = []

    def _make():
        from deepreefmap.config.classes import load_classes

        from deepreefmap_gui.app import DeepReefMapWindow

        window = DeepReefMapWindow(load_classes(), None)
        created.append(window)
        return window

    yield _make
    # Drain queued deliveries first so they land while the window still has its
    # timers, then stop the timers they armed, then delete: a tick must not
    # fire against a half-deleted window ("signal source deleted").
    if created:
        qapp.processEvents()
    for window in created:
        window._stop_window_timers()
        window.hide()
    qapp.processEvents()
    for window in created:
        window.deleteLater()
    qapp.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    qapp.processEvents()


# Roomy enough that no batch estimate ever trips the pre-flight.
_PLENTY_FREE = 4 * 1024**4


@pytest.fixture(autouse=True)
def _plenty_of_disk(monkeypatch):
    """Give the disk pre-flight a deterministic drive to measure.

    `_confirm_batch_space` reads the real filesystem under the run root, and
    tmp_path lives on /tmp -- a tmpfs on many Linux boxes. A developer whose
    tmpfs holds less than the batch estimate gets a blocking "Insufficient disk
    space" modal, and the suite stops dead until someone clicks it. The GUI suite
    passed or hung depending on the machine running it.

    A fixed generous reading rather than stubbing the check away: the tests that
    are *about* the pre-flight set their own free space over this one and go on
    exercising the real code path.
    """
    import shutil

    monkeypatch.setattr(
        shutil,
        "disk_usage",
        lambda _p: SimpleNamespace(total=_PLENTY_FREE, used=0, free=_PLENTY_FREE),
    )


@pytest.fixture
def window(make_window, out_root):
    """A fresh DeepReefMapWindow rooted at out_root. Function-scoped: tests
    mutate form state, legend toggles and signals, so sharing an instance would
    leak state.

    The root is set on the field as well as through the persisted setting the
    window read at construction, so a test asserting where a run landed can read
    that root off out_root rather than inferring it from QSettings.
    """
    window = make_window()
    window._out_root_input.setText(str(out_root))
    return window
