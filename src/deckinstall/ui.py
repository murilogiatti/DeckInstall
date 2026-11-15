"""Modern KDE-style PySide6 interface for DeckInstall."""

from __future__ import annotations

import logging
import shutil
import subprocess
import sys
from pathlib import Path

import requests
from PySide6.QtCore import QPoint, QRect, QSettings, QSize, Qt, QThread, Signal, qInstallMessageHandler
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
    QLayout,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from . import __app_name__, __version__
from .artwork import (
    STEAM_CDN_BASE,
    USER_AGENT,
    SteamMatch,
    download_steam_artwork,
    restore_steam_artwork_backup,
    search_steam_games,
)
from .i18n import available_languages, get_language, set_language, tr
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
    list_installed_protons,
    resolve_steam_paths,
)
from .steam_vdf import (
    CleanupCandidate,
    MigrationPlanItem,
    ShortcutEntry,
    add_or_update_shortcut,
    apply_launch_presets,
    backup_all_saves,
    delete_cleanup_candidates_checked,
    find_cleanup_candidates,
    get_shortcut_proton,
    import_external_prefix_saves,
    infer_game_root_from_exe,
    load_shortcuts,
    migrate_shortcut_to_games,
    plan_bulk_migration,
    remove_shortcut,
    restore_saves_backup,
    set_shortcut_proton,
    shutdown_steam,
    start_steam,
    sync_all_external_saves,
    sync_prefix_symlinks,
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
                    if steam_appid:
                        download_steam_artwork(steam_appid, new_appid, self.paths.grid_dir)
                    migrated.append(item.new_name)
                except Exception as exc:
                    errors.append(f"{item.entry.app_name}: {exc}")

            self.status.emit("Sincronizando e validando saves de todos os atalhos...")
            checked, copied = sync_all_external_saves(self.paths)

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


def _is_wine_game_running() -> bool:
    """Return True if a Windows .exe process appears to be running under Wine/Proton."""
    proc = Path("/proc")
    if not proc.is_dir():
        return False
    for p in proc.iterdir():
        if not p.name.isdigit():
            continue
        try:
            cmd = (
                (p / "cmdline")
                .read_bytes()
                .replace(b"\x00", b" ")
                .decode("utf-8", errors="ignore")
                .lower()
            )
            if ".exe" in cmd and "wineserver" not in cmd:
                return True
        except OSError:
            continue
    return False


class FlowLayout(QLayout):
    """Responsive wrapping layout so action buttons never clip horizontally."""

    def __init__(self, parent: QWidget | None = None, margin: int = 0, spacing: int = 8) -> None:
        super().__init__(parent)
        self.setContentsMargins(margin, margin, margin, margin)
        self._spacing = spacing
        self._items: list = []

    def __del__(self) -> None:
        while self._items:
            self.takeAt(0)

    def addItem(self, item) -> None:
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int):
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index: int):
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def expandingDirections(self) -> Qt.Orientation:
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        return self._do_layout(QRect(0, 0, width, 0), True)

    def setGeometry(self, rect: QRect) -> None:
        super().setGeometry(rect)
        self._do_layout(rect, False)

    def sizeHint(self) -> QSize:
        return self.minimumSize()

    def minimumSize(self) -> QSize:
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        m = self.contentsMargins()
        size += QSize(m.left() + m.right(), m.top() + m.bottom())
        return size

    def _do_layout(self, rect: QRect, test_only: bool) -> int:
        m = self.contentsMargins()
        eff = rect.adjusted(m.left(), m.top(), -m.right(), -m.bottom())
        x = eff.x()
        y = eff.y()
        line_height = 0
        for item in self._items:
            hint = item.sizeHint()
            next_x = x + hint.width() + self._spacing
            if next_x - self._spacing > eff.right() and line_height > 0:
                x = eff.x()
                y += line_height + self._spacing
                next_x = x + hint.width() + self._spacing
                line_height = 0
            if not test_only:
                item.setGeometry(QRect(QPoint(x, y), hint))
            x = next_x
            line_height = max(line_height, hint.height())
        return y + line_height - rect.y() + m.bottom()


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
        set_language(str(self.settings.value("language", "auto")))
        self.paths = resolve_steam_paths()
        self._detected_games: list[DetectedGame] = []
        self._active_workers: set[QThread] = set()
        self._closing_pending = False
        self._search_req_id = 0
        self._preview_req_id = 0
        self._extract_worker: ExtractWorker | None = None
        self._search_worker: SteamSearchWorker | None = None
        self._preview_worker: PreviewWorker | None = None
        self._add_worker: AddToSteamWorker | None = None
        self._bulk_worker: BulkMigrateWorker | None = None
        self._scan_worker: ScanWorker | None = None

        self.setWindowTitle(f"{__app_name__} v{__version__}")
        self.resize(1080, 700)
        self.setMinimumSize(840, 560)
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
            if self._closing_pending and not any(w.isRunning() for w in self._active_workers):
                self._closing_pending = False
                self.close()

        worker.finished.connect(_cleanup)

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._extract_worker and self._extract_worker.isRunning():
            self._extract_worker.cancel()
        if any(w.isRunning() for w in self._active_workers):
            self._closing_pending = True
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

        def _wrap_scroll(w: QWidget) -> QScrollArea:
            sa = QScrollArea()
            sa.setWidgetResizable(True)
            sa.setFrameShape(QFrame.Shape.NoFrame)
            sa.setWidget(w)
            return sa

        self.stack = QStackedWidget()
        self.stack.addWidget(_wrap_scroll(self._build_extract_page()))
        self.stack.addWidget(_wrap_scroll(self._build_add_page()))
        self.stack.addWidget(_wrap_scroll(self._build_library_page()))
        right_layout.addWidget(self.stack)

        self.status_label = QLabel(tr("status.ready"))
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
            tr("tabs.install"),
            tr("install.btn_add_steam").split(" (")[0],
            tr("tabs.library"),
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

        theme_lbl = QLabel(tr("header.theme"))
        theme_lbl.setObjectName("MutedText")
        layout.addWidget(theme_lbl)
        self.theme_combo = QComboBox()
        self.theme_combo.addItem(tr("header.theme.auto"), "auto")
        self.theme_combo.addItem(tr("header.theme.dark"), "dark")
        self.theme_combo.addItem(tr("header.theme.light"), "light")
        self.theme_combo.currentIndexChanged.connect(self._on_theme_changed)
        layout.addWidget(self.theme_combo)

        lang_lbl = QLabel(tr("header.language"))
        lang_lbl.setObjectName("MutedText")
        layout.addWidget(lang_lbl)
        self.lang_combo = QComboBox()
        self.lang_combo.addItem(tr("header.language.auto"), "auto")
        for code, disp in available_languages():
            self.lang_combo.addItem(disp, code)
        saved_lang = str(self.settings.value("language", "auto"))
        lidx = self.lang_combo.findData(saved_lang)
        if lidx >= 0:
            self.lang_combo.setCurrentIndex(lidx)
        self.lang_combo.currentIndexChanged.connect(self._on_language_changed)
        layout.addWidget(self.lang_combo)

        btn_about = QPushButton(tr("header.about"))
        btn_about.clicked.connect(self._show_about_dialog)
        layout.addWidget(btn_about)
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

        actions = FlowLayout(spacing=8)
        self.btn_bulk_migrate = QPushButton(tr("library.btn_bulk_migrate"))
        self.btn_bulk_migrate.setObjectName("primaryBtn")
        self.btn_bulk_migrate.clicked.connect(self._bulk_migrate_shortcuts)
        self.btn_migrate = QPushButton(tr("library.btn_migrate_one"))
        self.btn_migrate.clicked.connect(self._migrate_selected_shortcut)
        self.btn_art = QPushButton(tr("library.btn_update_art"))
        self.btn_art.clicked.connect(self._update_selected_shortcut_art)
        self.btn_revert_art = QPushButton(tr("library.btn_revert_art"))
        self.btn_revert_art.clicked.connect(self._revert_selected_shortcut_art)
        self.btn_proton = QPushButton(tr("library.btn_proton"))
        self.btn_proton.clicked.connect(self._change_selected_proton)
        self.btn_launch_opts = QPushButton(tr("library.btn_launch_opts"))
        self.btn_launch_opts.clicked.connect(self._edit_launch_options_presets)
        self.btn_backup_saves = QPushButton(tr("library.btn_backup_saves"))
        self.btn_backup_saves.clicked.connect(self._backup_all_saves)
        self.btn_restore_saves = QPushButton(tr("library.btn_restore_saves"))
        self.btn_restore_saves.clicked.connect(self._restore_saves_backup)
        self.btn_link_prefixes = QPushButton(tr("library.btn_link_prefixes"))
        self.btn_link_prefixes.clicked.connect(self._sync_prefix_links)
        self.btn_cleanup = QPushButton(tr("library.btn_cleanup"))
        self.btn_cleanup.clicked.connect(self._cleanup_old_prefixes)
        self.btn_remove = QPushButton(tr("library.btn_remove"))
        self.btn_remove.setObjectName("dangerBtn")
        self.btn_remove.clicked.connect(self._remove_selected_shortcut)
        for b in (
            self.btn_bulk_migrate,
            self.btn_migrate,
            self.btn_art,
            self.btn_revert_art,
            self.btn_proton,
            self.btn_launch_opts,
            self.btn_backup_saves,
            self.btn_restore_saves,
            self.btn_link_prefixes,
            self.btn_cleanup,
            self.btn_remove,
        ):
            actions.addWidget(b)
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

    def _on_language_changed(self) -> None:
        code = str(self.lang_combo.currentData() or "auto")
        self.settings.setValue("language", code)
        set_language(code)
        QMessageBox.information(
            self,
            __app_name__,
            tr("lang.changed", name=self.lang_combo.currentText()),
        )

    def _show_about_dialog(self) -> None:
        QMessageBox.about(
            self,
            tr("about.title", app=__app_name__),
            tr("about.body", app=__app_name__, version=__version__),
        )

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
            self.btn_revert_art,
            self.btn_proton,
            self.btn_launch_opts,
            self.btn_backup_saves,
            self.btn_restore_saves,
            self.btn_link_prefixes,
            self.btn_cleanup,
            self.btn_remove,
        ):
            btn.setEnabled(enabled)

    def _update_selected_shortcut_art(self) -> None:
        entry = self._selected_shortcut()
        if not entry or not self.paths:
            return
        exe_clean = entry.exe.strip().strip('"')
        is_flatpak_or_native = ("flatpak" in exe_clean.lower()) or (
            bool(exe_clean) and not exe_clean.lower().endswith(".exe")
        )
        if is_flatpak_or_native:
            ans = QMessageBox.question(
                self,
                __app_name__,
                tr("art.flatpak_confirm", name=entry.app_name, exe=exe_clean),
            )
            if ans != QMessageBox.StandardButton.Yes:
                return

        self._set_steam_busy(True)
        self.status_label.setText(f"Searching Steam Store for '{entry.app_name}'...")

        class _ArtSearchWorker(QThread):
            found = Signal(list)
            failed = Signal(str)

            def __init__(self, app_name: str) -> None:
                super().__init__()
                self._app_name = app_name

            def run(self) -> None:
                try:
                    self.found.emit(search_steam_games(self._app_name, limit=5))
                except Exception as exc:
                    self.failed.emit(str(exc))

        def _on_search_found(matches: list[SteamMatch]) -> None:
            if not matches:
                self._set_steam_busy(False)
                msg = tr("art.not_found", name=entry.app_name)
                self.status_label.setText(msg)
                QMessageBox.information(self, __app_name__, msg)
                return
            labels = [f"{m.name} (AppID {m.appid})" for m in matches]
            choice, ok = QInputDialog.getItem(
                self,
                tr("art.pick_title"),
                tr("art.pick_label", name=entry.app_name),
                labels,
                0,
                False,
            )
            if not ok or not choice:
                self._set_steam_busy(False)
                self.status_label.setText(tr("status.ready"))
                return
            chosen = matches[labels.index(choice)]

            class _ArtDownloadWorker(QThread):
                done = Signal(str)
                failed = Signal(str)

                def __init__(self, paths: SteamPaths, shortcut: ShortcutEntry, match: SteamMatch) -> None:
                    super().__init__()
                    self._paths = paths
                    self._shortcut = shortcut
                    self._match = match

                def run(self) -> None:
                    try:
                        res = download_steam_artwork(
                            self._match.appid, self._shortcut.appid_u32, self._paths.grid_dir
                        )
                        files_str = ", ".join(res.saved_files.keys()) or tr("art.none")
                        self.done.emit(
                            tr("art.updated", name=self._match.name, files=files_str)
                        )
                    except Exception as exc:
                        self.failed.emit(str(exc))

            dl_worker = _ArtDownloadWorker(self.paths, entry, chosen)
            self._track_worker(dl_worker)
            dl_worker.done.connect(_ok)
            dl_worker.failed.connect(_err)
            dl_worker.start()

        worker = _ArtSearchWorker(entry.app_name)
        self._track_worker(worker)

        def _ok(msg: str) -> None:
            self._set_steam_busy(False)
            self.status_label.setText(msg)
            QMessageBox.information(self, __app_name__, msg)

        def _err(err: str) -> None:
            self._set_steam_busy(False)
            QMessageBox.critical(self, __app_name__, tr("art.error", err=err))

        worker.found.connect(_on_search_found)
        worker.failed.connect(_err)
        worker.start()

    def _revert_selected_shortcut_art(self) -> None:
        entry = self._selected_shortcut()
        if not entry or not self.paths:
            return
        bak_dir = self.paths.grid_dir / ".deckinstall_art_bak" / str(entry.appid_u32)
        bak_files = [p.name for p in sorted(bak_dir.iterdir()) if p.is_file()] if bak_dir.is_dir() else []
        if not bak_files:
            QMessageBox.information(
                self, __app_name__, tr("art.revert_none", name=entry.app_name)
            )
            return
        if (
            QMessageBox.question(
                self,
                __app_name__,
                tr("art.revert_confirm", name=entry.app_name, files=", ".join(bak_files)),
            )
            != QMessageBox.StandardButton.Yes
        ):
            return
        try:
            restored = restore_steam_artwork_backup(entry.appid_u32, self.paths.grid_dir)
            msg = tr("art.revert_done", name=entry.app_name, files=", ".join(restored))
            self.status_label.setText(msg)
            QMessageBox.information(self, __app_name__, msg)
        except Exception as exc:
            QMessageBox.critical(self, __app_name__, tr("art.revert_error", err=exc))

    def _change_selected_proton(self) -> None:
        entry = self._selected_shortcut()
        if not entry or not self.paths:
            return
        protons = list_installed_protons(self.paths.steam_root)
        current = get_shortcut_proton(self.paths.config_vdf, entry.appid_u32) or "default"
        labels = [disp for _, disp in protons]
        cur_idx = next((i for i, (tid, _) in enumerate(protons) if tid == current), 0)
        choice, ok = QInputDialog.getItem(
            self,
            tr("proton.title"),
            tr("proton.label", name=entry.app_name, current=current),
            labels,
            cur_idx,
            False,
        )
        if not ok or not choice:
            return
        tool_id = protons[labels.index(choice)][0]
        try:
            set_shortcut_proton(self.paths.config_vdf, entry.appid_u32, tool_id)
            msg = tr("proton.updated", name=entry.app_name, tool=tool_id)
            self.status_label.setText(msg)
            QMessageBox.information(self, __app_name__, msg)
        except Exception as exc:
            QMessageBox.critical(self, __app_name__, tr("proton.error", err=exc))

    def _edit_launch_options_presets(self) -> None:
        entry = self._selected_shortcut()
        if not entry or not self.paths:
            return
        dlg = QDialog(self)
        dlg.setWindowTitle(tr("launch.title", name=entry.app_name))
        dlg.resize(520, 300)
        lay = QVBoxLayout(dlg)

        opts_now = entry.launch_options or ""
        chk_gm = QCheckBox(tr("launch.gamemode"))
        chk_gm.setChecked("gamemoderun" in opts_now.split())
        chk_mh = QCheckBox(tr("launch.mangohud"))
        chk_mh.setChecked("MANGOHUD=1" in opts_now)
        chk_w3d = QCheckBox(tr("launch.wined3d"))
        chk_w3d.setChecked("PROTON_USE_WINED3D=1" in opts_now)
        chk_esync = QCheckBox(tr("launch.no_esync"))
        chk_esync.setChecked("PROTON_NO_ESYNC=1" in opts_now)
        chk_fsync = QCheckBox(tr("launch.no_fsync"))
        chk_fsync.setChecked("PROTON_NO_FSYNC=1" in opts_now)

        raw_lbl = QLabel(tr("launch.raw_label"))
        raw_edit = QLineEdit(opts_now)

        def _sync_preview() -> None:
            raw_edit.setText(
                apply_launch_presets(
                    opts_now,
                    gamemode=chk_gm.isChecked(),
                    mangohud=chk_mh.isChecked(),
                    wined3d=chk_w3d.isChecked(),
                    no_esync=chk_esync.isChecked(),
                    no_fsync=chk_fsync.isChecked(),
                )
            )

        for chk in (chk_gm, chk_mh, chk_w3d, chk_esync, chk_fsync):
            chk.toggled.connect(_sync_preview)
            lay.addWidget(chk)
        lay.addWidget(raw_lbl)
        lay.addWidget(raw_edit)

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        btns.accepted.connect(dlg.accept)
        btns.rejected.connect(dlg.reject)
        lay.addWidget(btns)

        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        new_opts = raw_edit.text().strip()
        try:
            exe_p = Path(entry.exe.strip().strip('"'))
            start_p = Path(entry.start_dir.strip().strip('"')) if entry.start_dir else exe_p.parent
            add_or_update_shortcut(
                paths=self.paths,
                app_name=entry.app_name,
                exe_path=exe_p,
                start_dir=start_p,
                icon_path=Path(entry.icon.strip('"')) if entry.icon else None,
                launch_options=new_opts,
                enable_proton=False,
            )
            self._refresh_shortcuts()
            msg = tr("launch.saved", name=entry.app_name)
            self.status_label.setText(msg)
            QMessageBox.information(self, __app_name__, msg)
        except Exception as exc:
            QMessageBox.critical(self, __app_name__, str(exc))

    def _backup_all_saves(self) -> None:
        if not self.paths:
            return
        self._set_steam_busy(True)
        self.status_label.setText(tr("backup.running"))

        class _BackupWorker(QThread):
            done = Signal(str, int, int)
            failed = Signal(str)

            def __init__(self, paths: SteamPaths) -> None:
                super().__init__()
                self._paths = paths

            def run(self) -> None:
                try:
                    zpath, games, files = backup_all_saves(self._paths)
                    self.done.emit(str(zpath), games, files)
                except Exception as exc:
                    self.failed.emit(str(exc))

        def _ok(zpath: str, games: int, files: int) -> None:
            self._set_steam_busy(False)
            if files == 0:
                Path(zpath).unlink(missing_ok=True)
                msg = tr("backup.empty")
            else:
                msg = tr("backup.done", files=files, games=games, path=zpath)
            self.status_label.setText(msg.splitlines()[0])
            QMessageBox.information(self, __app_name__, msg)

        def _err(err: str) -> None:
            self._set_steam_busy(False)
            QMessageBox.critical(self, __app_name__, tr("backup.error", err=err))

        w = _BackupWorker(self.paths)
        self._track_worker(w)
        w.done.connect(_ok)
        w.failed.connect(_err)
        w.start()

    def _restore_saves_backup(self) -> None:
        if not self.paths:
            return
        bak_dir = Path.home() / "Backups" / "DeckInstall"
        start_dir = str(bak_dir if bak_dir.is_dir() else Path.home())
        chosen, _ = QFileDialog.getOpenFileName(
            self, tr("restore.pick"), start_dir, "Zip Archives (*.zip)"
        )
        if not chosen:
            return
        zpath = Path(chosen)
        if (
            QMessageBox.question(
                self, __app_name__, tr("restore.confirm", name=zpath.name)
            )
            != QMessageBox.StandardButton.Yes
        ):
            return
        try:
            games, files = restore_saves_backup(self.paths, zpath)
            msg = tr("restore.done", files=files, games=games)
            self.status_label.setText(msg)
            QMessageBox.information(self, __app_name__, msg)
        except Exception as exc:
            QMessageBox.critical(self, __app_name__, tr("restore.error", err=exc))

    def _sync_prefix_links(self) -> None:
        if not self.paths:
            return
        try:
            created, removed = sync_prefix_symlinks(self.paths)
            msg = tr("links.done", created=created, removed=removed)
            self.status_label.setText(msg)
            QMessageBox.information(self, __app_name__, msg)
        except Exception as exc:
            QMessageBox.critical(self, __app_name__, tr("links.error", err=exc))

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
                        if _is_wine_game_running():
                            raise RuntimeError(
                                "Feche o jogo Windows em execução antes de sincronizar saves."
                            )
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
        self._set_steam_busy(True)
        self.status_label.setText("Buscando prefixos antigos e compatdata órfãos...")

        class _CleanupScanWorker(QThread):
            found = Signal(list)
            failed = Signal(str)

            def __init__(self, paths: SteamPaths) -> None:
                super().__init__()
                self._paths = paths

            def run(self) -> None:
                try:
                    self.found.emit(find_cleanup_candidates(self._paths))
                except Exception as exc:
                    self.failed.emit(str(exc))

        def _on_scan_failed(err: str) -> None:
            self._set_steam_busy(False)
            self.status_label.setText("Erro ao buscar prefixos.")
            QMessageBox.critical(self, __app_name__, f"Erro ao buscar prefixos: {err}")

        worker = _CleanupScanWorker(self.paths)
        self._track_worker(worker)
        worker.found.connect(self._show_cleanup_dialog)
        worker.failed.connect(_on_scan_failed)
        worker.start()

    def _show_cleanup_dialog(self, candidates: list[CleanupCandidate]) -> None:
        if not candidates:
            self._set_steam_busy(False)
            self.status_label.setText("Nenhum prefixo antigo ou órfão encontrado.")
            QMessageBox.information(self, __app_name__, "Tudo limpo! Nenhum prefixo antigo ou órfão encontrado.")
            return

        dlg = QDialog(self)
        dlg.setWindowTitle(tr("cleanup.title"))
        dlg.resize(720, 460)
        lay = QVBoxLayout(dlg)
        info = QLabel(tr("cleanup.info"))
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
            total_lbl.setText(tr("cleanup.total", mb=sel_bytes / (1024 * 1024)))

        def _populate_list(items: list[CleanupCandidate]) -> None:
            lst.blockSignals(True)
            lst.clear()
            for cand in items:
                mb = cand.size_bytes / (1024 * 1024)
                if cand.saves_verified_migrated:
                    save_tag = tr("cleanup.tag_verified", game=cand.matched_game)
                elif cand.has_saves:
                    save_tag = tr("cleanup.tag_unverified")
                elif cand.category == "Prefixo Externo" and not cand.matched_game:
                    save_tag = tr("cleanup.tag_no_steam")
                else:
                    save_tag = tr("cleanup.tag_no_saves")
                cat_map = {
                    "Prefixo Externo": tr("cleanup.cat_external"),
                    "Backup": tr("cleanup.cat_backup"),
                    "Compatdata Órfão": tr("cleanup.cat_orphan"),
                }
                cat_disp = cat_map.get(cand.category, cand.category)
                hint = f" ({cand.game_hint})" if cand.game_hint else ""
                item = QListWidgetItem(f"[{cat_disp}] {cand.label}{hint} — {mb:.1f} MB{save_tag}")
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
        ok_btn.setText(tr("cleanup.btn_delete"))
        btn_revalidate = btns.addButton(
            tr("cleanup.btn_revalidate"), QDialogButtonBox.ButtonRole.ActionRole
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
                        if _is_wine_game_running():
                            raise RuntimeError(
                                "Feche o jogo Windows em execução antes de validar saves."
                            )
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
            self._set_steam_busy(False)
            return
        selected = [
            lst.item(i).data(Qt.ItemDataRole.UserRole)
            for i in range(lst.count())
            if lst.item(i).checkState() == Qt.CheckState.Checked
        ]
        if not selected or not self.paths:
            self._set_steam_busy(False)
            return
        self.status_label.setText(f"Apagando {len(selected)} prefixo(s)/backup(s)...")

        class _CleanupDeleteWorker(QThread):
            done = Signal(int, int, list)
            failed = Signal(str)

            def __init__(self, paths: SteamPaths, items: list[CleanupCandidate]) -> None:
                super().__init__()
                self._paths = paths
                self._items = items

            def run(self) -> None:
                try:
                    deleted, freed, errs = delete_cleanup_candidates_checked(
                        self._paths, self._items
                    )
                    self.done.emit(deleted, freed, errs)
                except Exception as exc:
                    self.failed.emit(str(exc))

        worker = _CleanupDeleteWorker(self.paths, selected)
        self._track_worker(worker)

        def _on_deleted(deleted: int, freed: int, errs: list[str]) -> None:
            self._set_steam_busy(False)
            msg = f"Removidos {deleted} item(ns), liberando {freed / (1024 * 1024):.1f} MB."
            if errs:
                msg += "\n\nAvisos/Erros:\n" + "\n".join(errs[:10])
            self.status_label.setText(f"Removidos {deleted} item(ns).")
            if errs:
                QMessageBox.warning(self, __app_name__, msg)
            else:
                QMessageBox.information(self, __app_name__, msg)

        def _on_delete_failed(err: str) -> None:
            self._set_steam_busy(False)
            self.status_label.setText("Erro ao apagar prefixos.")
            QMessageBox.critical(self, __app_name__, f"Erro ao apagar prefixos: {err}")

        worker.done.connect(_on_deleted)
        worker.failed.connect(_on_delete_failed)
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
