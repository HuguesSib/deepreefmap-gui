"""The `deepreefmap reconstruct` command line equivalent to a GUI run.

The GUI never shells out: every launch path builds a kwargs dict and calls
``run_reconstruction`` in-process. That leaves no record of what was actually
run, which is exactly what auditing a result or reproducing it elsewhere needs.

This module translates that same kwargs dict into argv, so the command shown to
the user cannot drift from the run it describes. Every flag is emitted even when
it holds its default, so the command doubles as a full settings record, except
for the options in ``_OPTIONAL``, whose only "unset" spelling is omission.

``tests/gui/test_run_command.py`` checks the translation table against the
installed library's Typer signature, so a renamed or added CLI option fails the
suite rather than producing a command that no longer runs.
"""

from __future__ import annotations

import shlex
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

# Kwargs that exist only for the in-process caller: a viewer to draw into and the
# events the transport controls set. The CLI has no equivalent and needs none.
IGNORED_KWARGS = frozenset(
    {
        "viewer",
        "cancel_event",
        "pause_event",
        "scene_writer",
        "manifest_extra",
        # Already carried by --out, whose last segment is the run name.
        "run_name",
        # Flattened into the backend-specific flags by _mapping_option_args.
        "mapping_options",
    }
)

# kwarg -> flag, for the scalar options. Order here is the order in the command.
_SCALAR_FLAGS: tuple[tuple[str, str], ...] = (
    ("fps", "--fps"),
    ("segmentation_name", "--segmentation"),
    ("mapping_name", "--mapping"),
    ("camera_profile_name", "--camera-profile"),
    ("begin_s", "--begin"),
    ("end_s", "--end"),
    ("transect_length", "--transect-length"),
    ("transect_crop_width", "--transect-crop-width"),
    ("classes_path", "--classes"),
    ("processing_width", "--processing-width"),
    ("processing_height", "--processing-height"),
    ("preprocess_batch_size", "--preprocess-batch-size"),
    ("grid_bins", "--grid-bins"),
    ("replacement_radius_factor", "--replacement-radius-factor"),
    ("replacement_radius_estimation_frames", "--replacement-radius-estimation-frames"),
    ("replacement_radius_override", "--replacement-radius-override"),
)

# kwarg -> flag, for the booleans Typer gives a --x/--no-x pair.
_PAIRED_BOOL_FLAGS: tuple[tuple[str, str], ...] = (
    ("enable_tsdf", "--tsdf"),
    ("refine_intrinsics_from_mapper", "--refine-intrinsics-from-mapper"),
    ("require_gravity_telemetry", "--require-gravity-telemetry"),
)

# Options the CLI can only be told to leave alone by not passing them: their
# default is None, and there is no literal that means "None" on a command line.
_OPTIONAL = frozenset(
    {
        "begin_s",
        "end_s",
        "transect_length",
        "transect_crop_width",
        "classes_path",
        "processing_width",
        "processing_height",
        "replacement_radius_factor",
        "replacement_radius_override",
    }
)

# Options taking a filesystem path, which is resolved absolute so the command
# runs from any directory.
_PATH_KWARGS = frozenset({"classes_path"})

# What each CLI option defaults to, for rebuilding a command from a manifest
# written before this module existed. Mirrors cli/main.py::reconstruct.
CLI_DEFAULTS: dict[str, Any] = {
    "fps": 10,
    "segmentation_name": "coralscapes-vit-b-dpt",
    "mapping_name": "scsfmlearner",
    "preprocess_batch_size": 4,
    "grid_bins": 2000,
    "replacement_radius_estimation_frames": 30,
    "enable_tsdf": False,
    "refine_intrinsics_from_mapper": False,
    "require_gravity_telemetry": False,
    "skip_segmentation": False,
}

_LOGER_BACKENDS = ("loger", "loger_star")

