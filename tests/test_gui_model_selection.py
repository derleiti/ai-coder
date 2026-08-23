from __future__ import annotations

import unittest

from aicoder.gui.chat_widget import ChatWidget


class _Combo:
    def __init__(self, value=""):
        self.value = value
        self.items = []
    def currentText(self): return self.value
    def clear(self): self.items = []; self.value = ""
    def addItem(self, value): self.items.append(value)
    def setCurrentText(self, value): self.value = value


class _Settings:
    def get_current_model(self): return "settings/model"
    def get_current_fallback(self): return "settings/fallback"


class ChatModelRefreshTests(unittest.TestCase):
    def _widget(self):
        widget = ChatWidget.__new__(ChatWidget)
        widget.model_combo = _Combo("chat/model")
        widget.fallback_combo = _Combo("chat/fallback")
        widget.settings_ref = _Settings()
        widget._syncing = False
        widget._model_override_dirty = False
        widget._fallback_override_dirty = False
        return widget

    def test_async_refresh_preserves_manual_chat_override(self):
        widget = self._widget()
        widget._model_override_dirty = True
        widget._fallback_override_dirty = True
        ChatWidget._on_models_updated(widget, ["settings/model", "chat/model", "chat/fallback"])
        self.assertEqual(widget.model_combo.currentText(), "chat/model")
        self.assertEqual(widget.fallback_combo.currentText(), "chat/fallback")

    def test_refresh_uses_settings_when_chat_not_overridden(self):
        widget = self._widget()
        ChatWidget._on_models_updated(widget, ["settings/model", "settings/fallback"])
        self.assertEqual(widget.model_combo.currentText(), "settings/model")
        self.assertEqual(widget.fallback_combo.currentText(), "settings/fallback")

    def test_explicit_settings_change_clears_chat_override(self):
        widget = self._widget()
        widget._model_override_dirty = True
        widget._fallback_override_dirty = True
        ChatWidget._on_settings_selection_changed(widget, "new/model", "new/fallback")
        self.assertEqual(widget.model_combo.currentText(), "new/model")
        self.assertEqual(widget.fallback_combo.currentText(), "new/fallback")
        self.assertFalse(widget._model_override_dirty)
        self.assertFalse(widget._fallback_override_dirty)
