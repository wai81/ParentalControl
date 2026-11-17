"""Thin ctypes wrappers over the Windows Terminal Services (WTS) API.

They work from a service running as SYSTEM in session 0 and act on the
interactive session of the child.
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes
from dataclasses import dataclass

if sys.platform != "win32":  # pragma: no cover
    raise ImportError("pc_agent.winapi works only on Windows")

_wtsapi32 = ctypes.WinDLL("wtsapi32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

WTS_ACTIVE = 0
_WTS_USER_NAME = 5
_WTS_DOMAIN_NAME = 7
_WTS_SESSION_INFO_EX = 25
_WTS_SESSIONSTATE_LOCK = 0
_WTS_SESSIONSTATE_UNLOCK = 1
_NO_SESSION = 0xFFFFFFFF

MB_OK = 0x0
MB_ICONWARNING = 0x30
MB_SETFOREGROUND = 0x10000
MB_TOPMOST = 0x40000


class _WTS_SESSION_INFOW(ctypes.Structure):
    _fields_ = [
        ("SessionId", wintypes.DWORD),
        ("pWinStationName", wintypes.LPWSTR),
        ("State", ctypes.c_int),
    ]


class _WTSINFOEX_LEVEL1_W(ctypes.Structure):
    _fields_ = [
        ("SessionId", wintypes.ULONG),
        ("SessionState", ctypes.c_int),
        ("SessionFlags", wintypes.LONG),
        ("WinStationName", wintypes.WCHAR * 33),
        ("UserName", wintypes.WCHAR * 21),
        ("DomainName", wintypes.WCHAR * 18),
        ("LogonTime", ctypes.c_longlong),
        ("ConnectTime", ctypes.c_longlong),
        ("DisconnectTime", ctypes.c_longlong),
        ("LastInputTime", ctypes.c_longlong),
        ("CurrentTime", ctypes.c_longlong),
        ("IncomingBytes", wintypes.DWORD),
        ("OutgoingBytes", wintypes.DWORD),
        ("IncomingFrames", wintypes.DWORD),
        ("OutgoingFrames", wintypes.DWORD),
        ("IncomingCompressedBytes", wintypes.DWORD),
        ("OutgoingCompressedBytes", wintypes.DWORD),
    ]


class _WTSINFOEXW(ctypes.Structure):
    _fields_ = [("Level", wintypes.DWORD), ("Data", _WTSINFOEX_LEVEL1_W)]


_wtsapi32.WTSEnumerateSessionsW.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
    ctypes.POINTER(ctypes.POINTER(_WTS_SESSION_INFOW)), ctypes.POINTER(wintypes.DWORD),
]
_wtsapi32.WTSEnumerateSessionsW.restype = wintypes.BOOL
_wtsapi32.WTSQuerySessionInformationW.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, ctypes.c_int,
    ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.DWORD),
]
_wtsapi32.WTSQuerySessionInformationW.restype = wintypes.BOOL
_wtsapi32.WTSFreeMemory.argtypes = [ctypes.c_void_p]
_wtsapi32.WTSFreeMemory.restype = None
_wtsapi32.WTSLogoffSession.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL]
_wtsapi32.WTSLogoffSession.restype = wintypes.BOOL
_wtsapi32.WTSDisconnectSession.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL]
_wtsapi32.WTSDisconnectSession.restype = wintypes.BOOL
_wtsapi32.WTSSendMessageW.argtypes = [
    wintypes.HANDLE, wintypes.DWORD,
    wintypes.LPWSTR, wintypes.DWORD, wintypes.LPWSTR, wintypes.DWORD,
    wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), wintypes.BOOL,
]
_wtsapi32.WTSSendMessageW.restype = wintypes.BOOL
_kernel32.WTSGetActiveConsoleSessionId.argtypes = []
_kernel32.WTSGetActiveConsoleSessionId.restype = wintypes.DWORD
_kernel32.ProcessIdToSessionId.argtypes = [wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
_kernel32.ProcessIdToSessionId.restype = wintypes.BOOL


@dataclass(frozen=True)
class Session:
    session_id: int
    state: int
    user: str
    domain: str


def _query(session_id: int, info_class: int) -> ctypes.c_void_p:
    buf = ctypes.c_void_p()
    size = wintypes.DWORD()
    if not _wtsapi32.WTSQuerySessionInformationW(
        None, session_id, info_class, ctypes.byref(buf), ctypes.byref(size)
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    return buf


def _query_string(session_id: int, info_class: int) -> str:
    buf = _query(session_id, info_class)
    try:
        return ctypes.wstring_at(buf.value) if buf.value else ""
    finally:
        _wtsapi32.WTSFreeMemory(buf)


def list_sessions() -> list[Session]:
    info = ctypes.POINTER(_WTS_SESSION_INFOW)()
    count = wintypes.DWORD()
    if not _wtsapi32.WTSEnumerateSessionsW(None, 0, 1, ctypes.byref(info), ctypes.byref(count)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        raw = [(info[i].SessionId, info[i].State) for i in range(count.value)]
    finally:
        _wtsapi32.WTSFreeMemory(ctypes.cast(info, ctypes.c_void_p))
    sessions = []
    for session_id, state in raw:
        try:
            user = _query_string(session_id, _WTS_USER_NAME)
            domain = _query_string(session_id, _WTS_DOMAIN_NAME)
        except OSError:
            continue
        if user:
            sessions.append(Session(session_id, state, user, domain))
    return sessions


def active_console_session() -> int | None:
    sid = _kernel32.WTSGetActiveConsoleSessionId()
    return None if sid == _NO_SESSION else int(sid)


def is_session_locked(session_id: int) -> bool | None:
    """True/False, or None when Windows cannot tell."""
    buf = _query(session_id, _WTS_SESSION_INFO_EX)
    try:
        info = ctypes.cast(buf, ctypes.POINTER(_WTSINFOEXW)).contents
        flags = info.Data.SessionFlags
    finally:
        _wtsapi32.WTSFreeMemory(buf)
    if flags == _WTS_SESSIONSTATE_LOCK:
        return True
    if flags == _WTS_SESSIONSTATE_UNLOCK:
        return False
    return None


def process_session_id(pid: int) -> int | None:
    sid = wintypes.DWORD()
    if not _kernel32.ProcessIdToSessionId(pid, ctypes.byref(sid)):
        return None
    return int(sid.value)


def logoff_session(session_id: int) -> bool:
    return bool(_wtsapi32.WTSLogoffSession(None, session_id, False))


def disconnect_session(session_id: int) -> bool:
    """For the console session this returns the user to the lock/sign-in screen."""
    return bool(_wtsapi32.WTSDisconnectSession(None, session_id, False))


def send_message(session_id: int, title: str, text: str, timeout_seconds: int = 45) -> bool:
    """Show a message box in the given session without waiting for the user."""
    response = wintypes.DWORD()
    return bool(
        _wtsapi32.WTSSendMessageW(
            None, session_id,
            title, len(title) * 2,
            text, len(text) * 2,
            MB_OK | MB_ICONWARNING | MB_TOPMOST | MB_SETFOREGROUND,
            timeout_seconds, ctypes.byref(response), False,
        )
    )
