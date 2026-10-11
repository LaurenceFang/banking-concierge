"""Runtime configuration, with secrets kept outside the source tree."""
from __future__ import annotations

import ctypes
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")


def deepseek_key() -> str | None:
    value = os.getenv("DEEPSEEK_API_KEY", "").strip()
    if value:
        return value
    if os.name != "nt" or not os.getenv("LOCALAPPDATA"):
        return None
    path = Path(os.environ["LOCALAPPDATA"]) / "BankingOS" / "deepseek-api-key.dpapi"
    if not path.exists():
        return None

    class Blob(ctypes.Structure):
        _fields_ = [("size", ctypes.c_ulong), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    encrypted = path.read_bytes()
    buffer = ctypes.create_string_buffer(encrypted)
    incoming = Blob(len(encrypted), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    outgoing = Blob()
    function = ctypes.WinDLL("crypt32", use_last_error=True).CryptUnprotectData
    function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                         ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(Blob)]
    function.restype = ctypes.c_int
    if not function(ctypes.byref(incoming), None, None, None, None, 1, ctypes.byref(outgoing)):
        return None
    try:
        return ctypes.string_at(outgoing.data, outgoing.size).decode("utf-8")
    finally:
        free = ctypes.WinDLL("kernel32").LocalFree
        free.argtypes = [ctypes.c_void_p]
        free.restype = ctypes.c_void_p
        free(ctypes.cast(outgoing.data, ctypes.c_void_p))


def database_url() -> str:
    configured = os.getenv("DATABASE_URL", "").strip()
    if configured:
        return configured
    local = ROOT / ".local"
    local.mkdir(exist_ok=True)
    return "sqlite:///" + (local / "bank.sqlite3").as_posix()


MODEL = "deepseek-flash"
DEEPSEEK_BASE = "https://api.deepseek.com"
