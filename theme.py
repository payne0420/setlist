"""Spotify dark theme — single source of styling for Setlist."""

import os
import sys

from PyQt5 import QtCore, QtWidgets
from PyQt5.QtGui import QFontDatabase


def _asset_url(name: str) -> str:
    """Absolute, forward-slashed file URL for a bundled asset (PyInstaller-aware).

    Qt stylesheet url() needs forward slashes even on Windows, and the assets
    live under sys._MEIPASS in a frozen build.
    """
    if getattr(sys, "frozen", False):
        base = os.path.join(sys._MEIPASS, "assets")
    else:
        base = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
    return os.path.join(base, name).replace("\\", "/")


_CHEVRON_DOWN = _asset_url("chevron-down.svg")
_CHEVRON_UP = _asset_url("chevron-up.svg")
_CHECK = _asset_url("check.svg")

COLORS = {
    "base": "#121212",
    "surface": "#181818",
    "hover": "#282828",
    "input_bg": "#2A2A2A",
    "input_border": "#3E3E3E",
    "focus": "#1DB954",
    "text_primary": "#FFFFFF",
    "text_secondary": "#B3B3B3",
    "text_tertiary": "#6E6E73",
    "accent": "#1DB954",
    "accent_hover": "#1ED760",
    "accent_pressed": "#169C46",
    "download_text": "#000000",
    "progress_track": "#3E3E3E",
}

