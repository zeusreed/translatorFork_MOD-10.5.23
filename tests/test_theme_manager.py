# tests/test_theme_manager.py
import unittest
from unittest.mock import MagicMock
from PyQt6 import QtCore, QtWidgets
from gemini_translator.ui import theme_manager as tm


def test_normalize_mode_accepts_valid_and_falls_back():
    assert tm.normalize_mode("light") == "light"
    assert tm.normalize_mode("dark") == "dark"
    assert tm.normalize_mode("auto") == "auto"
    assert tm.normalize_mode("custom") == "custom"
    assert tm.normalize_mode("nonsense") == "auto"
    assert tm.normalize_mode(None) == "auto"


def test_resolve_scheme():
    assert tm.resolve_scheme("light", system_is_dark=True) == "light"
    assert tm.resolve_scheme("dark", system_is_dark=False) == "dark"
    assert tm.resolve_scheme("auto", system_is_dark=True) == "dark"
    assert tm.resolve_scheme("auto", system_is_dark=False) == "light"


from gemini_translator.ui import themes


def test_resolve_base_colors_uses_preset_for_scheme():
    base = tm.resolve_base_colors("light")
    assert base["window_bg"] == themes.LIGHT_DEFAULT_THEME_COLORS["window_bg"]


def test_standard_accent_is_kept_when_no_manual_accent():
    base = tm.resolve_base_colors("dark", manual_colors={}, system_accent="#3478f6")
    assert base["accent"] == themes.DARK_DEFAULT_THEME_COLORS["accent"]


def test_manual_accent_overrides_system():
    base = tm.resolve_base_colors(
        "dark", manual_colors={"accent": "#abcdef"}, system_accent="#3478f6"
    )
    assert base["accent"] == "#abcdef"


def test_resolve_base_colors_always_complete():
    base = tm.resolve_base_colors("light")
    assert set(base) == {"window_bg", "panel_bg", "accent"}


def test_preset_equal_manual_colors_do_not_pin_theme():
    # A profile can hold the dark defaults as "manual colours" (legacy auto-save);
    # they must NOT block a light switch.
    base = tm.resolve_base_colors(
        "light", manual_colors=dict(themes.DARK_DEFAULT_THEME_COLORS)
    )
    assert base["window_bg"] == themes.LIGHT_DEFAULT_THEME_COLORS["window_bg"]


def test_custom_overrides_drops_preset_equal_keeps_custom():
    mixed = dict(themes.DARK_DEFAULT_THEME_COLORS)
    mixed["accent"] = "#abcdef"
    assert tm.custom_overrides(mixed) == {"accent": "#abcdef"}


def test_migrate_uses_explicit_mode_when_present():
    assert tm.migrate_theme_mode({"ui_theme_mode": "light"}) == "light"
    assert tm.migrate_theme_mode({"ui_theme_mode": "bogus"}) == "auto"


def test_migrate_existing_profile_defaults_to_dark():
    # Non-empty settings without a mode key = a pre-existing user (was on dark).
    assert tm.migrate_theme_mode({"api_keys_with_status": [{"key": "x"}]}) == "dark"


def test_migrate_fresh_profile_defaults_to_auto():
    assert tm.migrate_theme_mode({}) == "auto"
    assert tm.migrate_theme_mode(None) == "auto"


class FakeSettings:
    def __init__(self, data=None):
        self.data = dict(data or {})
    def load_settings(self):
        return dict(self.data)
    def save_settings(self, d):
        self.data = dict(d)
        return True


def test_load_mode_uses_migration():
    assert tm.load_mode(FakeSettings({"api_keys_with_status": [1]})) == "dark"
    assert tm.load_mode(FakeSettings({})) == "auto"
    assert tm.load_mode(FakeSettings({"ui_theme_mode": "light"})) == "light"


def test_save_mode_persists():
    s = FakeSettings({})
    tm.save_mode(s, "light")
    assert s.data["ui_theme_mode"] == "light"


def test_save_mode_preserves_custom_mode():
    s = FakeSettings({})
    tm.save_mode(s, "custom")
    assert s.data["ui_theme_mode"] == "custom"


def test_system_is_dark_reads_color_scheme(qapp):
    hints = MagicMock()
    hints.colorScheme.return_value = QtCore.Qt.ColorScheme.Dark
    fake_app = MagicMock()
    fake_app.styleHints.return_value = hints
    assert tm.system_is_dark(fake_app) is True
    hints.colorScheme.return_value = QtCore.Qt.ColorScheme.Light
    assert tm.system_is_dark(fake_app) is False

def test_system_accent_returns_hex(qapp):
    accent = tm.system_accent(qapp)
    assert accent is None or accent.startswith("#")

def test_apply_light_sets_light_stylesheet(qapp):
    tm.apply(qapp, mode="light", manual_colors={})
    sheet = qapp.styleSheet()
    assert themes.LIGHT_DEFAULT_THEME_COLORS["window_bg"] in sheet

def test_apply_dark_sets_dark_stylesheet(qapp):
    tm.apply(qapp, mode="dark", manual_colors={})
    sheet = qapp.styleSheet()
    assert themes.DARK_DEFAULT_THEME_COLORS["window_bg"] in sheet

def test_install_reapplies_on_real_color_scheme_signal(qapp):
    # Connects via install() to the REAL QStyleHints.colorSchemeChanged and
    # emits it, so the actual wiring is exercised (a wrong signal name would
    # never reach the handler). Auto re-applies; a fixed mode does not.
    calls = []
    orig_apply = tm.apply
    try:
        tm.apply = lambda *a, **k: calls.append(k.get("mode"))
        tm.install(qapp, FakeSettings({"ui_theme_mode": "auto"}))

        setattr(qapp, "_active_theme_mode", "auto")
        qapp.styleHints().colorSchemeChanged.emit(QtCore.Qt.ColorScheme.Dark)
        assert "auto" in calls

        calls.clear()
        setattr(qapp, "_active_theme_mode", "dark")
        qapp.styleHints().colorSchemeChanged.emit(QtCore.Qt.ColorScheme.Light)
        assert calls == []
    finally:
        tm.apply = orig_apply
        setattr(qapp, "_active_theme_mode", "auto")
