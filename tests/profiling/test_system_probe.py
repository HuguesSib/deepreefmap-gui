"""The system probe reads memory/CPU/GPU/disk on any OS, with torch mocked."""

from __future__ import annotations

import json
import sys
import types
from types import SimpleNamespace

from deepreefmap_gui.profiling import system_probe as sp


def _fake_torch(*, cuda=False, mps=False, name="GPU", free=1, total=2):
    return types.SimpleNamespace(
        cuda=types.SimpleNamespace(
            is_available=lambda: cuda,
            mem_get_info=lambda dev=0: (free, total),
            get_device_name=lambda dev=0: name,
        ),
        backends=types.SimpleNamespace(mps=types.SimpleNamespace(is_available=lambda: mps)),
    )


def test_probe_gpu_reports_cuda_vram(monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "torch", _fake_torch(cuda=True, name="RTX 4090", free=100, total=1000))
    gpu = sp._probe_gpu()
    assert gpu.kind == sp.GPU_CUDA
    assert gpu.name == "RTX 4090"
    assert (gpu.total_vram_bytes, gpu.free_vram_bytes) == (1000, 100)
    assert gpu.has_distinct_vram


def test_a_card_that_cannot_report_its_vram_is_still_a_card(monkeypatch) -> None:
    """ROCm builds do not all implement mem_get_info. Readiness asks this probe
    whether a card exists, so a missing byte count must not read as no GPU."""
    torch = _fake_torch(cuda=True, name="Radeon RX 7900")

    def unsupported(dev=0):
        raise RuntimeError("mem_get_info is not supported on this device")

    torch.cuda.mem_get_info = unsupported
    monkeypatch.setitem(sys.modules, "torch", torch)

    gpu = sp._probe_gpu()
    assert gpu.kind == sp.GPU_CUDA
    assert gpu.name == "Radeon RX 7900"
    assert (gpu.total_vram_bytes, gpu.free_vram_bytes) == (None, None)
    assert not gpu.has_distinct_vram
    assert sp.gpu_present()


def test_a_card_that_cannot_name_itself_is_still_a_card(monkeypatch) -> None:
    torch = _fake_torch(cuda=True)

    def unnamed(dev=0):
        raise RuntimeError("no device name")

    torch.cuda.get_device_name = unnamed
    monkeypatch.setitem(sys.modules, "torch", torch)

    assert sp._probe_gpu().kind == sp.GPU_CUDA


def test_gpu_present_follows_the_probe(monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "torch", _fake_torch())
    assert not sp.gpu_present()
    monkeypatch.setitem(sys.modules, "torch", _fake_torch(mps=True))
    sp.reset_gpu_probe()
    assert sp.gpu_present()


def test_the_card_is_identified_once_per_process(monkeypatch) -> None:
    """The identification costs seconds of driver initialisation, and a machine
    does not grow a card while the app is open."""
    calls = []

    def counted():
        calls.append(1)
        return True

    torch = _fake_torch(cuda=True, name="RTX 4090")
    torch.cuda.is_available = counted
    monkeypatch.setitem(sys.modules, "torch", torch)

    for _ in range(5):
        assert sp._probe_gpu().name == "RTX 4090"
    assert len(calls) == 1


def test_free_vram_is_re_read_while_the_identity_is_not(monkeypatch) -> None:
    """The card is fixed; what is left on it is not, and the gauges poll for it."""
    # Falling, so each reading is distinguishable. The identification consumes
    # the first one; every read after it is a fresh call.
    remaining = [1000, 800, 600, 400]
    torch = _fake_torch(cuda=True, name="RTX 4090")
    torch.cuda.mem_get_info = lambda dev=0: (remaining.pop(0), 1000)
    monkeypatch.setitem(sys.modules, "torch", torch)

    seen = [sp._probe_gpu() for _ in range(3)]
    assert [g.free_vram_bytes for g in seen] == [800, 600, 400]
    assert {g.name for g in seen} == {"RTX 4090"}
    assert {g.total_vram_bytes for g in seen} == {1000}


