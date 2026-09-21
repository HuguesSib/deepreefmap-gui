"""Capture one PNG per screen, in both UI modes, without a display or a GPU.

The GUI is the deliverable, so reviewing a layout change means looking at it.
This builds a real ``DeepReefMapWindow`` against a seeded survey store and grabs
the screen per section, so a before/after diff is a directory of images rather
than a description. Run it under a virtual X server -- VTK needs a GL context,
which the offscreen Qt platform plugin does not provide:

    xvfb-run -a -s "-screen 0 1600x1000x24" \\
        uv run python scripts/screenshots.py --out shots/

QSettings, the output root and the survey preset are all redirected into
tempdirs, the same isolation ``tests/conftest.py`` and ``tests/gui/conftest.py``
apply, so a capture never reads or overwrites the developer's own config, run
history or machine settings.
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path

# Before any Qt import: mock an empty version list so window construction never
# reaches GitHub, and force software GL so the capture works on a headless host
# with no driver.
os.environ.setdefault("DEEPREEFMAP_MOCK_VERSIONS", "")
os.environ.setdefault("QT_OPENGL", "desktop")
os.environ.setdefault("LIBGL_ALWAYS_SOFTWARE", "1")

# Wide enough that no page has to reflow, and tall enough to show whether a page
# fills the window or piles its content at the top.
WINDOW_SIZE = (1500, 940)

# Qt lays out and paints on the event loop, so every step yields to it. VTK's
# first render and the map widget's tile decode are the slow ones.
SETTLE_MS = 600
STARTUP_MS = 1800


def _isolate_config() -> Path:
    """Point QSettings and the output root at tempdirs. Returns the root."""
    from PySide6.QtCore import QSettings

    config = tempfile.mkdtemp(prefix="deepreefmap-shots-config-")
    for fmt in (QSettings.Format.NativeFormat, QSettings.Format.IniFormat):
        QSettings.setPath(fmt, QSettings.Scope.UserScope, config)
        QSettings.setPath(fmt, QSettings.Scope.SystemScope, config)

    out_root = Path(tempfile.mkdtemp(prefix="deepreefmap-shots-out-"))
    settings = QSettings("ECEO", "deepreefmap")
    settings.setValue("output_root_dir", str(out_root))
    # Skip the first-run environment check, which would otherwise open on the
    # setup page instead of the page being captured.
    settings.setValue("setup_complete", True)
    return out_root


def _standard_preset():
    """The organisation preset a capture's runs claim to have been made under."""
    from deepreefmap_gui.survey.preset import load_active_preset

    return load_active_preset().org


