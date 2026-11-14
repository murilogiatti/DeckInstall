"""Archive extraction and Windows setup.exe runner via Proton."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path

from .scanner import clean_game_name
from .steam_paths import find_proton_binary, find_steam_root

ARCHIVE_SUFFIXES = (".zip", ".rar", ".7z", ".tar.gz", ".tgz", ".tar.xz", ".txz", ".tar.zst", ".tar.bz2", ".tar")


def is_archive(path: Path) -> bool:
    """Return True if path has a supported archive extension."""
    low = path.name.lower()
    return any(low.endswith(ext) for ext in ARCHIVE_SUFFIXES)


def suggest_target_name(source: Path) -> str:
    """Derive a clean destination folder name from an archive or installer file."""
    name = source.name
    for ext in ARCHIVE_SUFFIXES:
        if name.lower().endswith(ext):
            name = name[: -len(ext)]
            break
    else:
        name = source.stem
    return clean_game_name(name)


def sanitize_folder_name(name: str) -> str:
    """Ensure folder_name is a single safe directory name without path traversal."""
    cleaned = name.strip().strip("/\\")
    if not cleaned or cleaned in (".", "..") or "/" in cleaned or "\\" in cleaned:
        raise ValueError(f"Nome de pasta inválido: {name!r}")
    return cleaned


def _select_extract_cmd(archive: Path, out_dir: Path) -> list[str]:
    low = archive.name.lower()
    if any(low.endswith(ext) for ext in (".tar.gz", ".tgz", ".tar.xz", ".txz", ".tar.zst", ".tar.bz2", ".tar")) and shutil.which("tar"):
        return ["tar", "-xf", str(archive), "-C", str(out_dir)]
    if low.endswith(".rar") and shutil.which("unrar"):
        return ["unrar", "x", "-o+", "-y", str(archive), str(out_dir) + "/"]
    if low.endswith(".zip") and shutil.which("unzip"):
        return ["unzip", "-o", str(archive), "-d", str(out_dir)]
    if shutil.which("7z"):
        return ["7z", "x", "-y", f"-o{out_dir}", str(archive)]
    raise RuntimeError(f"Nenhuma ferramenta de extração encontrada para: {archive.name}")


def _unwrap_single_folder(temp_dir: Path, final_dir: Path) -> None:
    """Move extracted contents to final_dir, avoiding double nested folders."""
    entries = list(temp_dir.iterdir())
    source_root = (
        entries[0]
        if len(entries) == 1 and entries[0].is_dir() and not entries[0].is_symlink()
        else temp_dir
    )
    final_dir.mkdir(parents=True, exist_ok=True)
    for item in source_root.iterdir():
        dest = final_dir / item.name
        if dest.exists():
            if dest.is_dir():
                shutil.rmtree(dest)
            else:
                dest.unlink()
        shutil.move(str(item), str(dest))


def extract_archive(
    archive: Path,
    dest_root: Path,
    game_folder_name: str | None = None,
    on_progress: Callable[[int, str], None] | None = None,
    cancel_flag: Callable[[], bool] | None = None,
) -> Path:
    """Extract an archive into dest_root/<game_folder_name> and return the target path."""
    folder_name = sanitize_folder_name(game_folder_name or suggest_target_name(archive))
    final_dir = dest_root / folder_name
    dest_root.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="deckinstall_extract_", dir=str(dest_root)) as tmp:
        tmp_path = Path(tmp)
        cmd = _select_extract_cmd(archive, tmp_path)
        if on_progress:
            on_progress(5, f"Extraindo {archive.name}...")

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
        )
        pct = 10
        assert proc.stdout is not None
        for line in proc.stdout:
            if cancel_flag and cancel_flag():
                proc.terminate()
                proc.wait(timeout=5)
                raise RuntimeError("Extração cancelada pelo usuário.")
            m = re.search(r"(\d{1,3})%", line)
            if m:
                pct = min(95, max(pct, int(m.group(1))))
            elif pct < 90:
                pct = min(90, pct + 1)
            if on_progress:
                on_progress(pct, line.strip()[:120] or f"Extraindo {archive.name}...")

        rc = proc.wait()
        if rc != 0:
            raise RuntimeError(f"Falha ao extrair {archive.name} (código {rc}).")

        if on_progress:
            on_progress(96, "Organizando arquivos...")
        _unwrap_single_folder(tmp_path, final_dir)

    if on_progress:
        on_progress(100, f"Concluído em {final_dir}")
    return final_dir


def run_windows_installer(
    installer_exe: Path,
    target_dir: Path,
    on_log: Callable[[str], None] | None = None,
) -> Path:
    """Run a Windows setup.exe using Steam's Proton in a disposable prefix."""
    steam_root = find_steam_root()
    if steam_root is None:
        raise RuntimeError("Steam não encontrado para executar o instalador via Proton.")
    proton = find_proton_binary(steam_root)
    if proton is None:
        raise RuntimeError("Nenhuma versão do Proton encontrada no Steam.")

    target_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="deckinstall_pfx_") as tmp_pfx:
        env = os.environ.copy()
        env["STEAM_COMPAT_CLIENT_INSTALL_PATH"] = str(steam_root)
        env["STEAM_COMPAT_DATA_PATH"] = tmp_pfx
        if on_log:
            on_log(
                f"Iniciando {installer_exe.name} via Proton.\n"
                f"Dica: no instalador, aponte o destino para Z:{target_dir}"
            )
        proc = subprocess.Popen(
            [str(proton), "run", str(installer_exe)],
            cwd=str(installer_exe.parent),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
        )
        if proc.stdout:
            for line in proc.stdout:
                if on_log and line.strip():
                    on_log(line.strip()[:140])
        rc = proc.wait()
        if rc != 0:
            raise RuntimeError(f"Instalador encerrou com código {rc}.")
    return target_dir
