# Osmo Client

Control a **DJI Osmo Nano** from your computer over Wi-Fi: live view, capture modes, photo and video,
exposure and format settings, camera status, and a gallery to preview and download your files. Runs on
**Windows, macOS and Linux**.

> Unofficial and independent: not affiliated with, endorsed or sponsored by DJI. See [Legal](#legal).

The app runs locally and opens its page in your browser at <http://127.0.0.1:8091>.

> **New in 1.1: macOS and Linux.** macOS is tested end to end with the Osmo Nano (linking, live view,
> settings, gallery and downloads); Linux is still being tested on real hardware. Reports are welcome.

![Osmo Client: live view, shutter, capture modes and camera settings](screenshots/main-app.png)

## Features

- **Live view** of the camera.
- **Capture modes**: Photo, Video, Super Night, Slow-mo, Timelapse, Hyperlapse (keys `1`–`6`).
- **Shutter**: takes a photo, starts/stops recording or a timelapse, depending on the mode (`Space`).
- **Exposure**: Auto / Manual, ISO 100–51200, shutter speed 1/8000 s–30 s, white balance (Auto or
  2000–10000 K). Each capture mode keeps its own exposure, as on the camera.
- **Format**: video resolution (4K, 2.7K, 1080p in 16:9 or 4:3) and frame rate (24–60 fps), slow motion
  (120 fps 4×, 240 fps 8×), photo size and ratio (L 36/31 MP, M 9/8 MP). Only what the current mode
  supports is offered.
- **Status**: recording state and time, storage free and remaining (video time / photos), estimated
  quality and bitrate, battery (%, voltage, temperature, charging), ISO/shutter in use when the camera
  publishes them.
- **Gallery**: browse the camera's files by day, filter videos/photos, preview (low-resolution video
  proxy or the photo), and **download to the computer over Wi-Fi** (queue, progress, resumes if
  interrupted) into `DJI Osmo/<camera>` in your Videos folder (Movies on macOS). Nothing is ever
  deleted from the camera.
- **Several cameras**: link more than one; one is active at a time. Shows whether the camera is
  asleep or on while disconnected.
- **Disconnect** button: releases the camera (so the phone app or the dock can use it) and puts your
  computer back on its own Wi-Fi without closing the app.

Other DJI Osmo cameras (Action 4/5 Pro/6, Pocket 3/4) use the same protocol and may work, fully or in
part; the commands were mapped on the Osmo Nano and only the Nano has been tested. The Osmo 360 is not
supported.

Need the camera as a webcam for video calls? See the companion app, **OsmoCam**.

## Screenshots

**Gallery**: the camera's files by day, ready to download to the computer.

![Gallery](screenshots/gallery.png)

**Preview** of a video (low-resolution proxy) before downloading it.

![Video preview](screenshots/video-playback.png)

**Settings and status**: exposure, format, storage, battery and the technical details panel.

<p>
  <img src="screenshots/camera-settings.png" alt="Camera, exposure and format settings" width="400">
  <img src="screenshots/live-camera-info.png" alt="Storage, battery and technical details" width="400">
</p>

**Cameras**: link one or more cameras and switch between them.

![Cameras panel](screenshots/add-cameras.png)

## Requirements

- A computer with **Bluetooth LE** and **Wi-Fi**:
  - **Windows** 10 or 11.
  - **macOS** 12 or newer.
  - **Linux** with **NetworkManager** (Ubuntu, Fedora, Mint, Debian desktop and most others) and BlueZ.
