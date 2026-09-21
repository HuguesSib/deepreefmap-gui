"""The cart pill: the Process destination, badged with the next session's count."""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFontMetrics, QPainter, QPainterPath
from PySide6.QtWidgets import QToolButton, QWidget

from deepreefmap_gui.core.icons import ICON_SM, cart_icon
from deepreefmap_gui.core.theme import ON_ACCENT, PRIMARY, RADIUS_SM, SPACE_SM, SPACE_XS
from deepreefmap_gui.core.widgets import utility_button_qss


class CartButton(QToolButton):
    """The destination pill for Process, at the far right of the header.

    Carries the cart's count as a badge painted into reserved right padding
    (the MachineButton pattern) so the label does not shift as it comes and
    goes. Hidden at zero: a badge that is always lit is a badge nobody reads.
    The badge inverts on a checked pill, or the ``PRIMARY`` fill would swallow it.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._count = 0
        self.setText("Processing queue")
        self.setIcon(cart_icon(ICON_SM))
        self.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.set_count(0)

    def _badge_text(self) -> str:
        return str(self._count) if self._count < 100 else "99+"

    def _badge_width(self) -> int:
        metrics = QFontMetrics(self.font())
        return max(ICON_SM, metrics.horizontalAdvance(self._badge_text()) + 2 * SPACE_XS)

    def set_count(self, count: int) -> None:
        self._count = max(0, count)
        reserved = SPACE_SM + self._badge_width() + SPACE_XS if self._count else SPACE_SM
        self.setStyleSheet(utility_button_qss(reserved))
        self.setAccessibleName(f"Processing queue: {self._count} queued")
        self.update()

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        if not self._count:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        width = self._badge_width()
        height = ICON_SM
        x = self.width() - SPACE_SM - width
        y = (self.height() - height) // 2
        fill, ink = (ON_ACCENT, PRIMARY) if self.isChecked() else (PRIMARY, ON_ACCENT)
        pill = QPainterPath()
        pill.addRoundedRect(QRectF(x, y, width, height), RADIUS_SM, RADIUS_SM)
        painter.fillPath(pill, QColor(fill))
        painter.setPen(QColor(ink))
        painter.drawText(
            QRectF(x, y, width, height), Qt.AlignmentFlag.AlignCenter, self._badge_text()
        )
        painter.end()
