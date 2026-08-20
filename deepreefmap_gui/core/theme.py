"""The design tokens, and the one place the palette and the global stylesheet are set.

Every colour, radius, spacing step and font size in the app comes from a name here. That is the
whole point: a literal `#4a4` or a bare `padding: 5px` at a call site has drifted off the ramp and
will not move when the ramp does. `apply_theme` then forces Fusion plus a dark palette so the app
looks the same whatever the host OS is doing, which matters because the stylesheets below are
hardcoded dark and a light platform palette shows through everything they do not cover.

Two things about the values are easy to get wrong:

- **The surface ramp is spaced by lightness, not by contrast ratio.** Any two dark greys sit near
  1:1 under WCAG, so a ratio says nothing about whether a card reads as a card. An earlier ramp
  put 13 points of lightness between the shell and a panel, and panels read as text floating on
  the window. `tests/core/test_theme.py` asserts the spacing and the 4.5:1 the accents do owe.
- **Styling a combo or spin box hands arrow drawing to the stylesheet engine**, which then draws
  nothing. Qt stylesheets accept only a URL, so the chevrons are painted into the cache directory
  and referenced from there. Painting needs a live QApplication, which is why the arrow rules are
  appended by `apply_theme` rather than living in the module-level QSS.
"""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import QPointF, QStandardPaths, Qt
from PySide6.QtGui import QColor, QPainter, QPalette, QPen, QPixmap
from PySide6.QtWidgets import QApplication, QProxyStyle, QStyle

logger = logging.getLogger(__name__)

# Structural greys as an elevation ramp: the shell is the darkest layer, panels
# sit above it, and hovered/raised controls above those. Each step is far enough
# from the last to be visible, and all carry the same faint blue-grey cast so
# they read as one surface family rather than as unrelated greys.
#
# The steps are wide on purpose. An earlier ramp put only 13 points of lightness
# between the shell and a card, and 20 between a card and its own border, which
# is why panels read as text floating on the window rather than as panels. The
# separation a reader actually sees between two large adjacent fills is the
# lightness delta, not the WCAG ratio (which is near 1:1 for any two dark greys),
# so the ramp is spaced by lightness and asserted that way in test_theme.py.
WINDOW = "#101317"  # app shell, behind everything
WINDOW_TEXT = "#dfe5ec"
BASE = "#181c21"  # text fields and item views, recessed into a panel
ALT_BASE = "#1e232a"
BUTTON = "#2f363f"
BORDER = "#3e4751"  # hairline: above the panel fills, below the top of the ramp
GROOVE = "#14181d"  # recessed track (progress bars, sliders): below BASE, not level with BUTTON

# Recurring dark fills and text shades that sit between the palette roles above.
CARD_BG = "#242a31"  # raised card/panel fill, a visible step off WINDOW
SURFACE_HI = "#434d59"  # hover and raised states, the top of the fill ramp
BORDER_STRONG = "#57626f"  # dividers and hovered control borders
PREVIEW_BG = "#0b0d10"  # backdrop behind image/preview panels before they load
OVERLAY_TEXT = "#e8e8e8"  # bright label text on the dark viewer overlays
TEXT_SECONDARY = "#b9c4cf"  # readouts a shade brighter than TEXT_MUTED
TEXT_MUTED = "#93a0ad"  # labels and captions beside the text they describe
TEXT_DIM = "#8b98a6"  # least prominent text, dimmer than TEXT_MUTED
DISABLED_FG = "#7d8590"  # unavailable controls; dimmer again, but still readable
PLACEHOLDER_TEXT = "#9aa3ad"  # prompt text inside an empty field
SLIDER_HANDLE = "#f0f0f0"  # near-white grab handle on trim/timeline sliders

# Item-view selection. A soft PRIMARY tint under unchanged body text, rather
# than a full-bleed PRIMARY slab with near-black text: a selected row should be
# legible at a glance, not the loudest thing on the page. Line edits keep the
# strong palette Highlight, where a hard selection colour is what you want.
SELECTION_BG = "#2f5478"

