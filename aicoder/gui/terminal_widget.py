"""Vollstaendiges PTY-Bash-Terminal fuer ai-coder GUI."""
from __future__ import annotations
import os
import pty
import signal
import fcntl
import termios
import struct
import select
import re
from PyQt6.QtWidgets import QWidget, QVBoxLayout, QLabel
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QTimer
from PyQt6.QtGui import QFont, QColor, QPainter, QPalette, QKeyEvent, QFontMetrics
import logging

logger = logging.getLogger("ailinux.terminal")

try:
    import pyte
    HAS_PYTE = True
except ImportError:
    HAS_PYTE = False


# ── PTY Reader Thread ──────────────────────────────────────────────────────────

class _PTYReader(QThread):
    data = pyqtSignal(bytes)
    done = pyqtSignal()

    def __init__(self, fd: int):
        super().__init__()
        self.fd = fd
        self.running = True

    def run(self):
        while self.running:
            try:
                r, _, _ = select.select([self.fd], [], [], 0.02)
                if r:
                    chunk = os.read(self.fd, 65536)
                    if chunk:
                        self.data.emit(chunk)
                    else:
                        break
            except OSError:
                break
        self.done.emit()

    def stop(self):
        self.running = False


# ── Terminal Display (pyte-basiert) ───────────────────────────────────────────

