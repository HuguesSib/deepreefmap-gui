"""Theme application and token validity."""

from __future__ import annotations


def test_apply_theme_sets_dark_palette(qapp) -> None:
    # apply_theme mutates the shared app, so snapshot and restore to keep other
    # tests isolated. We assert on the palette (not style().objectName(), which
    # the global stylesheet wraps in an empty-named QStyleSheetStyle proxy).
    from PySide6.QtGui import QPalette

    from deepreefmap_gui.core.theme import apply_theme

    prev_style = qapp.style().objectName()
    prev_palette = QPalette(qapp.palette())
    prev_qss = qapp.styleSheet()
    try:
        apply_theme(qapp)
        win = qapp.palette().color(QPalette.ColorRole.Window)
        button = qapp.palette().color(QPalette.ColorRole.Button)
        assert win.red() < 80 and win.green() < 80 and win.blue() < 80
        # Window is the app shell, the bottom of the elevation ramp, so the
        # controls that sit on top of it are lighter.
        assert win.lightness() < button.lightness()
    finally:
        qapp.setStyleSheet(prev_qss)
        qapp.setPalette(prev_palette)
        if prev_style:
            qapp.setStyle(prev_style)


def test_apply_theme_wakes_tooltips_sooner_than_fusion(qapp) -> None:
    """In the run table the tooltip is the detail view, and Fusion's 700ms wait
    makes hunting down a column feel stuck. See TOOLTIP_DELAY_MS."""
    from PySide6.QtGui import QPalette
    from PySide6.QtWidgets import QStyle

    from deepreefmap_gui.core.theme import TOOLTIP_DELAY_MS, apply_theme

    prev_style = qapp.style().objectName()
    prev_palette = QPalette(qapp.palette())
    prev_qss = qapp.styleSheet()
    try:
        apply_theme(qapp)
        hint = QStyle.StyleHint.SH_ToolTip_WakeUpDelay
        assert qapp.style().styleHint(hint) == TOOLTIP_DELAY_MS
        assert TOOLTIP_DELAY_MS < 700
    finally:
        qapp.setStyleSheet(prev_qss)
        qapp.setPalette(prev_palette)
        if prev_style:
            qapp.setStyle(prev_style)


def test_theme_semantic_constants_are_valid_hex() -> None:
    from PySide6.QtGui import QColor

    from deepreefmap_gui.core import theme

    for name in (
        "SUCCESS",
        "WARNING",
        "ERROR",
        "PRIMARY",
        "LINK",
        "UPDATE",
        "DANGER_BG",
        "DIRECTION_FORWARD",
        "DIRECTION_REVERSE",
    ):
        assert QColor(getattr(theme, name)).isValid()


def test_elevation_ramp_is_ordered() -> None:
    """Each surface is visibly lighter than the one it sits on.

    Panels that fall within a few greys of the shell are what made the app read
    flat, so the ramp is asserted rather than left to drift.
    """
    from PySide6.QtGui import QColor

    from deepreefmap_gui.core import theme

    ramp = [theme.WINDOW, theme.BASE, theme.CARD_BG, theme.BUTTON, theme.SURFACE_HI]
    lightness = [QColor(value).lightness() for value in ramp]
    assert lightness == sorted(lightness)
    assert lightness[-1] - lightness[0] >= 20

    border = QColor(theme.BORDER).lightness()
    assert border > QColor(theme.SURFACE_HI).lightness()


def _rgb(value):
    return tuple(int(value[index:index + 2], 16) / 255 for index in (1, 3, 5))


def _contrast(first, second):
    def luminance(channels):
        linear = [value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4 for value in channels]
        return sum(weight * value for weight, value in zip((0.2126, 0.7152, 0.0722), linear, strict=True))

    low, high = sorted((luminance(first), luminance(second)))
    return (high + 0.05) / (low + 0.05)


def test_charcoal_text_and_tinted_statuses_remain_readable():
    from deepreefmap_gui.core import theme
    from deepreefmap_gui.core.widgets import PILL_TINT_ALPHA

    surface = _rgb(theme.CARD_BG)
    for name in ("WINDOW_TEXT", "TEXT_MUTED", "PRIMARY", "SUCCESS", "ERROR", "WARNING", "IDLE"):
        foreground = _rgb(getattr(theme, name))
        tint = tuple(f * PILL_TINT_ALPHA / 255 + b * (1 - PILL_TINT_ALPHA / 255)
                     for f, b in zip(foreground, surface, strict=True))
        assert _contrast(foreground, surface) >= 4.5, name
        assert _contrast(foreground, tint) >= 4.5, name
    assert _contrast(_rgb(theme.BORDER), _rgb(theme.SURFACE_HI)) >= 3
