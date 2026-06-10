"""Settings-page YouTube Premium cookies card behavior."""

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


def _valid_cookie_file(tmp_path):
    p = tmp_path / "cookies.txt"
    p.write_text("# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tsecret\n")
    return str(p)


def test_youtube_card_enabled_for_primary_or_chain(win):
    panel = win.settings_panel
    _select_source(panel, "youtube")
    assert panel._youtube_card.isEnabled()
    _select_source(panel, "lossless")
    assert panel._youtube_card.isEnabled()  # default chain includes youtube
    _select_source(panel, "librespot")
    assert panel._youtube_card.isEnabled()  # default chain includes youtube
    labels = [panel._fallback_cb.itemText(j) for j in range(panel._fallback_cb.count())]
    idx = labels.index("Nothing — skip track")
    panel._fallback_cb.setCurrentIndex(idx)
    assert not panel._youtube_card.isEnabled()
    _select_source(panel, "youtube")
    assert panel._youtube_card.isEnabled()


def test_valid_cookie_path_persists(win, tmp_path):
    panel = win.settings_panel
    path = _valid_cookie_file(tmp_path)
    panel._yt_cookies_field.setText(path)
    panel._save_youtube_cookies()
    assert win._config["youtube_cookies_file"] == path


def test_invalid_cookie_path_not_persisted(win, tmp_path):
    panel = win.settings_panel
    good = _valid_cookie_file(tmp_path)
    panel._yt_cookies_field.setText(good)
    panel._save_youtube_cookies()
    bad = tmp_path / "bad.txt"
    bad.write_text('{"json": "garbage"}')
    panel._yt_cookies_field.setText(str(bad))
    panel._save_youtube_cookies()
    assert win._config["youtube_cookies_file"] == good
    assert "Not saved" in panel._yt_cookies_status_lbl.text()


def test_json_cookie_export_accepted_by_panel(win, tmp_path):
    panel = win.settings_panel
    path = tmp_path / "cookies.json"
    path.write_text('[{"name": "SID", "value": "secret", "domain": ".youtube.com"}]')
    panel._yt_cookies_field.setText(str(path))
    panel._save_youtube_cookies()
    assert win._config["youtube_cookies_file"] == str(path)


def test_clearing_field_disables(win, tmp_path):
    panel = win.settings_panel
    path = _valid_cookie_file(tmp_path)
    panel._yt_cookies_field.setText(path)
    panel._save_youtube_cookies()
    panel._yt_cookies_field.setText("")
    panel._save_youtube_cookies()
    assert win._config["youtube_cookies_file"] == ""
