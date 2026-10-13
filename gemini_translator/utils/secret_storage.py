"""Small OS-bound secret protection helpers for persisted application settings."""

from __future__ import annotations

import base64
import ctypes
import os
from ctypes import wintypes


_DPAPI_PREFIX = "dpapi:v1:"
_MAX_SECRET_BYTES = 64 * 1024


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def _dpapi_transform(value: bytes, *, protect: bool) -> bytes:
    if os.name != "nt":
        raise RuntimeError("Windows DPAPI is only available on Windows")
    if len(value) > _MAX_SECRET_BYTES:
        raise ValueError("Secret is too large to protect")

    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    source_buffer = ctypes.create_string_buffer(value, max(1, len(value)))
    source = _DataBlob(len(value), ctypes.cast(source_buffer, ctypes.POINTER(ctypes.c_ubyte)))
    result = _DataBlob()
    if protect:
        function = crypt32.CryptProtectData
        function.argtypes = [
            ctypes.POINTER(_DataBlob), wintypes.LPCWSTR, ctypes.POINTER(_DataBlob),
            ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_DataBlob),
        ]
        function.restype = wintypes.BOOL
        ok = function(ctypes.byref(source), "TranslatorFork setting", None, None, None, 0x1, ctypes.byref(result))
    else:
        function = crypt32.CryptUnprotectData
        function.argtypes = [
            ctypes.POINTER(_DataBlob), ctypes.POINTER(wintypes.LPWSTR), ctypes.POINTER(_DataBlob),
            ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_DataBlob),
        ]
        function.restype = wintypes.BOOL
        description = wintypes.LPWSTR()
        ok = function(ctypes.byref(source), ctypes.byref(description), None, None, None, 0x1, ctypes.byref(result))
        if description:
            kernel32.LocalFree(ctypes.cast(description, ctypes.c_void_p))

    if not ok:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(result.pbData, result.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(result.pbData, ctypes.c_void_p))


def protect_secret(value: str) -> str:
    """Protect a string for the current Windows user; keep other OS formats compatible."""
    if not isinstance(value, str) or not value or os.name != "nt":
        return value
    if value.startswith(_DPAPI_PREFIX):
        return value
    encrypted = _dpapi_transform(value.encode("utf-8"), protect=True)
    return _DPAPI_PREFIX + base64.b64encode(encrypted).decode("ascii")


def unprotect_secret(value: str) -> str:
    if not isinstance(value, str) or not value.startswith(_DPAPI_PREFIX):
        return value
    if os.name != "nt":
        raise RuntimeError("This API key was protected for a Windows user and cannot be read on this system")
    encoded = value[len(_DPAPI_PREFIX):]
    encrypted = base64.b64decode(encoded, validate=True)
    if len(encrypted) > _MAX_SECRET_BYTES:
        raise ValueError("Protected secret is too large")
    return _dpapi_transform(encrypted, protect=False).decode("utf-8")


def transform_settings_secrets(settings: dict, transform) -> dict:
    """Copy settings while transforming only API-key fields in supported schemas."""
    if not isinstance(settings, dict):
        return settings
    def visit(value):
        if isinstance(value, dict):
            result = {}
            for name, child in value.items():
                if name == "api_keys_with_status" and isinstance(child, list):
                    result[name] = [
                        {**visit(entry), "key": transform(entry.get("key", ""))}
                        if isinstance(entry, dict) else entry
                        for entry in child
                    ]
                elif name == "api_keys" and isinstance(child, list):
                    result[name] = [transform(key) if isinstance(key, str) else visit(key) for key in child]
                elif name in {"api_key", "embedding_api_key"} and isinstance(child, str):
                    result[name] = transform(child)
                else:
                    result[name] = visit(child)
            return result
        if isinstance(value, list):
            return [visit(item) for item in value]
        return value

    return visit(settings)


def unprotect_settings_secrets(settings: dict) -> dict:
    """Unlock available credentials without discarding unrelated settings on failure."""
    if not isinstance(settings, dict):
        return settings

    def unlock(value):
        try:
            return unprotect_secret(value)
        except Exception as exc:
            # Preserve the rest of the user's settings if an OS-bound key is
            # opened on another account or platform. Never pass ciphertext as a key.
            print(f"[SettingsManager WARN] API-ключ недоступен: {exc}")
            return ""

    return transform_settings_secrets(settings, unlock)


def settings_have_plaintext_secrets(settings: dict) -> bool:
    if os.name != "nt" or not isinstance(settings, dict):
        return False

    def visit(value):
        if isinstance(value, dict):
            for name, child in value.items():
                if name == "api_keys_with_status" and isinstance(child, list):
                    if any(
                        isinstance(entry, dict)
                        and isinstance(entry.get("key"), str)
                        and entry["key"]
                        and not entry["key"].startswith(_DPAPI_PREFIX)
                        for entry in child
                    ):
                        return True
                elif name == "api_keys" and isinstance(child, list):
                    if any(isinstance(key, str) and key and not key.startswith(_DPAPI_PREFIX) for key in child):
                        return True
                elif name in {"api_key", "embedding_api_key"}:
                    if isinstance(child, str) and child and not child.startswith(_DPAPI_PREFIX):
                        return True
                if visit(child):
                    return True
        elif isinstance(value, list):
            return any(visit(item) for item in value)
        return False

    return visit(settings)
