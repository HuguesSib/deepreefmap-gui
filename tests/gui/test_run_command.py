"""The CLI command the GUI records for a run and hands back once it has finished.

Scenario: the GUI never shells out, so the only guarantee that the command it
offers would actually reproduce the run is that both are built from the same
kwargs dict and that the flag names still match the installed library's CLI.
Expected behaviour: the translation is faithful, and a library whose CLI has
moved on fails `test_every_emitted_flag_exists_in_the_cli` rather than silently
producing a command that no longer runs.
"""

from __future__ import annotations

import inspect
import json
import shlex
from pathlib import Path

import pytest

from deepreefmap_gui.runs.run_command import (
    IGNORED_KWARGS,
    PLACEHOLDER_VIDEO,
    VIDEO_COMMA_WARNING,
    build_reconstruct_argv,
    command_for_kwargs,
    command_from_manifest,
    format_command,
    kwargs_from_manifest,
    project_directory,
    write_run_command_script,
)


def scs_kwargs(tmp_path: Path) -> dict:
    return {
        "video_paths": [str(tmp_path / "dive.mp4")],
        "output_dir": tmp_path / "out" / "run-1",
        "run_name": "run-1",
        "fps": 10,
        "segmentation_name": "coralscapes-vit-b-dpt",
        "mapping_name": "scsfmlearner",
        "camera_profile_name": "gopro_hero_10",
        "begin_s": None,
        "end_s": None,
        "transect_length": None,
        "transect_crop_width": None,
        "classes_path": None,
        "processing_width": 1376,
        "processing_height": 768,
        "preprocess_batch_size": 4,
        "grid_bins": 2000,
        "require_gravity_telemetry": False,
        "replacement_radius_factor": None,
        "replacement_radius_estimation_frames": 30,
        "replacement_radius_override": None,
        "enable_tsdf": False,
        "skip_segmentation": False,
        "mapping_options": {"target_width": 512, "target_height": 256},
    }


def flag_value(argv: list[str], flag: str) -> str:
    return argv[argv.index(flag) + 1]


def test_scsfmlearner_run_emits_its_own_backend_block(tmp_path):
    argv = build_reconstruct_argv(scs_kwargs(tmp_path))

    assert flag_value(argv, "--videos") == str(tmp_path / "dive.mp4")
    assert flag_value(argv, "--out") == str(tmp_path / "out" / "run-1")
    assert flag_value(argv, "--mapping") == "scsfmlearner"
    assert flag_value(argv, "--scsfmlearner-width") == "512"
    assert flag_value(argv, "--scsfmlearner-height") == "256"
    assert not [a for a in argv if a.startswith("--loger-")]


def test_loger_run_emits_its_own_backend_block(tmp_path):
    kwargs = scs_kwargs(tmp_path)
    kwargs["mapping_name"] = "loger_star"
    kwargs["mapping_options"] = {"window_size": 16, "overlap_size": 4, "model_path": None}
    kwargs["refine_intrinsics_from_mapper"] = True

    argv = build_reconstruct_argv(kwargs)

    assert flag_value(argv, "--loger-window-size") == "16"
    assert flag_value(argv, "--loger-overlap-size") == "4"
    # An unset checkpoint means "use the vendored one", which the CLI spells by
    # leaving the flag off.
    assert "--loger-model-path" not in argv
    assert "--refine-intrinsics-from-mapper" in argv
    assert not [a for a in argv if a.startswith("--scsfmlearner-")]


def test_defaults_are_spelled_out_rather_than_left_implicit(tmp_path):
    argv = build_reconstruct_argv(scs_kwargs(tmp_path))

    assert flag_value(argv, "--fps") == "10"
    assert flag_value(argv, "--grid-bins") == "2000"
    assert flag_value(argv, "--preprocess-batch-size") == "4"
    assert "--no-tsdf" in argv
    assert "--no-require-gravity-telemetry" in argv
    assert "--no-refine-intrinsics-from-mapper" in argv


def test_no_viser_flags_are_emitted(tmp_path):
    """The GUI never starts viser and --viser is off by default, so the whole
    group is omitted; --viser-port and --keep-viser-open would be dead config
    that reads as settings which applied."""
    argv = build_reconstruct_argv(scs_kwargs(tmp_path))

    assert not [a for a in argv if a.startswith("--") and "viser" in a]


