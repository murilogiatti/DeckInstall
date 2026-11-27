"""Valve's and umu's per-game fixes (read, never run) and inline suggestions."""

from __future__ import annotations

import io
import tarfile
import urllib.error
from pathlib import Path

import pytest

from deckinstall import game_fixes
from deckinstall.game_fixes import Fix, apply_fixes, fix_file, umu_fixes, valve_compat_options

PROTON_SCRIPT = '''
import os
def default_compat_config():
    ret = set()
    if "SteamAppId" in os.environ:
        appid = os.environ["SteamAppId"]
        if appid in [
                "1017900", #Age of Empires
                "123",
                ]:
            ret.add("noopwr")
        if appid in ["123"]:
            ret.add("hidenvgpu")
            ret.add("heapdelayfree")
    return ret
def other():
    if appid in ["999"]:
        ret.add("never")
'''


def test_valve_list_is_read_without_running_proton() -> None:
    assert valve_compat_options(PROTON_SCRIPT, "123") == ["noopwr", "hidenvgpu", "heapdelayfree"]
    assert valve_compat_options(PROTON_SCRIPT, 1017900) == ["noopwr"]
    assert valve_compat_options(PROTON_SCRIPT, "999") == []  # only default_compat_config counts
    assert valve_compat_options("this is not python (", "123") == []


def _fixes_root(tmp_path: Path, files: dict[str, str]) -> Path:
    root = tmp_path / "protonfixes"
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return root


def test_umu_fixes_are_translated_not_executed(tmp_path: Path) -> None:
    marker = tmp_path / "ran"
    root = _fixes_root(tmp_path, {
        "gamefixes-steam/1.py": (
            "import os\nfrom protonfixes import util\n"
            f"open({str(marker)!r}, 'w')  # module code: must never run\n"
            "def main() -> None:\n"
            "    '''Fix'''\n"
            "    util.protontricks('d3dcompiler_47')\n"
            "    util.set_environment('PULSE_LATENCY_MSEC', '60')\n"
            "    util.append_argument('-dx11')\n"
            "    util.winedll_override('xinput1_3', util.OverrideOrder.NATIVE_BUILTIN)\n"
            "    util.disable_fsync()\n"
            "    util.set_cpu_topology_limit(4)\n"
            "    install_dir = util.get_game_install_path()\n"
            "    if os.path.exists(install_dir + '/Mods/loader'):\n"
            "        util.winedll_override('dwmapi', util.OverrideOrder.NATIVE)\n"
            "    util.regedit_add('HKCU\\\\Software', 'x')\n"
        ),
        "gamefixes-steam/2.py": "from .shared import main  # shared code\n",
        "gamefixes-gog/umu-55.py": "from protonfixes import util\ndef main_with_id(game_id):\n    util.disable_nvapi()\n",
        "umu-database.csv": "TITLE,STORE,CODENAME,UMU_ID\nGame,gog,1207,umu-55\n",
    })
    game_dir = tmp_path / "game"
    game_dir.mkdir()
    fixes = umu_fixes(fix_file(root, 1), game_dir)
    assert fixes == [
        Fix("winetricks", "d3dcompiler_47", "umu"),
        Fix("env", "PULSE_LATENCY_MSEC=60", "umu"),
        Fix("arg", "-dx11", "umu"),
        Fix("dll", "xinput1_3=n,b", "umu"),
        Fix("env", "PROTON_NO_FSYNC=1", "umu"),
        Fix("env", "PROTON_CPU_TOPOLOGY=4:0,1,2,3", "umu"),
        Fix("advanced", "regedit_add", "umu"),
    ]
    assert not marker.exists()
    (game_dir / "Mods/loader").mkdir(parents=True)  # the condition is checked in the game folder
    assert Fix("dll", "dwmapi=n", "umu") in umu_fixes(fix_file(root, 1), game_dir)
    assert umu_fixes(fix_file(root, 2)) == [Fix("advanced", "shared", "umu")]
    assert umu_fixes(fix_file(root, None, "gog", "1207")) == [Fix("env", "PROTON_DISABLE_NVAPI=1", "umu")]
    assert fix_file(root, 3) is None and umu_fixes(None) == []


def test_real_protonfixes_translate_without_errors() -> None:
    import os
    import pwd

    real_home = Path(pwd.getpwuid(os.getuid()).pw_dir)  # the tests redirect HOME
    root = next((p / "protonfixes" for p in sorted((real_home / ".local/share/Steam/compatibilitytools.d")
                 .glob("GE-Proton*")) if (p / "protonfixes/gamefixes-steam").is_dir()), None)
    if root is None:
        pytest.skip("no GE-Proton with protonfixes here")
    files = list((root / "gamefixes-steam").glob("*.py"))
    usable = sum(1 for f in files if any(x.kind != "advanced" for x in umu_fixes(f)))
    assert len(files) > 100 and usable > len(files) // 2


