"""
NDI Live Switcher
-----------------
Discovers every NDI source on the local network and shows them as a grid of
cards: an optional live thumbnail, the machine / source name and buttons.

LIVE   -> button turns red; that feed plays full screen on the selected
          output display. Click again (or Esc on the output) to take it off.
Edit   -> opens the framing editor for that input: zoom in and move the
          picture left/right/up/down. Framing is applied to the output and
          thumbnail, and is remembered per input.
Hide   -> removes an unwanted input from the grid. Restore from the
          View menu or the "Hidden" button.

Menus: File (Open / Save settings, Quit), View (thumbnails on/off, hidden
inputs), Output (edit framing, take off air), Help.

Requires: NDI runtime/tools installed, plus `pip install -r requirements.txt`.
"""

import json
import sys
import time

import numpy as np
import NDIlib as ndi
from PySide6.QtCore import QPointF, QRectF, QSettings, QSize, Qt, QThread, Signal
from PySide6.QtGui import (
    QAction, QFont, QGuiApplication, QImage, QKeySequence, QPainter,
)
from PySide6.QtWidgets import (
    QApplication, QComboBox, QDialog, QFileDialog, QFrame, QGridLayout,
    QHBoxLayout, QLabel, QMainWindow, QMenu, QMessageBox, QPushButton,
    QScrollArea, QSlider, QSpinBox, QToolButton, QVBoxLayout, QWidget,
)

APP_NAME = "NDI Live Switcher"
APP_VERSION = "1.2"
REFRESH_MS = 2000
THUMB_W, THUMB_H = 256, 144
THUMB_FPS = 5
CARD_W = THUMB_W + 20
GRID_SPACING = 16
ZOOM_MIN, ZOOM_MAX = 100, 500     # percent
PAN_RANGE = 100                   # percent each way
DEFAULT_FRAMING = {"zoom": 100, "x": 0, "y": 0}

DARK_STYLE = """
QMainWindow, QWidget#root, QDialog { background:#121417; }
QLabel { color:#e6e6e6; }
QToolButton { color:#e6e6e6; background:#2b2f36; border:1px solid #444;
              border-radius:6px; padding:5px 10px; }
QMenuBar { background:#1c1f24; color:#e6e6e6; }
QMenuBar::item:selected { background:#3a3f48; }
QMenu { background:#1c1f24; color:#e6e6e6; border:1px solid #333; }
QMenu::item:selected { background:#3a3f48; }
QMenu::item:disabled { color:#666; }
QSpinBox, QComboBox { background:#1c1f24; color:#e6e6e6; border:1px solid #444;
                      border-radius:4px; padding:3px; }
"""
BTN_IDLE = """
QPushButton { background:#2b2f36; color:#e6e6e6; border:1px solid #444;
              border-radius:6px; padding:8px; font-weight:bold; font-size:14px; }
QPushButton:hover { background:#3a3f48; }
"""
BTN_LIVE = """
QPushButton { background:#d11a2a; color:white; border:1px solid #ff4d5a;
              border-radius:6px; padding:8px; font-weight:bold; font-size:14px; }
"""
BTN_SMALL = """
QPushButton { background:transparent; color:#aab2bc; border:1px solid #3a3f48;
              border-radius:6px; padding:8px 8px; font-size:12px; }
QPushButton:hover { color:white; border-color:#777; }
"""


def split_name(ndi_name: str):
    """NDI names look like 'MACHINE (Source Name)'."""
    if "(" in ndi_name and ndi_name.endswith(")"):
        machine, src = ndi_name.split("(", 1)
        return machine.strip(), src[:-1].strip()
    return ndi_name, ""


def clean_framing(f) -> dict:
    try:
        return {
            "zoom": int(min(ZOOM_MAX, max(ZOOM_MIN, int(f.get("zoom", 100))))),
            "x": int(min(PAN_RANGE, max(-PAN_RANGE, int(f.get("x", 0))))),
            "y": int(min(PAN_RANGE, max(-PAN_RANGE, int(f.get("y", 0))))),
        }
    except (AttributeError, TypeError, ValueError):
        return dict(DEFAULT_FRAMING)


# --------------------------------------------------------------------------- #
# NDI discovery
# --------------------------------------------------------------------------- #
class DiscoveryThread(QThread):
    sources_changed = Signal(list)

    def __init__(self):
        super().__init__()
        self._running = True
        self._last = None

    def run(self):
        finder = ndi.find_create_v2()
        if finder is None:
            return
        try:
            while self._running:
                ndi.find_wait_for_sources(finder, REFRESH_MS)
                names = sorted(s.ndi_name for s in ndi.find_get_current_sources(finder))
                if names != self._last:
                    self._last = names
                    self.sources_changed.emit(names)
        finally:
            ndi.find_destroy(finder)

    def stop(self):
        self._running = False
        self.wait(REFRESH_MS + 1000)


