import atexit
import os
import shutil
import subprocess
import tempfile
import time

import pytest


def _headless() -> None:
    """Keep the suite off the developer's screen: `Xvfb` if there is one, else `offscreen`.

    Xvfb first because the viewer tests need a GL context, and `offscreen`
    segfaults them on a machine with no software GL. An explicit
    `QT_QPA_PLATFORM` wins, and must be `xcb`: VTK under Wayland hangs.
    """
    if os.environ.get("QT_QPA_PLATFORM"):
        return
    if (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")) and _start_xvfb():
        os.environ["QT_QPA_PLATFORM"] = "xcb"
        return
    os.environ["QT_QPA_PLATFORM"] = "offscreen"


def _start_xvfb() -> bool:
    """Point DISPLAY at a throwaway X server of our own. True if one came up."""
    if not shutil.which("Xvfb"):
        return False
    for number in range(99, 110):
        if os.path.exists(f"/tmp/.X11-unix/X{number}"):
            continue
        try:
            server = subprocess.Popen(
                ["Xvfb", f":{number}", "-screen", "0", "1920x1200x24", "-nolisten", "tcp"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError:
            return False
        # Poll for the socket: Qt fails to connect if it starts first.
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if os.path.exists(f"/tmp/.X11-unix/X{number}"):
                atexit.register(_stop_xvfb, server)
                os.environ["DISPLAY"] = f":{number}"
                os.environ.pop("WAYLAND_DISPLAY", None)
                return True
            if server.poll() is not None:
                break  # Another server took the number, or Xvfb is unusable.
            time.sleep(0.05)
        server.kill()
    return False


def _stop_xvfb(server: subprocess.Popen) -> None:
    server.terminate()
    try:
        server.wait(timeout=5)
    except subprocess.TimeoutExpired:
        server.kill()


_headless()

# Keep the suite off the developer's real run history: measured peaks in
# ~/.local/share/deepreefmap/run_timings.json would otherwise shift memory
# grades and ETA priors, making tests fail on machines that have done runs.
os.environ.setdefault(
    "DEEPREEFMAP_RUN_TIMINGS",
    os.path.join(tempfile.mkdtemp(prefix="deepreefmap-test-timings-"), "run_timings.json"),
)

# Keep window construction off the network: an empty mock reads as a fetch
# failure, so no update badge appears and GitHub is never contacted.
os.environ.setdefault("DEEPREEFMAP_MOCK_VERSIONS", "")


@pytest.fixture(scope="session")
def qapp():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


@pytest.fixture(autouse=True)
def _no_file_manager(request, monkeypatch):
    """Stop a click reaching a real file manager on `tmp_path`.

    The effect functions rather than `reveal_in_file_manager`: callers import
    that name into their own module, and its dispatch stays real.
    `tests/core/test_reveal.py` is the exception, marked `real_reveal`.
    """
    if request.node.get_closest_marker("real_reveal"):
        yield
        return

    from deepreefmap_gui.core import reveal

    for name in ("_show_items", "_xdg_open", "_qt_open", "_reveal_windows", "_reveal_macos"):
        monkeypatch.setattr(reveal, name, lambda _path: False)

    try:
        from PySide6.QtGui import QDesktopServices
    except ImportError:
        yield
        return
    monkeypatch.setattr(QDesktopServices, "openUrl", staticmethod(lambda _url: True))
    yield


_MODAL_ADVICE = (
    "Nothing offscreen can close it, so the interpreter parks inside Qt's C++ "
    "event loop and the run stalls. Stub the call, disconnect the handler that "
    "opens it, or mark the test real_modal."
)


@pytest.fixture(autouse=True)
def _no_blocking_modals(request, monkeypatch):
    """Turn an unexpected modal into a named failure rather than a stalled suite.

    A test that drives a widget wired to the window reaches the real handler
    behind it, and those handlers open dialogs. pytest-timeout's signal method
    cannot interrupt one: its handler is Python-level and the interpreter never
    gets back to run it. One such dialog held every CI leg for five hours.

    Tests that mean to drive a dialog already stub the call themselves, and
    those stubs still win -- monkeypatch applies them after this fixture.

    Raising is what keeps the suite moving, but it is not what reports: these
    calls happen inside Qt slots, and PySide6 prints an unhandled slot exception
    rather than propagating it, so the test would otherwise pass with the
    traceback buried in captured output. The blocked calls are recorded and the
    test fails on the record at teardown.
    """
    if request.node.get_closest_marker("real_modal"):
        yield
        return

    try:
        from PySide6.QtWidgets import QDialog, QMenu, QMessageBox
    except ImportError:
        yield
        return

    blocked: list[str] = []

    def _block(what: str):
        message = f"{what} would block. {_MODAL_ADVICE}"
        blocked.append(message)
        raise AssertionError(message)

    def _blocked_exec(self, *_args, **_kwargs):
        _block(f"{type(self).__name__}.exec()")

    # QMessageBox and QMenu carry their own exec, so patching QDialog's alone
    # would leave both reachable.
    for cls in (QDialog, QMenu, QMessageBox):
        monkeypatch.setattr(cls, "exec", _blocked_exec)

    def _blocked_static(name):
        def _call(*_args, **_kwargs):
            _block(f"QMessageBox.{name}()")

        return staticmethod(_call)

    for name in ("question", "warning", "information", "critical", "about"):
        monkeypatch.setattr(QMessageBox, name, _blocked_static(name))
    yield
    if blocked:
        raise AssertionError(blocked[0])


@pytest.fixture(autouse=True, scope="session")
def _isolate_qsettings():
    """Redirect all QSettings storage to a tempdir for the whole test session.

    setDefaultFormat(IniFormat) does not flip QSettings(org, app) instances under
    PySide6, so redirect the NativeFormat path itself. On Windows that path is the
    registry, where setPath has no effect.

    A missing PySide6 is raised, not swallowed: yielding without isolation would
    let the rest of the session write to the developer's real config.
    """
    from PySide6.QtCore import QSettings

    tmp = tempfile.mkdtemp(prefix="deepreefmap-test-qsettings-")
    for fmt in (QSettings.Format.NativeFormat, QSettings.Format.IniFormat):
        QSettings.setPath(fmt, QSettings.Scope.UserScope, tmp)
        QSettings.setPath(fmt, QSettings.Scope.SystemScope, tmp)
    yield


@pytest.fixture
def sampled(monkeypatch):
    """A fast resource sampler, and a wait for its next reading.

    A stage's peak is whichever samples landed inside its window. The sampler
    polls twice a second, which suits a run that takes minutes; a stubbed run
    opens and closes every stage inside a microsecond and would record no peak
    at all, for reasons that have nothing to do with the code under test.
    """
    from deepreefmap_gui.profiling import instrumentation
    from deepreefmap_gui.profiling.perf_sampler import ResourceSampler

    created: list = []
    real_init = instrumentation.RunInstrumentation.__init__

    def spy(self, output_dir):
        real_init(self, output_dir)
        created.append(self)

    monkeypatch.setattr(
        instrumentation, "ResourceSampler", lambda: ResourceSampler(interval_s=0.01)
    )
    monkeypatch.setattr(instrumentation.RunInstrumentation, "__init__", spy)

    def wait(timeout: float = 5.0) -> None:
        """Block until the run in flight has one more reading than on entry.

        Polled rather than slept: a loaded runner can miss a tick, and a fixed
        sleep turns that into a flake.
        """
        before = len(created[-1]._sampler.samples) if created else 0
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if created and len(created[-1]._sampler.samples) > before:
                return
            time.sleep(0.005)

    return wait


@pytest.fixture(autouse=True)
def _fresh_gpu_probe(tmp_path, monkeypatch):
    """One machine's card per test, remembered nowhere.

    The probe caches what it identified for the process and records it for the
    next launch, so without this a test that mocked a 4090 would answer for the
    one after it -- and, worse, the developer's own recorded card would answer
    for a test that mocked nothing.
    """
    from deepreefmap_gui.profiling import system_probe

    monkeypatch.setenv("DEEPREEFMAP_GPU_CACHE", str(tmp_path / "gpu_probe.json"))
    system_probe.reset_gpu_probe()
    yield
    system_probe.reset_gpu_probe()
