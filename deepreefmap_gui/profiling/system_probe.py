"""Cross-platform machine probe: RAM, VRAM, CPU and disk, with no video needed."""

from __future__ import annotations

import logging
import platform
import sys
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

import psutil

logger = logging.getLogger(__name__)

# GPU memory kinds. "cuda" covers NVIDIA and the ROCm shim (both expose the
# torch.cuda API); "mps" is Apple unified memory (no distinct VRAM pool);
# "none" is CPU-only. "unknown" is the answer before the probe has landed --
# see start_gpu_probe.
GPU_CUDA = "cuda"
GPU_MPS = "mps"
GPU_NONE = "none"
GPU_UNKNOWN = "unknown"


@dataclass(frozen=True)
class GpuInfo:
    kind: str
    name: str
    total_vram_bytes: int | None  # None when unified (MPS) or unknown
    free_vram_bytes: int | None

    @property
    def has_distinct_vram(self) -> bool:
        """True when the GPU has its own memory pool to budget separately from RAM."""
        return self.kind == GPU_CUDA and self.total_vram_bytes is not None

    @property
    def resolved(self) -> bool:
        """False while the probe is still running, so a caller can say so."""
        return self.kind != GPU_UNKNOWN


@dataclass(frozen=True)
class SystemProfile:
    os_name: str
    os_release: str
    cpu_logical: int
    cpu_physical: int | None
    total_ram_bytes: int
    available_ram_bytes: int
    total_swap_bytes: int
    free_swap_bytes: int
    gpu: GpuInfo
    disk_total_bytes: int
    disk_free_bytes: int
    disk_path: str

    def to_dict(self) -> dict:
        """Flatten to a JSON-serialisable dict for run_timings metadata."""
        return asdict(self)


def _cuda_vram(torch) -> tuple[int | None, int | None]:
    """Total and free VRAM, or (None, None) where the build cannot report it.

    ROCm builds do not all implement mem_get_info, and a card is still a card
    without a byte count: this must not decide whether a GPU exists.
    """
    try:
        free, total = torch.cuda.mem_get_info(0)
        return int(total), int(free)
    except Exception:
        return None, None


def _cuda_name(torch) -> str:
    try:
        return torch.cuda.get_device_name(0)
    except Exception:
        return "GPU"


def _resolve_gpu_in_process() -> GpuInfo:
    """Read GPU kind, name and (where it exists) VRAM, tolerating any failure.

    Slow, and only worth doing once: importing torch costs half a second, and its
    first device enumeration costs seconds again -- 3.3 s on the ROCm build --
    because the GPU driver initialises inside it.
    """
    # torch is imported lazily so the RAM/CPU/disk probe stays usable when torch is
    # absent or slow to load.
    try:
        import torch
    except Exception:
        return GpuInfo(GPU_NONE, "CPU only (torch unavailable)", None, None)

    try:
        cuda = torch.cuda.is_available()
    except Exception:
        cuda = False
    if cuda:
        total, free = _cuda_vram(torch)
        return GpuInfo(GPU_CUDA, _cuda_name(torch), total, free)
    try:
        if torch.backends.mps.is_available():
            # Apple Silicon: the GPU draws from system RAM, so there is no
            # separate VRAM figure to report.
            return GpuInfo(GPU_MPS, "Apple GPU (unified memory)", None, None)
    except Exception:
        pass
    return GpuInfo(GPU_NONE, "CPU only", None, None)


# Run in a child interpreter by _resolve_gpu. Imports nothing this package owns,
# so the child starts on a bare interpreter and pays only for torch.
_PROBE_SOURCE = """
import json, sys
out = {"kind": "none", "name": "CPU only", "total": None, "free": None}
try:
    import torch
    if torch.cuda.is_available():
        out["kind"] = "cuda"
        try:
            out["name"] = torch.cuda.get_device_name(0)
        except Exception:
            out["name"] = "GPU"
        try:
            free, total = torch.cuda.mem_get_info(0)
            out["total"], out["free"] = int(total), int(free)
        except Exception:
            pass
    elif getattr(torch.backends, "mps", None) is not None and torch.backends.mps.is_available():
        out["kind"] = "mps"
        out["name"] = "Apple GPU (unified memory)"
except Exception as exc:
    out["error"] = str(exc)[:300]
# On its own last line and prefixed: a ROCm banner, a vendor .pth or a
# sitecustomize print reaches this stdout too, and the parent has to find the
# answer among them. Guarded because a GUI-subsystem child can have no stdout
# at all, and dying here would look to the parent like a driver that crashed.
try:
    sys.stdout.write(chr(10) + "DRM_GPU " + json.dumps(out) + chr(10))
    sys.stdout.flush()
except Exception:
    pass
"""

