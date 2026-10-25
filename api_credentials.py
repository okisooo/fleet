"""Read Fleet's API keys from the current user's Windows Credential Manager.

The native settings page saves them. Keys never travel through panel.json,
settings.json, the local usage bridge, or provider caches.
"""
from __future__ import annotations

import ctypes
import os
from ctypes import wintypes

TARGETS = {name: "Fleet/api/" + name for name in ("anthropic", "anthropic-admin")}


class _Credential(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD), ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR), ("Comment", wintypes.LPWSTR),
        ("LastWritten", wintypes.FILETIME), ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)), ("Persist", wintypes.DWORD),
        ("AttributeCount", wintypes.DWORD), ("Attributes", ctypes.c_void_p),
        ("TargetAlias", wintypes.LPWSTR), ("UserName", wintypes.LPWSTR),
    ]


def _read_target(target):
    if os.name != "nt":
        return None
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    pointer = ctypes.POINTER(_Credential)()
    advapi.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                               ctypes.POINTER(ctypes.POINTER(_Credential))]
    advapi.CredReadW.restype = wintypes.BOOL
    advapi.CredFree.argtypes = [ctypes.c_void_p]
    advapi.CredFree.restype = None
    if not advapi.CredReadW(target, 1, 0, ctypes.byref(pointer)):
        return None
    try:
        cred = pointer.contents
        if not 0 < cred.CredentialBlobSize <= 2560:
            return None
        return ctypes.string_at(cred.CredentialBlob, cred.CredentialBlobSize).decode("utf-16-le").strip() or None
    except (ValueError, UnicodeError):
        return None
    finally:
        advapi.CredFree(pointer)


def read(provider):
    target = TARGETS.get(provider)
    return _read_target(target) if target else None
