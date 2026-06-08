"""State-machine tests for MainWindow's queue controller.

These exercise the sequential orchestration (advance / halt / finalize / mode
transitions) with ``_start_scraper`` stubbed out, so NO real ScraperThread or
network is involved — only the controller logic the design critique flagged as
the risky part. The real Qt signal wiring is covered by the headless smoke
launch in the verify step.
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt5.QtWidgets import QApplication

import Spotify_Downloader as S
from download_queue import ACTIVE, CANCELLED, DONE, FAILED

PL = "https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M"
AL = "https://open.spotify.com/album/4aawyAB9vmqN3uQ7FjRGTy"
TR = "https://open.spotify.com/track/2D9coh76MCXNqDEUCHl5vl"


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def win(app, tmp_path):
    w = S.MainWindow()
    w.download_path = str(tmp_path)
    w._download_path_set = True
    # Capture launches instead of starting real threads.
    w._launches = []

    def fake_start(url, cancel_event, on_item_finished, item_id=None):
        w._launches.append({"url": url, "cb": on_item_finished, "item_id": item_id})

    w._start_scraper = fake_start
    yield w
    w.deleteLater()


def _drain(app):
    # Fire any pending QTimer.singleShot(0, ...) deferred advances.
    for _ in range(5):
        app.processEvents()


def test_start_empty_queue_is_noop(win):
    win.start_queue()
    assert win._mode == "idle"
    assert win._launches == []


def test_start_launches_first_item_only(win):
    win.queue_add_text(f"{PL}\n{AL}")
    win.start_queue()
    assert win._mode == "queue"
    assert win.DownloadBtn.text() == "Stop Queue"
    assert len(win._launches) == 1
    item0 = win._queue.items[0]
    assert item0.status == ACTIVE
    assert win._launches[0]["item_id"] == item0.id
    # The second item is not started until the first finishes.
    assert win._queue.items[1].status != ACTIVE


def test_sequential_advance_to_completion(win, app):
    win.queue_add_text(f"{PL}\n{AL}")
    win.start_queue()
    win._launches[0]["cb"](DONE, "Download Complete!")
    _drain(app)
    assert win._queue.items[0].status == DONE
    assert len(win._launches) == 2
    assert win._queue.items[1].status == ACTIVE

    win._launches[1]["cb"](DONE, "Download Complete!")
    _drain(app)
    assert win._queue.items[1].status == DONE
    assert win._mode == "idle"
    assert win.DownloadBtn.text() == "Download"
    assert win.SettingsBtn.isEnabled()


def test_failure_still_advances(win, app):
    win.queue_add_text(f"{PL}\n{AL}")
    win.start_queue()
    win._launches[0]["cb"](FAILED, "Download failed - no audio file produced")
    _drain(app)
    assert win._queue.items[0].status == FAILED
    assert win._queue.items[1].status == ACTIVE  # failure does not halt the queue


def test_stop_halts_and_cancels_remaining(win, app):
    win.queue_add_text(f"{PL}\n{AL}\n{TR}")
    win.start_queue()
    launches_at_stop = len(win._launches)
    win.stop_queue()
    assert win._queue_halted is True
    # Not-yet-started items are cancelled immediately; active one still running.
    assert win._queue.items[1].status == CANCELLED
    assert win._queue.items[2].status == CANCELLED
    assert win._queue.items[0].status == ACTIVE
    # The active item reports cancelled -> finalize, NO advance to a new item.
    win._launches[0]["cb"](CANCELLED, "Download cancelled")
    _drain(app)
    assert win._mode == "idle"
    assert len(win._launches) == launches_at_stop
    assert win.DownloadBtn.text() == "Download"


def test_clear_blocked_while_running(win):
    win.queue_add_text(PL)
    win.start_queue()
    win.clear_queue()  # must be refused while a queue is active
    assert len(win._queue) == 1


def test_clear_when_idle(win):
    win.queue_add_text(f"{PL}\n{AL}")
    assert len(win._queue) == 2
    win.clear_queue()
    assert win._queue.is_empty()


def test_single_download_sets_then_resets_mode(win, app):
    win.PlaylistLink.setText(PL)
    win.on_returnButton()
    assert win._mode == "single"
    assert len(win._launches) == 1
    assert win._launches[0]["item_id"] is None  # single path, no queue item
    win._launches[0]["cb"](DONE, "Download Complete!")
    _drain(app)
    assert win._mode == "idle"
    assert win.DownloadBtn.text() == "Download"


def test_start_queue_blocked_during_single(win):
    win.PlaylistLink.setText(PL)
    win.on_returnButton()  # enter single mode
    win.queue_add_text(AL)
    win.start_queue()  # should be refused
    assert win._mode == "single"


def test_single_stop_is_not_reported_as_queue_running(win):
    win.PlaylistLink.setText(PL)
    win.on_returnButton()  # single mode
    win._stop_download()  # -> stopping (from single)
    assert win._mode == "stopping"
    assert win._stopping_from == "single"
    assert win.queue_is_running() is False


def test_queue_stop_is_reported_as_queue_running(win):
    win.queue_add_text(f"{PL}\n{AL}")
    win.start_queue()
    win.stop_queue()  # -> stopping (from queue)
    assert win._mode == "stopping"
    assert win.queue_is_running() is True
