# better-dash

An alternative navigation app for your Royal Enfield Tripper TFT screen.

The script in this repo, **`tripper_app_like_nav.py`**, is a Python emulator
of the official Royal Enfield phone app. It runs on a Mac (Linux works too)
joined to the dash's Wi-Fi access point and reproduces the K1G/UDP control
plane plus the H.264/RTP video stream — enough to take the dash from
`Connected` → `Navigation` and play arbitrary video on its TFT.

## Roadmap

### Phase 1 — Testing & refinement
- [x] Improve stream framerate beyond 4 fps
- [x] Understand bike button controls & auxiliary messages (UDP/2002)
- [ ] Pi software — auto-connect to Tripper WiFi & launch UI headlessly

### Phase 2 — Raspberry Pi as a standalone
- [ ] Port Python script to Raspberry Pi Zero
- [ ] Bluetooth GPX ingestion from phone before ride
- [x] Custom navigation UI (turn-by-turn, 526×300) — **Qt UI now in `dash_ui/`**
- [ ] Plug-and-play install (pre-flashed SD image)

### Side branch — Android Auto
> Parallel effort — stream Android Auto interface to the Tripper dash.
> Separate branch, won't affect the main roadmap.
- [ ] Investigate AA protocol compatibility with Tripper WiFi stack
- [ ] Community contributors welcome

## What works today

- Authenticates with the dash (RSA-encrypted session-key handshake).
- Drives the dash into Navigation mode (route card, `q3c.z2`, projection
  on/off TLVs, route-card keep-alive, nav-info instruction bubble).
- Streams an H.264 video file (any container ffmpeg can read) over RTP
  to UDP/5000. The dash decodes it on its embedded H.264 decoder.
- Streams a **live custom UI** rendered in Python (the `dash_ui` package),
  and reacts to the bike's joystick / click events live (LEFT, RIGHT,
  DOWN, CLICK).
- **Qt-based UI** (`qt_renderer.py`) with smooth gradients, real font
  hinting, and an interactive map view that follows a GPX track with
  pre-cached OSM tiles.


## What's in this repo

| File / dir | Role |
|---|---|
| `tripper_app_like_nav.py` | Standalone script: auth + nav-mode handshake + 1 Hz tick + RTP video stream from any file. Best entry point if you just want to "send video to the dash". |
| `dash_ui/` | Python package: renderer, encoder, RTP packetizer, K1G control plane, GPX parser, tile downloader. |
| `dash_ui/prototype.py` | CLI prototype — pygame renderer, full end-to-end with the bike. |
| `dash_ui/local_test.py` | Local dev harness — pygame renderer, keyboard-driven, no bike needed. |
| `dash_ui/qt_renderer.py` | **Qt renderer** (PySide6): same `Renderer` protocol as pygame but with sub-pixel text, smooth gradients, and a live map view. |
| `dash_ui/qt_prototype.py` | CLI prototype — Qt renderer, full end-to-end with the bike. |
| `dash_ui/qt_local_test.py` | **Qt local dev harness** — opens a Mac window, keyboard-driven, no bike needed. |
| `dash_ui/gpx.py` | Zero-dependency GPX 1.0/1.1 parser + arc-length walker. |
| `dash_ui/tiles.py` | XYZ slippy-map tile math, on-disk cache layout, and polite OSM downloader. |
| `dash_ui/download_tiles.py` | CLI tool — bulk pre-fetch tiles along a GPX corridor before a ride. |
| `gpx_files/` | Sample GPX tracks. Includes `Section of Leh-Manali Highway.gpx`. |
| `icons/` | PNG icons used by the Qt UI menu. |
| `requirements.txt` | Python dependencies. |


## Install

```bash
git clone <this repo>
cd better-dash

python3 -m venv .venv
source .venv/bin/activate

pip install --upgrade pip
pip install -r requirements.txt
brew install ffmpeg               # macOS — script invokes the `ffmpeg` binary
                                  # On Debian/Ubuntu: sudo apt install ffmpeg
```

`cryptography` is required for the auth handshake.  `pygame-ce` is only
required for the interactive `dash_ui` prototype — `tripper_app_like_nav.py`
on its own only needs `cryptography` + `ffmpeg`.  `PySide6` is required for
the Qt renderer (`qt_renderer.py`, `qt_prototype.py`, `qt_local_test.py`).

