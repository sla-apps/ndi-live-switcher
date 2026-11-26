"""
NDI Live Switcher
-----------------
Discovers every NDI source on the local network, shows them as a grid of
cards (machine name + source name) with a LIVE button under each.

Click LIVE  -> button turns red and that feed plays full screen on the
               selected output display (an external monitor by default).
Click again -> takes it off air and closes the output window.
Only one source is live at a time; clicking another switches to it.
Press Esc on the output window to close it.

Requires: NDI runtime/tools installed, plus `pip install -r requirements.txt`.
"""

import sys
import threading

import numpy as np
import NDIlib as ndi
from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtGui import QGuiApplication, QImage, QPainter, QFont
from PySide6.QtWidgets import (
    QApplication, QComboBox, QFrame, QGridLayout, QHBoxLayout, QLabel,
    QMainWindow, QPushButton, QScrollArea, QVBoxLayout, QWidget,
)

COLUMNS = 4
REFRESH_MS = 2000

BTN_IDLE = """
QPushButton { background:#2b2f36; color:#e6e6e6; border:1px solid #444;
              border-radius:6px; padding:10px; font-weight:bold; font-size:14px; }
QPushButton:hover { background:#3a3f48; }
"""
BTN_LIVE = """
QPushButton { background:#d11a2a; color:white; border:1px solid #ff4d5a;
              border-radius:6px; padding:10px; font-weight:bold; font-size:14px; }
"""


# --------------------------------------------------------------------------- #
# NDI discovery
# --------------------------------------------------------------------------- #
class DiscoveryThread(QThread):
    """Polls the network for NDI sources and emits the sorted list of names."""
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
# NDI receiving
# --------------------------------------------------------------------------- #
class ReceiverThread(QThread):
    """Connects to one named NDI source and emits video frames as QImages."""
    frame_ready = Signal(QImage)
    status = Signal(str)

    def __init__(self, source_name: str):
        super().__init__()
        self.source_name = source_name
        self._running = True

    def _find_source(self):
        finder = ndi.find_create_v2()
        try:
            for _ in range(10):  # up to ~10 s
                if not self._running:
                    return None, finder
                ndi.find_wait_for_sources(finder, 1000)
                for s in ndi.find_get_current_sources(finder):
                    if s.ndi_name == self.source_name:
                        return s, finder
            return None, finder
        except Exception:
            return None, finder

    def run(self):
        self.status.emit(f"Connecting to {self.source_name}…")
        source, finder = self._find_source()
        if source is None:
            ndi.find_destroy(finder)
            self.status.emit(f"Source not found: {self.source_name}")
            return

        desc = ndi.RecvCreateV3()
        desc.color_format = ndi.RECV_COLOR_FORMAT_BGRX_BGRA
        desc.bandwidth = ndi.RECV_BANDWIDTH_HIGHEST
        recv = ndi.recv_create_v3(desc)
        if recv is None:
            ndi.find_destroy(finder)
            self.status.emit("Could not create NDI receiver")
            return

        ndi.recv_connect(recv, source)
        ndi.find_destroy(finder)  # source info has been copied into the receiver
        self.status.emit("")

        try:
            while self._running:
                t, v, a, m = ndi.recv_capture_v2(recv, 500)
                if t == ndi.FRAME_TYPE_VIDEO:
                    h, w = v.yres, v.xres
                    stride = v.line_stride_in_bytes
                    data = np.copy(v.data)
                    ndi.recv_free_video_v2(recv, v)
                    # BGRX bytes == QImage.Format_RGB32 on little-endian machines
                    img = QImage(data.data, w, h, stride, QImage.Format_RGB32).copy()
                    self.frame_ready.emit(img)
                elif t == ndi.FRAME_TYPE_AUDIO:
                    ndi.recv_free_audio_v2(recv, a)
                elif t == ndi.FRAME_TYPE_METADATA:
                    ndi.recv_free_metadata(recv, m)
        finally:
            ndi.recv_destroy(recv)

    def stop(self):
        self._running = False
        self.wait(2000)


