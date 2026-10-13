"""Centralized window-title branding for translatorFork_MOD."""

import os

APP_WINDOW_BRAND = "translatorFork_MOD"
_DEFAULT_PROFILE_ALIASES = {"", "default", "global", "main"}


def _profile_brand_suffix():
    profile = str(os.environ.get("GT_SETTINGS_PROFILE", "") or "").strip()
    if profile.lower() not in _DEFAULT_PROFILE_ALIASES:
        return f" [{profile}]"
    if str(os.environ.get("GT_SETTINGS_DIR", "") or "").strip():
        return " [isolated]"
    return ""


def app_window_brand():
    return f"{APP_WINDOW_BRAND}{_profile_brand_suffix()}"


def rebrand_window_title(title):
    brand = app_window_brand()
    text = "" if title is None else str(title).strip()
    if not text:
        return brand
    if text == brand or text.startswith(brand):
        return text
    return f"{brand} - {text}"


def install_window_title_branding(app=None):
    from PyQt6 import QtWidgets

    widget_class = QtWidgets.QWidget
    if not getattr(widget_class, "_translatorfork_mod_title_patch_installed", False):
        original_set_window_title = widget_class.setWindowTitle

        def branded_set_window_title(self, title):
            original_set_window_title(self, rebrand_window_title(title))

        widget_class.setWindowTitle = branded_set_window_title
        widget_class._translatorfork_mod_title_patch_installed = True

    application = app or QtWidgets.QApplication.instance()
    if application is not None:
        brand = app_window_brand()
        application.setApplicationName(brand)
        if hasattr(application, "setApplicationDisplayName"):
            application.setApplicationDisplayName(brand)
        for widget in application.topLevelWidgets():
            widget.setWindowTitle(widget.windowTitle())