def test_the_command_runs_from_anywhere_without_a_cd(tmp_path):
    """`uv run` finds its environment by walking up from the working directory,
    so the project is named explicitly rather than chained behind a `cd`."""
    text = command_for_kwargs(scs_kwargs(tmp_path))

    assert "cd " not in text
    assert "&&" not in text
    tokens = shlex.split(text.replace("\\\n", " "))
    assert tokens[:4] == ["uv", "run", "--project", str(project_directory())]


def test_unset_optional_values_omit_their_flag(tmp_path):
    argv = build_reconstruct_argv(scs_kwargs(tmp_path))

    for flag in (
        "--begin",
        "--end",
        "--transect-length",
        "--transect-crop-width",
        "--classes",
        "--replacement-radius-factor",
        "--replacement-radius-override",
    ):
        assert flag not in argv


def test_set_optional_values_appear(tmp_path):
    kwargs = scs_kwargs(tmp_path)
    kwargs.update(
        begin_s=12.5, end_s=90.0, transect_length=10.0, transect_crop_width=1.5,
        replacement_radius_override=0.004,
    )

    argv = build_reconstruct_argv(kwargs)

    assert flag_value(argv, "--begin") == "12.5"
    assert flag_value(argv, "--end") == "90.0"
    assert flag_value(argv, "--transect-length") == "10.0"
    assert flag_value(argv, "--transect-crop-width") == "1.5"
    assert flag_value(argv, "--replacement-radius-override") == "0.004"


def test_skip_segmentation_has_no_negative_form(tmp_path):
    off = build_reconstruct_argv(scs_kwargs(tmp_path))
    assert "--skip-segmentation" not in off
    assert "--no-skip-segmentation" not in off

    kwargs = scs_kwargs(tmp_path)
    kwargs["skip_segmentation"] = True
    on = build_reconstruct_argv(kwargs)
    assert "--skip-segmentation" in on
    assert "--no-skip-segmentation" not in on


def test_paths_with_spaces_survive_a_round_trip(tmp_path):
    kwargs = scs_kwargs(tmp_path)
    video = tmp_path / "reef dive 1.mp4"
    kwargs["video_paths"] = [str(video)]

    tokens = shlex.split(format_command(build_reconstruct_argv(kwargs), multiline=False))

    assert str(video) in tokens


def test_a_comma_in_a_video_path_is_called_out(tmp_path):
    kwargs = scs_kwargs(tmp_path)
    kwargs["video_paths"] = [str(tmp_path / "dive,2.mp4")]

    assert command_for_kwargs(kwargs).startswith(VIDEO_COMMA_WARNING)


def test_several_videos_join_with_commas(tmp_path):
    kwargs = scs_kwargs(tmp_path)
    kwargs["video_paths"] = [str(tmp_path / "a.mp4"), str(tmp_path / "b.mp4")]

    argv = build_reconstruct_argv(kwargs)

    assert flag_value(argv, "--videos") == f"{tmp_path / 'a.mp4'},{tmp_path / 'b.mp4'}"


def test_an_unfilled_form_still_produces_a_readable_command(tmp_path):
    kwargs = scs_kwargs(tmp_path)
    kwargs["video_paths"] = []

    argv = build_reconstruct_argv(kwargs)

    assert flag_value(argv, "--videos") == PLACEHOLDER_VIDEO


def test_multiline_command_pastes_as_one_command(tmp_path):
    text = command_for_kwargs(scs_kwargs(tmp_path))

    assert "\\\n" in text
    # Line continuations are what make the multi-line form a single command; the
    # shell lexer is the authority on whether they did their job.
    tokens = shlex.split(text.replace("\\\n", " "))
    assert "reconstruct" in tokens
    assert str(tmp_path / "dive.mp4") in tokens


def test_a_recorded_command_is_preferred_over_rebuilding_it(tmp_path):
    argv = build_reconstruct_argv(scs_kwargs(tmp_path))
    manifest = {"cli_argv": argv, "fps": 999}

    assert command_from_manifest(manifest, tmp_path) == format_command(argv)


