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


## Install

```bash
git clone <this repo>
cd better-dash

python3 -m venv .venv
source .venv/bin/activate

pip install --upgrade pip
pip install cryptography     # required for the RSA/AES auth handshake
brew install ffmpeg          # macOS — script invokes the `ffmpeg` binary
                             # On Debian/Ubuntu: sudo apt install ffmpeg
```

`cryptography` is the only Python dependency; the rest of the script
is stdlib.

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

## Disclaimer

This is reverse-engineered, hobbyist code. It is **not affiliated with
Royal Enfield**. Use at your own risk; the dash's safety-critical
features should never depend on this. The protocol description and any
captured handshake material here come from black-box network observation
of an officially paired phone + dash on a private network.
