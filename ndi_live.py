"""
NDI Live Switcher
-----------------
Discovers every NDI source on the network and shows them as a grid of cards:
an optional live thumbnail, the machine / source name and buttons.

LIVE      -> that feed (or holding image) goes full screen on the output
             display. Switching holds the current picture until the new
             input is ready, then cuts or fades to it (Output > Transition).
Edit      -> framing editor: zoom in and move the picture.
Hide      -> removes an unwanted input from the grid.
Holding   -> add images (File > Add Holding Image) to switch to when there
             is no input to go to.
Network   -> View > NDI Network Settings: extra IPs / groups for sources on
             other subnets, plus Refresh.

Requires: NDI runtime/tools installed, plus `pip install -r requirements.txt`.
"""

import json
import os
import sys
import time

import numpy as np
import NDIlib as ndi
from PySide6.QtCore import (
    QPointF, QRectF, QSettings, QSize, Qt, QThread, QTimer, QVariantAnimation,
    Signal,
)
from PySide6.QtGui import (
    QAction, QActionGroup, QFont, QGuiApplication, QImage, QKeySequence,
    QPainter,
)
from PySide6.QtWidgets import (
    QApplication, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
    QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMenu,
    QMessageBox, QPushButton, QScrollArea, QSlider, QSpinBox, QToolButton,
    QVBoxLayout, QWidget,
)

APP_NAME = "NDI Live Switcher"
APP_VERSION = "1.3"
REFRESH_MS = 1000
THUMB_W, THUMB_H = 256, 144
THUMB_FPS = 5
CARD_W = THUMB_W + 20
GRID_SPACING = 16
ZOOM_MIN, ZOOM_MAX = 100, 500
PAN_RANGE = 100
DEFAULT_FRAMING = {"zoom": 100, "x": 0, "y": 0}
TRANSITIONS = [("Cut", 0), ("Fade 0.25 s", 250), ("Fade 0.5 s", 500), ("Fade 1 s", 1000)]
NO_SIGNAL_MS = 8000
IMAGE_FILTER = "Images (*.png *.jpg *.jpeg *.bmp *.gif *.tif *.tiff *.webp)"

# NDI discovery options (shared by every finder the app creates)
NET = {"extra_ips": "", "groups": ""}

