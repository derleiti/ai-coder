"""Shared pytest process setup.

Keep one QApplication alive for the complete test process. Several GUI tests run
worker/event-loop code in sequence; letting the last Python QApplication wrapper
be garbage-collected between modules can abort Qt on headless CI runners.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PyQt6.QtWidgets import QApplication
except Exception:  # pragma: no cover - optional GUI dependency in minimal installs
    _QT_APP = None
else:
    _QT_APP = QApplication.instance() or QApplication([])