def test_a_manifest_without_a_recorded_command_is_rebuilt_from_its_fields(tmp_path):
    manifest = {
        "input_videos": [str(tmp_path / "dive.mp4")],
        "fps": 8,
        "segmentation_model": "segformer-b2",
        "mapping_backend": "loger",
        "camera_profile": "gopro_hero_10",
        "begin_s": 5.0,
        "end_s": None,
        "processing_width": 1024,
        "processing_height": 576,
        "preprocess_batch_size": 8,
        "grid_bins": 1500,
        "enable_tsdf": True,
        "mapping_options": {"window_size": 32, "overlap_size": 3},
        "transect": {"length": 10.0, "crop_width": 1.0, "applied": True},
        "mode": "semantic",
    }

    argv = build_reconstruct_argv(kwargs_from_manifest(manifest, tmp_path))

    assert flag_value(argv, "--fps") == "8"
    assert flag_value(argv, "--segmentation") == "segformer-b2"
    assert flag_value(argv, "--transect-length") == "10.0"
    assert flag_value(argv, "--begin") == "5.0"
    # The GUI stamps this one, not the pipeline, so it was being missed and the
    # rebuilt command asked for a different amount of graphics memory than the
    # run it claims to reproduce.
    assert flag_value(argv, "--preprocess-batch-size") == "8"
    assert "--tsdf" in argv
    assert "--no-tsdf" not in argv


def test_a_manifest_with_no_batch_size_falls_back_to_the_cli_default(tmp_path):
    """Manifests written before the GUI folded it in carry no batch size at all."""
    manifest = {"input_videos": [str(tmp_path / "dive.mp4")], "camera_profile": "gopro_hero_10"}

    argv = build_reconstruct_argv(kwargs_from_manifest(manifest, tmp_path))

    assert flag_value(argv, "--preprocess-batch-size") == "4"


def test_a_skip_segmentation_run_is_recognised_from_its_mode(tmp_path):
    manifest = {
        "input_videos": [str(tmp_path / "dive.mp4")],
        "segmentation_model": "__skip__",
        "mode": "geometry_only",
        "camera_profile": "gopro_hero_10",
    }

    argv = build_reconstruct_argv(kwargs_from_manifest(manifest, tmp_path))

    assert "--skip-segmentation" in argv
    # __skip__ is not a model the CLI would accept, so the recovered command
    # falls back to the CLI's own default for a flag that will be ignored anyway.
    assert flag_value(argv, "--segmentation") == "coralscapes-vit-b-dpt"


def test_the_script_written_beside_a_run_is_executable(tmp_path):
    argv = build_reconstruct_argv(scs_kwargs(tmp_path))

    path = write_run_command_script(tmp_path, argv)

    assert path.name == "run_command.sh"
    assert path.stat().st_mode & 0o111
    body = path.read_text()
    assert body.startswith("#!/usr/bin/env bash")
    assert "reconstruct" in body


# --- The contract with the installed library ----------------------------------


def cli_parameters() -> dict:
    from deepreefmap.cli.main import reconstruct

    return dict(inspect.signature(reconstruct).parameters)


def emitted_flags(tmp_path: Path) -> set[str]:
    """Every flag the translator can produce, across both mapping backends."""
    flags: set[str] = set()
    for backend, options in (
        ("scsfmlearner", {"target_width": 512, "target_height": 256, "checkpoint_path": "/x.pt"}),
        ("loger", {"window_size": 32, "overlap_size": 3, "model_path": "/y.pt"}),
    ):
        kwargs = scs_kwargs(tmp_path)
        kwargs.update(
            mapping_name=backend,
            mapping_options=options,
            skip_segmentation=True,
            begin_s=1.0,
            end_s=2.0,
            transect_length=10.0,
            transect_crop_width=1.0,
            classes_path="/classes.yaml",
            replacement_radius_factor=1.0,
            replacement_radius_override=0.004,
        )
        flags |= {a for a in build_reconstruct_argv(kwargs) if a.startswith("--")}
    return flags


