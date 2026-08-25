"""The icon set: Lucide glyphs, rendered to DPI-aware QIcons.

The glyphs are Lucide (ISC), vendored as SVG under ``resources/icons`` rather
than fetched or installed: a field laptop is offline, and an icon set that has
to be downloaded is an icon set that is sometimes missing. The licence sits
beside them.

Every icon here is one 24px-grid Lucide glyph at one stroke weight, so two of
them in the same button are drawn the same. Colour is applied by substituting
the ``currentColor`` the glyphs are drawn with, which is why each is a function
of ``(size, color)`` rather than a file path.

Only two icons are still drawn by hand, and both are data rather than glyphs:
``status_dot_icon`` is a dot in whatever colour an outcome has, and
``section_state_icon`` picks one of these glyphs for a verdict.
"""

from __future__ import annotations

import functools
from importlib import resources
from typing import Callable

from PySide6.QtCore import QPointF, QSize, Qt
from PySide6.QtGui import QColor, QGuiApplication, QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

from deepreefmap_gui.core.theme import (
    DIRECTION_FORWARD,
    DIRECTION_REVERSE,
    ERROR,
    SUCCESS,
    TEXT_MUTED,
    WARNING,
)

# Inline with text, on a toolbar button, and on the transport controls.
ICON_SM, ICON_MD, ICON_LG = 16, 20, 24

# Ink for a glyph with no role colour of its own: near-white, so it reads on the
# dark surfaces without the hard contrast of pure white. Not a theme token
# because it is the icon layer's own default, not a text colour.
DEFAULT_INK = "#e6e6e6"


@functools.lru_cache(maxsize=64)
def _source(name: str) -> str:
    path = resources.files("deepreefmap_gui.resources").joinpath(f"icons/{name}.svg")
    return path.read_text(encoding="utf-8")


# The screen scale factors a glyph is rendered for. A 150% Windows display asks
# for 1.5x, and Qt picks the 2x bitmap and scales it down, which is sharp; give
# it only the 1x one and it scales up, which is not.
_SCALES = (1, 2, 3)


@functools.lru_cache(maxsize=1024)
def _rendered(name: str, pixels: int, colour: str) -> QPixmap:
    """One glyph at one size in device pixels, in one colour.

    Cached: rows repaint constantly.
    """
    svg = _source(name).replace("currentColor", colour)
    pm = QPixmap(pixels, pixels)
    pm.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pm)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    QSvgRenderer(svg.encode("utf-8")).render(painter)
    painter.end()
    return pm


@functools.lru_cache(maxsize=512)
def _icon(name: str, size: int, colour: str) -> QIcon:
    """One glyph as an icon carrying a bitmap per screen scale.

    The glyph is re-rendered from the vector at each scale rather than one
    bitmap being resampled, which is the reason for keeping these as vectors at
    all. Which bitmap is used is Qt's choice, made per screen at paint time.
    """
    icon = QIcon()
    for scale in _SCALES:
        icon.addPixmap(_rendered(name, size * scale, colour))
    return icon


def _glyph(
    name: str, *, size: int = ICON_MD, ink: str = DEFAULT_INK
) -> Callable[..., QIcon]:
    """Bind a Lucide glyph to the size and ink this app draws it at.

    The result takes the same ``(size, color)`` every icon here does, so a
    caller that wants a bigger or an amber one says so at the call site.
    """

    def build(size_: int = size, color: QColor | None = None) -> QIcon:
        colour = (color.name() if isinstance(color, QColor) else color) or ink
        return _icon(name, size_, colour)

    build.__name__ = f"{name.replace('-', '_')}_icon"
    build.__doc__ = f"The Lucide '{name}' glyph."
    return build


def icon_pixmap(icon: QIcon, size: int = ICON_SM, ratio: float | None = None) -> QPixmap:
    """A glyph as a pixmap for a QLabel, at a screen's scale.

    A button takes the QIcon and picks its own bitmap; a label takes a pixmap
    and draws it as given, so the scale has to be asked for here. Pass the
    label's own ``devicePixelRatio()`` where there is one; the primary screen's
    is the fallback for a widget that has not been shown yet.
    """
    if ratio is None:
        screen = QGuiApplication.primaryScreen()
        ratio = screen.devicePixelRatio() if screen is not None else 1.0
    return icon.pixmap(QSize(size, size), ratio)


# --- The set ----------------------------------------------------------------
#
# One entry per glyph the interface uses, named for the job it does here rather
# than for the picture, so a change of picture is one line.

