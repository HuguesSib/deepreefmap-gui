"""What each model costs in memory, per backend and per segmenter.

Two kinds of cost, and the distinction is the whole point of the table:

- Fixed: weights and the working set of one inference step. Constant in the
  number of frames. Segmentation is released before mapping loads
  (orchestrator.py:305), so the peak is the larger stage, never the sum.
- Per frame: memory that accumulates across the sequence. LoGeR detaches every
  window's point maps to CPU and holds them all while torch.cat builds the
  merged copy, so its mapping cost is linear in the whole clip with no cap.

Figures are traced from the pinned pipeline commit and, for segmentation
activations, measured by running each forward pass under a tensor-storage
tracker. Both are approximations; a recorded peak from a real run supersedes
them.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

MB = 1024**2
GB = 1024**3

# Python, torch and the Qt application, present before any model loads.
INTERPRETER_BASELINE_BYTES = int(1.5 * GB)

# Prepared frames stay in RAM for the whole run at the processing resolution:
# rgb uint8 (3) + labels uint8 (1) + keep_mask uint8 (1). The PNG cache written
# for resume is in addition to this, not instead of it.
FRAME_BYTES_PER_PIXEL = 5

# The replacement-radius voxel stage holds the unreduced cloud plus its int64
# key and sort transients.
CLOUD_BYTES_PER_POINT = 103

# Share of mapping pixels surviving the depth window, keep mask and confidence
# cut. Data-dependent and not derivable from source; taken from a recorded
# 2359-frame run that kept 31.7k points per frame of 141120. It decides whether
# the cloud stage or the mapping merge is the run's peak.
NOMINAL_KEEP_FRACTION = 0.22


@dataclass(frozen=True)
class MappingCost:
    """One mapping backend's memory profile.

    Byte-per-pixel figures are per mapping pixel, which is the backend's own
    inference grid and unrelated to the processing resolution.

    Loading and mapping are separate peaks, not one sum: the checkpoint copy is
    released before the merge runs, so a run's high-water mark is whichever
    stage is worst, not the total of all of them.
    """

    key: str
    mapping_pixels: int
    merge_bytes_per_pixel: int  # peak while the sequence is assembled
    resident_bytes_per_pixel: int  # what survives into the cloud stage
    load_ram_bytes: int  # host RAM peak while weights load, freed after
    weights_ram_bytes: int  # what stays resident once loaded
    vram_fixed_bytes: int  # weights plus one window's activations
    vram_bytes_per_frame: int  # sequence tensors resident on the device

    def merge_bytes_per_frame(self) -> int:
        return self.merge_bytes_per_pixel * self.mapping_pixels

    def cloud_bytes_per_frame(self) -> int:
        """The cloud stage, with the mapping result still resident beneath it."""
        per_pixel = self.resident_bytes_per_pixel + CLOUD_BYTES_PER_POINT * NOMINAL_KEEP_FRACTION
        return int(per_pixel * self.mapping_pixels)


# LoGeR maps at a fixed 504x280 that no setting reaches. Its merge holds the
# per-window parts (points + local_points + conf = 28 B/px) against the merged
# copy, inflated by the 32/29 window overlap. loger_star ships se3: true, which
# routes through a sim3 merge that builds a second set of aligned point maps.
#
# The static read of that code gives 62 and 75 B/px. Against a recorded
# 2359-frame run those over-predict by roughly a third, so the merged copy does
# not fully coexist with the parts in practice; the values below are pulled back
# to the observed slope and remain the least certain numbers in this file.
_LOGER_PIXELS = 504 * 280

# Pi3 materialises fp32 params, then torch.load adds a second full state dict
# with no release between them. Measured in isolation at 13.6 GB peak for
# backend construction alone, settling to 8.7 GB resident.
_LOGER_LOAD_RAM = 12 * GB
_LOGER_WEIGHTS_RAM = 5 * GB

# Device-side: the weights plus the working set of the sliding window. Measured
# on an RX 7900 XT (ROCm 6.4, fp16 autocast, loger_star with se3) at 504x280:
# 4.67 GB of weights resident after construction, a 9.5 GB peak through the
# first window of 32 frames, 11.5 GB from the second window on, and ~1.7 MB per
# frame after that (the per-frame figure below). Compared against the budget at
# zero frames, so on its own it decides whether an 8 GB card is refused. A VRAM
# peak recorded on the card in front of the user supersedes it in either
# direction (memory_estimate.py). Apple unified memory is unmeasured.
_LOGER_VRAM_FIXED = int(11.5 * GB)

_MAPPING_COSTS: tuple[MappingCost, ...] = (
    MappingCost(
        key="loger",
        mapping_pixels=_LOGER_PIXELS,
        merge_bytes_per_pixel=40,
        resident_bytes_per_pixel=20,
        load_ram_bytes=_LOGER_LOAD_RAM,
        weights_ram_bytes=_LOGER_WEIGHTS_RAM,
        vram_fixed_bytes=_LOGER_VRAM_FIXED,
        # The whole clip is uploaded as one fp32 tensor before inference.
        vram_bytes_per_frame=12 * _LOGER_PIXELS,
    ),
    MappingCost(
        key="loger_star",
        mapping_pixels=_LOGER_PIXELS,
        merge_bytes_per_pixel=48,
        resident_bytes_per_pixel=20,
        load_ram_bytes=_LOGER_LOAD_RAM,
        weights_ram_bytes=_LOGER_WEIGHTS_RAM,
        vram_fixed_bytes=_LOGER_VRAM_FIXED,
        vram_bytes_per_frame=12 * _LOGER_PIXELS,
    ),
    # Frame-by-frame, and returns depth only: no world points, no confidence.
    MappingCost(
        key="scsfmlearner",
        mapping_pixels=512 * 256,
        merge_bytes_per_pixel=8,
        resident_bytes_per_pixel=4,
        load_ram_bytes=768 * MB,
        weights_ram_bytes=256 * MB,
        vram_fixed_bytes=512 * MB,
        vram_bytes_per_frame=0,
    ),
)

_MAPPING_BY_KEY = {cost.key: cost for cost in _MAPPING_COSTS}

# The most expensive backend, for an unknown name: a warning that overstates is
# recoverable, one that understates is an OOM kill.
_MAPPING_FALLBACK = _MAPPING_BY_KEY["loger_star"]


@dataclass(frozen=True)
class SegmentationCost:
    """One segmenter's weights and its activation cost for a single frame.

    Activation is linear in the preprocessing batch size for every segmenter
    measured, with no fixed component.
    """

    key: str
    weights_vram_bytes: int
    activation_bytes_per_frame: int
    load_ram_bytes: int

    def vram_bytes(self, batch_size: int) -> int:
        return self.weights_vram_bytes + self.activation_bytes_per_frame * max(1, batch_size)


# SegFormer B2 and B5 share hidden sizes and decoder width, so they cost the
# same to run; only their depth differs, which is parameters rather than peak.
_SEGMENTATION_COSTS: tuple[SegmentationCost, ...] = (
    SegmentationCost("coralscapes-vit-s-dpt", 170 * MB, 247 * MB, 340 * MB),
    SegmentationCost("coralscapes-vit-b-dpt", 443 * MB, 1077 * MB, 886 * MB),
    SegmentationCost("coralscapes-vit-l-dpt", 1329 * MB, 1343 * MB, 2660 * MB),
    SegmentationCost("segformer-b2", 110 * MB, 1759 * MB, 220 * MB),
    SegmentationCost("segformer-b5", 339 * MB, 1759 * MB, 678 * MB),
)

_SEGMENTATION_BY_KEY = {cost.key: cost for cost in _SEGMENTATION_COSTS}

_SEGMENTATION_FALLBACK = _SEGMENTATION_BY_KEY["coralscapes-vit-l-dpt"]


def _rescaled(tabled_base: int, tabled: int, measured: int) -> int:
    """Hold a tabled figure's ratio to the weights, on the measured weights.

    The ratios are measured (a torch checkpoint materialises roughly two and a
    half copies of itself while loading, a safetensors file about two) and hold
    whatever the parameters weigh, so only the base is replaced.
    """
    if tabled_base <= 0:
        return tabled
    # Integer arithmetic: a float ratio leaves the result a byte off a round
    # multiple, which is invisible in a gigabyte and awkward to assert on.
    return measured * tabled // tabled_base


def mapping_cost(backend: str) -> MappingCost:
    """The backend's profile, or the most expensive one if the name is unknown.

    Weights are read off the checkpoint when it is installed; everything derived
    from them is rescaled to match, in host RAM as much as on the graphics card.
    A checkpoint is materialised in RAM before any of it reaches the device, so
    an understated weights figure understates both budgets.
    """
    from deepreefmap_gui.profiling.model_weights import weights_bytes

    base = _MAPPING_BY_KEY.get(backend, _MAPPING_FALLBACK)
    # Weights are looked up against the profile actually being used, so an
    # unknown name is costed at the *measured* most-expensive backend rather
    # than at its tabled figure.
    measured = weights_bytes(base.key)
    if measured is None:
        return base
    # Activations are the part of the device figure that is not the weights, and
    # the part no file can state. Held as tabled, added to the real weights.
    activations = max(0, base.vram_fixed_bytes - base.weights_ram_bytes)
    return replace(
        base,
        weights_ram_bytes=measured,
        load_ram_bytes=_rescaled(base.weights_ram_bytes, base.load_ram_bytes, measured),
        vram_fixed_bytes=measured + activations,
    )


def modelled_mapping_backends() -> dict[str, MappingCost]:
    """Every backend with a profile here, so a caller can compare them."""
    return dict(_MAPPING_BY_KEY)


def segmentation_cost(model: str) -> SegmentationCost:
    """The segmenter's profile, with its weights read off disk when installed.

    A DPT head ships without its backbone, which `AutoModel.from_pretrained`
    fetches at first use, so both are counted.
    """
    from deepreefmap_gui.profiling.model_weights import weights_bytes

    base = _SEGMENTATION_BY_KEY.get(model, _SEGMENTATION_FALLBACK)
    measured = weights_bytes(base.key)
    if measured is None:
        return base
    return replace(
        base,
        weights_vram_bytes=measured,
        load_ram_bytes=_rescaled(base.weights_vram_bytes, base.load_ram_bytes, measured),
    )
