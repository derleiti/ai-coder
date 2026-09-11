"""Main chat hub: local AICoder chat plus Shared Notify conversation tabs."""
from __future__ import annotations

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QTabWidget, QVBoxLayout, QWidget

from .. import shared_notify as shared
from .chat_widget import ChatWidget
from .notify_chat_widget import NotifyConversationWidget


class ChatHubWidget(QWidget):
    def __init__(self, settings_ref=None, parent=None):
        super().__init__(parent)
        self.local_chat = ChatWidget(settings_ref=settings_ref)
        self._conversation_tabs: dict[str, NotifyConversationWidget] = {}
        self._unread: dict[str, int] = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.tabs = QTabWidget()
        self.tabs.setTabsClosable(True)
        self.tabs.tabCloseRequested.connect(self._close_tab)
        self.tabs.currentChanged.connect(self._tab_changed)
        self.tabs.addTab(self.local_chat, "Local AI")
        self.tabs.tabBar().setTabButton(0, self.tabs.tabBar().ButtonPosition.RightSide, None)
        layout.addWidget(self.tabs)

        self._timer = QTimer(self)
        self._timer.setInterval(1500)
        self._timer.timeout.connect(self._drain_incoming)
        self._timer.start()

    def __getattr__(self, name):
        # Preserve legacy MainWindow call sites that treated chat_tab as ChatWidget.
        local = self.__dict__.get("local_chat")
        if local is not None and hasattr(local, name):
            return getattr(local, name)
        raise AttributeError(name)

    def open_conversation(self, conversation: dict):
        cid = str((conversation or {}).get("conversation_id") or "")
        if not cid:
            return
        widget = self._conversation_tabs.get(cid)
        if widget is None:
            widget = NotifyConversationWidget(conversation, self)
            widget.title_changed.connect(lambda _title, c=cid: self._update_tab_title(c))
            self._conversation_tabs[cid] = widget
            self.tabs.addTab(widget, widget.display_title())
        else:
            widget.update_conversation(conversation)
        self._unread.pop(cid, None)
        self._update_tab_title(cid)
        self.tabs.setCurrentWidget(widget)

    def _conversation_id_for_index(self, index: int) -> str:
        widget = self.tabs.widget(index)
        for cid, candidate in self._conversation_tabs.items():
            if candidate is widget:
                return cid
        return ""

    def _close_tab(self, index: int):
        if index == 0:
            return
        cid = self._conversation_id_for_index(index)
        widget = self.tabs.widget(index)
        self.tabs.removeTab(index)
        if cid:
            self._conversation_tabs.pop(cid, None)
            self._unread.pop(cid, None)
        if widget is not None:
            widget.deleteLater()

    def _tab_changed(self, index: int):
        cid = self._conversation_id_for_index(index)
        if cid:
            self._unread.pop(cid, None)
            self._update_tab_title(cid)
            widget = self._conversation_tabs.get(cid)
            if widget is not None:
                widget.refresh()

    def _update_tab_title(self, cid: str):
        widget = self._conversation_tabs.get(cid)
        if widget is None:
            return
        index = self.tabs.indexOf(widget)
        if index < 0:
            return
        count = int(self._unread.get(cid, 0))
        suffix = f" ({count})" if count else ""
        self.tabs.setTabText(index, widget.display_title() + suffix)

    def _drain_incoming(self):
        rows = shared.drain_received_messages()
        for message in rows:
            metadata = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
            cid = str(metadata.get("conversation_id") or "")
            if not cid:
                continue
            widget = self._conversation_tabs.get(cid)
            if widget is not None:
                if self.tabs.currentWidget() is widget:
                    widget.refresh()
                else:
                    self._unread[cid] = self._unread.get(cid, 0) + 1
                    self._update_tab_title(cid)