def test_the_command_reaches_run_reconstruction_with_the_kwargs_it_came_from(
    tmp_path, monkeypatch
):
    """The whole promise of the feature, checked against the real Typer app.

    Drives the generated argv through the installed CLI with the orchestrator
    stubbed out, then compares what arrived against what the GUI would have
    called in-process. Nothing short of this catches a flag that parses but
    binds to a different parameter.
    """
    import deepreefmap.pipeline.orchestrator as orchestrator
    from deepreefmap.cli.main import app
    from deepreefmap.config.classes import DEFAULT_CLASSES_PATH
    from typer.testing import CliRunner

    captured: dict = {}
    monkeypatch.setattr(orchestrator, "run_reconstruction", lambda **kw: captured.update(kw))

    kwargs = scs_kwargs(tmp_path)
    kwargs.update(
        fps=7,
        segmentation_name="segformer-b2",
        mapping_name="loger_star",
        mapping_options={"window_size": 16, "overlap_size": 4, "model_path": None},
        refine_intrinsics_from_mapper=True,
        require_gravity_telemetry=True,
        enable_tsdf=True,
        preprocess_batch_size=6,
        grid_bins=1500,
        begin_s=12.5,
        end_s=90.0,
        transect_length=10.0,
        transect_crop_width=1.5,
        replacement_radius_override=0.004,
    )

    result = CliRunner().invoke(app, ["reconstruct", *build_reconstruct_argv(kwargs)])

    assert result.exit_code == 0, result.output
    for key, expected in kwargs.items():
        if key in IGNORED_KWARGS and key != "mapping_options":
            continue
        if key == "output_dir":
            assert Path(captured[key]) == expected
            continue
        if key == "classes_path" and expected is None:
            # Leaving --classes off means the packaged default, which the CLI
            # fills in itself rather than passing None through.
            assert captured[key] == DEFAULT_CLASSES_PATH
            continue
        assert captured[key] == expected, key


def test_every_emitted_flag_exists_in_the_cli(tmp_path):
    params = cli_parameters()
    for flag in emitted_flags(tmp_path):
        name = flag[2:].replace("-", "_")
        # Typer's paired booleans are declared once, under the positive name.
        name = name.removeprefix("no_")
        assert name in params, f"{flag} is not an option of `deepreefmap reconstruct`"


def test_every_cli_option_is_either_emitted_or_deliberately_skipped(tmp_path):
    # Options the GUI has no business setting from a run form. Present so that a
    # new library option shows up here as a failure rather than as a quietly
    # missing flag.
    known_absent = {
        # The GUI draws into its own Qt viewer and never starts viser. --viser
        # already defaults to off, so omitting the group runs identically; the
        # other two are only ever reached through a viser the run never built.
        "viser",
        "viser_port",
        "keep_viser_open",
    }
    emitted = {f[2:].replace("-", "_") for f in emitted_flags(tmp_path)}
    emitted |= {n[3:] for n in list(emitted) if n.startswith("no_")}
    for name in cli_parameters():
        assert name in emitted or name in known_absent, (
            f"`deepreefmap reconstruct --{name.replace('_', '-')}` is not covered by "
            "run_command.py; add it to the translation table or to known_absent"
        )


def test_kwargs_the_cli_has_no_equivalent_for_are_not_emitted(tmp_path):
    kwargs = scs_kwargs(tmp_path)
    kwargs.update({key: object() for key in IGNORED_KWARGS if key != "mapping_options"})
    kwargs["mapping_options"] = {"target_width": 512, "target_height": 256}

    argv = build_reconstruct_argv(kwargs)

    for token in argv:
        assert "object object at" not in token


# --- Wiring into Browse -------------------------------------------------------


@pytest.fixture
def clipboard(monkeypatch):
    """A stub clipboard: the real X11 selection is shared with the desktop and
    races with clipboard managers."""
    captured: list[str] = []

    class _Clipboard:
        def setText(self, text):
            captured.append(text)

    class _App:
        @staticmethod
        def clipboard():
            return _Clipboard()

    monkeypatch.setattr("deepreefmap_gui.runs.browse.QGuiApplication", _App)
    return captured


def test_browse_copies_a_finished_run_command(window, tmp_path, out_root, clipboard):
    run_dir = out_root / "run-1"
    run_dir.mkdir()
    manifest = {
        "input_videos": [str(tmp_path / "dive.mp4")],
        "cli_argv": ["--videos", str(tmp_path / "dive.mp4"), "--fps", "9"],
    }
    (run_dir / "run_manifest.json").write_text(json.dumps(manifest))
    window._refresh_data_manager()
    assert window._data_run_table.select_run_dir(str(run_dir))

    window._on_data_copy_command_clicked()

    assert len(clipboard) == 1
    assert "--fps 9" in clipboard[0]
