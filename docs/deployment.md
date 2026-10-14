# Setting up Iris and Shimmer

This sets up the [ground station RPC](remote.md) on our two machines:

| Machine | Role | Runs |
| --- | --- | --- |
| **Iris** | Raspberry Pi on the aircraft, wired to the A8 Mini | The camera service: `python -m siyi_sdk.remote.server` |
| **Shimmer** | Ground station | Ground software using `RemoteCamera`, plus RTSP video straight from the camera |

```
Shimmer ── radio link ──┬──▶ Iris :8765 (camera service) ──UDP 37260──▶ A8 Mini 192.168.144.25
                        └──────────────────────── RTSP :8554 ─────────────▶ A8 Mini
```

Shimmer sends commands to Iris over a websocket. Iris talks to the camera and runs point lock. Shimmer pulls video from the camera directly, because the camera's `192.168.144.x` network is routed over the radio.

Both machines get a full clone of this repository. Replace `<iris>` below with Iris's hostname or IP address as seen from Shimmer.

## 1. Iris (aircraft)

### Network

Iris needs an address on the camera's network, on the interface wired to the camera, for example `192.168.144.20/24`. Don't give that interface a default gateway; the camera network is local. With NetworkManager (Raspberry Pi OS Bookworm), where `eth0` is the camera port:

```bash
sudo nmcli con mod "Wired connection 1" ipv4.method manual ipv4.addresses 192.168.144.20/24 ipv4.gateway ""
sudo nmcli con up "Wired connection 1"
ping -c 3 192.168.144.25          # the camera should answer
```

Skip this if the network is already configured.

### Install

```bash
sudo apt install -y git libgl1 libglib2.0-0      # OpenCV's shared libraries on Raspberry Pi OS Lite
curl -LsSf https://astral.sh/uv/install.sh | sh  # uv; then open a new shell
git clone https://github.com/ncsuarc/siyi_sdk.git ~/siyi_sdk
cd ~/siyi_sdk
uv sync --no-dev --extra remote-server
```

`remote-server` installs the camera service with OpenCV and PyAV for point lock. `--no-dev` skips the test and lint tools.

### First run

```bash
uv run python -m siyi_sdk.remote.server --camera-ip 192.168.144.25 --log-format console
```

Within a few seconds, look for `camera_service_listening port=8765`, and no repeating connection errors. Stop it with Ctrl+C once Shimmer has connected (step 2).

### Start at boot

Create `/etc/systemd/system/siyi-camera.service`, replacing `pi` with Iris's user name:

```ini
[Unit]
Description=SIYI camera service (A8 Mini RPC)
After=network-online.target
Wants=network-online.target

[Service]
User=pi
WorkingDirectory=/home/pi/siyi_sdk
ExecStart=/home/pi/siyi_sdk/.venv/bin/python -m siyi_sdk.remote.server --camera-ip 192.168.144.25
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now siyi-camera
systemctl status siyi-camera
journalctl -u siyi-camera -f       # live logs
```

The service listens on port 8765 on all interfaces. If Iris runs a firewall, allow that port (`sudo ufw allow 8765/tcp`). Lock settings and calibration are saved in `~/.config/siyi/lock.json` on Iris.

## 2. Shimmer (ground station)

### Install

```bash
git clone https://github.com/ncsuarc/siyi_sdk.git
cd siyi_sdk
uv sync --no-dev --extra remote --extra stream-opencv
```

`remote` is the RPC client; `stream-opencv` decodes the camera's RTSP video on Shimmer. Add `--extra tracking` only to run the debugging tools.

### Check the links

From Shimmer:

```bash
ping <iris>                       # the radio link to Iris
ping 192.168.144.25               # the camera, routed over the radio (needed for video)
```

Then connect to the service:

```bash
uv run python -c "
import asyncio
from siyi_sdk.remote import RemoteCamera
async def main():
    async with RemoteCamera('ws://<iris>:8765', name='shimmer') as cam:
        state = await cam.get_state()
        print('camera alive:', state['camera']['alive'], '| video:', state['video'], '| controller:', cam.is_controller)
asyncio.run(main())
"
```

Expect `camera alive: True`, video `live: True`, and `controller: True`.

| Symptom | Check |
| --- | --- |
| `ConnectionError: Cannot reach camera service` | Service running on Iris (`systemctl status siyi-camera`), port 8765 open, correct `<iris>` address |
| `camera alive: False` | `ping 192.168.144.25` *on Iris*; Iris's address on the camera network |
| Video `live: False` | Give it a few seconds after start. Check the service logs on Iris for video errors |
| `controller: False` | Another ground program is connected and has control. Use `take_control(force=True)` or close the other one |

### Try it

```bash
uv run python examples/remote_ground.py ws://<iris>:8765 --video
```

This prints attitude, nudges the gimbal, points it down, and locks the image centre for ten seconds. `--video` also opens the camera's RTSP stream on Shimmer.

### Calibrate point lock (once)

Point the camera at a textured, still scene (not sky or water), with the aircraft still. Then, from Shimmer:

```bash
uv run python -c "
import asyncio
from siyi_sdk.remote import RemoteCamera
async def main():
    async with RemoteCamera('ws://<iris>:8765') as cam:
        print(await cam.calibrate())
asyncio.run(main())
"
```

The gimbal turns about 20 degrees on each axis and back. The result is stored on Iris and used by every later lock, across reboots. Repeat it if the camera is remounted the other way up.

## 3. Ground software on Shimmer

Shimmer's own program uses the SDK from this clone. Either run it with `uv run` from the repository, or install the clone into the ground software's own environment:

```bash
pip install -e "/path/to/siyi_sdk[remote,stream-opencv]"
```

Then connect with `RemoteCamera("ws://<iris>:8765")`. Show video from `SIYIStream(StreamConfig(build_rtsp_url("192.168.144.25")))`, and pass the clicked frame's timestamp to `lock()`. The [ground station guide](remote.md) has the full API.

## Updating

Keep both machines on the same commit. A protocol mismatch is logged as `protocol_mismatch` on Shimmer.

```bash
# Iris
cd ~/siyi_sdk && git pull && uv sync --no-dev --extra remote-server && sudo systemctl restart siyi-camera

# Shimmer
cd siyi_sdk && git pull && uv sync --no-dev --extra remote --extra stream-opencv
```

## Before flight

- `systemctl status siyi-camera` on Iris shows `active (running)`.
- Shimmer's `get_state()` shows the camera alive, video live, and Shimmer as controller.
- Moving the stick moves the gimbal. Letting go stops it.
- Hold the stick and unplug Iris's radio briefly: the gimbal stops within about a second. Plug it back in, and Shimmer reconnects and takes control again by itself. A running point lock keeps going through a dropout; start the service with `--on-disconnect release` if it should stop instead.
- A lock holds while you pan the aircraft by hand, and `release()` ends it.
