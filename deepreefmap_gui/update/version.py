"""Updates UI: release check worker, Updates-tab controls, desktop entry toggle."""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtGui import QColor

from deepreefmap_gui.core.theme import UPDATE
from deepreefmap_gui.core.widgets import confirm
from deepreefmap_gui.core.window_protocol import MixinBase
from deepreefmap_gui.packaging.releases import (
    current_version,
    fetch_releases,
    newer_releases,
    parse_version,
    pyapp_binary_path,
    release_version,
    selectable_releases,
)

logger = logging.getLogger(__name__)

# Amber accent used to flag the Updates tab when a newer release exists.
_UPDATE_ACCENT = QColor(UPDATE)


class VersionCheckMixin(MixinBase):
    """DeepReefMapWindow methods for checking GitHub releases and installing updates."""

    # The newest release worth offering, or "" when this is already it. Read by
    # the Setup button, which paints its own slot for it.
    _update_available: str = ""

    def _check_for_update(self) -> None:
        current = current_version()
        releases = fetch_releases()
        pyapp_bin = pyapp_binary_path()
        self._sig_update_check_done.emit(current, releases, pyapp_bin)

    def _set_updates_tab_alert(self, latest: str | None) -> None:
        """Show a waiting release on the Setup button and the Updates view. None clears it."""
        self._update_available = latest or ""
        self._refresh_machine_button()
        self._refresh_update_notice()

    def _apply_update_check(self, current: str, releases: list[dict] | None, pyapp_bin: str | None) -> None:
        self._current_version_str = current
        self._update_version_label.setText(f"Version: <b>{current}</b>")
        self._update_status_label.setToolTip("")
        self._set_updates_tab_alert(None)
        self._update_show_all.setVisible(False)
        self._update_version_combo.setVisible(False)
        self._update_btn.setVisible(False)
        self._available_releases = list(releases or [])

        newer = newer_releases(self._available_releases, current)
        if newer:
            self._set_updates_tab_alert(release_version(newer[0]))

        # Install and rollback swap the binary in place; hidden when running from source.
        if pyapp_bin is None:
            self._update_status_label.setText("Development mode. Launch the installed binary to manage versions.")
            return

        if releases is None:
            self._update_status_label.setText("Couldn't reach GitHub.")
            return
        if not releases:
            self._update_status_label.setText("No releases found.")
            return
        # Rollback needs a version other than the current one.
        self._update_show_all.setVisible(any(release_version(r) != current for r in releases))
        self._populate_update_versions()

    def _locally_kept_versions(self) -> set[str]:
        """Versions whose binary is on disk, so rollback needs no download."""
        from deepreefmap_gui.packaging.binary_swap import available_previous_versions

        pyapp_bin = pyapp_binary_path()
        if pyapp_bin is None:
            return set()
        try:
            return set(available_previous_versions(pyapp_bin))
        except OSError:
            return set()

    def _populate_update_versions(self) -> None:
        current = self._current_version_str
        include_older = self._update_show_all.isChecked()
        selectable = selectable_releases(self._available_releases, current, include_older)
        current_v = parse_version(current)
        kept = self._locally_kept_versions()
        self._update_version_combo.clear()
        for rel in selectable:
            version = release_version(rel)
            rv = parse_version(version)
            marker = ""
            if current_v is not None and rv is not None:
                marker = " ↑" if rv > current_v else " ↓"
            # A kept binary rolls back with no download.
            kept_marker = " (kept)" if version in kept else ""
            self._update_version_combo.addItem(f"{version}{marker}{kept_marker}", rel)
        has_items = self._update_version_combo.count() > 0
        self._update_version_combo.setVisible(has_items)
        self._update_btn.setVisible(has_items)
        self._update_status_label.setToolTip("")
        if not has_items:
            self._update_status_label.setText("Up to date.")
        elif include_older:
            self._update_status_label.setText("Pick a version to install or roll back to:")
            self._update_status_label.setToolTip("An older version may not open surveys created by this one.")
        else:
            self._update_status_label.setText(
                f"Latest: <b>{release_version(selectable[0])}</b>. Pick a version to install:"
            )

    def _on_toggle_show_all_versions(self, _checked: bool) -> None:
        if self._available_releases:
            self._populate_update_versions()

    # --- Installed environments ----------------------------------------------
    # Each version keeps its own PyApp environment; none are pruned automatically.

    @staticmethod
    def _format_size(num_bytes: int) -> str:
        gb = num_bytes / 1024**3
        if gb >= 1:
            return f"{gb:.1f} GB"
        return f"{num_bytes // 1024**2} MB"

    def _refresh_envs(self) -> None:
        import threading

        threading.Thread(target=self._measure_envs, daemon=True).start()

    def _measure_envs(self) -> None:
        """Off-thread: each version env's real size, plus the model-cache total."""
        from deepreefmap_gui.packaging.environments import env_disk_usage, list_environments

        info: dict = {"environments": [], "model_bytes": None}
        try:
            for env in list_environments():
                reclaimable, apparent = env_disk_usage(env.path)
                info["environments"].append(
                    {
                        "version": env.version,
                        "path": str(env.path),
                        "current": env.current,
                        "reclaimable": reclaimable,
                        "apparent": apparent,
                    }
                )
        except OSError:
            logger.debug("Environment scan failed", exc_info=True)
        try:
            from huggingface_hub import scan_cache_dir

            from deepreefmap_gui.models.cache import hf_cache_root

            info["model_bytes"] = scan_cache_dir(cache_dir=hf_cache_root()).size_on_disk
        except Exception:
            logger.debug("Model cache scan failed", exc_info=True)
        self._sig_envs_done.emit(info)

    def _apply_envs(self, info: dict) -> None:
        from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QWidget

        layout = self._env_list_layout
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget() if item is not None else None
            if widget is not None:
                widget.deleteLater()

        environments = info.get("environments", [])
        if not environments:
            layout.addWidget(QLabel("No installed environments found."))
        for env in environments:
            row = QWidget()
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            real = self._format_size(env["reclaimable"])
            shared = self._format_size(max(env["apparent"] - env["reclaimable"], 0))
            running = " (running)" if env["current"] else ""
            label = QLabel(f"<b>{env['version']}</b>{running}: {real} on disk, {shared} shared with the cache")
            label.setWordWrap(True)
            row_layout.addWidget(label, 1)
            if not env["current"]:
                delete_btn = QPushButton("Delete")
                delete_btn.clicked.connect(
                    lambda _c=False, p=env["path"], v=env["version"]: self._on_delete_environment(p, v)
                )
                row_layout.addWidget(delete_btn)
            layout.addWidget(row)

        model_bytes = info.get("model_bytes")
        if model_bytes is None:
            self._model_cache_label.setText("Downloaded models: size unavailable.")
        else:
            self._model_cache_label.setText(f"Downloaded models: <b>{self._format_size(model_bytes)}</b>")

    def _on_delete_environment(self, path: str, version: str) -> None:
        from deepreefmap_gui.packaging.environments import delete_environment

        if not confirm(
            self,
            "Delete environment",
            f"Delete the environment for version {version}?\n\n"
            "Installing this version again rebuilds it from the package cache.",
        ):
            return
        try:
            delete_environment(path)
        except (OSError, ValueError):
            logger.exception("Failed to delete environment %s", path)
        self._refresh_envs()

    def _confirm_downgrade(self, target: str) -> bool:
        """Back up the survey database, then confirm a rollback with its outlook.

        The backup is stamped with the survey's current format and written
        before anything is swapped. Returns True when no rollback is involved.
        """
        current_v = parse_version(self._current_version_str)
        target_v = parse_version(target)
        if current_v is None or target_v is None or target_v >= current_v:
            return True

        from deepreefmap_gui.survey.backup import write_backup
        from deepreefmap_gui.survey.health import inspect_survey_db
        from deepreefmap_gui.survey.rollback import rollback_outlook
        from deepreefmap_gui.survey.store import SURVEY_DB_NAME

        db_path = Path(self._out_root_input.text()).expanduser() / SURVEY_DB_NAME
        outlook = rollback_outlook(db_path, target)
        health = inspect_survey_db(db_path)
        if health.db_version is not None:
            write_backup(db_path, health.db_version)
        return confirm(
            self,
            "Roll back to an older version",
            f"Version {target} is older than the running {self._current_version_str}.\n\n"
            f"{outlook.summary()}\n\n"
            f"Roll back to {target}?",
        )

    def _on_update(self) -> None:
        from deepreefmap_gui.update.dialog import UpdateProgressDialog

        pyapp_bin = pyapp_binary_path()
        if pyapp_bin is None:
            logger.warning("Install clicked but no PyApp binary detected")
            return
        index = self._update_version_combo.currentIndex()
        if index < 0:
            return
        release = self._update_version_combo.itemData(index)
        if not isinstance(release, dict):
            logger.warning("Selected release has no metadata")
            return
        version = release_version(release)
        if not self._confirm_downgrade(version):
            return
        # A kept binary rolls back offline; anything else downloads.
        rollback = version in self._locally_kept_versions()
        self._update_btn.setEnabled(False)
        try:
            dialog = UpdateProgressDialog(
                target_version=version,
                release=release,
                binary_path=Path(pyapp_bin),
                current_version=self._current_version_str,
                rollback=rollback,
                parent=self,
            )
            dialog.run()
        finally:
            self._update_btn.setEnabled(True)
