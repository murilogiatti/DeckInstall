"""PySide6 main window for DeckInstall."""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import QSettings, Qt, QThread, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from . import __app_name__, __version__
from .artwork import SteamMatch, download_steam_artwork, search_steam_games
from .extractor import (
    extract_archive,
    is_archive,
    run_windows_installer,
    suggest_target_name,
)
from .scanner import DetectedGame, scan_all_games, scan_game_folder
from .steam_paths import (
    SteamPaths,
    is_gamescope_session,
    is_steam_running,
    resolve_steam_paths,
)
from .steam_vdf import (
    ShortcutEntry,
    add_or_update_shortcut,
    load_shortcuts,
    remove_shortcut,
    shutdown_steam,
    start_steam,
)
from .theme import ThemeMode, apply_theme


class ExtractWorker(QThread):
    """Background worker for archive extraction or Proton setup.exe execution."""

    progress = Signal(int, str)
    finished_ok = Signal(str)
    failed = Signal(str)

    def __init__(self, source: Path, dest_root: Path, folder_name: str) -> None:
        super().__init__()
        self.source = source
        self.dest_root = dest_root
        self.folder_name = folder_name
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def run(self) -> None:
        try:
            if is_archive(self.source):
                out = extract_archive(
                    self.source,
                    self.dest_root,
                    self.folder_name,
                    on_progress=lambda p, msg: self.progress.emit(p, msg),
                    cancel_flag=lambda: self._cancelled,
                )
            elif self.source.suffix.lower() == ".exe":
                self.progress.emit(20, f"Executando {self.source.name} via Proton...")
                out = run_windows_installer(
                    self.source,
                    self.dest_root / self.folder_name,
                    on_log=lambda msg: self.progress.emit(50, msg),
                )
                self.progress.emit(100, f"Instalador finalizado em {out}")
            else:
                raise RuntimeError(f"Formato não suportado: {self.source.name}")
            self.finished_ok.emit(str(out))
        except Exception as exc:
            self.failed.emit(str(exc))


class SteamSearchWorker(QThread):
    """Background worker for Steam Store search."""

    results = Signal(list)
    failed = Signal(str)

    def __init__(self, query: str) -> None:
        super().__init__()
        self.query = query

    def run(self) -> None:
        try:
            self.results.emit(search_steam_games(self.query))
        except Exception as exc:
            self.failed.emit(str(exc))


class AddToSteamWorker(QThread):
    """Background worker to safely shut down Steam, write VDFs, fetch art, and relaunch."""

    status = Signal(str)
    finished_ok = Signal(str)
    failed = Signal(str)

    def __init__(
        self,
        paths: SteamPaths,
        app_name: str,
        exe_path: Path,
        steam_appid: int | None,
        enable_proton: bool,
        restart_steam: bool,
    ) -> None:
        super().__init__()
        self.paths = paths
        self.app_name = app_name
        self.exe_path = exe_path
        self.steam_appid = steam_appid
        self.enable_proton = enable_proton
        self.restart_steam = restart_steam

    def run(self) -> None:
        try:
            was_running = is_steam_running()
            if was_running:
                self.status.emit("Fechando Steam com segurança...")
                if not shutdown_steam():
                    raise RuntimeError("Não foi possível fechar o Steam a tempo.")

            self.status.emit("Gravando atalho e Proton padrão no Steam...")
            shortcut_id = add_or_update_shortcut(
                self.paths,
                app_name=self.app_name,
                exe_path=self.exe_path,
                start_dir=self.exe_path.parent,
                enable_proton=self.enable_proton,
            )

            art_msg = "Sem artes baixadas."
            if self.steam_appid:
                self.status.emit(f"Baixando artes oficiais (AppID {self.steam_appid})...")
                art = download_steam_artwork(
                    steam_appid=self.steam_appid,
                    shortcut_appid_u32=shortcut_id,
                    grid_dir=self.paths.grid_dir,
                )
                art_msg = f"Artes salvas: {', '.join(art.saved_files.keys()) or 'nenhuma'}"

            if was_running and self.restart_steam:
                self.status.emit("Reiniciando Steam...")
                start_steam()

            self.finished_ok.emit(
                f"'{self.app_name}' adicionado (ID {shortcut_id}). {art_msg}"
            )
        except Exception as exc:
            self.failed.emit(str(exc))