# A control that sits on a selected row. A quiet button is drawn on nothing,
# and on nothing it disappears into the selection fill, so it keeps a dark
# ground under itself. The outline is off white rather than off the border
# ramp: every grey in that ramp is close enough to the selection blue that the
# edge it draws cannot be found.
SELECTION_CONTROL_BG = "rgba(0, 0, 0, 90)"
SELECTION_CONTROL_BORDER = "rgba(255, 255, 255, 120)"

# Named semantic accents. These consolidate several inconsistent spellings that
# were scattered across the GUI (e.g. success was both "#4a4" and
# QColor(74, 170, 74)); migrate call sites onto these so there's one value each.
#
# Every one of these clears 4.5:1 against WINDOW, BASE and CARD_BG, and against
# the tinted status pill each one paints for itself. See test_theme.py.
SUCCESS = "#5cbf5c"
WARNING = "#e8a04a"
ERROR = "#ff6b5e"  # brighter than the old #c0392b/#c84 for contrast on dark
PRIMARY = "#4aa3ff"
# Work that is planned and has not started. A cool slate rather than the plain
# grey of TEXT_MUTED: a queued section is waiting, not unavailable, and the two
# read the same when both are drawn as grey beside a disabled control.
IDLE = "#8aa0b8"
# Which way a pass was swum. Teal and orchid rather than teal and orange: orange
# sits in the same hue band as WARNING, UPDATE and ERROR, and the same tables
# paint failures, so a reverse pass would read as a caution.
DIRECTION_FORWARD = "#46c2b4"
DIRECTION_REVERSE = "#e58fd6"
PRIMARY_DARK = "#2a78c8"  # PRIMARY's outline/handle-border shade
LINK = "#9ecbff"
UPDATE = "#e0a030"
DANGER_BG = "#8a2222"  # filled "Confirm delete?" button; distinct from ERROR text
BLOCK = "#ff7a70"  # hard "blocked" red on gauges and pre-flight verdicts; distinct from ERROR text

# Text laid over a filled accent, rather than beside it: the CTA label, the
# selected segment of a segmented control, a highlighted item.
ON_ACCENT = WINDOW
BRIGHT_TEXT = "#ffffff"

# Panels that float over the 3D canvas rather than over the shell. They are
# deliberately translucent -- the point of an overlay is that the cloud shows
# through it -- so they are their own small ramp rather than values off the
# surface one, which assumes an opaque parent.
OVERLAY_BG = "rgba(20, 20, 20, 200)"
OVERLAY_BG_STRONG = "rgba(28, 28, 28, 240)"
OVERLAY_BORDER = "rgba(255, 255, 255, 40)"
OVERLAY_BORDER_STRONG = "rgba(255, 255, 255, 80)"
OVERLAY_FILL = "rgba(255, 255, 255, 20)"
OVERLAY_FILL_HI = "rgba(255, 255, 255, 50)"
OVERLAY_ACCENT_FILL = "rgba(74, 163, 255, 90)"
OVERLAY_HANDLE = "#dddddd"
OVERLAY_TEXT_DIM = "#b8b8b8"
OVERLAY_TEXT_LINK = "#cfd6dd"
OVERLAY_DANGER = "#ff8080"

# Compound tokens that travel together (background + text + border).
WARN_BG, WARN_TEXT, WARN_BORDER = "#4a3a14", "#ffd98a", "#8a6b1a"

# Shared thin bar look for run-progress and utilisation meters, so every bar in
# the app reads the same. Height in px; bar_qss colors the fill chunk.
BAR_HEIGHT = 8

# Geometry, in px. Two radii (cards, controls) keep every corner on the same
# rhythm; anything spelling its own 3px or 5px has drifted off it.
RADIUS = 6
RADIUS_SM = 4

# Spacing scale. Every margin and gap in the app comes from here, so vertical
# rhythm is a choice of step rather than a fresh number per call site. PAGE_MARGIN
# and GUTTER name the two steps used most, and stay as names because they say
# what they are for.
SPACE_XS = 4
SPACE_SM = 8
SPACE_MD = 12
SPACE_LG = 16
SPACE_XL = 24
SPACE_XXL = 32
PAGE_MARGIN = SPACE_MD  # padding between a page's content and the window edge
GUTTER = SPACE_MD  # gap between two panes, cards, or rows of controls

# Below this a splitter has not been laid out yet and its width is a placeholder,
# so a share of the page is computed from its pane sizes instead.
SPLIT_MIN_TOTAL = 400

