"""The filament menus' colour icons (filament_icons.py and its port in static/panel.js)."""

from __future__ import annotations

import json
import shutil
import subprocess
from importlib import resources

import pytest

from os2slice.filament_icons import (
    BLACK,
    BLUE,
    BROWN,
    GREEN,
    ORANGE,
    PURPLE,
    RED,
    WHITE,
    YELLOW,
    colour_emoji,
    normalise_colour,
    option_content,
)

CASES = {
    RED: ["#FF0000", "#C0392B", "#8B0000", "#E74C3C", "#FFC0CB", "#DC143C"],
    ORANGE: ["#FF8400", "#FFA500", "#FF4500", "#F2754E", "#D2691E", "#FFDAB9"],
    YELLOW: ["#FFFF00", "#FFD700", "#F4D03F", "#F1C40F"],
    GREEN: ["#00FF00", "#006400", "#2ECC71", "#00AE42", "#7FFF00"],
    BLUE: ["#0000FF", "#000080", "#00FFFF", "#008080", "#87CEEB", "#0A2472"],
    PURPLE: ["#800080", "#FF00FF", "#8E44AD", "#4B0082"],
    BROWN: ["#8B4513", "#A0522D", "#5C4033", "#D2B48C", "#808000"],
    BLACK: ["#000000", "#1A1A1A", "#404040", "#808080", "#0D0D12"],
    WHITE: ["#FFFFFF", "#F5F5F5", "#C0C0C0", "#FFFDD0", "#FAFAFA"],
}
INVALID = ["", "red", "#FFF", "#GGGGGG", "FF0000", "#FF00001", "#FF0000;x", None, 123]


@pytest.mark.parametrize(
    ("colour", "emoji"), [(c, e) for e, colours in CASES.items() for c in colours]
)
def test_nearest_emoji(colour: str, emoji: str) -> None:
    assert colour_emoji(colour) == emoji


@pytest.mark.parametrize("colour", INVALID)
def test_no_emoji_for_anything_but_a_colour(colour: object) -> None:
    assert colour_emoji(colour) == ""
    assert normalise_colour(colour) is None


def test_normalise_takes_the_first_six_digits() -> None:
    assert normalise_colour("#ff8400") == "#FF8400"
    assert normalise_colour("#FF8400CC") == "#FF8400"
    assert normalise_colour(" #00ae42 ") == "#00AE42"
    assert colour_emoji("#FF0000FF") == RED


def test_option_content_has_emoji_swatch_and_escaped_label() -> None:
    assert option_content("Red <PLA> & co", "#ff0000aa") == (
        f'<span class="fc-emoji">{RED} </span>'
        '<span class="fc-swatch" style="background:#FF0000"></span>'
        "Red &lt;PLA&gt; &amp; co"
    )


@pytest.mark.parametrize("colour", ['#FF0000" onmouseover="x', "url(x)", None, ""])
def test_option_content_without_a_valid_colour_is_just_the_label(colour: object) -> None:
    assert option_content('Preset "PLA"', colour) == "Preset &quot;PLA&quot;"


def test_panel_js_port_agrees() -> None:
    """colourEmoji in panel.js gives the same square as colour_emoji for every case."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("needs Node")
    source = resources.files("os2slice").joinpath("static", "panel.js").read_text("utf-8")
    start = source.index("  const SQUARES = {")
    end = source.index("  // An option's content")
    colours = [c for cs in CASES.values() for c in cs] + [c for c in INVALID if c is not None]
    script = (
        source[start:end] + f"console.log(JSON.stringify({json.dumps(colours)}.map(colourEmoji)));"
    )
    run = subprocess.run(
        [node, "-e", script], capture_output=True, text=True, timeout=30, check=True
    )
    assert json.loads(run.stdout) == [colour_emoji(c) for c in colours]
