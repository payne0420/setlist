"""Hand-authored main-window UI for Setlist — sidebar nav + stacked content.

Replaces the original pyuic5-generated ``Template.py`` layout. It is built
programmatically (not loaded from a ``.ui`` file) so the redesign isn't tied to
the stale ``Template.ui`` Designer artifact, which the app never loaded at
runtime anyway.

Every widget attribute name the download engine in ``Spotify_Downloader.py``
references is preserved (PlaylistLink, DownloadBtn, QueueBtn, SettingsBtn,
AlbumName, MainSongName, PlaylistMsg_2, showPreviewCheck, AddMetaDataCheck,
CounterLabel, statusMsg, SongDownloadprogress, CoverImg, SongName, YearText,
ArtistNameText, AlbumText, label_8, version, author), so the
existing signal/slot wiring keeps working on top of the new layout.
"""

from PyQt5 import QtCore, QtGui, QtWidgets

_POINTING = QtCore.Qt.PointingHandCursor

# Shared height for the URL field and the primary action buttons (Download,
# queue Start/Stop/Clear) so they line up across pages.
INPUT_H = 46


class Ui_MainWindow:
    def setupUi(self, MainWindow):
        MainWindow.setObjectName("MainWindow")
        MainWindow.resize(900, 580)
        MainWindow.setMinimumSize(QtCore.QSize(740, 520))

        self.centralwidget = QtWidgets.QWidget(MainWindow)
        self.centralwidget.setObjectName("centralwidget")
        root = QtWidgets.QHBoxLayout(self.centralwidget)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self._build_sidebar()
        root.addWidget(self.sidebar)

        self.content = QtWidgets.QStackedWidget(self.centralwidget)
        self.content.setObjectName("content")
        root.addWidget(self.content, 1)

        self._build_home_page()  # index 0
        self._build_queue_page()  # index 1
        self._build_history_page()  # index 2
        self._build_settings_page()  # index 3

        MainWindow.setCentralWidget(self.centralwidget)
        self.retranslateUi(MainWindow)
        QtCore.QMetaObject.connectSlotsByName(MainWindow)

    # ------------------------------------------------------------------ parts
    def _nav_button(self, name, checkable=True):
        b = QtWidgets.QPushButton(self.sidebar)
        b.setObjectName(name)
        b.setProperty("nav", True)
        b.setCheckable(checkable)
        b.setCursor(QtGui.QCursor(_POINTING))
        b.setMinimumHeight(40)
        return b

    def _build_sidebar(self):
        self.sidebar = QtWidgets.QFrame(self.centralwidget)
        self.sidebar.setObjectName("sidebar")
        self.sidebar.setFixedWidth(200)
        side = QtWidgets.QVBoxLayout(self.sidebar)
        side.setContentsMargins(16, 20, 16, 16)
        side.setSpacing(6)

        self.wordmark = QtWidgets.QLabel(self.sidebar)
        self.wordmark.setObjectName("wordmark")
        side.addWidget(self.wordmark)
        side.addSpacing(20)

        # Page-switching nav — all four are checkable and exclusive; each maps
        # to a page in the content stack (Settings is now a pane, not a dialog).
        self.navHome = self._nav_button("navHome")
        self.navQueue = self._nav_button("navQueue")
        self.navHistory = self._nav_button("navHistory")
        self.SettingsBtn = self._nav_button("SettingsBtn")

        self.navGroup = QtWidgets.QButtonGroup(self.sidebar)
        self.navGroup.setExclusive(True)
        for b in (self.navHome, self.navQueue, self.navHistory, self.SettingsBtn):
            self.navGroup.addButton(b)
            side.addWidget(b)
        self.navHome.setChecked(True)

        side.addStretch(1)

        self.version = QtWidgets.QLabel(self.sidebar)
        self.version.setObjectName("version")
        side.addWidget(self.version)
        self.author = QtWidgets.QLabel(self.sidebar)
        self.author.setObjectName("author")
        self.author.setWordWrap(True)
        side.addWidget(self.author)

    def _build_home_page(self):
        self.homePage = QtWidgets.QWidget()
        self.homePage.setObjectName("homePage")
        lay = QtWidgets.QVBoxLayout(self.homePage)
        lay.setContentsMargins(28, 24, 28, 24)
        lay.setSpacing(16)

        self.homeTitle = QtWidgets.QLabel(self.homePage)
        self.homeTitle.setObjectName("pageTitle")
        lay.addWidget(self.homeTitle)

        # URL + Download
        urlRow = QtWidgets.QHBoxLayout()
        urlRow.setSpacing(12)
        self.PlaylistLink = QtWidgets.QLineEdit(self.homePage)
        self.PlaylistLink.setObjectName("PlaylistLink")
        self.PlaylistLink.setFixedHeight(INPUT_H)
        self.PlaylistLink.setClearButtonEnabled(True)
        self.DownloadBtn = QtWidgets.QPushButton(self.homePage)
        self.DownloadBtn.setObjectName("DownloadBtn")
        self.DownloadBtn.setFixedHeight(INPUT_H)  # match the URL field height
        self.DownloadBtn.setMinimumWidth(130)
        self.DownloadBtn.setCursor(QtGui.QCursor(_POINTING))
        urlRow.addWidget(self.PlaylistLink, 1)
        urlRow.addWidget(self.DownloadBtn)
        lay.addLayout(urlRow)

        self.QueueBtn = QtWidgets.QPushButton(self.homePage)
        self.QueueBtn.setObjectName("QueueBtn")
        self.QueueBtn.setFixedHeight(INPUT_H)
        self.QueueBtn.setCursor(QtGui.QCursor(_POINTING))
        lay.addWidget(self.QueueBtn)

        # Now-playing card
        self.nowCard = QtWidgets.QFrame(self.homePage)
        self.nowCard.setObjectName("card")
        card = QtWidgets.QVBoxLayout(self.nowCard)
        card.setContentsMargins(16, 14, 16, 14)
        card.setSpacing(8)

        self.PlaylistMsg_2 = QtWidgets.QLabel(self.nowCard)
        self.PlaylistMsg_2.setObjectName("PlaylistMsg_2")
        card.addWidget(self.PlaylistMsg_2)

        self.MainSongName = QtWidgets.QLabel(self.nowCard)
        self.MainSongName.setObjectName("MainSongName")
        self.MainSongName.setWordWrap(True)
        card.addWidget(self.MainSongName)

        self.AlbumName = QtWidgets.QLabel(self.nowCard)
        self.AlbumName.setObjectName("AlbumName")
        self.AlbumName.setWordWrap(True)
        card.addWidget(self.AlbumName)

        # Inline preview (cover + meta), toggled by Show preview.
        self.previewBox = QtWidgets.QFrame(self.nowCard)
        self.previewBox.setObjectName("previewBox")
        prev = QtWidgets.QHBoxLayout(self.previewBox)
        prev.setContentsMargins(0, 4, 0, 4)
        prev.setSpacing(12)
        self.CoverImg = QtWidgets.QLabel(self.previewBox)
        self.CoverImg.setObjectName("CoverImg")
        self.CoverImg.setFixedSize(72, 72)
        self.CoverImg.setScaledContents(True)
        prev.addWidget(self.CoverImg, 0, QtCore.Qt.AlignTop)
        meta = QtWidgets.QVBoxLayout()
        meta.setSpacing(2)
        self.SongName = QtWidgets.QLabel(self.previewBox)
        self.SongName.setObjectName("SongName")
        self.SongName.setWordWrap(True)
        self.ArtistNameText = QtWidgets.QLabel(self.previewBox)
        self.ArtistNameText.setObjectName("ArtistNameText")
        self.ArtistNameText.setWordWrap(True)
        self.YearText = QtWidgets.QLabel(self.previewBox)
        self.YearText.setObjectName("YearText")
        meta.addWidget(self.SongName)
        meta.addWidget(self.ArtistNameText)
        meta.addWidget(self.YearText)
        meta.addStretch(1)
        prev.addLayout(meta, 1)
        card.addWidget(self.previewBox)
        self.previewBox.setVisible(False)

        cardDivider = QtWidgets.QFrame(self.nowCard)
        cardDivider.setObjectName("cardDivider")
        cardDivider.setFrameShape(QtWidgets.QFrame.HLine)
        cardDivider.setFixedHeight(1)
        card.addWidget(cardDivider)

        # Engine sets these but they have no place in the new layout — keep them
        # as hidden children so the existing setText calls are harmless.
        self.AlbumText = QtWidgets.QLabel(self.nowCard)
        self.AlbumText.setObjectName("AlbumText")
        self.AlbumText.hide()
        self.label_8 = QtWidgets.QLabel(self.nowCard)
        self.label_8.setObjectName("label_8")
        self.label_8.hide()

        # Options + counter
        optRow = QtWidgets.QHBoxLayout()
        optRow.setSpacing(18)
        self.showPreviewCheck = QtWidgets.QCheckBox(self.nowCard)
        self.showPreviewCheck.setObjectName("showPreviewCheck")
        self.AddMetaDataCheck = QtWidgets.QCheckBox(self.nowCard)
        self.AddMetaDataCheck.setObjectName("AddMetaDataCheck")
        optRow.addWidget(self.showPreviewCheck)
        optRow.addWidget(self.AddMetaDataCheck)
        optRow.addStretch(1)
        self.CounterLabel = QtWidgets.QLabel(self.nowCard)
        self.CounterLabel.setObjectName("CounterLabel")
        optRow.addWidget(self.CounterLabel)
        card.addLayout(optRow)

        self.statusMsg = QtWidgets.QLabel(self.nowCard)
        self.statusMsg.setObjectName("statusMsg")
        self.statusMsg.setWordWrap(True)
        card.addWidget(self.statusMsg)

        self.SongDownloadprogress = QtWidgets.QProgressBar(self.nowCard)
        self.SongDownloadprogress.setObjectName("SongDownloadprogress")
        self.SongDownloadprogress.setMinimumHeight(6)
        self.SongDownloadprogress.setMaximumHeight(6)
        self.SongDownloadprogress.setTextVisible(False)
        self.SongDownloadprogress.setValue(0)
        card.addWidget(self.SongDownloadprogress)

        lay.addWidget(self.nowCard)

        # Live per-track download list
        self.tracksTitle = QtWidgets.QLabel(self.homePage)
        self.tracksTitle.setObjectName("sectionLabel")
        lay.addWidget(self.tracksTitle)
        self.trackList = QtWidgets.QListWidget(self.homePage)
        self.trackList.setObjectName("trackList")
        self.trackList.setSelectionMode(QtWidgets.QAbstractItemView.NoSelection)
        self.trackList.setFocusPolicy(QtCore.Qt.NoFocus)
        lay.addWidget(self.trackList, 1)

        self.content.addWidget(self.homePage)

    def _build_queue_page(self):
        self.queuePage = QtWidgets.QWidget()
        self.queuePage.setObjectName("queuePage")
        self.queuePageLayout = QtWidgets.QVBoxLayout(self.queuePage)
        self.queuePageLayout.setContentsMargins(28, 24, 28, 24)
        self.queuePageLayout.setSpacing(16)
        self.queueTitle = QtWidgets.QLabel(self.queuePage)
        self.queueTitle.setObjectName("pageTitle")
        self.queuePageLayout.addWidget(self.queueTitle)
        # The QueuePanel widget is injected here by the controller.
        self.content.addWidget(self.queuePage)

    def _build_history_page(self):
        self.historyPage = QtWidgets.QWidget()
        self.historyPage.setObjectName("historyPage")
        lay = QtWidgets.QVBoxLayout(self.historyPage)
        lay.setContentsMargins(28, 24, 28, 24)
        lay.setSpacing(16)
        titleRow = QtWidgets.QHBoxLayout()
        self.historyTitle = QtWidgets.QLabel(self.historyPage)
        self.historyTitle.setObjectName("pageTitle")
        titleRow.addWidget(self.historyTitle)
        titleRow.addStretch(1)
        self.clearHistoryBtn = QtWidgets.QPushButton(self.historyPage)
        self.clearHistoryBtn.setObjectName("QueueBtn")
        self.clearHistoryBtn.setMinimumHeight(32)
        self.clearHistoryBtn.setCursor(QtGui.QCursor(_POINTING))
        titleRow.addWidget(self.clearHistoryBtn)
        lay.addLayout(titleRow)
        self.historyHint = QtWidgets.QLabel(self.historyPage)
        self.historyHint.setObjectName("sectionLabel")
        lay.addWidget(self.historyHint)
        self.historyList = QtWidgets.QListWidget(self.historyPage)
        self.historyList.setObjectName("trackList")
        self.historyList.setSelectionMode(QtWidgets.QAbstractItemView.NoSelection)
        self.historyList.setFocusPolicy(QtCore.Qt.NoFocus)
        lay.addWidget(self.historyList, 1)
        self.content.addWidget(self.historyPage)

    def _build_settings_page(self):
        self.settingsPage = QtWidgets.QWidget()
        self.settingsPage.setObjectName("settingsPage")
        self.settingsPageLayout = QtWidgets.QVBoxLayout(self.settingsPage)
        self.settingsPageLayout.setContentsMargins(28, 24, 28, 24)
        self.settingsPageLayout.setSpacing(16)
        self.settingsTitle = QtWidgets.QLabel(self.settingsPage)
        self.settingsTitle.setObjectName("pageTitle")
        self.settingsPageLayout.addWidget(self.settingsTitle)
        # The SettingsPanel widget is injected here by the controller.
        self.content.addWidget(self.settingsPage)

    # ------------------------------------------------------------------ text
    def retranslateUi(self, MainWindow):
        _t = QtCore.QCoreApplication.translate
        MainWindow.setWindowTitle(_t("MainWindow", "Setlist"))
        self.wordmark.setText(_t("MainWindow", "♪  Setlist"))
        self.navHome.setText(_t("MainWindow", "Home"))
        self.navQueue.setText(_t("MainWindow", "Queue"))
        self.navHistory.setText(_t("MainWindow", "History"))
        self.SettingsBtn.setText(_t("MainWindow", "Settings"))
        self.version.setText(_t("MainWindow", "v2.2.0"))
        self.author.setText(_t("MainWindow", "A fork of Sunnify by Sunny Patel"))

        self.homeTitle.setText(_t("MainWindow", "Home"))
        self.PlaylistLink.setPlaceholderText(
            _t("MainWindow", "Paste a Spotify playlist, album, or track URL")
        )
        self.DownloadBtn.setText(_t("MainWindow", "Download"))
        self.QueueBtn.setText(_t("MainWindow", "⬇  Add to Download Queue"))
        self.PlaylistMsg_2.setText(_t("MainWindow", "NOW PLAYING"))
        self.MainSongName.setText(_t("MainWindow", "Nothing playing"))
        self.AlbumName.setText(_t("MainWindow", "Paste a Spotify link to get started"))
        self.showPreviewCheck.setText(_t("MainWindow", "Show preview"))
        self.AddMetaDataCheck.setText(_t("MainWindow", "Add meta tags"))
        self.CounterLabel.setText(_t("MainWindow", "Songs downloaded 0"))
        self.statusMsg.setText(_t("MainWindow", "Idle"))
        self.tracksTitle.setText(_t("MainWindow", "TRACKS"))

        self.queueTitle.setText(_t("MainWindow", "Queue"))
        self.historyTitle.setText(_t("MainWindow", "History"))
        self.clearHistoryBtn.setText(_t("MainWindow", "Clear history"))
        self.historyHint.setText(_t("MainWindow", "COMPLETED THIS SESSION"))
        self.settingsTitle.setText(_t("MainWindow", "Settings"))