class MainWindow(QMainWindow):
    """Main DeckInstall application window."""

    def __init__(self, app: QApplication) -> None:
        super().__init__()
        self.app = app
        self.settings = QSettings(__app_name__, __app_name__)
        self.paths = resolve_steam_paths()
        self._detected_games: list[DetectedGame] = []
        self._steam_matches: list[SteamMatch] = []
        self._extract_worker: ExtractWorker | None = None
        self._search_worker: SteamSearchWorker | None = None
        self._add_worker: AddToSteamWorker | None = None

        self.setWindowTitle(f"{__app_name__} v{__version__}")
        self.resize(860, 620)
        self._build_ui()
        self._apply_saved_theme()
        self._refresh_games()
        self._refresh_shortcuts()

    def _build_ui(self) -> None:
        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(14, 14, 14, 14)
        root.setSpacing(10)

        # Header
        header = QHBoxLayout()
        title_box = QVBoxLayout()
        title = QLabel(f"<b>{__app_name__}</b> <span style='opacity:0.7;'>v{__version__}</span>")
        title.setStyleSheet("font-size: 18px;")
        subtitle = QLabel(
            f"Usuário Steam: {self.paths.user_id}"
            if self.paths
            else "Steam não detectado neste usuário"
        )
        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        header.addLayout(title_box)
        header.addStretch()

        header.addWidget(QLabel("Tema:"))
        self.theme_combo = QComboBox()
        self.theme_combo.addItem("Automático", "auto")
        self.theme_combo.addItem("Escuro", "dark")
        self.theme_combo.addItem("Claro", "light")
        self.theme_combo.currentIndexChanged.connect(self._on_theme_changed)
        header.addWidget(self.theme_combo)
        root.addLayout(header)

        # Tabs
        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_extract_tab(), "1. Extrair / Instalar")
        self.tabs.addTab(self._build_add_tab(), "2. Adicionar ao Steam")
        self.tabs.addTab(self._build_library_tab(), "3. Biblioteca Não-Steam")
        root.addWidget(self.tabs)

        self.status_label = QLabel("Pronto.")
        root.addWidget(self.status_label)
        self.setCentralWidget(central)

    def _build_extract_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        form = QFormLayout()

        src_row = QHBoxLayout()
        self.extract_src = QLineEdit()
        self.extract_src.setPlaceholderText("Arquivo .zip, .rar, .7z, .tar.* ou setup.exe")
        btn_src = QPushButton("Selecionar Arquivo")
        btn_src.clicked.connect(self._pick_archive_or_installer)
        src_row.addWidget(self.extract_src)
        src_row.addWidget(btn_src)
        form.addRow("Arquivo:", src_row)

        self.extract_name = QLineEdit()
        self.extract_name.setPlaceholderText("Nome da pasta em ~/Games")
        form.addRow("Nome do Jogo:", self.extract_name)

        dst_row = QHBoxLayout()
        self.extract_dst_root = QLineEdit(str(Path.home() / "Games"))
        btn_dst = QPushButton("Pasta")
        btn_dst.clicked.connect(self._pick_dest_root)
        dst_row.addWidget(self.extract_dst_root)
        dst_row.addWidget(btn_dst)
        form.addRow("Destino:", dst_row)

        layout.addLayout(form)

        btn_row = QHBoxLayout()
        self.btn_extract = QPushButton("Extrair / Instalar")
        self.btn_extract.setObjectName("primaryBtn")
        self.btn_extract.clicked.connect(self._start_extract)
        self.btn_cancel_extract = QPushButton("Cancelar")
        self.btn_cancel_extract.setEnabled(False)
        self.btn_cancel_extract.clicked.connect(self._cancel_extract)
        self.btn_use_extracted = QPushButton("Ir para Adicionar ao Steam →")
        self.btn_use_extracted.setEnabled(False)
        self.btn_use_extracted.clicked.connect(self._send_extracted_to_add_tab)
        btn_row.addWidget(self.btn_extract)
        btn_row.addWidget(self.btn_cancel_extract)
        btn_row.addStretch()
        btn_row.addWidget(self.btn_use_extracted)
        layout.addLayout(btn_row)

        self.extract_progress = QProgressBar()
        layout.addWidget(self.extract_progress)

        self.extract_log = QTextEdit()
        self.extract_log.setReadOnly(True)
        layout.addWidget(self.extract_log)
        return tab

    def _build_add_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)

        scan_row = QHBoxLayout()
        btn_scan = QPushButton("Reescanear Pastas")
        btn_scan.clicked.connect(self._refresh_games)
        btn_browse_exe = QPushButton("Escolher .exe Manualmente")
        btn_browse_exe.clicked.connect(self._pick_manual_exe)
        scan_row.addWidget(btn_scan)
        scan_row.addWidget(btn_browse_exe)
        scan_row.addStretch()
        layout.addLayout(scan_row)

        self.games_list = QListWidget()
        self.games_list.setMaximumHeight(150)
        self.games_list.currentRowChanged.connect(self._on_game_selected)
        layout.addWidget(self.games_list)

        cfg_box = QGroupBox("Configuração do Atalho Steam")
        form = QFormLayout(cfg_box)

        self.add_name = QLineEdit()
        form.addRow("Nome no Steam:", self.add_name)

        self.add_exe_combo = QComboBox()
        self.add_exe_combo.setEditable(True)
        form.addRow("Executável (.exe):", self.add_exe_combo)

        art_row = QHBoxLayout()
        self.steam_search_input = QLineEdit()
        self.steam_search_input.setPlaceholderText("Nome ou Steam AppID para artes oficiais")
        btn_search = QPushButton("Buscar na Steam")
        btn_search.clicked.connect(self._search_steam_art)
        art_row.addWidget(self.steam_search_input)
        art_row.addWidget(btn_search)
        form.addRow("Buscar Artes:", art_row)

        self.steam_match_combo = QComboBox()
        form.addRow("Jogo na Steam:", self.steam_match_combo)

        self.chk_proton = QCheckBox("Ativar Proton padrão automaticamente (recomendado)")
        self.chk_proton.setChecked(True)
        form.addRow("", self.chk_proton)

        self.chk_restart = QCheckBox("Reabrir o Steam automaticamente após gravar")
        self.chk_restart.setChecked(True)
        form.addRow("", self.chk_restart)

        layout.addWidget(cfg_box)

        self.btn_add_steam = QPushButton("Adicionar ao Steam (com Artes)")
        self.btn_add_steam.setObjectName("primaryBtn")
        self.btn_add_steam.clicked.connect(self._add_selected_to_steam)
        layout.addWidget(self.btn_add_steam)
        layout.addStretch()
        return tab

    def _build_library_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)

        top = QHBoxLayout()
        btn_reload = QPushButton("Atualizar Lista")
        btn_reload.clicked.connect(self._refresh_shortcuts)
        top.addWidget(btn_reload)
        top.addStretch()
        layout.addLayout(top)

        self.shortcuts_list = QListWidget()
        layout.addWidget(self.shortcuts_list)

        actions = QHBoxLayout()
        btn_art = QPushButton("Baixar/Atualizar Artes do Selecionado")
        btn_art.clicked.connect(self._update_selected_shortcut_art)
        btn_remove = QPushButton("Remover do Steam")
        btn_remove.setObjectName("dangerBtn")
        btn_remove.clicked.connect(self._remove_selected_shortcut)
        actions.addWidget(btn_art)
        actions.addStretch()
        actions.addWidget(btn_remove)
        layout.addLayout(actions)
        return tab

    def _apply_saved_theme(self) -> None:
        saved = str(self.settings.value("theme", "auto"))
        idx = self.theme_combo.findData(saved)
        if idx >= 0:
            self.theme_combo.setCurrentIndex(idx)
        mode: ThemeMode = saved if saved in ("auto", "dark", "light") else "auto"
        apply_theme(self.app, mode)

    def _on_theme_changed(self) -> None:
        mode = self.theme_combo.currentData()
        self.settings.setValue("theme", mode)
        apply_theme(self.app, mode)

    def _pick_archive_or_installer(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Selecionar arquivo do jogo",
            str(Path.home() / "Downloads"),
            "Arquivos (*.zip *.rar *.7z *.tar *.tar.gz *.tgz *.tar.xz *.tar.zst *.exe);;Todos (*)",
        )
        if path:
            p = Path(path)
            self.extract_src.setText(str(p))
            if not self.extract_name.text().strip():
                self.extract_name.setText(suggest_target_name(p))

    def _pick_dest_root(self) -> None:
        path = QFileDialog.getExistingDirectory(
            self, "Selecionar pasta de destino", self.extract_dst_root.text()
        )
        if path:
            self.extract_dst_root.setText(path)

    def _start_extract(self) -> None:
        src = Path(self.extract_src.text().strip())
        if not src.is_file():
            QMessageBox.warning(self, __app_name__, "Selecione um arquivo válido.")
            return
        name = self.extract_name.text().strip() or suggest_target_name(src)
        dst = Path(self.extract_dst_root.text().strip() or str(Path.home() / "Games"))

        self.extract_log.clear()
        self.extract_progress.setValue(0)
        self.btn_extract.setEnabled(False)
        self.btn_cancel_extract.setEnabled(True)
        self.btn_use_extracted.setEnabled(False)

        self._extract_worker = ExtractWorker(src, dst, name)
        self._extract_worker.progress.connect(self._on_extract_progress)
        self._extract_worker.finished_ok.connect(self._on_extract_done)
        self._extract_worker.failed.connect(self._on_extract_failed)
        self._extract_worker.start()

    def _cancel_extract(self) -> None:
        if self._extract_worker:
            self._extract_worker.cancel()

    def _on_extract_progress(self, pct: int, msg: str) -> None:
        self.extract_progress.setValue(pct)
        self.extract_log.append(msg)
        self.status_label.setText(msg)

    def _on_extract_done(self, out_dir: str) -> None:
        self.btn_extract.setEnabled(True)
        self.btn_cancel_extract.setEnabled(False)
        self.btn_use_extracted.setEnabled(True)
        self._last_extracted_dir = Path(out_dir)
        self.status_label.setText(f"Concluído: {out_dir}")
        self._refresh_games()

    def _on_extract_failed(self, err: str) -> None:
        self.btn_extract.setEnabled(True)
        self.btn_cancel_extract.setEnabled(False)
        self.extract_log.append(f"ERRO: {err}")
        QMessageBox.critical(self, __app_name__, err)

    def _send_extracted_to_add_tab(self) -> None:
        target = getattr(self, "_last_extracted_dir", None)
        if isinstance(target, Path) and target.is_dir():
            game = scan_game_folder(target)
            if game:
                self._populate_game_form(game)
        self.tabs.setCurrentIndex(1)

    def _refresh_games(self) -> None:
        self._detected_games = scan_all_games()
        self.games_list.clear()
        for g in self._detected_games:
            tag = f" [AppID {g.steam_appid}]" if g.steam_appid else ""
            self.games_list.addItem(f"{g.name}{tag}  —  {g.primary_exe.name}")

    def _on_game_selected(self, row: int) -> None:
        if 0 <= row < len(self._detected_games):
            self._populate_game_form(self._detected_games[row])

    def _populate_game_form(self, game: DetectedGame) -> None:
        self.add_name.setText(game.name)
        self.add_exe_combo.clear()
        for exe in game.executables:
            self.add_exe_combo.addItem(str(exe))
        self.steam_search_input.setText(
            str(game.steam_appid) if game.steam_appid else game.name
        )
        self.steam_match_combo.clear()
        if game.steam_appid:
            self.steam_match_combo.addItem(
                f"AppID detectado: {game.steam_appid}", game.steam_appid
            )

    def _pick_manual_exe(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Selecionar executável do jogo",
            str(Path.home() / "Games"),
            "Executáveis (*.exe);;Todos (*)",
        )
        if path:
            exe = Path(path)
            game = scan_game_folder(exe.parent)
            name = game.name if game else exe.stem
            self.add_name.setText(name)
            self.add_exe_combo.clear()
            self.add_exe_combo.addItem(str(exe))
            self.steam_search_input.setText(
                str(game.steam_appid) if (game and game.steam_appid) else name
            )

    def _search_steam_art(self) -> None:
        query = self.steam_search_input.text().strip()
        if not query:
            return
        if query.isdigit():
            appid = int(query)
            self.steam_match_combo.clear()
            self.steam_match_combo.addItem(f"AppID manual: {appid}", appid)
            self.status_label.setText(f"AppID {appid} selecionado.")
            return

        self.status_label.setText(f"Buscando '{query}' na Steam...")
        self._search_worker = SteamSearchWorker(query)
        self._search_worker.results.connect(self._on_steam_results)
        self._search_worker.failed.connect(
            lambda e: QMessageBox.warning(self, __app_name__, f"Erro na busca: {e}")
        )
        self._search_worker.start()

    def _on_steam_results(self, matches: list[SteamMatch]) -> None:
        self._steam_matches = matches
        self.steam_match_combo.clear()
        for m in matches:
            self.steam_match_combo.addItem(f"{m.name} ({m.appid})", m.appid)
        self.status_label.setText(
            f"{len(matches)} resultado(s) encontrado(s) na Steam."
            if matches
            else "Nenhum jogo encontrado na Steam com esse nome."
        )

    def _add_selected_to_steam(self) -> None:
        if not self.paths:
            QMessageBox.critical(self, __app_name__, "Instalação do Steam não encontrada.")
            return
        app_name = self.add_name.text().strip()
        exe_str = self.add_exe_combo.currentText().strip()
        if not app_name or not exe_str or not Path(exe_str).is_file():
            QMessageBox.warning(self, __app_name__, "Informe um nome e executável válido.")
            return

        if is_gamescope_session() and is_steam_running():
            ans = QMessageBox.question(
                self,
                __app_name__,
                "Você está no Modo Jogo (gamescope). Reiniciar o Steam aqui recarrega a interface.\nDeseja continuar mesmo assim?",
            )
            if ans != QMessageBox.StandardButton.Yes:
                return

        steam_appid = self.steam_match_combo.currentData()
        if steam_appid is None and self.steam_search_input.text().strip().isdigit():
            steam_appid = int(self.steam_search_input.text().strip())

        self.btn_add_steam.setEnabled(False)
        self._add_worker = AddToSteamWorker(
            paths=self.paths,
            app_name=app_name,
            exe_path=Path(exe_str),
            steam_appid=steam_appid,
            enable_proton=self.chk_proton.isChecked(),
            restart_steam=self.chk_restart.isChecked(),
        )
        self._add_worker.status.connect(self.status_label.setText)
        self._add_worker.finished_ok.connect(self._on_add_done)
        self._add_worker.failed.connect(self._on_add_failed)
        self._add_worker.start()

    def _on_add_done(self, msg: str) -> None:
        self.btn_add_steam.setEnabled(True)
        self.status_label.setText(msg)
        self._refresh_shortcuts()
        QMessageBox.information(self, __app_name__, msg)

    def _on_add_failed(self, err: str) -> None:
        self.btn_add_steam.setEnabled(True)
        QMessageBox.critical(self, __app_name__, err)

    def _refresh_shortcuts(self) -> None:
        self.shortcuts_list.clear()
        if not self.paths:
            return
        for entry in load_shortcuts(self.paths.shortcuts_vdf):
            item = QListWidgetItem(f"{entry.app_name}  ({entry.appid_u32})  —  {entry.exe}")
            item.setData(Qt.ItemDataRole.UserRole, entry)
            self.shortcuts_list.addItem(item)

    def _selected_shortcut(self) -> ShortcutEntry | None:
        item = self.shortcuts_list.currentItem()
        if item is None:
            return None
        return item.data(Qt.ItemDataRole.UserRole)

    def _update_selected_shortcut_art(self) -> None:
        entry = self._selected_shortcut()
        if not entry or not self.paths:
            return
        matches = search_steam_games(entry.app_name, limit=1)
        if not matches:
            QMessageBox.information(
                self, __app_name__, f"Não encontrei '{entry.app_name}' na loja Steam."
            )
            return
        res = download_steam_artwork(matches[0].appid, entry.appid_u32, self.paths.grid_dir)
        QMessageBox.information(
            self,
            __app_name__,
            f"Artes de '{matches[0].name}' atualizadas: {', '.join(res.saved_files.keys()) or 'nenhuma'}",
        )

    def _remove_selected_shortcut(self) -> None:
        entry = self._selected_shortcut()
        if not entry or not self.paths:
            return
        ans = QMessageBox.question(
            self,
            __app_name__,
            f"Remover '{entry.app_name}' do Steam? (Os arquivos do jogo não serão apagados.)",
        )
        if ans != QMessageBox.StandardButton.Yes:
            return
        was_running = is_steam_running()
        if was_running:
            shutdown_steam()
        removed = remove_shortcut(self.paths, entry.appid_u32)
        if was_running:
            start_steam()
        if removed:
            self._refresh_shortcuts()
            self.status_label.setText(f"Removido: {entry.app_name}")


def main() -> int:
    """Application entry point."""
    app = QApplication(sys.argv)
    app.setApplicationName(__app_name__)
    app.setApplicationVersion(__version__)
    icon_file = Path(__file__).resolve().parent.parent.parent / "assets" / "icon.png"
    if icon_file.is_file():
        app.setWindowIcon(QIcon(str(icon_file)))
    win = MainWindow(app)
    win.show()
    return app.exec()