STYLESHEET = f"""
QMainWindow {{
    background-color: {COLORS["base"]};
}}

QWidget {{
    color: {COLORS["text_primary"]};
}}

QFrame#frame, QFrame#SONGINFORMATION {{
    background-color: {COLORS["surface"]};
    border: 1px solid {COLORS["hover"]};
    border-radius: 12px;
}}

QLabel {{
    background: transparent;
    color: {COLORS["text_primary"]};
    border: none;
}}

QLabel#title {{
    font-size: 20px;
    font-weight: 700;
    color: {COLORS["text_primary"]};
}}

QLabel#version, QLabel#author {{
    font-size: 11px;
    color: {COLORS["text_tertiary"]};
}}

QLabel#label_3, QLabel#label_7, QLabel#label_10, QLabel#PlaylistMsg_2,
QLabel#label_6, QLabel#label_9, QLabel#label_11, QLabel#label_8 {{
    font-size: 11px;
    font-weight: 600;
    letter-spacing: 1px;
    color: {COLORS["text_secondary"]};
    text-transform: uppercase;
}}

QLabel#AlbumName {{
    font-size: 13px;
    color: {COLORS["text_secondary"]};
}}

QLabel#statusMsg, QLabel#CounterLabel,
QLabel#SongName, QLabel#YearText, QLabel#ArtistNameText, QLabel#AlbumText {{
    font-size: 13px;
    color: {COLORS["text_primary"]};
}}

QLabel#MainSongName {{
    font-size: 15px;
    font-weight: 600;
    color: {COLORS["text_primary"]};
}}

QLabel#CoverImg {{
    background-color: {COLORS["hover"]};
    border: 1px solid {COLORS["hover"]};
    border-radius: 8px;
}}

QLineEdit#PlaylistLink {{
    background-color: {COLORS["input_bg"]};
    border: 1px solid {COLORS["input_border"]};
    border-radius: 8px;
    padding: 8px 12px;
    font-size: 13px;
    color: {COLORS["text_primary"]};
    selection-background-color: {COLORS["accent"]};
    selection-color: {COLORS["download_text"]};
}}

QLineEdit#PlaylistLink:focus {{
    border: 1px solid {COLORS["focus"]};
}}

QLineEdit#PlaylistLink:read-only {{
    border: 1px solid {COLORS["input_border"]};
}}

QLineEdit#PlaylistLink:read-only:focus {{
    border: 1px solid {COLORS["input_border"]};
}}

/* Multi-playlist queue dialog: paste box + queue list */
QPlainTextEdit {{
    background-color: {COLORS["input_bg"]};
    border: 1px solid {COLORS["input_border"]};
    border-radius: 8px;
    padding: 6px 8px;
    font-size: 13px;
    color: {COLORS["text_primary"]};
    selection-background-color: {COLORS["accent"]};
    selection-color: {COLORS["download_text"]};
}}

QPlainTextEdit:focus {{
    border: 1px solid {COLORS["focus"]};
}}

QListWidget {{
    background-color: {COLORS["input_bg"]};
    border: 1px solid {COLORS["input_border"]};
    border-radius: 8px;
    padding: 4px;
    font-size: 13px;
    color: {COLORS["text_primary"]};
    outline: none;
}}

QListWidget::item {{
    padding: 6px 8px;
    border-radius: 6px;
}}

QListWidget::item:selected {{
    background-color: {COLORS["hover"]};
    color: {COLORS["text_primary"]};
}}

QPushButton#DownloadBtn {{
    background-color: {COLORS["accent"]};
    color: {COLORS["download_text"]};
    border: none;
    border-radius: 20px;
    font-size: 13px;
    font-weight: 700;
    padding: 8px 20px;
    min-height: 36px;
    min-width: 90px;
}}

QPushButton#DownloadBtn:hover {{
    background-color: {COLORS["accent_hover"]};
}}

QPushButton#DownloadBtn:pressed {{
    background-color: {COLORS["accent_pressed"]};
}}

QPushButton#DownloadBtn:disabled {{
    background-color: {COLORS["input_border"]};
    color: {COLORS["text_tertiary"]};
}}

QPushButton#QueueBtn {{
    background-color: {COLORS["input_bg"]};
    color: {COLORS["text_primary"]};
    border: 1px solid {COLORS["input_border"]};
    border-radius: 20px;
    font-size: 13px;
    font-weight: 600;
    padding: 8px 20px;
    min-height: 36px;
}}

QPushButton#QueueBtn:hover {{
    background-color: {COLORS["hover"]};
    border-color: {COLORS["accent"]};
}}

QPushButton#QueueBtn:pressed {{
    background-color: {COLORS["input_border"]};
}}

QPushButton#QueueBtn:disabled {{
    color: {COLORS["text_tertiary"]};
    border-color: {COLORS["input_border"]};
}}

/* Queue action bar — compact, refined buttons (not the big Home pill) so a
   row of three reads as a tidy toolbar instead of fat touching pills. */
QPushButton#queueStartBtn {{
    background-color: {COLORS["accent"]};
    color: {COLORS["download_text"]};
    border: none;
    border-radius: 10px;
    font-size: 13px;
    font-weight: 700;
    padding: 8px 22px;
}}

QPushButton#queueStartBtn:hover {{
    background-color: {COLORS["accent_hover"]};
}}

QPushButton#queueStartBtn:pressed {{
    background-color: {COLORS["accent_pressed"]};
}}

QPushButton#queueStartBtn:disabled {{
    background-color: {COLORS["input_border"]};
    color: {COLORS["text_tertiary"]};
}}

QPushButton#queueStopBtn, QPushButton#queueClearBtn {{
    background-color: {COLORS["input_bg"]};
    color: {COLORS["text_primary"]};
    border: 1px solid {COLORS["input_border"]};
    border-radius: 10px;
    font-size: 13px;
    font-weight: 600;
    padding: 8px 22px;
}}

QPushButton#queueStopBtn:hover, QPushButton#queueClearBtn:hover {{
    background-color: {COLORS["hover"]};
    border-color: {COLORS["text_tertiary"]};
}}

QPushButton#queueStopBtn:pressed, QPushButton#queueClearBtn:pressed {{
    background-color: {COLORS["input_border"]};
}}

QPushButton#queueStopBtn:disabled, QPushButton#queueClearBtn:disabled {{
    color: {COLORS["text_tertiary"]};
    border-color: {COLORS["input_border"]};
}}

/* Sidebar navigation buttons (Home / Queue / History / Settings) */
QPushButton[nav="true"] {{
    background-color: transparent;
    color: {COLORS["text_secondary"]};
    border: none;
    border-left: 3px solid transparent;
    border-radius: 8px;
    font-size: 13px;
    text-align: left;
    padding: 9px 12px;
}}

QPushButton[nav="true"]:hover {{
    color: {COLORS["text_primary"]};
    background-color: {COLORS["hover"]};
}}

QPushButton[nav="true"]:checked {{
    color: {COLORS["text_primary"]};
    background-color: {COLORS["hover"]};
    border-left: 3px solid {COLORS["accent"]};
    font-weight: 600;
}}

QPushButton[nav="true"]:disabled {{
    color: {COLORS["text_tertiary"]};
}}

QPushButton#Select_Home {{
    background-color: transparent;
    color: {COLORS["text_secondary"]};
    border: none;
    font-size: 12px;
    text-align: left;
    padding: 4px 0;
}}

QPushButton#Select_Home:hover {{
    color: {COLORS["accent_hover"]};
}}

QPushButton#Select_Home:pressed {{
    color: {COLORS["accent"]};
}}

/* ---- Redesign: sidebar shell, content pages, cards, lists ---- */
QStackedWidget#content {{
    background-color: {COLORS["base"]};
}}

QFrame#sidebar {{
    background-color: {COLORS["surface"]};
    border: none;
    border-right: 1px solid {COLORS["hover"]};
}}

QLabel#wordmark {{
    font-size: 18px;
    font-weight: 700;
    color: {COLORS["text_primary"]};
}}

QLabel#pageTitle {{
    font-size: 22px;
    font-weight: 700;
    color: {COLORS["text_primary"]};
}}

QLabel#sectionLabel {{
    font-size: 11px;
    font-weight: 600;
    letter-spacing: 1px;
    color: {COLORS["text_secondary"]};
}}

QFrame#card {{
    background-color: {COLORS["surface"]};
    border: 1px solid {COLORS["hover"]};
    border-radius: 12px;
}}

QFrame#previewBox {{
    background-color: transparent;
    border: none;
}}

QFrame#cardDivider {{
    background-color: {COLORS["hover"]};
    border: none;
    max-height: 1px;
}}

/* Settings pane: form row labels read as muted text, controls stand out */
QWidget#settingsPage QFrame#card QLabel {{
    color: {COLORS["text_secondary"]};
    font-size: 13px;
}}

QListWidget#trackList {{
    background-color: transparent;
    border: none;
    outline: none;
}}

QListWidget#trackList::item {{
    padding: 7px 10px;
    margin: 1px 0;
    border-radius: 8px;
    color: {COLORS["text_primary"]};
}}

QListWidget#trackList::item:hover {{
    background-color: {COLORS["hover"]};
}}

QLabel#queueEmptyGlyph {{
    font-size: 44px;
    color: {COLORS["text_tertiary"]};
}}

QLabel#queueEmptyTitle {{
    font-size: 15px;
    font-weight: 600;
    color: {COLORS["text_secondary"]};
}}

QLabel#queueEmptyHint {{
    font-size: 13px;
    color: {COLORS["text_tertiary"]};
}}

/* Settings scroll area: viewport must stay transparent so the page's dark
   background shows through (scroll viewports auto-fill with the palette
   otherwise). */
QScrollArea#settingsScroll,
QScrollArea#settingsScroll > QWidget > QWidget {{
    background: transparent;
}}

/* Dark, minimal scrollbars (were native/unstyled before) */
QScrollBar:vertical {{
    background: transparent;
    width: 10px;
    margin: 2px;
}}

QScrollBar::handle:vertical {{
    background: {COLORS["input_border"]};
    border-radius: 4px;
    min-height: 28px;
}}

QScrollBar::handle:vertical:hover {{
    background: {COLORS["text_tertiary"]};
}}

QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0;
    background: none;
}}

QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
    background: none;
}}

QScrollBar:horizontal {{
    background: transparent;
    height: 10px;
    margin: 2px;
}}

QScrollBar::handle:horizontal {{
    background: {COLORS["input_border"]};
    border-radius: 4px;
    min-width: 28px;
}}

QScrollBar::handle:horizontal:hover {{
    background: {COLORS["text_tertiary"]};
}}

QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
    width: 0;
    background: none;
}}

QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {{
    background: none;
}}

QCheckBox {{
    spacing: 8px;
    color: {COLORS["text_primary"]};
    font-size: 13px;
}}

QCheckBox::indicator {{
    width: 18px;
    height: 18px;
    border-radius: 4px;
}}

QCheckBox::indicator:unchecked {{
    background-color: transparent;
    border: 2px solid {COLORS["text_tertiary"]};
}}

QCheckBox::indicator:unchecked:hover {{
    border-color: {COLORS["text_secondary"]};
}}

QCheckBox::indicator:checked {{
    background-color: {COLORS["accent"]};
    border: 2px solid {COLORS["accent"]};
    image: url({_CHECK});
}}

QProgressBar#SongDownloadprogress, QProgressBar#SongDownloadprogressBar {{
    background-color: {COLORS["progress_track"]};
    border: none;
    border-radius: 3px;
    min-height: 6px;
    max-height: 6px;
    text-align: center;
}}

QProgressBar#SongDownloadprogress::chunk, QProgressBar#SongDownloadprogressBar::chunk {{
    background-color: {COLORS["accent"]};
    border-radius: 3px;
}}

QComboBox {{
    background-color: {COLORS["input_bg"]};
    border: 1px solid {COLORS["input_border"]};
    border-radius: 8px;
    padding: 6px 12px;
    font-size: 13px;
    color: {COLORS["text_primary"]};
    min-height: 28px;
}}

QComboBox:focus, QComboBox:on {{
    border: 1px solid {COLORS["focus"]};
}}

QComboBox::drop-down {{
    subcontrol-origin: padding;
    subcontrol-position: top right;
    width: 28px;
    border: none;
    border-top-right-radius: 8px;
    border-bottom-right-radius: 8px;
}}

QComboBox::down-arrow {{
    image: url({_CHEVRON_DOWN});
    width: 12px;
    height: 8px;
    margin-right: 10px;
}}

QComboBox QAbstractItemView {{
    background-color: {COLORS["surface"]};
    border: 1px solid {COLORS["input_border"]};
    border-radius: 8px;
    color: {COLORS["text_primary"]};
    selection-background-color: {COLORS["hover"]};
    selection-color: {COLORS["text_primary"]};
    padding: 4px;
    outline: none;
}}

QSpinBox {{
    background-color: {COLORS["input_bg"]};
    border: 1px solid {COLORS["input_border"]};
    border-radius: 8px;
    padding: 6px 8px;
    font-size: 13px;
    color: {COLORS["text_primary"]};
    min-height: 28px;
}}

QSpinBox:focus {{
    border: 1px solid {COLORS["focus"]};
}}

QSpinBox::up-button, QSpinBox::down-button {{
    subcontrol-origin: border;
    width: 22px;
    background-color: {COLORS["hover"]};
    border: none;
    border-left: 1px solid {COLORS["input_border"]};
}}

QSpinBox::up-button {{
    subcontrol-position: top right;
    border-top-right-radius: 8px;
}}

QSpinBox::down-button {{
    subcontrol-position: bottom right;
    border-bottom-right-radius: 8px;
}}

QSpinBox::up-button:hover, QSpinBox::down-button:hover {{
    background-color: {COLORS["input_border"]};
}}

QSpinBox::up-arrow {{
    image: url({_CHEVRON_UP});
    width: 11px;
    height: 7px;
}}

QSpinBox::down-arrow {{
    image: url({_CHEVRON_DOWN});
    width: 11px;
    height: 7px;
}}

QDialog {{
    background-color: {COLORS["base"]};
    color: {COLORS["text_primary"]};
}}

QLabel#settingsHeader {{
    font-size: 18px;
    font-weight: 700;
    color: {COLORS["text_primary"]};
    padding-bottom: 4px;
}}

QDialog QPushButton {{
    background-color: transparent;
    color: {COLORS["text_secondary"]};
    border: none;
    border-radius: 8px;
    font-size: 13px;
    padding: 8px 16px;
    min-height: 32px;
}}

QDialog QPushButton:hover {{
    color: {COLORS["text_primary"]};
    background-color: {COLORS["hover"]};
}}

QPushButton#settingsOkBtn {{
    background-color: {COLORS["accent"]};
    color: {COLORS["download_text"]};
    font-weight: 700;
    padding: 8px 24px;
}}

QPushButton#settingsOkBtn:hover {{
    background-color: {COLORS["accent_hover"]};
    color: {COLORS["download_text"]};
}}

QPushButton#settingsOkBtn:pressed {{
    background-color: {COLORS["accent_pressed"]};
    color: {COLORS["download_text"]};
}}

QPushButton#settingsCancelBtn {{
    background-color: transparent;
    color: {COLORS["text_secondary"]};
}}

QPushButton#settingsCancelBtn:hover {{
    color: {COLORS["text_primary"]};
    background-color: {COLORS["hover"]};
}}

QDialogButtonBox {{
    dialogbuttonbox-buttons-have-icons: 0;
}}

QToolTip {{
    background-color: {COLORS["surface"]};
    color: {COLORS["text_primary"]};
    border: 1px solid {COLORS["input_border"]};
    border-radius: 6px;
    padding: 6px 10px;
    font-size: 12px;
}}
"""


