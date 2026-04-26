# better-dash

An alternative navigation app for your Royal Enfield Tripper TFT screen.

The script in this repo, **`tripper_app_like_nav.py`**, is a Python emulator
of the official Royal Enfield phone app. It runs on a Mac (Linux works too)
joined to the dash's Wi-Fi access point and reproduces the K1G/UDP control
plane plus the H.264/RTP video stream — enough to take the dash from
`Connected` → `Navigation` and play arbitrary video on its TFT.

## Roadmap

### Phase 1 — Testing & refinement
- [ ] Improve stream framerate beyond 4 fps
- [ ] Understand bike button controls & auxiliary messages (UDP/2002)
- [ ] Pi software — auto-connect to Tripper WiFi & launch UI headlessly

### Phase 2 — Raspberry Pi as a standalone
- [ ] Port Python script to Raspberry Pi Zero
- [ ] Bluetooth GPX ingestion from phone before ride
- [ ] Custom navigation UI (turn-by-turn, 526×300)
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


## What's in this repo

| File / dir | Role |
|---|---|
| `tripper_app_like_nav.py` | Standalone script: auth + nav-mode handshake + 1 Hz tick + RTP video stream from any file. Best entry point if you just want to "send video to the dash". |
| `dash_ui/` | Python package that renders an interactive UI live (pygame), encodes it to H.264, packetises it as RTP, and forwards bike-button events back into the renderer. Imports the K1G primitives from `tripper_app_like_nav.py` for the control plane. |
| `dash_ui/prototype.py` | CLI prototype: full end-to-end "connect to dash + stream UI + react to buttons". |
| `dash_ui/local_test.py` | Local-only harness: opens a Mac window and drives the renderer with the keyboard (no bike, no network). Use this to iterate on the UI fast. |
| `requirements.txt` | Python dependencies. |


## Install

```bash
git clone <this repo>
cd better-dash

python3 -m venv .venv
source .venv/bin/activate

pip install --upgrade pip
pip install -r requirements.txt   # cryptography + pygame-ce
brew install ffmpeg               # macOS — script invokes the `ffmpeg` binary
                                  # On Debian/Ubuntu: sudo apt install ffmpeg
```

`cryptography` is required for the auth handshake.  `pygame-ce` is only
required for the interactive `dash_ui` prototype — `tripper_app_like_nav.py`
on its own only needs `cryptography` + `ffmpeg`.

> **Note on pygame:** the upstream `pygame` package (2.6.1) has a known
> `pygame.font` circular-import bug on Python 3.13/3.14 that makes all
> text invisible.  We use `pygame-ce` (the community fork — 100% drop-in
> compatible) instead.  If you already have `pygame` installed, run
> `pip uninstall -y pygame && pip install pygame-ce`.

## Connect to the dash

1. On the bike, enable the Tripper Wi-Fi (the menu shows the SSID and
   the AP comes up as something like `RE_xxxx_yymmdd`).
2. On the Mac, join that Wi-Fi network. There is no password.
3. Confirm you got an IP on the `192.168.1.x` subnet. The dash itself
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

## Custom UI prototype (`dash_ui`)

`dash_ui` is a Python package that renders an interactive UI in pygame,
encodes it on the fly into H.264, packetises it as RTP and ships it to
the dash on UDP/5000.  In parallel, it listens on UDP/2002 for the
bike's joystick events and forwards them into the renderer's event
queue, so the dash's LEFT / RIGHT / DOWN / CLICK buttons drive the
on-screen menu.

This is what was used to record the dash photo of a four-row menu
("Speed / Map / Music / Settings") with a yellow highlight and live
button feedback.

### Local development (no bike needed)

The fastest iteration loop: `local_test.py` opens a Mac window with
the same renderer the dash sees, and maps the Mac keyboard to the
same `Button` events `bike_link.py` would deliver from UDP/2002.

```bash
source .venv/bin/activate
python -m dash_ui.local_test --scale 2   # 1052x600 window (526x300 ×2)
```

| Key | Bike button |
|---|---|
| ← / `a` | LEFT |
| → / `d` | RIGHT |
| ↓ / `s` / ↑ / `w` | DOWN (bike has no UP) |
| Enter / Space | CLICK |
| Esc / `q` | quit |

### Stream the UI to the dash

Once your Mac is on the dash's Wi-Fi (same prerequisites as the
plain video stream above):

```bash
python -m dash_ui.prototype --ssid RE_xxxx_yymmdd --fps 12
```

You should see:

1. The same UI you saw in `local_test.py` appear on the dash's TFT.
2. Pressing the joystick on the bike (LEFT / RIGHT / DOWN / CLICK)
   moves the highlight and toggles the detail panel.
3. Selecting **Video** + DOWN plays the file passed to `--video-file`
   (defaults to `test_640.mp4` in the working directory).

Useful flags for the prototype:

| Flag | Default | Purpose |
|---|---|---|
| `--ssid` | _(required)_ | Same as `tripper_app_like_nav.py`. |
| `--fps` | 8 | UI / encoder frame rate. The stock phone uses 4 fps; 8–12 is more responsive but pushes the dash decoder. Drop back to 4 if the dash blinks. |
| `--bitrate-kbps` | 205 | H.264 bitrate. Bump to 300–450 if you raised the fps. |
| `--rtp-payload` | 1380 | Max RTP payload bytes. Lower to 1000/1200 if you see Wi-Fi loss. |
| `--video-file` | `test_640.mp4` | File played by the **Video** menu item. |
| `--calibration-grid` | off | Stream a grid + concentric circles instead of the menu — useful for finding the round dash's safe area. |
| `--windowed` | off | Also show the renderer in a Mac window for debugging. |
| `--fake-buttons` | off | Inject LEFT/RIGHT/DOWN/CLICK on a 1.5 s timer (for verifying the UI reacts before the bike is on). |
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

`dash_ui/pygame_renderer.py` is the only file you need to edit to
change the look.  Modify `MENU_ITEMS`, the `_apply()` button handler,
and `_draw()` to add new screens / behaviours; everything else
(encoder, RTP packetizer, K1G handshake, button RX, route-card
keep-alive) is plumbing you do not need to touch.

If you want to write a renderer that isn't pygame (e.g. an OpenGL
off-screen framebuffer or a web view), implement the `Renderer`
protocol from `dash_ui/renderer.py` — it's just `width`, `height`,
`fps`, and `render_frame() → bytes` (RGB24).

## Disclaimer

This is reverse-engineered, hobbyist code. It is **not affiliated with
Royal Enfield**. Use at your own risk; the dash's safety-critical
features should never depend on this. The protocol description and any
captured handshake material here come from black-box network observation
of an officially paired phone + dash on a private network.