# Smallest comfortable click target. Chips, inline cell buttons and icon buttons
# all sit on this floor: below it they are fiddly with a trackpad, which is how
# this app is driven in the field.
CONTROL_HEIGHT = 28

# Row padding in the item views, apart from the spacing scale above. A row is
# padded against the rows either side of it rather than against a panel edge, so
# it wants a tighter step than SPACE_XS: rows are the densest surface the app
# draws, and Browse lists a whole field season of them, where every pixel of
# padding costs one less run on screen. A row still clears CONTROL_HEIGHT once
# the text is inside it, so these do not make a row hard to hit.
ROW_PAD_V = 2  # table rows, which carry text and nothing else
TREE_ROW_PAD_V = 3  # tree rows, which also carry a link-state icon
ROW_PAD_H = 8
HEADER_PAD_V = 4  # column headers, a shade looser so they read as a header

# A table row's height, set on the vertical header rather than left to the QSS
# above. QTableView takes its row height from defaultSectionSize and ignores the
# item padding entirely, so tightening only the stylesheet changed the look of a
# row without fitting one more of them on the screen.
TABLE_ROW_HEIGHT = 26

# A readable measure for a page of prose and short rows. Stretched to fill a
# 1500px window such a page is a card with a hole in it, and the eye has to track
# across the whole window to get from a sentence to the button that acts on it.
READING_WIDTH = 900

# Type scale, in points rather than pixels so it follows the user's font-size
# preference the way the base font does. The strings are for QSS; the numbers for
# QFont.setPointSize. FONT_MD matches core.fonts.BASE_POINT_SIZE.
FONT_XS_PT, FONT_SM_PT, FONT_MD_PT, FONT_LG_PT, FONT_XL_PT = 8, 9, 10, 12, 14
FONT_XS = f"{FONT_XS_PT}pt"
FONT_SM = f"{FONT_SM_PT}pt"
FONT_MD = f"{FONT_MD_PT}pt"
FONT_LG = f"{FONT_LG_PT}pt"
FONT_XL = f"{FONT_XL_PT}pt"

# Two weights above body. Semibold carries section titles and labels; bold is for
# the one thing on a screen that has to be read first.
WEIGHT_SEMIBOLD = 600
WEIGHT_BOLD = 700


# How long the pointer rests before the first tooltip appears, in ms.
#
# Fusion waits 700, which is tuned for a tooltip that explains a control you are
# already looking at. In the run table the tooltip *is* the detail view: a reader
# hunting down a column opens one per row, and 700ms each makes that hunt feel
# stuck. Platforms cluster around 500 (Windows, and Material's desktop guidance),
# and usability guidance puts the floor near 300 -- below roughly 200 the tooltip
# flickers up while the pointer is only crossing a row on its way elsewhere.
#
# 400 sits inside that band, nearer the responsive end because the content here
# is worth reading rather than a label restating a button.
TOOLTIP_DELAY_MS = 400

# The drive card is a panel, not a label: it covers the row above the drive
# button and takes a moment to read, so crossing the foot of the window on the
# way to something else must not raise it. Longer than a tooltip for that
# reason, and short enough that deliberately resting on a drive still answers.
CARD_HOVER_DELAY_MS = 650

# How often a raised card re-checks that the cursor is still on it or on the
# control that raised it. A frameless tooltip window sitting under the pointer
# can swallow the button's leave event, which leaves the card up with nothing
# to dismiss it.
CARD_HOVER_GUARD_MS = 200


class _AppStyle(QProxyStyle):
    """Fusion, with a tooltip that wakes up sooner. See TOOLTIP_DELAY_MS."""

    def styleHint(self, hint, option=None, widget=None, data=None) -> int:  # noqa: N802
        if hint == QStyle.StyleHint.SH_ToolTip_WakeUpDelay:
            return TOOLTIP_DELAY_MS
        return super().styleHint(hint, option, widget, data)


