"""Settings-page fallback chain combo behavior."""

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
    cfg.write_text(json.dumps({"librespot_consented": True}))
    monkeypatch.setattr(S, "_config_path", lambda: str(cfg))
    w = S.MainWindow()
    yield w
    w.close()


def _select_source(panel, value):
    idx = next(i for i, (_, v) in enumerate(panel._source_options) if v == value)
    panel._source_cb.setCurrentIndex(idx)


def _fallback_labels(panel):
    return [panel._fallback_cb.itemText(i) for i in range(panel._fallback_cb.count())]


def test_youtube_fallback_row_disabled(win):
    panel = win.settings_panel
    _select_source(panel, "youtube")
    assert not panel._fallback_cb.isEnabled()
    assert not panel._fallback_lbl.isEnabled()
    assert _fallback_labels(panel) == ["No fallback — YouTube is the last resort"]


def test_lossless_fallback_options(win):
    panel = win.settings_panel
    _select_source(panel, "lossless")
    assert panel._fallback_cb.isEnabled()
    labels = _fallback_labels(panel)
    assert "Nothing — skip track" in labels
    assert "Spotify, then YouTube" in labels
    assert "YouTube, then Spotify" in labels


def test_librespot_fallback_options(win):
    panel = win.settings_panel
    _select_source(panel, "librespot")
    assert _fallback_labels(panel) == ["Nothing — skip track", "YouTube"]


def test_youtube_to_lossless_restores_default_chain(win):
    panel = win.settings_panel
    _select_source(panel, "lossless")
    assert win._config["fallback_order"] == ["youtube"]


def test_lossless_to_youtube_clears_chain(win):
    panel = win.settings_panel
    _select_source(panel, "lossless")
    _select_source(panel, "youtube")
    assert win._config["fallback_order"] == []


def test_old_checkbox_attributes_gone(win):
    panel = win.settings_panel
    assert not hasattr(panel, "_lossless_fallback_cb")
    assert not hasattr(panel, "_ext_yt_fallback_cb")


def test_fallback_persists_round_trip(win):
    panel = win.settings_panel
    _select_source(panel, "lossless")
    idx = next(i for i, lbl in enumerate(_fallback_labels(panel)) if lbl == "Nothing — skip track")
    panel._fallback_cb.setCurrentIndex(idx)
    assert win._config["fallback_order"] == []


def test_youtube_card_enabled_when_youtube_in_chain(win):
    panel = win.settings_panel
    _select_source(panel, "lossless")
    assert panel._youtube_card.isEnabled()
    idx = next(i for i, lbl in enumerate(_fallback_labels(panel)) if lbl == "Nothing — skip track")
    panel._fallback_cb.setCurrentIndex(idx)
    assert not panel._youtube_card.isEnabled()


def test_spotify_card_enabled_when_librespot_in_chain(win):
    panel = win.settings_panel
    _select_source(panel, "lossless")
    idx = next(i for i, lbl in enumerate(_fallback_labels(panel)) if lbl == "Spotify, then YouTube")
    panel._fallback_cb.setCurrentIndex(idx)
    assert panel._spotify_card.isEnabled()


def test_fallback_consent_decline_reverts(win, monkeypatch):
    cfg = win._config
    cfg["librespot_consented"] = False
    panel = win.settings_panel
    _select_source(panel, "lossless")
    prev = list(win._config["fallback_order"])

    def fake_confirm(_self):
        return False

    monkeypatch.setattr(S.SettingsPanel, "_confirm_librespot_consent", fake_confirm)
    idx = next(i for i, lbl in enumerate(_fallback_labels(panel)) if lbl == "Spotify")
    panel._fallback_cb.setCurrentIndex(idx)
    assert win._config["fallback_order"] == prev