_PROBE_MARKER = "DRM_GPU "

# Generous: this is a driver waking up, behind whatever else the machine is doing
# on the first boot of the day. Only a hung child hits it.
_PROBE_TIMEOUT_S = 120

# What a probe that could not get an answer reports. Distinct from a machine that
# really has no card only in that it is never written to the cache file: a driver
# that crashed once must not tell the next launch there is no card.
_UNVERIFIED_NONE = GpuInfo(GPU_NONE, "CPU only (could not check)", None, None)


def _parse_probe_output(text: str) -> dict | None:
    """The answer the child marked, ignoring whatever else reached its stdout."""
    import json

    for line in reversed(text.splitlines()):
        stripped = line.strip()
        if not stripped.startswith(_PROBE_MARKER):
            continue
        try:
            payload = json.loads(stripped[len(_PROBE_MARKER):])
        except ValueError:
            return None
        return payload if isinstance(payload, dict) else None
    return None


def _resolve_gpu() -> GpuInfo:
    """Identify the card in a child interpreter, falling back to this one.

    A thread would do for the wall clock but not for the window: importing torch
    is thousands of Python bytecodes holding the GIL, so a probe running here
    stops Qt repainting for as long as it takes and the app comes up as a black
    rectangle. A child process shares no interpreter lock with the one drawing.

    The cost is that torch stays unimported here, so the run that needs it pays
    its import then -- on the batch worker, behind a progress bar, which is where
    a wait belongs.
    """
    import subprocess

    if "torch" in sys.modules:
        # Already loaded -- during a run, or under a test that supplied its own.
        # The expense this function exists to avoid has been paid, and a child
        # would answer for a different interpreter than the one about to process.
        return _resolve_gpu_in_process()

    try:
        proc = subprocess.run(
            [sys.executable, "-c", _PROBE_SOURCE],
            capture_output=True,
            timeout=_PROBE_TIMEOUT_S,
            check=False,
            # A GUI-subsystem parent on Windows has no console to inherit, and
            # without this the child flashes one on screen.
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception as exc:
        # The child never ran: no interpreter, no permission to spawn, a timeout.
        # Nothing has been learned about the card, and nothing about torch has
        # been shown to be dangerous, so this process can ask it directly.
        logger.warning("Could not run the GPU probe (%s); asking torch here instead", exc)
        return _resolve_gpu_in_process()

    if proc.returncode != 0:
        # It ran and died. Overwhelmingly that is the driver taking the process
        # down inside the enumeration, so repeating the call in this process
        # would take the window with it. Report no card and say why.
        logger.error(
            "The GPU probe exited with %s; treating this machine as CPU-only. stderr: %s",
            proc.returncode,
            proc.stderr.decode("utf-8", "replace")[-500:].strip() or "(none)",
        )
        return _UNVERIFIED_NONE

    payload = _parse_probe_output(proc.stdout.decode("utf-8", "replace"))
    if payload is None:
        logger.error(
            "The GPU probe printed no answer; treating this machine as CPU-only. stdout: %s",
            proc.stdout.decode("utf-8", "replace")[-500:].strip() or "(empty)",
        )
        return _UNVERIFIED_NONE

    if payload.get("error"):
        logger.warning("GPU probe reported: %s", payload["error"])
    kind = payload.get("kind", GPU_NONE)
    if kind not in (GPU_CUDA, GPU_MPS, GPU_NONE):
        kind = GPU_NONE
    return GpuInfo(kind, payload.get("name") or "CPU only", payload.get("total"), payload.get("free"))


# The card as identified once. Free VRAM is refreshed per read from here; kind,
# name and total are fixed for the process.
GPU_PENDING = GpuInfo(GPU_UNKNOWN, "Checking…", None, None)

class _GpuProbe:
    """Everything the probe remembers, in one object rather than five globals.

    Mutated, never rebound, so the module-level name stays the single handle the
    lock protects and reset_gpu_probe has one thing to clear.
    """

    def __init__(self) -> None:
        self.lock = threading.Lock()
        # The card as identified this session. Free VRAM is refreshed per read
        # from it; kind, name and total are fixed once it is set.
        self.identity: GpuInfo | None = None
        self.thread: threading.Thread | None = None
        self.waiters: list[Callable[[GpuInfo], None]] = []
        # The card from the last time the app ran here, and whether the file
        # holding it has been read yet this session.
        self.remembered: GpuInfo | None = None
        self.remembered_read = False

    def clear(self) -> None:
        with self.lock:
            self.identity = None
            self.thread = None
            self.waiters.clear()
            self.remembered = None
            self.remembered_read = False


_gpu = _GpuProbe()


def _torch_build() -> str:
    """The installed torch distribution's version, without importing it.

    Which card torch reports is a property of the wheel as much as of the
    machine: a cu130 build on an AMD card finds nothing, and the rocm build of
    the same torch finds the card. So this is what a remembered answer is keyed
    on -- switching extras must not leave the old verdict standing. Reads a
    metadata file, about a millisecond, where importing torch costs seconds.
    """
    import importlib.metadata

    try:
        return importlib.metadata.version("torch")
    except Exception:
        return "absent"


def _cached_gpu() -> GpuInfo | None:
    """What the card was last time this app ran here, with this torch, if anything.

    The answer costs seconds of driver initialisation to obtain and is the same
    on every launch of a laptop nobody has opened, so the first frame is drawn
    from it and the live probe corrects it if it has changed. Never the only
    source: a machine whose card was removed is told so within seconds.
    """
    # Read once: every repaint that grades this machine asks, and the file cannot
    # change under a running app -- only this app writes it. Under the lock
    # because probe_system(wait_for_gpu=False) is public and the batch calls it
    # off the GUI thread, so this is not a single-threaded reader.
    with _gpu.lock:
        if _gpu.remembered_read:
            return _gpu.remembered

    import json

    from deepreefmap_gui.paths import gpu_probe_cache_path

    remembered: GpuInfo | None
    try:
        payload = json.loads(gpu_probe_cache_path().read_text(encoding="utf-8"))
        kind = payload["kind"]
        if kind not in (GPU_CUDA, GPU_MPS, GPU_NONE):
            raise ValueError(f"unknown kind {kind!r}")
        if payload.get("torch") != _torch_build():
            raise ValueError("recorded against a different torch build")
        # No free VRAM: what was left on the card last session says nothing
        # about now, and the System tab's gauge would paint it as live.
        remembered = GpuInfo(kind, payload["name"], payload.get("total"), None)
    except FileNotFoundError:
        remembered = None  # never run on this machine
    except Exception as exc:
        logger.info("Not reusing the recorded graphics card: %s", exc)
        remembered = None
    with _gpu.lock:
        _gpu.remembered = remembered
        _gpu.remembered_read = True
    return remembered


def _cache_gpu(info: GpuInfo) -> None:
    """Record the card for the next launch, if this one actually found out.

    A probe that could not get an answer is not evidence of no card, and writing
    it would tell the next launch there is none: an antivirus that blocked the
    child once, or a driver mid-reinstall, would leave the machine reading
    CPU-only until something happened to overwrite it. Free VRAM is left out for
    the same reason -- next launch it is a stale number, not a measurement.
    """
    if info is _UNVERIFIED_NONE:
        return

    import json

    from deepreefmap_gui.paths import gpu_probe_cache_path

    path = gpu_probe_cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "kind": info.kind,
                    "name": info.name,
                    "total": info.total_vram_bytes,
                    "torch": _torch_build(),
                }
            ),
            encoding="utf-8",
        )
    except OSError as exc:
        logger.warning("Could not record the graphics card for next launch: %s", exc)