# --------------------------------------------------------------------------- #
# Full-screen output window
# --------------------------------------------------------------------------- #
class OutputWindow(QWidget):
    closed = Signal()

    def __init__(self):
        super().__init__()
        self.setWindowTitle("NDI Output")
        self.setStyleSheet("background:black;")
        self.setCursor(Qt.BlankCursor)
        self._image = None
        self._message = ""

    def set_frame(self, img: QImage):
        self._image = img
        self._message = ""
        self.update()

    def set_message(self, text: str):
        self._message = text
        if text:
            self._image = None
        self.update()

    def show_on_screen(self, screen):
        self.hide()
        self.setScreen(screen)
        self.setGeometry(screen.geometry())
        self.showFullScreen()

    def paintEvent(self, _):
        p = QPainter(self)
        p.fillRect(self.rect(), Qt.black)
        if self._image is not None and not self._image.isNull():
            scaled = self._image.scaled(self.size(), Qt.KeepAspectRatio,
                                        Qt.SmoothTransformation)
            x = (self.width() - scaled.width()) // 2
            y = (self.height() - scaled.height()) // 2
            p.drawImage(x, y, scaled)
        elif self._message:
            p.setPen(Qt.gray)
            p.setFont(QFont("Arial", 24))
            p.drawText(self.rect(), Qt.AlignCenter, self._message)

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape:
            self.close()

    def closeEvent(self, e):
        self.closed.emit()
        super().closeEvent(e)


# --------------------------------------------------------------------------- #
# Source card
# --------------------------------------------------------------------------- #
class SourceCard(QFrame):
    live_clicked = Signal(str)

    def __init__(self, ndi_name: str):
        super().__init__()
        self.ndi_name = ndi_name
        self.setStyleSheet("QFrame { background:#1c1f24; border:1px solid #333;"
                           " border-radius:10px; }")
        self.setFixedSize(240, 150)

        # NDI names look like "MACHINE (Source Name)"
        if "(" in ndi_name and ndi_name.endswith(")"):
            machine, src = ndi_name.split("(", 1)
            machine, src = machine.strip(), src[:-1].strip()
        else:
            machine, src = ndi_name, ""

        m = QLabel(machine)
        m.setStyleSheet("color:white; font-size:16px; font-weight:bold; border:none;")
        m.setAlignment(Qt.AlignCenter)
        m.setWordWrap(True)
        s = QLabel(src)
        s.setStyleSheet("color:#9aa3ad; font-size:12px; border:none;")
        s.setAlignment(Qt.AlignCenter)
        s.setWordWrap(True)

        self.button = QPushButton("LIVE")
        self.button.setCursor(Qt.PointingHandCursor)
        self.button.clicked.connect(lambda: self.live_clicked.emit(self.ndi_name))
        self.set_live(False)

        lay = QVBoxLayout(self)
        lay.addWidget(m)
        lay.addWidget(s)
        lay.addStretch()
        lay.addWidget(self.button)

    def set_live(self, live: bool):
        self.button.setStyleSheet(BTN_LIVE if live else BTN_IDLE)
        self.button.setText("● LIVE" if live else "LIVE")