crosshair_icon = _glyph("crosshair")
refresh_icon = _glyph("refresh-cw")
chevron_right_icon = _glyph("chevron-right", size=ICON_SM, ink=TEXT_MUTED)
chevron_down_icon = _glyph("chevron-down", size=ICON_SM, ink=TEXT_MUTED)
play_icon = _glyph("play")
pause_icon = _glyph("pause")
arrow_right_icon = _glyph("arrow-right")
arrow_left_icon = _glyph("arrow-left")


def direction_arrow_icon(
    direction: str, size: int = ICON_SM, color: QColor | None = None
) -> QIcon:
    """The arrow for a pass direction, already in that direction's colour.

    One place picks left from right and the ink that goes with it, so no call
    site can pair a forward arrow with a reverse colour.
    """
    forward = (direction or "").strip().lower() == "forward"
    glyph = arrow_right_icon if forward else arrow_left_icon
    ink = color or QColor(DIRECTION_FORWARD if forward else DIRECTION_REVERSE)
    return glyph(size, ink)
check_icon = _glyph("check", size=ICON_SM, ink=SUCCESS)
download_icon = _glyph("download")
upload_icon = _glyph("upload", size=ICON_SM, ink=TEXT_MUTED)
warning_icon = _glyph("triangle-alert", size=ICON_SM, ink=WARNING)
blocked_icon = _glyph("ban", size=ICON_SM, ink=ERROR)
cog_icon = _glyph("settings", size=ICON_SM, ink=TEXT_MUTED)
log_icon = _glyph("scroll-text", size=ICON_SM, ink=TEXT_MUTED)
copy_icon = _glyph("copy", size=ICON_SM, ink=TEXT_MUTED)
pencil_icon = _glyph("pencil", size=ICON_SM, ink=TEXT_MUTED)
trash_icon = _glyph("trash-2", size=ICON_SM, ink=TEXT_MUTED)
link_icon = _glyph("link", size=ICON_SM, ink=SUCCESS)
broken_link_icon = _glyph("unlink", size=ICON_SM, ink=ERROR)
folder_icon = _glyph("folder", size=ICON_SM, ink=TEXT_MUTED)
drive_icon = _glyph("hard-drive", size=ICON_SM, ink=TEXT_MUTED)
server_icon = _glyph("server", size=ICON_SM, ink=TEXT_MUTED)
bell_icon = _glyph("bell", size=ICON_SM, ink=TEXT_MUTED)
silence_icon = _glyph("bell-off", size=ICON_SM, ink=TEXT_MUTED)
close_icon = _glyph("x", size=ICON_SM, ink=TEXT_MUTED)
grip_icon = _glyph("grip-vertical", size=ICON_SM, ink=TEXT_MUTED)
cart_icon = _glyph("shopping-cart", size=ICON_SM)
videos_icon = _glyph("film", size=ICON_SM)
lock_icon = _glyph("lock", size=ICON_SM, ink=WARNING)

# The destination glyphs. A transect is a route between two points; browsing is
# reading a table of what came back from them.
transects_icon = _glyph("route", size=ICON_SM)
browse_icon = _glyph("table-2", size=ICON_SM)
process_icon = _glyph("cpu", size=ICON_SM)


# Glyphs for the header's alert box. Only the two states worth acting on get
# one: a badge that is always lit is a badge nobody reads.
#
# The vocabulary belongs to simple/section_state.py, which is Qt-free and so
# cannot be imported from core. tests/simple/test_section_state.py holds the two
# lists together.
STEP_STATES = ("todo", "ok", "attention", "blocked")

_STATE_GLYPHS = {"attention": warning_icon, "blocked": blocked_icon}


def status_dot_icon(colour: str, size: int = ICON_SM) -> QIcon:
    """A filled dot in an outcome's colour, for a row whose status is one word
    inside a longer line and has no room for a chip.

    Drawn rather than a glyph: the colour is the whole content, and there is no
    picture to get right. One bitmap per screen scale, as for the glyphs.
    """
    icon = QIcon()
    for scale in _SCALES:
        pixels = size * scale
        pm = QPixmap(pixels, pixels)
        pm.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pm)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(colour))
        radius = pixels * 0.22
        painter.drawEllipse(QPointF(pixels / 2, pixels / 2), radius, radius)
        painter.end()
        icon.addPixmap(pm)
    return icon


def section_state_icon(state: str, size: int = ICON_SM) -> QIcon | None:
    """The glyph for a destination's verdict, or None when there is nothing to say."""
    if state not in STEP_STATES:
        raise ValueError(f"Unknown section state: {state!r}")
    draw = _STATE_GLYPHS.get(state)
    return draw(size) if draw is not None else None