def gpu_hint() -> GpuInfo:
    """The card as best known right now: probed if it has been, remembered if not.

    What the window paints with before its own probe lands. GPU_PENDING only on a
    machine this app has never run on.
    """
    with _gpu.lock:
        if _gpu.identity is not None:
            return _gpu.identity
    return _cached_gpu() or GPU_PENDING


def start_gpu_probe(on_done: Callable[[GpuInfo], None] | None = None) -> None:
    """Identify the card on a daemon thread, once per process.

    Idempotent, and safe from any thread. `on_done` runs when the answer exists --
    on the probe thread, or inline if the answer is already in -- so a Qt caller
    passes something that only emits a signal.
    """
    with _gpu.lock:
        settled = _gpu.identity
        if settled is None:
            if on_done is not None:
                _gpu.waiters.append(on_done)
            if _gpu.thread is None:
                _gpu.thread = threading.Thread(target=_run_gpu_probe, name="gpu-probe", daemon=True)
                _gpu.thread.start()
    if settled is not None and on_done is not None:
        on_done(settled)


def _run_gpu_probe() -> None:
    """The probe thread. It must settle an answer whatever happens.

    Everything after the probe is in the `finally`: this thread is created once
    and never replaced, so an exception escaping here would leave the identity
    unset and the waiters uncalled for the life of the process -- the strip
    spinning "Checking graphics card" forever, and every later blocking read
    joining a dead thread and concluding there is no card.
    """
    info = _UNVERIFIED_NONE
    try:
        info = _resolve_gpu()
        _cache_gpu(info)
    except Exception:
        logger.exception("GPU probe failed; this machine will read as CPU-only")
    finally:
        with _gpu.lock:
            _gpu.identity = info
            waiters = list(_gpu.waiters)
            _gpu.waiters.clear()
        for waiter in waiters:
            try:
                waiter(info)
            except Exception:
                logger.exception("GPU probe callback failed")