# mapping_options key -> flag, per backend. Only the block for the selected
# backend is emitted: the CLI builds mapping_options from whichever branch
# matches --mapping and silently drops the other backend's flags.
_LOGER_OPTION_FLAGS: tuple[tuple[str, str], ...] = (
    ("window_size", "--loger-window-size"),
    ("overlap_size", "--loger-overlap-size"),
    ("model_path", "--loger-model-path"),
)
_SCS_OPTION_FLAGS: tuple[tuple[str, str], ...] = (
    ("target_width", "--scsfmlearner-width"),
    ("target_height", "--scsfmlearner-height"),
    ("checkpoint_path", "--scsfmlearner-checkpoint-path"),
)

_LOGER_OPTION_DEFAULTS = {"window_size": 32, "overlap_size": 3}
_SCS_OPTION_DEFAULTS = {"target_width": 512, "target_height": 256}

# Shown in place of a path a set of run kwargs does not carry, so the command
# still reads as one rather than breaking off mid-flag.
PLACEHOLDER_VIDEO = "<no video selected>"

VIDEO_COMMA_WARNING = (
    "# WARNING: a video path contains a comma. --videos is comma-separated, so "
    "this command cannot express it; rename or move the file before running."
)


def _absolute(value: Any) -> str:
    """A path spelled absolutely, so the command does not depend on the CWD."""
    return str(Path(str(value)).expanduser().resolve())


def _scalar(value: Any) -> str:
    """A CLI token for a scalar. Floats keep their repr; bools never land here."""
    return str(value)


def _mapping_option_args(kwargs: Mapping[str, Any]) -> list[str]:
    """The backend-specific flags for whichever mapper is selected."""
    backend = str(kwargs.get("mapping_name") or "")
    options = kwargs.get("mapping_options") or {}
    if not isinstance(options, Mapping):
        return []
    if backend in _LOGER_BACKENDS:
        table, defaults = _LOGER_OPTION_FLAGS, _LOGER_OPTION_DEFAULTS
    elif backend == "scsfmlearner":
        table, defaults = _SCS_OPTION_FLAGS, _SCS_OPTION_DEFAULTS
    else:
        # Any other backend takes an empty mapping_options in the CLI, so it has
        # no flags of its own to emit.
        return []
    args: list[str] = []
    for key, flag in table:
        value = options.get(key, defaults.get(key))
        # model_path and checkpoint_path both mean "use the default checkpoint"
        # when unset, which the CLI spells by omitting the flag.
        if value is None or value == "":
            continue
        args += [flag, _absolute(value) if "path" in key else _scalar(value)]
    return args


def build_reconstruct_argv(kwargs: Mapping[str, Any]) -> list[str]:
    """Argv (after the `reconstruct` subcommand) for a run launched with `kwargs`.

    Takes the dict the GUI hands ``instrumented_reconstruction``, so anything the
    run honours is represented and anything it ignores is absent.
    """
    videos = kwargs.get("video_paths") or []
    joined = ",".join(_absolute(v) for v in videos) if videos else PLACEHOLDER_VIDEO
    args: list[str] = ["--videos", joined]

    output_dir = kwargs.get("output_dir")
    if output_dir is not None:
        args += ["--out", _absolute(output_dir)]

    for key, flag in _SCALAR_FLAGS:
        value = kwargs.get(key, CLI_DEFAULTS.get(key))
        if value is None and key in _OPTIONAL:
            continue
        if value is None:
            continue
        args += [flag, _absolute(value) if key in _PATH_KWARGS else _scalar(value)]

    for key, flag in _PAIRED_BOOL_FLAGS:
        on = bool(kwargs.get(key, CLI_DEFAULTS.get(key, False)))
        args.append(flag if on else f"--no-{flag[2:]}")

    # Declared explicitly as "--skip-segmentation" in the library CLI, so it has
    # no --no- form; the only way to say False is to leave it out.
    if kwargs.get("skip_segmentation"):
        args.append("--skip-segmentation")

    # No viser flags at all. The GUI draws into its own Qt viewer and never
    # starts viser, and --viser already defaults to off, so omitting the group
    # is behaviourally identical to spelling it out. --viser-port and
    # --keep-viser-open are dead on top of that: the orchestrator reaches either
    # one only through `owned_viser`, which stays None whenever viser is off, so
    # printing them would read as settings that applied when they did not.
    args += _mapping_option_args(kwargs)
    return args