# --------------------------------------------------------------------------- #
# NDI receiving (full-quality output / editor, or low-res thumbnails)
# --------------------------------------------------------------------------- #
class ReceiverThread(QThread):
    frame_ready = Signal(QImage)
    status = Signal(str)

    def __init__(self, source_name: str, preview: bool = False):
        super().__init__()
        self.source_name = source_name
        self.preview = preview
        self._running = True

    def _find_source(self, finder):
        while self._running:
            ndi.find_wait_for_sources(finder, 1000)
            for s in ndi.find_get_current_sources(finder):
                if s.ndi_name == self.source_name:
                    return s
        return None

    def run(self):
        self.status.emit("Connecting…")
        finder = ndi.find_create_v2()
        if finder is None:
            self.status.emit("NDI error")
            return
        source = self._find_source(finder)
        if source is None:
            ndi.find_destroy(finder)
            return

        desc = ndi.RecvCreateV3()
        desc.color_format = ndi.RECV_COLOR_FORMAT_BGRX_BGRA
        desc.bandwidth = (ndi.RECV_BANDWIDTH_LOWEST if self.preview
                          else ndi.RECV_BANDWIDTH_HIGHEST)
        recv = ndi.recv_create_v3(desc)
        if recv is None:
            ndi.find_destroy(finder)
            self.status.emit("Could not create NDI receiver")
            return

        ndi.recv_connect(recv, source)
        ndi.find_destroy(finder)
        min_gap = 1.0 / THUMB_FPS if self.preview else 0.0
        last_emit = 0.0
        got_video = False

        try:
            while self._running:
                t, v, a, m = ndi.recv_capture_v2(recv, 500)
                if t == ndi.FRAME_TYPE_VIDEO:
                    now = time.monotonic()
                    if now - last_emit < min_gap:
                        ndi.recv_free_video_v2(recv, v)
                        continue
                    last_emit = now
                    h, w = v.yres, v.xres
                    stride = v.line_stride_in_bytes
                    data = np.copy(v.data)
                    ndi.recv_free_video_v2(recv, v)
                    img = QImage(data.data, w, h, stride, QImage.Format_RGB32)
                    if self.preview:
                        img = img.scaled(THUMB_W * 2, THUMB_H * 2, Qt.KeepAspectRatio,
                                         Qt.SmoothTransformation)
                    else:
                        img = img.copy()
                    if not got_video:
                        got_video = True
                        self.status.emit("")
                    self.frame_ready.emit(img)
                elif t == ndi.FRAME_TYPE_AUDIO:
                    ndi.recv_free_audio_v2(recv, a)
                elif t == ndi.FRAME_TYPE_METADATA:
                    ndi.recv_free_metadata(recv, m)
        finally:
            ndi.recv_destroy(recv)

    def stop(self, wait: bool = True):
        self._running = False
        if wait:
            self.wait(2500)


def retire_thread(thread: ReceiverThread, graveyard: set):
    """Stop a receiver without blocking the UI; keep a reference until done."""
    if thread is None:
        return
    for sig in (thread.frame_ready, thread.status):
        try:
            sig.disconnect()
        except (RuntimeError, TypeError):
            pass
    graveyard.add(thread)
    thread.finished.connect(lambda: graveyard.discard(thread))
    thread.stop(wait=False)


# --------------------------------------------------------------------------- #
# A view that draws a frame with zoom + pan applied
# --------------------------------------------------------------------------- #
class FramedView(QWidget):
    def __init__(self, font_size: int = 12):
        super().__init__()
        self._image = None
        self._message = ""
        self._font_size = font_size
        self.framing = dict(DEFAULT_FRAMING)
        self._drawn_size = (1.0, 1.0)

    def set_image(self, img: QImage):
        self._image = img
        self._message = ""
        self.update()

    def set_message(self, text: str):
        if text:
            self._message = text
            self._image = None
            self.update()

    def clear(self):
        self._image = None
        self._message = ""
        self.update()

    def set_framing(self, f: dict):
        self.framing = clean_framing(f)
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.fillRect(self.rect(), Qt.black)
        img = self._image
        if img is not None and not img.isNull() and img.width() and img.height():
            p.setRenderHint(QPainter.SmoothPixmapTransform)
            W, H = self.width(), self.height()
            w, h = img.width(), img.height()
            s = min(W / w, H / h) * self.framing["zoom"] / 100.0
            sw, sh = w * s, h * s
            self._drawn_size = (sw, sh)
            cx = W / 2 + (self.framing["x"] / 100.0) * sw / 2
            cy = H / 2 + (self.framing["y"] / 100.0) * sh / 2
            p.drawImage(QRectF(cx - sw / 2, cy - sh / 2, sw, sh), img)
        elif self._message:
            p.setPen(Qt.gray)
            p.setFont(QFont("Arial", self._font_size))
            p.drawText(self.rect(), Qt.AlignCenter, self._message)


