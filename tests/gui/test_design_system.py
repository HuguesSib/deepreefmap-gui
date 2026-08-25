"""Rules the design system relies on, asserted so they stop drifting.

The token layer in core/theme.py has always been well built and only partly
adopted; every finding below is one that had already happened at least once.
"""

from __future__ import annotations

import ast
import re
import subprocess
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parents[2] / "deepreefmap_gui"

# Where a raw colour is legitimate: theme.py defines the palette, and icons.py
# paints glyphs whose default ink is a parameter, not a surface.
_COLOUR_EXEMPT = {"core/theme.py"}

_HEX = re.compile(r"#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})\b")

# A brace wrapping a bare identifier is an f-string placeholder that never got
# interpolated. Real QSS braces open a block, so they are followed by
# declarations containing a colon, never by a lone name.
_UNFORMATTED = re.compile(r"\{[A-Za-z_][A-Za-z_0-9]*\}")


def _sources() -> list[Path]:
    return sorted(p for p in PACKAGE.rglob("*.py") if "__pycache__" not in p.parts)


def _rel(path: Path) -> str:
    return path.relative_to(PACKAGE).as_posix()


def _qss_blocks(sheet: str) -> list[tuple[str, str]]:
    """(selector, declarations) pairs. A sheet with no braces is one bare block."""
    if "{" not in sheet:
        return [("", sheet)]
    blocks = []
    rest = sheet
    while "{" in rest:
        selector, rest = rest.split("{", 1)
        declarations, _, rest = rest.partition("}")
        blocks.append((selector.strip(), declarations))
    return blocks


def _is_bare_qt_type(selector: str) -> bool:
    """True for `QWidget`/`QLabel`-style selectors, which match by inheritance."""
    parts = selector.split()
    return not parts or all(p.startswith("Q") and p[1:2].isupper() for p in parts)


def _stylesheet_arguments(path: Path) -> list[tuple[int, str]]:
    """Every literal string reachable from a setStyleSheet call in this file."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr != "setStyleSheet":
            continue
        for arg in node.args:
            found.extend(
                (inner.lineno, inner.value)
                for inner in ast.walk(arg)
                if isinstance(inner, ast.Constant) and isinstance(inner.value, str)
            )
    return found


def test_no_raw_hex_colour_in_a_stylesheet() -> None:
    """Colours come from theme.py, so there is one value per meaning.

    A literal here does not just duplicate a token, it silently opts out of
    every contrast guarantee the token carries.
    """
    offenders = []
    for path in _sources():
        if _rel(path) in _COLOUR_EXEMPT:
            continue
        for lineno, text in _stylesheet_arguments(path):
            offenders.extend(
                f"{_rel(path)}:{lineno} {match.group(0)}" for match in _HEX.finditer(text)
            )
    assert offenders == []


def test_no_stylesheet_passes_an_uninterpolated_placeholder() -> None:
    """Scenario: a stylesheet is built from theme tokens across two lines.

    Expected behaviour: every fragment carries its own `f`. Implicit
    concatenation only makes the first fragment an f-string, so a token in the
    second reaches Qt as the literal text `{TOKEN}`, and Qt drops that whole
    declaration without raising. Four rules had been dead this way, two of them
    for tokens the module never even imported.
    """
    offenders = []
    for path in _sources():
        for lineno, text in _stylesheet_arguments(path):
            offenders.extend(
                f"{_rel(path)}:{lineno} {match.group(0)}" for match in _UNFORMATTED.finditer(text)
            )
    assert offenders == []


def test_a_container_stylesheet_that_draws_a_border_is_object_name_scoped(window) -> None:
    """Scenario: a header bar wants a hairline under itself.

    Expected behaviour: the rule names the bar. Qt applies an unscoped rule to
    every descendant too, so an unscoped `border-bottom` on a container draws a
    stray underline beneath each label and button inside it -- which is exactly
    what the simple-mode header and view bar used to do.

    Checked on the built window rather than on the source, because whether a
    stylesheet cascades depends on whether the widget has children, which only
    the assembled tree knows.
    """
    from PySide6.QtWidgets import QWidget

    offenders = []
    for widget in window.findChildren(QWidget):
        sheet = widget.styleSheet()
        if "border" not in sheet or not widget.findChildren(QWidget):
            continue  # Nothing to leak, or a leaf with nothing to leak into.
        for selector, declarations in _qss_blocks(sheet):
            if "border" not in declarations:
                continue
            # An object name pins the rule to one widget; so does a custom
            # class name, which Qt matches exactly rather than by inheritance.
            # A bare Qt type (QWidget, QLabel) is what cascades into children.
            if "#" in selector or not _is_bare_qt_type(selector):
                continue
            # A widget naming its own type is styling itself; the descendants
            # that inherit it are Qt's internal scrollbars and viewport.
            if selector.split()[-1:] == [type(widget).__name__]:
                continue
            offenders.append(f"{type(widget).__name__}: {selector or '<unscoped>'}")
    assert offenders == []


def test_no_em_dashes_in_prose_or_user_facing_strings() -> None:
    """House style: recast with a comma, colon, parentheses or a full stop.

    A lone em dash standing in for an empty value is the documented exception,
    so a bare "-" string is left alone.

    The en dash is banned alongside it. Only the em dash was ever checked, so the
    en dash spread unnoticed through every time-range label in the app; a range
    is written with a plain hyphen, which also survives tabular figures.

    Every tracked text file, not only the Python: the rule is about the prose,
    and the prose is as much in the build workflow and the packaging comments as
    it is in a docstring.
    """
    repo = PACKAGE.parent
    tracked = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=repo,
        capture_output=True,
        check=True,
    ).stdout.decode()
    offenders = []
    for name in filter(None, tracked.split("\0")):
        path = repo / name
        # This file states the rule, so it necessarily contains the character.
        if path == Path(__file__) or path.suffix in {".png", ".ico", ".icns"}:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for number, line in enumerate(text.splitlines(), 1):
            for dash in ("—", "\u2013"):
                if dash not in line or f'"{dash}"' in line:
                    continue
                offenders.append(f"{name}:{number}")
                break
    assert offenders == []


def test_font_sizes_are_points_rather_than_pixels() -> None:
    """A px size ignores the user's font-size preference; the 10pt base honours it."""
    offenders = []
    for path in _sources():
        for lineno, text in _stylesheet_arguments(path):
            if re.search(r"font-size:\s*\d+px", text):
                offenders.append(f"{_rel(path)}:{lineno}")
    assert offenders == []


