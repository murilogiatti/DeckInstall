"""Modern KDE-style PySide6 interface for DeckInstall."""

from __future__ import annotations

import logging
import shutil
import subprocess
import sys
from pathlib import Path

import requests
from PySide6.QtCore import QSettings, Qt, QThread, Signal, qInstallMessageHandler
from PySide6.QtGui import QCloseEvent, QDragEnterEvent, QDropEvent, QIcon, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QStackedWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from . import __app_name__, __version__
from .artwork import STEAM_CDN_BASE, USER_AGENT, SteamMatch, download_steam_artwork, search_steam_games
from .extractor import (
    extract_archive,
    is_archive,
    run_windows_installer,
    suggest_target_name,
)
from .scanner import DetectedGame, clean_game_name, scan_all_games, scan_game_folder
from .steam_paths import (
    SteamPaths,
    is_gamescope_session,
    is_steam_running,
    resolve_steam_paths,
)
from .steam_vdf import (
    CleanupCandidate,
    MigrationPlanItem,
    ShortcutEntry,
    add_or_update_shortcut,
    delete_cleanup_candidates,
    find_cleanup_candidates,
    import_external_prefix_saves,
    infer_game_root_from_exe,
    load_shortcuts,
    migrate_shortcut_to_games,
    plan_bulk_migration,
    remove_shortcut,
    shutdown_steam,
    start_steam,
    sync_all_external_saves,
)
from .theme import ThemeMode, apply_theme


def _pick_file(parent: QWidget, title: str, start_dir: Path, kdialog_filter: str, qt_filter: str) -> str:
    """Open native KDE kdialog file picker when available, falling back to QFileDialog."""
    if shutil.which("kdialog"):
        res = subprocess.run(
            ["kdialog", "--title", title, "--getopenfilename", str(start_dir), kdialog_filter],
            capture_output=True,
            text=True,
            check=False,
        )
        if res.returncode == 0:
            return res.stdout.strip()
        if res.returncode == 1:
            return ""
    path, _ = QFileDialog.getOpenFileName(parent, title, str(start_dir), qt_filter)
    return path


def _pick_dir(parent: QWidget, title: str, start_dir: Path) -> str:
    """Open native KDE kdialog folder picker when available, falling back to QFileDialog."""
    if shutil.which("kdialog"):
        res = subprocess.run(
            ["kdialog", "--title", title, "--getexistingdirectory", str(start_dir)],
            capture_output=True,
            text=True,
            check=False,
        )
        if res.returncode == 0:
            return res.stdout.strip()
        if res.returncode == 1:
            return ""
    return QFileDialog.getExistingDirectory(parent, title, str(start_dir))


class DropZoneFrame(QFrame):
    """Drag-and-drop card for archive or setup.exe selection."""

    file_dropped = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("DropZone")
        self.setAcceptDrops(True)

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent) -> None:
        urls = event.mimeData().urls()
        if urls:
            local = urls[0].toLocalFile()
            if local:
                self.file_dropped.emit(local)


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
                    cancel_flag=lambda: self._cancelled,
                )
                self.progress.emit(100, f"Instalador finalizado em {out}")
            else:
                raise RuntimeError(f"Formato não suportado: {self.source.name}")
            self.finished_ok.emit(str(out))
        except Exception as exc:
            self.failed.emit(str(exc))


class SteamSearchWorker(QThread):
    """Background worker for Steam Store search."""

    results = Signal(int, list)
    failed = Signal(int, str)

    def __init__(self, req_id: int, query: str) -> None:
        super().__init__()
        self.req_id = req_id
        self.query = query

    def run(self) -> None:
        try:
            self.results.emit(self.req_id, search_steam_games(self.query))
        except Exception as exc:
            self.failed.emit(self.req_id, str(exc))


class PreviewWorker(QThread):
    """Background worker to fetch a Steam header preview image."""

    loaded = Signal(int, bytes)

    def __init__(self, req_id: int, steam_appid: int) -> None:
        super().__init__()
        self.req_id = req_id
        self.steam_appid = steam_appid

    def run(self) -> None:
        url = f"{STEAM_CDN_BASE}/{self.steam_appid}/header.jpg"
        try:
            resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=10)
            if resp.status_code == 200 and len(resp.content) > 512:
                self.loaded.emit(self.req_id, resp.content)
        except requests.RequestException:
            pass


class ScanWorker(QThread):
    """Background worker to scan game folders without blocking the UI thread."""

    finished_scan = Signal(list)

    def run(self) -> None:
        try:
            self.finished_scan.emit(scan_all_games())
        except Exception:
            logging.exception("ScanWorker failed")
            self.finished_scan.emit([])


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

            imported = import_external_prefix_saves(
                paths=self.paths,
                shortcut_appid_u32=shortcut_id,
                game_name=self.app_name,
                steam_appid=self.steam_appid,
            )
            if imported:
                self.status.emit(f"{imported} arquivo(s) de save importados de prefixo externo.")
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


