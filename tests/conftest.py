"""Native input tests use DLL doubles, never the host desktop or UI Automation."""
import ctypes
import importlib
import sys
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def windows_module(monkeypatch):
    if sys.platform != "win32":
        pytest.skip("Windows input structures")
    # Mock before importing: even import-time DPI setup must not touch the host.
    monkeypatch.setattr(ctypes, "WinDLL", lambda *a, **kw: MagicMock())
    return importlib.import_module("dcua.backends.windows")