class InteractiveView(FramedView):
    """FramedView you can drag to move and scroll to zoom."""
    edited = Signal(dict)

    def __init__(self):
        super().__init__(font_size=18)
        self.setCursor(Qt.OpenHandCursor)
        self._drag_from = None
        self._drag_start = None

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._drag_from = QPointF(e.position())
            self._drag_start = dict(self.framing)
            self.setCursor(Qt.ClosedHandCursor)

    def mouseMoveEvent(self, e):
        if self._drag_from is None:
            return
        d = e.position() - self._drag_from
        sw, sh = self._drawn_size
        f = dict(self._drag_start)
        f["x"] = round(f["x"] + d.x() / (sw / 2) * 100)
        f["y"] = round(f["y"] + d.y() / (sh / 2) * 100)
        f = clean_framing(f)
        if f != self.framing:
            self.edited.emit(f)

    def mouseReleaseEvent(self, _):
        self._drag_from = None
        self.setCursor(Qt.OpenHandCursor)

    def wheelEvent(self, e):
        step = 10 if e.angleDelta().y() > 0 else -10
        f = dict(self.framing)
        f["zoom"] += step
        f = clean_framing(f)
        if f != self.framing:
            self.edited.emit(f)


# --------------------------------------------------------------------------- #
# Full-screen output window
# --------------------------------------------------------------------------- #
class OutputWindow(FramedView):
    closed = Signal()

    def __init__(self):
        super().__init__(font_size=24)
        self.setWindowTitle("NDI Output")
        self.setCursor(Qt.BlankCursor)

    def show_on_screen(self, screen):
        self.hide()
        self.setScreen(screen)
        self.setGeometry(screen.geometry())
        self.showFullScreen()

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape:
            self.close()

    def closeEvent(self, e):
        self.closed.emit()
        super().closeEvent(e)


