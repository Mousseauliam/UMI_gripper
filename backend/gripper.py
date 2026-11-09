import math
import struct
import threading
import time
from pathlib import Path
from .canbus import CanBus
from .utils import finite, load_json, motor_id, save_json

KP = 2.04  # N·m/rad
KD = 0.178571  # N·m·s/rad
PERIOD = 0.005  # s (5 ms, 200 Hz)
SOFT_HOLD_KP = 0.4  # N·m/rad
HOLD_KP = 1.2  # N·m/rad
TOUCH_RUMBLE = 0.5  # vibration amplitude (0 to 1)
HOLD_ERROR_RAD = 0.2  # rad
TOUCHING_TORQUE = 0.3  # N·m
# Normalized motor travel from the STEP linkage, sampled at closure = (i / 32)**2.
STEP_MOTOR_TRAVEL = (
    0.0000000, 0.0042026, 0.0160694, 0.0338792, 0.0558120, 0.0804323,
    0.1067531, 0.1341371, 0.1621861, 0.1906572, 0.2194082, 0.2483618,
    0.2774836, 0.3067676, 0.3362278, 0.3658917, 0.3957982, 0.4259944,
    0.4565361, 0.4874871, 0.5189205, 0.5509204, 0.5835847, 0.6170288,
    0.6513916, 0.6868436, 0.7236005, 0.7619425, 0.8022490, 0.8450584,
    0.8911852, 0.9419775, 1.0000000,
)


def jaw_angle(value, config):
    opened, closed = config["opened"], config["closed"]
    if value <= 0:
        return opened
    if value >= 1:
        return closed
    index = math.sqrt(value) * (len(STEP_MOTOR_TRAVEL) - 1)
    segment = min(int(index), len(STEP_MOTOR_TRAVEL) - 2)
    low, high = STEP_MOTOR_TRAVEL[segment:segment + 2]
    fraction = low + (high - low) * (index - segment)
    return opened + (closed - opened) * fraction


