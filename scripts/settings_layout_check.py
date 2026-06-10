"""Repro/verify harness for the Settings-page squeeze bug.

Boots the real MainWindow offscreen at the default 900x580, switches to the
Settings page WITHOUT resizing (the user's repro), renders a screenshot, and
checks the geometry invariants that the bug violates:

  1. the SettingsPanel must not be squeezed below its own sizeHint height
     (squeeze = QFormLayout rows overlap because fixed-height fields can't
     shrink with the compressed row positions), and
  2. sibling cards/sections inside the panel must not overlap each other.

Exit 0 = layout sane, exit 1 = squeeze/overlap detected. Writes the rendered
PNGs next to itself for eyeballing.
"""

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt5.QtWidgets import QApplication  # noqa: E402

import Spotify_Downloader as S  # noqa: E402
import theme  # noqa: E402

OUT_DIR = os.path.dirname(os.path.abspath(__file__))


def check(win, label):
    app = QApplication.instance()
    app.processEvents()
    panel = win.settings_panel
    shot = os.path.join(OUT_DIR, f"settings_{label}.png")
    win.grab().save(shot)

    failures = []
    # 1. Panel squeezed below what its layout needs -> rows must overlap.
    need = panel.sizeHint().height()
    got = panel.height()
    if got < need - 1:
        failures.append(f"panel squeezed: height {got}px < sizeHint {need}px")

    # 2. Direct children of the panel's VBox must be stacked, never overlapping.
    body = panel.layout()
    rects = []
    for i in range(body.count()):
        w = body.itemAt(i).widget()
        if w is not None and w.isVisibleTo(panel):
            rects.append((w.objectName() or type(w).__name__, w.geometry()))
    for (na, a), (nb, b) in zip(rects, rects[1:]):
        if b.top() < a.bottom() - 1:
            failures.append(
                f"overlap: {nb} (y={b.top()}) starts above bottom of {na} (y={a.bottom()})"
            )

    print(f"[{label}] window={win.width()}x{win.height()} panel={got}px needed={need}px -> {shot}")
    for f in failures:
        print(f"  FAIL {f}")
    return failures


def check_wheel(win):
    """A wheel tick over a combo must scroll the page, never edit the value.

    Qt only walks a wheel event up the parent chain for *spontaneous* (real
    user) events, which sendEvent can't fabricate — so verify the two halves
    of that chain: the combo must leave the event unaccepted (that is what
    makes a real tick propagate), and the viewport must scroll on a wheel.
    """
    from PyQt5.QtCore import QPoint, QPointF, Qt
    from PyQt5.QtGui import QWheelEvent

    def wheel_at(widget):
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

    app = QApplication.instance()
    failures = []

    combo = win.settings_panel._source_cb
    idx_before = combo.currentIndex()
    ev = wheel_at(combo)
    app.sendEvent(combo, ev)
    if combo.currentIndex() != idx_before:
        failures.append("wheel over combo changed its value")
    if ev.isAccepted():
        failures.append("combo accepted the wheel (a real tick would not reach the page)")

    viewport = win._settings_scroll.viewport()
    scrollbar = win._settings_scroll.verticalScrollBar()
    pos_before = scrollbar.value()
    app.sendEvent(viewport, wheel_at(viewport))
    if scrollbar.value() <= pos_before:
        failures.append("viewport wheel did not scroll the page")

    print(
        f"[wheel] combo index {idx_before}->{combo.currentIndex()} "
        f"accepted={ev.isAccepted()}, viewport scroll {pos_before}->{scrollbar.value()}"
    )
    for f in failures:
        print(f"  FAIL {f}")
    return failures


def main():
    app = QApplication.instance() or QApplication([])
    theme.apply(app)  # match the real app so screenshots show themed rendering
    win = S.MainWindow()
    win.resize(900, 580)  # ui_main.py default launch size
    win.show()
    app.processEvents()

    # The user's exact repro: switch to Settings, no resize.
    win.SettingsBtn.setChecked(True)
    failures = check(win, "default_900x580")
    failures += check_wheel(win)

    # Also confirm a generously tall window stays sane (the "after resize" case).
    win.resize(1100, 1900)
    failures += check(win, "tall_1100x1900")

    win.close()
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