class _TermDisplay(QWidget):
    """Rendert pyte.Screen — empfaengt auch Key-Events."""
    key_input = pyqtSignal(bytes)

    # Standard 16-Farben (Dracula-Palette)
    PALETTE = {
        "default":      "#f8f8f2", "black":  "#282a36", "red":     "#ff5555",
        "green":        "#50fa7b", "yellow": "#f1fa8c", "blue":    "#6272a4",
        "magenta":      "#ff79c6", "cyan":   "#8be9fd", "white":   "#f8f8f2",
        "brightblack":  "#6272a4", "brightred":   "#ff6e6e", "brightgreen": "#69ff94",
        "brightyellow": "#ffffa5", "brightblue":  "#d6acff",
        "brightmagenta":"#ff92df", "brightcyan":  "#a4ffff", "brightwhite": "#ffffff",
    }
    BG_COLOR = "#0d0d0d"
    CURSOR_COLOR = "#00d4ff"

    def __init__(self, parent=None):
        super().__init__(parent)
        self.screen: pyte.Screen | None = None
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAutoFillBackground(False)
        font = QFont("Cascadia Code", 11)
        font.setStyleHint(QFont.StyleHint.Monospace)
        self.setFont(font)
        fm = QFontMetrics(font)
        self._cw = fm.horizontalAdvance("M")
        self._ch = fm.height()
        self._cursor_vis = True
        self._blink = QTimer(self)
        self._blink.timeout.connect(self._toggle_cursor)
        self._blink.start(500)
        self._color_cache: dict = {}
        self._bg = QColor(self.BG_COLOR)
        self._cur = QColor(self.CURSOR_COLOR)
        # Palette vorkompilieren
        self._pal = {k: QColor(v) for k, v in self.PALETTE.items()}

    def _toggle_cursor(self):
        self._cursor_vis = not self._cursor_vis
        self.update()

    def set_screen(self, screen: pyte.Screen):
        self.screen = screen
        self.update()

    def paintEvent(self, _event):
        if not self.screen or self._cw <= 0 or self._ch <= 0:
            return
        p = QPainter(self)
        p.fillRect(self.rect(), self._bg)
        fn = self.font()
        fn_bold = QFont(fn)
        fn_bold.setBold(True)

        for row in range(self.screen.lines):
            if row >= len(self.screen.display):
                break
            line = self.screen.display[row]
            y = row * self._ch + self._ch - 3
            for col, ch in enumerate(line):
                x = col * self._cw
                try:
                    cell = self.screen.buffer[row][col]
                    fg = self._resolve(cell.fg, self._pal["default"])
                    bg = self._resolve(cell.bg, self._bg)
                    bold = cell.bold
                    reverse = cell.reverse
                except (KeyError, IndexError, AttributeError):
                    fg = self._pal["default"]
                    bg = self._bg
                    bold = False
                    reverse = False

                if reverse:
                    fg, bg = bg, fg
                if bg != self._bg:
                    p.fillRect(x, row * self._ch, self._cw, self._ch, bg)
                if ch and ch.strip():
                    p.setFont(fn_bold if bold else fn)
                    p.setPen(fg)
                    p.drawText(x, y, ch)

        # Cursor
        if self._cursor_vis and self.screen:
            cx = self.screen.cursor.x * self._cw
            cy = self.screen.cursor.y * self._ch
            p.fillRect(cx, cy, self._cw, self._ch, self._cur)
            if 0 <= self.screen.cursor.y < len(self.screen.display):
                line = self.screen.display[self.screen.cursor.y]
                if 0 <= self.screen.cursor.x < len(line):
                    ch = line[self.screen.cursor.x]
                    if ch and ch.strip():
                        p.setPen(QColor(self.BG_COLOR))
                        p.setFont(fn)
                        p.drawText(cx, cy + self._ch - 3, ch)

    def _resolve(self, color, default: QColor) -> QColor:
        if color == "default" or color is None:
            return default
        key = str(color)
        if key not in self._color_cache:
            if isinstance(color, str):
                self._color_cache[key] = QColor(
                    self._pal.get(color, self._pal["default"])
                ) if not color.startswith("#") else QColor(color)
            elif isinstance(color, (tuple, list)) and len(color) >= 3:
                self._color_cache[key] = QColor(int(color[0]), int(color[1]), int(color[2]))
            elif isinstance(color, int):
                if color < 16:
                    names = list(self.PALETTE.keys())[1:]
                    self._color_cache[key] = QColor(self.PALETTE.get(names[min(color, len(names)-1)], "#f8f8f2"))
                elif color < 232:
                    c = color - 16
                    vals = [0, 95, 135, 175, 215, 255]
                    self._color_cache[key] = QColor(vals[c//36], vals[(c//6)%6], vals[c%6])
                else:
                    g = 8 + (color - 232) * 10
                    self._color_cache[key] = QColor(g, g, g)
            else:
                self._color_cache[key] = default
        return self._color_cache[key]

    def keyPressEvent(self, ev: QKeyEvent):
        key = ev.key()
        mods = ev.modifiers()
        text = ev.text()
        ctrl = bool(mods & Qt.KeyboardModifier.ControlModifier)
        shift = bool(mods & Qt.KeyboardModifier.ShiftModifier)
        alt = bool(mods & Qt.KeyboardModifier.AltModifier)

        # Ctrl+Shift+C/V
        if ctrl and shift and key == Qt.Key.Key_C:
            from PyQt6.QtWidgets import QApplication
            # Copy: selected text (not implemented yet — placeholder)
            return
        if ctrl and shift and key == Qt.Key.Key_V:
            from PyQt6.QtWidgets import QApplication
            txt = QApplication.clipboard().text()
            if txt:
                self.key_input.emit(txt.encode("utf-8"))
            return

        _MAP = {
            Qt.Key.Key_Return:   b"\r",
            Qt.Key.Key_Enter:    b"\r",
            Qt.Key.Key_Backspace:b"\x7f",
            Qt.Key.Key_Tab:      b"\t",
            Qt.Key.Key_Escape:   b"\x1b",
            Qt.Key.Key_Up:       b"\x1b[A",
            Qt.Key.Key_Down:     b"\x1b[B",
            Qt.Key.Key_Right:    b"\x1b[C",
            Qt.Key.Key_Left:     b"\x1b[D",
            Qt.Key.Key_Home:     b"\x1b[H",
            Qt.Key.Key_End:      b"\x1b[F",
            Qt.Key.Key_PageUp:   b"\x1b[5~",
            Qt.Key.Key_PageDown: b"\x1b[6~",
            Qt.Key.Key_Delete:   b"\x1b[3~",
            Qt.Key.Key_Insert:   b"\x1b[2~",
        }
        fn_codes = {i+1: c for i, c in enumerate([
            b"\x1bOP",b"\x1bOQ",b"\x1bOR",b"\x1bOS",
            b"\x1b[15~",b"\x1b[17~",b"\x1b[18~",b"\x1b[19~",
            b"\x1b[20~",b"\x1b[21~",b"\x1b[23~",b"\x1b[24~",
        ])}

        if key in _MAP:
            self.key_input.emit(_MAP[key])
        elif Qt.Key.Key_F1 <= key <= Qt.Key.Key_F12:
            fn = key - Qt.Key.Key_F1 + 1
            self.key_input.emit(fn_codes.get(fn, b""))
        elif ctrl and not shift and not alt and Qt.Key.Key_A <= key <= Qt.Key.Key_Z:
            self.key_input.emit(bytes([key - Qt.Key.Key_A + 1]))
        elif text:
            if alt:
                self.key_input.emit(b"\x1b" + text.encode("utf-8"))
            else:
                self.key_input.emit(text.encode("utf-8"))

    def event(self, ev):
        if ev.type() == ev.Type.KeyPress:
            ke = ev
            if ke.key() in (Qt.Key.Key_Tab, Qt.Key.Key_Backtab):
                self.key_input.emit(b"\t" if ke.key() == Qt.Key.Key_Tab else b"\x1b[Z")
                return True
        return super().event(ev)

    def sizeHint(self):
        from PyQt6.QtCore import QSize
        return QSize(800, 220)

    def minimumSizeHint(self):
        from PyQt6.QtCore import QSize
        return QSize(200, 80)


# ── Haupt-Terminal-Widget ──────────────────────────────────────────────────────

class TerminalWidget(QWidget):
    """
    Vollstaendiges PTY-Bash-Terminal.
    - pyte VT100-Emulation
    - Farbiger Output (256-Farben)
    - vim, htop, nano, etc. funktionieren
    - API: send_command(cmd) fuer externe Aufrufe (z.B. von AI)
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._master_fd: int | None = None
        self._pid: int | None = None
        self._reader: _PTYReader | None = None
        self.cols = 120
        self.rows = 30

        if HAS_PYTE:
            self.screen = pyte.Screen(self.cols, self.rows)
            self.stream = pyte.Stream(self.screen)
        else:
            self.screen = None
            self.stream = None

        self._update_timer = QTimer(self)
        self._update_timer.setSingleShot(True)
        self._update_timer.timeout.connect(self._flush)
        self._pending = False

        self._build_ui()

        if HAS_PYTE:
            self._start_pty()
        else:
            self._display.hide()
            self._err_label = QLabel(
                "pyte nicht installiert.\n"
                "sudo apt install python3-pyte"
            )
            self._err_label.setStyleSheet("color:#ff5555; padding:12px;")
            self._err_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.layout().addWidget(self._err_label)

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Header-Leiste
        header = QLabel("  ▶ bash")
        header.setStyleSheet(
            "background:#111827; color:#00d4ff; font-size:11px; "
            "padding:3px 8px; border-top:1px solid #333;"
        )
        layout.addWidget(header)

        # Terminal-Anzeige
        self._display = _TermDisplay(self)
        if self.screen:
            self._display.set_screen(self.screen)
        self._display.key_input.connect(self._write)
        layout.addWidget(self._display, 1)

    def _start_pty(self):
        try:
            master_fd, slave_fd = pty.openpty()
            # Terminal-Größe setzen
            winsize = struct.pack("HHHH", self.rows, self.cols, 0, 0)
            fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, winsize)

            pid = os.fork()
            if pid == 0:
                # Kind: Shell starten
                os.close(master_fd)
                os.setsid()
                fcntl.ioctl(slave_fd, termios.TIOCSCTTY, 0)
                for fd in (0, 1, 2):
                    os.dup2(slave_fd, fd)
                if slave_fd > 2:
                    os.close(slave_fd)
                env = os.environ.copy()
                env.update({
                    "TERM": "xterm-256color",
                    "COLORTERM": "truecolor",
                    "COLUMNS": str(self.cols),
                    "LINES": str(self.rows),
                })
                shell = env.get("SHELL", "/bin/bash")
                os.execvpe(shell, [shell, "--login", "-i"], env)
                os._exit(1)
            else:
                # Elter
                os.close(slave_fd)
                self._master_fd = master_fd
                self._pid = pid
                flags = fcntl.fcntl(master_fd, fcntl.F_GETFL)
                fcntl.fcntl(master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

                self._reader = _PTYReader(master_fd)
                self._reader.data.connect(self._on_data)
                self._reader.done.connect(self._on_done)
                self._reader.start()
        except Exception as e:
            logger.error(f"PTY start failed: {e}")

    def _on_data(self, data: bytes):
        if self.stream:
            try:
                self.stream.feed(data.decode("utf-8", errors="replace"))
            except Exception as e:
                logger.debug(f"pyte feed error: {e}")
        if not self._pending:
            self._pending = True
            self._update_timer.start(8)  # ~120fps

    def _flush(self):
        self._pending = False
        self._display.update()

    def _write(self, data: bytes):
        if self._master_fd is not None:
            try:
                os.write(self._master_fd, data)
            except OSError:
                pass

    def _on_done(self):
        logger.info("PTY process finished")

    def send_command(self, cmd: str):
        """Befehl von außen senden (z.B. von AI oder Chat-Widget)."""
        self._write((cmd + "\n").encode("utf-8"))

    def _resize_pty(self, cols: int, rows: int):
        if self._master_fd is not None:
            try:
                winsize = struct.pack("HHHH", rows, cols, 0, 0)
                fcntl.ioctl(self._master_fd, termios.TIOCSWINSZ, winsize)
                if self.screen:
                    self.screen.resize(rows, cols)
                    self.cols = cols
                    self.rows = rows
            except OSError:
                pass

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        if self._display and self._display.isVisible():
            cw = self._display._cw or 8
            ch = self._display._ch or 16
            new_cols = max(20, self._display.width() // cw)
            new_rows = max(5, self._display.height() // ch)
            if new_cols != self.cols or new_rows != self.rows:
                self._resize_pty(new_cols, new_rows)

    def closeEvent(self, ev):
        self._cleanup()
        super().closeEvent(ev)

    def _cleanup(self):
        if self._reader:
            self._reader.stop()
            self._reader.wait(2000)
            self._reader = None
        if self._pid:
            try:
                os.kill(self._pid, signal.SIGTERM)
                os.waitpid(self._pid, 0)
            except OSError:
                pass
            self._pid = None
        if self._master_fd is not None:
            try:
                os.close(self._master_fd)
            except OSError:
                pass
            self._master_fd = None