def _chevron_file(direction: str, color: str, size: int = 16) -> str:
    """Path to a painted chevron, for the QSS rules that need an `image:`.

    Styling a QComboBox or QAbstractSpinBox box model hands arrow drawing to the
    stylesheet engine, which then draws nothing unless given an image, so a
    styled combo loses the one mark that says it opens. Qt stylesheets take only
    a URL, and this project ships no icon resources, so the arrows are painted
    once into the cache dir and referenced from there.
    """
    cache = Path(
        QStandardPaths.writableLocation(QStandardPaths.StandardLocation.CacheLocation)
        or "."
    )
    cache.mkdir(parents=True, exist_ok=True)
    path = cache / f"chevron-{direction}-{color.lstrip('#')}-{size}.png"
    # A scaled screen wants more pixels than the stylesheet asks for. Qt's image
    # loader looks for an `@2x` sibling of any file it is given and uses it when
    # the screen is scaled, so each arrow is painted once per scale.
    for suffix, scale in (("", 1), ("@2x", 2), ("@3x", 3)):
        at = path.with_name(f"{path.stem}{suffix}{path.suffix}")
        if at.exists():
            continue
        pixels = size * scale
        pixmap = QPixmap(pixels, pixels)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(QColor(color), 1.5 * scale)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        mid, arm = pixels / 2, pixels * 0.22
        drop = arm if direction == "down" else -arm
        painter.drawLine(QPointF(mid - arm, mid - drop / 2), QPointF(mid, mid + drop / 2))
        painter.drawLine(QPointF(mid, mid + drop / 2), QPointF(mid + arm, mid - drop / 2))
        painter.end()
        pixmap.save(str(at))
    # Qt stylesheet urls take forward slashes on every platform. Callers quote
    # the result: this lands under the user's cache directory, which on Windows
    # sits below a profile name that routinely contains a space, and an unquoted
    # url() stops parsing there -- taking every rule after it in the block with
    # it, so the combo and spin arrows all disappear.
    return path.as_posix()


def _arrow_qss() -> str:
    """Arrow sub-control rules, built after a QApplication exists so the
    chevrons can be painted."""
    down = _chevron_file("down", TEXT_MUTED)
    up = _chevron_file("up", TEXT_MUTED)
    down_off = _chevron_file("down", DISABLED_FG)
    up_off = _chevron_file("up", DISABLED_FG)
    # Brighter than the TEXT_MUTED header text: the indicator names the one
    # column the rows are ordered by, so it must not blend into the labels.
    sort_up = _chevron_file("up", WINDOW_TEXT)
    sort_down = _chevron_file("down", WINDOW_TEXT)
    return f"""
QComboBox::drop-down {{
    subcontrol-origin: padding;
    subcontrol-position: center right;
    border: none;
    width: 20px;
}}
QComboBox::down-arrow {{ image: url("{down}"); width: 12px; height: 12px; }}
QComboBox::down-arrow:disabled {{ image: url("{down_off}"); }}
QAbstractSpinBox::up-button, QAbstractSpinBox::down-button {{
    subcontrol-origin: border;
    border: none;
    width: 16px;
    background: transparent;
}}
QAbstractSpinBox::up-button {{ subcontrol-position: top right; }}
QAbstractSpinBox::down-button {{ subcontrol-position: bottom right; }}
QAbstractSpinBox::up-arrow {{ image: url("{up}"); width: 10px; height: 10px; }}
QAbstractSpinBox::down-arrow {{ image: url("{down}"); width: 10px; height: 10px; }}
QAbstractSpinBox::up-arrow:disabled, QAbstractSpinBox::up-arrow:off {{
    image: url("{up_off}");
}}
QAbstractSpinBox::down-arrow:disabled, QAbstractSpinBox::down-arrow:off {{
    image: url("{down_off}");
}}
/* Styling ::section hands the whole header to the stylesheet engine, which
   draws no native sort indicator, so a sorted column showed nothing at all. */
QHeaderView::up-arrow {{
    image: url("{sort_up}");
    width: 10px;
    height: 10px;
    subcontrol-origin: padding;
    subcontrol-position: center right;
}}
QHeaderView::down-arrow {{
    image: url("{sort_down}");
    width: 10px;
    height: 10px;
    subcontrol-origin: padding;
    subcontrol-position: center right;
}}
"""


def bar_qss(chunk: str) -> str:
    """Flat GROOVE track with a rounded, colored fill chunk."""
    return (
        f"QProgressBar {{ background:{GROOVE}; border:none; border-radius:3px; }}"
        f" QProgressBar::chunk {{ background:{chunk}; border-radius:3px; }}"
    )

