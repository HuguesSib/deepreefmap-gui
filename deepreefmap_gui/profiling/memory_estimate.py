"""Whether a run fits in this machine, and if not, what to change.

Peak memory is modelled as a fixed cost plus a per-frame cost, separately for
RAM and VRAM, from the tables in model_costs.py. Both limits are inverted to a
frame ceiling, so the answer to "it does not fit" is a length the machine can
actually process rather than a number the user has to interpret.

Swap counts towards the RAM budget where the machine has it, capped at the
installed RAM again. A run that spills finishes, far more slowly, and refusing
one outright on a machine with 32 GB of swap sitting idle is the wrong answer.
The cap is the honest half: past roughly its own RAM again the working set is
mostly on disk and the run thrashes rather than degrades. Recorded peaks are
already RAM plus the swap the run spilled into (run_history.py), so a budget
without swap graded a measured demand against a pool it was never measured in
and called a run that had succeeded here impossible.

Spilling into swap is not a fault, so it is not a warning: a run that fits the
combined pool grades "ok" and the readout says how much of it runs from swap and
that this is slower. A machine that warns on every run it can actually do is a
machine the user learns to ignore.

The pool is held to what is free as well as to what is installed. A desktop
sitting in 20 GB does not hand it over because a run would like it, and a verdict
graded against the box's figure alone was green on runs that would be killed. The
two are different problems and the wording keeps them apart: the machine being
too small is answered by trimming the pass, another application holding the
memory is answered by closing it.

A Linux RAM exhaustion is an uncatchable OOM kill, so this advises before a long
run rather than crash into it.

The overall verdict names whichever pool is tighter, but `Verdict.resources`
grades both separately and always returns both, including on a machine where one
of them does not exist. They are separate hardware with separate answers -- a
laptop can have memory to spare and a card that cannot take the run at all -- and
a reader shown one figure cannot tell which of the two they are looking at.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from deepreefmap_gui.profiling.model_costs import (
    FRAME_BYTES_PER_PIXEL,
    INTERPRETER_BASELINE_BYTES,
    mapping_cost,
    segmentation_cost,
)
from deepreefmap_gui.profiling.system_probe import GPU_CUDA, GPU_MPS, SystemProfile, format_bytes

# The OS, desktop and background apps hold RAM the run can never claim. Grading
# against full physical RAM assumes a bare machine.
_OS_RESERVE_FRACTION = 0.12
_OS_RESERVE_MIN = 3 * 1024**3
_OS_RESERVE_MAX = 8 * 1024**3

# Share of the budget above which a run is close enough to warn about. The wall
# is higher on a machine with swap, and the last stretch of it is disk: a run
# that runs out there slows to a crawl first rather than being killed outright,
# so the margin that has to be kept clear is a thinner one.
_WARN_FRACTION = 0.85
_SWAP_WARN_FRACTION = 0.95

# Allocator slack and per-block transients.
_OVERHEAD = 1.15

# The desktop compositor and allocator fragmentation on the card, which a run
# never gets back.
_GPU_RESERVE_FRACTION = 0.08
_GPU_RESERVE_MIN = 512 * 1024**2
_GPU_RESERVE_MAX = int(1.5 * 1024**3)


def _os_reserve(total_ram_bytes: int) -> int:
    return int(min(_OS_RESERVE_MAX, max(_OS_RESERVE_MIN, total_ram_bytes * _OS_RESERVE_FRACTION)))


def _usable_swap(profile: SystemProfile) -> int:
    """Swap a run may count on: what the machine has, up to its RAM again.

    Installed rather than currently free, for the reason the RAM budget is:
    a verdict that moves because something else swapped cannot be acted on.

    What "has" means is the platform's answer, and two of the three understate it.
    Windows reports the pagefile at its present size, which the system grows on
    demand, and macOS reports a swapfile that starts near nothing and grows the
    same way. Both err towards a smaller budget than the machine would really
    give, which is the side to be wrong on.
    """
    return max(0, min(profile.total_swap_bytes, profile.total_ram_bytes))


def _gpu_reserve(total_vram_bytes: int) -> int:
    return int(
        min(_GPU_RESERVE_MAX, max(_GPU_RESERVE_MIN, total_vram_bytes * _GPU_RESERVE_FRACTION))
    )


@dataclass(frozen=True)
class RunShape:
    """The settings that decide what a run costs."""

    frames: int
    width: int
    height: int
    mapping_backend: str
    seg_model: str
    batch_size: int = 4


@dataclass(frozen=True)
class Stage:
    """One stage's RAM as a fixed cost plus a per-frame cost.

    Stages do not coexist, so a run's peak is the worst of them and its frame
    ceiling is the tightest of theirs.
    """

    name: str
    fixed_bytes: int
    bytes_per_frame: int

    def bytes_at(self, frames: int) -> int:
        return self.fixed_bytes + self.bytes_per_frame * frames

    def frame_ceiling(self, budget_bytes: int) -> int:
        if self.bytes_per_frame <= 0:
            return _UNLIMITED if budget_bytes >= self.fixed_bytes else 0
        return max(0, (budget_bytes - self.fixed_bytes) // self.bytes_per_frame)


@dataclass(frozen=True)
class Cost:
    """Peak memory for a run, per stage, so a frame ceiling can be derived."""

    stages: tuple[Stage, ...]
    fixed_vram_bytes: int
    vram_bytes_per_frame: int
    frames: int
    source: str  # "measured" | "estimated" -- of the RAM figures
    vram_source: str = "estimated"  # "measured" once a peak from this card lands
    # Which of the two terms set the fixed VRAM figure: they do not add (the
    # segmenter is released before mapping loads), so one of them is the whole
    # number. Blaming the wrong one sends the user to a control that cannot help.
    vram_fixed_from: str = "mapping"  # "mapping" | "segmentation"

    @property
    def ram_bytes(self) -> int:
        return max(stage.bytes_at(self.frames) for stage in self.stages)

    @property
    def peak_stage(self) -> Stage:
        return max(self.stages, key=lambda stage: stage.bytes_at(self.frames))

    @property
    def vram_bytes(self) -> int:
        return self.fixed_vram_bytes + self.vram_bytes_per_frame * self.frames


@dataclass(frozen=True)
class Budget:
    """What the machine can actually give a run."""

    ram_bytes: int
    vram_bytes: int | None
    unified: bool  # GPU draws from system RAM, so one pool serves both
    swap_bytes: int = 0  # spill space, slow but real
    # RAM and swap actually free when the machine was last probed. Zero means the
    # probe said nothing, not that nothing is free.
    free_bytes: int = 0

    @property
    def memory_bytes(self) -> int:
        """Everything a run could occupy on a machine with nothing else running."""
        return self.ram_bytes + self.swap_bytes

    @property
    def usable_bytes(self) -> int:
        """What a run can have right now: neither more than the machine's share of
        its own memory, nor more than what is genuinely free.

        The installed figure alone was the optimistic one -- a desktop holding
        20 GB does not hand it over because a run would like it -- and grading a
        run "ok" against memory another application is sitting in is how a green
        readout turns into an OOM kill.
        """
        if not self.free_bytes:
            return self.memory_bytes
        return min(self.memory_bytes, self.free_bytes)


@dataclass(frozen=True)
class ResourceFit:
    """One pool graded on its own: what it holds, what the run wants of it.

    The two pools are separate hardware and a run is held to both, so each is
    reported on its own track with its own verdict. Folding them into one figure
    hid a card that could not take the run behind memory that comfortably could.
    """

    key: str  # "ram" | "vram"
    label: str
    level: str  # "ok" | "warn" | "block" | "none"
    message: str
    need_bytes: int = 0
    budget_bytes: int = 0
    held_bytes: int = 0
    swap_need_bytes: int = 0

    @property
    def available(self) -> bool:
        """Whether there is a pool here to measure at all."""
        return self.level != "none"

    @property
    def fits(self) -> bool:
        return self.level in ("ok", "none")


@dataclass(frozen=True)
class Verdict:
    level: str  # "ok" | "warn" | "block"
    cost: Cost
    budget: Budget
    # "ram" | "vram" | "ram_fixed" | "vram_fixed" | "". The _fixed pair are a
    # different fact from the other two: they are decided before the first frame,
    # so nothing the user can set moves them.
    limit: str
    max_frames: int
    percent: float
    headline: str
    detail: str
    shape: RunShape | None = None
    # Demand on the RAM side, the GPU's share included where the pool is shared.
    memory_need_bytes: int = 0
    # Frames that fit in what is free at this moment, as against max_frames, which
    # is what the machine can give a run when nothing else is holding it.
    max_frames_now: int = 0

    @property
    def fits(self) -> bool:
        return self.level == "ok"

    @property
    def limit_is_fixed(self) -> bool:
        return self.limit in ("ram_fixed", "vram_fixed")

    @property
    def need_bytes(self) -> int:
        """Demand on whichever resource decided the verdict."""
        if self.limit.startswith("vram"):
            return self.cost.vram_bytes
        return self.memory_need_bytes or self.cost.ram_bytes

    @property
    def swap_need_bytes(self) -> int:
        """How much of the demand lands in swap, which is what makes a run slow."""
        if self.limit.startswith("vram") or not self.budget.swap_bytes:
            return 0
        return max(0, min(self.need_bytes, self.budget.usable_bytes) - self.budget.ram_bytes)

    @property
    def budget_bytes(self) -> int:
        """The pool the demand is measured against: what is free to give it.

        Swap is in the figure whether or not this particular run reaches into it.
        What the machine can give a run does not depend on which run is asking,
        and a denominator that grew when the mapping method changed read as
        though the computer had gained memory.
        """
        if self.limit.startswith("vram"):
            return self.budget.vram_bytes or 0
        return self.budget.usable_bytes

    @property
    def held_by_others_bytes(self) -> int:
        """Memory this machine has but something else is in, at probe time."""
        return max(0, self.budget.memory_bytes - self.budget.usable_bytes)

    @property
    def budget_label(self) -> str:
        """What that pool is called, so a readout never quotes swap as plain memory."""
        if self.limit.startswith("vram"):
            return "graphics memory"
        return "memory and swap" if self.budget.swap_bytes else "memory"

    @property
    def resources(self) -> tuple[ResourceFit, ResourceFit]:
        """Both pools, memory first, each graded against itself.

        Always both, whatever decided the overall verdict: a run is held to the
        card and to the memory at once, and a reader who is shown only the
        tighter of the two cannot tell which one they are looking at.
        """
        return (_ram_resource(self), _vram_resource(self))


def estimate_cost(shape: RunShape, *, recorded: dict | None = None) -> Cost:
    """What this run is expected to peak at.

    ``recorded`` is a measured peak from a comparable run. It replaces the fixed
    term only: the per-frame slope stays analytic, so a peak recorded at one
    length does not scale its own constant baseline when applied to another.
    """
    mapping = mapping_cost(shape.mapping_backend)
    segmentation = segmentation_cost(shape.seg_model)

    # Prepared frames are built before mapping loads and stay resident to the
    # end, so every stage carries them.
    prepared = int(max(1, shape.width * shape.height) * FRAME_BYTES_PER_PIXEL)
    resident = INTERPRETER_BASELINE_BYTES + mapping.weights_ram_bytes

    def slack(value: float) -> int:
        return int(value * _OVERHEAD)

    stages = [
        # Preprocessing: frames accumulate under the segmentation model.
        Stage("preprocess", slack(INTERPRETER_BASELINE_BYTES + segmentation.load_ram_bytes), slack(prepared)),
        # Loading the mapping checkpoint, on top of every prepared frame.
        Stage("load", slack(INTERPRETER_BASELINE_BYTES + mapping.load_ram_bytes), slack(prepared)),
        Stage("mapping", slack(resident), slack(prepared + mapping.merge_bytes_per_frame())),
        Stage("cloud", slack(resident), slack(prepared + mapping.cloud_bytes_per_frame())),
    ]

    source = "estimated"
    if recorded and recorded.get("frames") and recorded.get("ram_bytes"):
        # A recorded peak fixes the level; the analytic slope still shapes it,
        # so a peak measured at one length does not rescale its own baseline.
        recorded_frames = int(recorded["frames"])
        predicted = max(stage.bytes_at(recorded_frames) for stage in stages)
        shortfall = int(recorded["ram_bytes"]) - predicted
        if shortfall > 0:
            stages = [
                Stage(stage.name, stage.fixed_bytes + shortfall, stage.bytes_per_frame)
                for stage in stages
            ]
        source = "measured"

    seg_vram = segmentation.vram_bytes(shape.batch_size)
    fixed_vram = max(seg_vram, mapping.vram_fixed_bytes)
    vram_source = "estimated"
    if recorded and recorded.get("vram_bytes") and recorded.get("vram_frames") is not None:
        # VRAM is a single line, a fixed term plus a slope, so one recorded
        # point identifies the intercept in either direction. The caller offers
        # a peak from this card at this batch size only, and the figure is
        # device-wide use, so correcting to it errs high.
        implied = int(recorded["vram_bytes"]) - mapping.vram_bytes_per_frame * int(
            recorded["vram_frames"]
        )
        if implied > 0:
            # Segmentation runs before mapping loads and is modelled separately,
            # so it floors the fixed term whatever mapping turned out to cost.
            fixed_vram = max(seg_vram, implied)
            vram_source = "measured"

    return Cost(
        stages=tuple(stages),
        fixed_vram_bytes=fixed_vram,
        vram_bytes_per_frame=mapping.vram_bytes_per_frame,
        frames=max(0, shape.frames),
        source=source,
        vram_source=vram_source,
        vram_fixed_from="segmentation" if fixed_vram == seg_vram else "mapping",
    )


def machine_budget(profile: SystemProfile) -> Budget:
    """RAM and VRAM a run may claim.

    Two figures, and a run is held to both. ``ram_bytes`` is installed less an OS
    reserve: what this machine could give a run, which does not move when a
    browser opens and is what the frame ceilings are quoted from. ``free_bytes``
    is what is actually free at this moment, which is what a run will really get.
    Grading on the first alone was optimistic; grading on the second alone would
    tell a diver to trim a transect when the answer is to close a browser.

    VRAM is graded the same way and for the same reason. What the driver reports
    free moves under the user, on Windows even while nothing is running.
    """
    ram = max(0, profile.total_ram_bytes - _os_reserve(profile.total_ram_bytes))
    swap = _usable_swap(profile)
    # What is free right now, which the run is also held to: the installed figure
    # says what the machine could give a run, not what it has left to give.
    free = max(0, profile.available_ram_bytes) + max(0, min(profile.free_swap_bytes, swap))
    gpu = profile.gpu
    if gpu.kind == GPU_MPS:
        return Budget(
            ram_bytes=ram, vram_bytes=None, unified=True, swap_bytes=swap, free_bytes=free
        )
    vram = None
    if gpu.kind == GPU_CUDA:
        # Installed less a reserve, for the reason above. A card that reports
        # only what is free still gets graded against that -- a noisy budget
        # beats no budget at all, because a VRAM budget of None means every run
        # grades "ok" on the graphics card however large it is.
        total = gpu.total_vram_bytes or gpu.free_vram_bytes
        if total:
            vram = max(0, total - _gpu_reserve(total))
    return Budget(
        ram_bytes=ram, vram_bytes=vram, unified=False, swap_bytes=swap, free_bytes=free
    )


def _max_frames_for(cost: Cost, budget: Budget, *, pool: int | None = None) -> int:
    """Frames the tightest stage allows.

    Against what the machine can give a run by default, not against what is free
    this second: a length that dropped because a browser opened is not a length
    anybody can plan a dive around. The caller passes the live pool when it needs
    the other question answered -- what would fit right now.
    """
    if pool is None:
        pool = budget.memory_bytes
    stages = cost.stages
    if budget.unified:
        # One pool: the GPU's demand comes out of the same RAM.
        stages = tuple(
            Stage(
                stage.name,
                stage.fixed_bytes + cost.fixed_vram_bytes,
                stage.bytes_per_frame + cost.vram_bytes_per_frame,
            )
            for stage in stages
        )
    ceiling = min(stage.frame_ceiling(pool) for stage in stages)
    if budget.vram_bytes is not None and not budget.unified:
        vram_stage = Stage("vram", cost.fixed_vram_bytes, cost.vram_bytes_per_frame)
        ceiling = min(ceiling, vram_stage.frame_ceiling(budget.vram_bytes))
    return min(ceiling, _UNLIMITED)


_UNLIMITED = 1 << 40


def max_frames(profile: SystemProfile, shape: RunShape, *, recorded: dict | None = None) -> int:
    """Frames this machine can process in one pass at these settings."""
    return _max_frames_for(estimate_cost(shape, recorded=recorded), machine_budget(profile))


def grade(
    profile: SystemProfile, shape: RunShape, *, recorded: dict | None = None
) -> Verdict:
    """Whether the run fits, what limits it, and how much it would have to shrink."""
    cost = estimate_cost(shape, recorded=recorded)
    budget = machine_budget(profile)

    ram_need = cost.ram_bytes
    if budget.unified:
        ram_need += cost.vram_bytes
    # Against what is free right now, swap included: that is the wall the run
    # hits, and it is lower than the installed one on a machine with a desktop
    # open on it.
    usable = budget.usable_bytes
    percent = 100.0 * ram_need / usable if usable else 0.0
    ceiling = _max_frames_for(cost, budget)
    ceiling_now = _max_frames_for(cost, budget, pool=usable)

    over_ram = ram_need > usable
    # The machine has the memory, something else is in it. A different problem
    # with a different answer: closing an application costs nothing, trimming a
    # transect costs data.
    busy = over_ram and ram_need <= budget.memory_bytes
    over_vram = (
        budget.vram_bytes is not None
        and not budget.unified
        and cost.vram_bytes > budget.vram_bytes
    )

    fixed_ram = max(stage.fixed_bytes for stage in cost.stages)
    if over_ram or over_vram:
        level = "block"
        # Which resource decided keeps its old precedence; what is new is
        # telling a fixed cost apart from a length. A run whose fixed term alone
        # is over budget does not fit at one frame, so advice about the frame
        # rate, the trim or the resolution is advice that cannot be taken.
        if over_ram:
            if busy:
                # Named for the cause, not the resource: the pass is the size it
                # always was, and what changed is what else is running.
                limit = "busy"
            else:
                limit = "ram_fixed" if fixed_ram > budget.memory_bytes else "ram"
        else:
            limit = (
                "vram_fixed"
                if cost.fixed_vram_bytes > (budget.vram_bytes or 0)
                else "vram"
            )
    elif percent >= 100.0 * (_SWAP_WARN_FRACTION if budget.swap_bytes else _WARN_FRACTION):
        level, limit = "warn", "ram"
    else:
        # Spilling into swap lands here: it is slower, not wrong, and the readout
        # says so in the same sentence that reports what the run needs.
        level, limit = "ok", ""

    headline, detail = _wording(level, limit, cost, budget, ram_need, shape, fixed_ram)
    return Verdict(
        level=level,
        cost=cost,
        budget=budget,
        limit=limit,
        max_frames=ceiling,
        max_frames_now=ceiling_now,
        memory_need_bytes=ram_need,
        percent=percent,
        headline=headline,
        detail=detail,
        shape=shape,
    )


@dataclass(frozen=True)
class Fit:
    """A verdict in the units the run is configured in: a length and a frame rate.

    The single entry point for the rest of the app. Cheap enough to call on any
    settings change; nothing here touches the disk or the GPU beyond one probe.
    """

    verdict: Verdict
    seconds: float
    fps: int
    max_seconds: float
    suggested_fps: int | None
    suggested_seconds: float | None
    # A mapping backend that was re-graded and came back fitting, for the case
    # where the run does not fit at any length. None when none of them do.
    suggested_backend: str | None = None
    suggested_backend_vram: int | None = None
    # The largest preprocessing batch that grades non-block, when the batch size
    # is what pushes the device over. Replaces a second, separately calibrated
    # warning that used to contradict this one.
    suggested_batch_size: int | None = None

    @property
    def level(self) -> str:
        return self.verdict.level

    @property
    def fits(self) -> bool:
        return self.verdict.level == "ok"

    @property
    def headline(self) -> str:
        return self.verdict.headline

    @property
    def detail(self) -> str:
        return self.verdict.detail

    @property
    def advice(self) -> str:
        """What to change, in the controls the user has in front of them."""
        if self.fits:
            return ""
        if self.verdict.limit_is_fixed:
            # The batch size leads whenever it is what overflowed: it is a
            # control on this form, and it fixes the run outright. Recommending
            # a different backend for a segmentation overflow would change the
            # method for a reason that had nothing to do with it.
            if self.suggested_batch_size is not None and (
                self.verdict.cost.vram_fixed_from == "segmentation"
            ):
                return (
                    f"Set the preprocessing batch size to {self.suggested_batch_size} "
                    "in advanced settings."
                )
            if self.suggested_backend is None:
                return "No mapping method is expected to fit this machine at these settings."
            size = (
                f" It needs about {format_bytes(self.suggested_backend_vram)} "
                "of graphics memory."
                if self.suggested_backend_vram is not None
                else ""
            )
            return f"Choose {self.suggested_backend} under Mapping.{size}"
        parts = []
        # First, because it is the only fix here that costs nothing: the machine
        # has the memory, and the pass is the length the dive was.
        if self.verdict.limit == "busy":
            parts.append("close other applications")
        if self.suggested_fps is not None:
            parts.append(f"set FPS to {self.suggested_fps}")
        if self.suggested_seconds is not None:
            parts.append(f"trim the pass to about {format_duration(self.suggested_seconds)}")
        if not parts:
            return "Lower the resolution, or split the pass into shorter sections."
        return f"{parts[0][0].upper()}{parts[0][1:]}" + (f", or {parts[1]}." if len(parts) > 1 else ".")


def format_duration(seconds: float) -> str:
    """A run length as `9 min` or `1 h 20 min`."""
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{max(1, minutes)} min"
    hours, rem = divmod(minutes, 60)
    return f"{hours} h {rem} min" if rem else f"{hours} h"


def fit_for_pass(
    profile: SystemProfile,
    *,
    seconds: float,
    fps: int,
    width: int,
    height: int,
    mapping_backend: str,
    seg_model: str,
    batch_size: int = 4,
    recorded: dict | None = None,
) -> Fit:
    """Grade one pass and say what would make it fit."""
    fps = max(1, fps)
    shape = RunShape(
        frames=int(seconds * fps),
        width=width,
        height=height,
        mapping_backend=mapping_backend,
        seg_model=seg_model,
        batch_size=batch_size,
    )
    verdict = grade(profile, shape, recorded=recorded)
    ceiling = verdict.max_frames
    max_seconds = ceiling / fps
    # A busy machine is asked what fits right now, because that is the verdict
    # being explained. Everything else is asked what the machine can do, so the
    # length offered does not shrink because something else opened.
    advice_ceiling = verdict.max_frames_now if verdict.limit == "busy" else ceiling

    suggested_fps: int | None = None
    suggested_seconds: float | None = None
    suggested_backend: str | None = None
    suggested_backend_vram: int | None = None
    suggested_batch_size: int | None = None
    if verdict.limit_is_fixed:
        # Offered only if it actually fits: re-graded rather than assumed, so the
        # app never sends somebody to a control that will not help either. The
        # recorded peak is dropped for the re-grade -- a peak measured under one
        # backend says nothing about another.
        for backend in _lighter_backends(mapping_backend):
            lighter = grade(profile, replace(shape, mapping_backend=backend))
            if lighter.level != "block":
                suggested_backend = backend
                suggested_backend_vram = lighter.cost.fixed_vram_bytes
                break
    elif verdict.level != "ok":
        # The largest frame rate that still fits the pass as trimmed.
        for rate in range(fps - 1, 0, -1):
            if seconds * rate <= advice_ceiling:
                suggested_fps = rate
                break
        # Only worth saying when it is meaningfully shorter than the pass
        # already is, and long enough to still be a usable transect.
        advice_seconds = advice_ceiling / fps
        if advice_seconds >= 60 and advice_seconds <= 0.85 * seconds:
            suggested_seconds = advice_seconds
    if verdict.limit.startswith("vram") and batch_size > 1:
        # The batch size only reaches the device through the segmentation term,
        # so this is worth saying only when shrinking it actually changes the
        # verdict. Same model, same units as everything else on this readout.
        for size in range(batch_size - 1, 0, -1):
            if grade(profile, replace(shape, batch_size=size), recorded=recorded).level != "block":
                suggested_batch_size = size
                break
    return Fit(
        verdict=verdict,
        seconds=seconds,
        fps=fps,
        max_seconds=max_seconds,
        suggested_fps=suggested_fps,
        suggested_seconds=suggested_seconds,
        suggested_backend=suggested_backend,
        suggested_backend_vram=suggested_backend_vram,
        suggested_batch_size=suggested_batch_size,
    )


def _lighter_backends(current: str) -> list[str]:
    """Modelled backends that hold less on the device than `current`, cheapest first."""
    from deepreefmap_gui.profiling.model_costs import modelled_mapping_backends

    costs = modelled_mapping_backends()
    here = costs.get(current)
    ceiling = here.vram_fixed_bytes if here is not None else None
    lighter = [
        (cost.vram_fixed_bytes, key)
        for key, cost in costs.items()
        if key != current and (ceiling is None or cost.vram_fixed_bytes < ceiling)
    ]
    return [key for _, key in sorted(lighter)]


def _pool_phrase(budget: Budget) -> str:
    """What one run can occupy, naming swap where the machine has it.

    Never folded into a single figure: a run told it has 58 GB, when 31 of them
    are a swapfile, has been told the wrong thing about how long it will take.
    """
    if not budget.swap_bytes:
        return format_bytes(budget.ram_bytes)
    return (
        f"{format_bytes(budget.ram_bytes)} of memory and "
        f"{format_bytes(budget.swap_bytes)} of swap"
    )


def _budget_label(budget: Budget) -> str:
    """What the memory pool is called, so swap is never quoted as plain memory."""
    return "memory and swap" if budget.swap_bytes else "memory"


def _fig(text: str) -> str:
    """A figure inside a resource message.

    `ResourceFit.message` is rich text: the numbers and the model names are what
    change when a setting moves, so they are the part of the sentence that reads
    at a glance rather than being hunted for in prose.
    """
    return f"<b>{text}</b>"


def _sized(value: int) -> str:
    return _fig(format_bytes(value))


def _rich_pool_phrase(budget: Budget) -> str:
    """`_pool_phrase` with its figures marked, for the rich-text messages."""
    if not budget.swap_bytes:
        return _sized(budget.ram_bytes)
    return f"{_sized(budget.ram_bytes)} of memory and {_sized(budget.swap_bytes)} of swap"


def _ram_resource(verdict: Verdict) -> ResourceFit:
    """The memory pool, graded on its own terms.

    On a unified machine the graphics demand is already inside the need, which
    is what makes it the only pool there is to report.
    """
    budget = verdict.budget
    cost = verdict.cost
    need = verdict.memory_need_bytes or cost.ram_bytes
    usable = budget.usable_bytes
    held = max(0, budget.memory_bytes - usable)
    spill = max(0, min(need, usable) - budget.ram_bytes)
    pool = _rich_pool_phrase(budget)
    label = "Memory and swap" if budget.swap_bytes else "Memory"
    if budget.unified:
        label += " (shared with the graphics processor)"
    warn_at = _SWAP_WARN_FRACTION if budget.swap_bytes else _WARN_FRACTION
    slower = (
        f" About {_sized(spill)} of that runs from swap, so it will be slower."
        if spill
        else ""
    )

    if need > usable:
        level = "block"
        if need <= budget.memory_bytes:
            # The machine has it, something else is in it: a different problem,
            # with a fix that costs nothing.
            message = (
                f"Needs about {_sized(need)}, and only {_sized(usable)} of the "
                f"{pool} this machine can give a run is free -- {_sized(held)} is "
                f"held by other applications."
            )
        else:
            fixed_ram = max(stage.fixed_bytes for stage in cost.stages)
            backend = verdict.shape.mapping_backend if verdict.shape else "the mapping model"
            if fixed_ram > budget.memory_bytes:
                message = (
                    f"Loading {_fig(backend)} alone takes about {_sized(fixed_ram)}, "
                    f"more than the {pool} this machine can give a run. Frame rate, "
                    f"length and resolution do not change that."
                )
            else:
                message = (
                    f"Needs about {_sized(need)}, more than the {pool} this "
                    f"machine can give a run."
                )
    elif usable and 100.0 * need / usable >= 100.0 * warn_at:
        level = "warn"
        message = (
            f"Close to the limit: needs about {_sized(need)} of the "
            f"{_sized(usable)} of {_budget_label(budget)} free for a run.{slower}"
        )
    else:
        level = "ok"
        message = (
            f"Fits: needs about {_sized(need)} of the {_sized(usable)} "
            f"of {_budget_label(budget)} free for a run.{slower}"
        )
    return ResourceFit(
        key="ram",
        label=label,
        level=level,
        message=message,
        need_bytes=need,
        budget_bytes=usable,
        held_bytes=held,
        swap_need_bytes=spill,
    )


def _vram_resource(verdict: Verdict) -> ResourceFit:
    """The graphics card's pool, graded on its own terms.

    Reported even where there is no separate pool to grade -- a machine with no
    card, or one that shares the system's memory -- because a row that vanishes
    reads as a row that passed.
    """
    budget = verdict.budget
    cost = verdict.cost
    shape = verdict.shape
    if budget.unified:
        return ResourceFit(
            key="vram",
            label="Graphics memory",
            level="none",
            message=(
                "This machine's graphics processor draws from system memory, so "
                "the whole run is graded against the pool above."
            ),
        )
    if budget.vram_bytes is None:
        return ResourceFit(
            key="vram",
            label="Graphics memory",
            level="none",
            message="No graphics card was detected, so the run uses memory only.",
        )
    need = cost.vram_bytes
    pool = budget.vram_bytes
    measured = (
        " Measured on this card on an earlier run."
        if cost.vram_source == "measured"
        else " This is an estimate until a run on this card records what it took."
    )
    if need > pool:
        level = "block"
        if cost.fixed_vram_bytes > pool:
            if cost.vram_fixed_from == "segmentation" and shape is not None:
                message = (
                    f"Reading {_fig(str(shape.batch_size))} frames at a time through "
                    f"{_fig(shape.seg_model)} takes about "
                    f"{_sized(cost.fixed_vram_bytes)} on the card, more than "
                    f"the {_sized(pool)} it can give a run. Frame rate, "
                    f"length and resolution do not change that -- the batch size "
                    f"does.{measured}"
                )
            else:
                backend = shape.mapping_backend if shape else "the mapping model"
                message = (
                    f"{_fig(backend)} holds about {_sized(cost.fixed_vram_bytes)} on "
                    f"the card before the first frame is read, more than the "
                    f"{_sized(pool)} it can give a run. Frame rate, length "
                    f"and resolution do not change that.{measured}"
                )
        else:
            message = (
                f"Needs about {_sized(need)} at {_fig(str(cost.frames))} frames, more "
                f"than the {_sized(pool)} of graphics memory this card can "
                f"give a run."
            )
    elif need >= _WARN_FRACTION * pool:
        level = "warn"
        message = (
            f"Close to this card's limit: needs about {_sized(need)} of the "
            f"{_sized(pool)} of graphics memory it can give a run."
        )
    else:
        level = "ok"
        message = (
            f"Fits: needs about {_sized(need)} of the {_sized(pool)} of "
            f"graphics memory this card can give a run."
        )
    return ResourceFit(
        key="vram",
        label="Graphics memory",
        level=level,
        message=message,
        need_bytes=need,
        budget_bytes=pool,
    )


def _wording(
    level: str,
    limit: str,
    cost: Cost,
    budget: Budget,
    ram_need: int,
    shape: RunShape,
    fixed_ram: int,
) -> tuple[str, str]:
    """Plain statements of what was measured against what."""
    spill = max(0, min(ram_need, budget.usable_bytes) - budget.ram_bytes)
    held = max(0, budget.memory_bytes - budget.usable_bytes)
    # Said once, wherever the verdict lands: the pass is slower for it, and that
    # is the whole of what swap changes about a run that fits.
    slower = (
        f" About {format_bytes(spill)} of that runs from swap, so it will be slower."
        if spill
        else ""
    )
    # The whole pool, every time. What the machine can give a run does not depend
    # on which run is being graded, and a figure that grew when the mapping method
    # changed read as though the computer had gained memory.
    pool = _pool_phrase(budget)
    # Said wherever the verdict lands, because it is the difference between the
    # figure on the box and the figure a run will get.
    in_use = (
        f" {format_bytes(held)} of it is in use by other applications right now."
        if held
        else ""
    )
    if level == "ok":
        return "", (
            f"Needs about {format_bytes(ram_need)} of the {pool} this machine "
            f"can give a run.{in_use}{slower}"
        )
    if limit == "busy":
        return "Other applications are using memory this pass may need", (
            f"Needs about {format_bytes(ram_need)}. This machine has "
            f"{pool} for a run, but {format_bytes(held)} of it is in use by "
            f"other applications, leaving {format_bytes(budget.usable_bytes)}. "
            f"Closing them costs nothing; the pass would have to be shortened."
        )
    # A fixed cost belongs to the model that was chosen, so the sentence names
    # it: the choice is the only thing that can be changed about it, and a
    # verdict that will not say what caused it cannot be acted on.
    if limit == "vram_fixed":
        # The fixed term decides this verdict on its own, before frame rate,
        # length or resolution enter into it, so where the figure came from is
        # part of the verdict rather than a footnote to it.
        measured = (
            " Measured on this card on an earlier run."
            if cost.vram_source == "measured"
            else " This is an estimate until a run on this card records what it took."
        )
        # Whichever term set the figure is the one named. Segmentation's is the
        # batch size times a per-frame cost, so it is answerable by a control the
        # user has; the mapping backend's is answerable only by changing backend.
        if cost.vram_fixed_from == "segmentation":
            return f"{shape.seg_model} may need more graphics memory than this card has", (
                f"Reading {shape.batch_size} frames at a time through "
                f"{shape.seg_model} takes about {format_bytes(cost.fixed_vram_bytes)} "
                f"on the graphics card; this card can give a run "
                f"{format_bytes(budget.vram_bytes or 0)}. Frame rate, length and "
                f"resolution do not change that -- the batch size does.{measured}"
            )
        return f"{shape.mapping_backend} may need more graphics memory than this card has", (
            f"{shape.mapping_backend} holds about {format_bytes(cost.fixed_vram_bytes)} "
            f"on the graphics card before the first frame is read; this card can "
            f"give a run {format_bytes(budget.vram_bytes or 0)}. Frame rate, "
            f"length and resolution do not change that.{measured}"
        )
    if limit == "ram_fixed":
        return f"{shape.mapping_backend} may need more memory than this computer has", (
            f"Loading {shape.mapping_backend} alone takes about "
            f"{format_bytes(fixed_ram)}; this computer can give a run "
            f"{_pool_phrase(budget)}. Frame rate, length and "
            f"resolution do not change that."
        )
    if limit == "vram":
        return "May be too long for the graphics card", (
            f"Needs about {format_bytes(cost.vram_bytes)} of graphics memory at "
            f"{cost.frames} frames; this card can give a run "
            f"{format_bytes(budget.vram_bytes or 0)}."
        )
    if level == "block":
        return "May be too long to process in one pass", (
            f"Needs about {format_bytes(ram_need)} of memory; this machine can "
            f"give a run {pool}.{in_use}"
        )
    return "Close to this machine's limit", (
        f"Needs about {format_bytes(ram_need)} of the {pool} this machine "
        f"can give a run.{in_use}{slower}"
    )