def await_gpu_probe(timeout: float | None = None) -> None:
    """Block until the card is identified. Never call this from the GUI thread."""
    start_gpu_probe()
    with _gpu.lock:
        thread = _gpu.thread
    if thread is not None:
        thread.join(timeout)


def _probe_gpu(*, wait: bool = True) -> GpuInfo:
    """The card, with free VRAM as of now.

    `wait=False` answers from what the last launch recorded, or GPU_PENDING on a
    machine with no record, rather than paying for the identification. It is what
    the GUI thread passes.
    """
    with _gpu.lock:
        settled = _gpu.identity
    if settled is None:
        if not wait:
            # Deliberately does not start the probe: the GUI owns when that
            # happens, so that it is once and after the window is on screen.
            return _cached_gpu() or GPU_PENDING
        await_gpu_probe()
        with _gpu.lock:
            settled = _gpu.identity
        if settled is None:  # probe thread died without recording anything
            return GpuInfo(GPU_NONE, "CPU only", None, None)
    if settled.kind != GPU_CUDA or "torch" not in sys.modules:
        # Only once something else in this process has torch loaded -- during a
        # run. Importing it here to freshen one number would cost seconds, and
        # cost them on whichever thread asked, which is usually the one painting.
        return settled
    _total, free = _cuda_vram(sys.modules["torch"])
    return GpuInfo(settled.kind, settled.name, settled.total_vram_bytes, free)


def reset_gpu_probe() -> None:
    """Forget the card, probed and remembered, so the next probe runs again. For tests."""
    _gpu.clear()


def gpu_present(*, wait: bool = True) -> bool:
    """Whether processing can use a card. The readiness row and the run gate both
    ask this, so they cannot disagree about the same machine.

    Unknown counts as present: the gates this feeds only ever take something away,
    and blocking a run on a card that was merely not yet counted is worse than
    letting the probe land a moment later and block it then.
    """
    return _probe_gpu(wait=wait).kind != GPU_NONE


def probe_system(disk_path: Path | str | None = None, *, wait_for_gpu: bool = True) -> SystemProfile:
    """Snapshot the machine's memory, CPU, GPU and free disk on the output volume.

    `wait_for_gpu=False` reports GPU_UNKNOWN rather than blocking on the first
    torch device enumeration; it is what the GUI thread passes.
    """
    vm = psutil.virtual_memory()
    try:
        sw = psutil.swap_memory()
        swap_total, swap_free = int(sw.total), int(sw.free)
    except Exception:
        swap_total = swap_free = 0
    path = Path(disk_path) if disk_path is not None else Path.cwd()
    try:
        du = psutil.disk_usage(str(path))
        disk_total, disk_free = int(du.total), int(du.free)
    except OSError:
        disk_total = disk_free = 0
    return SystemProfile(
        os_name=platform.system(),
        os_release=platform.release(),
        cpu_logical=psutil.cpu_count(logical=True) or 1,
        cpu_physical=psutil.cpu_count(logical=False),
        total_ram_bytes=int(vm.total),
        available_ram_bytes=int(vm.available),
        total_swap_bytes=swap_total,
        free_swap_bytes=swap_free,
        gpu=_probe_gpu(wait=wait_for_gpu),
        disk_total_bytes=disk_total,
        disk_free_bytes=disk_free,
        disk_path=str(path),
    )


@dataclass(frozen=True)
class Utilisation:
    ram_used_bytes: int
    ram_total_bytes: int
    ram_percent: float
    cpu_percent: float
    vram_used_bytes: int | None
    vram_total_bytes: int | None
    # None, never zero, where the platform would not report swap at all. The
    # sampler falls back to this reading when it cannot measure the run's own
    # pages, and a zero there is an observation the fleet's statistics count.
    swap_used_bytes: int | None = None
    swap_total_bytes: int = 0

    @property
    def vram_percent(self) -> float | None:
        if self.vram_used_bytes is None or not self.vram_total_bytes:
            return None
        return 100.0 * self.vram_used_bytes / self.vram_total_bytes

    @property
    def swap_percent(self) -> float | None:
        """Swap in use as a percent, or None when the machine has no swap."""
        if self.swap_used_bytes is None or not self.swap_total_bytes:
            return None
        return 100.0 * self.swap_used_bytes / self.swap_total_bytes


