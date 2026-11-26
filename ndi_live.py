"""
NDI Live Switcher
-----------------
Discovers every NDI source on the local network and shows them as a grid of
cards: a live thumbnail, the machine / source name and a LIVE button.

Click LIVE  -> button turns red and that feed plays full screen on the
               selected output display (an external monitor by default).
Click again -> takes it off air and closes the output window.
Only one source is live at a time; clicking another switches to it.
Press Esc on the output window to close it.

Hide        -> removes an unwanted input from the grid (and stops its
               thumbnail, saving network bandwidth). Hidden inputs are
               remembered between sessions and can be restored from the
               "Hidden" menu in the top bar.

Requires: NDI runtime/tools installed, plus `pip install -r requirements.txt`.
"""

import sys
import time

import numpy as np
import NDIlib as ndi
from PySide6.QtCore import QSettings, QSize, Qt, QThread, Signal
from PySide6.QtGui import QFont, QGuiApplication, QImage, QPainter, QPixmap
from PySide6.QtWidgets import (
    QApplication, QComboBox, QFrame, QGridLayout, QHBoxLayout, QLabel,
    QMainWindow, QMenu, QPushButton, QScrollArea, QToolButton, QVBoxLayout,
    QWidget,
)

REFRESH_MS = 2000
THUMB_W, THUMB_H = 256, 144
THUMB_FPS = 5
CARD_W = THUMB_W + 20
GRID_SPACING = 16

BTN_IDLE = """
QPushButton { background:#2b2f36; color:#e6e6e6; border:1px solid #444;
              border-radius:6px; padding:8px; font-weight:bold; font-size:14px; }
QPushButton:hover { background:#3a3f48; }
"""
BTN_LIVE = """
QPushButton { background:#d11a2a; color:white; border:1px solid #ff4d5a;
              border-radius:6px; padding:8px; font-weight:bold; font-size:14px; }
"""
BTN_HIDE = """
QPushButton { background:transparent; color:#8a929c; border:1px solid #3a3f48;
              border-radius:6px; padding:8px 10px; font-size:12px; }
QPushButton:hover { color:white; border-color:#666; }
"""