# Layered on top of Fusion + the palette for the few things the palette alone
# doesn't make consistent. Additive: existing per-widget stylesheets still
# cascade over this and win on conflicts.
#
# Note that a per-widget setStyleSheet *replaces* the matching rule here rather
# than merging with it, so an override that only wants to recolour a widget must
# still restate the padding and radius it is displacing.
#
# Checkboxes and radio buttons are deliberately absent: styling them in QSS
# means supplying indicator images, which this project has no pipeline for, so
# Fusion keeps drawing them.
GLOBAL_QSS = f"""
QGroupBox {{
    background-color: {CARD_BG};
    border: 1px solid {BORDER};
    border-radius: {RADIUS}px;
    /* Tall enough for the title to sit clear of the frame; any less and the
       top border is drawn straight through the text. */
    margin-top: 20px;
    /* The title sits above the frame, so the card's own layout margin is all
       the breathing room its first row needs. */
    padding-top: 0px;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 10px;
    padding: 0 4px;
    color: {TEXT_MUTED};
    font-weight: 600;
}}

QPushButton, QToolButton {{
    background-color: {BUTTON};
    color: {WINDOW_TEXT};
    border: 1px solid {BORDER};
    border-radius: {RADIUS_SM}px;
    padding: 5px 12px;
}}
QPushButton:hover, QToolButton:hover {{
    background-color: {SURFACE_HI};
    border-color: {BORDER_STRONG};
}}
QPushButton:pressed, QToolButton:pressed {{
    background-color: {BASE};
}}
QPushButton:disabled, QToolButton:disabled {{
    background-color: {CARD_BG};
    color: {DISABLED_FG};
    border-color: {BORDER};
}}
/* A latched button is a mode that stays on until it is pressed again, so it has
   to look held down rather than like every other button on the row. */
QPushButton:checked, QToolButton:checked {{
    background-color: {SURFACE_HI};
    border-color: {PRIMARY};
    color: {LINK};
    font-weight: 600;
}}

/* Label tone. The colour a label carries is a role, not a per-site decision:
   before these existed the same `color: TEXT_MUTED` string was repeated at 34
   call sites, each one a place a token could drift. Set with
   `label.setProperty("tone", "muted")`, or via the factories in core/widgets.py. */
QLabel[tone="muted"] {{
    color: {TEXT_MUTED};
}}
QLabel[tone="secondary"] {{
    color: {TEXT_SECONDARY};
}}
QLabel[tone="dim"] {{
    color: {TEXT_DIM};
}}
QLabel[tone="warn"] {{
    color: {WARN_TEXT};
}}

/* One filled action per screen: the step's forward move. */
QPushButton[cta="true"] {{
    background-color: {PRIMARY};
    color: {WINDOW};
    border-color: {PRIMARY_DARK};
    font-weight: 600;
}}
QPushButton[cta="true"]:hover {{
    background-color: {LINK};
    border-color: {PRIMARY};
}}
QPushButton[cta="true"]:pressed {{
    background-color: {PRIMARY_DARK};
}}
QPushButton[cta="true"]:disabled {{
    background-color: {GROOVE};
    color: {DISABLED_FG};
    border-color: {BORDER};
}}

/* Secondary actions that should not compete: Back, inline cell buttons. Quieter
   than a default button, but still a button at rest -- with no border at all
   these read as static labels sitting between the real controls. */
QPushButton[quiet="true"], QToolButton[quiet="true"] {{
    background-color: transparent;
    color: {TEXT_SECONDARY};
    border-color: {BORDER};
}}
QPushButton[quiet="true"]:hover, QToolButton[quiet="true"]:hover {{
    background-color: {SURFACE_HI};
    color: {WINDOW_TEXT};
    border-color: {BORDER_STRONG};
}}
QPushButton[quiet="true"]:disabled, QToolButton[quiet="true"]:disabled {{
    background-color: transparent;
    color: {DISABLED_FG};
    border-color: {BORDER};
}}

/* Fixed-size icon buttons have no room for the padding above. */
QPushButton[pad="none"], QToolButton[pad="none"] {{
    padding: 0;
}}

/* A mark you click rather than a button: a row's disclosure chevron. Even the
   quiet border above makes one read as a control of the same standing as the
   row's real actions, which a chevron is not. */
QToolButton[bare="true"] {{
    background-color: transparent;
    border: none;
    padding: 0;
}}
QToolButton[bare="true"]:hover {{
    background-color: transparent;
}}

QLineEdit, QPlainTextEdit, QTextEdit, QComboBox, QSpinBox, QDoubleSpinBox,
QAbstractSpinBox {{
    background-color: {BASE};
    border: 1px solid {BORDER};
    border-radius: {RADIUS_SM}px;
    padding: 4px 8px;
    selection-background-color: {PRIMARY};
}}
QLineEdit:hover, QComboBox:hover, QSpinBox:hover, QDoubleSpinBox:hover,
QAbstractSpinBox:hover {{
    border-color: {BORDER_STRONG};
}}
QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled,
QDoubleSpinBox:disabled, QAbstractSpinBox:disabled {{
    background-color: {CARD_BG};
    color: {DISABLED_FG};
}}

QListWidget, QListView, QTreeWidget, QTreeView, QTableWidget, QTableView {{
    background-color: {BASE};
    alternate-background-color: {ALT_BASE};
    border: 1px solid {BORDER};
    border-radius: {RADIUS}px;
    gridline-color: {BORDER};
}}
QListWidget::item, QListView::item, QTreeWidget::item, QTreeView::item {{
    padding: {TREE_ROW_PAD_V}px {ROW_PAD_H}px;
    border-radius: {RADIUS_SM}px;
}}
QTableWidget::item, QTableView::item {{
    padding: {ROW_PAD_V}px {ROW_PAD_H}px;
}}
QListWidget::item:hover, QListView::item:hover, QTreeWidget::item:hover,
QTreeView::item:hover {{
    background-color: {SURFACE_HI};
}}
QListWidget::item:selected, QListView::item:selected,
QTreeWidget::item:selected, QTreeView::item:selected,
QTableWidget::item:selected, QTableView::item:selected {{
    background-color: {SELECTION_BG};
    color: {WINDOW_TEXT};
}}

QHeaderView::section {{
    background-color: {CARD_BG};
    color: {TEXT_MUTED};
    font-weight: 600;
    border: none;
    border-bottom: 1px solid {BORDER};
    padding: {HEADER_PAD_V}px {ROW_PAD_H}px;
}}
/* Hover brightening only where a click does something: `sortable` is set by
   core/widgets.py::enable_sorting, so a header that cannot sort stays flat. */
QHeaderView[sortable="true"]::section:hover {{
    color: {WINDOW_TEXT};
}}
QTableCornerButton::section {{
    background-color: {CARD_BG};
    border: none;
    border-bottom: 1px solid {BORDER};
}}

QScrollBar:vertical {{
    background: transparent;
    width: 10px;
    margin: 0;
}}
QScrollBar:horizontal {{
    background: transparent;
    height: 10px;
    margin: 0;
}}
QScrollBar::handle:vertical, QScrollBar::handle:horizontal {{
    background: {BORDER_STRONG};
    border-radius: 5px;
    min-height: 24px;
    min-width: 24px;
}}
QScrollBar::handle:hover {{
    background: {TEXT_DIM};
}}
QScrollBar::add-line, QScrollBar::sub-line {{
    height: 0;
    width: 0;
}}
QScrollBar::add-page, QScrollBar::sub-page {{
    background: transparent;
}}

QToolTip {{
    color: {WINDOW_TEXT};
    background-color: {SURFACE_HI};
    border: 1px solid {BORDER_STRONG};
    border-radius: {RADIUS_SM}px;
    padding: 4px 8px;
}}
QTabWidget::pane {{
    background-color: {CARD_BG};
    border: 1px solid {BORDER};
    border-radius: {RADIUS}px;
    top: -1px;
}}
QTabBar::tab {{
    background: transparent;
    color: {TEXT_MUTED};
    padding: 6px 12px;
    border: 1px solid transparent;
    border-bottom: none;
    border-top-left-radius: {RADIUS_SM}px;
    border-top-right-radius: {RADIUS_SM}px;
}}
QTabBar::tab:hover {{
    color: {WINDOW_TEXT};
    background: {SURFACE_HI};
}}
QTabBar::tab:selected {{
    background: {CARD_BG};
    color: {WINDOW_TEXT};
    border-color: {BORDER};
}}
QTabBar::tab:disabled {{
    color: {DISABLED_FG};
}}
QSplitter::handle {{
    background-color: {WINDOW};
}}
QSplitter::handle:hover {{
    background-color: {BORDER};
}}
/* Keyboard focus has to be visible on every focusable thing, not just the text
   inputs: Fusion draws no focus rect of its own once a widget is QSS-styled.
   Each control below states its focused border itself, because a per-widget
   stylesheet elsewhere replaces these rules rather than merging with them. */
QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus, QSpinBox:focus,
QDoubleSpinBox:focus, QComboBox:focus, QAbstractSpinBox:focus {{
    border: 1px solid {PRIMARY};
}}
QPushButton:focus, QToolButton:focus {{
    border: 1px solid {PRIMARY};
    background-color: {SURFACE_HI};
}}
QPushButton[cta="true"]:focus {{
    border: 1px solid {WINDOW_TEXT};
    background-color: {LINK};
}}
QPushButton[quiet="true"]:focus, QToolButton[quiet="true"]:focus {{
    border: 1px solid {PRIMARY};
    color: {WINDOW_TEXT};
}}
QCheckBox:focus, QRadioButton:focus, QGroupBox:focus {{
    color: {LINK};
}}
QListWidget:focus, QListView:focus, QTreeWidget:focus, QTreeView:focus,
QTableWidget:focus, QTableView:focus {{
    border: 1px solid {PRIMARY};
}}
QTabBar::tab:focus {{
    border-color: {PRIMARY};
    color: {WINDOW_TEXT};
}}
"""