class Axis:
    def __init__(self, config, bus, lock, record):
        self.config, self.bus, self.lock, self.record = config, bus, lock, record
        self.active = self.vr = self.opening = self.released = False
        self.trigger_value = 0.0
        self.contact_trigger = None
        self.rumble = 0.0
        self.target = None
        self.kp = KP
        self.telemetry = {}
        self.seen = self.started = 0
        self.voltage_at = 0
        self.feedback_sequence = 0

    def stop(self):
        with self.lock:
            try:
                if self.active:
                    self.bus.stop(self.config["selected"])
            finally:
                self.active = self.vr = self.opening = False

    def positions(self):
        opened = finite(self.config["opened"], -12.5, 12.5)
        closed = finite(self.config["closed"], -12.5, 12.5)
        if abs(opened - closed) < 0.001:
            raise ValueError("Set distinct open and closed positions")
        return opened, closed

    def on(self, vr=False):
        with self.lock:
            if self.active:
                self.stop()
            c = self.config
            mid = motor_id(c["selected"])
            opened, _ = self.positions()
            self.bus.stop(mid)
            position = finite(self.bus.read(mid, 0x7019), -4 * math.pi, 4 * math.pi)
            voltage = self.bus.read(mid, 0x701C)
            self.bus.write(mid, 0x7005, 0, "B")
            self.bus.write(mid, 0x7028, 20000, "I")
            self.kp = KP
            self.contact_trigger = None
            self.trigger_value = self.rumble = 0.0
            self.target = position
            self.telemetry = dict(position=position, voltage=voltage)
            self.bus.faults.pop(mid, None)
            self.active = True
            try:
                self.bus.request(3, mid, response=2)
                self.bus.send_mit_batch([(mid, position, KP, KD)])
                self.started = self.seen = time.monotonic()
                self.vr = vr
                self.released = False
                if vr:
                    self.opening = True
                    self.move(opened)
            except Exception:
                self.stop()
                raise

    def move(self, position):
        if not self.active:
            raise ValueError("Motor OFF")
        c = self.config
        self.target = finite(position, min(c["opened"], c["closed"]), max(c["opened"], c["closed"]))

    def command_kp(self):
        direction = math.copysign(1, self.config["closed"] - self.config["opened"])
        closing_error = direction * (self.target - self.telemetry["position"])
        closing_torque = direction * self.telemetry.get("torque", 0.0)
        if self.vr:
            error_threshold = 0.06  # rad, release an existing contact
            touching = closing_torque > TOUCHING_TORQUE and closing_error >= HOLD_ERROR_RAD
        else:
            speed = abs(self.telemetry.get("velocity", math.inf))
            error_threshold = HOLD_ERROR_RAD
            touching = self.config["selected"] == 28 and closing_torque > 0.4 and speed < 0.15  # torque in N·m, speed in rad/s

        if closing_error <= error_threshold:
            self.contact_trigger = None
            self.rumble = 0.0
            return KP
        if self.contact_trigger is None:
            if not touching:
                return KP
            self.contact_trigger = self.trigger_value

        if not self.vr:
            return HOLD_KP
        if self.contact_trigger >= 1.0:
            self.rumble = 0.8 * self.trigger_value  # vibration amplitude (0 to 1)
            return HOLD_KP
        pressure = max(0.0, (self.trigger_value - self.contact_trigger) / (1 - self.contact_trigger))
        self.rumble = TOUCH_RUMBLE + (0.8 - TOUCH_RUMBLE) * pressure  # vibration amplitude (0 to 1)
        return SOFT_HOLD_KP + (HOLD_KP - SOFT_HOLD_KP) * pressure

    def trigger(self, value):
        with self.lock:
            value = finite(value, 0, 1)
            if not self.vr or not self.active:
                raise ValueError("VR inactive")
            self.seen = time.monotonic()
            self.released = self.released or value <= 0.02
            if self.released:
                self.opening = False
                self.set_trigger_target(value)

    def set_trigger_target(self, value):
        self.trigger_value = value
        self.move(jaw_angle(value, self.config))

    def feedback(self, now):
        mid = self.config["selected"]
        status = self.bus.status.get(mid, {})
        if self.bus.faults.get(mid):
            raise RuntimeError(f"Motor {mid} fault: {self.bus.faults[mid]}")
        if status.get("faults"):
            raise RuntimeError(f"Motor {mid} fault mask: {status['faults']}")
        if self.vr and now - self.seen > 1:
            raise RuntimeError("VR signal lost: motors stopped")
        if now - self.started > 0.25 and now - status.get("seen", 0) > 0.25:
            raise RuntimeError(f"CAN feedback lost for motor {mid}")
        if now - self.started > 0.5 and status.get("mode") == 0:
            raise RuntimeError(f"Motor {mid} disabled despite MIT command")
        if status.get("sequence", 0) == self.feedback_sequence:
            return
        self.feedback_sequence = status["sequence"]
        voltage = self.telemetry.get("voltage")
        parameter = self.bus.parameters.get((mid, 0x701C))
        if parameter:
            value = struct.unpack("<f", parameter[0])[0]
            voltage = value if math.isfinite(value) else None
        self.telemetry = dict(
            position=status["position"],
            velocity=status["velocity"],
            torque=status["torque"],
            temperature=status["temperature"],
            voltage=voltage,
            stamp_ns=status["stamp_ns"],
            control_mode="MIT",
            kp=self.kp,
            kd=KD,
            target=self.target,
            requested_target=self.target,
            velocity_target=0.0,
            torque_ff=0.0,
            opening=self.opening,
            released=self.released,
            motor_mode=status.get("mode"),
        )
        self.record.motor_state(self.config, self.telemetry)

    def poll(self):
        mid = self.config["selected"]
        position = self.bus.read(mid, 0x7019)
        voltage = self.telemetry.get("voltage")
        if voltage is None or time.monotonic() - self.voltage_at >= 1:
            voltage = self.bus.read(mid, 0x701C)
            self.bus.stop(mid)
            self.voltage_at = time.monotonic()
        status = self.bus.status.get(mid, {})
        temperature = (
            status.get("temperature") if time.monotonic() - status.get("seen", 0) < 2 else None
        )
        self.telemetry = dict(
            position=position,
            voltage=voltage,
            temperature=temperature,
            stamp_ns=time.monotonic_ns(),
            control_mode="MIT",
            kp=self.kp,
            kd=KD,
        )
        self.record.motor_state(self.config, self.telemetry)


