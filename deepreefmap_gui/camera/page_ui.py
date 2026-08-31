"""The Cameras view: the lenses this computer can undistort, and how to add one.

A Setup view rather than a destination, beside Models: a camera profile is
something installed on this machine, not part of a survey. The run form still
offers `Calibrate…` beside its profile picker, because that is where somebody
discovers they need one; this is where the set as a whole is read, checked
against the clip it came from, and pruned.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from deepreefmap_gui.camera.inventory import (
    ProfileEntry,
    delete_profile,
    export_profile,
    import_profile,
    list_profiles,
)
from deepreefmap_gui.camera.profiles import camera_profiles_dir
from deepreefmap_gui.core.theme import GUTTER, PRIMARY, SPACE_SM, TEXT_MUTED
from deepreefmap_gui.core.widgets import (
    StatusChip,
    confirm,
    muted_label,
    secondary_label,
    section_card,
)

logger = logging.getLogger(__name__)

INTRO = (
    "A profile undistorts the footage before it is mapped. The bundled ones cover the "
    "cameras we ship with; calibrate a clip to add any other, or import one calibrated "
    "on another laptop."
)
BUNDLED = "Bundled"
CALIBRATED = "Calibrated here"
IMPORTED = "Imported"
_PREVIEW_WIDTH = 420
_PROFILE_FILTER = "Camera profiles (*.json);;All files (*)"


def _origin(entry: ProfileEntry) -> str:
    """Where this profile came from, in one word."""
    if not entry.local:
        return BUNDLED
    return IMPORTED if entry.imported else CALIBRATED


def _facts(entry: ProfileEntry) -> str:
    """The line under a profile's name: what it describes, in one sentence."""
    parts = [entry.resolution, entry.camera_model.title() if entry.camera_model else ""]
    if entry.focal_px is not None:
        parts.append(f"focal length {entry.focal_px:.0f} px")
    return "  ·  ".join(part for part in parts if part)


def _provenance(entry: ProfileEntry) -> str:
    """Where a calibrated profile came from, and how well it reconstructed."""
    parts = []
    # The bundled profiles name a clip nobody here has, so only a profile made on
    # this machine says which one it came from.
    if entry.local and entry.source_video:
        parts.append(f"from {entry.source_video}")
    if entry.registered is not None:
        registered, offered = entry.registered
        parts.append(f"{registered} of {offered} frames registered")
    if entry.reprojection_error_px is not None:
        parts.append(f"{entry.reprojection_error_px:.2f} px mean reprojection error")
    return "  ·  ".join(parts)