class BulkMigrateWorker(QThread):
    """Background worker to migrate multiple Non-Steam games to ~/Games safely."""

    status = Signal(str)
    finished_ok = Signal(str)
    failed = Signal(str)

    def __init__(self, paths: SteamPaths, items: list[MigrationPlanItem]) -> None:
        super().__init__()
        self.paths = paths
        self.items = items

    def run(self) -> None:
        was_running = is_steam_running()
        migrated: list[str] = []
        errors: list[str] = []
        try:
            if was_running:
                self.status.emit("Fechando Steam com segurança...")
                if not shutdown_steam():
                    raise RuntimeError("Não foi possível fechar o Steam a tempo.")

            games_root = Path.home() / "Games"
            for idx, item in enumerate(self.items, start=1):
                self.status.emit(
                    f"[{idx}/{len(self.items)}] Migrando {item.entry.app_name} → {item.new_name}..."
                )
                try:
                    new_appid, _, steam_appid = migrate_shortcut_to_games(
                        paths=self.paths,
                        entry=item.entry,
                        games_root=games_root,
                        new_name=item.new_name,
                    )
                    if steam_appid is None:
                        matches = search_steam_games(item.new_name, limit=1)
                        if matches:
                            steam_appid = matches[0].appid
                            import_external_prefix_saves(
                                paths=self.paths,
                                shortcut_appid_u32=new_appid,
                                game_name=item.new_name,
                                steam_appid=steam_appid,
                            )
                    if steam_appid:
                        download_steam_artwork(steam_appid, new_appid, self.paths.grid_dir)
                    migrated.append(item.new_name)
                except Exception as exc:
                    errors.append(f"{item.entry.app_name}: {exc}")

            self.status.emit("Sincronizando e validando saves de todos os atalhos...")
            checked, copied = sync_all_external_saves(self._paths)

            summary = (
                f"Migrados ({len(migrated)}): {', '.join(migrated) or 'nenhum'}.\n"
                f"Saves validados em {checked} atalho(s) ({copied} arquivo(s) importado(s)/atualizado(s))."
            )
            if errors:
                summary += "\nErros:\n" + "\n".join(errors)
            self.finished_ok.emit(summary)
        except Exception as exc:
            self.failed.emit(str(exc))
        finally:
            if was_running and not is_steam_running():
                self.status.emit("Reiniciando Steam...")
                start_steam()


def _make_card() -> tuple[QFrame, QVBoxLayout]:
    card = QFrame()
    card.setObjectName("Card")
    layout = QVBoxLayout(card)
    layout.setContentsMargins(16, 14, 16, 14)
    layout.setSpacing(10)
    return card, layout