def split_name(ndi_name: str):
    """NDI names look like 'MACHINE (Source Name)'."""
    if "(" in ndi_name and ndi_name.endswith(")"):
        machine, src = ndi_name.split("(", 1)
        return machine.strip(), src[:-1].strip()
    return ndi_name, ""


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
# NDI receiving (used for both full-quality output and low-res thumbnails)
# --------------------------------------------------------------------------- #
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
        while self._running:  # keep looking until found or stopped
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
                    # BGRX bytes == QImage.Format_RGB32 on little-endian machines
                    img = QImage(data.data, w, h, stride, QImage.Format_RGB32)
                    if self.preview:
                        img = img.scaled(THUMB_W, THUMB_H, Qt.KeepAspectRatio,
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
            p.drawImage((self.width() - scaled.width()) // 2,
                        (self.height() - scaled.height()) // 2, scaled)
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
# Source card: thumbnail + name + LIVE + Hide
# --------------------------------------------------------------------------- #
class SourceCard(QFrame):
    live_clicked = Signal(str)
    hide_clicked = Signal(str)

    def __init__(self, ndi_name: str):
        super().__init__()
        self.ndi_name = ndi_name
        self.setObjectName("card")
        self.setFixedWidth(CARD_W)
        self._set_border(False)

        self.thumb = QLabel("Connecting…")
        self.thumb.setFixedSize(QSize(THUMB_W, THUMB_H))
        self.thumb.setAlignment(Qt.AlignCenter)
        self.thumb.setStyleSheet("background:black; color:#666; border:none;"
                                 " border-radius:6px; font-size:12px;")

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

        hide = QPushButton("Hide")
        hide.setToolTip("Hide this input (restore it from the Hidden menu)")
        hide.setCursor(Qt.PointingHandCursor)
        hide.setStyleSheet(BTN_HIDE)
        hide.clicked.connect(lambda: self.hide_clicked.emit(self.ndi_name))

        buttons = QHBoxLayout()
        buttons.addWidget(self.button, 1)
        buttons.addWidget(hide)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.setSpacing(6)
        lay.addWidget(self.thumb)
        lay.addWidget(m)
        lay.addWidget(s)
        lay.addLayout(buttons)
        self.set_live(False)

        # low-bandwidth thumbnail feed
        self.preview = ReceiverThread(ndi_name, preview=True)
        self.preview.frame_ready.connect(self._on_thumb)
        self.preview.status.connect(self._on_status)
        self.preview.start()

    def _set_border(self, live: bool):
        colour = "#d11a2a" if live else "#333"
        self.setStyleSheet(f"QFrame#card {{ background:#1c1f24; border:2px solid {colour};"
                           " border-radius:10px; }")

    def _on_thumb(self, img: QImage):
        self.thumb.setPixmap(QPixmap.fromImage(img))

    def _on_status(self, text: str):
        if text:
            self.thumb.clear()
            self.thumb.setText(text)

    def set_live(self, live: bool):
        self.button.setStyleSheet(BTN_LIVE if live else BTN_IDLE)
        self.button.setText("● LIVE" if live else "LIVE")
        self._set_border(live)

    def shutdown(self, graveyard: set):
        """Stop the thumbnail thread without blocking the UI."""
        t = self.preview
        try:
            t.frame_ready.disconnect()
            t.status.disconnect()
        except (RuntimeError, TypeError):
            pass
        graveyard.add(t)
        t.finished.connect(lambda: graveyard.discard(t))
        t.stop(wait=False)


# --------------------------------------------------------------------------- #
# Main window
# --------------------------------------------------------------------------- #
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("NDI Live Switcher")
        self.resize(1200, 760)
        self.setStyleSheet("QMainWindow, QWidget#root { background:#121417; }"
                           " QLabel { color:#e6e6e6; }"
                           " QToolButton { color:#e6e6e6; background:#2b2f36;"
                           "   border:1px solid #444; border-radius:6px; padding:5px 10px; }"
                           " QMenu { background:#1c1f24; color:#e6e6e6; }"
                           " QMenu::item:selected { background:#3a3f48; }")

        self.settings = QSettings("NDI Live Switcher", "NDI Live Switcher")
        self.hidden: set[str] = set(self.settings.value("hidden", [], type=list) or [])
        self.online: list[str] = []
        self.cards: dict[str, SourceCard] = {}
        self._graveyard: set = set()  # threads that are shutting down
        self.live_source: str | None = None
        self.receiver: ReceiverThread | None = None
        self.output = OutputWindow()
        self.output.closed.connect(self._take_off_air)

        root = QWidget(objectName="root")
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)

        # ---- top bar ----
        bar = QHBoxLayout()
        title = QLabel("NDI Sources")
        title.setStyleSheet("font-size:22px; font-weight:bold;")
        self.count_label = QLabel("Searching…")
        self.count_label.setStyleSheet("color:#9aa3ad;")

        self.hidden_btn = QToolButton()
        self.hidden_btn.setPopupMode(QToolButton.InstantPopup)
        self.hidden_menu = QMenu(self)
        self.hidden_menu.aboutToShow.connect(self._build_hidden_menu)
        self.hidden_btn.setMenu(self.hidden_menu)

        self.screen_combo = QComboBox()
        self.screen_combo.setMinimumWidth(260)
        self._populate_screens()
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

        # ---- grid ----
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
        idx = next((i for i, sc in enumerate(screens) if sc is not primary), 0)
        if current is not None and current < len(screens):
            idx = current
        self.screen_combo.setCurrentIndex(idx)

    def _selected_screen(self):
        screens = QGuiApplication.screens()
        i = self.screen_combo.currentData() or 0
        return screens[i] if i < len(screens) else QGuiApplication.primaryScreen()

    # ---- sources / grid ----
    def _on_sources(self, names: list):
        self.online = names
        self._sync_cards()

    def _sync_cards(self):
        wanted = {n for n in self.online if n not in self.hidden}
        if self.live_source:
            wanted.add(self.live_source)  # never drop the live card
        for n in list(self.cards):
            if n not in wanted:
                card = self.cards.pop(n)
                card.shutdown(self._graveyard)
                self.grid.removeWidget(card)
                card.deleteLater()
        for n in wanted:
            if n not in self.cards:
                card = SourceCard(n)
                card.live_clicked.connect(self._on_live_clicked)
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

    # ---- hiding ----
    def _hide_source(self, name: str):
        if name == self.live_source:
            self._take_off_air()
            self.output.close()
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
        for name in sorted(self.hidden):
            label = name if name in self.online else f"{name}   (offline)"
            act = self.hidden_menu.addAction(f"Show  {label}")
            act.triggered.connect(lambda _=False, n=name: self._unhide(n))
        if self.hidden:
            self.hidden_menu.addSeparator()
            self.hidden_menu.addAction("Show all").triggered.connect(self._unhide_all)

    # ---- live switching ----
    def _on_live_clicked(self, name: str):
        if name == self.live_source:
            self._take_off_air()
            self.output.close()
        else:
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
        was = self.live_source
        self.live_source = None
        if was in self.cards:
            self.cards[was].set_live(False)
        if was and (was in self.hidden or was not in self.online):
            self._sync_cards()

    def _stop_receiver(self):
        if self.receiver is not None:
            try:
                self.receiver.frame_ready.disconnect()
                self.receiver.status.disconnect()
            except (RuntimeError, TypeError):
                pass
            self.receiver.stop()
            self.receiver = None

    def closeEvent(self, e):
        self._stop_receiver()
        self.output.close()
        for card in self.cards.values():
            card.shutdown(self._graveyard)
        self.discovery.stop()
        for t in list(self._graveyard):
            t.wait(2500)
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
