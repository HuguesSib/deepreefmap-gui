"""Model library: reveal the cache folder and export/import portable model packs.

Kept apart from management.py (download/delete/auth) so each mixin stays focused. All
heavy work runs on a daemon thread and marshals back through the _sig_pack_* signals,
matching the download/QC-render pattern elsewhere in the window.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from deepreefmap_gui.core.theme import TEXT_SECONDARY
from deepreefmap_gui.core.widgets import muted_label, ok_cancel_row, secondary_label
from deepreefmap_gui.core.window_protocol import MixinBase
from deepreefmap_gui.survey.models.notification import INFO as NOTIFY_INFO
from deepreefmap_gui.survey.models.notification import MACHINE as NOTIFY_MACHINE
from deepreefmap_gui.survey.models.notification import WARNING as NOTIFY_WARNING

if TYPE_CHECKING:
    from deepreefmap_gui.models.packs import ProgressCallback

logger = logging.getLogger(__name__)


def _byte_label(n: float) -> str:
    if n >= 1024**3:
        return f"{n / 1024**3:.1f} GB"
    return f"{n / 1024**2:.0f} MB"


class PackProgressDialog(QDialog):
    """Progress for a pack export or import.

    A pack is written and verified one repo at a time, so the raw callbacks alternate
    between an "export"/"import" phase and a "verify" phase many times over. Reflecting
    that directly made the bar jump backwards on every switch. Instead the bar tracks
    one monotonic figure: bytes done across every pass, over bytes to do across every
    pass. Each pass counts once, so the readout is scaled back to the pack's own size.
    The layout is fixed so a changing label never resizes the window.
    """

    canceled = Signal()

    _VERBS = {"export": "Copying", "verify": "Verifying", "import": "Installing"}
    _INNER_WIDTH = 400 - 18 - 18  # dialog width minus the layout's side margins

    def __init__(self, heading: str, passes: int, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setModal(True)
        self.setWindowTitle(heading)
        self.setFixedWidth(400)

        self._passes = max(passes, 1)
        self._phase_bytes: dict[str, int] = {}
        self._total = 1
        self._cancelling = False

        layout = QVBoxLayout(self)
        layout.setSpacing(8)
        layout.setContentsMargins(18, 16, 18, 14)

        self._heading = QLabel(heading)
        self._heading.setStyleSheet("font-weight: bold;")
        layout.addWidget(self._heading)

        # One line, never wrapped, fixed width: a long model name is elided with an
        # ellipsis rather than reflowing the dialog to a new height.
        self._status = QLabel("Preparing…")
        self._status.setWordWrap(False)
        self._status.setFixedWidth(self._INNER_WIDTH)
        self._status.setStyleSheet(f"color: {TEXT_SECONDARY};")
        layout.addWidget(self._status)

        self._bar = QProgressBar()
        self._bar.setRange(0, 100)
        self._bar.setValue(0)
        self._bar.setTextVisible(True)
        layout.addWidget(self._bar)

        self._detail = muted_label("")
        layout.addWidget(self._detail)

        row = QHBoxLayout()
        row.addStretch(1)
        self._cancel_btn = QPushButton("Cancel")
        self._cancel_btn.clicked.connect(self._on_cancel_clicked)
        row.addWidget(self._cancel_btn)
        layout.addLayout(row)

    def report(self, phase: str, label: str, current: int, total: int) -> None:
        if self._cancelling:
            return
        self._total = max(total, 1)
        self._phase_bytes[phase] = current
        done = sum(self._phase_bytes.values())
        fraction = min(done / (self._total * self._passes), 1.0)
        self._bar.setValue(round(100 * fraction))
        # The verify pass folds into one 0 to pack-size sweep.
        shown = fraction * self._total
        self._detail.setText(f"{_byte_label(shown)} of {_byte_label(self._total)}")

        verb = self._VERBS.get(phase, "Working on")
        activity = f"{verb} {label}" if label else "Finishing…"
        self._set_status(activity)

    def _set_status(self, text: str) -> None:
        metrics = self._status.fontMetrics()
        self._status.setText(metrics.elidedText(text, Qt.TextElideMode.ElideRight, self._INNER_WIDTH))

    def _on_cancel_clicked(self) -> None:
        if self._cancelling:
            return
        self._cancelling = True
        self._cancel_btn.setEnabled(False)
        self._set_status("Cancelling…")
        self.canceled.emit()


def _throttled(emit: ProgressCallback, cancel: threading.Event | None = None) -> ProgressCallback:
    """Forward pack progress only when the whole percent moves, and honour cancel.

    The verify pass reports every 1 MB, which for a 15 GB pack is fifteen thousand
    queued signals and dialog repaints. One per percent is all the bar can show.

    The callback is the one place the worker touches on every chunk, so a set cancel
    event is raised from here as PackCancelled, unwinding the copy at the next MB."""
    from deepreefmap_gui.models.packs import PackCancelled

    last: tuple[str, str, int] = ("", "", -1)

    def forward(phase: str, label: str, current: int, total: int) -> None:
        nonlocal last
        if cancel is not None and cancel.is_set():
            raise PackCancelled("Cancelled")
        step = (phase, label, round(100 * current / max(total, 1)))
        if step == last and current < total:
            return
        last = step
        emit(phase, label, current, total)

    return forward


def _size_hint(approx_size_mb: float | None) -> str:
    if not approx_size_mb:
        return ""
    if approx_size_mb >= 1024:
        return f"~{approx_size_mb / 1024:.1f} GB"
    return f"~{approx_size_mb:.0f} MB"


class Choice(NamedTuple):
    """One selectable row: a model name, its size in MB, and whether it can be picked."""

    name: str
    size_mb: float | None
    enabled: bool = True
    note: str = ""


class ModelSelectDialog(QDialog):
    """Checkbox list of models to put in an export pack, or to take out of one."""

    def __init__(
        self,
        choices: list[Choice],
        parent: QWidget | None = None,
        *,
        title: str = "Export models",
        prompt: str = "Models to include:",
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)

        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        layout.addWidget(QLabel(prompt))

        self._sizes = {c.name: float(c.size_mb or 0) for c in choices}
        self._checks: dict[str, QCheckBox] = {}

        grid = QGridLayout()
        grid.setColumnStretch(0, 1)
        grid.setHorizontalSpacing(24)
        grid.setVerticalSpacing(6)
        grid.setContentsMargins(0, 0, 0, 0)

        self._select_all = QCheckBox("Select all")
        self._select_all.setTristate(True)
        self._select_all.setChecked(True)
        self._select_all.clicked.connect(self._on_select_all)
        grid.addWidget(self._select_all, 0, 0)

        divider = QFrame()
        divider.setFrameShape(QFrame.Shape.HLine)
        divider.setFrameShadow(QFrame.Shadow.Sunken)
        grid.addWidget(divider, 1, 0, 1, 2)

        for row, choice in enumerate(choices, start=2):
            cb = QCheckBox(choice.name)
            cb.setChecked(choice.enabled)
            cb.setEnabled(choice.enabled)
            if choice.note:
                cb.setToolTip(choice.note)
            cb.toggled.connect(self._on_model_toggled)
            self._checks[choice.name] = cb
            size = secondary_label(choice.note or _size_hint(choice.size_mb))
            size.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            grid.addWidget(cb, row, 0)
            grid.addWidget(size, row, 1)
        layout.addLayout(grid)

        self._total_label = muted_label()
        layout.addWidget(self._total_label)

        buttons = ok_cancel_row(self)
        layout.addWidget(buttons)
        self._ok_button = buttons.button(QDialogButtonBox.StandardButton.Ok)

        self._refresh_summary()
        self.setMinimumWidth(360)

    def selected_names(self) -> set[str]:
        return {name for name, cb in self._checks.items() if cb.isChecked()}

    def _selectable(self) -> list[QCheckBox]:
        return [cb for cb in self._checks.values() if cb.isEnabled()]

    def _on_select_all(self) -> None:
        # A click out of the partial state selects everything.
        select = self._select_all.checkState() != Qt.CheckState.Unchecked
        for cb in self._selectable():
            cb.setChecked(select)

    def _on_model_toggled(self) -> None:
        chosen = len(self.selected_names())
        if chosen == 0:
            state = Qt.CheckState.Unchecked
        elif chosen == len(self._selectable()):
            state = Qt.CheckState.Checked
        else:
            state = Qt.CheckState.PartiallyChecked
        self._select_all.blockSignals(True)
        self._select_all.setCheckState(state)
        self._select_all.blockSignals(False)
        self._refresh_summary()

    def _refresh_summary(self) -> None:
        chosen = self.selected_names()
        total = sum(self._sizes[name] for name in chosen)
        noun = "model" if len(chosen) == 1 else "models"
        summary = f"{len(chosen)} of {len(self._selectable())} {noun} selected"
        if total:
            summary += f", {_size_hint(total)} total"
        self._total_label.setText(summary)
        self._ok_button.setEnabled(bool(chosen))


class ModelLibraryMixin(MixinBase):
    """DeepReefMapWindow methods for opening the cache and packing/unpacking models."""

    def _open_model_library(self) -> None:
        from deepreefmap_gui.models.cache import hf_cache_root

        root = hf_cache_root()
        root.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(root)))

    def _start_pack_progress(self, heading: str, passes: int = 1) -> threading.Event:
        cancel = threading.Event()
        progress = PackProgressDialog(heading, passes, self)
        # The worker keeps running until it sees the cancel event and returns through
        # _on_pack_done, which is what dismisses the dialog.
        progress.canceled.connect(cancel.set)
        progress.show()
        self._pack_progress_dialog = progress
        self._pack_cancel_event = cancel
        return cancel

    def _on_export_models(self) -> None:
        if self._downloading:
            self._status_label.setText("Wait for downloads to finish before exporting.")
            return
        cached = [info for info, is_cached in self._last_model_states if is_cached]
        if not cached:
            self._status_label.setText("No downloaded models to export yet.")
            return

        dlg = ModelSelectDialog([Choice(info.name, info.approx_size_mb) for info in cached], self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        chosen = dlg.selected_names()
        models = [info for info in cached if info.name in chosen]
        if not models:
            return

        dest = QFileDialog.getExistingDirectory(self, "Choose a folder for the model pack")
        if not dest:
            return

        self._status_label.setText("Exporting model pack…")
        # Two passes over the bytes: write, then read back to verify.
        cancel = self._start_pack_progress("Exporting models", passes=2)

        def _worker() -> None:
            from deepreefmap_gui.models import packs

            try:
                pack = packs.export_model_pack(
                    models,
                    dest,
                    progress_cb=_throttled(self._sig_pack_progress.emit, cancel),
                )
                self._sig_pack_done.emit(True, f"Exported model pack to {pack}")
            except packs.PackCancelled:
                self._sig_pack_done.emit(False, "Export cancelled.")
            except Exception as exc:
                logger.exception("Model pack export failed")
                self._sig_pack_done.emit(False, f"Export failed: {str(exc)[:200]}")

        threading.Thread(target=_worker, daemon=True).start()

    def _on_import_model_pack(self) -> None:
        from deepreefmap_gui.models import packs

        if self._downloading:
            self._status_label.setText("Wait for downloads to finish before importing.")
            return

        chosen = QFileDialog.getExistingDirectory(self, "Select a model pack folder")
        if not chosen:
            return
        pack_dir = Path(chosen)
        # Accept the pack folder itself, a parent that contains it, or a single repo
        # folder someone copied off a pack on its own.
        if not packs.is_model_pack(pack_dir):
            candidate = pack_dir / packs.PACK_DIR_NAME
            if packs.is_model_pack(candidate):
                pack_dir = candidate
            else:
                self._status_label.setText("That folder is not a DeepReefMap model pack.")
                return
        if pack_dir.name.startswith("models--"):
            pack_dir = pack_dir.parent

        wanted = self._choose_models_to_import(pack_dir)
        if wanted is None:
            return

        self._status_label.setText("Importing model pack…")
        cancel = self._start_pack_progress("Importing models", passes=1)

        def _worker() -> None:
            try:
                result = packs.import_model_pack(
                    pack_dir,
                    progress_cb=_throttled(self._sig_pack_progress.emit, cancel),
                    model_names=wanted or None,
                )
                parts: list[str] = []
                if result.imported:
                    parts.append(f"{len(result.imported)} imported")
                if result.already_present:
                    parts.append(f"{len(result.already_present)} already present")
                detail = f" ({', '.join(parts)})" if parts else ""
                self._sig_pack_done.emit(True, f"Imported model pack{detail}")
            except packs.PackCancelled:
                self._sig_pack_done.emit(False, "Import cancelled.")
            except Exception as exc:
                logger.exception("Model pack import failed")
                self._sig_pack_done.emit(False, f"Import failed: {str(exc)[:200]}")

        threading.Thread(target=_worker, daemon=True).start()

    def _choose_models_to_import(self, pack_dir: Path) -> list[str] | None:
        """Ask which of a pack's models to take. None means the user cancelled;
        an empty list means take everything (a schema-1 pack is one archive and
        cannot be split, and a pack that lists no models is imported whole)."""
        from deepreefmap_gui.models import packs

        try:
            offered = packs.list_pack_models(pack_dir)
        except packs.PackError as exc:
            self._status_label.setText(f"Could not read the pack: {str(exc)[:160]}")
            return None
        if not offered or packs.pack_schema_version(pack_dir) < 2:
            return []

        dlg = ModelSelectDialog(
            [
                Choice(
                    m.name,
                    m.size_bytes / 1024**2,
                    enabled=m.available,
                    note="" if m.available else "incomplete in this pack",
                )
                for m in offered
            ],
            self,
            title="Import models",
            prompt="Models to import:",
        )
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return None
        return sorted(dlg.selected_names())

    def _on_pack_progress(self, phase: str, label: str, current: int, total: int) -> None:
        dlg = getattr(self, "_pack_progress_dialog", None)
        if dlg is not None:
            dlg.report(phase, label, current, total)

    def _on_pack_done(self, ok: bool, message: str) -> None:
        dlg = getattr(self, "_pack_progress_dialog", None)
        if dlg is not None:
            dlg.close()
            self._pack_progress_dialog = None
        self._status_label.setText(message)
        self._notify_post(
            {
                "fingerprint": "models.pack_imported" if ok else "models.pack_failed",
                "title": message,
                "severity": NOTIFY_INFO if ok else NOTIFY_WARNING,
                "scope": NOTIFY_MACHINE,
                "section": "machine",
            }
        )
        if ok:
            # Reflow the model rows so freshly imported models show as cached.
            threading.Thread(target=self._refresh_model_status, daemon=True).start()