def test_a_non_blocking_probe_never_loads_torch(monkeypatch) -> None:
    """What the GUI thread calls. Loading torch there is the several-second
    freeze this whole arrangement exists to prevent."""
    monkeypatch.delitem(sys.modules, "torch", raising=False)

    def refuse(name, *args, **kwargs):
        raise AssertionError(f"the non-blocking path imported {name}")

    monkeypatch.setattr(sp, "_resolve_gpu", refuse)
    monkeypatch.setattr(sp, "_resolve_gpu_in_process", refuse)

    gpu = sp._probe_gpu(wait=False)
    assert gpu.kind == sp.GPU_UNKNOWN
    assert not gpu.resolved
    # And it does not start the probe behind the caller's back: the window does
    # that, once, after it is on screen.
    assert sp._gpu.thread is None


def test_the_card_found_is_offered_to_the_next_launch(monkeypatch, tmp_path) -> None:
    monkeypatch.setitem(sys.modules, "torch", _fake_torch(cuda=True, name="RTX 4090", free=7, total=9))
    sp.await_gpu_probe()
    assert sp._probe_gpu().name == "RTX 4090"

    # A fresh process: nothing probed, but the record is on disk.
    sp.reset_gpu_probe()
    monkeypatch.delitem(sys.modules, "torch", raising=False)
    hinted = sp._probe_gpu(wait=False)
    assert hinted.kind == sp.GPU_CUDA
    assert hinted.name == "RTX 4090"
    assert hinted.total_vram_bytes == 9


def test_a_record_from_another_torch_build_is_not_reused(monkeypatch) -> None:
    """Which card torch reports is a property of the wheel: a cu130 build on an
    AMD card finds nothing where the rocm build of the same torch finds it. The
    recorded answer must not survive switching between them."""
    monkeypatch.setattr(sp, "_torch_build", lambda: "2.9.1+rocm6.4")
    monkeypatch.setitem(sys.modules, "torch", _fake_torch(cuda=True, name="Radeon RX 7900"))
    sp.await_gpu_probe()
    assert sp._probe_gpu().name == "Radeon RX 7900"

    sp.reset_gpu_probe()
    monkeypatch.setattr(sp, "_torch_build", lambda: "2.13.0")
    monkeypatch.delitem(sys.modules, "torch", raising=False)
    assert sp._probe_gpu(wait=False) is sp.GPU_PENDING


def test_a_probe_that_could_not_check_is_not_recorded(monkeypatch) -> None:
    """An antivirus blocking the child once, or a driver mid-reinstall, must not
    leave the next launch believing this machine has no card."""
    from deepreefmap_gui.paths import gpu_probe_cache_path

    monkeypatch.delitem(sys.modules, "torch", raising=False)
    monkeypatch.setattr(sp, "_resolve_gpu", lambda: sp._UNVERIFIED_NONE)
    sp.await_gpu_probe()

    assert sp._probe_gpu().kind == sp.GPU_NONE
    assert not gpu_probe_cache_path().exists()


def test_a_child_that_dies_is_not_retried_in_this_process(monkeypatch) -> None:
    """A non-zero exit is overwhelmingly the driver killing the process inside
    the enumeration. Repeating that call here would take the window with it."""
    import subprocess

    monkeypatch.delitem(sys.modules, "torch", raising=False)
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=-11, stdout=b"", stderr=b"boom")
    )

    def refuse():
        raise AssertionError("fell back to importing torch in the GUI process")

    monkeypatch.setattr(sp, "_resolve_gpu_in_process", refuse)
    assert sp._resolve_gpu() is sp._UNVERIFIED_NONE


def test_the_answer_is_found_among_whatever_else_the_child_printed(monkeypatch) -> None:
    """ROCm banners, vendor .pth files and sitecustomize all reach that stdout."""
    import subprocess

    monkeypatch.delitem(sys.modules, "torch", raising=False)
    noise = (
        b"/opt/amdgpu/share/libdrm/amdgpu.ids: No such file or directory\n"
        b'{"kind": "cuda", "name": "not the answer"}\n'
        b'\nDRM_GPU {"kind": "cuda", "name": "Radeon RX 7900", "total": 9, "free": 7}\n'
    )
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0, stdout=noise, stderr=b"")
    )

    gpu = sp._resolve_gpu()
    assert (gpu.kind, gpu.name, gpu.total_vram_bytes) == (sp.GPU_CUDA, "Radeon RX 7900", 9)