def _archive(members: dict[str, bytes], symlink: str = "") -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        if symlink:
            info = tarfile.TarInfo(symlink)
            info.type = tarfile.SYMTYPE
            info.linkname = "/etc/passwd"
            tar.addfile(info)
    return buf.getvalue()


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_protonfixes_download_keeps_only_fix_files(tmp_path: Path) -> None:
    data = _archive({
        "umu-protonfixes-master/gamefixes-steam/1.py": b"def main():\n    pass\n",
        "umu-protonfixes-master/umu-database.csv": b"TITLE,STORE,CODENAME,UMU_ID\n",
        "umu-protonfixes-master/../../evil.py": b"x",
        "umu-protonfixes-master/util.py": b"code we never need",
    }, symlink="umu-protonfixes-master/gamefixes-steam/2.py")
    calls: list[object] = []

    def opener(request, timeout):
        calls.append(request)
        return _Response(data)

    cache = tmp_path / "pf"
    assert game_fixes.refresh_protonfixes(cache, opener=opener, now=1000.0) == cache
    kept = sorted(str(p.relative_to(cache)) for p in cache.rglob("*") if p.is_file())
    assert kept == [".fetched", "gamefixes-steam/1.py", "umu-database.csv"]
    assert not (tmp_path / "evil.py").exists()
    assert game_fixes.refresh_protonfixes(cache, opener=opener, now=2000.0) == cache and len(calls) == 1  # fresh

    def offline(request, timeout):
        raise urllib.error.URLError("offline")

    week = game_fixes.PROTONFIXES_MAX_AGE_S
    assert game_fixes.refresh_protonfixes(cache, opener=offline, now=1000.0 + week + 1) == cache  # old copy kept
    assert game_fixes.refresh_protonfixes(tmp_path / "none", opener=offline) is None


def test_fixes_go_into_the_launch_options_in_the_right_order() -> None:
    fixes = [
        Fix("env", "PULSE_LATENCY_MSEC=60", "umu"),
        Fix("arg", "-dx11", "umu"),
        Fix("compat", "noopwr", "valve"),
        Fix("dll", "dwmapi=n,b", "umu"),
        Fix("winetricks", "d3dx9", "umu"),
    ]
    opts, dll = apply_fixes("A=1 STEAM_COMPAT_CONFIG=hidenvgpu gamemoderun %command% -x", "", fixes)
    # Variables before gamemoderun (after it they would be its arguments), Steam's options merged.
    assert opts == "A=1 PULSE_LATENCY_MSEC=60 STEAM_COMPAT_CONFIG=hidenvgpu,noopwr gamemoderun %command% -x -dx11"
    assert dll == "dwmapi=n,b"
    assert apply_fixes("", "", [Fix("arg", "-dx11", "umu")]) == ("%command% -dx11", "")


def test_inline_recommendations_change_only_the_selected_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest import mock

    from PySide6.QtWidgets import QApplication, QPushButton, QVBoxLayout, QWidget

    from deckinstall import ui_recommendations as rec
    from deckinstall.i18n import tr

    app = QApplication.instance() or QApplication([])
    found = rec.Found(
        presets={"prime": True},
        valve=[Fix("compat", "noopwr", "valve")],
        umu=[Fix("arg", "-dx11", "umu"), Fix("winetricks", "d3dx9", "umu")],
        umu_file=True,
    )
    monkeypatch.setattr(rec, "gather", lambda target: found)
    target = rec.Target("Game", 1, None, "steam", "valve", None, "%command% --custom", "", prefix_ready=False)
    parent = QWidget()
    layout = QVBoxLayout(parent)
    win, editor = mock.Mock(), mock.Mock()
    editor.values.return_value = target.opts, ""
    rec.add_game_recommendations(win, parent, layout, editor, target)
    worker = win._track_worker.call_args.args[0]
    assert worker.wait(3000)
    app.processEvents()

    buttons = {b.text(): b for b in parent.findChildren(QPushButton)}
    editor.set_options.assert_not_called()
    assert not buttons[tr("rec.fix_winetricks", value="d3dx9")].isEnabled()
    assert tr("launch.prime_offload") not in buttons
    buttons[tr("rec.fix_compat", value="noopwr")].click()
    editor.set_options.assert_called_once_with("STEAM_COMPAT_CONFIG=noopwr %command% --custom", "")
    parent.deleteLater()
    app.processEvents()


def test_upscaler_upgrades_only_for_upscalers_the_game_ships(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    from deckinstall import optiscaler
    from deckinstall import ui_recommendations as rec
    from deckinstall.hardware import Gpu, HardwareProfile

    rtx = Gpu("nvidia", "nvidia", "b", "RTX 3050", False, True, "rtx")
    deck = Gpu("amd", "amdgpu", "a", "Custom GPU 0405", True, True, "rdna2", 1024)
    exe = tmp_path / "Game.exe"
    exe.write_bytes(b"MZ")
    ships: list[str] = []
    monkeypatch.setattr(optiscaler, "check_optiscaler_compatibility", lambda e, d: SimpleNamespace(upscalers=tuple(ships)))
    monkeypatch.setattr("deckinstall.classify.detect_anticheat", lambda d: [])
    monkeypatch.setattr(rec, "refresh_protonfixes", lambda: None)
    monkeypatch.setattr(rec, "local_protonfixes", lambda dirs: None)

    def run(gpu: Gpu, upscalers: list[str]) -> rec.Found:
        ships[:] = upscalers
        monkeypatch.setattr(rec, "hardware", lambda: HardwareProfile("amd", "", "", (gpu,), "wayland"))
        return rec.gather(rec.Target("Game", 1, tmp_path, "standalone", "ge", None, "", "", True, exe=exe))

    assert run(rtx, []).presets == {}  # no DLSS in the game: nothing to upgrade
    assert run(rtx, ["DLSS"]).presets == {"dlss_upgrade": True}
    assert run(deck, ["FSR"]).presets == {"fsr3_upgrade": True}  # FSR 3.1 runs on the Deck's GPU
    only_dlss = run(deck, ["DLSS"])
    assert only_dlss.presets == {} and only_dlss.suggest_optiscaler  # OptiScaler can give it FSR/XeSS
    assert not run(rtx, ["DLSS"]).suggest_optiscaler