def test_every_icon_only_button_carries_an_accessible_name(window) -> None:
    """A tooltip is mouse-only, so it is no label at all to a keyboard or reader."""
    from PySide6.QtWidgets import QAbstractButton

    def named(button) -> bool:
        if button.text().strip() or button.accessibleName().strip():
            return True
        # A button Qt built for a QAction (a QLineEdit trailing action, a menu
        # entry) takes its name from that action.
        return any(action.text().strip() for action in button.actions())

    from PySide6.QtWidgets import QLineEdit

    nameless = [
        button
        for button in window.findChildren(QAbstractButton)
        # An icon and nothing else: a tooltip is the only label it has, and a
        # tooltip is mouse-only. Qt builds its own clear button inside a
        # QLineEdit, which is not ours to name.
        if not button.icon().isNull()
        and not named(button)
        and not isinstance(button.parent(), QLineEdit)
    ]
    assert [b.toolTip() or type(b).__name__ for b in nameless] == []


def test_keyboard_reaches_every_destination(window) -> None:
    """One binding per place you go, so the trackpad is never the only route."""
    shortcuts = {
        action.shortcut().toString()
        for action in window.actions()
        if not action.shortcut().isEmpty()
    }
    assert {"Ctrl+L", "Ctrl+B", "Ctrl+,", "F1"} <= shortcuts


def test_focus_is_visible_on_more_than_text_fields() -> None:
    """Fusion draws no focus rect once a widget is QSS-styled.

    The global sheet used to carry a blanket `outline: none` with a replacement
    only on text inputs, which left a keyboard user with nothing to follow
    through a dialog.
    """
    from deepreefmap_gui.core.theme import GLOBAL_QSS

    assert "*:focus" not in GLOBAL_QSS
    for selector in ("QPushButton:focus", "QToolButton:focus", "QTableWidget:focus"):
        assert selector in GLOBAL_QSS


