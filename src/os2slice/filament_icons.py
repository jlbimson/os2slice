"""Colour icons for the filament menus: a nearest coloured-square emoji, plus an exact swatch.

Every browser shows the emoji in the option text. Browsers with customizable selects
(`appearance: base-select`) keep the option's markup, so the page's CSS hides the emoji and
shows the exact-colour swatch instead; older parsers drop the spans and keep the text.

static/panel.js has a port of `colour_emoji` (`colourEmoji`): keep the two in agreement.
"""

from __future__ import annotations

import colorsys
import html
import re

_HEX = re.compile(r"#([0-9a-fA-F]{6})(?:[0-9a-fA-F]{2})?")

RED, ORANGE, YELLOW, GREEN, BLUE, PURPLE, BROWN, BLACK, WHITE = (
    "\U0001f7e5",  # 🟥
    "\U0001f7e7",  # 🟧
    "\U0001f7e8",  # 🟨
    "\U0001f7e9",  # 🟩
    "\U0001f7e6",  # 🟦
    "\U0001f7ea",  # 🟪
    "\U0001f7eb",  # 🟫
    "⬛",  # ⬛
    "⬜",  # ⬜
)


def normalise_colour(colour: object) -> str | None:
    """ "#RRGGBB" (upper case) from "#RRGGBB" or "#RRGGBBAA"; None for anything else."""
    if not isinstance(colour, str):
        return None
    m = _HEX.fullmatch(colour.strip())
    return f"#{m.group(1).upper()}" if m else None


def colour_emoji(colour: object) -> str:
    """The coloured square nearest to a filament colour, or "" when it isn't a colour.

    HSL rules: very dark is black, very light is white, unsaturated is black or white by
    lightness; otherwise by hue, with dark reds-to-yellows (and desaturated oranges, like
    tan) as brown.
    """
    hex_ = normalise_colour(colour)
    if hex_ is None:
        return ""
    r, g, b = (int(hex_[i : i + 2], 16) / 255 for i in (1, 3, 5))
    h, lum, sat = colorsys.rgb_to_hls(r, g, b)
    hue = h * 360
    top, chroma = max(r, g, b), max(r, g, b) - min(r, g, b)
    if lum < 0.15:
        return BLACK
    if lum > 0.9:
        return WHITE
    if chroma < 0.1 or sat < 0.12:
        return WHITE if lum >= 0.6 else BLACK
    if hue < 12 or hue >= 335:
        return RED
    if hue < 70 and (top < 0.65 or (hue < 50 and sat < 0.45 and lum < 0.75)):
        return BROWN
    if hue < 45:
        return ORANGE
    if hue < 70:
        return YELLOW
    if hue < 165:
        return GREEN
    if hue < 255:
        return BLUE
    return PURPLE


def option_content(label: str, colour: object) -> str:
    """An option's escaped inner HTML: emoji, swatch and label, or just the label."""
    hex_ = normalise_colour(colour)
    if hex_ is None:
        return html.escape(label, quote=True)
    return (
        f'<span class="fc-emoji">{colour_emoji(hex_)} </span>'
        f'<span class="fc-swatch" style="background:{html.escape(hex_, quote=True)}"></span>'
        f"{html.escape(label, quote=True)}"
    )


# The closed select shows the chosen option's markup (swatch) with base-select; older
# parsers drop this button and its content.
SELECTED_BUTTON = "<button><selectedcontent></selectedcontent></button>"