def apply_theme(app: QApplication) -> None:
    """Force a consistent dark theme regardless of the host OS appearance."""
    # Otherwise the app inherits the platform palette: on macOS in Light mode the
    # standard widgets render light while the hardcoded-dark stylesheets stay dark.
    try:
        app.setStyle(_AppStyle("Fusion"))
    except Exception:
        logger.warning("Could not set Fusion style; keeping platform style", exc_info=True)

    # Tell the platform we want dark so native chrome follows, notably the macOS
    # window titlebar and native dialogs, which Fusion + the palette don't reach.
    # Needs Qt 6.8+; harmless no-op on older builds.
    try:
        app.styleHints().setColorScheme(Qt.ColorScheme.Dark)
    except (AttributeError, TypeError):
        logger.debug("setColorScheme unavailable (needs Qt 6.8+)", exc_info=True)

    role = QPalette.ColorRole
    group = QPalette.ColorGroup
    pal = QPalette()
    pal.setColor(role.Window, QColor(WINDOW))
    pal.setColor(role.WindowText, QColor(WINDOW_TEXT))
    pal.setColor(role.Base, QColor(BASE))
    pal.setColor(role.AlternateBase, QColor(ALT_BASE))
    pal.setColor(role.ToolTipBase, QColor(GROOVE))
    pal.setColor(role.ToolTipText, QColor(WINDOW_TEXT))
    pal.setColor(role.Text, QColor(WINDOW_TEXT))
    pal.setColor(role.PlaceholderText, QColor(PLACEHOLDER_TEXT))
    pal.setColor(role.Button, QColor(BUTTON))
    pal.setColor(role.ButtonText, QColor(WINDOW_TEXT))
    pal.setColor(role.BrightText, QColor(BRIGHT_TEXT))
    pal.setColor(role.Link, QColor(LINK))
    pal.setColor(role.Highlight, QColor(PRIMARY))
    pal.setColor(role.HighlightedText, QColor(ON_ACCENT))

    pal.setColor(group.Disabled, role.Text, QColor(DISABLED_FG))
    pal.setColor(group.Disabled, role.WindowText, QColor(DISABLED_FG))
    pal.setColor(group.Disabled, role.ButtonText, QColor(DISABLED_FG))
    pal.setColor(group.Disabled, role.Highlight, QColor(GROOVE))
    pal.setColor(group.Disabled, role.HighlightedText, QColor(DISABLED_FG))
    app.setPalette(pal)

    # Arrow rules come last: they need a live QApplication to paint their images.
    app.setStyleSheet(GLOBAL_QSS + _arrow_qss())
