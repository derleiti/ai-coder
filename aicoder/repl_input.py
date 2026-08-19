"""Interactive REPL input with coding-agent style keybindings.

``prompt_toolkit`` owns the cursor while the prompt is visible.  That keeps
late terminal output from being painted over the user's current input and also
gives us proper multiline editing and persistent history.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable

try:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.completion import WordCompleter
    from prompt_toolkit.formatted_text import ANSI
    from prompt_toolkit.history import FileHistory, InMemoryHistory
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.patch_stdout import patch_stdout
    from prompt_toolkit.styles import Style
except (ImportError, OSError):
    # Keep the basic ``input`` fallback usable when prompt_toolkit is missing
    # or its package metadata cannot be read in a restricted environment.
    PromptSession = None


COMMANDS = [
    "/clear", "/exit", "/fallback", "/help", "/keys", "/model",
    "/command", "/commands", "/guidelines", "/models", "/new", "/permissions",
    "/plan", "/quit", "/runtime", "/setup", "/status", "/swarm", "/tools",
]


class PromptCancelled(Exception):
    """The current editable prompt was cancelled without ending the REPL."""


class ReplInput:
    """Prompt session with a dependency-free ``input`` fallback."""

    def __init__(self, history_file: Path, toolbar: Callable[[], str]):
        self._session = None
        self._ansi = None
        self._patch_stdout = None
        self._toolbar = toolbar
        self.persistent_history = False

        if PromptSession is None:
            return

        bindings = KeyBindings()

        @bindings.add("enter")
        def _submit(event):
            """Enter sends, matching Codex/Claude-style agent prompts."""
            event.current_buffer.validate_and_handle()

        @bindings.add("escape", "enter")
        def _newline(event):
            """Alt+Enter (and terminals mapping Shift+Enter) inserts a line."""
            event.current_buffer.insert_text("\n")

        @bindings.add("c-c")
        def _cancel_or_interrupt(event):
            buffer = event.current_buffer
            if buffer.text:
                buffer.reset()
            else:
                event.app.exit(exception=PromptCancelled)

        @bindings.add("c-d")
        def _delete_or_exit(event):
            buffer = event.current_buffer
            if buffer.text:
                buffer.delete()
            else:
                event.app.exit(exception=EOFError)

        @bindings.add("c-l")
        def _clear_screen(event):
            event.app.renderer.clear()

        style = Style.from_dict({
            "prompt": "bold ansicyan",
            "bottom-toolbar": "bg:#151a24 #8b98ad",
            "bottom-toolbar.key": "bg:#151a24 #43d9c0 bold",
        })
        completer = WordCompleter(COMMANDS, sentence=True, ignore_case=True)
        try:
            history_file.parent.mkdir(parents=True, exist_ok=True)
            # FileHistory opens the file only when the first entry is stored.
            # Probe it now so a read-only config directory cannot crash the
            # prompt-toolkit event loop after the user presses Enter.
            with history_file.open("ab"):
                pass
            history = FileHistory(str(history_file))
            self.persistent_history = True
        except OSError:
            history = InMemoryHistory()
        self._session = PromptSession(
            history=history,
            completer=completer,
            complete_while_typing=False,
            key_bindings=bindings,
            multiline=True,
            enable_history_search=True,
            style=style,
        )
        self._ansi = ANSI
        self._patch_stdout = patch_stdout

    @property
    def enhanced(self) -> bool:
        return self._session is not None

    def read(self, prompt: str) -> str:
        if self._session is None:
            return input(prompt)

        # patch_stdout makes any delayed log line temporarily suspend and then
        # correctly repaint the editable prompt instead of corrupting it.
        with self._patch_stdout(raw=True):
            return self._session.prompt(
                self._ansi(prompt),
                bottom_toolbar=self._bottom_toolbar,
                prompt_continuation=lambda width, _line, _wrapped: " " * max(0, width - 2) + "· ",
            )

    def _bottom_toolbar(self):
        return [
            ("class:bottom-toolbar.key", " Enter "),
            ("class:bottom-toolbar", "send  "),
            ("class:bottom-toolbar.key", " Alt+Enter "),
            ("class:bottom-toolbar", "newline  "),
            ("class:bottom-toolbar.key", " Ctrl+C "),
            ("class:bottom-toolbar", "clear/cancel  "),
            ("class:bottom-toolbar.key", " Ctrl+R "),
            ("class:bottom-toolbar", "history   "),
            ("class:bottom-toolbar", self._toolbar()),
        ]