- [Python](https://www.python.org/downloads/) 3.10 to 3.14 (3.14 recommended). Python 3.15 is too new
  for now: some of the libraries used here have no build for it yet. With several versions installed,
  the newest from 3.10 to 3.14 is used.
  - macOS: the `python3` that comes with macOS is too old; install Python 3.14 from python.org (or with
    Homebrew: `brew install python@3.14`).
  - Debian / Ubuntu: also `sudo apt install python3-venv`.
- **Internet through another adapter** (Ethernet, USB tethering or a second Wi-Fi adapter) if you need
  it while connected: the Wi-Fi adapter joins the camera's network.

## Run it

1. Download or clone this repository.
2. Turn the camera on and close the DJI Mimo app on your phone (the camera accepts one client at a time).
3. Start the app. The first run creates a private Python environment and installs the requirements
   (needs internet once). The page opens in your browser.
   - **Windows**: double-click **`osmo-client.bat`**.
   - **macOS / Linux**: open a terminal in this folder and run **`./osmo-client.sh`** (or
     `sh osmo-client.sh`). On macOS, allow **Bluetooth** for Terminal when asked.
4. Pick your camera under **Nearby** and click **Link**. The camera shows **"OSMO"**: approve it on the
   camera screen within ~20 seconds. Next time it connects to the camera used last by itself.

Stop with **Ctrl+C** in the console window: the computer's Wi-Fi goes back to the network it was on.

### Options

The same on every system (`osmo-client.bat` on Windows, `./osmo-client.sh` on macOS and Linux):

```
--off                     start disconnected (connect from the page)
--downloads FOLDER        download folder (default: DJI Osmo in Videos, Movies on macOS)
--interface NAME          Wi-Fi adapter to use: "Wi-Fi 2" on Windows, en0 on macOS, wlan0 on Linux
                          (default: the first one)
--home NETWORK            Wi-Fi network to go back to on Windows and Linux (default: the one the computer is on)
--port 8091               port of the local page
--no-browser              don't open the page automatically
--help                    everything else
```

## Good to know

- While the **Gallery** is open, the camera pauses its live view (like the phone app's album): it then
  sends files about 6× faster (~45 MB/s instead of ~8 MB/s) and stays cooler.
- Changing the format restarts the camera's encoder: the live view stops for a few seconds. Many changes
  in a row heat the camera; around 67 °C it stops charging and may drop Wi-Fi until it cools down.
- ISO and shutter can only be set in **Manual** exposure.
- There is no known command to put the Nano to sleep: after **Disconnect** it goes to sleep by itself
  after a while. **Connect** wakes it up.
- **macOS**: when you disconnect, the app makes the Mac forget the camera's network and switches Wi-Fi
  off and on; the Mac then rejoins its usual network by itself, in a few seconds.
- **Linux**: the camera's network is saved in NetworkManager as a connection named after the camera,
  set to never connect by itself or take over your internet route.

## How it works

1. **Bluetooth LE**: finds the camera, pairs (the "OSMO" prompt), wakes it and asks for the name and
   password of its Wi-Fi access point.
2. **Wi-Fi**: the computer joins that access point (the camera is `192.168.2.1`).
3. **Datalink**: a small UDP protocol carries commands, status and the H.264 live view. Files download
   over HTTP from the camera.

The protocol is documented in [docs/PROTOCOL.md](docs/PROTOCOL.md).

Code layout:

| path | what |
|---|---|
| `osmo_client.py` | entry point: connection loop (Bluetooth → Wi-Fi → datalink) |
| `client/app.py` | commands, settings, gallery and camera selection |
| `client/state.py` | camera status parsed for the page |
| `client/downloads.py` | download queue |
| `client/server.py` | local web server and JSON API (127.0.0.1 only) |
| `client/web/index.html` | the page |
| `camlink/` | reusable protocol package: BLE, Wi-Fi (one backend per system), datalink, commands, media |

## Troubleshooting

- **The camera is not listed**: turn it on, bring it closer, make sure the phone app is not connected to
  it, then **Search again**.
- **Stuck at "Approve OSMO"**: look at the camera screen and accept the prompt (it lasts ~20 s); then
  click **Link** again.
- **No internet while connected**: expected on the Wi-Fi adapter; use Ethernet or a second adapter, or
  click **Disconnect** when you are done.
- **"Not available" for the microSD**: the dock's microSD is only listed when the camera is docked with
  a card inside.
- **macOS, "Bluetooth is not available" or nothing found**: allow Bluetooth for Terminal (or the app you
  started it from) in System Settings > Privacy & Security > Bluetooth, then start it again.
- **macOS 15 or newer, "No route to host"**: allow Terminal under System Settings > Privacy & Security >
  Local Network.
- **Linux, "NetworkManager (nmcli) was not found"**: join the camera's Wi-Fi yourself; its name and
  password are in `cameras.json` (see [Privacy](#privacy)). The app carries on once you are connected.
- **Linux, "Insufficient privileges"** when joining the camera: your user may not change network
  settings; ask your administrator, or join the camera's network yourself.

## Privacy

Everything stays on your computer. The app talks only to the camera and serves its page on
`127.0.0.1` (not reachable from the network). The cameras you link (Bluetooth address, name, and the
camera's Wi-Fi password) are stored in `cameras.json`, shared with OsmoCam:

- Windows: `%LOCALAPPDATA%\osmo-pc\cameras.json`
- macOS: `~/Library/Application Support/osmo-pc/cameras.json`
- Linux: `~/.local/share/osmo-pc/cameras.json`

The same folder holds the app's private Python environment. Downloads go to the folder you choose. No
telemetry.

## Legal

Osmo Client is an independent project, not affiliated with, endorsed or sponsored by DJI.
DJI, Osmo, Osmo Nano and DJI Mimo are trademarks of SZ DJI Technology Co., Ltd., used here only to name
the cameras this software works with. The repository contains no DJI code, firmware or assets. Its
purpose is interoperability: letting owners use their own camera with their computer.

It is provided "as is", without warranty of any kind (see [LICENSE](LICENSE)). It may stop working after
a camera firmware update. Use it at your own risk.

Third-party notices and credits: [NOTICE.md](NOTICE.md).

## License

[MIT](LICENSE)