def test_a_child_that_printed_no_answer_reports_no_card(monkeypatch) -> None:
    import subprocess

    monkeypatch.delitem(sys.modules, "torch", raising=False)
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0, stdout=b"hello\n", stderr=b"")
    )
    monkeypatch.setattr(sp, "_resolve_gpu_in_process", _refuse_in_process)
    assert sp._resolve_gpu() is sp._UNVERIFIED_NONE


def _refuse_in_process():
    raise AssertionError("fell back to importing torch in the GUI process")


def test_the_probe_always_settles_an_answer(monkeypatch) -> None:
    """The thread is created once and never replaced, so an exception escaping it
    would leave every reader waiting on a card that is never counted."""
    seen = []

    def explode():
        raise RuntimeError("probe blew up")

    monkeypatch.setattr(sp, "_resolve_gpu", explode)
    sp.start_gpu_probe(seen.append)
    sp.await_gpu_probe()

    assert [g.kind for g in seen] == [sp.GPU_NONE]
    assert sp._gpu.identity is not None
    assert not sp._gpu.waiters


def test_last_session_free_vram_is_not_offered_as_a_live_reading(monkeypatch) -> None:
    """The System tab polls this for its gauge. A number from last launch painted
    as now would show an idle card at 90% used, and never move."""
    monkeypatch.setitem(sys.modules, "torch", _fake_torch(cuda=True, name="RTX 4090", free=3, total=9))
    sp.await_gpu_probe()
    assert sp._probe_gpu().free_vram_bytes == 3

    sp.reset_gpu_probe()
    monkeypatch.delitem(sys.modules, "torch", raising=False)
    remembered = sp._probe_gpu(wait=False)
    assert remembered.total_vram_bytes == 9
    assert remembered.free_vram_bytes is None


def test_a_corrupt_record_is_ignored_rather_than_believed(monkeypatch) -> None:
    from deepreefmap_gui.paths import gpu_probe_cache_path

    path = gpu_probe_cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    assert sp._probe_gpu(wait=False) is sp.GPU_PENDING


def test_probe_gpu_treats_mps_as_unified(monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "torch", _fake_torch(mps=True))
    gpu = sp._probe_gpu()
    assert gpu.kind == sp.GPU_MPS
    assert gpu.total_vram_bytes is None
    assert not gpu.has_distinct_vram


def test_probe_gpu_cpu_only(monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "torch", _fake_torch())
    assert sp._probe_gpu().kind == sp.GPU_NONE


def test_probe_gpu_tolerates_missing_torch(monkeypatch) -> None:
    # sys.modules[name] = None makes `import name` raise ImportError.
    monkeypatch.setitem(sys.modules, "torch", None)
    gpu = sp._probe_gpu()
    assert gpu.kind == sp.GPU_NONE


def test_probe_system_reports_positive_machine_figures() -> None:
    profile = sp.probe_system()
    assert profile.os_name  # "Linux" / "Windows" / "Darwin"
    assert profile.cpu_logical >= 1
    assert profile.total_ram_bytes > 0
    assert 0 <= profile.available_ram_bytes <= profile.total_ram_bytes
    assert profile.gpu.kind in (sp.GPU_CUDA, sp.GPU_MPS, sp.GPU_NONE)


def test_probe_system_bad_disk_path_is_zeroed(tmp_path) -> None:
    profile = sp.probe_system(tmp_path / "does" / "not" / "exist")
    assert profile.disk_free_bytes == 0
    assert profile.disk_total_bytes == 0


def test_profile_round_trips_through_json() -> None:
    profile = sp.probe_system()
    restored = json.loads(json.dumps(profile.to_dict()))
    assert restored["gpu"]["kind"] == profile.gpu.kind
    assert restored["total_ram_bytes"] == profile.total_ram_bytes


def test_utilisation_vram_percent() -> None:
    u = sp.Utilisation(1, 2, 50.0, 10.0, vram_used_bytes=250, vram_total_bytes=1000)
    assert u.vram_percent == 25.0
    assert sp.Utilisation(1, 2, 50.0, 10.0, None, None).vram_percent is None


