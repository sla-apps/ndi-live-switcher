"""End-to-end smoke test run on the Windows build machine.

Sends two real NDI test feeds, starts the app off-screen and checks that
it discovers them, shows thumbnails, goes live, switches smoothly, shows a
holding image and opens the framing editor.
"""
import os
import sys
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import NDIlib as ndi

assert ndi.initialize(), "NDI initialise failed"

for name in ["FindCreate", "find_create_v2", "find_get_current_sources",
             "find_wait_for_sources", "find_destroy", "RecvCreateV3", "recv_create_v3",
             "recv_connect", "recv_capture_v2", "recv_free_video_v2", "recv_free_audio_v2",
             "recv_free_metadata", "recv_destroy", "RECV_BANDWIDTH_LOWEST",
             "RECV_BANDWIDTH_HIGHEST", "RECV_COLOR_FORMAT_BGRX_BGRA", "FRAME_TYPE_VIDEO",
             "FRAME_TYPE_AUDIO", "FRAME_TYPE_METADATA"]:
    assert hasattr(ndi, name), f"NDIlib is missing {name}"
fc = ndi.FindCreate()
fc.show_local_sources = True
fc.extra_ips = "127.0.0.1"
fc.groups = "public"
print("FindCreate settings OK")

stop = threading.Event()


def sender(label, colour):
    sc = ndi.SendCreate()
    sc.ndi_name = label
    s = ndi.send_create(sc)
    frame = ndi.VideoFrameV2()
    img = np.zeros((720, 1280, 4), dtype=np.uint8)
    img[:, :, :3] = colour
    img[:, :, 3] = 255
    frame.data = img
    frame.FourCC = ndi.FOURCC_VIDEO_TYPE_BGRX
    while not stop.is_set():
        ndi.send_send_video_v2(s, frame)
        time.sleep(1 / 30)
    ndi.send_destroy(s)


for label, colour in (("CI Feed A", (0, 0, 255)), ("CI Feed B", (0, 255, 0))):
    threading.Thread(target=sender, args=(label, colour), daemon=True).start()

from PySide6.QtCore import QTimer
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication

import ndi_live

app = QApplication(sys.argv)
win = ndi_live.MainWindow()
win.show()
results = {}
names = {}


def step1():
    print("cards:", list(win.cards))
    for key in ("A", "B"):
        names[key] = next((n for n in win.cards if f"CI Feed {key}" in n), None)
    results["discovered both feeds"] = all(names.values())
    if not results["discovered both feeds"]:
        return finish()
    results["thumbnail received"] = ndi_live.valid(win.cards[names["A"]].thumb._image)
    win._go_live(("ndi", names["A"]))
    QTimer.singleShot(3000, step2)


def step2():
    results["live A on output"] = ndi_live.valid(win.output._image)
    results["live button red"] = "LIVE" in win.cards[names["A"]].button.text() and \
        win.cards[names["A"]].button.text().startswith("●")
    before = win.output._image
    win._go_live(("ndi", names["B"]))
    # Immediately after switching, the old picture must still be on screen (no black gap)
    results["no black gap while switching"] = win.output._image is before
    QTimer.singleShot(3000, step3)


def step3():
    img = win.output._image
    px = QColor(img.pixel(img.width() // 2, img.height() // 2)) if ndi_live.valid(img) else None
    results["switched to B"] = px is not None and px.green() > 200 and px.red() < 50
    hold = QImage(640, 360, QImage.Format_RGB32)
    hold.fill(QColor("orange"))
    path = os.path.abspath("ci_hold.png")
    hold.save(path)
    win._add_holding_image_path(path)
    win._go_holding()
    QTimer.singleShot(1500, step4)


def step4():
    results["holding image on output"] = win.live is not None and win.live[0] == "img" \
        and ndi_live.valid(win.output._image)
    win._open_editor(names["A"])
    QTimer.singleShot(2500, step5)


def step5():
    dlg = win.editors.get(names["A"])
    results["editor opened with picture"] = dlg is not None and ndi_live.valid(dlg.view._image)
    if dlg:
        dlg._set_all({"zoom": 200, "x": 10, "y": -10})
        results["framing saved"] = win.framing.get(names["A"]) == {"zoom": 200, "x": 10, "y": -10}
        dlg.close()
    win.thumb_action.setChecked(False)
    results["thumbnails switch off"] = all(c.preview is None for c in win.cards.values())
    win._off_air_and_close()
    results["off air"] = win.live is None
    finish()


def finish():
    ok = bool(results) and all(results.values())
    print("\n=== SMOKE TEST RESULTS ===")
    for k, v in results.items():
        print(f"  {'PASS' if v else 'FAIL'}  {k}")
    print("OVERALL:", "PASS" if ok else "FAIL")
    stop.set()
    win.close()
    app.exit(0 if ok else 1)


QTimer.singleShot(8000, step1)
QTimer.singleShot(60000, finish)  # safety net
code = app.exec()
ndi.destroy()
sys.exit(code)
