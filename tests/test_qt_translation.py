"""Qt's own button texts must follow the app language."""

from __future__ import annotations

from PySide6.QtWidgets import QApplication, QDialogButtonBox

from deckinstall.ui import _install_qt_translator


def test_pt_br_standard_buttons_translated() -> None:
    app = QApplication.instance() or QApplication([])
    assert isinstance(app, QApplication)
    assert _install_qt_translator(app, "pt_BR") is True
    box = QDialogButtonBox(QDialogButtonBox.StandardButton.Yes | QDialogButtonBox.StandardButton.No)
    texts = {b.text().replace("&", "") for b in box.buttons()}
    assert texts == {"Sim", "Não"}
    box.deleteLater()


def test_english_needs_no_translator() -> None:
    app = QApplication.instance() or QApplication([])
    assert isinstance(app, QApplication)
    assert _install_qt_translator(app, "en") is False


def test_optional_install_default_button() -> None:
    from PySide6.QtWidgets import QMessageBox

    from deckinstall.ui import _optional_install_default

    assert _optional_install_default(True) == QMessageBox.StandardButton.No
    assert _optional_install_default(False) == QMessageBox.StandardButton.Yes