class MainWindow(QMainWindow):
    """Main DeckInstall application window."""

    def __init__(self, app: QApplication) -> None:
        super().__init__()
        self.app = app
        self.settings = QSettings(__app_name__, __app_name__)
        self.paths = resolve_steam_paths()
        self._detected_games: list[DetectedGame] = []
        self._active_workers: set[QThread] = set()
        self._search_req_id = 0
        self._preview_req_id = 0
        self._extract_worker: ExtractWorker | None = None
        self._search_worker: SteamSearchWorker | None = None
        self._preview_worker: PreviewWorker | None = None
        self._add_worker: AddToSteamWorker | None = None
        self._bulk_worker: BulkMigrateWorker | None = None
        self._scan_worker: ScanWorker | None = None

        self.setWindowTitle(f"{__app_name__} v{__version__}")
        self.resize(980, 660)
        self._build_ui()
        self._apply_saved_theme()
        self._refresh_games()
        self._refresh_shortcuts()

    def _track_worker(self, worker: QThread) -> None:
        """Keep a strong reference to running QThreads until finished to prevent Qt SIGABRT."""
        worker.setParent(self)
        self._active_workers.add(worker)

        def _cleanup() -> None:
            self._active_workers.discard(worker)
            for attr in (
                "_extract_worker",
                "_search_worker",
                "_preview_worker",
                "_add_worker",
                "_bulk_worker",
                "_scan_worker",
            ):
                if getattr(self, attr, None) is worker:
                    setattr(self, attr, None)
            worker.deleteLater()

        worker.finished.connect(_cleanup)

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._extract_worker and self._extract_worker.isRunning():
            self._extract_worker.cancel()
        still_running = False
        for worker in list(self._active_workers):
            if worker.isRunning() and not worker.wait(5000):
                still_running = True
        if still_running:
            self.status_label.setText("Aguardando operação em andamento terminar antes de fechar...")
            event.ignore()
            return
        super().closeEvent(event)

    def _build_ui(self) -> None:
        central = QWidget()
        central.setObjectName("ContentArea")
        shell = QHBoxLayout(central)
        shell.setContentsMargins(0, 0, 0, 0)
        shell.setSpacing(0)

        shell.addWidget(self._build_sidebar())

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(22, 20, 22, 14)
        right_layout.setSpacing(12)

        self.stack = QStackedWidget()
        self.stack.addWidget(self._build_extract_page())
        self.stack.addWidget(self._build_add_page())
        self.stack.addWidget(self._build_library_page())
        right_layout.addWidget(self.stack)

        self.status_label = QLabel("Pronto.")
        self.status_label.setObjectName("MutedText")
        right_layout.addWidget(self.status_label)

        shell.addWidget(right, 1)
        self.setCentralWidget(central)

    def _build_sidebar(self) -> QWidget:
        sidebar = QWidget()
        sidebar.setObjectName("Sidebar")
        sidebar.setFixedWidth(225)
        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(14, 20, 14, 16)
        layout.setSpacing(6)

        brand_row = QHBoxLayout()
        icon_lbl = QLabel()
        icon_path = Path(__file__).resolve().parent.parent.parent / "assets" / "icon.png"
        if icon_path.is_file():
            pix = QPixmap(str(icon_path)).scaled(
                36, 36, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation
            )
            icon_lbl.setPixmap(pix)
        brand_text = QVBoxLayout()
        brand_text.setSpacing(0)
        title = QLabel(f"<b>{__app_name__}</b>")
        title.setStyleSheet("font-size: 16px;")
        ver = QLabel(f"v{__version__}")
        ver.setObjectName("MutedText")
        brand_text.addWidget(title)
        brand_text.addWidget(ver)
        brand_row.addWidget(icon_lbl)
        brand_row.addSpacing(8)
        brand_row.addLayout(brand_text)
        brand_row.addStretch()
        layout.addLayout(brand_row)
        layout.addSpacing(18)

        self._nav_buttons: list[QPushButton] = []
        nav_items = [
            "Extrair / Instalar",
            "Adicionar ao Steam",
            "Biblioteca Não-Steam",
        ]
        for idx, label in enumerate(nav_items):
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.setAutoExclusive(True)
            btn.clicked.connect(lambda _=False, i=idx: self.stack.setCurrentIndex(i))
            layout.addWidget(btn)
            self._nav_buttons.append(btn)
        self._nav_buttons[0].setChecked(True)

        layout.addStretch()

        steam_lbl = QLabel(
            f"Steam ID: {self.paths.user_id}" if self.paths else "Steam não detectado"
        )
        steam_lbl.setObjectName("MutedText")
        layout.addWidget(steam_lbl)

        theme_lbl = QLabel("Tema")
        theme_lbl.setObjectName("MutedText")
        layout.addWidget(theme_lbl)
        self.theme_combo = QComboBox()
        self.theme_combo.addItem("Automático", "auto")
        self.theme_combo.addItem("Escuro", "dark")
        self.theme_combo.addItem("Claro", "light")
        self.theme_combo.currentIndexChanged.connect(self._on_theme_changed)
        layout.addWidget(self.theme_combo)
        return sidebar

    def _build_extract_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(14)

        title = QLabel("Extrair ou Instalar Jogo")
        title.setObjectName("PageTitle")
        sub = QLabel("Arraste um arquivo (.zip, .rar, .7z, .tar.* ou setup.exe) ou escolha pelo seletor nativo do KDE.")
        sub.setObjectName("MutedText")
        sub.setWordWrap(True)
        layout.addWidget(title)
        layout.addWidget(sub)

        drop_card = DropZoneFrame()
        drop_card.file_dropped.connect(self._set_extract_source)
        drop_layout = QVBoxLayout(drop_card)
        drop_layout.setContentsMargins(18, 16, 18, 16)
        drop_layout.setSpacing(10)

        src_row = QHBoxLayout()
        self.extract_src = QLineEdit()
        self.extract_src.setPlaceholderText("Selecione ou arraste o arquivo baixado aqui...")
        btn_src = QPushButton("Procurar Arquivo")
        btn_src.clicked.connect(self._pick_archive_or_installer)
        src_row.addWidget(self.extract_src, 1)
        src_row.addWidget(btn_src)
        drop_layout.addLayout(src_row)

        form = QFormLayout()
        form.setSpacing(10)
        self.extract_name = QLineEdit()
        self.extract_name.setPlaceholderText("Nome da pasta do jogo em ~/Games")
        form.addRow("Nome do Jogo:", self.extract_name)

        dst_row = QHBoxLayout()
        self.extract_dst_root = QLineEdit(str(Path.home() / "Games"))
        btn_dst = QPushButton("Escolher Pasta")
        btn_dst.clicked.connect(self._pick_dest_root)
        dst_row.addWidget(self.extract_dst_root, 1)
        dst_row.addWidget(btn_dst)
        form.addRow("Destino:", dst_row)
        drop_layout.addLayout(form)
        layout.addWidget(drop_card)

        prog_card, prog_layout = _make_card()
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
        prog_layout.addLayout(btn_row)

        self.extract_progress = QProgressBar()
        prog_layout.addWidget(self.extract_progress)

        self.extract_log = QTextEdit()
        self.extract_log.setReadOnly(True)
        prog_layout.addWidget(self.extract_log)
        layout.addWidget(prog_card, 1)
        return page

    def _build_add_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(14)

        title = QLabel("Adicionar Jogo ao Steam")
        title.setObjectName("PageTitle")
        sub = QLabel("Selecione o jogo detectado, confirme as artes oficiais da Steam e adicione com 1 clique.")
        sub.setObjectName("MutedText")
        layout.addWidget(title)
        layout.addWidget(sub)

        list_card, list_layout = _make_card()
        scan_row = QHBoxLayout()
        lbl_det = QLabel("Jogos encontrados")
        lbl_det.setObjectName("SectionTitle")
        btn_scan = QPushButton("Reescanear")
        btn_scan.clicked.connect(self._refresh_games)
        btn_browse_exe = QPushButton("Escolher .exe")
        btn_browse_exe.clicked.connect(self._pick_manual_exe)
        scan_row.addWidget(lbl_det)
        scan_row.addStretch()
        scan_row.addWidget(btn_scan)
        scan_row.addWidget(btn_browse_exe)
        list_layout.addLayout(scan_row)

        self.games_list = QListWidget()
        self.games_list.setMaximumHeight(145)
        self.games_list.currentRowChanged.connect(self._on_game_selected)
        list_layout.addWidget(self.games_list)
        layout.addWidget(list_card)

        cfg_card, cfg_layout = _make_card()
        cfg_split = QHBoxLayout()
        cfg_split.setSpacing(16)

        form = QFormLayout()
        form.setSpacing(10)
        self.add_name = QLineEdit()
        form.addRow("Nome no Steam:", self.add_name)

        self.add_exe_combo = QComboBox()
        self.add_exe_combo.setEditable(True)
        form.addRow("Executável:", self.add_exe_combo)

        art_row = QHBoxLayout()
        self.steam_search_input = QLineEdit()
        self.steam_search_input.setPlaceholderText("Nome ou Steam AppID")
        btn_search = QPushButton("Buscar Artes")
        btn_search.clicked.connect(self._search_steam_art)
        art_row.addWidget(self.steam_search_input, 1)
        art_row.addWidget(btn_search)
        form.addRow("Steam Store:", art_row)

        self.steam_match_combo = QComboBox()
        self.steam_match_combo.currentIndexChanged.connect(self._on_steam_match_changed)
        form.addRow("Correspondência:", self.steam_match_combo)

        self.chk_proton = QCheckBox("Ativar Proton Stable automaticamente")
        self.chk_proton.setChecked(True)
        form.addRow("", self.chk_proton)

        self.chk_restart = QCheckBox("Reabrir o Steam após adicionar")
        self.chk_restart.setChecked(True)
        form.addRow("", self.chk_restart)

        cfg_split.addLayout(form, 1)

        self.preview_label = QLabel("Prévia da Arte\n(Steam Store)")
        self.preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview_label.setFixedSize(230, 108)
        self.preview_label.setStyleSheet("border: 1px solid #344157; border-radius: 8px;")
        cfg_split.addWidget(self.preview_label, 0, Qt.AlignmentFlag.AlignTop)

        cfg_layout.addLayout(cfg_split)

        self.btn_add_steam = QPushButton("Adicionar ao Steam (com Artes)")
        self.btn_add_steam.setObjectName("primaryBtn")
        self.btn_add_steam.clicked.connect(self._add_selected_to_steam)
        cfg_layout.addWidget(self.btn_add_steam)
        layout.addWidget(cfg_card, 1)
        return page

    def _build_library_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(14)

        title = QLabel("Biblioteca Não-Steam")
        title.setObjectName("PageTitle")
        sub = QLabel("Gerencie os atalhos já adicionados ao Steam, atualize artes oficiais ou remova entradas.")
        sub.setObjectName("MutedText")
        layout.addWidget(title)
        layout.addWidget(sub)

        lib_card, lib_layout = _make_card()
        top = QHBoxLayout()
        lbl = QLabel("Atalhos cadastrados")
        lbl.setObjectName("SectionTitle")
        btn_reload = QPushButton("Atualizar Lista")
        btn_reload.clicked.connect(self._refresh_shortcuts)
        top.addWidget(lbl)
        top.addStretch()
        top.addWidget(btn_reload)
        lib_layout.addLayout(top)

        self.shortcuts_list = QListWidget()
        lib_layout.addWidget(self.shortcuts_list, 1)

        actions = QHBoxLayout()
        self.btn_bulk_migrate = QPushButton("Migrar Todos para ~/Games")
        self.btn_bulk_migrate.setObjectName("primaryBtn")
        self.btn_bulk_migrate.clicked.connect(self._bulk_migrate_shortcuts)
        self.btn_migrate = QPushButton("Migrar Selecionado")
        self.btn_migrate.clicked.connect(self._migrate_selected_shortcut)
        self.btn_art = QPushButton("Atualizar Artes")
        self.btn_art.clicked.connect(self._update_selected_shortcut_art)
        self.btn_cleanup = QPushButton("Limpar Prefixos Migrados...")
        self.btn_cleanup.clicked.connect(self._cleanup_old_prefixes)
        self.btn_remove = QPushButton("Remover do Steam")
        self.btn_remove.setObjectName("dangerBtn")
        self.btn_remove.clicked.connect(self._remove_selected_shortcut)
        actions.addWidget(self.btn_bulk_migrate)
        actions.addWidget(self.btn_migrate)
        actions.addWidget(self.btn_art)
        actions.addWidget(self.btn_cleanup)
        actions.addStretch()
        actions.addWidget(self.btn_remove)
        lib_layout.addLayout(actions)
        layout.addWidget(lib_card, 1)
        return page

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

    def _set_extract_source(self, path_str: str) -> None:
        p = Path(path_str)
        if p.is_file():
            self.extract_src.setText(str(p))
            if not self.extract_name.text().strip():
                self.extract_name.setText(suggest_target_name(p))

    def _pick_archive_or_installer(self) -> None:
        path = _pick_file(
            self,
            "Selecionar arquivo do jogo",
            Path.home() / "Downloads",
            "*.zip *.rar *.7z *.tar *.tar.gz *.tgz *.tar.xz *.tar.zst *.exe |Arquivos de Jogo",
            "Arquivos (*.zip *.rar *.7z *.tar *.tar.gz *.tgz *.tar.xz *.tar.zst *.exe);;Todos (*)",
        )
        if path:
            self._set_extract_source(path)

    def _pick_dest_root(self) -> None:
        start = Path(self.extract_dst_root.text().strip() or str(Path.home() / "Games"))
        path = _pick_dir(self, "Selecionar pasta de destino", start)
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
        self._track_worker(self._extract_worker)
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
        self._nav_buttons[1].setChecked(True)
        self.stack.setCurrentIndex(1)

    def _refresh_games(self) -> None:
        worker = ScanWorker()
        self._scan_worker = worker
        self._track_worker(worker)
        worker.finished_scan.connect(
            lambda games, w=worker: self._on_games_scanned(w, games)
        )
        worker.start()

    def _on_games_scanned(self, worker: ScanWorker, games: list[DetectedGame]) -> None:
        if worker is not self._scan_worker:
            return
        self._detected_games = games
        self.games_list.clear()
        for g in self._detected_games:
            tag = f" [AppID {g.steam_appid}]" if g.steam_appid else ""
            self.games_list.addItem(f"{g.name}{tag}  —  {g.primary_exe.name}")

    def _on_game_selected(self, row: int) -> None:
        if 0 <= row < len(self._detected_games):
            self._populate_game_form(self._detected_games[row])

    def _populate_game_form(self, game: DetectedGame) -> None:
        self._search_req_id += 1
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
        path = _pick_file(
            self,
            "Selecionar executável do jogo",
            Path.home() / "Games",
            "*.exe |Executáveis Windows",
            "Executáveis (*.exe);;Todos (*)",
        )
        if path:
            self._search_req_id += 1
            exe = Path(path)
            game = scan_game_folder(exe.parent)
            name = game.name if game else exe.stem
            self.add_name.setText(name)
            self.add_exe_combo.clear()
            self.add_exe_combo.addItem(str(exe))
            self.steam_search_input.setText(
                str(game.steam_appid) if (game and game.steam_appid) else name
            )
            self.steam_match_combo.clear()
            if game and game.steam_appid:
                self.steam_match_combo.addItem(
                    f"AppID detectado: {game.steam_appid}", game.steam_appid
                )
            else:
                self.preview_label.setText("Prévia da Arte\n(Steam Store)")

    def _search_steam_art(self) -> None:
        query = self.steam_search_input.text().strip()
        if not query:
            return
        if query.isdigit():
            self._search_req_id += 1
            appid = int(query)
            self.steam_match_combo.clear()
            self.steam_match_combo.addItem(f"AppID manual: {appid}", appid)
            self.status_label.setText(f"AppID {appid} selecionado.")
            return

        self.status_label.setText(f"Buscando '{query}' na Steam...")
        self._search_req_id += 1
        req_id = self._search_req_id
        worker = SteamSearchWorker(req_id, query)
        self._search_worker = worker
        self._track_worker(worker)
        worker.results.connect(self._on_steam_results)
        worker.failed.connect(self._on_steam_search_failed)
        worker.start()

    def _on_steam_search_failed(self, req_id: int, err: str) -> None:
        if req_id != self._search_req_id:
            return
        QMessageBox.warning(self, __app_name__, f"Erro na busca: {err}")

    def _on_steam_results(self, req_id: int, matches: list[SteamMatch]) -> None:
        if req_id != self._search_req_id:
            return
        self.steam_match_combo.clear()
        for m in matches:
            self.steam_match_combo.addItem(f"{m.name} ({m.appid})", m.appid)
        self.status_label.setText(
            f"{len(matches)} resultado(s) encontrado(s) na Steam."
            if matches
            else "Nenhum jogo encontrado na Steam com esse nome."
        )

    def _on_steam_match_changed(self) -> None:
        appid = self.steam_match_combo.currentData()
        self._preview_req_id += 1
        if not isinstance(appid, int):
            self.preview_label.setText("Prévia da Arte\n(Steam Store)")
            return
        req_id = self._preview_req_id
        self.preview_label.setText("Carregando prévia...")
        worker = PreviewWorker(req_id, appid)
        self._preview_worker = worker
        self._track_worker(worker)
        worker.loaded.connect(self._set_preview_bytes)
        worker.start()

    def _set_preview_bytes(self, req_id: int, raw: bytes) -> None:
        if req_id != self._preview_req_id:
            return
        pix = QPixmap()
        if pix.loadFromData(raw):
            scaled = pix.scaled(
                self.preview_label.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            self.preview_label.setPixmap(scaled)

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

        self._set_steam_busy(True)
        self._add_worker = AddToSteamWorker(
            paths=self.paths,
            app_name=app_name,
            exe_path=Path(exe_str),
            steam_appid=steam_appid,
            enable_proton=self.chk_proton.isChecked(),
            restart_steam=self.chk_restart.isChecked(),
        )
        self._track_worker(self._add_worker)
        self._add_worker.status.connect(self.status_label.setText)
        self._add_worker.finished_ok.connect(self._on_add_done)
        self._add_worker.failed.connect(self._on_add_failed)
        self._add_worker.start()

    def _on_add_done(self, msg: str) -> None:
        self._set_steam_busy(False)
        self.status_label.setText(msg)
        self._refresh_shortcuts()
        QMessageBox.information(self, __app_name__, msg)

    def _on_add_failed(self, err: str) -> None:
        self._set_steam_busy(False)
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

    def _set_steam_busy(self, busy: bool) -> None:
        enabled = not busy
        for btn in (
            self.btn_add_steam,
            self.btn_bulk_migrate,
            self.btn_migrate,
            self.btn_art,
            self.btn_cleanup,
            self.btn_remove,
        ):
            btn.setEnabled(enabled)

    def _update_selected_shortcut_art(self) -> None:
        entry = self._selected_shortcut()
        if not entry or not self.paths:
            return
        self._set_steam_busy(True)
        self.status_label.setText(f"Buscando artes para '{entry.app_name}'...")

        class _ArtUpdateWorker(QThread):
            done = Signal(str)
            failed = Signal(str)

            def __init__(self, paths: SteamPaths, shortcut: ShortcutEntry) -> None:
                super().__init__()
                self._paths = paths
                self._shortcut = shortcut

            def run(self) -> None:
                try:
                    matches = search_steam_games(self._shortcut.app_name, limit=1)
                    if not matches:
                        self.done.emit(f"Não encontrei '{self._shortcut.app_name}' na loja Steam.")
                        return
                    res = download_steam_artwork(
                        matches[0].appid, self._shortcut.appid_u32, self._paths.grid_dir
                    )
                    self.done.emit(
                        f"Artes de '{matches[0].name}' atualizadas: "
                        f"{', '.join(res.saved_files.keys()) or 'nenhuma'}"
                    )
                except Exception as exc:
                    self.failed.emit(str(exc))

        worker = _ArtUpdateWorker(self.paths, entry)
        self._track_worker(worker)

        def _ok(msg: str) -> None:
            self._set_steam_busy(False)
            self.status_label.setText(msg)
            QMessageBox.information(self, __app_name__, msg)

        def _err(err: str) -> None:
            self._set_steam_busy(False)
            QMessageBox.critical(self, __app_name__, f"Erro ao atualizar artes: {err}")

        worker.done.connect(_ok)
        worker.failed.connect(_err)
        worker.start()

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
        self._set_steam_busy(True)
        self.status_label.setText(f"Removendo '{entry.app_name}' do Steam...")

        class _RemoveWorker(QThread):
            done = Signal(bool)
            failed = Signal(str)

            def __init__(self, paths: SteamPaths, shortcut: ShortcutEntry) -> None:
                super().__init__()
                self._paths = paths
                self._shortcut = shortcut

            def run(self) -> None:
                was_running = is_steam_running()
                try:
                    if was_running and not shutdown_steam():
                        raise RuntimeError("Não foi possível fechar o Steam a tempo.")
                    removed = remove_shortcut(self._paths, self._shortcut.appid_u32)
                    self.done.emit(removed)
                except Exception as exc:
                    self.failed.emit(str(exc))
                finally:
                    if was_running and not is_steam_running():
                        start_steam()

        worker = _RemoveWorker(self.paths, entry)
        self._track_worker(worker)

        def _on_removed(removed: bool) -> None:
            self._set_steam_busy(False)
            if removed:
                self._refresh_shortcuts()
                self.status_label.setText(f"Removido: {entry.app_name}")

        def _on_remove_err(err: str) -> None:
            self._set_steam_busy(False)
            QMessageBox.critical(self, __app_name__, err)

        worker.done.connect(_on_removed)
        worker.failed.connect(_on_remove_err)
        worker.start()

    def _migrate_selected_shortcut(self) -> None:
        entry = self._selected_shortcut()
        if not entry or not self.paths:
            return
        old_exe = Path(entry.exe.strip().strip('"'))
        try:
            old_root = infer_game_root_from_exe(old_exe)
        except Exception as exc:
            QMessageBox.critical(self, __app_name__, str(exc))
            return
        suggested = clean_game_name(old_root.name)
        new_name, ok = QInputDialog.getText(
            self,
            __app_name__,
            "Nome padronizado do jogo (pasta em ~/Games e título no Steam):",
            text=suggested,
        )
        if not ok or not new_name.strip():
            return
        if is_gamescope_session() and is_steam_running():
            ans = QMessageBox.question(
                self,
                __app_name__,
                "Você está no Modo Jogo (gamescope). Migrar reiniciará o Steam.\nContinuar?",
            )
            if ans != QMessageBox.StandardButton.Yes:
                return
        item = MigrationPlanItem(
            entry=entry,
            old_root=old_root,
            new_name=new_name.strip(),
            new_root=Path.home() / "Games" / new_name.strip(),
            has_compatdata=(self.paths.compatdata_dir / str(entry.appid_u32)).is_dir(),
        )
        self._set_steam_busy(True)
        self._bulk_worker = BulkMigrateWorker(self.paths, [item])
        self._track_worker(self._bulk_worker)
        self._bulk_worker.status.connect(self.status_label.setText)
        self._bulk_worker.finished_ok.connect(self._on_bulk_done)
        self._bulk_worker.failed.connect(self._on_bulk_failed)
        self._bulk_worker.start()

    def _bulk_migrate_shortcuts(self) -> None:
        if not self.paths:
            return
        self._set_steam_busy(True)
        self.status_label.setText("Analisando jogos, saves externos e Proton Stable...")

        class _BulkPlanWorker(QThread):
            ready = Signal(list)
            failed = Signal(str)

            def __init__(self, paths: SteamPaths) -> None:
                super().__init__()
                self._paths = paths

            def run(self) -> None:
                try:
                    entries = load_shortcuts(self._paths.shortcuts_vdf)
                    plan = plan_bulk_migration(
                        entries=entries,
                        games_root=Path.home() / "Games",
                        compatdata_dir=self._paths.compatdata_dir,
                        config_vdf=self._paths.config_vdf,
                    )
                    self.ready.emit(plan)
                except Exception as exc:
                    self.failed.emit(str(exc))

        worker = _BulkPlanWorker(self.paths)
        self._track_worker(worker)
        worker.ready.connect(self._confirm_and_run_bulk_plan)
        worker.failed.connect(self._on_bulk_failed)
        worker.start()

    def _confirm_and_run_bulk_plan(self, plan: list[MigrationPlanItem]) -> None:
        if not self.paths:
            self._set_steam_busy(False)
            return
        if not plan:
            self.status_label.setText("Atalhos já padronizados. Validando e sincronizando saves externos...")

            class _SaveSyncWorker(QThread):
                done = Signal(int, int)
                failed = Signal(str)

                def __init__(self, paths: SteamPaths) -> None:
                    super().__init__()
                    self._paths = paths

                def run(self) -> None:
                    try:
                        checked, copied = sync_all_external_saves(self._paths)
                        self.done.emit(checked, copied)
                    except Exception as exc:
                        self.failed.emit(str(exc))

            def _on_sync_done(checked: int, copied: int) -> None:
                self._set_steam_busy(False)
                self.status_label.setText("Saves validados e sincronizados.")
                QMessageBox.information(
                    self,
                    __app_name__,
                    f"Todos os jogos .exe já estão padronizados em ~/Games!\n\n"
                    f"Validação concluída em {checked} atalho(s): "
                    f"{copied} arquivo(s) de save importado(s)/atualizado(s).\n"
                    f"Agora você pode usar 'Limpar Prefixos Migrados...' com segurança.",
                )

            sync_worker = _SaveSyncWorker(self.paths)
            self._track_worker(sync_worker)
            sync_worker.done.connect(_on_sync_done)
            sync_worker.failed.connect(self._on_bulk_failed)
            sync_worker.start()
            return

        lines = [
            f"• {p.entry.app_name} → {p.new_name} (saves: {'sim' if p.has_compatdata else 'novo/externo'})"
            for p in plan
        ]
        msg = "Os seguintes jogos serão padronizados em ~/Games com Proton Stable, artes oficiais e importação de saves:\n\n" + "\n".join(lines)
        if is_gamescope_session() and is_steam_running():
            msg += "\n\nAviso: você está no Modo Jogo (gamescope); o Steam será reiniciado."
        if QMessageBox.question(self, __app_name__, msg) != QMessageBox.StandardButton.Yes:
            self._set_steam_busy(False)
            return

        self._bulk_worker = BulkMigrateWorker(self.paths, plan)
        self._track_worker(self._bulk_worker)
        self._bulk_worker.status.connect(self.status_label.setText)
        self._bulk_worker.finished_ok.connect(self._on_bulk_done)
        self._bulk_worker.failed.connect(self._on_bulk_failed)
        self._bulk_worker.start()

    def _on_bulk_done(self, summary: str) -> None:
        self._set_steam_busy(False)
        self.status_label.setText("Migração em massa concluída.")
        self._refresh_games()
        self._refresh_shortcuts()
        QMessageBox.information(self, __app_name__, summary)

    def _on_bulk_failed(self, err: str) -> None:
        self._set_steam_busy(False)
        QMessageBox.critical(self, __app_name__, f"Erro na migração em massa: {err}")

    def _cleanup_old_prefixes(self) -> None:
        if not self.paths:
            return
        self.btn_cleanup.setEnabled(False)
        self.status_label.setText("Buscando prefixos antigos e compatdata órfãos...")

        class _CleanupScanWorker(QThread):
            found = Signal(list)

            def __init__(self, paths: SteamPaths) -> None:
                super().__init__()
                self._paths = paths

            def run(self) -> None:
                self.found.emit(find_cleanup_candidates(self._paths))

        worker = _CleanupScanWorker(self.paths)
        self._track_worker(worker)
        worker.found.connect(self._show_cleanup_dialog)
        worker.start()

    def _show_cleanup_dialog(self, candidates: list[CleanupCandidate]) -> None:
        self.btn_cleanup.setEnabled(True)
        if not candidates:
            self.status_label.setText("Nenhum prefixo antigo ou órfão encontrado.")
            QMessageBox.information(self, __app_name__, "Tudo limpo! Nenhum prefixo antigo ou órfão encontrado.")
            return

        dlg = QDialog(self)
        dlg.setWindowTitle("Limpar Prefixos Já Migrados e Órfãos")
        dlg.resize(720, 460)
        lay = QVBoxLayout(dlg)
        info = QLabel(
            "Revise os prefixos antigos/órfãos antes de apagar. "
            "Só vêm marcados automaticamente os prefixos sem saves ou cujos saves já foram validados no Steam."
        )
        info.setWordWrap(True)
        lay.addWidget(info)

        lst = QListWidget()
        lay.addWidget(lst, 1)

        total_lbl = QLabel()

        def _update_total() -> None:
            sel_bytes = 0
            for i in range(lst.count()):
                it = lst.item(i)
                if it.checkState() == Qt.CheckState.Checked:
                    c: CleanupCandidate = it.data(Qt.ItemDataRole.UserRole)
                    sel_bytes += c.size_bytes
            total_lbl.setText(f"Espaço selecionado para liberar: {sel_bytes / (1024 * 1024):.1f} MB")

        def _populate_list(items: list[CleanupCandidate]) -> None:
            lst.blockSignals(True)
            lst.clear()
            for cand in items:
                mb = cand.size_bytes / (1024 * 1024)
                if cand.saves_verified_migrated:
                    save_tag = f" [SAVES VALIDADOS ✓: {cand.matched_game}]"
                elif cand.has_saves:
                    save_tag = " [TEM SAVES - NÃO VALIDADO]"
                elif cand.category == "Prefixo Externo" and not cand.matched_game:
                    save_tag = " [SEM JOGO NO STEAM]"
                else:
                    save_tag = " [SEM SAVES]"
                item = QListWidgetItem(f"[{cand.category}] {cand.label} — {mb:.1f} MB{save_tag}")
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                if cand.category == "Prefixo Externo":
                    auto_check = bool(cand.matched_game) and (
                        (not cand.has_saves) or cand.saves_verified_migrated
                    )
                else:
                    auto_check = (not cand.has_saves) or cand.saves_verified_migrated
                item.setCheckState(
                    Qt.CheckState.Checked if auto_check else Qt.CheckState.Unchecked
                )
                item.setData(Qt.ItemDataRole.UserRole, cand)
                lst.addItem(item)
            lst.blockSignals(False)
            _update_total()

        _populate_list(candidates)
        lst.itemChanged.connect(lambda _: _update_total())
        lay.addWidget(total_lbl)

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        ok_btn = btns.button(QDialogButtonBox.StandardButton.Ok)
        ok_btn.setText("Apagar Selecionados")
        btn_revalidate = btns.addButton(
            "Validar e Importar Saves", QDialogButtonBox.ButtonRole.ActionRole
        )
        btns.accepted.connect(dlg.accept)
        btns.rejected.connect(dlg.reject)
        lay.addWidget(btns)

        dialog_open = True
        dlg.finished.connect(lambda _: nonlocal_close())

        def nonlocal_close() -> None:
            nonlocal dialog_open
            dialog_open = False

        def _revalidate_now() -> None:
            if not self.paths:
                return
            btn_revalidate.setEnabled(False)
            ok_btn.setEnabled(False)
            lst.setEnabled(False)
            info.setText("Sincronizando saves pendentes e revalidando prefixos...")

            class _RevalidateWorker(QThread):
                done = Signal(list, int, int, int)
                failed = Signal(str)

                def __init__(self, paths: SteamPaths) -> None:
                    super().__init__()
                    self._paths = paths

                def run(self) -> None:
                    try:
                        entries = load_shortcuts(self._paths.shortcuts_vdf)
                        pending_plan = plan_bulk_migration(
                            entries=entries,
                            games_root=Path.home() / "Games",
                            compatdata_dir=self._paths.compatdata_dir,
                            config_vdf=self._paths.config_vdf,
                        )
                        checked, copied = sync_all_external_saves(self._paths)
                        new_cands = find_cleanup_candidates(self._paths)
                        self.done.emit(new_cands, checked, copied, len(pending_plan))
                    except Exception as exc:
                        self.failed.emit(str(exc))

            def _on_reval_done(
                new_cands: list[CleanupCandidate], checked: int, copied: int, pending: int
            ) -> None:
                if not dialog_open:
                    return
                btn_revalidate.setEnabled(True)
                ok_btn.setEnabled(True)
                lst.setEnabled(True)
                _populate_list(new_cands)
                extra = (
                    f" Atenção: há {pending} jogo(s) ainda não padronizado(s) — rode 'Migrar Todos para ~/Games' antes."
                    if pending
                    else ""
                )
                info.setText(
                    f"Validação concluída ({checked} atalho(s), {copied} save(s) importado(s)/atualizado(s)).{extra}"
                )

            def _on_reval_err(err: str) -> None:
                if not dialog_open:
                    return
                btn_revalidate.setEnabled(True)
                ok_btn.setEnabled(True)
                lst.setEnabled(True)
                info.setText(f"Erro ao validar saves: {err}")

            w = _RevalidateWorker(self.paths)
            self._track_worker(w)
            w.done.connect(_on_reval_done)
            w.failed.connect(_on_reval_err)
            w.start()

        btn_revalidate.clicked.connect(_revalidate_now)

        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        selected = [
            lst.item(i).data(Qt.ItemDataRole.UserRole)
            for i in range(lst.count())
            if lst.item(i).checkState() == Qt.CheckState.Checked
        ]
        if not selected:
            return
        self._set_steam_busy(True)
        self.status_label.setText(f"Apagando {len(selected)} prefixo(s)/backup(s)...")

        class _CleanupDeleteWorker(QThread):
            done = Signal(int, int)

            def __init__(self, items: list[CleanupCandidate]) -> None:
                super().__init__()
                self._items = items

            def run(self) -> None:
                deleted, freed = delete_cleanup_candidates(self._items)
                self.done.emit(deleted, freed)

        worker = _CleanupDeleteWorker(selected)
        self._track_worker(worker)

        def _on_deleted(deleted: int, freed: int) -> None:
            self._set_steam_busy(False)
            msg = f"Removidos {deleted} item(ns), liberando {freed / (1024 * 1024):.1f} MB."
            self.status_label.setText(msg)
            QMessageBox.information(self, __app_name__, msg)

        worker.done.connect(_on_deleted)
        worker.start()


def _qt_message_handler(mode, context, message: str) -> None:
    logging.warning("Qt[%s]: %s", mode, message)


def main() -> int:
    """Application entry point."""
    qInstallMessageHandler(_qt_message_handler)
    app = QApplication(sys.argv)
    app.setApplicationName(__app_name__)
    app.setApplicationVersion(__version__)
    icon_file = Path(__file__).resolve().parent.parent.parent / "assets" / "icon.png"
    if icon_file.is_file():
        app.setWindowIcon(QIcon(str(icon_file)))
    win = MainWindow(app)
    win.show()
    return app.exec()