# --------------------------------------------------------------------------- #
# Framing editor
# --------------------------------------------------------------------------- #
class EditDialog(QDialog):
    framing_changed = Signal(str, dict)

    def __init__(self, ndi_name: str, framing: dict, parent=None):
        super().__init__(parent)
        self.ndi_name = ndi_name
        self.graveyard = parent._graveyard if parent else set()
        self.setWindowTitle(f"Edit framing – {ndi_name}")
        self.setStyleSheet(DARK_STYLE)
        self._busy = False

        self.view = InteractiveView()
        self.view.setMinimumSize(640, 360)
        self.view.setToolTip("Drag to move · scroll to zoom")
        self.view.edited.connect(self._set_all)

        hint = QLabel("Drag the picture to move it, scroll to zoom, or use the controls below. "
                      "Arrow keys move, +/- zoom.")
        hint.setStyleSheet("color:#9aa3ad;")
        hint.setWordWrap(True)

        self.zoom_s, self.zoom_n = self._slider_pair(ZOOM_MIN, ZOOM_MAX, "%")
        self.x_s, self.x_n = self._slider_pair(-PAN_RANGE, PAN_RANGE, "%")
        self.y_s, self.y_n = self._slider_pair(-PAN_RANGE, PAN_RANGE, "%")

        grid = QGridLayout()
        grid.addWidget(QLabel("Zoom"), 0, 0)
        grid.addWidget(self._btn("−", lambda: self._nudge("zoom", -10)), 0, 1)
        grid.addWidget(self.zoom_s, 0, 2)
        grid.addWidget(self._btn("+", lambda: self._nudge("zoom", 10)), 0, 3)
        grid.addWidget(self.zoom_n, 0, 4)

        grid.addWidget(QLabel("Left / Right"), 1, 0)
        grid.addWidget(self._btn("◀", lambda: self._nudge("x", -2)), 1, 1)
        grid.addWidget(self.x_s, 1, 2)
        grid.addWidget(self._btn("▶", lambda: self._nudge("x", 2)), 1, 3)
        grid.addWidget(self.x_n, 1, 4)

        grid.addWidget(QLabel("Up / Down"), 2, 0)
        grid.addWidget(self._btn("▲", lambda: self._nudge("y", -2)), 2, 1)
        grid.addWidget(self.y_s, 2, 2)
        grid.addWidget(self._btn("▼", lambda: self._nudge("y", 2)), 2, 3)
        grid.addWidget(self.y_n, 2, 4)
        grid.setColumnStretch(2, 1)

        reset = QPushButton("Reset")
        reset.setStyleSheet(BTN_IDLE)
        reset.clicked.connect(lambda: self._set_all(dict(DEFAULT_FRAMING)))
        close = QPushButton("Done")
        close.setStyleSheet(BTN_IDLE)
        close.clicked.connect(self.close)
        bottom = QHBoxLayout()
        bottom.addWidget(reset)
        bottom.addStretch()
        bottom.addWidget(close)

        lay = QVBoxLayout(self)
        lay.addWidget(self.view, 1)
        lay.addWidget(hint)
        lay.addLayout(grid)
        lay.addLayout(bottom)

        self._set_all(clean_framing(framing), emit=False)

        self.receiver = ReceiverThread(ndi_name)
        self.receiver.frame_ready.connect(self.view.set_image)
        self.receiver.status.connect(self.view.set_message)
        self.receiver.start()

    def _btn(self, text, fn):
        b = QPushButton(text)
        b.setFixedWidth(36)
        b.setStyleSheet(BTN_SMALL)
        b.setAutoRepeat(True)
        b.clicked.connect(fn)
        return b

    def _slider_pair(self, lo, hi, suffix):
        s = QSlider(Qt.Horizontal)
        s.setRange(lo, hi)
        n = QSpinBox()
        n.setRange(lo, hi)
        n.setSuffix(suffix)
        n.setFixedWidth(80)
        s.valueChanged.connect(self._from_controls)
        n.valueChanged.connect(lambda v, s=s: s.setValue(v))
        return s, n

    def current(self) -> dict:
        return {"zoom": self.zoom_s.value(), "x": self.x_s.value(), "y": self.y_s.value()}

    def _from_controls(self, _=None):
        if not self._busy:
            self._set_all(self.current())

    def _set_all(self, f: dict, emit: bool = True):
        f = clean_framing(f)
        self._busy = True
        for (s, n), key in ((self.zoom_s_pair(), "zoom"), (self.x_pair(), "x"), (self.y_pair(), "y")):
            s.setValue(f[key])
            n.setValue(f[key])
        self._busy = False
        self.view.set_framing(f)
        if emit:
            self.framing_changed.emit(self.ndi_name, f)

    def zoom_s_pair(self):
        return self.zoom_s, self.zoom_n

    def x_pair(self):
        return self.x_s, self.x_n

    def y_pair(self):
        return self.y_s, self.y_n

    def _nudge(self, key, delta):
        f = self.current()
        f[key] += delta
        self._set_all(f)

    def keyPressEvent(self, e):
        k = e.key()
        moves = {Qt.Key_Left: ("x", -2), Qt.Key_Right: ("x", 2),
                 Qt.Key_Up: ("y", -2), Qt.Key_Down: ("y", 2),
                 Qt.Key_Plus: ("zoom", 10), Qt.Key_Equal: ("zoom", 10),
                 Qt.Key_Minus: ("zoom", -10)}
        if k in moves:
            self._nudge(*moves[k])
        else:
            super().keyPressEvent(e)

    def reject(self):  # Esc key -> close properly so the receiver stops
        self.close()

    def closeEvent(self, e):
        retire_thread(self.receiver, self.graveyard)
        self.receiver = None
        super().closeEvent(e)


