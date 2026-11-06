# UMI gripper

Two RS05 grippers, four Pi cameras and Quest teleoperation, with MCAP recording.

## Code map

| File | Responsibility |
| --- | --- |
| `backend/main.py` | Entry point, HTTP routes, startup and shutdown |
| `backend/app.py` | HTTP/WebSocket handlers connecting the interface to the hardware |
| `backend/gripper.py` | Calibration, left/right routing and the 200 Hz MIT loop |
| `backend/canbus.py` | RS05 CAN messages through the USB CANable serial adapter |
| `backend/cameras.py` | USB/CSI capture, preview and H.264 encoding |
| `backend/recording.py` | MCAP messages and episode lifecycle |
| `backend/export.py` | Optional LeRobot and GR00T dataset exports |
| `backend/utils.py` | Paths, environment settings, JSON persistence and input checks |
| `frontend/index.html`, `frontend/css/style.css` | Page structure and appearance |
| `frontend/js/app.js` | Gripper, Recording and Robot Episode interface |
| `frontend/js/vr.js` | Passthrough, panels, controller poses, triggers and recording buttons |
| `frontend/js/quest_camera.js` | Quest camera capture and transfer |
| `deploy/git_sync.py` | Pi-only Git updater |

## Run manually on the Pi

Use Python 3.11 or newer. Camera capture also needs `ffmpeg`, `rpicam-vid` and the appropriate Camarray driver/boot configuration. These hardware dependencies are not installed by the Git updater.

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m backend.main
```

Open `http://<pi-ip>:8080`. Quest WebXR needs a secure context: place a certificate trusted by the headset at `data/tls/cert.pem` and its key at `data/tls/key.pem`, then use `https://<pi-host>:8443`.

`UMI_DATA_DIR`, `UMI_HTTP_PORT` and `UMI_HTTPS_PORT` override the default data directory and ports. The first start copies defaults from `config/` into `data/`. Calibrations, TLS keys, episodes and exports stay in `data/`, excluded from Git. No previous calibration or recording is included.

## Grippers and VR

Left motor: **18**. Right motor: **28**. Save each motor's open/closed positions in the interface before activation. `KP = 2.04`, `KD = 0.178571` and `PERIOD = 0.005` are in `backend/gripper.py`. Velocity target and feedforward torque are zero; the trigger sets the requested position. Zero velocity target is a damping reference, not a speed limit. The 200 Hz loop is a scheduling target, not a real-time guarantee.

Entering VR opens both grippers. Release each trigger once to take control. A toggles the right preview; X toggles the left. B starts recording or ends with success; holding B for two seconds ends with not judged. Y ends with fail. Motor faults and lost control/feedback stop actuation.

USB camera order is defined in `Cameras.usb_loop` in `backend/cameras.py`; CSI splits the Camarray image into torso left/right. Check the mapping after reconnecting hardware.

## Recording

MCAP stores independent camera frames and timestamps without forcing equal frame counts:

- `/cam/wrist_left`, `/cam/wrist_right`: wrist video.
- `/cam/head_stereo/left`, `/cam/head_stereo/right`: torso video (Origin topic names).
- `/cam/quest`: Quest video when camera capture is available and enabled.
- `/quest/poses`: headset/controller poses, including world and head-relative data.
- `/gripper/state`, `/gripper/record`, `/gripper/telemetry`: gripper state and measurements.
- `/origin/episode`, `/umi/events`: episode metadata and events.

The wire schemas are in `backend/schemas.json`. Dataset exports resample separately; they do not change the source MCAP. Missing camera streams are reported in episode metadata.

## Git updates on the Pi

Repository: https://github.com/Mousseauliam/UMI_gripper (branch `main`).

On the PC, use normal `git add`, `git commit`, then `git push`. Nothing watches files on the PC. On the Pi, `umi-git-sync.service` runs `deploy/git_sync.py`, checking GitHub every 10 seconds while Ethernet has a connection and IP address. It clones into `/home/admin/umi-v3`, then fast-forwards to new commits. Local changes or divergent commits are left untouched and reported in the journal.

```sh
sudo bash deploy/install-sync.sh
systemctl status umi-git-sync
journalctl -u umi-git-sync -f
```

Configuration: `/etc/umi-git-sync.json`. The read-only deploy key is `/home/admin/.ssh/umi_git_deploy`, stored only on the Pi. The Git service downloads commits. umi-app.path starts the app on the first deployment. umi-app.service runs it on subsequent boots. After later pushes, restart the app between sessions with sudo systemctl restart umi-app. The app uses /home/admin/umi-env and stores calibrations and episodes in /home/admin/umi-data. Re-run the installer after changing the updater. Until the repository has its first commit on `main`, the service waits and checks again every 10 seconds.

## CAD

[Onshape design](https://cad.onshape.com/documents/b1fdebf4e63a57263e618c6b/w/da7161f2845b0c85eb6ef8cc/e/bb00067b4393a12e92135545)

Put current STL/3MF exports in `assets/print/` and STEP exports in `assets/simulation/`. These folders are placeholders: no CAD export was supplied with the source, and older V1/V2 models have not been substituted.

## Speed and mid-grip precision

Gripper speed sets the minimum commanded time for one calibrated stroke, for both axes in either direction. Choose 0.30, 0.45 (default), 0.75 or 1.20 seconds. This applies to entering VR, toggle and trigger movements. Actual travel depends on load and motor response. The MIT loop advances the command by at most stroke × elapsed time / selected time on each tick. Delayed ticks use at most 20 ms of catch-up. Small deliberate movements can reach their target in just a few ticks.

While holding a trigger, changes smaller than 1.5% of its range accumulate relative to the last accepted value. The outer 1% snaps to fully open/closed. Each hand has independent state. Raw trigger readings remain in Quest samples, while the MCAP motor command is the limited setpoint sent to CAN. This setting limits setpoint speed, not gripping force.
