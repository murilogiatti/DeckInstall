![DeckInstall Banner](assets/hero_banner.jpg)

# DeckInstall

Non-Steam game installer, Proton prefix migrator, save validator, and official Steam artwork manager for **Steam Deck / SteamOS** (KDE Plasma + Qt6).

**Current version:** `0.21.0`

## Features
- **Library Hub & Smart Initial View**: The **Library** is the home page, with large **Install game**, **Backup & sync** and **Maintenance** buttons on top; each opens its page with a **← Library** button to go back; there is no sidebar: Steam status, *Find Steam again*, theme, language and *About* live in **Maintenance**. The app always opens on the Library, with a 4-column sortable Non-Steam Library (`Name`, `Source`, `Proton / Runner` with `[OptiScaler]` badge, `Executable`), instant source filter pills (`All`, `Steam`, `Standalone`, `External`), and a compact 2-tab **Configure Game** dialog with inline Proton switching. The **Play** button works like Steam's: *Loading…* while the game starts, *Stop* while it runs (two games can run together) and an error with the last lines of the log when it fails to start or closes right away; closing DeckInstall never closes a running game.
- **Standalone & Steam Modes**: Install and launch Windows games via **Standalone mode** (`umu-run` + `GE-Proton11+`, with `GE-Proton10` fallback and automatic runtime fallback, no Steam required) or add them directly as transactional Non-Steam Steam shortcuts.
- **Unified Install / Add Flow, Progressive Disclosure & Auto Artwork**: Extracts `.zip`, `.rar`, `.7z`, and `.tar.*` into the selected library (`~/DeckInstall/Games/<Game>` by default; unwrapping nested single root folders without wiping existing files) or runs Windows `setup.exe` installers via Proton inside the unified **Install game** page alongside detected folders, smart drag-and-drop routing, every option visible on one touch-scrollable page, BoilR-style automatic artwork preview, and automatic transition after extraction.
- **Per-Disk Game Libraries & One Prefix per Game**: New installs go to a library on the chosen disk (`~/DeckInstall` internally, `<disk>/DeckInstall` on microSD/USB) with `Games`, `Prefixes` and `Caches` folders and a `library.json`; libraries are found again after a remount, games on an unmounted disk are listed as unavailable, and exFAT/NTFS disks keep the Wine prefix on the internal disk. Each game has a portable manifest (`.deckinstall.json`, library-relative paths) recording everything DeckInstall created, and adding it to Steam links `compatdata/<appid>` to the same prefix instead of copying it. Games from the old `~/Games` layout can be migrated into the library in one step (offered at startup and in Maintenance), with a save snapshot first and a journal so **Undo last migration** puts everything back.
- **Smart Installers (GOG, Repacks, Linux .sh)**: Recognizes archives, GOG offline installers (unpacked with `innoextract` when available), FitGirl/DODI-style repacks, other Windows setups and Linux `.sh` installers. Inno Setup installers get `/DIR=` pointing at the library, repacks run with `WINE_LARGE_ADDRESS_AWARE=0`, a clear unarc.dll error, and a throwaway installer prefix that cannot see `/` or your home folder (only `C:`, the installer folder as `I:` and the library as `D:`). The game executable is picked from the folder the installer created by ranking its Windows 32/64-bit `.exe` files (installers and helpers skipped), and can be switched in one click. OnlineFix/Goldberg/CODEX get the DLL overrides they need, GOG games run with `STORE=gog` and the matching umu `GAMEID`, and archives are no longer capped at 4 GiB (zip-bomb guard by compression ratio, free space checked against the declared size).
- **Proton per Game & Safe OptiScaler**: Each game runs on **Default (newest GE-Proton)** or a pinned version, with **Use previous version** in one click; GE-Proton updates move Default games automatically and keep only the current and previous versions (never one a game or Steam still uses), and **Games' Proton** pins/unpins many games at once. OptiScaler installs are journaled and roll back on error, a half-done install can be undone with Remove, the proxy DLL is chosen to avoid ReShade/OnlineFix clashes, installs are recorded in the game manifest, and **OptiScaler (bulk)** installs or removes it on many games (DeckInstall games, Steam shortcuts and Steam library games) with a compatibility check, closing and reopening Steam only once.
- **Smart Game Detection & Dynamic Scan Folders**: By default searches your home folder (down to each game folder, skipping hidden folders, prefixes, caches and `node_modules`) plus every mounted SD card or disk (`/run/media`, `/media`, `/mnt`, `/var/mnt`, read from the mount table); extra folders can be added and the defaults switched off, removed or restored. Only folders with a Windows 32/64-bit `.exe` that runs with Proton are listed (DOS and other programs are left out). Helper/redistributable binaries are skipped to highlight the real game executable, and validated `steam_appid.txt` (`1..4294967295`) and emulator metadata are read.
- **Transactional Steam Shortcuts, Prefix Guard & Multi-Proton Manager**: Safely manages `shortcuts.vdf` and `config.vdf` under file locking (`flock`), preserves existing `LaunchOptions`, `icon`, and Proton tool mappings, migrates `compatdata`/grid assets when paths change, enforces a `win32` prefix guard, disables VDF writes with a warning banner if `shortcuts.vdf` is corrupted, and lets you inspect or remove installed Proton/GE-Proton versions.
- **Run `.exe` in Prefix, OptiScaler & Winetricks**: Run arbitrary `.exe` tools (patches, trainers, modders) inside any game's Wine/Proton prefix, inject/roll back OptiScaler DLSS/FSR/XeSS per game, and install Winetricks verbs/DLLs directly.
- **Per-Game Execution Logs**: Every launch of a DeckInstall game (from the app, Steam, the menu or the desktop) writes its output from `launch.sh` to `~/.local/share/DeckInstall/logs/<game id>/run.log`, keeping the last 5 runs with start time and exit code; the folder is recorded in the game manifest and each run can be viewed in **Configure Game**.
- **Backup/Sync & Ludusavi**: Automatic save snapshots before uninstall, adopt, move and other risky steps (and after each play session), deduplicated by content and kept to the newest N per game; restore always takes a safety snapshot first. Saves stored under the home folder (native games) are limited to game folders such as `~/.local/share/<game>` and `~/.config/<game>`; shell files, `.ssh`, autostart and executables are always refused. The **Backup & sync** page lists every game with its origin (DeckInstall, Steam shortcut, **Steam store games** with their Proton prefix and local cloud folder, Heroic, Lutris, Bottles, Hydra, UMU and **Xodus Nexus** Game Pass titles) and the current save size, keeps uninstalled games with their snapshots, offers folder-based Save Sync, and imports/exports **Ludusavi** backups (SHA-1 checks, strict path validation; backups of games not installed yet are kept as snapshots to restore later, importing the same backup again creates nothing, and *Remove import duplicates* keeps the oldest copy of identical imports). Nexus restores are refused while Nexus is running, and Steam cloud restores while Steam is running (the app id is checked).
- **Uninstall, Adopt & Move Games**: **Uninstall** follows the game manifest and shows each item (game files, prefix, saves snapshot, Steam link, shortcuts, artwork, OptiScaler, cache, logs) with the space it frees, keeping prefixes shared with other games. **Adopt games...** brings games from **Hydra** (including ones left in `drive_c`), **Heroic**, **Lutris**, **Bottles**, **UMU**, old Steam shortcuts or a loose folder into the library, moving (not copying) game and prefix and undoing everything if a step fails. **Move to another disk** sends a game to a library on microSD/USB.
- **Maintenance & Cleanup Rules**: Cleanup is a set of declarative rules grouped as Games, Runtimes, Other launchers and System (unchecked by default), with file integrity verification (`SHA-256`, size, mtime, `drive_c` save scan) protecting active shortcuts before deletion; the cleanup list is always sorted largest first. In Desktop Mode Steam edits apply right away (Steam closes and reopens); in Gaming Mode they are saved to disk and applied once Steam is closed, even across app restarts and updates. Gaming Mode is detected from the gamescope session (a game wrapped in gamescope on the desktop does not count). Bulk actions (OptiScaler, Proton versions, Proton per game, legacy migration) live in the Library's **In bulk** group.
- **Launch Presets & Official Steam Artwork**: One presets dialog for `gamemoderun`, MangoHud (level, position, FPS limit), DLSS upgrade, NVAPI, NVIDIA PRIME, Wayland, `PROTON_USE_WINED3D` and esync/fsync, with a summary on the install form; Steam Store search and direct AppID lookup (with Flatpak/native warning); with official artwork downloaded when a game is added to Steam.
- **Modular Architecture, AppImage Auto-Updater & i18n**: Separated background worker modules (`ui_workers_library.py`, `ui_workers_maint.py`), built-in AppImage auto-updater via GitHub Releases, **Auto**, **Dark**, and **Light** themes, and JSON-based localization (`src/deckinstall/locales/en.json` and `pt_BR.json`).

## Quick Start (AppImage)
Download `DeckInstall-<version>-x86_64.AppImage` from [GitHub Releases](https://github.com/murilogiatti/DeckInstall/releases):

```bash
chmod +x DeckInstall-0.21.0-x86_64.AppImage
./DeckInstall-0.21.0-x86_64.AppImage --version
QT_QPA_PLATFORM=offscreen ./DeckInstall-0.21.0-x86_64.AppImage --smoke-test
./DeckInstall-0.21.0-x86_64.AppImage
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
Push a semantic tag matching `VERSION` (e.g. `v0.21.0`) to automatically run the complete test suite on Python 3.10, 3.11, and 3.13, check lint and types, compile a standalone binary with Nuitka, smoke-test the packaged `AppDir`, build `DeckInstall-<version>-x86_64.AppImage`, and attach it plus `SHA256SUMS` to the GitHub Release. Pushes and pull requests only run the tests (in parallel), lint and types; a test AppImage can be built from **Actions > Run workflow**.

## License
Distributed under the [MIT License](LICENSE).