> **Note on pygame:** the upstream `pygame` package (2.6.1) has a known
> `pygame.font` circular-import bug on Python 3.13/3.14 that makes all
> text invisible.  We use `pygame-ce` (the community fork — 100% drop-in
> compatible) instead.  If you already have `pygame` installed, run
> `pip uninstall -y pygame && pip install pygame-ce`.

> **Note on PySide6:** the first run of `pip install PySide6` downloads
> ~100 MB of Qt6 wheels.  It only needs to happen once per virtual-env.
> On a Raspberry Pi, prefer the system package
> `sudo apt install python3-pyside6.qtgui python3-pyside6.qtwidgets`
> or build from source — the pip wheel targets `x86_64` / `aarch64`.


## Connect to the dash

1. Ensure your Tripper dash is in 'Digital' mode: (`up arrow` to activate settings menu) > `Appearance` > `Screen Type` > `Digital`
2. On the bike, enable the Tripper Wi-Fi (the menu shows the SSID and
   the AP comes up as something like `RE_xxxx_yymmdd`).
3. On the Mac, join that Wi-Fi network. The password is **`12345678`**.
4. Confirm you got an IP on the `192.168.1.x` subnet. The dash itself
   is **`192.168.1.1`**.

```bash
ifconfig | grep "inet 192.168.1"   # should show your DHCP lease
ping -c 2 192.168.1.1              # should reply
```

## Run it

```bash
# Replace SSID with whatever your dash advertises.
python tripper_app_like_nav.py \
    --ssid RE_xxxx_yymmdd \
    --hostname MacBook \
    --route-title "Hello Tripper" \
    --video /path/to/your/video.mp4 \
    --video-loop
```

Within ~2–3 seconds you should see:

1. Mac → dash burst (auth request + hostname announce).
2. Dash → Mac RSA pubkey (`07 00` modulus + `07 03` exponent), logged.
3. Mac → dash RSA-encrypted session key. Dash replies `07 01 01` (auth OK).
4. Mac sends `0x007E` route card + `q3c.z2` + projection-on TLVs.
5. ffmpeg starts encoding; the custom RTP packetizer pushes packets to
   UDP/5000 and your video appears on the dash's nav screen.

Stop with `Ctrl+C`. The script sends `q3c.h` + `q3c.x` (projection-off)
on the way out so the dash returns cleanly to the home screen.

## Useful flags

The script has a lot of knobs; the ones you'll touch most often:

| Flag | Default | Purpose |
|---|---|---|
| `--ssid` | _(required)_ | Dash Wi-Fi SSID. Embedded inside the encrypted session key — auth fails if it's wrong. |
| `--video` | – | Path to the file to stream. Without it, the dash stays on the loading-dots placeholder. |
| `--video-loop` | off | Loop the input forever instead of stopping at EOF. |
| `--route-title` | `"Navigation"` | Text shown on the dash's destination card. |
| `--static-image red` | – | Diagnostic: stream a solid colour (any ffmpeg colour name) instead of a video file. Useful for isolating "is the stream OK or is the content the problem?". |
| `--respond-2002` | off | Send app-like K1G acks back when the dash sends `09 xx` commands. Required for some firmwares. |
| `--no-auth` | off | Skip the RSA handshake — only useful for protocol experiments; the dash will refuse most commands. |
| `--auth-timeout` | 8 s | How long to wait for `07 01 01` before continuing. |
| `--bike-ip` | `192.168.1.1` | Override if your dash uses a non-default IP. |
| `--rtp-port` | 5000 | UDP port on the dash that receives H.264 RTP. |

Run `python tripper_app_like_nav.py --help` for the full list (battery
percentage, GPS-on/off, music/alarm volume placeholders, nav-info
maneuver code, distance unit, …).


## Qt UI (`dash_ui/qt_renderer`)

`qt_renderer.py` is a modern alternative to the pygame renderer.  It uses
PySide6/QPainter for hardware-accelerated off-screen rendering:

- Sub-pixel anti-aliased text via QFontDatabase (system fonts).
- Smooth QLinearGradient / QRadialGradient fills.
- QPainterPath for the map route polyline and rounded UI cards.
- Composited alpha — soft shadows, translucent overlays.
- Live map view: composites pre-cached XYZ tiles from disk, draws the GPX
  polyline, and advances a bike avatar along the route at a configurable speed.