def seed_survey(out_root: Path) -> None:
    """Write the survey a capture describes: three transects, six passes, six runs.

    Deliberately not empty. An empty app hides every layout problem that only
    appears once a table has rows, a name is long enough to elide, or a detail
    pane has something to show.
    """
    from deepreefmap_gui.survey.models import (
        RunRecord,
        SurveyBatch,
        Transect,
        TransectPass,
        VideoAsset,
    )
    from deepreefmap_gui.survey.models.convert import survey_manifest_block
    from deepreefmap_gui.survey.preset import manifest_config_block
    from deepreefmap_gui.survey.store import SURVEY_DB_NAME, SurveyStore

    places = [
        ("Vatu-i-Ra North", -17.2841, 178.5211, 50.0),
        ("Vatu-i-Ra South", -17.2903, 178.5188, 50.0),
        ("Namena Reef East", -17.1102, 179.0921, 30.0),
    ]
    covers = [
        {"hard coral": 0.34, "soft coral": 0.11, "algae": 0.22, "sand": 0.18, "rubble": 0.09},
        {"hard coral": 0.31, "soft coral": 0.13, "algae": 0.25, "sand": 0.16, "rubble": 0.09},
    ]
    # One of each outcome, so the status pills and the failure copy are captured
    # rather than assumed.
    statuses = ["succeeded", "succeeded", "succeeded", "succeeded", "failed", "succeeded"]

    store = SurveyStore(out_root / SURVEY_DB_NAME)
    # Two sessions, so By session has something to be a grouping of rather than
    # one node holding everything.
    sessions = [
        SurveyBatch(created_at="2026-08-04T09:12:00"),
        SurveyBatch(created_at="2026-08-02T14:35:00"),
    ]
    for session in sessions:
        store.add_batch(session)
    transects = []
    for index, (name, lat, lon, length) in enumerate(places):
        transect = Transect(
            name=name,
            start_lat=lat,
            start_lon=lon,
            end_lat=lat - 0.0004,
            end_lon=lon + 0.0005,
            length_m=length,
            depth_m=8.0 + index,
        )
        store.add_transect(transect)
        transects.append(transect)

    # The last clip carries no checksum, so the rail's third link state has
    # something to paint. The paths point nowhere on purpose: every clip comes
    # back "missing", which is the state a capture would otherwise never show
    # and the one whose copy most needs reading.
    videos = [
        store.upsert_video(
            VideoAsset(
                file_name=f"GX01000{n + 1}.MP4",
                path=f"/data/dive1/GX01000{n + 1}.MP4",
                hash=None if n == len(statuses) - 1 else f"{n:02d}" * 16,
            )
        )
        for n in range(len(statuses))
    ]

    for index, status in enumerate(statuses):
        transect = transects[index % len(transects)]
        session = sessions[0] if index >= 2 else sessions[1]
        pass_ = TransectPass(
            transect_id=transect.id,
            video_id=videos[index].id,
            begin_s=0.0,
            end_s=90.0 + 30 * index,
            direction="forward" if index % 2 == 0 else "reverse",
            batch_id=session.id,
        )
        store.add_pass(pass_)
        run = RunRecord(pass_id=pass_.id, run_dir_name=f"20260804-10{index}000", status=status)
        store.add_run(run)

        run_dir = out_root / run.run_dir_name
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "run_manifest.json").write_text(
            json.dumps(
                {
                    "name": f"{transect.name} pass {index + 1}",
                    "mode": "semantic",
                    "input_videos": [videos[index].path],
                    "video_hashes": [videos[index].hash],
                    "run_timestamp": f"2026-08-04T1{index}:00:00+00:00",
                    "begin_s": 0.0,
                    "end_s": 90.0 + 30 * index,
                    "run_duration_s": 300.0 + 60 * index,
                    "frames_processed": 450,
                    "fps": 5,
                    "semantic_reference_points": 1_200_000,
                    "benthic_cover": covers[index % len(covers)],
                    # What produced the numbers, as a real run records it. The
                    # last pass deviated, so the provenance rows are captured
                    # saying so rather than only saying "as standard".
                    "segmentation_model": "coralscapes-vit-b-dpt",
                    "mapping_backend": "loger_star",
                    "deepreefmap_version": "1.4.0",
                    "survey": survey_manifest_block(
                        run,
                        pass_,
                        transect,
                        session,
                        config=manifest_config_block(
                            _standard_preset(), {"fps": 3} if index == 5 else {}
                        ),
                    ),
                }
            ),
            encoding="utf-8",
        )
        # The cover the analysis actually reads. Without this the Transects page
        # captures its empty state, which is the one thing a shot of a cover
        # panel must not show.
        if status == "succeeded":
            (run_dir / "benthic_cover.json").write_text(
                json.dumps(_cover_file(index)), encoding="utf-8"
            )
    store.close()


# Class ids from the bundled Coralscapes table, so aggregate_cover can group
# them. Two passes of one transect differ slightly, which is what makes the
# repeatability columns say anything.
_COVER_IDS = {"1": 0.34, "13": 0.22, "3": 0.18, "4": 0.09, "20": 0.17}


def _cover_file(index: int) -> dict:
    """One run's benthic_cover.json, jittered per pass so the spread is real."""
    jitter = 1.0 + (0.06 if index % 2 else -0.06)
    total = 250_000.0
    return {
        "classes": {
            class_id: {
                "name": class_id,
                "count": fraction * jitter * total,
                "fraction": fraction * jitter,
            }
            for class_id, fraction in _COVER_IDS.items()
        },
        "denominator": total,
    }