class CameraProfilesPanel(QWidget):
    """The profile list, its Calibrate button, and the delete beside each one."""

    changed = Signal()
    # Named so a caller can select what just arrived, as the calibration does.
    _imported = Signal(str)
    _published = Signal(object, object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(GUTTER)

        header = QHBoxLayout()
        intro = muted_label(INTRO)
        intro.setWordWrap(True)
        header.addWidget(intro, 1)
        self._import = QPushButton("Import…")
        self._import.setProperty("quiet", "true")
        self._import.setToolTip("Add a profile calibrated on another laptop, from a file.")
        self._import.clicked.connect(self._on_import)
        header.addWidget(self._import, 0, Qt.AlignmentFlag.AlignTop)
        self._calibrate = QPushButton("Calibrate…")
        self._calibrate.setProperty("cta", "true")
        self._calibrate.setToolTip("Calibrate a new profile from a clip shot on the camera it describes.")
        self._calibrate.clicked.connect(self._on_calibrate)
        header.addWidget(self._calibrate, 0, Qt.AlignmentFlag.AlignTop)
        outer.addLayout(header)

        # No registry until the window says there is one, following the archive
        # controls: a button that publishes nowhere is worse than no button.
        self._connected = False
        self._published.connect(self._on_published)

        self._cards = QVBoxLayout()
        self._cards.setSpacing(GUTTER)
        outer.addLayout(self._cards)

        self._status = secondary_label("")
        self._status.setWordWrap(True)
        outer.addWidget(self._status)

        self._location = secondary_label("")
        self._location.setWordWrap(True)
        outer.addWidget(self._location)
        outer.addStretch(1)
        self.refresh()

    def refresh(self) -> None:
        """Re-read the profiles on disk and rebuild the cards."""
        while self._cards.count():
            item = self._cards.takeAt(0)
            widget = item.widget() if item is not None else None
            if widget is not None:
                # Unparented before it is queued for deletion: deleteLater alone
                # leaves the old card a child until the loop spins, so a rebuild
                # would show both it and its replacement.
                widget.setParent(None)
                widget.deleteLater()
        for entry in list_profiles():
            self._cards.addWidget(self._card(entry))
        self._location.setText(
            f"Profiles are kept in {camera_profiles_dir()}. Set DEEPREEFMAP_CAMERA_PROFILES "
            "to point this laptop at a shared folder instead."
        )

    def _card(self, entry: ProfileEntry) -> QWidget:
        card, layout = section_card()

        title = QHBoxLayout()
        name = QLabel(entry.name)
        name.setStyleSheet("font-weight: 600;")
        title.addWidget(name)
        chip = StatusChip()
        chip.set_status(_origin(entry), PRIMARY if entry.local else TEXT_MUTED)
        title.addWidget(chip)
        title.addStretch(1)
        if entry.local and self._connected:
            publish = QPushButton("Publish")
            publish.setProperty("quiet", "true")
            publish.setToolTip(
                "Send this calibration to the registry, so the console can give it to "
                "every laptop that shoots on this camera."
            )
            publish.clicked.connect(lambda _=False, e=entry: self._on_publish(e))
            title.addWidget(publish)
        if entry.local:
            share = QPushButton("Export…")
            share.setProperty("quiet", "true")
            share.setToolTip("Write this profile to a file, to import on another laptop.")
            share.clicked.connect(lambda _=False, e=entry: self._on_export(e))
            title.addWidget(share)
        if entry.log is not None:
            log = QPushButton("Log")
            log.setProperty("quiet", "true")
            log.setToolTip("What the calibration did, and what COLMAP made of the clip.")
            log.clicked.connect(lambda _=False, path=entry.log: self._open_log(path))
            title.addWidget(log)
        if entry.local:
            remove = QPushButton("Delete")
            remove.setProperty("quiet", "true")
            remove.setToolTip("Remove this profile from this computer. Runs already made carry their own copy.")
            remove.clicked.connect(lambda _=False, e=entry: self._on_delete(e))
            title.addWidget(remove)
        layout.addLayout(title)

        if entry.error:
            layout.addWidget(secondary_label(f"This profile could not be read: {entry.error}"))
            return card

        layout.addWidget(muted_label(_facts(entry)))
        provenance = _provenance(entry)
        if provenance:
            layout.addWidget(secondary_label(provenance))
        if entry.preview is not None:
            preview = QLabel()
            pixmap = QPixmap(str(entry.preview))
            if not pixmap.isNull():
                preview.setPixmap(
                    pixmap.scaledToWidth(_PREVIEW_WIDTH, Qt.TransformationMode.SmoothTransformation)
                )
                preview.setToolTip("The clip as shot, beside the same frame rectified.")
                layout.addWidget(preview)
        layout.addSpacing(SPACE_SM)
        return card

    def _on_import(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Import camera profile", "", _PROFILE_FILTER)
        if not path:
            return
        self._import_file(Path(path))

    def _import_file(self, path: Path, name: str | None = None) -> None:
        """Take the file, asking for another name when this one is taken.

        A name already held by different content is a collision, not a mistake:
        two laptops calibrating one rig both produce `hero12_dome`, and the one
        already here was somebody's measurement too.
        """
        try:
            entry = import_profile(path, name=name)
        except FileExistsError as exc:
            chosen, accepted = QInputDialog.getText(
                self,
                "Name taken",
                f"This laptop already has a different {exc.args[0]}.\nImport it as:",
                text=f"{exc.args[0]}_2",
            )
            if accepted and chosen.strip():
                self._import_file(path, chosen.strip())
            return
        except Exception as exc:
            logger.warning("Could not import the camera profile at %s: %s", path, exc)
            QMessageBox.warning(self, "Import failed", f"{path.name} is not a camera profile.")
            return
        self.refresh()
        self.changed.emit()
        self._imported.emit(entry.name)

    def _on_export(self, entry: ProfileEntry) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "Export camera profile", f"{entry.name}.json", _PROFILE_FILTER
        )
        if not path:
            return
        try:
            export_profile(entry, Path(path))
        except OSError as exc:
            logger.warning("Could not export %s: %s", entry.name, exc)
            QMessageBox.warning(self, "Export failed", str(exc))

    def set_server_connected(self, connected: bool) -> None:
        """Offer publishing only where there is a registry to publish to.

        Remembered rather than pushed once: the cards are rebuilt on every
        refresh, and a new card must not arrive carrying a button the window has
        already said there is no server for.
        """
        if connected == self._connected:
            return
        self._connected = connected
        self.refresh()

    def _on_publish(self, entry: ProfileEntry) -> None:
        """Offer one calibration to the registry, on a worker thread.

        The whole exchange is one small request, but it is still a network call
        and the window must not stop for it.
        """
        from deepreefmap_gui.camera.profiles import load_profile, profile_payload

        try:
            profile = load_profile(entry.name)
        except Exception as exc:
            logger.warning("Could not read %s to publish it: %s", entry.name, exc)
            self._status.setText(f"{entry.name} could not be read.")
            return
        diagnostics = profile.diagnostics or {}
        payload = {
            "name": entry.name,
            "document": profile_payload(profile),
            "source_clip": str(diagnostics.get("source_video") or ""),
            "reprojection_error_px": diagnostics.get("mean_reprojection_error_px"),
            "registered_frames": diagnostics.get("n_registered_images"),
        }
        self._status.setText(f"Publishing {entry.name}…")

        def work() -> None:
            from deepreefmap_gui.sync import client, credentials

            try:
                held = credentials.load()
                if held is None:
                    raise RuntimeError("this laptop is not enrolled with a registry")
                answer = client.SyncClient(held.base_url, held.token).publish_calibration(payload)
            except Exception as exc:  # reported on the GUI thread
                self._published.emit(None, str(exc))
                return
            self._published.emit(answer, None)

        threading.Thread(target=work, name="publish-calibration", daemon=True).start()

    def _on_published(self, answer: object, error: object) -> None:
        if error is not None:
            self._status.setText(f"Could not publish: {error}")
            return
        answered = answer if isinstance(answer, dict) else {}
        version = answered.get("version")
        if answered.get("created"):
            self._status.setText(f"Published as version {version}. The console can now assign it.")
        else:
            self._status.setText(f"The registry already holds this calibration as version {version}.")

    def _open_log(self, path: Path) -> None:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def _on_calibrate(self) -> None:
        from deepreefmap_gui.camera.calibration_dialog import CalibrationDialog

        dialog = CalibrationDialog(self)
        if dialog.exec() == QDialog.DialogCode.Accepted and dialog.saved_name:
            self.refresh()
            self.changed.emit()

    def _on_delete(self, entry: ProfileEntry) -> None:
        if not confirm(
            self,
            "Delete camera profile",
            f"Delete {entry.name}? Runs already made carry their own copy of it; a new run "
            "cannot use it until it is calibrated or imported again.",
        ):
            return
        try:
            delete_profile(entry)
        except OSError as exc:
            logger.warning("Could not delete camera profile %s: %s", entry.name, exc)
        self.refresh()
        self.changed.emit()