Everything else (encoder, RTP packetizer, K1G handshake, button events) is
identical to the pygame path — both renderers implement the same `Renderer`
protocol from `dash_ui/renderer.py`.


### Step 1 — Pre-cache tiles along the GPX track

The dash has no internet connection while riding, so tiles must be downloaded
in advance to a local `tile_cache/` directory.  The sample route included in
this repo is a 90 km section of the **Leh–Manali Highway** (Himachal Pradesh,
India — bbox `33.41°N 77.73°E` → `33.66°N 77.88°E`).

**Download zoom levels 13 and 14** (recommended for riding — level 13 gives
a wide overview, level 14 gives street-level detail):

```bash
source .venv/bin/activate

python -m dash_ui.download_tiles \
    "gpx_files/Section of Leh-Manali Highway.gpx" \
    --zoom 13 --zoom 14 \
    --mode corridor \
    --buffer-km 1 \
    --cache-dir tile_cache
```

Expected output (the track has 1 028 points over ~90 km):

```
Mode: corridor  (buffer 1 km, zooms [13, 14])
  Section of Leh-Manali Highway.gpx: 1028 pts, 90.1 km, +NNN tiles
Plan: NNN tiles total — NNN to download, 0 already cached.
  Estimated: ~X MB, ~Y min at 80 ms/tile
```

A **dry run** (no network fetch) lets you see the tile count first:

```bash
python -m dash_ui.download_tiles \
    "gpx_files/Section of Leh-Manali Highway.gpx" \
    --zoom 13 --zoom 14 \
    --dry-run
```

If you want extra margin for detours (fuel stops, exploration), use
`--mode bbox` instead — it caches every tile inside the bounding box:

```bash
python -m dash_ui.download_tiles \
    "gpx_files/Section of Leh-Manali Highway.gpx" \
    --zoom 13 --zoom 14 \
    --mode bbox \
    --buffer-km 3 \
    --cache-dir tile_cache
```

**Zoom level reference:**

| Zoom | Coverage per tile | Typical use |
|------|------------------|-------------|
| 13   | ~20 km wide      | Route overview |
| 14   | ~10 km wide      | Main navigation zoom (recommended default) |
| 15   | ~5 km wide       | Street-level (larger download) |

> **OSM fair-use note:** The public `tile.openstreetmap.org` servers are
> donation-funded.  The downloader adds an 80 ms delay between fetches and
> sends an honest `User-Agent` header.  For larger areas or production use,
> point `--base-url` at a commercial tile provider (Mapbox, MapTiler) or a
> self-hosted tile server.


### Step 2 — Launch the Qt UI locally (no bike needed)

`qt_local_test.py` opens a desktop window so you can iterate on the UI
without the bike:

```bash
source .venv/bin/activate

python -m dash_ui.qt_local_test \
    --gpx-dir gpx_files \
    --tile-cache tile_cache \
    --nav-zoom 14 \
    --scale 2
```

This opens a 1052×600 window (526×300 dash resolution ×2).  The UI
navigates with the keyboard:

| Key | Bike button | Action |
|-----|-------------|--------|
| `←` / `a` | LEFT | Scroll left / previous item |
| `→` / `d` | RIGHT | Scroll right / next item |
| `↓` / `s` / `↑` / `w` / Enter / Space | DOWN | Activate / open / back |
| Esc / `q` | — | Quit |

Optional flags:

| Flag | Default | Purpose |
|------|---------|---------|
| `--scale` | 2 | Window scale factor (2 → 1052×600) |
| `--fps-cap` | 30 | Wall-clock refresh rate for the local window |
| `--ui-fps` | 12 | Renderer frame rate (controls animation timing) |
| `--nav-zoom` | 14 | OSM zoom level for the map screen |
| `--nav-speed-kmh` | 80 | Simulated bike speed along the GPX track |
| `--gpx-dir` | `gpx_files` | Folder scanned for `.gpx` files |
| `--tile-cache` | `tile_cache` | Pre-cached tile pyramid |
| `--calibration-grid` | off | Show alignment grid instead of the menu |


### Step 3 — Stream the Qt UI to the dash

