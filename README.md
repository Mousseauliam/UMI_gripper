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

`UMI_DATA_DIR`, `UMI_HTTP_PORT` and `UMI_HTTPS_PORT` override the default data directory and ports. The first start copies defaults from `config/` into `data/`. Calibrations, TLS keys and episodes stay in `data/`, excluded from Git. No previous calibration or recording is included.

## Grippers and VR

Left motor: **18**. Right motor: **28**. Save each motor's open/closed positions in the interface before activation. `KP = 2.04`, `KD = 0.178571` and `PERIOD = 0.005` are in `backend/gripper.py`. Velocity target and feedforward torque are zero; the trigger sets the requested position. Zero velocity target is a damping reference, not a speed limit. The 200 Hz loop is a scheduling target, not a real-time guarantee.

Entering VR opens both grippers. Release each trigger once to take control. A toggles the right preview; X toggles the left. B starts recording or ends with success; holding B for two seconds ends with not judged. Y ends with fail. Motor faults and lost control/feedback stop actuation.

USB camera order is defined in `Cameras.usb_loop` in `backend/cameras.py`; CSI splits the Camarray image into torso left/right. Check the mapping after reconnecting hardware.

## From the Quest trigger to the jaws

### 1. Trigger reading to requested closure

WebXR reports a value between 0 (released) and 1 (fully squeezed) for each index trigger. Early in the squeeze, this reading can lag behind the physical finger travel. Both hands use the same fixed correction in `correctedTrigger` in `frontend/js/vr.js`:

```text
closure = 0.45 * raw + 0.55 * (1 - exp(-6 * raw)) / (1 - exp(-6))
```

The curve gives more closure per unit of raw input near the start and less near the end. The horizontal axis below is the reading from the Quest. The vertical axis is the corrected percentage shown in VR and sent over the control WebSocket. The Pi routes the left hand to motor 18 and the right hand to motor 28. Clicking the thumbstick freezes that hand's corrected request while the motor continues regulating the grasp. A second click resumes live input.

![Raw Quest trigger reading versus corrected closure request](assets/plots/trigger-response.svg)

### 2. Requested closure to motor angle

The RS05 rotates a crank whose drive pin sits about 12 mm from the shaft centre. The pin moves a connecting rod, which rotates a rocker. The rocker and a second link guide the jaw. As the links change orientation, the same increment of shaft rotation produces different amounts of jaw travel along the closing axis.

To compensate, `jaw_angle` in `backend/gripper.py` uses the inverse linkage curve from the STEP model. Given a fraction of desired jaw travel, it looks up the fraction of motor rotation needed to reach it. The 33 entries in `STEP_MOTOR_TRAVEL` correspond to closure values `(i / 32)^2`, concentrating samples near the open position. The code interpolates between entries and applies the saved endpoints for that motor:

```text
target_angle = open_angle + motor_fraction * (closed_angle - open_angle)
```

![Requested closure versus fraction of calibrated motor rotation](assets/plots/motor-response.svg)

For example, a **30% raw trigger reading** becomes **59.5% requested closure**, which commands **67.6% of the motor's calibrated rotation** in free space. The model describes a rigid linkage. Backlash, pad compression and the accuracy of the saved endpoints affect the physical result.

### 3. Motor feedback and first contact

The Pi sends MIT position commands at a target rate of 200 Hz. In free space it sends the angle above with fixed gains, zero velocity target and zero feedforward torque. With a stationary shaft, requested torque is approximately `KP * (target_angle - measured_angle)`. While the shaft moves, the damping term also contributes.

Contact detection uses a second geometry table, `PAD_TIP_TRAVEL_MM`. The command curve follows a reference point on the jaw; the contact table follows the tip of one pad. These points have different paths as the jaw rotates. `pad_tip_mm` converts the target and measured motor angles separately into millimetres along the closing axis. Their difference estimates how far that tip is lagging behind the request.

With the current settings, contact requires all three conditions across two distinct motor feedback samples: at least **2.5 mm** of tip lag, more than **0.18 N·m** of torque in the closing direction, and motor speed at or below **0.20 rad/s** in magnitude. Contact clears when the lag falls to **1.0 mm** or less. This lower release threshold keeps small fluctuations from repeatedly switching the vibration on and off.

### 4. Extra squeeze after contact

The controller remembers the corrected trigger value at contact. The remaining squeeze up to 100% increases the requested holding torque linearly from **0.18 to 1.50 N·m** and vibration amplitude from **10% to 100%**. For example, if contact starts at 60%, squeezing to 80% uses half the remaining travel and requests **0.84 N·m** and **55% vibration**. If contact first occurs at full squeeze, the controller requests the maximum immediately.

The controller keeps `KP` and `KD` fixed. It commands a position ahead of the measured shaft position in the closing direction by up to `hold_torque / KP` radians, capped by the remaining angle to the operator's target. This limits how hard the motor pushes against a blocked jaw. The torque shown in the interface is motor feedback. Force at the object also depends on linkage angle and where the object touches the pad.

## Recording

MCAP stores independent camera frames and timestamps without forcing equal frame counts:

- `/cam/wrist_left`, `/cam/wrist_right`: wrist video.
- `/cam/head_stereo/left`, `/cam/head_stereo/right`: torso video (Origin topic names).
- `/cam/quest`: Quest video when camera capture is available and enabled.
- `/quest/poses`: headset/controller poses, including world and head-relative data.
- `/gripper/state`, `/gripper/record`, `/gripper/telemetry`: gripper state and measurements.
- `/origin/episode`, `/umi/events`: episode metadata and events.

The wire schemas are in `backend/schemas.json`. Missing camera streams are reported in episode metadata.

## Git updates on the Pi

Repository: https://github.com/Mousseauliam/UMI_gripper (branch `main`).

On the PC, use normal `git add`, `git commit`, then `git push`. Nothing watches files on the PC. On the Pi, `umi-git-sync.service` runs `deploy/git_sync.py`, checking GitHub every 10 seconds while Ethernet has a connection and IP address. It clones into `/home/admin/umi-v3`, then fast-forwards to new commits. Local changes or divergent commits are left untouched and reported in the journal.

```sh
sudo bash deploy/install-sync.sh
systemctl status umi-git-sync
journalctl -u umi-git-sync -f
```

Configuration: `/etc/umi-git-sync.json`. The read-only deploy key is `/home/admin/.ssh/umi_git_deploy`, stored only on the Pi. With Ethernet connected, Git checks main every 10 seconds. A separate timer checks the downloaded revision every 10 seconds and restarts umi-app whenever it changes, including during VR or recording. Open pages check the served revision every 5 seconds and reload automatically. The app uses /home/admin/umi-env and stores calibrations and episodes in /home/admin/umi-data. Re-run the installer after changing the updater. Until the repository has its first commit on `main`, the service waits and checks again every 10 seconds.

## CAD

[Onshape design](https://cad.onshape.com/documents/b1fdebf4e63a57263e618c6b/w/da7161f2845b0c85eb6ef8cc/e/bb00067b4393a12e92135545)

Put current STL/3MF exports in `assets/print/` and STEP exports in `assets/simulation/`. These folders are placeholders: no CAD export was supplied with the source, and older V1/V2 models have not been substituted.
