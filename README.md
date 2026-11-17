![DeckInstall Banner](assets/hero_banner.jpg)

# DeckInstall

Non-Steam game installer, Proton prefix migrator, save validator, and official Steam artwork manager for **Steam Deck / SteamOS** (KDE Plasma + Qt6).

**Current version:** `0.13.0`

## Features
- **Unified 4-Tab UI & Smart Initial View**: Streamlined 4-tab layout (`Install / Add Game`, `Games & Library`, `Saves & Backups`, `Maintenance`) with a clean sidebar, automatic landing on the Library tab when games already exist, a 4-column sortable Non-Steam Library (`Name`, `Source`, `Proton / Runner` with `[OptiScaler]` badge, `Executable`), instant source filter pills (`All`, `Steam`, `Standalone`, `External`), and a compact 2-tab **Configure Game** dialog with inline Proton switching.
- **Standalone & Steam Modes**: Install and launch Windows games via **Standalone mode** (`umu-run` + `GE-Proton11+`, with `GE-Proton10` fallback and automatic runtime fallback, no Steam required) or add them directly as transactional Non-Steam Steam shortcuts.
- **Unified Install / Add Flow, Progressive Disclosure & Auto Artwork**: Extracts `.zip`, `.rar`, `.7z`, and `.tar.*` into `~/Games/<Game>` (unwrapping nested single root folders without wiping existing files) or runs Windows `setup.exe` installers via Proton inside the unified **Install / Add Game** tab alongside detected folders, smart drag-and-drop routing, collapsible **Advanced Options ▾**, BoilR-style automatic artwork preview, and automatic transition after extraction.
- **Smart Game Detection & Dynamic Scan Folders**: Scans configurable scan folders (`~/Games`, `~/Downloads`, `~/Syncthing`, SD cards `/run/media/*`, and user-added paths), skipping helper/redistributable binaries to highlight the real game executable while reading validated `steam_appid.txt` (`1..4294967295`) and emulator metadata.
- **Transactional Steam Shortcuts, Prefix Guard & Multi-Proton Manager**: Safely manages `shortcuts.vdf` and `config.vdf` under file locking (`flock`), preserves existing `LaunchOptions`, `icon`, and Proton tool mappings, migrates `compatdata`/grid assets when paths change, enforces a `win32` prefix guard, disables VDF writes with a warning banner if `shortcuts.vdf` is corrupted, and lets you inspect or remove installed Proton/GE-Proton versions.
- **Run `.exe` in Prefix, OptiScaler & Winetricks**: Run arbitrary `.exe` tools (patches, trainers, modders) inside any game's Wine/Proton prefix, inject/roll back OptiScaler DLSS/FSR/XeSS per game, and install Winetricks verbs/DLLs directly.
- **Per-Game Execution Logs**: Captures stdout/stderr to `~/.local/share/DeckInstall/logs/<game>.log` when launching Standalone or External games from DeckInstall and exposes a built-in **View Last Execution Log...** viewer inside **Configure Game**.
- **Saves & Backups, Save Sync & Snapshots**: Dedicated **Saves & Backups** tab with a sortable 4-column table (`Item`, `Type`, `Date`, `Size`) unifying full ZIP backups (`~/Games/saves/backups` and legacy `~/Backups/DeckInstall`) and per-game save/state snapshots with one-click restore and deletion, full Standalone-only `.zip` restore without Steam, folder-based Save Sync, and newer/missing save import from **Hydra**, **Heroic**, **Lutris**, **Bottles**, and **UMU** prefixes.
- **Maintenance Tab & Verified Prefix Cleanup**: Dedicated `ui_maintenance.py` view with parallel `os.scandir` storage health metrics (`dir_size_bytes_many`), external/orphan prefix and `.deckinstall.bak` cleanup with file integrity verification (`SHA-256`, size, mtime, and unconventional `drive_c` save scan) protecting active shortcuts before deletion.
- **Launch Presets & Official Steam Artwork**: One-click toggles for `gamemoderun`, `mangohud`, `PROTON_USE_WINED3D`, `PROTON_NO_ESYNC`, and `PROTON_NO_FSYNC`; Steam Store search and direct AppID lookup (with Flatpak/native warning); and `.deckinstall_art_bak/` backup for one-click **Revert Artwork**.
- **Modular Architecture, AppImage Auto-Updater & i18n**: Separated background worker modules (`ui_workers_library.py`, `ui_workers_maint.py`), built-in AppImage auto-updater via GitHub Releases, **Auto**, **Dark**, and **Light** themes, and JSON-based localization (`src/deckinstall/locales/en.json` and `pt_BR.json`).

## Quick Start (AppImage)
Download `DeckInstall-<version>-x86_64.AppImage` from [GitHub Releases](https://github.com/murilogiatti/DeckInstall/releases):

```bash
chmod +x DeckInstall-0.13.0-x86_64.AppImage
./DeckInstall-0.13.0-x86_64.AppImage --version
QT_QPA_PLATFORM=offscreen ./DeckInstall-0.13.0-x86_64.AppImage --smoke-test
./DeckInstall-0.13.0-x86_64.AppImage
```

## Translating DeckInstall
English (`en`) is the primary language. To add a new translation:
1. Copy [`src/deckinstall/locales/en.json`](src/deckinstall/locales/en.json) to `src/deckinstall/locales/<locale>.json` (e.g. `es.json`, `fr.json`, `de.json`, `ja.json`).
2. Translate the values while keeping all `{placeholders}` intact.
3. DeckInstall automatically detects new `*.json` files in `locales/` and lists them in the **Language** selector.

## Local Development
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
python main.py
```

Run unit tests (strict `ResourceWarning` + headless Qt):
```bash
QT_QPA_PLATFORM=offscreen PYTHONPATH=src python3 -W error::ResourceWarning -m pytest -q -W error::ResourceWarning
ruff check .
mypy src
```

## Build AppImage (GitHub Actions)
Push a semantic tag matching `VERSION` (e.g. `v0.13.0`) to automatically run the complete test suite on Python 3.10, 3.11, and 3.13, check lint and types, compile a standalone binary with Nuitka, smoke-test the packaged `AppDir`, build `DeckInstall-<version>-x86_64.AppImage`, and attach it plus `SHA256SUMS` to the GitHub Release.

## License
Distributed under the [MIT License](LICENSE).
