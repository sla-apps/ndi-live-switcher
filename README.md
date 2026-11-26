# NDI Live Switcher

Shows every NDI source on your network as a grid. Each card has a **LIVE** button.
When you click it, the button turns red and that feed plays full screen on your external display.

## Setup
1. Install the free **NDI Tools / NDI Runtime** from https://ndi.video/tools/
2. Install Python 3.9–3.12, then:
   ```
   pip install -r requirements.txt
   ```
3. Run:
   ```
   python ndi_live.py
   ```

## Use
- Sources show up on their own within a couple of seconds, and the grid refreshes as devices join or leave.
- Each card shows a live thumbnail (low-bandwidth, about 5 fps), the machine name and the NDI source name.
- **Hide** removes an input you don't want from the grid and stops its thumbnail. Hidden inputs stay hidden the next time you open the app. You can bring them back from the **Hidden (n) ▾** menu in the top bar, one at a time or with **Show all**.
- **LIVE** turns the button red and opens that feed full screen on the display picked under *Output display*. The first external monitor is picked by default.
- If you click a different source's LIVE, the output switches to it. Only one source is live at a time.
- If you click the red button again, or press **Esc** on the output, the feed goes off air.

## Switching
When you click LIVE on another input, the current picture stays on screen until the new input is ready, so there's no black gap. It then cuts or fades to the new input. Choose **Output ▸ Transition**: Cut, or a fade of 0.25 s, 0.5 s (the default) or 1 s.

## Holding images
Use **＋ Holding Image** (or File ▸ Add Holding Image, Ctrl+I) to add one or more images, such as a logo or a "we'll be right back" slide. Each image gets its own card with a LIVE button. **Ctrl+H** jumps straight to the first holding image.

## Finding sources
**⟳ Refresh** (F5) searches the network again. **View ▸ NDI Network Settings** lets you add the IP addresses of devices on other subnets or VLANs, and NDI group names. If your network uses an NDI Discovery Server, set it up in NDI Access Manager; the app follows that setting too.

## Menus
- **File:** Open Settings… (Ctrl+O), Save Settings… (Ctrl+S), Reset All Framing, Quit (Ctrl+Q)
- **View:** Show Thumbnails on/off (Ctrl+T), Hidden Inputs
- **Output:** Edit Framing of Live Input (Ctrl+E), Take Off Air (Ctrl+B)
- **Help:** About

## Framing editor
Click **Edit** on any card to open it. You can:
- Zoom from 100% to 500% with the slider, the − and + buttons, the mouse wheel, or the +/- keys.
- Move the picture left, right, up or down with the sliders, the arrow buttons or the arrow keys, or by dragging it.
- Click **Reset** to go back to the full picture.

Changes show on the output straight away if that input is live. Each input keeps its own framing, and the app saves it automatically.

**Save Settings…** writes everything to a `.json` file: framing for every input, hidden inputs, the thumbnails setting and the output display. **Open Settings…** loads one back, so you can keep a settings file per show or venue.

## Troubleshooting
- **No sources:** check that your computer and the NDI devices are on the same subnet, and that your firewall allows NDI (TCP/UDP 5353 for mDNS and 5960+). You can also run NDI Studio Monitor to check that the sources are visible at all.
- **`import NDIlib` fails:** `ndi-python` ships wheels for Windows, macOS and Linux on common Python versions. If none matches your Python version, try Python 3.10 or 3.11.