def sample_utilisation() -> Utilisation:
    """A cheap live snapshot for the GUI gauges, safe to call on a timer.

    ``cpu_percent`` is the non-blocking reading (percent since the previous call), so
    drive it from a steady interval rather than a one-off call for a real figure.
    """
    vm = psutil.virtual_memory()
    try:
        sw = psutil.swap_memory()
        swap_used, swap_total = int(sw.used), int(sw.total)
    except Exception:
        # Null rather than zero, for the reason sample_process_memory gives: the
        # sampler falls back to this figure, and a machine that will not report
        # swap must stay out of the fleet's swap statistics rather than vote a
        # nothing into them. A machine with no swap still reports a real zero.
        swap_used, swap_total = None, 0
    vram_used = vram_total = None
    # Driven from a repaint timer on the GUI thread, so it reports no VRAM until
    # the probe lands rather than freezing the window for the length of it.
    gpu = _probe_gpu(wait=False)
    if gpu.has_distinct_vram and gpu.total_vram_bytes is not None and gpu.free_vram_bytes is not None:
        vram_total = gpu.total_vram_bytes
        vram_used = gpu.total_vram_bytes - gpu.free_vram_bytes
    return Utilisation(
        ram_used_bytes=int(vm.total - vm.available),
        ram_total_bytes=int(vm.total),
        ram_percent=float(vm.percent),
        cpu_percent=psutil.cpu_percent(interval=None),
        vram_used_bytes=vram_used,
        vram_total_bytes=vram_total,
        swap_used_bytes=swap_used,
        swap_total_bytes=swap_total,
    )


# Per-process swap is a Linux figure (VmSwap, via smaps_rollup). It is also the
# only platform where the call that carries it is cheap: on Windows the same
# psutil call walks the process's working set to compute USS, which on a
# multi-gigabyte run is far too slow to poll twice a second.
_PROCESS_SWAP_READABLE = sys.platform.startswith("linux")


def sample_process_memory() -> tuple[int, int | None] | None:
    """This process tree's resident and swapped-out bytes, or None if unreadable.

    The pipeline runs inside this process, so its own footprint is the run's
    demand. The machine-wide reading is the wrong basis for a peak that is stored
    and compared later: it charges the run for every other application on the
    desktop, and on a machine with pages already in swap it charged it for those
    too, which made a light mapping backend record a larger peak than a heavy one
    and left the memory grade unable to rank them.

    The swap half is None off Linux, where the figure cannot be read cheaply.
    None rather than zero, because these peaks are pooled across the fleet: a
    zero reads as an observation, and one from every Windows and macOS run pulled
    the fleet's mean swap towards nothing and invented variance around it. A
    Linux run that genuinely swapped nothing still reports zero. The local memory
    estimate is unaffected either way: it reads an absent swap figure as no
    spill, which is the direction that leaves the model's own estimate standing
    rather than talking a machine into a run that will not fit.
    """
    try:
        me = psutil.Process()
        processes = [me, *me.children(recursive=True)]
    except Exception:
        return None
    rss = swap = 0
    read_any = False
    for proc in processes:
        try:
            info = proc.memory_full_info() if _PROCESS_SWAP_READABLE else proc.memory_info()
        except Exception:
            continue
        rss += int(getattr(info, "rss", 0) or 0)
        swap += int(getattr(info, "swap", 0) or 0)
        read_any = True
    if not read_any:
        return None
    return (rss, swap if _PROCESS_SWAP_READABLE else None)


def format_bytes(n: float | None) -> str:
    """Render a byte count as `3.4 GB` / `812 MB`, or a dash when unknown.

    Binary units throughout, matching what an OS disk dialog reports. This is the
    only byte formatter in the app on purpose: a second SI-based one meant the
    Data panel showed a run as "4.29 GB" while the System panel showed the free
    space it had to fit into as "4.0 GB", from the same byte count.
    """
    if n is None:
        return "—"
    gb = n / 1024**3
    if gb >= 1.0:
        return f"{gb:.1f} GB"
    return f"{n / 1024**2:.0f} MB"
