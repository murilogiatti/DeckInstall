"""Lightweight JSON-based internationalization (i18n) with English as primary language."""

from __future__ import annotations

import json
import locale
import os
from pathlib import Path

LOCALES_DIR = Path(__file__).resolve().parent / "locales"
DEFAULT_LANG = "en"

_LANGUAGE_NAMES: dict[str, str] = {
    "en": "English",
    "pt_BR": "Português (Brasil)",
    "es": "Español",
    "fr": "Français",
    "de": "Deutsch",
    "it": "Italiano",
    "ja": "日本語",
    "zh_CN": "简体中文",
}

_en_catalog: dict[str, str] = {}
_active_catalog: dict[str, str] = {}
_current_lang: str = DEFAULT_LANG


def _load_catalog(lang: str) -> dict[str, str]:
    path = LOCALES_DIR / f"{lang}.json"
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return {str(k): str(v) for k, v in data.items()}
    except Exception:
        pass
    return {}


def available_languages() -> list[tuple[str, str]]:
    """Return sorted list of (code, display_name) for available locale files."""
    codes: set[str] = {"en"}
    if LOCALES_DIR.is_dir():
        for f in LOCALES_DIR.glob("*.json"):
            codes.add(f.stem)
    result = [(code, _LANGUAGE_NAMES.get(code, code)) for code in sorted(codes)]
    return result


def detect_system_language() -> str:
    """Detect best matching locale code from environment/system, defaulting to English."""
    raw = os.environ.get("LC_ALL") or os.environ.get("LANG") or ""
    if not raw:
        try:
            raw = locale.getlocale()[0] or ""
        except Exception:
            raw = ""
    norm = raw.split(".", 1)[0].replace("-", "_")
    avail = {code for code, _ in available_languages()}
    if norm in avail:
        return norm
    prefix = norm.split("_", 1)[0].lower()
    if prefix == "pt" and "pt_BR" in avail:
        return "pt_BR"
    if prefix in avail:
        return prefix
    for code in avail:
        if code.split("_", 1)[0].lower() == prefix:
            return code
    return DEFAULT_LANG


def set_language(lang: str) -> str:
    """Set active language ('auto' resolves system locale). Returns resolved language code."""
    global _en_catalog, _active_catalog, _current_lang
    if not _en_catalog:
        _en_catalog = _load_catalog(DEFAULT_LANG)
    resolved = detect_system_language() if lang == "auto" else lang
    catalog = _load_catalog(resolved)
    if not catalog and resolved != DEFAULT_LANG:
        resolved = DEFAULT_LANG
        catalog = _en_catalog
    _current_lang = resolved
    _active_catalog = catalog or _en_catalog
    return _current_lang


def get_language() -> str:
    """Return currently active language code."""
    return _current_lang


def tr(key: str, **kwargs: object) -> str:
    """Translate key using active locale, falling back to English, then key itself."""
    if not _en_catalog:
        set_language(DEFAULT_LANG)
    text = _active_catalog.get(key) or _en_catalog.get(key) or key
    if kwargs:
        try:
            return text.format(**kwargs)
        except Exception:
            return text
    return text