# --------------------------------------------------------------------------- #
# Source card
# --------------------------------------------------------------------------- #
class SourceCard(QFrame):
    live_clicked = Signal(str)
    edit_clicked = Signal(str)
    hide_clicked = Signal(str)

    def __init__(self, ndi_name: str, show_thumb: bool, framing: dict, graveyard: set):
        super().__init__()
        self.ndi_name = ndi_name
        self.graveyard = graveyard
        self.preview = None
        self.setObjectName("card")
        self.setFixedWidth(CARD_W)
        self._set_border(False)

        self.thumb = FramedView(font_size=11)
        self.thumb.setFixedSize(QSize(THUMB_W, THUMB_H))
        self.thumb.set_framing(framing)

        machine, src = split_name(ndi_name)
        m = QLabel(machine)
        m.setStyleSheet("color:white; font-size:15px; font-weight:bold; border:none;")
        m.setWordWrap(True)
        s = QLabel(src or " ")
        s.setStyleSheet("color:#9aa3ad; font-size:12px; border:none;")
        s.setWordWrap(True)
        self.setToolTip(ndi_name)

        self.button = QPushButton("LIVE")
        self.button.setCursor(Qt.PointingHandCursor)
        self.button.clicked.connect(lambda: self.live_clicked.emit(self.ndi_name))

        edit = QPushButton("Edit")
        edit.setToolTip("Zoom and position this input")
        edit.setCursor(Qt.PointingHandCursor)
        edit.setStyleSheet(BTN_SMALL)
        edit.clicked.connect(lambda: self.edit_clicked.emit(self.ndi_name))

        hide = QPushButton("Hide")
        hide.setToolTip("Hide this input (restore it from View ▸ Hidden inputs)")
        hide.setCursor(Qt.PointingHandCursor)
        hide.setStyleSheet(BTN_SMALL)
        hide.clicked.connect(lambda: self.hide_clicked.emit(self.ndi_name))

        buttons = QHBoxLayout()
        buttons.addWidget(self.button, 1)
        buttons.addWidget(edit)
        buttons.addWidget(hide)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.setSpacing(6)
        lay.addWidget(self.thumb)
        lay.addWidget(m)
        lay.addWidget(s)
        lay.addLayout(buttons)
        self.set_live(False)
        self.set_thumbnails(show_thumb)

    def _set_border(self, live: bool):
        colour = "#d11a2a" if live else "#333"
        self.setStyleSheet(f"QFrame#card {{ background:#1c1f24; border:2px solid {colour};"
                           " border-radius:10px; }")

    def set_thumbnails(self, on: bool):
        self.thumb.setVisible(on)
        if on and self.preview is None:
            self.thumb.set_message("Connecting…")
            self.preview = ReceiverThread(self.ndi_name, preview=True)
            self.preview.frame_ready.connect(self.thumb.set_image)
            self.preview.status.connect(self.thumb.set_message)
            self.preview.start()
        elif not on and self.preview is not None:
            retire_thread(self.preview, self.graveyard)
            self.preview = None
            self.thumb.clear()

    def set_framing(self, f: dict):
        self.thumb.set_framing(f)

    def set_live(self, live: bool):
        self.button.setStyleSheet(BTN_LIVE if live else BTN_IDLE)
        self.button.setText("● LIVE" if live else "LIVE")
        self._set_border(live)

    def shutdown(self):
        retire_thread(self.preview, self.graveyard)
        self.preview = None