def apply(app):
    """Apply the global Spotify-dark stylesheet and default font.

    Leads the family list with the platform's native UI font — San Francisco
    on macOS (i.e. the SF Pro look the design targets), Segoe UI on Windows —
    which always resolves, so Qt never hits the "missing font family" warning
    or the font-alias population cost on startup. The named faces remain as
    fallbacks for platforms whose system font isn't already one of them.
    """
    app.setStyleSheet(STYLESHEET)
    font = QFontDatabase.systemFont(QFontDatabase.GeneralFont)
    font.setPointSize(10)
    app.setFont(font)


class ThemedComboBox(QtWidgets.QComboBox):
    """QComboBox whose drop-down popup has no white macOS window frame.

    The popup is a separate top-level window, so the global stylesheet only
    reaches the inner item-view — leaving the rounded dark list sitting on an
    opaque white system window (the "white halo"/frame around the dropdown).
    Making that popup window frameless + translucent lets only the styled view
    show, with transparent corners instead of white.

    On macOS, WA_TranslucentBackground must be set *before* setWindowFlags:
    changing flags recreates the native window, and it is recreated opaque
    unless the translucency attributes are already in place.
    """

    def showPopup(self):
        super().showPopup()
        popup = self.view().window()
        popup.setAttribute(QtCore.Qt.WA_TranslucentBackground, True)
        popup.setAttribute(QtCore.Qt.WA_NoSystemBackground, True)
        popup.setWindowFlags(
            QtCore.Qt.Popup | QtCore.Qt.FramelessWindowHint | QtCore.Qt.NoDropShadowWindowHint
        )
        # setWindowFlags hides the window; re-show so the new flags take effect.
        popup.show()
