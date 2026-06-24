"""M-Claw terminal UI rendering primitives."""

from .theme import ACCENT_COLOR, ACCENT_DIM, ACCENT_LIGHT, BANNER_TEXT_COLOR, TUI_BRAND_TITLE, select_box
from .components import build_panel, command_table, data_table, key_value_table, render_panel, section_title
from .console import print_plain, print_rich

__all__ = [
    "ACCENT_COLOR",
    "ACCENT_DIM",
    "ACCENT_LIGHT",
    "BANNER_TEXT_COLOR",
    "TUI_BRAND_TITLE",
    "build_panel",
    "command_table",
    "data_table",
    "key_value_table",
    "print_plain",
    "print_rich",
    "render_panel",
    "section_title",
    "select_box",
]