# --------------------------------------------------------------------------- #
# Main window
# --------------------------------------------------------------------------- #
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.resize(1200, 780)
        self.setStyleSheet(DARK_STYLE)

        self.settings = QSettings(APP_NAME, APP_NAME)
        self.hidden: set[str] = set(self.settings.value("hidden", [], type=list) or [])
        self.show_thumbs: bool = self.settings.value("thumbnails", True, type=bool)
        try:
            raw = json.loads(self.settings.value("framing", "{}", type=str) or "{}")
            self.framing = {k: clean_framing(v) for k, v in raw.items()}
        except (ValueError, AttributeError):
            self.framing = {}
        self.saved_screen = self.settings.value("output_screen", "", type=str)

        self.online: list[str] = []
        self.cards: dict[str, SourceCard] = {}
        self.editors: dict[str, EditDialog] = {}
        self._graveyard: set = set()
        self.live_source: str | None = None
        self.receiver: ReceiverThread | None = None
        self.output = OutputWindow()
        self.output.closed.connect(self._take_off_air)

        self._build_menus()

        root = QWidget(objectName="root")
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)

        bar = QHBoxLayout()
        title = QLabel("NDI Sources")
        title.setStyleSheet("font-size:22px; font-weight:bold;")
        self.count_label = QLabel("Searching…")
        self.count_label.setStyleSheet("color:#9aa3ad;")

        self.hidden_btn = QToolButton()
        self.hidden_btn.setPopupMode(QToolButton.InstantPopup)
        self.hidden_btn.setMenu(self.hidden_menu)

        self.screen_combo = QComboBox()
        self.screen_combo.setMinimumWidth(260)
        self._populate_screens(prefer=self.saved_screen)
        self.screen_combo.currentIndexChanged.connect(self._save_screen)
        app = QGuiApplication.instance()
        app.screenAdded.connect(lambda _: self._populate_screens())
        app.screenRemoved.connect(lambda _: self._populate_screens())

        bar.addWidget(title)
        bar.addSpacing(12)
        bar.addWidget(self.count_label)
        bar.addStretch()
        bar.addWidget(self.hidden_btn)
        bar.addSpacing(12)
        bar.addWidget(QLabel("Output display:"))
        bar.addWidget(self.screen_combo)
        outer.addLayout(bar)

        self.grid_host = QWidget()
        self.grid_host.setStyleSheet("background:#121417;")
        self.grid = QGridLayout(self.grid_host)
        self.grid.setSpacing(GRID_SPACING)
        self.grid.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setWidget(self.grid_host)
        self.scroll.setStyleSheet("QScrollArea { border:none; }")
        outer.addWidget(self.scroll)

        self._update_hidden_button()

        self.discovery = DiscoveryThread()
        self.discovery.sources_changed.connect(self._on_sources)
        self.discovery.start()

    # ---- menus ----
    def _build_menus(self):
        mb = self.menuBar()

        file_m = mb.addMenu("&File")
        a = QAction("&Open Settings…", self, shortcut=QKeySequence.Open)
        a.triggered.connect(self._open_settings_file)
        file_m.addAction(a)
        a = QAction("&Save Settings…", self, shortcut=QKeySequence.Save)
        a.triggered.connect(self._save_settings_file)
        file_m.addAction(a)
        file_m.addSeparator()
        a = QAction("Reset All Framing", self)
        a.triggered.connect(self._reset_all_framing)
        file_m.addAction(a)
        file_m.addSeparator()
        a = QAction("&Quit", self, shortcut=QKeySequence("Ctrl+Q"))
        a.setMenuRole(QAction.QuitRole)
        a.triggered.connect(self.close)
        file_m.addAction(a)

        view_m = mb.addMenu("&View")
        self.thumb_action = QAction("Show &Thumbnails", self, checkable=True,
                                    shortcut=QKeySequence("Ctrl+T"))
        self.thumb_action.setChecked(self.show_thumbs)
        self.thumb_action.toggled.connect(self._set_thumbnails)
        view_m.addAction(self.thumb_action)
        view_m.addSeparator()
        self.hidden_menu = QMenu("&Hidden Inputs", self)
        self.hidden_menu.aboutToShow.connect(self._build_hidden_menu)
        view_m.addMenu(self.hidden_menu)

        out_m = mb.addMenu("&Output")
        self.edit_live_action = QAction("&Edit Framing of Live Input…", self,
                                        shortcut=QKeySequence("Ctrl+E"))
        self.edit_live_action.triggered.connect(
            lambda: self.live_source and self._open_editor(self.live_source))
        out_m.addAction(self.edit_live_action)
        self.off_air_action = QAction("Take &Off Air", self, shortcut=QKeySequence("Ctrl+B"))
        self.off_air_action.triggered.connect(self._off_air_and_close)
        out_m.addAction(self.off_air_action)
        out_m.aboutToShow.connect(self._update_output_menu)
        self._update_output_menu()

        help_m = mb.addMenu("&Help")
        a = QAction(f"&About {APP_NAME}", self)
        a.setMenuRole(QAction.AboutRole)
        a.triggered.connect(self._about)
        help_m.addAction(a)

    def _update_output_menu(self):
        on = self.live_source is not None
        self.edit_live_action.setEnabled(on)
        self.off_air_action.setEnabled(on)

    def _about(self):
        QMessageBox.about(self, APP_NAME,
                          f"<b>{APP_NAME}</b> v{APP_VERSION}<br><br>"
                          "Shows NDI sources on your network. Click LIVE to send a "
                          "source full screen to the output display.<br><br>"
                          "Shortcuts: Ctrl+T thumbnails · Ctrl+E edit live framing · "
                          "Ctrl+B take off air · Esc on output closes it · Ctrl+Q quit.")

    # ---- screens ----
    def _populate_screens(self, prefer: str = ""):
        current = self.screen_combo.currentData()
        self.screen_combo.blockSignals(True)
        self.screen_combo.clear()
        screens = QGuiApplication.screens()
        primary = QGuiApplication.primaryScreen()
        for i, sc in enumerate(screens):
            g = sc.geometry()
            tag = " (main)" if sc is primary else ""
            self.screen_combo.addItem(f"{i + 1}: {sc.name()} {g.width()}x{g.height()}{tag}", i)
        idx = next((i for i, sc in enumerate(screens) if sc is not primary), 0)
        if prefer:
            idx = next((i for i, sc in enumerate(screens) if sc.name() == prefer), idx)
        elif current is not None and current < len(screens):
            idx = current
        self.screen_combo.setCurrentIndex(idx)
        self.screen_combo.blockSignals(False)

    def _selected_screen(self):
        screens = QGuiApplication.screens()
        i = self.screen_combo.currentData() or 0
        return screens[i] if i < len(screens) else QGuiApplication.primaryScreen()

    def _save_screen(self, *_):
        self.settings.setValue("output_screen", self._selected_screen().name())

    # ---- sources / grid ----
    def _on_sources(self, names: list):
        self.online = names
        self._sync_cards()

    def _sync_cards(self):
        wanted = {n for n in self.online if n not in self.hidden}
        if self.live_source:
            wanted.add(self.live_source)
        for n in list(self.cards):
            if n not in wanted:
                card = self.cards.pop(n)
                card.shutdown()
                self.grid.removeWidget(card)
                card.deleteLater()
        for n in wanted:
            if n not in self.cards:
                card = SourceCard(n, self.show_thumbs, self.framing.get(n, DEFAULT_FRAMING),
                                  self._graveyard)
                card.live_clicked.connect(self._on_live_clicked)
                card.edit_clicked.connect(self._open_editor)
                card.hide_clicked.connect(self._hide_source)
                card.set_live(n == self.live_source)
                self.cards[n] = card
        self._relayout()

        shown = len(self.cards)
        hidden_online = sum(1 for n in self.online if n in self.hidden)
        if not self.online:
            self.count_label.setText("No NDI sources found yet…")
        else:
            txt = f"{shown} source{'s' if shown != 1 else ''} shown"
            if hidden_online:
                txt += f" · {hidden_online} hidden"
            self.count_label.setText(txt)
        self._update_hidden_button()

    def _relayout(self):
        for card in self.cards.values():
            self.grid.removeWidget(card)
        avail = self.scroll.viewport().width() - 20
        cols = max(1, avail // (CARD_W + GRID_SPACING))
        for i, n in enumerate(sorted(self.cards)):
            self.grid.addWidget(self.cards[n], i // cols, i % cols)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._relayout()

    # ---- thumbnails ----
    def _set_thumbnails(self, on: bool):
        self.show_thumbs = on
        self.settings.setValue("thumbnails", on)
        for card in self.cards.values():
            card.set_thumbnails(on)

    # ---- hiding ----
    def _hide_source(self, name: str):
        if name == self.live_source:
            self._off_air_and_close()
        self.hidden.add(name)
        self._save_hidden()
        self._sync_cards()

    def _unhide(self, name: str):
        self.hidden.discard(name)
        self._save_hidden()
        self._sync_cards()

    def _unhide_all(self):
        self.hidden.clear()
        self._save_hidden()
        self._sync_cards()

    def _save_hidden(self):
        self.settings.setValue("hidden", sorted(self.hidden))

    def _update_hidden_button(self):
        n = len(self.hidden)
        self.hidden_btn.setText(f"Hidden ({n}) ▾")
        self.hidden_btn.setEnabled(n > 0)

    def _build_hidden_menu(self):
        self.hidden_menu.clear()
        if not self.hidden:
            self.hidden_menu.addAction("No hidden inputs").setEnabled(False)
            return
        for name in sorted(self.hidden):
            label = name if name in self.online else f"{name}   (offline)"
            act = self.hidden_menu.addAction(f"Show  {label}")
            act.triggered.connect(lambda _=False, n=name: self._unhide(n))
        self.hidden_menu.addSeparator()
        self.hidden_menu.addAction("Show all").triggered.connect(self._unhide_all)

    # ---- framing ----
    def _open_editor(self, name: str):
        dlg = self.editors.get(name)
        if dlg is None:
            dlg = EditDialog(name, self.framing.get(name, DEFAULT_FRAMING), self)
            dlg.framing_changed.connect(self._apply_framing)
            dlg.finished.connect(lambda _=0, n=name: self.editors.pop(n, None))
            dlg.setAttribute(Qt.WA_DeleteOnClose)
            dlg.destroyed.connect(lambda _=None, n=name: self.editors.pop(n, None))
            self.editors[name] = dlg
            dlg.resize(760, 640)
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()

    def _apply_framing(self, name: str, f: dict):
        f = clean_framing(f)
        if f == DEFAULT_FRAMING:
            self.framing.pop(name, None)
        else:
            self.framing[name] = f
        if name in self.cards:
            self.cards[name].set_framing(f)
        if name == self.live_source:
            self.output.set_framing(f)
        self._save_framing()

    def _save_framing(self):
        self.settings.setValue("framing", json.dumps(self.framing))

    def _reset_all_framing(self):
        if QMessageBox.question(self, APP_NAME, "Reset zoom and position for every input?") \
                != QMessageBox.Yes:
            return
        for name in list(self.framing):
            self._apply_framing(name, DEFAULT_FRAMING)
        for dlg in list(self.editors.values()):
            dlg._set_all(dict(DEFAULT_FRAMING), emit=False)

    # ---- settings files ----
    def _settings_dict(self) -> dict:
        return {
            "app": APP_NAME,
            "version": APP_VERSION,
            "show_thumbnails": self.show_thumbs,
            "hidden_inputs": sorted(self.hidden),
            "framing": self.framing,
            "output_screen": self._selected_screen().name(),
        }

    def _save_settings_file(self):
        path, _ = QFileDialog.getSaveFileName(self, "Save Settings", "ndi-live-settings.json",
                                              "Settings (*.json)")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(self._settings_dict(), fh, indent=2)
            self.statusBar().showMessage(f"Settings saved to {path}", 5000)
        except OSError as err:
            QMessageBox.warning(self, APP_NAME, f"Could not save settings:\n{err}")

    def _open_settings_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "Open Settings", "", "Settings (*.json)")
        if not path:
            return
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            if not isinstance(data, dict):
                raise ValueError("not a settings file")
        except (OSError, ValueError) as err:
            QMessageBox.warning(self, APP_NAME, f"Could not open settings:\n{err}")
            return

        self.hidden = set(str(n) for n in data.get("hidden_inputs", []))
        self._save_hidden()

        new_framing = {str(k): clean_framing(v) for k, v in
                       (data.get("framing") or {}).items()}
        for name in set(self.framing) | set(new_framing):
            self._apply_framing(name, new_framing.get(name, DEFAULT_FRAMING))
        for name, dlg in self.editors.items():
            dlg._set_all(self.framing.get(name, DEFAULT_FRAMING), emit=False)

        if self.live_source in self.hidden:
            self._off_air_and_close()

        screen = data.get("output_screen")
        if screen:
            self._populate_screens(prefer=str(screen))
            self._save_screen()

        self.thumb_action.setChecked(bool(data.get("show_thumbnails", True)))
        self._sync_cards()
        self.statusBar().showMessage(f"Settings loaded from {path}", 5000)

    # ---- live switching ----
    def _on_live_clicked(self, name: str):
        if name == self.live_source:
            self._off_air_and_close()
        else:
            self._go_live(name)

    def _go_live(self, name: str):
        self._stop_receiver()
        if self.live_source in self.cards:
            self.cards[self.live_source].set_live(False)
        self.live_source = name
        self.cards[name].set_live(True)

        self.output.set_framing(self.framing.get(name, DEFAULT_FRAMING))
        self.output.set_message("Connecting…")
        if not self.output.isVisible():
            self.output.show_on_screen(self._selected_screen())

        self.receiver = ReceiverThread(name)
        self.receiver.frame_ready.connect(self.output.set_image)
        self.receiver.status.connect(self.output.set_message)
        self.receiver.start()

    def _off_air_and_close(self):
        self._take_off_air()
        self.output.close()

    def _take_off_air(self):
        self._stop_receiver()
        was = self.live_source
        self.live_source = None
        if was in self.cards:
            self.cards[was].set_live(False)
        if was and (was in self.hidden or was not in self.online):
            self._sync_cards()

    def _stop_receiver(self):
        if self.receiver is not None:
            retire_thread(self.receiver, self._graveyard)
            self.receiver = None

    def closeEvent(self, e):
        for dlg in list(self.editors.values()):
            dlg.close()
        self._stop_receiver()
        self.output.close()
        for card in self.cards.values():
            card.shutdown()
        self.discovery.stop()
        for t in list(self._graveyard):
            t.wait(2500)
        super().closeEvent(e)
        QApplication.quit()


def main():
    if not ndi.initialize():
        print("NDI could not be initialised. Is the NDI runtime installed?")
        sys.exit(1)
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    win = MainWindow()
    win.show()
    code = app.exec()
    ndi.destroy()
    sys.exit(code)


if __name__ == "__main__":
    main()