def test_utilisation_swap_percent() -> None:
    u = sp.Utilisation(1, 2, 50.0, 10.0, None, None, swap_used_bytes=200, swap_total_bytes=1000)
    assert u.swap_percent == 20.0
    # No swap configured -> None, not a divide-by-zero.
    assert sp.Utilisation(1, 2, 50.0, 10.0, None, None).swap_percent is None


def test_sample_utilisation_reports_swap() -> None:
    u = sp.sample_utilisation()
    assert u.swap_total_bytes >= 0
    # Used can never exceed the pool it comes from. (The previous bound compared
    # used against max(total, used), which holds for any value.)
    assert 0 <= u.swap_used_bytes <= u.swap_total_bytes


def test_a_machine_that_will_not_report_swap_reports_none_not_zero(monkeypatch) -> None:
    """Scenario: psutil cannot read the swap pool at all.

    Expected behaviour: null, as the per-process reading already is. The
    sampler falls back to this figure, and a zero there is an observation the
    fleet's swap statistics count.
    """
    def refuse() -> None:
        raise OSError("no swap here")

    monkeypatch.setattr(sp.psutil, "swap_memory", refuse)

    u = sp.sample_utilisation()

    assert u.swap_used_bytes is None
    assert u.swap_percent is None


def test_sample_process_memory_reads_this_process() -> None:
    """The run's own footprint, which is what a stored peak has to mean.

    Runs on whichever platform the suite is on: the call has to come back with
    something usable everywhere, since a None sends the sampler back to the
    machine-wide reading it exists to replace.
    """
    mine = sp.sample_process_memory()

    assert mine is not None
    rss, swap = mine
    assert rss > 0
    # None off Linux, where the call carrying swap walks the working set and is
    # far too slow to poll twice a second.
    assert swap is None or swap >= 0
    # And it is this process, not the machine: the desktop around it is larger.
    assert rss < sp.sample_utilisation().ram_total_bytes


class _OneProcess:
    """A process tree of one, reporting a fixed footprint and no swap."""

    def __init__(self, swap: int = 0) -> None:
        self._swap = swap

    def children(self, recursive=False):
        return []

    def memory_full_info(self):
        return SimpleNamespace(rss=2048, swap=self._swap)

    def memory_info(self):
        return SimpleNamespace(rss=2048)


def test_process_swap_is_none_where_the_platform_will_not_report_it(monkeypatch) -> None:
    """Scenario: Windows or macOS, where reading per-process swap is too slow to poll.

    Expected behaviour: None, not zero. These peaks are pooled across the fleet,
    and a zero from every run on those platforms is counted as an observation.
    """
    monkeypatch.setattr(sp, "_PROCESS_SWAP_READABLE", False)
    monkeypatch.setattr(sp.psutil, "Process", lambda *a, **k: _OneProcess())

    assert sp.sample_process_memory() == (2048, None)


def test_a_linux_run_that_swapped_nothing_reports_zero(monkeypatch) -> None:
    """The measured zero has to stay distinguishable from the unmeasurable one."""
    monkeypatch.setattr(sp, "_PROCESS_SWAP_READABLE", True)
    monkeypatch.setattr(sp.psutil, "Process", lambda *a, **k: _OneProcess())

    assert sp.sample_process_memory() == (2048, 0)


def test_process_memory_survives_a_platform_that_will_not_report_it(monkeypatch) -> None:
    """psutil raises for a process it cannot inspect; the sampler has a fallback."""
    class _Blind:
        def children(self, recursive=False):
            return []

        def memory_full_info(self):
            raise RuntimeError("denied")

        def memory_info(self):
            raise RuntimeError("denied")

    monkeypatch.setattr(sp.psutil, "Process", lambda *a, **k: _Blind())
    assert sp.sample_process_memory() is None


def test_format_bytes() -> None:
    assert sp.format_bytes(None) == "\u2014"  # em dash placeholder for unknown
    assert sp.format_bytes(2 * 1024**3) == "2.0 GB"
    assert sp.format_bytes(512 * 1024**2) == "512 MB"
