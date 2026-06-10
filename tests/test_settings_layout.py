"""Settings-page layout regression tests.

Regression: the SettingsPanel is taller than the default 900x580 window and
used to sit directly on its stacked page — switching to Settings squeezed the
QFormLayout rows below their fixed heights so they painted on top of each
other until the window was manually resized. The panel now lives in a scroll
area, and wheel input over a combo/spin scrolls the page instead of editing
the setting under the cursor.
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt5.QtCore import QPoint, QPointF, Qt
from PyQt5.QtGui import QWheelEvent
from PyQt5.QtWidgets import QApplication

import Spotify_Downloader as S


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def win(app):
    w = S.MainWindow()
    w.resize(900, 580)  # the ui_main.py launch size that triggered the bug
    w.show()
    app.processEvents()
    w.SettingsBtn.setChecked(True)  # the repro: switch to Settings, no resize
    app.processEvents()
    yield w
    w.close()


def _wheel_at(widget):
    return QWheelEvent(
        QPointF(10, 10),
        QPointF(widget.mapToGlobal(QPoint(10, 10))),
        QPoint(0, -120),
        QPoint(0, -120),
        Qt.NoButton,
        Qt.NoModifier,
        Qt.NoScrollPhase,
        False,
    )


def test_panel_gets_full_height_in_short_window(win):
    # Squeezed panel == overlapping rows: fixed-height fields can't shrink
    # with compressed row positions, so anything below sizeHint is the bug.
    panel = win.settings_panel
    assert panel.height() >= panel.sizeHint().height() - 1


def test_panel_children_never_overlap(win):
    panel = win.settings_panel
    body = panel.layout()
    rects = [
        body.itemAt(i).widget().geometry()
        for i in range(body.count())
        if body.itemAt(i).widget() is not None and body.itemAt(i).widget().isVisibleTo(panel)
    ]
    for above, below in zip(rects, rects[1:]):
        assert below.top() >= above.bottom() - 1


def test_settings_page_scrolls_in_short_window(win):
    scrollbar = win._settings_scroll.verticalScrollBar()
    assert scrollbar.maximum() > 0  # content taller than viewport -> scrollable
    QApplication.instance().sendEvent(
        win._settings_scroll.viewport(), _wheel_at(win._settings_scroll.viewport())
    )
    assert scrollbar.value() > 0


def test_wheel_over_combo_refused_and_value_unchanged(win):
    # Qt propagates only *spontaneous* wheel events to the parent chain, so
    # assert the combo's half of the contract: value untouched and the event
    # left unaccepted (which is what routes a real tick to the scroll area).
    combo = win.settings_panel._source_cb
    before = combo.currentIndex()
    ev = _wheel_at(combo)
    QApplication.instance().sendEvent(combo, ev)
    assert combo.currentIndex() == before
    assert not ev.isAccepted()


def test_wheel_over_spinbox_refused_and_value_unchanged(win):
    spin = win.settings_panel._max_extended_minutes_spin
    before = spin.value()
    ev = _wheel_at(spin)
    QApplication.instance().sendEvent(spin, ev)
    assert spin.value() == before
    assert not ev.isAccepted()
