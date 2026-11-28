"""Install and Library pages of the main window (built once, bound onto the window)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import (
    Qt,
    QTimer,
)
from PySide6.QtGui import (
    QAction,
    QKeySequence,
    QShortcut,
)
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .i18n import available_languages, tr
from .standalone import (
    uses_library_layout,
)
from .ui_cards import (
    SHORTCUT_DESKTOP_KEY,
    SHORTCUT_MENU_KEY,
    DropZoneFrame,
    FlowLayout,
    ShownWhenEnabledButton,
    _make_card,
    _setting_bool,
    action_grid,
    action_row,
    note_label,
    page_layout,
    section_card,
    section_page,
    shortcut_check,
    shortcut_target_button,
)
from .ui_dialogs import (
    bulk_proton_dialog,
)

if TYPE_CHECKING:
    from .ui import MainWindow


def build_extract_page(win: MainWindow) -> QWidget:
    page, layout = page_layout(tr("install.page_title"), tr("install.page_subtitle"))

    drop_card = DropZoneFrame()
    drop_card.file_dropped.connect(win._set_extract_source)
    drop_layout = QVBoxLayout(drop_card)
    drop_layout.setContentsMargins(18, 16, 18, 16)
    drop_layout.setSpacing(10)

    src_lbl = QLabel(tr("install.source_title"))
    src_lbl.setObjectName("SectionTitle")
    drop_layout.addWidget(src_lbl)
    src_hint = QLabel(tr("install.source_hint"))
    src_hint.setObjectName("MutedText")
    src_hint.setWordWrap(True)
    drop_layout.addWidget(src_hint)

    src_row = QHBoxLayout()
    win.extract_src = QLineEdit()
    win.extract_src.setPlaceholderText(tr("install.source_placeholder"))
    btn_src = QPushButton(tr("install.browse"))
    btn_src.clicked.connect(win._pick_archive_or_installer)
    src_row.addWidget(win.extract_src, 1)
    src_row.addWidget(btn_src)
    drop_layout.addLayout(src_row)


    btn_row = FlowLayout(spacing=8)
    win.btn_extract = QPushButton(tr("install.btn_extract"))
    win.btn_extract.setObjectName("primaryBtn")
    win.btn_extract.clicked.connect(win._start_extract)
    win.btn_cancel_extract = ShownWhenEnabledButton(tr("install.btn_cancel"))
    win.btn_cancel_extract.setEnabled(False)
    win.btn_cancel_extract.clicked.connect(win._cancel_extract)
    win.btn_use_extracted = ShownWhenEnabledButton(tr("install.btn_continue_add"))
    win.btn_use_extracted.setEnabled(False)
    win.btn_use_extracted.clicked.connect(win._send_extracted_to_add_tab)
    btn_row.addWidget(win.btn_extract)
    btn_row.addWidget(win.btn_cancel_extract)
    btn_row.addWidget(win.btn_use_extracted)
    drop_layout.addLayout(btn_row)


    # Progress and log appear in the Source step once an extraction/setup starts.
    win.extract_details_box = QWidget()
    win.extract_details_box.setVisible(False)
    details_lay = QVBoxLayout(win.extract_details_box)
    details_lay.setContentsMargins(0, 4, 0, 0)
    details_lay.setSpacing(8)

    win.extract_progress = QProgressBar()
    details_lay.addWidget(win.extract_progress)

    win.extract_log = QTextEdit()
    win.extract_log.setReadOnly(True)
    win.extract_log.setMaximumHeight(140)
    win.extract_log.setPlaceholderText(tr("install.log_placeholder"))
    details_lay.addWidget(win.extract_log)
    drop_layout.addWidget(win.extract_details_box)

    layout.addWidget(drop_card)
    layout.addWidget(win._build_add_page(), 1)
    return page


def build_add_page(win: MainWindow) -> QWidget:
    page = QWidget()
    layout = QVBoxLayout(page)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(12)

    list_card, list_layout = _make_card()
    scan_row = QHBoxLayout()
    lbl_det = QLabel(tr("install.detected_title"))
    lbl_det.setObjectName("SectionTitle")
    win.btn_scan = QPushButton(tr("install.btn_rescan"))
    win.btn_scan.clicked.connect(win._refresh_games)
    win.btn_scan.setToolTip(tr("install.rescan_tip"))  # the folders searched are in Settings
    btn_browse_exe = QPushButton(tr("install.pick_exe"))
    btn_browse_exe.clicked.connect(win._pick_manual_exe)
    # Games already installed elsewhere (Heroic, Lutris, Hydra, old Steam shortcuts...) come in here.
    win.btn_adopt_games = QPushButton(tr("adopt.btn_open"))
    win.btn_adopt_games.setToolTip(tr("adopt.btn_open_tip"))
    win.btn_adopt_games.clicked.connect(win._open_adopt_wizard)
    scan_row.addWidget(lbl_det)
    scan_row.addStretch()
    scan_row.addWidget(win.btn_scan)
    scan_row.addWidget(btn_browse_exe)
    scan_row.addWidget(win.btn_adopt_games)
    list_layout.addLayout(scan_row)

    win.games_list = QTableWidget(0, 2)
    win.games_list.setHorizontalHeaderLabels(
        [tr("add.detected_name_column"), tr("add.detected_folder_column")]
    )
    win.games_list.verticalHeader().setVisible(False)
    win.games_list.setMinimumHeight(120)
    win.games_list.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
    win.games_list.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
    win.games_list.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
    win.games_list.setSortingEnabled(True)
    win.games_list.horizontalHeader().setSectionResizeMode(
        0, QHeaderView.ResizeMode.ResizeToContents
    )
    win.games_list.horizontalHeader().setSectionResizeMode(
        1, QHeaderView.ResizeMode.Stretch
    )
    win.games_list.horizontalHeader().setStretchLastSection(True)
    win.games_list.horizontalHeader().setSectionsClickable(True)
    win.games_list.currentCellChanged.connect(
        lambda row, _column, _previous_row, _previous_column: win._on_game_selected(row)
    )
    list_layout.addWidget(win.games_list, 1)
    # Only games are listed; other programs found (tools, installers...) stay one tick away.
    win.chk_show_other_programs = QCheckBox()
    win.chk_show_other_programs.setVisible(False)
    win.chk_show_other_programs.toggled.connect(lambda _on: win._show_detected_games())
    list_layout.addWidget(win.chk_show_other_programs)

    # Step 2 (Game): which folder and exe, its name, the Steam match and the cover preview.
    cfg_split = QSplitter(Qt.Orientation.Horizontal)
    cfg_split.setChildrenCollapsible(False)

    form_widget = QWidget()
    form = QFormLayout(form_widget)
    form.setContentsMargins(0, 0, 8, 0)
    form.setSpacing(10)

    win._match_install_timer = QTimer(win)
    win._match_install_timer.setSingleShot(True)
    win._match_install_timer.setInterval(180)
    win._match_install_timer.timeout.connect(win._update_remove_existing_state)
    win.add_exe_combo = QComboBox()
    win.add_exe_combo.setEditable(True)
    win.add_exe_combo.setSizeAdjustPolicy(
        QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
    )
    win.add_exe_combo.setMinimumContentsLength(24)
    win.add_exe_combo.currentTextChanged.connect(win._on_exe_changed)
    form.addRow(tr("install.exe_label"), win.add_exe_combo)

    win.add_name = QLineEdit()
    win.add_name.setPlaceholderText(tr("install.dest_placeholder"))
    win.add_name.textChanged.connect(lambda: win._match_install_timer.start())
    win.add_name.textEdited.connect(win._on_add_name_edited)
    form.addRow(tr("install.folder_label"), win.add_name)

    win.steam_match_combo = QComboBox()
    win.steam_match_combo.setSizeAdjustPolicy(
        QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
    )
    win.steam_match_combo.setMinimumContentsLength(24)
    win.steam_match_combo.currentIndexChanged.connect(win._on_steam_match_changed)
    # One line for the Steam match: the result picked, and the search that fills it.
    art_row = QHBoxLayout()
    win.steam_search_input = QLineEdit()
    win.steam_search_input.setPlaceholderText(tr("install.game_name_placeholder"))
    win.steam_search_input.returnPressed.connect(win._search_steam_art)
    btn_search = QPushButton(tr("install.search_steam"))
    btn_search.clicked.connect(win._search_steam_art)
    art_row.addWidget(win.steam_match_combo, 3)
    art_row.addWidget(win.steam_search_input, 2)
    art_row.addWidget(btn_search)
    form.addRow(tr("add.match_label"), art_row)

    # Step 3 (where and how it runs) has its own card: library, mode, presets, shortcuts.
    run_form = QFormLayout()
    run_form.setSpacing(10)

    win.add_library_combo = QComboBox()
    win.add_library_combo.setToolTip(tr("add.library_tooltip"))
    win.add_library_combo.currentIndexChanged.connect(win._remember_library_choice)
    run_form.addRow(tr("add.library"), win.add_library_combo)
    if not uses_library_layout():
        win.add_library_combo.setVisible(False)
        label_item = run_form.labelForField(win.add_library_combo)
        if label_item is not None:
            label_item.setVisible(False)

    win.chk_keep_in_place = QCheckBox(tr("add.keep_in_place"))
    win.chk_keep_in_place.setChecked(False)
    run_form.addRow("", win.chk_keep_in_place)

    # Who runs the game: Steam (Steam mode) or DeckInstall itself (Standalone mode).
    mode_row = QHBoxLayout()
    mode_row.setSpacing(8)
    win.btn_mode_steam = shortcut_target_button(tr("mode.steam"))
    win.btn_mode_standalone = shortcut_target_button(tr("mode.standalone"))
    win._install_mode_group = QButtonGroup(win)
    win._install_mode_group.setExclusive(True)
    for button in (win.btn_mode_steam, win.btn_mode_standalone):
        win._install_mode_group.addButton(button)
        mode_row.addWidget(button)
        button.toggled.connect(lambda _checked: win._apply_install_mode_ui())
        button.clicked.connect(win._on_install_mode_clicked)
    win._install_mode_chosen = False  # until the user picks one, follow the system's default
    win._standalone_launchers = (  # menu, desktop: as left last time
        _setting_bool(win.settings, SHORTCUT_MENU_KEY, True),
        _setting_bool(win.settings, SHORTCUT_DESKTOP_KEY, False),
    )
    mode_row.addStretch()
    win.lbl_mode_note = QLabel()
    win.lbl_mode_note.setObjectName("MutedText")
    win.lbl_mode_note.setWordWrap(True)
    mode_box = QWidget()
    mode_col = QVBoxLayout(mode_box)
    mode_col.setContentsMargins(0, 0, 0, 0)
    mode_col.setSpacing(4)
    mode_col.addLayout(mode_row)
    mode_col.addWidget(win.lbl_mode_note)
    run_form.addRow(tr("mode.label"), mode_box)

    win._add_launch_options = ""
    win._add_winedlloverrides = ""
    # New games start with the options used last; they are changed in the Library's Configure.
    win.lbl_add_presets = QLabel()
    win.lbl_add_presets.setObjectName("MutedText")
    win.lbl_add_presets.setWordWrap(True)
    run_form.addRow(tr("add.presets_label"), win.lbl_add_presets)

    # Standalone mode: where to find the game besides the Library.
    targets_row = FlowLayout(spacing=16)
    # Standalone launchers start as they were left last time.
    win.btn_shortcut_menu = shortcut_check(
        tr("add.shortcut_menu"), _setting_bool(win.settings, SHORTCUT_MENU_KEY, True)
    )
    win.btn_shortcut_desktop = shortcut_check(
        tr("add.shortcut_desktop"), _setting_bool(win.settings, SHORTCUT_DESKTOP_KEY, False)
    )
    for box, key in ((win.btn_shortcut_menu, SHORTCUT_MENU_KEY), (win.btn_shortcut_desktop, SHORTCUT_DESKTOP_KEY)):
        box.toggled.connect(lambda on, k=key: win._remember_shortcut_choice(k, on))
    # Shows where a Steam-mode game goes; follows the mode, never clicked on its own.
    win.btn_shortcut_steam = shortcut_check(tr("add.shortcut_steam"))
    win.btn_shortcut_steam.setEnabled(False)
    for target_box in (win.btn_shortcut_steam, win.btn_shortcut_menu, win.btn_shortcut_desktop):
        targets_row.addWidget(target_box)
    win.lbl_shortcuts_note = QLabel()
    win.lbl_shortcuts_note.setObjectName("MutedText")
    win.lbl_shortcuts_note.setWordWrap(True)
    targets_box = QWidget()
    targets_col = QVBoxLayout(targets_box)
    targets_col.setContentsMargins(0, 0, 0, 0)
    targets_col.setSpacing(4)
    targets_col.addLayout(targets_row)
    targets_col.addWidget(win.lbl_shortcuts_note)
    run_form.addRow(tr("add.shortcuts_label"), targets_box)
    # Steam mode: Steam closes to write the shortcut.
    win.chk_restart = QCheckBox(tr("add.reopen_steam"))
    win.chk_restart.setChecked(True)
    win.chk_restart.setToolTip(tr("add.reopen_steam_tooltip"))
    run_form.addRow("", win.chk_restart)

    cfg_split.addWidget(form_widget)

    preview_wrap = QWidget()
    preview_lay = QVBoxLayout(preview_wrap)
    preview_lay.setContentsMargins(8, 0, 0, 0)
    win.preview_label = QLabel(tr("install.art_preview_empty"))
    win.preview_label.setObjectName("PreviewBox")
    win.preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
    win.preview_label.setWordWrap(True)
    win.preview_label.setMinimumSize(180, 108)
    preview_lay.addWidget(win.preview_label)
    preview_lay.addStretch()
    cfg_split.addWidget(preview_wrap)
    cfg_split.setStretchFactor(0, 1)
    cfg_split.setStretchFactor(1, 0)
    cfg_split.setSizes([700, 240])
    list_layout.addWidget(cfg_split)
    layout.addWidget(list_card)

    run_card, run_layout, _top = section_card(tr("install.run_title"))
    run_layout.addLayout(run_form)
    actions_row = FlowLayout(spacing=8)
    win.btn_install = QPushButton(tr("install.btn_install"))
    win.btn_install.setObjectName("primaryBtn")
    win.btn_install.clicked.connect(win._install_selected)
    actions_row.addWidget(win.btn_install)
    win.chk_keep_in_place.toggled.connect(win._update_install_label)
    win._update_install_label()

    win.btn_remove_existing = QPushButton(tr("add.btn_remove_existing"))
    win.btn_remove_existing.setObjectName("dangerBtn")
    win.btn_remove_existing.clicked.connect(win._remove_existing_from_add)
    win.btn_remove_existing.setVisible(False)
    actions_row.addWidget(win.btn_remove_existing)
    run_layout.addLayout(actions_row)
    layout.addWidget(run_card)
    win._reset_add_launch_options()
    win._apply_install_mode_ui(follow_default=True)

    layout.addStretch(1)
    return page


def _page_install() -> int:
    from .ui import PAGE_INSTALL  # ui imports this module

    return PAGE_INSTALL


def _build_library_empty_state(win: MainWindow) -> QWidget:
    """Shown instead of the table when the library has no game at all (not when a filter hides them)."""
    box = QWidget()
    box.setObjectName("LibraryEmptyState")
    lay = QVBoxLayout(box)
    lay.addStretch(1)
    title = QLabel(tr("library.empty_title"))
    title.setObjectName("SectionTitle")
    title.setAlignment(Qt.AlignmentFlag.AlignCenter)
    body = QLabel(tr("library.empty_body"))
    body.setObjectName("MutedText")
    body.setWordWrap(True)
    body.setAlignment(Qt.AlignmentFlag.AlignCenter)
    row = QHBoxLayout()
    row.addStretch(1)
    install = QPushButton(tr("library.empty_install"))
    install.setObjectName("primaryBtn")
    install.clicked.connect(lambda: win._show_page(_page_install()))
    adopt = QPushButton(tr("adopt.btn_open"))
    adopt.clicked.connect(win._open_adopt_wizard)
    row.addWidget(install)
    row.addWidget(adopt)
    row.addStretch(1)
    for w in (title, body):
        lay.addWidget(w)
    lay.addLayout(row)
    lay.addStretch(1)
    box.setVisible(False)
    win.library_empty_state = box
    return box


def _build_library_no_results_state(win: MainWindow) -> QWidget:
    """Shown when games exist in the library but the active search/filter hides all of them."""
    box = QWidget()
    box.setObjectName("LibraryNoResultsState")
    lay = QVBoxLayout(box)
    lay.addStretch(1)
    title = QLabel(tr("library.no_results_title"))
    title.setObjectName("SectionTitle")
    title.setAlignment(Qt.AlignmentFlag.AlignCenter)
    body = QLabel(tr("library.no_results_body"))
    body.setObjectName("MutedText")
    body.setWordWrap(True)
    body.setAlignment(Qt.AlignmentFlag.AlignCenter)
    row = QHBoxLayout()
    row.addStretch(1)
    clear_btn = QPushButton(tr("library.btn_clear_filters"))
    clear_btn.setObjectName("primaryBtn")
    clear_btn.clicked.connect(win._clear_library_filters)
    row.addWidget(clear_btn)
    row.addStretch(1)
    lay.addWidget(title)
    lay.addWidget(body)
    lay.addLayout(row)
    lay.addStretch(1)
    box.setVisible(False)
    win.library_no_results_state = box
    return box


def build_library_page(win: MainWindow) -> QWidget:
    page = QWidget()
    layout = QVBoxLayout(page)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(12)

    my_lib_page, my_lib_layout = page_layout(tr("library.page_title"), tr("library.page_subtitle"))


    # 1. The games: pick one, then act on it (the everyday part).
    lib_card, lib_layout, top = section_card(tr("library.shortcuts_title"))
    win.btn_reload = QPushButton(tr("save_sync.btn_refresh"))
    win.btn_reload.setToolTip(tr("library.btn_reload_tip"))
    win.btn_reload.clicked.connect(win._refresh_shortcuts)
    top.addWidget(win.btn_reload)

    win.vdf_error_banner = QLabel()
    win.vdf_error_banner.setWordWrap(True)
    win.vdf_error_banner.setObjectName("VdfErrorBanner")
    win.vdf_error_banner.setVisible(False)
    lib_layout.addWidget(win.vdf_error_banner)

    win.unavailable_banner = QLabel()
    win.unavailable_banner.setWordWrap(True)
    win.unavailable_banner.setObjectName("UnavailableBanner")
    win.unavailable_banner.setVisible(False)
    lib_layout.addWidget(win.unavailable_banner)

    win.vdf_queue_banner = QFrame()
    win.vdf_queue_banner.setObjectName("VdfQueueBanner")
    vq_lay = QHBoxLayout(win.vdf_queue_banner)
    vq_lay.setContentsMargins(8, 4, 8, 4)
    win.lbl_vdf_queue = QLabel()
    win.lbl_vdf_queue.setObjectName("VdfQueueText")
    win.lbl_vdf_queue.setWordWrap(True)
    win.btn_vdf_apply = QPushButton(tr("vdf_queue.btn_apply"))
    win.btn_vdf_apply.setObjectName("primaryBtn")
    win.btn_vdf_apply.clicked.connect(lambda: win._apply_vdf_queue())
    win.btn_vdf_clear = QPushButton(tr("vdf_queue.btn_clear"))
    win.btn_vdf_clear.clicked.connect(win._clear_vdf_queue)
    vq_lay.addWidget(win.lbl_vdf_queue, 1)
    vq_lay.addWidget(win.btn_vdf_apply)
    vq_lay.addWidget(win.btn_vdf_clear)
    win.vdf_queue_banner.setVisible(False)
    lib_layout.addWidget(win.vdf_queue_banner)

    win._library_source_filter = "all"
    win._lib_filter_group = QButtonGroup(win)
    win._lib_filter_group.setExclusive(True)
    source_row = QHBoxLayout()
    source_row.setSpacing(6)
    for key, label in (
        ("all", tr("maint.filter_all")),
        ("steam", "Steam"),
        ("standalone", tr("library.filter_standalone")),
        ("external", tr("library.filter_external")),
        ("favorites", tr("library.filter_favorites")),
    ):
        pill = QPushButton(label)
        pill.setObjectName("FilterPill")
        pill.setCheckable(True)
        if key == "all":
            pill.setChecked(True)
        pill.clicked.connect(
            lambda _checked=False, k=key: win._on_library_source_filter_changed(k)
        )
        win._lib_filter_group.addButton(pill)
        source_row.addWidget(pill)
    source_row.addStretch()
    lib_layout.addLayout(source_row)

    filter_row = QHBoxLayout()
    filter_row.setSpacing(8)
    win.library_search_input = QLineEdit()
    win.library_search_input.setPlaceholderText(tr("library.search_placeholder"))
    win.library_search_input.setClearButtonEnabled(True)
    win._library_search_timer = QTimer(win)
    win._library_search_timer.setSingleShot(True)
    win._library_search_timer.setInterval(150)
    win._library_search_timer.timeout.connect(win._filter_library_list)

    def _on_search_changed(_text: str = "") -> None:
        # Instant filtering for small libraries & tests; debounced for 150+ games.
        if win.shortcuts_list.rowCount() >= 150:
            win._library_search_timer.start()
        else:
            win._library_search_timer.stop()
            win._filter_library_list()

    win.library_search_input.textChanged.connect(_on_search_changed)
    win.lbl_library_count = QLabel()
    win.lbl_library_count.setObjectName("MutedText")
    filter_row.addWidget(win.library_search_input, 1)
    filter_row.addWidget(win.lbl_library_count)
    from .ui_library_grid import set_view

    win.btn_view_list = QPushButton(tr("library.view_list"))
    win.btn_view_grid = QPushButton(tr("library.view_grid"))
    for btn, view in ((win.btn_view_list, "list"), (win.btn_view_grid, "grid")):
        btn.setObjectName("FilterPill")
        btn.setCheckable(True)
        btn.clicked.connect(lambda _=False, v=view: set_view(win, v))
        filter_row.addWidget(btn)
    lib_layout.addLayout(filter_row)

    win.shortcuts_list = QTableWidget(0, 7)
    win.shortcuts_list.setHorizontalHeaderLabels(
        [
            tr("table.name"),
            tr("table.source"),
            tr("table.runner"),
            tr("table.graphics"),
            tr("table.engine"),
            tr("table.size"),
            tr("table.executable"),
        ]
    )
    win.shortcuts_list.verticalHeader().setVisible(False)
    win.shortcuts_list.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
    win.shortcuts_list.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
    win.shortcuts_list.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
    header = win.shortcuts_list.horizontalHeader()
    for col, width in ((0, 210), (1, 105), (2, 130), (3, 90), (4, 110), (5, 80)):
        header.setSectionResizeMode(col, QHeaderView.ResizeMode.Interactive)
        win.shortcuts_list.setColumnWidth(col, width)
    header.setSectionResizeMode(6, QHeaderView.ResizeMode.Stretch)
    header.setStretchLastSection(True)
    header.setSectionsClickable(True)
    win.shortcuts_list.setSortingEnabled(True)
    win.shortcuts_list.horizontalHeader().sortIndicatorChanged.connect(
        lambda *_args: QTimer.singleShot(0, win, win._filter_library_list)
    )
    win.shortcuts_list.currentCellChanged.connect(lambda *_args: win._update_library_action_states())
    win.shortcuts_list.cellDoubleClicked.connect(lambda *_args: win._open_configure_game_dialog())
    del_shortcut = QShortcut(QKeySequence(QKeySequence.StandardKey.Delete), win.shortcuts_list)
    del_shortcut.setContext(Qt.ShortcutContext.WidgetShortcut)
    del_shortcut.activated.connect(win._remove_selected_shortcut)
    lib_layout.addWidget(win.shortcuts_list, 1)
    from .ui_library_grid import build_library_grid

    lib_layout.addWidget(build_library_grid(win), 1)
    lib_layout.addWidget(_build_library_empty_state(win), 1)
    lib_layout.addWidget(_build_library_no_results_state(win), 1)

    win.btn_play_game = QPushButton(tr("library.btn_play"))
    win.btn_play_game.setObjectName("primaryBtn")
    win.btn_play_game.clicked.connect(win._play_selected_game)


    win.act_ext_to_steam = QAction(tr("library.btn_ext_to_steam"), win)
    win.act_ext_to_steam.triggered.connect(win._external_add_to_steam)
    win.act_ext_import_saves = QAction(tr("library.btn_ext_import_saves"), win)
    win.act_ext_import_saves.triggered.connect(win._external_import_saves)
    win.btn_configure_game = QPushButton(tr("library.btn_configure_game"))
    win.btn_configure_game.clicked.connect(win._open_configure_game_dialog)
    find_shortcut = QShortcut(QKeySequence(QKeySequence.StandardKey.Find), win)
    find_shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
    find_shortcut.activated.connect(win._focus_library_search)

    # The selected game's actions, all in the row under the list (one button per action; the
    # QActions keep the enabled/visible state and the Configure dialog reuses two of them).
    win.act_favorite = QAction(tr("library.btn_favorite"), win)
    win.act_favorite.setCheckable(True)
    win.act_favorite.triggered.connect(win._toggle_favorite)
    win.act_move_game = QAction(tr("library.btn_move_disk"), win)
    win.act_move_game.triggered.connect(win._move_selected_game)
    win.act_game_art = QAction(tr("art.btn_selected"), win)
    win.act_remove_game = QAction(tr("library.btn_uninstall"), win)
    win.act_remove_game.triggered.connect(win._remove_selected_shortcut)

    win.act_bulk_proton = QAction(tr("library.btn_bulk_proton"), win)
    win.act_bulk_proton.triggered.connect(lambda: bulk_proton_dialog(win))
    win.act_bulk_optiscaler = QAction(tr("library.btn_bulk_optiscaler"), win)
    win.act_bulk_optiscaler.triggered.connect(win._open_bulk_optiscaler)
    from .ui_library_art import fetch_missing_art, fetch_selected_art

    win.act_game_art.triggered.connect(lambda: fetch_selected_art(win))
    footer = FlowLayout(spacing=8)
    lib_layout.addLayout(footer)
    footer.addWidget(win.btn_play_game)
    footer.addWidget(win.btn_configure_game)
    win.game_action_buttons = {}
    for action in (
        win.act_favorite, win.act_game_art, win.act_move_game,
        win.act_ext_to_steam, win.act_ext_import_saves, win.act_remove_game,
    ):
        btn = action_button(action, "dangerBtn" if action is win.act_remove_game else "")
        footer.addWidget(btn)
        win.game_action_buttons[action] = btn
    win.shortcuts_list.setMinimumHeight(420)
    win.library_grid.setMinimumHeight(420)

    # 2. Every game at once (games are added on the Install tab).
    all_card, all_layout, _all_top = section_card(tr("library.section_all"))
    win.btn_all_art = QPushButton(tr("art.btn_missing"))
    win.btn_all_art.clicked.connect(lambda: fetch_missing_art(win))
    all_grid = action_grid()
    action_row(all_grid, 0, action_button(win.act_bulk_proton), tr("library.bulk_proton_hint"))
    action_row(all_grid, 1, action_button(win.act_bulk_optiscaler), tr("library.bulk_optiscaler_hint"))
    action_row(all_grid, 2, win.btn_all_art, tr("art.btn_missing_hint"))
    all_layout.addLayout(all_grid)
    my_lib_layout.addWidget(lib_card, 1)
    my_lib_layout.addWidget(all_card)
    layout.addWidget(my_lib_page, 1)
    return page


def action_button(action: QAction, object_name: str = "") -> QPushButton:
    """A push button that shows and triggers `action` (text, enabled, visible and checked follow it)."""
    btn = QPushButton(action.text())
    if object_name:
        btn.setObjectName(object_name)

    def sync() -> None:
        btn.setText(action.text())
        btn.setToolTip(action.toolTip() if action.toolTip() != action.text().replace("&", "") else "")
        btn.setEnabled(action.isEnabled())
        btn.setCheckable(action.isCheckable())
        btn.setChecked(action.isChecked())
        if not action.isVisible():
            btn.hide()
        elif btn.parentWidget() is not None:  # never show a parentless button as a window
            btn.show()

    action.changed.connect(sync)
    action.toggled.connect(lambda _on: sync())
    btn.clicked.connect(lambda _checked=False: action.trigger())
    sync()
    return btn


def update_sgdb_state(win: MainWindow) -> None:
    """Say whether a SteamGridDB key is saved (the key itself is never shown)."""
    from .steamgriddb import api_key

    win.lbl_sgdb_state.setText(tr("sgdb.state_set") if api_key() else tr("sgdb.state_unset"))


def build_settings_page(win: MainWindow) -> QWidget:
    """Settings > General: preferences (look, new games, artwork) and the system (Steam, this PC, the app)."""
    from .ui import DEFAULT_FPS_KEY  # ui imports this module
    from .ui_scale import SCALE_KEY

    page, layout = section_page()

    win.theme_combo = QComboBox()
    win.theme_combo.addItem(tr("header.theme.auto"), "auto")
    win.theme_combo.addItem(tr("header.theme.dark"), "dark")
    win.theme_combo.addItem(tr("header.theme.light"), "light")
    win.theme_combo.currentIndexChanged.connect(win._on_theme_changed)
    win.lang_combo = QComboBox()
    win.lang_combo.addItem(tr("header.language.auto"), "auto")
    for code, disp in available_languages():
        win.lang_combo.addItem(disp, code)
    lidx = win.lang_combo.findData(str(win.settings.value("language", "auto")))
    if lidx >= 0:
        win.lang_combo.setCurrentIndex(lidx)
    win.lang_combo.currentIndexChanged.connect(win._on_language_changed)
    win.scale_combo = QComboBox()
    win.scale_combo.addItem(tr("settings.scale_auto"), "auto")
    win.scale_combo.addItem(tr("settings.scale_100"), "1")
    win.scale_combo.addItem(tr("settings.scale_125"), "1.25")
    sidx = win.scale_combo.findData(str(win.settings.value(SCALE_KEY, "auto") or "auto"))
    win.scale_combo.setCurrentIndex(max(0, sidx))

    def _on_scale_changed(_idx: int) -> None:
        win.settings.setValue(SCALE_KEY, win.scale_combo.currentData())
        win.message_bar.show_message(tr("settings.scale_restart"), "info", key="scale")

    win.scale_combo.currentIndexChanged.connect(_on_scale_changed)

    # New games: the frame cap they start with (Configure can change it per game later).
    win.default_fps_combo = QComboBox()
    for label, data in (
        (tr("settings.fps_last"), "last"),
        (tr("launch.mangohud_fps_unlimited"), "0"),
        (tr("settings.fps_half"), "half"),
        (tr("settings.fps_full"), "full"),
        *((tr("launch.mangohud_fps_value", fps=fps), str(fps)) for fps in (30, 40, 45, 60)),
    ):
        win.default_fps_combo.addItem(label, data)
    fidx = win.default_fps_combo.findData(str(win.settings.value(DEFAULT_FPS_KEY, "last") or "last"))
    win.default_fps_combo.setCurrentIndex(max(0, fidx))
    win.default_fps_combo.setToolTip(tr("settings.fps_hint"))

    def _on_default_fps(_idx: int) -> None:
        win.settings.setValue(DEFAULT_FPS_KEY, win.default_fps_combo.currentData())
        win._reset_add_launch_options()

    win.default_fps_combo.currentIndexChanged.connect(_on_default_fps)

    # Artwork: the SteamGridDB key (never shown, only whether one is saved).
    win.lbl_sgdb_state = note_label()
    btn_sgdb = QPushButton(tr("sgdb.btn"))
    btn_sgdb.setToolTip(tr("sgdb.tip"))
    btn_sgdb.clicked.connect(win._set_steamgriddb_key)
    sgdb_row = QHBoxLayout()
    sgdb_row.addWidget(btn_sgdb)
    sgdb_row.addWidget(win.lbl_sgdb_state)
    sgdb_row.addStretch()
    update_sgdb_state(win)

    # Preferences: one setting per line.
    prefs, prefs_layout, _top = section_card(tr("settings.prefs_title"))
    form = QFormLayout()
    form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.FieldsStayAtSizeHint)
    form.addRow(tr("header.theme"), win.theme_combo)
    form.addRow(tr("header.language"), win.lang_combo)
    form.addRow(tr("settings.scale"), win.scale_combo)
    form.addRow(tr("settings.new_games_fps"), win.default_fps_combo)
    form.addRow(tr("settings.art_label"), sgdb_row)
    win.btn_scan_folders = QPushButton(tr("scan_folders.btn"))
    win.btn_scan_folders.clicked.connect(win._open_scan_folders_dialog)
    win.lbl_scan_folders_count = note_label()
    win._update_scan_folders_label()
    folders_row = QHBoxLayout()
    folders_row.addWidget(win.btn_scan_folders)
    folders_row.addWidget(win.lbl_scan_folders_count)
    folders_row.addStretch()
    form.addRow(tr("settings.scan_folders_label"), folders_row)
    prefs_layout.addLayout(form)
    prefs_layout.addWidget(note_label(tr("settings.prefs_hint")))
    layout.addWidget(prefs)

    # System: the Steam in use, this computer, and the app itself.
    system, system_layout, system_top = section_card(tr("settings.system_title"))
    win.btn_redetect_steam = QPushButton(tr("sidebar.redetect_steam"))
    win.btn_redetect_steam.setToolTip(tr("settings.redetect_hint"))
    win.btn_redetect_steam.clicked.connect(win._redetect_steam)
    system_top.addWidget(win.btn_redetect_steam)
    sys_form = QFormLayout()
    win.steam_status_lbl = QLabel(win._format_steam_status_text())
    win.steam_status_lbl.setWordWrap(True)
    sys_form.addRow(tr("settings.steam_title"), win.steam_status_lbl)
    win.steam_account_combo = QComboBox()
    win.steam_account_combo.setToolTip(tr("settings.steam_account_hint"))
    win.steam_account_combo.currentIndexChanged.connect(win._on_steam_account_changed)
    sys_form.addRow(tr("settings.steam_account_label"), win.steam_account_combo)
    sys_form.setRowVisible(win.steam_account_combo, False)  # shown with more than one account
    win._settings_system_form = sys_form
    from .ui_hardware import build_hardware_rows, refresh_hardware_card

    build_hardware_rows(win, sys_form)
    refresh_hardware_card(win)
    system_layout.addLayout(sys_form)
    btn_about = QPushButton(tr("header.about"))
    btn_about.setToolTip(tr("settings.about_hint"))
    btn_about.clicked.connect(win._show_about_dialog)
    win.btn_copy_diag = QPushButton(tr("maint.copy_diagnostics"))
    win.btn_copy_diag.setToolTip(tr("maint.copy_diagnostics_tip"))
    win.btn_copy_diag.clicked.connect(win._copy_diagnostics)
    app_row = QHBoxLayout()
    app_row.addWidget(btn_about)
    app_row.addWidget(win.btn_copy_diag)
    app_row.addStretch()
    system_layout.addLayout(app_row)
    layout.addWidget(system)
    layout.addStretch(1)
    win._update_steam_status_style()
    return page
