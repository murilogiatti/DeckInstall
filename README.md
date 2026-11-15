![DeckInstall Banner](assets/hero_banner.jpg)

# DeckInstall

Non-Steam game installer, prefix migrator, save validator, and official Steam artwork manager for **Steam Deck / SteamOS** (KDE Plasma + Qt6).

## Features
- **Extract & Install**: Extracts `.zip`, `.rar`, `.7z`, and `.tar.*` directly into `~/Games/<Game>` (automatically unwrapping nested single root folders) or runs `setup.exe` installers via Proton in a temporary prefix.
- **Smart Game Detection**: Scans `~/Games`, `~/Downloads`, `~/Syncthing`, and SD cards (`/run/media/*`), skipping helper/redistributable binaries to highlight the real game executable.
- **Standardized Steam Shortcuts & Proton Stable**: Safely closes Steam when needed, backs up `shortcuts.vdf` and `config.vdf`, writes shortcuts with Proton Stable, and supports transactional rollback.
- **Multi-Launcher Save Import & Verification**: Imports newer/missing saves from **Hydra**, **Heroic**, **Lutris**, **Bottles**, and **UMU** prefixes, verifying exact file integrity (`SHA-256`, file size, and mtime) before marking old prefixes safe to delete.
- **Safe Prefix Cleanup**: Lists external prefixes, `.deckinstall.bak` backups, and orphan `compatdata` folders with inferred game names, protecting any prefix still referenced by an active Steam shortcut.
- **Official Steam Artwork + Backup/Revert**: Searches Steam Store (with Flatpak/native app warning and match picker) and backs up existing grid images to `.deckinstall_art_bak/` before updating, allowing one-click **Revert Artwork**.
- **Themes & Internationalization (i18n)**: Supports **Auto**, **Dark**, and **Light** themes, with **English** as the primary language and JSON-based localization (`src/deckinstall/locales/*.json`).

## Translating DeckInstall
English (`en`) is the primary language. To add a new translation:
1. Copy [`src/deckinstall/locales/en.json`](src/deckinstall/locales/en.json) to `src/deckinstall/locales/<locale>.json` (e.g. `es.json`, `fr.json`, `de.json`, `ja.json`).
2. Translate the values while keeping all `{placeholders}` intact.
3. DeckInstall automatically detects new `*.json` files in `locales/` and lists them in the **Language** selector.

## Local Development
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python main.py
```

Run tests:
```bash
python3 -m unittest discover -s tests -v
```

## Build AppImage (GitHub Actions)
Push a semantic tag (e.g. `v0.7.0`) to automatically test, compile with Nuitka, build `DeckInstall-<version>-x86_64.AppImage`, and attach it to the GitHub Release.

## License
Distributed under the [MIT License](LICENSE).