Once tiles are cached and the Mac is joined to the dash's Wi-Fi:

```bash
source .venv/bin/activate

python -m dash_ui.qt_prototype \
    --ssid RE_xxxx_yymmdd \
    --gpx-dir gpx_files \
    --tile-cache tile_cache \
    --nav-zoom 14 \
    --fps 12 \
    --bitrate-kbps 350
```

The Qt prototype shares every flag with the pygame `prototype.py`.  The
ones most relevant to the map view:

| Flag | Default | Purpose |
|------|---------|---------|
| `--ssid` | _(required)_ | Dash Wi-Fi SSID |
| `--gpx-dir` | `gpx_files` | GPX folder scanned by the GPS Tracks menu |
| `--tile-cache` | `tile_cache` | Pre-cached tile pyramid |
| `--nav-zoom` | 14 | OSM zoom level for the map basemap |
| `--nav-speed-kmh` | 20 | Simulated speed along the GPX track |
| `--fps` | 12 | Encoder frame rate |
| `--bitrate-kbps` | 300 | H.264 bitrate (300–450 is reliable at 12 fps) |
| `--fake-buttons` | off | Inject LEFT/RIGHT/DOWN on a 1.5 s timer |
| `--no-auth` | off | Skip RSA handshake (experiments only) |

Run `python -m dash_ui.qt_prototype --help` for the full flag list.


## Custom UI prototype (`dash_ui` — pygame renderer)

`dash_ui` also ships a pygame-based prototype for environments where
PySide6 is not available.

### Local development (no bike needed)

```bash
source .venv/bin/activate
python -m dash_ui.local_test --scale 2   # 1052x600 window (526x300 ×2)
```

### Stream the UI to the dash

```bash
python -m dash_ui.prototype --ssid RE_xxxx_yymmdd --fps 12
```

Useful flags for the prototype:

| Flag | Default | Purpose |
|---|---|---|
| `--ssid` | _(required)_ | Same as `tripper_app_like_nav.py`. |
| `--fps` | 8 | UI / encoder frame rate. The stock phone uses 4 fps; 8–12 is more responsive but pushes the dash decoder. Drop back to 4 if the dash blinks. |
| `--bitrate-kbps` | 205 | H.264 bitrate. Bump to 300–450 if you raised the fps. |
| `--rtp-payload` | 1380 | Max RTP payload bytes. Lower to 1000/1200 if you see Wi-Fi loss. |
| `--video-file` | `test_640.mp4` | File played by the **Video** menu item. |
| `--calibration-grid` | off | Stream a grid + concentric circles instead of the menu — useful for finding the round dash's safe area. |
| `--fake-buttons` | off | Inject LEFT/RIGHT/DOWN/CLICK on a 1.5 s timer. |
| `--no-auth` | off | Skip the RSA handshake (protocol experiments only). |

### Bike-side button events

While streaming, the dash sends joystick / click events on UDP/2002 as
`09 00 0001 XX` segments.  Discovered byte values:

| Bytes | Button |
|---|---|
| `09 00 0001 13` | RIGHT |
| `09 00 0001 14` | LEFT |
| `09 00 0001 15` | DOWN |
| `09 00 0001 18` | CLICK (followed by `05 …` and `09 …` segments) |

`bike_link.py` echoes a `06 80 0001 XX` ack back to the dash (same
shape as the existing `q3c.r2 / u2 / …` acks) and dispatches each
event to the renderer via the `on_button` callback.

### How to extend the UI

To change the look, edit the renderer file that matches your stack:

- **Qt:** `dash_ui/qt_renderer.py` — modify `MENU_ITEMS`, `_apply()`,
  and `_draw_*()` methods.
- **pygame:** `dash_ui/pygame_renderer.py` — same approach.

Both renderers implement the `Renderer` protocol from `dash_ui/renderer.py`
(`width`, `height`, `fps`, `render_frame() → bytes` in RGB24).  Any renderer
that satisfies that protocol works with `DashUIStream` unchanged.


## Disclaimer

This is reverse-engineered, hobbyist code. It is **not affiliated with
Royal Enfield**. Use at your own risk; the dash's safety-critical
features should never depend on this. The protocol description and any
captured handshake material here come from black-box network observation
of an officially paired phone + dash on a private network.