class Capture:
    """A window on a virtual screen, and the grabs taken from it."""

    def __init__(self, out_dir: Path) -> None:
        from PySide6.QtGui import QSurfaceFormat
        from PySide6.QtWidgets import QApplication

        self._out_dir = out_dir
        out_dir.mkdir(parents=True, exist_ok=True)

        # Same surface request as launch(): VTK 9's OpenGL2 backend needs >=3.2
        # core, and it has to be set before the QApplication exists.
        fmt = QSurfaceFormat()
        fmt.setRenderableType(QSurfaceFormat.RenderableType.OpenGL)
        fmt.setVersion(3, 2)
        fmt.setProfile(QSurfaceFormat.OpenGLContextProfile.CoreProfile)
        QSurfaceFormat.setDefaultFormat(fmt)

        self.app = QApplication.instance() or QApplication([])
        from deepreefmap_gui.core.fonts import apply_app_fonts
        from deepreefmap_gui.core.theme import apply_theme

        apply_app_fonts(self.app)
        apply_theme(self.app)

    def settle(self, ms: int = SETTLE_MS) -> None:
        from PySide6.QtCore import QEventLoop, QTimer

        loop = QEventLoop()
        QTimer.singleShot(ms, loop.quit)
        loop.exec()

    def shot(self, name: str) -> Path:
        self.settle()
        path = self._out_dir / f"{name}.png"
        # grabWindow(0) takes the whole screen rather than the widget, which is
        # what catches the VTK canvas: it paints straight to the screen and a
        # QWidget.grab() of it comes back empty.
        self.app.primaryScreen().grabWindow(0).save(str(path))
        print(f"  {path}")
        return path


def build_window():
    """A window with a graphics card assumed, so the run gate is deterministic.

    The bundled preset selects a GPU-only mapper, so on a machine without a card
    every Run-page capture would show the blocked state instead of the page.
    """
    from deepreefmap.config.classes import load_classes

    from deepreefmap_gui.app import DeepReefMapWindow
    from deepreefmap_gui.form.panel import FormPanelMixin

    FormPanelMixin._gpu_available = lambda self: True  # type: ignore[method-assign]
    window = DeepReefMapWindow(load_classes(), None)
    window.resize(*WINDOW_SIZE)
    window.show()
    return window


def capture_all(out_dir: Path) -> None:
    from deepreefmap_gui.runs.browse import _GROUPED_FACETS
    from deepreefmap_gui.simple.machine import MACHINE_VIEWS

    out_root = _isolate_config()
    seed_survey(out_root)

    capture = Capture(out_dir)
    window = build_window()
    capture.settle(STARTUP_MS)

    capture.settle(SETTLE_MS * 2)
    # The link scan is threaded in the app; here it is resolved directly so the
    # clip rail is captured with its answers rather than mid-flight.
    from deepreefmap_gui.survey.catalogue import resolve_link_states

    window._apply_clip_link_states(resolve_link_states(window._video_entries))

    for section in ("transects", "process", "browse"):
        window._set_simple_section(section)
        capture.shot(section)

    # Each view of Setup: they share a page, so a regression in one is
    # invisible in a shot of another.
    window._set_simple_section("machine")
    for view in MACHINE_VIEWS:
        window._set_machine_view(view)
        capture.settle()
        capture.shot(f"machine-{view}")

    # Selections and groupings are where the layout actually has to hold up, so
    # each is captured rather than only the resting state.
    window._set_simple_section("browse")
    window._data_run_table.selectRow(0)
    capture.shot("browse-selected")

    for facet in _GROUPED_FACETS:
        window._data_facet_buttons[facet].click()
        capture.settle()
        capture.shot(f"browse-by-{facet}")
        # Selecting the first group too: each facet has its own detail pane, and
        # a shot of the resting state never shows any of them. The table's
        # selection is cleared first, because a selected run outranks the group
        # in the pane and the earlier browse-selected shot left one behind.
        tree = window._data_tree
        if tree.topLevelItemCount():
            window._data_run_table.clearSelection()
            window._data_run_table.setCurrentCell(-1, -1)
            tree.setCurrentItem(tree.topLevelItem(0))
            capture.settle()
            capture.shot(f"browse-by-{facet}-selected")
    window._data_facet_buttons["runs"].click()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("shots"),
        help="Directory to write the PNGs into (default: shots/)",
    )
    args = parser.parse_args()
    capture_all(args.out)


if __name__ == "__main__":
    main()