DARK_STYLE = """
QMainWindow, QWidget#root, QDialog { background:#121417; }
QLabel { color:#e6e6e6; }
QToolButton { color:#e6e6e6; background:#2b2f36; border:1px solid #444;
              border-radius:6px; padding:5px 10px; }
QToolButton:hover { background:#3a3f48; }
QMenuBar { background:#1c1f24; color:#e6e6e6; }
QMenuBar::item:selected { background:#3a3f48; }
QMenu { background:#1c1f24; color:#e6e6e6; border:1px solid #333; }
QMenu::item:selected { background:#3a3f48; }
QMenu::item:disabled { color:#666; }
QSpinBox, QComboBox, QLineEdit { background:#1c1f24; color:#e6e6e6;
              border:1px solid #444; border-radius:4px; padding:4px; }
QStatusBar { color:#9aa3ad; }
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


def make_finder():
    """Create an NDI finder using the current network settings."""
    fc = ndi.FindCreate()
    fc.show_local_sources = True
    if NET["extra_ips"].strip():
        fc.extra_ips = NET["extra_ips"].strip()
    if NET["groups"].strip():
        fc.groups = NET["groups"].strip()
    return ndi.find_create_v2(fc)


def draw_framed(p: QPainter, W: int, H: int, img: QImage, framing: dict):
    """Draw img into a W x H area with zoom/pan; returns the drawn size."""
    w, h = img.width(), img.height()
    s = min(W / w, H / h) * framing["zoom"] / 100.0
    sw, sh = w * s, h * s
    cx = W / 2 + (framing["x"] / 100.0) * sw / 2
    cy = H / 2 + (framing["y"] / 100.0) * sh / 2
    p.drawImage(QRectF(cx - sw / 2, cy - sh / 2, sw, sh), img)
    return sw, sh


def valid(img) -> bool:
    return img is not None and not img.isNull() and img.width() > 0 and img.height() > 0


# --------------------------------------------------------------------------- #
# Threads
# --------------------------------------------------------------------------- #
class DiscoveryThread(QThread):
    sources_changed = Signal(list)

    def __init__(self):
        super().__init__()
        self._running = True

    def run(self):
        finder = make_finder()
        if finder is None:
            return
        last = None
        try:
            while self._running:
                names = sorted(s.ndi_name for s in ndi.find_get_current_sources(finder))
                if names != last:
                    last = names
                    self.sources_changed.emit(names)
                ndi.find_wait_for_sources(finder, REFRESH_MS)
        finally:
            ndi.find_destroy(finder)

    def stop(self, wait: bool = True):
        self._running = False
        if wait:
            self.wait(REFRESH_MS + 1500)


class ReceiverThread(QThread):
    """Connects to one named NDI source and emits video frames as QImages."""
    frame_ready = Signal(QImage)
    status = Signal(str)

    def __init__(self, source_name: str, preview: bool = False):
        super().__init__()
        self.source_name = source_name
        self.preview = preview
        self._running = True

    def _find_source(self, finder):
        while self._running:
            for s in ndi.find_get_current_sources(finder):
                if s.ndi_name == self.source_name:
                    return s
            ndi.find_wait_for_sources(finder, 200)
        return None

    def run(self):
        self.status.emit("Connecting…")
        finder = make_finder()
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

        try:
            while self._running:
                t, v, a, m = ndi.recv_capture_v2(recv, 200)
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
                    if self._running:
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


def retire_thread(thread, graveyard: set):
    """Stop a worker thread without blocking the UI; keep a ref until done."""
    if thread is None:
        return
    for name in ("frame_ready", "status", "sources_changed"):
        sig = getattr(thread, name, None)
        if sig is not None:
            try:
                sig.disconnect()
            except (RuntimeError, TypeError):
                pass
    graveyard.add(thread)
    thread.finished.connect(lambda: graveyard.discard(thread))
    thread.stop(wait=False)


# --------------------------------------------------------------------------- #
# Views
# --------------------------------------------------------------------------- #
class FramedView(QWidget):
    """Draws a frame with zoom + pan applied, or a message."""

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
        if valid(self._image):
            p.setRenderHint(QPainter.SmoothPixmapTransform)
            self._drawn_size = draw_framed(p, self.width(), self.height(),
                                           self._image, self.framing)
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
        f["x"] = round(f["x"] + d.x() / max(1.0, sw / 2) * 100)
        f["y"] = round(f["y"] + d.y() / max(1.0, sh / 2) * 100)
        f = clean_framing(f)
        if f != self.framing:
            self.edited.emit(f)

    def mouseReleaseEvent(self, _):
        self._drag_from = None
        self.setCursor(Qt.OpenHandCursor)

    def wheelEvent(self, e):
        f = dict(self.framing)
        f["zoom"] += 10 if e.angleDelta().y() > 0 else -10
        f = clean_framing(f)
        if f != self.framing:
            self.edited.emit(f)


class OutputWindow(FramedView):
    """Full-screen output. Holds the current picture while the next input
    connects, then cuts or cross-fades to it."""
    closed = Signal()

    def __init__(self):
        super().__init__(font_size=24)
        self.setWindowTitle("NDI Output")
        self.setCursor(Qt.BlankCursor)
        self.fade_ms = 500
        self._old = None
        self._old_framing = dict(DEFAULT_FRAMING)
        self._alpha = 1.0
        self._pending = False
        self._pending_framing = dict(DEFAULT_FRAMING)
        self._pending_label = ""

        self._anim = QVariantAnimation(self)
        self._anim.setStartValue(0.0)
        self._anim.setEndValue(1.0)
        self._anim.valueChanged.connect(self._on_anim)
        self._anim.finished.connect(self._end_fade)

        self._no_signal = QTimer(self)
        self._no_signal.setSingleShot(True)
        self._no_signal.setInterval(NO_SIGNAL_MS)
        self._no_signal.timeout.connect(self._on_no_signal)

    # -- switching --
    def prepare_switch(self, framing: dict, label: str):
        """Next frame that arrives belongs to a new input."""
        self._pending = True
        self._pending_framing = clean_framing(framing)
        self._pending_label = label
        self._no_signal.start()

    def set_live_framing(self, f: dict):
        if self._pending:
            self._pending_framing = clean_framing(f)
        else:
            self.set_framing(f)

    def set_image(self, img: QImage):
        if self._pending:
            self._pending = False
            self._no_signal.stop()
            self._anim.stop()
            if self.fade_ms > 0 and self.isVisible():
                self._old = self._image if valid(self._image) else None
                self._old_framing = dict(self.framing)
                self._alpha = 0.0
                self._anim.setDuration(self.fade_ms)
                self._anim.start()
            else:
                self._old = None
                self._alpha = 1.0
            self.framing = self._pending_framing
        self._image = img
        self._message = ""
        self.update()

    def set_message(self, text: str):
        # Never replace a picture with a status message: keep holding it.
        if text and not valid(self._image):
            self._message = text
            self.update()

    def _on_no_signal(self):
        if self._pending:
            self._pending = False
            self._image = None
            self._old = None
            self._message = f"No signal from {self._pending_label}"
            self.update()

    def _on_anim(self, v):
        self._alpha = float(v)
        self.update()

    def _end_fade(self):
        self._old = None
        self._alpha = 1.0
        self.update()

    def reset(self):
        self._anim.stop()
        self._no_signal.stop()
        self._pending = False
        self._old = None
        self._alpha = 1.0
        self.clear()

    # -- window --
    def show_on_screen(self, screen):
        self.hide()
        self.setScreen(screen)
        self.setGeometry(screen.geometry())
        self.showFullScreen()

    def paintEvent(self, _):
        p = QPainter(self)
        p.fillRect(self.rect(), Qt.black)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        W, H = self.width(), self.height()
        drew = False
        if valid(self._old) and self._alpha < 1.0:
            draw_framed(p, W, H, self._old, self._old_framing)
            drew = True
        if valid(self._image):
            p.setOpacity(self._alpha)
            draw_framed(p, W, H, self._image, self.framing)
            p.setOpacity(1.0)
            drew = True
        if not drew and self._message:
            p.setPen(Qt.gray)
            p.setFont(QFont("Arial", self._font_size))
            p.drawText(self.rect(), Qt.AlignCenter, self._message)

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape:
            self.close()

    def closeEvent(self, e):
        self.closed.emit()
        super().closeEvent(e)


# --------------------------------------------------------------------------- #
# Dialogs
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

        hint = QLabel("Drag the picture to move it, scroll to zoom, or use the controls "
                      "below. Arrow keys move, +/- zoom.")
        hint.setStyleSheet("color:#9aa3ad;")
        hint.setWordWrap(True)

        self.pairs = {
            "zoom": self._slider_pair(ZOOM_MIN, ZOOM_MAX),
            "x": self._slider_pair(-PAN_RANGE, PAN_RANGE),
            "y": self._slider_pair(-PAN_RANGE, PAN_RANGE),
        }
        rows = [("Zoom", "zoom", "−", "+", 10),
                ("Left / Right", "x", "◀", "▶", 2),
                ("Up / Down", "y", "▲", "▼", 2)]
        grid = QGridLayout()
        for r, (label, key, minus, plus, step) in enumerate(rows):
            s, n = self.pairs[key]
            grid.addWidget(QLabel(label), r, 0)
            grid.addWidget(self._btn(minus, lambda _=False, k=key, d=-step: self._nudge(k, d)), r, 1)
            grid.addWidget(s, r, 2)
            grid.addWidget(self._btn(plus, lambda _=False, k=key, d=step: self._nudge(k, d)), r, 3)
            grid.addWidget(n, r, 4)
        grid.setColumnStretch(2, 1)

        reset = QPushButton("Reset")
        reset.setStyleSheet(BTN_IDLE)
        reset.clicked.connect(lambda: self._set_all(dict(DEFAULT_FRAMING)))
        done = QPushButton("Done")
        done.setStyleSheet(BTN_IDLE)
        done.clicked.connect(self.close)
        bottom = QHBoxLayout()
        bottom.addWidget(reset)
        bottom.addStretch()
        bottom.addWidget(done)

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

    def _slider_pair(self, lo, hi):
        s = QSlider(Qt.Horizontal)
        s.setRange(lo, hi)
        n = QSpinBox()
        n.setRange(lo, hi)
        n.setSuffix("%")
        n.setFixedWidth(80)
        s.valueChanged.connect(self._from_controls)
        n.valueChanged.connect(lambda v, s=s: s.setValue(v))
        return s, n

    def current(self) -> dict:
        return {k: s.value() for k, (s, _) in self.pairs.items()}

    def _from_controls(self, _=None):
        if not self._busy:
            self._set_all(self.current())

    def _set_all(self, f: dict, emit: bool = True):
        f = clean_framing(f)
        self._busy = True
        for key, (s, n) in self.pairs.items():
            s.setValue(f[key])
            n.setValue(f[key])
        self._busy = False
        self.view.set_framing(f)
        if emit:
            self.framing_changed.emit(self.ndi_name, f)

    def _nudge(self, key, delta):
        f = self.current()
        f[key] += delta
        self._set_all(f)

    def keyPressEvent(self, e):
        moves = {Qt.Key_Left: ("x", -2), Qt.Key_Right: ("x", 2),
                 Qt.Key_Up: ("y", -2), Qt.Key_Down: ("y", 2),
                 Qt.Key_Plus: ("zoom", 10), Qt.Key_Equal: ("zoom", 10),
                 Qt.Key_Minus: ("zoom", -10)}
        if e.key() in moves:
            self._nudge(*moves[e.key()])
        else:
            super().keyPressEvent(e)

    def reject(self):
        self.close()

    def closeEvent(self, e):
        retire_thread(self.receiver, self.graveyard)
        self.receiver = None
        super().closeEvent(e)


class NetworkDialog(QDialog):
    def __init__(self, parent, online: list):
        super().__init__(parent)
        self.setWindowTitle("NDI Network Settings")
        self.setStyleSheet(DARK_STYLE)
        self.setMinimumWidth(560)

        self.ips = QLineEdit(NET["extra_ips"])
        self.ips.setPlaceholderText("e.g. 192.168.10.21, 192.168.20.0  (leave blank for none)")
        self.groups = QLineEdit(NET["groups"])
        self.groups.setPlaceholderText("e.g. public, studio  (blank = NDI default)")

        form = QFormLayout()
        form.addRow("Extra IP addresses:", self.ips)
        form.addRow("NDI groups:", self.groups)

        found = QLabel(f"<b>{len(online)} source(s) found right now:</b><br>"
                       + ("<br>".join(online) if online else "<i>none</i>"))
        found.setStyleSheet("color:#cfd5dc;")
        found.setWordWrap(True)
        found.setTextInteractionFlags(Qt.TextSelectableByMouse)

        help_text = QLabel(
            "<b>Missing sources?</b><ul style='margin-left:-20px'>"
            "<li>NDI finds sources automatically only on <b>your own subnet</b>. For devices "
            "on another subnet or VLAN, VPN or Wi-Fi, type their IP addresses above.</li>"
            "<li>If a source is in a private <b>NDI group</b>, add the group name above.</li>"
            "<li>If your network uses an <b>NDI Discovery Server</b>, set it up in "
            "<i>NDI Access Manager</i> (part of NDI Tools). This app uses that setting too.</li>"
            "<li>Windows Firewall: allow this app on <b>private and public</b> networks. "
            "Windows often marks wired networks as 'Public'.</li>"
            "<li>Make sure NDI Tools is up to date. Older NDI|HX cameras may also need "
            "the NDI HX driver.</li></ul>")
        help_text.setStyleSheet("color:#9aa3ad;")
        help_text.setWordWrap(True)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("Apply && Refresh")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        lay = QVBoxLayout(self)
        lay.addLayout(form)
        lay.addWidget(found)
        lay.addWidget(help_text)
        lay.addWidget(buttons)

    def values(self):
        return self.ips.text().strip(), self.groups.text().strip()


# --------------------------------------------------------------------------- #
# Cards
# --------------------------------------------------------------------------- #
class BaseCard(QFrame):
    def __init__(self, title: str, subtitle: str, tooltip: str):
        super().__init__()
        self.setObjectName("card")
        self.setFixedWidth(CARD_W)

        self.thumb = FramedView(font_size=11)
        self.thumb.setFixedSize(QSize(THUMB_W, THUMB_H))

        t = QLabel(title)
        t.setStyleSheet("color:white; font-size:15px; font-weight:bold; border:none;")
        t.setWordWrap(True)
        s = QLabel(subtitle or " ")
        s.setStyleSheet("color:#9aa3ad; font-size:12px; border:none;")
        s.setWordWrap(True)
        self.setToolTip(tooltip)

        self.button = QPushButton("LIVE")
        self.button.setCursor(Qt.PointingHandCursor)
        self.buttons = QHBoxLayout()
        self.buttons.addWidget(self.button, 1)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.setSpacing(6)
        lay.addWidget(self.thumb)
        lay.addWidget(t)
        lay.addWidget(s)
        lay.addLayout(self.buttons)
        self.set_live(False)

    def small_button(self, text, tip, fn):
        b = QPushButton(text)
        b.setToolTip(tip)
        b.setCursor(Qt.PointingHandCursor)
        b.setStyleSheet(BTN_SMALL)
        b.clicked.connect(fn)
        self.buttons.addWidget(b)
        return b

    def set_live(self, live: bool):
        self.button.setStyleSheet(BTN_LIVE if live else BTN_IDLE)
        self.button.setText("● LIVE" if live else "LIVE")
        colour = "#d11a2a" if live else self.border_colour()
        self.setStyleSheet(f"QFrame#card {{ background:#1c1f24; border:2px solid {colour};"
                           " border-radius:10px; }")

    def border_colour(self):
        return "#333"


class SourceCard(BaseCard):
    live_clicked = Signal(str)
    edit_clicked = Signal(str)
    hide_clicked = Signal(str)

    def __init__(self, ndi_name: str, show_thumb: bool, framing: dict, graveyard: set):
        machine, src = split_name(ndi_name)
        super().__init__(machine, src, ndi_name)
        self.ndi_name = ndi_name
        self.graveyard = graveyard
        self.preview = None
        self.thumb.set_framing(framing)
        self.button.clicked.connect(lambda: self.live_clicked.emit(self.ndi_name))
        self.small_button("Edit", "Zoom and position this input",
                          lambda: self.edit_clicked.emit(self.ndi_name))
        self.small_button("Hide", "Hide this input (restore from View ▸ Hidden Inputs)",
                          lambda: self.hide_clicked.emit(self.ndi_name))
        self.set_thumbnails(show_thumb)

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

    def shutdown(self):
        retire_thread(self.preview, self.graveyard)
        self.preview = None


class ImageCard(BaseCard):
    live_clicked = Signal(str)
    remove_clicked = Signal(str)

    def __init__(self, path: str, image: QImage, show_thumb: bool):
        super().__init__(os.path.basename(path), "Holding image", path)
        self.path = path
        self.thumb.set_image(image)
        self.thumb.setVisible(show_thumb)
        self.button.clicked.connect(lambda: self.live_clicked.emit(self.path))
        self.small_button("Remove", "Remove this holding image from the app",
                          lambda: self.remove_clicked.emit(self.path))

    def border_colour(self):
        return "#2f4f6f"

    def set_thumbnails(self, on: bool):
        self.thumb.setVisible(on)

    def shutdown(self):
        pass


# --------------------------------------------------------------------------- #
# Main window
# --------------------------------------------------------------------------- #
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.resize(1200, 780)
        self.setStyleSheet(DARK_STYLE)

        st = self.settings = QSettings(APP_NAME, APP_NAME)
        self.hidden: set[str] = set(st.value("hidden", [], type=list) or [])
        self.show_thumbs: bool = st.value("thumbnails", True, type=bool)
        try:
            raw = json.loads(st.value("framing", "{}", type=str) or "{}")
            self.framing = {k: clean_framing(v) for k, v in raw.items()}
        except (ValueError, AttributeError):
            self.framing = {}
        self.saved_screen = st.value("output_screen", "", type=str)
        NET["extra_ips"] = st.value("extra_ips", "", type=str) or ""
        NET["groups"] = st.value("groups", "", type=str) or ""
        fade_ms = st.value("transition_ms", 500, type=int)

        self.online: list[str] = []
        self.cards: dict[str, SourceCard] = {}
        self.holding: list[str] = []
        self.images: dict[str, QImage] = {}
        self.image_cards: dict[str, ImageCard] = {}
        self.editors: dict[str, EditDialog] = {}
        self._graveyard: set = set()
        self.live = None             # ("ndi", name) | ("img", path) | None
        self.receiver = None

        self.output = OutputWindow()
        self.output.fade_ms = fade_ms
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

        add_img = QToolButton()
        add_img.setText("＋ Holding Image")
        add_img.clicked.connect(self._add_holding_image)
        refresh = QToolButton()
        refresh.setText("⟳ Refresh")
        refresh.setToolTip("Search the network again (F5)")
        refresh.clicked.connect(self._restart_discovery)

        self.hidden_btn = QToolButton()
        self.hidden_btn.setPopupMode(QToolButton.InstantPopup)
        self.hidden_btn.setMenu(self.hidden_menu)

        self.screen_combo = QComboBox()
        self.screen_combo.setMinimumWidth(240)
        self._populate_screens(prefer=self.saved_screen)
        self.screen_combo.currentIndexChanged.connect(self._save_screen)
        app = QGuiApplication.instance()
        app.screenAdded.connect(lambda _: self._populate_screens())
        app.screenRemoved.connect(lambda _: self._populate_screens())

        bar.addWidget(title)
        bar.addSpacing(12)
        bar.addWidget(self.count_label)
        bar.addStretch()
        bar.addWidget(add_img)
        bar.addWidget(refresh)
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

        for path in st.value("holding_images", [], type=list) or []:
            self._add_holding_image_path(str(path), save=False, warn=False)
        self._update_hidden_button()

        self.discovery = None
        self._start_discovery()

    # ---- helpers ----
    @property
    def live_ndi(self):
        return self.live[1] if self.live and self.live[0] == "ndi" else None

    def _card_for(self, key):
        if not key:
            return None
        return (self.cards if key[0] == "ndi" else self.image_cards).get(key[1])

    # ---- menus ----
    def _build_menus(self):
        mb = self.menuBar()

        def act(menu, text, fn, shortcut=None, **kw):
            a = QAction(text, self, **kw)
            if shortcut:
                a.setShortcut(QKeySequence(shortcut))
            a.triggered.connect(fn)
            menu.addAction(a)
            return a

        file_m = mb.addMenu("&File")
        act(file_m, "&Open Settings…", self._open_settings_file, QKeySequence.Open)
        act(file_m, "&Save Settings…", self._save_settings_file, QKeySequence.Save)
        file_m.addSeparator()
        act(file_m, "Add &Holding Image…", self._add_holding_image, "Ctrl+I")
        file_m.addSeparator()
        act(file_m, "Reset All Framing", self._reset_all_framing)
        file_m.addSeparator()
        q = act(file_m, "&Quit", self.close, "Ctrl+Q")
        q.setMenuRole(QAction.QuitRole)

        view_m = mb.addMenu("&View")
        self.thumb_action = QAction("Show &Thumbnails", self, checkable=True)
        self.thumb_action.setShortcut(QKeySequence("Ctrl+T"))
        self.thumb_action.setChecked(self.show_thumbs)
        self.thumb_action.toggled.connect(self._set_thumbnails)
        view_m.addAction(self.thumb_action)
        view_m.addSeparator()
        self.hidden_menu = QMenu("&Hidden Inputs", self)
        self.hidden_menu.aboutToShow.connect(self._build_hidden_menu)
        view_m.addMenu(self.hidden_menu)
        view_m.addSeparator()
        act(view_m, "&Refresh Sources", self._restart_discovery, "F5")
        act(view_m, "NDI &Network Settings…", self._network_settings)

        out_m = mb.addMenu("&Output")
        self.hold_action = act(out_m, "Go to &Holding Image", self._go_holding, "Ctrl+H")
        self.edit_live_action = act(out_m, "&Edit Framing of Live Input…",
                                    lambda: self.live_ndi and self._open_editor(self.live_ndi),
                                    "Ctrl+E")
        self.off_air_action = act(out_m, "Take &Off Air", self._off_air_and_close, "Ctrl+B")
        out_m.addSeparator()
        trans_m = out_m.addMenu("&Transition")
        group = QActionGroup(self)
        for label, ms in TRANSITIONS:
            a = QAction(label, self, checkable=True)
            a.setChecked(ms == self.output.fade_ms)
            a.triggered.connect(lambda _=False, v=ms: self._set_transition(v))
            group.addAction(a)
            trans_m.addAction(a)
        self.transition_actions = group
        out_m.aboutToShow.connect(self._update_output_menu)
        self._update_output_menu()

        help_m = mb.addMenu("&Help")
        a = act(help_m, f"&About {APP_NAME}", self._about)
        a.setMenuRole(QAction.AboutRole)

    def _update_output_menu(self):
        self.edit_live_action.setEnabled(self.live_ndi is not None)
        self.off_air_action.setEnabled(self.live is not None)
        self.hold_action.setEnabled(bool(self.holding))

    def _set_transition(self, ms: int):
        self.output.fade_ms = ms
        self.settings.setValue("transition_ms", ms)
        for a in self.transition_actions.actions():
            a.setChecked(dict(TRANSITIONS).get(a.text()) == ms)

    def _about(self):
        QMessageBox.about(self, APP_NAME,
                          f"<b>{APP_NAME}</b> v{APP_VERSION}<br><br>"
                          "Click LIVE to send an NDI source or holding image full screen "
                          "to the output display.<br><br>"
                          "Shortcuts: Ctrl+H holding image · Ctrl+B off air · Ctrl+E edit "
                          "live framing · Ctrl+T thumbnails · Ctrl+I add holding image · "
                          "F5 refresh · Esc on output closes it · Ctrl+Q quit.")

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

    # ---- discovery ----
    def _start_discovery(self):
        self.discovery = DiscoveryThread()
        self.discovery.sources_changed.connect(self._on_sources)
        self.discovery.start()

    def _restart_discovery(self):
        """Search again (e.g. after changing network settings)."""
        retire_thread(self.discovery, self._graveyard)
        for card in self.cards.values():
            card.shutdown()
            self.grid.removeWidget(card)
            card.deleteLater()
        self.cards.clear()
        self.online = []
        self.count_label.setText("Searching…")
        self._start_discovery()
        self._sync_cards()
        self.statusBar().showMessage("Searching the network for NDI sources…", 4000)

    def _network_settings(self):
        dlg = NetworkDialog(self, self.online)
        if dlg.exec() == QDialog.Accepted:
            NET["extra_ips"], NET["groups"] = dlg.values()
            self.settings.setValue("extra_ips", NET["extra_ips"])
            self.settings.setValue("groups", NET["groups"])
            self._restart_discovery()

    # ---- grid ----
    def _on_sources(self, names: list):
        self.online = names
        self._sync_cards()

    def _sync_cards(self):
        wanted = {n for n in self.online if n not in self.hidden}
        if self.live_ndi:
            wanted.add(self.live_ndi)
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
                card.live_clicked.connect(lambda name: self._on_live_clicked(("ndi", name)))
                card.edit_clicked.connect(self._open_editor)
                card.hide_clicked.connect(self._hide_source)
                card.set_live(self.live == ("ndi", n))
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
        ordered = [self.image_cards[p] for p in self.holding if p in self.image_cards]
        ordered += [self.cards[n] for n in sorted(self.cards)]
        for card in ordered:
            self.grid.removeWidget(card)
        avail = self.scroll.viewport().width() - 20
        cols = max(1, avail // (CARD_W + GRID_SPACING))
        for i, card in enumerate(ordered):
            self.grid.addWidget(card, i // cols, i % cols)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._relayout()

    # ---- thumbnails ----
    def _set_thumbnails(self, on: bool):
        self.show_thumbs = on
        self.settings.setValue("thumbnails", on)
        for card in list(self.cards.values()) + list(self.image_cards.values()):
            card.set_thumbnails(on)

    # ---- holding images ----
    def _add_holding_image(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "Add Holding Image", "", IMAGE_FILTER)
        for p in paths:
            self._add_holding_image_path(p)

    def _add_holding_image_path(self, path: str, save: bool = True, warn: bool = True):
        path = os.path.abspath(path)
        if path in self.images:
            return True
        img = QImage(path)
        if img.isNull():
            if warn:
                QMessageBox.warning(self, APP_NAME, f"Could not open image:\n{path}")
            return False
        img = img.convertToFormat(QImage.Format_ARGB32_Premultiplied)
        self.images[path] = img
        self.holding.append(path)
        card = ImageCard(path, img, self.show_thumbs)
        card.live_clicked.connect(lambda p: self._on_live_clicked(("img", p)))
        card.remove_clicked.connect(self._remove_holding_image)
        self.image_cards[path] = card
        self._relayout()
        if save:
            self._save_holding()
        return True

    def _remove_holding_image(self, path: str):
        if self.live == ("img", path):
            self._off_air_and_close()
        card = self.image_cards.pop(path, None)
        if card:
            self.grid.removeWidget(card)
            card.deleteLater()
        self.images.pop(path, None)
        if path in self.holding:
            self.holding.remove(path)
        self._save_holding()
        self._relayout()

    def _save_holding(self):
        self.settings.setValue("holding_images", list(self.holding))

    def _go_holding(self):
        if self.holding:
            self._go_live(("img", self.holding[0]))

    # ---- hiding ----
    def _hide_source(self, name: str):
        if self.live == ("ndi", name):
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
            a = self.hidden_menu.addAction(f"Show  {label}")
            a.triggered.connect(lambda _=False, n=name: self._unhide(n))
        self.hidden_menu.addSeparator()
        self.hidden_menu.addAction("Show all").triggered.connect(self._unhide_all)

    # ---- framing ----
    def _open_editor(self, name: str):
        dlg = self.editors.get(name)
        if dlg is None:
            dlg = EditDialog(name, self.framing.get(name, DEFAULT_FRAMING), self)
            dlg.framing_changed.connect(self._apply_framing)
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
        if self.live == ("ndi", name):
            self.output.set_live_framing(f)
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
            "holding_images": list(self.holding),
            "transition_ms": self.output.fade_ms,
            "output_screen": self._selected_screen().name(),
            "network": dict(NET),
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
        if path:
            self.load_settings_file(path)

    def load_settings_file(self, path: str):
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

        new_framing = {str(k): clean_framing(v)
                       for k, v in (data.get("framing") or {}).items()}
        for name in set(self.framing) | set(new_framing):
            self._apply_framing(name, new_framing.get(name, DEFAULT_FRAMING))
        for name, dlg in self.editors.items():
            dlg._set_all(self.framing.get(name, DEFAULT_FRAMING), emit=False)

        missing = []
        wanted_imgs = [os.path.abspath(str(p)) for p in data.get("holding_images", [])]
        for p in list(self.holding):
            if p not in wanted_imgs:
                self._remove_holding_image(p)
        for p in wanted_imgs:
            if not self._add_holding_image_path(p, save=False, warn=False):
                missing.append(p)
        self._save_holding()

        if "transition_ms" in data:
            try:
                self._set_transition(int(data["transition_ms"]))
            except (TypeError, ValueError):
                pass

        if self.live_ndi in self.hidden:
            self._off_air_and_close()

        screen = data.get("output_screen")
        if screen:
            self._populate_screens(prefer=str(screen))
            self._save_screen()

        self.thumb_action.setChecked(bool(data.get("show_thumbnails", True)))

        net = data.get("network") or {}
        new_net = {"extra_ips": str(net.get("extra_ips", "")), "groups": str(net.get("groups", ""))}
        if new_net != NET:
            NET.update(new_net)
            self.settings.setValue("extra_ips", NET["extra_ips"])
            self.settings.setValue("groups", NET["groups"])
            self._restart_discovery()
        else:
            self._sync_cards()

        self.statusBar().showMessage(f"Settings loaded from {path}", 5000)
        if missing:
            QMessageBox.warning(self, APP_NAME, "These holding images could not be found:\n"
                                + "\n".join(missing))

    # ---- live switching ----
    def _on_live_clicked(self, key):
        if key == self.live:
            self._off_air_and_close()
        else:
            self._go_live(key)

    def _go_live(self, key):
        kind, ident = key
        if kind == "img" and ident not in self.images:
            return
        self._stop_receiver()
        old_card = self._card_for(self.live)
        if old_card:
            old_card.set_live(False)
        self.live = key
        card = self._card_for(key)
        if card:
            card.set_live(True)

        if not self.output.isVisible():
            self.output.reset()
            self.output.show_on_screen(self._selected_screen())

        if kind == "img":
            self.output.prepare_switch(DEFAULT_FRAMING, os.path.basename(ident))
            self.output.set_image(self.images[ident])
        else:
            self.output.prepare_switch(self.framing.get(ident, DEFAULT_FRAMING), ident)
            self.output.set_message("Connecting…")
            rcv = ReceiverThread(ident)
            rcv.frame_ready.connect(lambda img, r=rcv: self._on_live_frame(r, img))
            rcv.status.connect(lambda t, r=rcv: r is self.receiver and self.output.set_message(t))
            self.receiver = rcv
            rcv.start()

    def _on_live_frame(self, rcv, img):
        if rcv is self.receiver:  # ignore late frames from a previous input
            self.output.set_image(img)

    def _off_air_and_close(self):
        self._take_off_air()
        self.output.close()

    def _take_off_air(self):
        self._stop_receiver()
        was = self.live
        self.live = None
        card = self._card_for(was)
        if card:
            card.set_live(False)
        self.output.reset()
        if was and was[0] == "ndi" and (was[1] in self.hidden or was[1] not in self.online):
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
        if self.discovery:
            retire_thread(self.discovery, self._graveyard)
        for t in list(self._graveyard):
            t.wait(3000)
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