@pytest.mark.parametrize(
    "token",
    [
        "WINDOW_TEXT",
        "TEXT_SECONDARY",
        "TEXT_MUTED",
        "TEXT_DIM",
        "PLACEHOLDER_TEXT",
        "SUCCESS",
        "WARNING",
        "ERROR",
        "PRIMARY",
        "IDLE",
        "LINK",
        "UPDATE",
        "BLOCK",
        "DIRECTION_FORWARD",
        "DIRECTION_REVERSE",
    ],
)
def test_text_tokens_clear_aa_on_every_surface_they_sit_on(token) -> None:
    """4.5:1 against the shell, an item view and a card.

    Not against BUTTON or SURFACE_HI: those carry WINDOW_TEXT, and a hover fill
    is transient.
    """
    from deepreefmap_gui.core import theme

    colour = getattr(theme, token)
    for surface in (theme.WINDOW, theme.BASE, theme.ALT_BASE, theme.CARD_BG):
        assert contrast(colour, surface) >= 4.5, (token, surface)


def test_status_pills_stay_readable_against_their_own_tint() -> None:
    """The pill is filled from the colour its text is drawn in, so every point
    of alpha is a point of legibility spent."""
    from deepreefmap_gui.core import theme
    from deepreefmap_gui.core.widgets import (
        DIRECTION_COLORS,
        PILL_TINT_ALPHA,
        STATUS_COLORS,
    )

    for name, colour in (*STATUS_COLORS.items(), *DIRECTION_COLORS.items()):
        for row_fill in (theme.BASE, theme.ALT_BASE):
            pill = composite(colour, row_fill, PILL_TINT_ALPHA / 255)
            assert contrast(colour, pill) >= 4.0, (name, row_fill)


def test_the_two_directions_are_told_apart_from_each_other_and_from_a_warning() -> None:
    """A reverse pass sharing WARNING's hue band reads as a caution, in tables
    that paint real cautions."""
    from PySide6.QtGui import QColor

    from deepreefmap_gui.core import theme

    forward = QColor(theme.DIRECTION_FORWARD).hue()
    reverse = QColor(theme.DIRECTION_REVERSE).hue()

    def apart(a: int, b: int) -> int:
        gap = abs(a - b) % 360
        return min(gap, 360 - gap)

    assert apart(forward, reverse) >= 60
    # ERROR is the nearest of the three at 54 degrees, which is why the arrow
    # travels with the colour everywhere and is the whole signal on a selected row.
    for caution in (theme.WARNING, theme.UPDATE, theme.ERROR):
        assert apart(reverse, QColor(caution).hue()) >= 50, caution


def test_the_elevation_ramp_separates_by_lightness() -> None:
    """What a reader sees between two large adjacent dark fills is the lightness
    delta; the WCAG ratio of any two dark greys is near 1:1 and says nothing.

    The ramp used to put 13 points between the shell and a card, which is why
    panels read as text floating on the window rather than as panels.
    """
    from PySide6.QtGui import QColor

    from deepreefmap_gui.core import theme

    ramp = [theme.WINDOW, theme.BASE, theme.CARD_BG, theme.BUTTON, theme.SURFACE_HI]
    steps = [QColor(value).lightness() for value in ramp]
    assert steps == sorted(steps)
    assert QColor(theme.CARD_BG).lightness() - QColor(theme.WINDOW).lightness() >= 20
    assert QColor(theme.BORDER).lightness() - QColor(theme.CARD_BG).lightness() >= 20


# --- colour helpers -------------------------------------------------------


def _channels(value: str) -> tuple[int, int, int]:
    value = value.lstrip("#")
    return tuple(int(value[i : i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def luminance(value: str) -> float:
    def channel(raw: int) -> float:
        c = raw / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (channel(c) for c in _channels(value))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a: str, b: str) -> float:
    la, lb = luminance(a), luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def composite(front: str, back: str, alpha: float) -> str:
    f, b = _channels(front), _channels(back)
    return "#%02x%02x%02x" % tuple(
        round(f[i] * alpha + b[i] * (1 - alpha)) for i in range(3)
    )


def test_every_status_the_interface_shows_has_a_colour_of_its_own() -> None:
    """The map covers the vocabulary exactly, so no status reaches the screen on
    a call site's neutral fallback."""
    from deepreefmap_gui.core.widgets import STATUS_COLORS
    from deepreefmap_gui.survey.statuses import DISPLAY_STATUSES

    assert set(STATUS_COLORS) == set(DISPLAY_STATUSES)


def test_a_run_that_stopped_short_reads_the_same_either_way() -> None:
    """Whether the store recorded the abandonment is not a distinction a diver
    acts on."""
    from deepreefmap_gui.core.widgets import STATUS_COLORS

    assert STATUS_COLORS["incomplete"] == STATUS_COLORS["interrupted"]