def project_directory() -> Path | None:
    """The uv project the CLI lives in, or None when this is an installed binary.

    `uv run` locates its environment by walking up from the working directory, so
    invoking it from anywhere else fails outright. Passing this as --project is
    what lets the command run from wherever the user happens to paste it.
    """
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "pyproject.toml").is_file():
            return parent
    return None


def command_prefix() -> list[str]:
    """How to invoke the CLI: through uv in a checkout, bare when installed.

    --project rather than a `cd … &&` prefix: it is one command instead of a
    shell chain and it does not move the pasting shell. The classes YAML resolves
    from package resources wherever it is run. The camera profile does not: the
    library resolves a name against a CWD-relative `./camera_profiles` and then
    its own package resources, and only `gopro_hero_10` is packaged, so the
    script writes the run's own copy of the profile beside itself first.
    """
    project = project_directory()
    if project is None:
        return ["deepreefmap", "reconstruct"]
    return ["uv", "run", "--project", str(project), "deepreefmap", "reconstruct"]


def format_command(args: Sequence[str], *, multiline: bool = True) -> str:
    """A pasteable command line for the given `reconstruct` arguments.

    Multi-line puts one flag per line so the settings can be read down the page;
    it still pastes and runs as a single command.
    """
    prefix = command_prefix()
    if not multiline:
        return " ".join(shlex.quote(t) for t in [*prefix, *args])

    lines: list[str] = [" ".join(shlex.quote(t) for t in prefix) + " \\"]
    # Flags and their values pair onto one line; bare switches stand alone.
    idx = 0
    pieces: list[str] = []
    while idx < len(args):
        token = args[idx]
        if token.startswith("--") and idx + 1 < len(args) and not args[idx + 1].startswith("--"):
            pieces.append(f"{token} {shlex.quote(args[idx + 1])}")
            idx += 2
        else:
            pieces.append(shlex.quote(token))
            idx += 1
    for i, piece in enumerate(pieces):
        suffix = " \\" if i < len(pieces) - 1 else ""
        lines.append(f"  {piece}{suffix}")
    return "\n".join(lines)


def command_for_kwargs(kwargs: Mapping[str, Any], *, multiline: bool = True) -> str:
    """The full command for a run about to be launched with `kwargs`."""
    args = build_reconstruct_argv(kwargs)
    command = format_command(args, multiline=multiline)
    videos = kwargs.get("video_paths") or []
    if any("," in str(v) for v in videos):
        command = f"{VIDEO_COMMA_WARNING}\n{command}"
    return command