# --------------------------------------------------------------------------- #
# Main window
# --------------------------------------------------------------------------- #
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("NDI Live Switcher")
        self.resize(1100, 700)
        self.setStyleSheet("QMainWindow, QWidget#root { background:#121417; }"
                           " QLabel { color:#e6e6e6; }")

        self.cards: dict[str, SourceCard] = {}
        self.live_source: str | None = None
        self.receiver: ReceiverThread | None = None
        self.output = OutputWindow()
        self.output.closed.connect(self._on_output_closed)

        root = QWidget(objectName="root")
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)

        # Top bar
        bar = QHBoxLayout()
        title = QLabel("NDI Sources")
        title.setStyleSheet("font-size:22px; font-weight:bold;")
        self.count_label = QLabel("Searching…")
        self.count_label.setStyleSheet("color:#9aa3ad;")
        self.screen_combo = QComboBox()
        self.screen_combo.setMinimumWidth(260)
        self._populate_screens()
        QGuiApplication.instance().screenAdded.connect(lambda _: self._populate_screens())
        QGuiApplication.instance().screenRemoved.connect(lambda _: self._populate_screens())

        bar.addWidget(title)
        bar.addSpacing(12)
        bar.addWidget(self.count_label)
        bar.addStretch()
        bar.addWidget(QLabel("Output display:"))
        bar.addWidget(self.screen_combo)
        outer.addLayout(bar)

        # Grid
        self.grid_host = QWidget()
        self.grid_host.setStyleSheet("background:#121417;")
        self.grid = QGridLayout(self.grid_host)
        self.grid.setSpacing(16)
        self.grid.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.grid_host)
        scroll.setStyleSheet("QScrollArea { border:none; }")
        outer.addWidget(scroll)

        # Discovery
        self.discovery = DiscoveryThread()
        self.discovery.sources_changed.connect(self._update_sources)
        self.discovery.start()

    # ---- screens ----
    def _populate_screens(self):
        current = self.screen_combo.currentData()
        self.screen_combo.clear()
        screens = QGuiApplication.screens()
        primary = QGuiApplication.primaryScreen()
        for i, sc in enumerate(screens):
            g = sc.geometry()
            tag = " (main)" if sc is primary else ""
            self.screen_combo.addItem(f"{i + 1}: {sc.name()} {g.width()}x{g.height()}{tag}", i)
        # default to first non-primary screen (the external display)
        idx = next((i for i, sc in enumerate(screens) if sc is not primary), 0)
        if current is not None and current < len(screens):
            idx = current
        self.screen_combo.setCurrentIndex(idx)

    def _selected_screen(self):
        screens = QGuiApplication.screens()
        i = self.screen_combo.currentData() or 0
        return screens[i] if i < len(screens) else QGuiApplication.primaryScreen()

    # ---- sources ----
    def _update_sources(self, names: list):
        for n in list(self.cards):
            if n not in names and n != self.live_source:
                self.cards.pop(n).deleteLater()
        for n in names:
            if n not in self.cards:
                card = SourceCard(n)
                card.live_clicked.connect(self._on_live_clicked)
                self.cards[n] = card
        # re-lay out in sorted order
        for i, n in enumerate(sorted(self.cards)):
            self.grid.addWidget(self.cards[n], i // COLUMNS, i % COLUMNS)
        count = len(names)
        self.count_label.setText(f"{count} source{'s' if count != 1 else ''} found"
                                 if count else "No NDI sources found yet…")

    # ---- live switching ----
    def _on_live_clicked(self, name: str):
        if name == self.live_source:
            self._take_off_air()
            self.output.close()
            return
        self._go_live(name)

    def _go_live(self, name: str):
        self._stop_receiver()
        if self.live_source in self.cards:
            self.cards[self.live_source].set_live(False)
        self.live_source = name
        self.cards[name].set_live(True)

        self.output.set_message("Connecting…")
        if not self.output.isVisible():
            self.output.show_on_screen(self._selected_screen())

        self.receiver = ReceiverThread(name)
        self.receiver.frame_ready.connect(self.output.set_frame)
        self.receiver.status.connect(lambda t: t and self.output.set_message(t))
        self.receiver.start()

    def _take_off_air(self):
        self._stop_receiver()
        if self.live_source in self.cards:
            self.cards[self.live_source].set_live(False)
        self.live_source = None

    def _stop_receiver(self):
        if self.receiver is not None:
            self.receiver.frame_ready.disconnect()
            self.receiver.stop()
            self.receiver = None

    def _on_output_closed(self):
        self._take_off_air()

    def closeEvent(self, e):
        self._take_off_air()
        self.output.close()
        self.discovery.stop()
        super().closeEvent(e)


def main():
    if not ndi.initialize():
        print("NDI could not be initialised. Is the NDI runtime installed?")
        sys.exit(1)
    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()
    code = app.exec()
    ndi.destroy()
    sys.exit(code)


if __name__ == "__main__":
    main()
