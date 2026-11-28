"""Extracted dialog modules for DeckInstall's MainWindow."""

from __future__ import annotations

from .configure_game_dialog import open_configure_game_dialog, open_scan_folders_dialog
from .launch_presets_dialog import open_launch_presets_dialog
from .prefix_tools_dialogs import (
    open_optiscaler_dialog,
    open_winetricks_dialog,
    run_exe_in_prefix_dialog,
)
from .proton_bulk_dialog import bulk_proton_dialog
from .save_sync_dialog import open_save_sync_dialog

__all__ = [
    "bulk_proton_dialog",
    "open_configure_game_dialog",
    "open_launch_presets_dialog",
    "open_optiscaler_dialog",
    "open_save_sync_dialog",
    "open_scan_folders_dialog",
    "open_winetricks_dialog",
    "run_exe_in_prefix_dialog",
]