def kwargs_from_manifest(manifest: Mapping[str, Any], run_dir: Path) -> dict[str, Any]:
    """Rebuild the launch kwargs from a run manifest.

    The fallback for runs written before ``cli_command`` was recorded. The
    manifest carries no ``require_gravity_telemetry`` and, on a skip-segmentation
    run, no segmentation model name. Those come back as the CLI defaults, which
    is the closest honest answer available.
    """
    transect = manifest.get("transect")
    transect = transect if isinstance(transect, dict) else {}
    segmentation = manifest.get("segmentation_model")
    skipped = segmentation == "__skip__" or manifest.get("mode") == "geometry_only"
    kwargs: dict[str, Any] = {
        "video_paths": list(manifest.get("input_videos") or []),
        "output_dir": run_dir,
        "fps": manifest.get("fps", CLI_DEFAULTS["fps"]),
        "segmentation_name": (
            CLI_DEFAULTS["segmentation_name"] if skipped else segmentation
        ),
        "mapping_name": manifest.get("mapping_backend", CLI_DEFAULTS["mapping_name"]),
        "camera_profile_name": manifest.get("camera_profile"),
        "begin_s": manifest.get("begin_s"),
        "end_s": manifest.get("end_s"),
        "transect_length": transect.get("length"),
        "transect_crop_width": transect.get("crop_width"),
        "classes_path": manifest.get("classes"),
        "processing_width": manifest.get("processing_width"),
        "processing_height": manifest.get("processing_height"),
        # Stamped by profiling/instrumentation.py, not by the pipeline, so it is
        # absent from a manifest the GUI never folded into. Substituting the CLI
        # default there changes the VRAM the rebuilt command asks for.
        "preprocess_batch_size": manifest.get(
            "preprocess_batch_size", CLI_DEFAULTS["preprocess_batch_size"]
        ),
        "grid_bins": manifest.get("grid_bins", CLI_DEFAULTS["grid_bins"]),
        "replacement_radius_factor": manifest.get("replacement_radius_factor"),
        "replacement_radius_estimation_frames": manifest.get(
            "replacement_radius_estimation_frames",
            CLI_DEFAULTS["replacement_radius_estimation_frames"],
        ),
        "replacement_radius_override": manifest.get("replacement_radius_override"),
        "enable_tsdf": manifest.get("enable_tsdf", CLI_DEFAULTS["enable_tsdf"]),
        "refine_intrinsics_from_mapper": manifest.get(
            "refine_intrinsics_from_mapper", CLI_DEFAULTS["refine_intrinsics_from_mapper"]
        ),
        "skip_segmentation": skipped,
        "mapping_options": manifest.get("mapping_options") or {},
    }
    return kwargs


def command_from_manifest(
    manifest: Mapping[str, Any], run_dir: Path, *, multiline: bool = True
) -> str:
    """The command that reproduces a finished run.

    Prefers what the run recorded for itself; falls back to reading the manifest
    for runs made before that was written.
    """
    recorded = manifest.get("cli_argv")
    if isinstance(recorded, list) and recorded:
        return format_command([str(a) for a in recorded], multiline=multiline)
    recorded_text = manifest.get("cli_command")
    if isinstance(recorded_text, str) and recorded_text.strip():
        return recorded_text
    return command_for_kwargs(kwargs_from_manifest(manifest, run_dir), multiline=multiline)


def write_run_command_script(
    output_dir: Path, args: Sequence[str], *, camera_profile_name: str | None = None
) -> Path:
    """Drop a runnable `run_command.sh` beside the run's outputs.

    Written before the pipeline starts, so a run that crashes or is cancelled,
    the one most worth auditing, still says what it was asked to do.

    A field-calibrated profile exists on the laptop that made it and nowhere
    else, so the script stages the run's own copy into the `./camera_profiles`
    the library looks in. Without that preamble the CLI refuses the name and
    exits before the pipeline starts, which made every such script unrunnable.
    """
    path = output_dir / "run_command.sh"
    body = format_command(args, multiline=True)
    preamble = _profile_preamble(camera_profile_name)
    path.write_text(
        f"#!/usr/bin/env bash\nset -euo pipefail\n\n{preamble}{body}\n", encoding="utf-8"
    )
    path.chmod(0o755)
    return path


def _profile_preamble(camera_profile_name: str | None) -> str:
    """Stage the run's copy of the camera profile where the library resolves names.

    Emitted for a bundled profile too: the copy is the calibration this run
    actually used, and a library installed elsewhere may package a different one
    under the same name. `cp -n` so a second run of the script leaves alone
    whatever the first put there. Empty when the run recorded no copy.
    """
    from deepreefmap_gui.camera.profiles import RUN_PROFILE_NAME

    if not camera_profile_name:
        return ""
    return (
        f'cd "$(dirname "$0")"\n'
        f"mkdir -p camera_profiles\n"
        f'cp -n "{RUN_PROFILE_NAME}" "camera_profiles/{camera_profile_name}.json"\n\n'
    )