SIDES = {"left": 18, "right": 28}


class Motor:
    def __init__(self, path, record):
        self.path = Path(path)
        self.config = load_json(self.path)
        self.config.pop("travel_seconds", None)
        for calibration in self.config["grippers"].values():
            calibration.pop("jaw_points", None)
            calibration.pop("trigger_midpoint", None)
            calibration.pop("trigger_curve", None)
        self.bus = CanBus()
        self.lock = threading.RLock()
        self.record = record
        self.axes = {}
        self.motors = {}
        self.error = ""
        self.scanning = False
        self.running = True
        self.timing = dict(
            target_hz=200, measured_hz=0.0, max_interval_ms=0.0, cycles=0, missed_deadlines=0
        )
        self.last_tick = None
        self.window_start = None
        self.window_cycles = 0
        self.voltage_next = 0
        self.voltage_index = 0

    def axis(self, mid):
        if mid not in self.axes:
            calibration = self.config["grippers"].get(str(mid), {})
            config = {"selected": mid, "opened": None, "closed": None, **calibration}
            self.axes[mid] = Axis(config, self.bus, self.lock, self.record)
        return self.axes[mid]

    @property
    def active(self):
        return any(a.active for a in self.axes.values())

    @property
    def vr(self):
        return any(a.vr for a in self.axes.values())

    @property
    def opening(self):
        return any(a.opening for a in self.axes.values())

    def save(self):
        for mid, axis in self.axes.items():
            self.config["grippers"][str(mid)] = {
                k: axis.config.get(k) for k in ("opened", "closed", "uid")
            }
        save_json(self.path, self.config)

    def stop(self):
        with self.lock:
            errors = []
            for axis in self.axes.values():
                try:
                    axis.stop()
                except Exception as exc:
                    errors.append(str(exc))
            if errors:
                raise RuntimeError("; ".join(errors))

    def on(self, vr=False):
        with self.lock:
            self.stop()
            self.last_tick = self.window_start = None
            self.window_cycles = 0
            self.timing = dict(
                target_hz=200, measured_hz=0.0, max_interval_ms=0.0, cycles=0, missed_deadlines=0
            )
            if not vr:
                self.axis(self.config["selected"]).on()
                return
            found = []
            for side, mid in SIDES.items():
                uid = None
                for attempt in range(3):
                    uid = self.bus.ping(mid, 0.15)
                    if uid:
                        break
                if not uid:
                    raise ValueError(f"Gripper {side} ({mid}) missing: check power and CAN")
                self.motors[mid] = uid
                axis = self.axis(mid)
                try:
                    axis.positions()
                except (TypeError, ValueError):
                    raise ValueError(
                        f"Calibrate the {side} gripper ({mid}): set open and closed positions"
                    )
                found.append(axis)
            try:
                for axis in found:
                    axis.on(vr=True)
                now = time.monotonic()
                for axis in found:
                    axis.seen = axis.started = now
            except Exception:
                self.stop()
                raise

    def trigger(self, values):
        with self.lock:
            if not self.vr:
                raise ValueError("VR inactive")
            active = [
                (side, self.axes[mid])
                for side, mid in SIDES.items()
                if mid in self.axes and self.axes[mid].vr
            ]
            try:
                for side, axis in active:
                    if values.get(side) is None:
                        raise ValueError(f"Missing trigger signal ({side})")
                    finite(values[side], 0, 1)
                for side, axis in active:
                    axis.trigger(values[side])
            except Exception:
                self.stop()
                raise

    def action(self, name, data):
        with self.lock:
            self.error = ""
            if self.scanning and name != "off":
                raise ValueError("Scan in progress")
            axis = self.axis(self.config["selected"])
            if name == "on":
                self.on()
            elif name == "off":
                self.stop()
            elif name == "vr":
                self.on(vr=True)
            elif name == "toggle":
                if self.vr:
                    raise ValueError("Exit VR mode")
                c = axis.config
                position = axis.target if axis.target is not None else axis.telemetry["position"]
                near_open = abs(position - c["opened"]) < abs(position - c["closed"])
                axis.set_trigger_target(1.0 if near_open else 0.0)
            elif name == "select":
                if self.active:
                    raise ValueError("Turn the motor off before switching motors")
                mid = motor_id(data["id"])
                uid = self.bus.ping(mid)
                if not uid:
                    raise ValueError("Motor not detected")
                self.motors[mid] = uid
                self.axis(mid).config["uid"] = uid
                self.config["selected"] = mid
                self.save()
            elif name in ("opened", "closed"):
                if self.active:
                    raise ValueError("Turn the motor off before setting the angles")
                axis.config[name] = finite(
                    self.bus.read(axis.config["selected"], 0x7019), -12.5, 12.5
                )
                self.save()
            else:
                raise ValueError("Unknown command")

    def scan(self):
        with self.lock:
            if self.active or self.scanning:
                raise ValueError("Turn the motor off before scanning")
            self.scanning = True
            self.motors = {}
        try:
            for mid in range(1, 255):
                with self.lock:
                    uid = self.bus.ping(mid, 0.025)
                    if uid:
                        self.motors[mid] = uid
        finally:
            self.scanning = False

    def state(self):
        with self.lock:
            axis = self.axis(self.config["selected"])
            config = dict(self.config, **axis.config)
            grippers = {
                side: dict(
                    id=mid,
                    detected=mid in self.motors,
                    rumble=self.axes[mid].rumble if mid in self.axes and self.axes[mid].vr else 0.0,
                    active=self.axes[mid].active if mid in self.axes else False,
                    opening=self.axes[mid].opening if mid in self.axes else False,
                    released=self.axes[mid].released if mid in self.axes else False,
                    calibration=self.config["grippers"].get(str(mid), {}),
                    telemetry=self.axes[mid].telemetry.copy() if mid in self.axes else {},
                )
                for side, mid in SIDES.items()
            }
            return dict(
                active=self.active,
                vr=self.vr,
                opening=self.opening,
                config=config,
                telemetry=axis.telemetry.copy(),
                grippers=grippers,
                control=dict(mode="MIT", kp=KP, kd=KD, **self.timing),
                motors=self.motors.copy(),
                scanning=self.scanning,
                error=self.error,
            )

    def control_tick(self, now):
        active = [axis for axis in self.axes.values() if axis.active]
        if not active:
            return
        self.bus.drain()
        for axis in active:
            axis.feedback(now)
        for axis in active:
            axis.kp = axis.command_kp()
        commands = [(a.config["selected"], a.target, a.kp, KD) for a in active]
        self.bus.send_mit_batch(commands)
        stamp = time.monotonic_ns()
        for axis in active:
            self.record.motor_command(axis.config, axis.target, stamp)
        if now >= self.voltage_next:
            axis = active[self.voltage_index % len(active)]
            self.bus.send(17, axis.config["selected"], struct.pack("<HHI", 0x701C, 0, 0))
            self.voltage_index += 1
            self.voltage_next = now + 0.5
        if self.last_tick is not None:
            interval = now - self.last_tick
            self.timing["max_interval_ms"] = max(self.timing["max_interval_ms"], interval * 1000)
            if interval > PERIOD * 1.5:
                self.timing["missed_deadlines"] += 1
        self.last_tick = now
        self.timing["cycles"] += 1
        if self.window_start is None:
            self.window_start = now
            self.window_cycles = 0
        else:
            self.window_cycles += 1
            if now - self.window_start >= 1:
                self.timing["measured_hz"] = self.window_cycles / (now - self.window_start)
                self.window_cycles = 0
                self.window_start = now

    def loop(self):
        deadline = time.monotonic()
        while self.running:
            try:
                with self.lock:
                    if not self.scanning:
                        if self.active:
                            self.control_tick(time.monotonic())
                        else:
                            axis = self.axis(self.config["selected"])
                            axis.poll()
                            if axis.config.get("uid"):
                                self.motors[axis.config["selected"]] = axis.config["uid"]
            except Exception as exc:
                self.error = str(exc)
                try:
                    self.stop()
                except Exception:
                    pass
            period = PERIOD if self.active else 0.2
            deadline += period
            now = time.monotonic()
            if deadline < now:
                deadline += (int((now - deadline) / period) + 1) * period
            time.sleep(max(0, deadline - now))

    def close(self):
        self.running = False
        try:
            self.stop()
        finally:
            self.bus.close()
