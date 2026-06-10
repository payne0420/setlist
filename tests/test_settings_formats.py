"""Settings-page per-source format behavior.

The Audio format dropdown only offers what the selected source can honestly
deliver (SOURCE_FORMATS): YouTube transcodes its lossy stream, so the lossless
containers are not offered; the lossless and librespot sources pin their native
.flac / .ogg. Dependent controls grey out per source: bitrate is YouTube-only,
the lossless quality tier (now in the Output card) is Real-FLAC-only.
"""

import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt5.QtWidgets import QApplication

import Spotify_Downloader as S


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def win(app, tmp_path, monkeypatch):
    cfg = tmp_path / "config.json"
    # Consent pre-seeded so switching to librespot doesn't pop the modal dialog.
    cfg.write_text(json.dumps({"format": "m4a", "librespot_consented": True}))
    monkeypatch.setattr(S, "_config_path", lambda: str(cfg))
    w = S.MainWindow()
    yield w
    w.close()


def _select_source(panel, value):
    idx = next(i for i, (_, v) in enumerate(panel._source_options) if v == value)
    panel._source_cb.setCurrentIndex(idx)


def _format_items(panel):
    return [panel._format_cb.itemText(i) for i in range(panel._format_cb.count())]


def test_youtube_offers_lossy_formats_only(win):
    panel = win.settings_panel
    assert _format_items(panel) == list(S.SOURCE_FORMATS["youtube"])
    assert panel._format_cb.currentText() == "m4a"  # persisted choice kept
    assert panel._format_cb.isEnabled()
    assert panel._quality_cb.isEnabled()
    assert not panel._lossless_quality_cb.isEnabled()
    # Row labels grey out together with their fields.
    assert panel._format_lbl.isEnabled()
    assert panel._quality_lbl.isEnabled()
    assert not panel._lossless_quality_lbl.isEnabled()
    assert not panel._tidal_api_lbl.isEnabled()


def test_lossless_source_pins_flac(win):
    panel = win.settings_panel
    _select_source(panel, "lossless")
    assert _format_items(panel) == ["flac"]
    assert not panel._format_cb.isEnabled()  # pinned, not a choice
    assert not panel._quality_cb.isEnabled()  # bitrate is YouTube-only
    assert panel._lossless_quality_cb.isEnabled()
    assert win._config["format"] == "flac"
    assert not panel._format_lbl.isEnabled()
    assert not panel._quality_lbl.isEnabled()
    assert panel._lossless_quality_lbl.isEnabled()
    assert panel._tidal_api_lbl.isEnabled()


def test_librespot_source_pins_ogg(win):
    panel = win.settings_panel
    _select_source(panel, "librespot")
    assert _format_items(panel) == ["ogg"]
    assert not panel._format_cb.isEnabled()
    assert not panel._quality_cb.isEnabled()  # native ~320k; fallback keeps source codec
    assert not panel._lossless_quality_cb.isEnabled()
    assert win._config["format"] == "ogg"
    assert not panel._format_lbl.isEnabled()
    assert not panel._quality_lbl.isEnabled()
    assert not panel._lossless_quality_lbl.isEnabled()


def test_switching_back_to_youtube_restores_lossy_default(win):
    panel = win.settings_panel
    _select_source(panel, "lossless")
    _select_source(panel, "youtube")
    assert _format_items(panel) == list(S.SOURCE_FORMATS["youtube"])
    assert panel._format_cb.currentText() == "mp3"  # flac not offered -> default
    assert panel._format_cb.isEnabled()
    assert panel._quality_cb.isEnabled()
    assert not panel._lossless_quality_cb.isEnabled()


def test_youtube_offers_original_and_disables_quality(win):
    panel = win.settings_panel
    assert "original" in _format_items(panel)
    idx = panel._format_cb.findText("original")
    panel._format_cb.setCurrentIndex(idx)
    assert not panel._quality_cb.isEnabled()
    assert not panel._quality_lbl.isEnabled()
    assert win._config["format"] == "original"
    idx = panel._format_cb.findText("mp3")
    panel._format_cb.setCurrentIndex(idx)
    assert panel._quality_cb.isEnabled()
    assert panel._quality_lbl.isEnabled()
