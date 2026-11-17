![DeckInstall Banner](assets/hero_banner.jpg)

# DeckInstall

Non-Steam game installer, Proton prefix migrator, save validator, and official Steam artwork manager for **Steam Deck / SteamOS** (KDE Plasma + Qt6).

**Current version:** `0.12.1`

## Features
- **Unified 3-Tab UI (Install / Extract, Games & Library, Maintenance)**: Combines game installation, library management, and per-game configuration into a streamlined 3-tab interface with a unified **Configure Game** dialog.
- **Standalone & Steam Modes**: Install and launch Windows games via **Standalone mode** (`umu-run` + `GE-Proton11+`, with `GE-Proton10` fallback and automatic runtime fallback, no Steam required) or add them directly as transactional Non-Steam Steam shortcuts.
- **Extract, Install & In-Place Defaults**: Extracts `.zip`, `.rar`, `.7z`, and `.tar.*` into `~/Games/<Game>` (automatically unwrapping nested single root folders and merging without wiping existing files), defaults local `.exe` files to in-place installation, or runs Windows `setup.exe` installers via Proton and automatically adopts the resulting `.deckinstall_installer_pfx` prefix into Steam `compatdata`.
- **Smart Game Detection & Dynamic Scan Folders**: Scans configurable scan folders (`~/Games`, `~/Downloads`, `~/Syncthing`, SD cards `/run/media/*`, and user-added paths), skipping helper/redistributable binaries to highlight the real game executable while reading validated `steam_appid.txt` (`1..4294967295`) and emulator metadata.
- **Transactional Steam Shortcuts, Prefix Guard & Multi-Proton Manager**: Safely manages `shortcuts.vdf` and `config.vdf` under file locking (`flock`), preserves existing `LaunchOptions`, `icon`, and Proton tool mappings, migrates `compatdata`/grid assets when paths change, enforces a `win32` prefix guard, disables VDF writes with a warning banner if `shortcuts.vdf` is corrupted, and lets you inspect or remove installed Proton/GE-Proton versions.
- **Run `.exe` in Prefix, OptiScaler & Winetricks**: Run arbitrary `.exe` tools (patches, trainers, modders) inside any game's Wine/Proton prefix, inject/roll back OptiScaler DLSS/FSR/XeSS per game, and install Winetricks verbs/DLLs directly.
- **Bulk Migration, Save Sync & Snapshots**: Moves games from `~/Downloads` into `~/Games` across Steam users with multi-user rollback, imports newer/missing saves from **Hydra**, **Heroic**, **Lutris**, **Bottles**, and **UMU** prefixes (with symlink-escape guards and security/stability hardening), supports folder-based Save Sync, and creates/restores ZIP save snapshots.
- **Maintenance Tab & Verified Prefix Cleanup**: Dedicated Maintenance view with storage health metrics, external/orphan prefix and `.deckinstall.bak` cleanup with file integrity verification (`SHA-256`, size, mtime, and unconventional `drive_c` save scan) protecting active shortcuts before deletion.
- **Launch Presets & Official Steam Artwork**: One-click toggles for `gamemoderun`, `mangohud`, `PROTON_USE_WINED3D`, `PROTON_NO_ESYNC`, and `PROTON_NO_FSYNC`; Steam Store search and direct AppID lookup (with Flatpak/native warning); and `.deckinstall_art_bak/` backup for one-click **Revert Artwork**.
- **AppImage Auto-Updater, Themes & Internationalization (i18n)**: Built-in AppImage auto-updater via GitHub Releases, **Auto**, **Dark**, and **Light** themes, and JSON-based localization (`src/deckinstall/locales/en.json` and `pt_BR.json`).

## Quick Start (AppImage)
Download `DeckInstall-<version>-x86_64.AppImage` from [GitHub Releases](https://github.com/murilogiatti/DeckInstall/releases):

```bash
chmod +x DeckInstall-0.12.1-x86_64.AppImage
./DeckInstall-0.12.1-x86_64.AppImage --version
QT_QPA_PLATFORM=offscreen ./DeckInstall-0.12.1-x86_64.AppImage --smoke-test
./DeckInstall-0.12.1-x86_64.AppImage
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
Push a semantic tag matching `VERSION` (e.g. `v0.12.1`) to automatically run the complete test suite on Python 3.10, 3.11, and 3.13, check lint and types, compile a standalone binary with Nuitka, smoke-test the packaged `AppDir`, build `DeckInstall-<version>-x86_64.AppImage`, and attach it plus `SHA256SUMS` to the GitHub Release.

## License
Distributed under the [MIT License](LICENSE).
