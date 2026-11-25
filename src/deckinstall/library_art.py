"""SteamGridDB artwork for games already in the Library.

Steam shortcuts get every missing grid image (capsule, header, hero, logo) in Steam's grid
folder, the same files Steam shows. Games without a shortcut (Autonomous mode, other launchers)
get a capsule in DeckInstall's own covers folder, shown by the Library cover grid.
Without a SteamGridDB key nothing here touches the network.
"""

from __future__ import annotations

import logging
import zlib
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

import requests

from . import steamgriddb
from .remove_all import data_dir

CAPSULE_ONLY = ("capsule",)


def covers_dir() -> Path:
    """Covers of games without a Steam shortcut (removed with DeckInstall's data)."""
    return data_dir() / "covers"


def cover_file_id(key: str) -> int:
    """Stable number naming a Library row's cover file, like a shortcut appid names Steam's."""
    return zlib.crc32(key.encode("utf-8"))


@dataclass(frozen=True)
class ArtJob:
    name: str
    grid_dir: Path
    file_id: int
    steam_appid: int | None
    kinds: tuple[str, ...] | None  # None: every kind (Steam shortcuts)


def job_for_row(
    kind: str, obj: object, key: str, name: str, grid_dir: Path | None, managed: object | None = None
) -> ArtJob | None:
    """The artwork job for one Library row; managed: the DeckInstall game behind a shortcut."""
    steam_appid = getattr(managed, "steam_appid", None) or getattr(obj, "steam_appid", None)
    steam_appid = steam_appid if isinstance(steam_appid, int) and steam_appid > 0 else None
    if kind == "steam":
        appid_u32 = getattr(obj, "appid_u32", None)
        if grid_dir is None or not isinstance(appid_u32, int):
            return None
        return ArtJob(name, grid_dir, appid_u32, steam_appid, None)
    if kind in ("standalone", "external"):
        return ArtJob(name, covers_dir(), cover_file_id(key), steam_appid, CAPSULE_ONLY)
    return None


def _remove_existing(job: ArtJob) -> None:
    """Make room for new art: back up Steam's files first, drop DeckInstall's own covers."""
    from .artwork import _grid_files_for_appid, backup_steam_artwork

    if job.kinds is None:
        backup_steam_artwork(job.file_id, job.grid_dir)
    for path in _grid_files_for_appid(job.file_id, job.grid_dir):
        stem = path.name[len(str(job.file_id)):].split(".", 1)[0]
        if job.kinds is None or stem == "p":
            path.unlink(missing_ok=True)


def fetch_artwork(
    jobs: Iterable[ArtJob],
    *,
    replace: bool = False,
    cancelled: Callable[[], bool] | None = None,
    key: str | None = None,
) -> tuple[int, int, list[str]]:
    """Fill the missing art of each job (replace: swap what is there, after a backup).

    Returns (games that got art, image files saved, games SteamGridDB had nothing for).
    Raises requests.HTTPError when the key is refused.
    """
    key = steamgriddb.api_key() if key is None else key
    games = files = 0
    nothing: list[str] = []
    for job in jobs:
        if cancelled is not None and cancelled():
            break
        if replace:
            try:
                _remove_existing(job)
            except OSError as exc:
                logging.warning("Could not set aside the art of %s: %s", job.name, exc)
                continue
        try:
            saved = steamgriddb.fill_missing_artwork(
                job.file_id, job.grid_dir, name=job.name, steam_appid=job.steam_appid, key=key, kinds=job.kinds
            )
        except requests.HTTPError:
            raise
        except (requests.RequestException, OSError) as exc:
            logging.info("SteamGridDB failed for %s: %s", job.name, exc)
            saved = {}
        if saved:
            games += 1
            files += len(saved)
        elif replace or _missing(job):
            nothing.append(job.name)
    return games, files, nothing


def _missing(job: ArtJob) -> bool:
    kinds = job.kinds or tuple(steamgriddb._KINDS)
    return any(
        not steamgriddb._existing(job.grid_dir, job.file_id, steamgriddb._KINDS[k][2]) for k in kinds
    )


def has_all_art(job: ArtJob) -> bool:
    return not _missing(job)
