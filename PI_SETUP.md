# Raspberry Pi setup

This guide walks through running `better-dash` on a Raspberry Pi instead
of a Mac/Linux laptop. The Pi joins the bike's Wi-Fi AP, runs the Python
emulator, and pushes the H.264 stream to the dash — exactly the same
pipeline as the Mac, just headless.

Tested on a Pi Zero 2 W (512 MB RAM) running Raspberry Pi OS Bookworm
64-bit Lite. Works on any Pi 3 / 4 / 5 / Zero 2 W. **Pi Zero (1) and Pi 1
are not supported** — they are 32-bit only and PySide6 has no 32-bit ARM
wheels.

> **Working footprint:** ~180 MB RAM at runtime (Qt UI + ffmpeg encode +
> Python). Comfortable on a 512 MB Pi.

---

## 1. Flash the SD card

Use **Raspberry Pi OS Bookworm 64-bit (Lite)**. Set this in the imager's
advanced options before writing:

- Hostname (e.g. `tripperpi`)
- SSH enabled (key or password)
- Wi-Fi SSID + password (your home network — used for the initial install
  only; we'll add the dash AP later)
- User account

> **Why 64-bit?** PySide6 ships pre-built wheels for `aarch64` only.
> 32-bit Pi OS would force you to build Qt from source (slow and brittle).

Boot the Pi, SSH in, then:

```bash
sudo apt update
sudo apt full-upgrade -y
```

If you flashed the smallest SD card you had, expand the filesystem so
later installs don't run out of disk:

```bash
sudo raspi-config --expand-rootfs
sudo reboot
df -h /     # should report the full SD card size
```

---

## 2. System packages

```bash
sudo apt install -y \
  git python3-pip python3-venv \
  ffmpeg \
  libxcb-cursor0 libxkbcommon-x11-0 libgl1
```

`ffmpeg` is mandatory — the streamer shells out to it for H.264
encoding. The three `lib*` packages are the runtime libs Qt needs at
import time, even when running headless.

Verify:

```bash
ffmpeg -version | head -1     # should print 'ffmpeg version …'
uname -m                      # should print 'aarch64'
```

---

## 3. Clone and install

```bash
git clone https://github.com/<your-fork>/better-dash.git
cd better-dash

python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
```

### A gotcha worth knowing about: `/tmp` is small

Pi OS mounts `/tmp` as a RAM-backed `tmpfs` (about half of total RAM).
The PySide6 wheel set unpacks to ~170 MB and will fail mid-install with
`No space left on device` on a 512 MB Pi.

Fix: tell pip to use disk for its temp dir.

```bash
mkdir -p ~/tmp
TMPDIR=~/tmp pip install -r requirements.txt
```

If you only need the Qt UI (not the older pygame prototype), the
slimmer install is enough — it skips PySide6's "Addons" subpackage:

```bash
TMPDIR=~/tmp pip install PySide6-Essentials pygame cryptography
```

Verify:

```bash
python -c "import PySide6, pygame, cryptography; print('ok')"
```

---

## 4. (Optional) Connect the Pi to the dash automatically

Save the dash's Wi-Fi as a NetworkManager profile so the Pi auto-joins
whenever the bike is on:

```bash
sudo nmcli dev wifi connect "RE_xxxx_yymmdd" password "12345678"
```

(Replace `RE_xxxx_yymmdd` with your dash's actual SSID — it's printed on
the dash menu and in the AP advertisement. The factory password on every
Tripper is `12345678`.)

If you also have a home Wi-Fi saved, give the dash AP higher priority so
it wins whenever it's powered on:

```bash
sudo nmcli connection modify "RE_xxxx_yymmdd" connection.autoconnect-priority 100
```

Verify on the bike:

```bash
ip addr show wlan0      # expect 192.168.1.x
ping -c 2 192.168.1.1   # the dash itself
```

---

## 5. (Optional) Pre-cache map tiles for the Qt nav screen

Skip this if you only want to stream a video file or test the menu UI.

```bash
source .venv/bin/activate
python -m dash_ui.download_tiles \
    "gpx_files/Section of Leh-Manali Highway.gpx" \
    --zoom 13 --zoom 14 \
    --mode corridor \
    --buffer-km 1 \
    --cache-dir tile_cache
```

See the README for tile-mode/zoom options.

---

## 6. Smoke tests

### 6a. Qt UI runs locally (no bike, no display)

```bash
QT_QPA_PLATFORM=offscreen python -m dash_ui.qt_local_test
```

If it sits there printing nothing for 10 seconds and Ctrl+C exits
cleanly, the Qt stack is healthy.

### 6b. (Optional) See the UI from your Mac over SSH

If you want to see the Qt window from a Mac over SSH:

1. On the Mac (one time): `brew install --cask xquartz`, then log out
   and back in once so XQuartz registers.
2. SSH with `-Y`: `ssh -Y pi@<pi-hostname>`
3. On the Pi:
   ```bash
   cd better-dash && source .venv/bin/activate
   export QT_QPA_PLATFORM=xcb
   python -m dash_ui.qt_local_test --scale 2
   ```

The window appears on the Mac. Useful for iterating on the UI before
plugging into the bike.

### 6c. Encoder pipeline runs (no bike)

```bash
python -m dash_ui.qt_prototype --no-auth --bike-ip 127.0.0.1 --fps 8
```

You should see ffmpeg launch and lines like
`[dash_ui] stream started → 127.0.0.1:5000`. Ctrl+C to stop.

---

## 7. Run it on the bike

With the Pi joined to the dash AP (step 4):

```bash
cd better-dash && source .venv/bin/activate

python -m dash_ui.qt_prototype \
    --ssid RE_xxxx_yymmdd \
    --fps 10 \
    --bitrate-kbps 300
```

Expected sequence in the logs:

1. `[bike_link] UDP/2000 → 192.168.1.255 …`
2. `[bike_link] auth ok` (or similar) once the RSA handshake completes.
3. `[dash_ui/encoder] starting ffmpeg …`
4. `[dash_ui] stream started → 192.168.1.1:5000`
5. The dash leaves the loading-dots placeholder and renders the Qt UI.

Bike joystick events (LEFT / RIGHT / DOWN / CLICK) appear as
`[bike_link] button: …` lines and drive the menu.

---

## 8. (Optional) Auto-launch on boot

Run the streamer as a systemd service so the Pi starts driving the dash
the moment ignition powers it on. Save as
`/etc/systemd/system/better-dash.service` (replace `<USER>` with your Pi
account name and adjust the path):

```ini
[Unit]
Description=better-dash Tripper streamer
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=<USER>
WorkingDirectory=/home/<USER>/better-dash
ExecStart=/home/<USER>/better-dash/.venv/bin/python -m dash_ui.qt_prototype \
    --ssid RE_xxxx_yymmdd --fps 10 --bitrate-kbps 300
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Enable:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now better-dash.service
journalctl -u better-dash -f      # follow the logs
```

---

## Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| `pip install PySide6` → `No matching distribution found` | You're on 32-bit Pi OS. Reflash with the 64-bit image. |
| `pip install` → `No space left on device` | `/tmp` is tmpfs and too small. Use `TMPDIR=~/tmp pip install …`. |
| Qt errors mentioning `libxcb-cursor` / `libxkbcommon` | Re-run the `apt install` in §2. |
| `[bike_link] auth timeout` | Wrong `--ssid`. The dash validates the SSID inside the encrypted blob — it must match the AP exactly. |
| Dash stays on loading dots / logo loop | Network-level issue. `ping 192.168.1.1` from the Pi. If it fails, the Pi didn't join the dash AP. |
| Choppy frames | Lower `--fps` (8 or 6) and `--bitrate-kbps` (200). Watch CPU with `htop` from a second SSH session. |
| Ctrl+C leaves the script printing Qt warnings | Should already be fixed in `qt_local_test.py` — pull the latest. |
