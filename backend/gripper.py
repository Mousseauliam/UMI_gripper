import math
import struct
import threading
import time
from pathlib import Path
from .canbus import CanBus
from .utils import finite, load_json, motor_id, save_json

KP = 2.04
KD = 0.178571
PERIOD = 0.005
DEFAULT_TRAVEL_SECONDS = 0.45
def jaw_angle(value, config):
    opened, closed = config["opened"], config["closed"]
    points = config.get("jaw_points") or {}
    if not points:
        fraction = math.acos(1 - 2 * value) / math.pi
        return opened + fraction * (closed - opened)
    anchors = [(0.0, opened), *sorted((int(p) / 100, a) for p, a in points.items()), (1.0, closed)]
    for (start, a), (end, b) in zip(anchors, anchors[1:]):
        if value <= end:
            return a + (b - a) * (value - start) / (end - start)



class Axis:
    def __init__(self, config, bus, lock, record):
        self.config, self.bus, self.lock, self.record = config, bus, lock, record
        self.active = self.vr = self.opening = self.released = False
        self.target = None
        self.command_target = None
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
            raise ValueError("Définir deux positions distinctes")
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
            self.target = self.command_target = position
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

    def trigger(self, value):
        with self.lock:
            value = finite(value, 0, 1)
            if not self.vr or not self.active:
                raise ValueError("VR inactive")
            self.seen = time.monotonic()
            self.released = self.released or value <= 0.02
            if self.released:
                self.opening = False
                self.move(jaw_angle(value, self.config))

    def feedback(self, now):
        mid = self.config["selected"]
        status = self.bus.status.get(mid, {})
        if self.bus.faults.get(mid):
            raise RuntimeError(f"Défaut moteur {mid}: {self.bus.faults[mid]}")
        if status.get("faults"):
            raise RuntimeError(f"Défaut moteur {mid}: masque {status['faults']}")
        if self.vr and now - self.seen > 1:
            raise RuntimeError("Signal VR perdu : moteurs arrêtés")
        if now - self.started > 0.25 and now - status.get("seen", 0) > 0.25:
            raise RuntimeError(f"Retour CAN perdu : moteur {mid}")
        if now - self.started > 0.5 and status.get("mode") == 0:
            raise RuntimeError(f"Moteur {mid} désactivé malgré la commande MIT")
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
            kp=KP,
            kd=KD,
            target=self.command_target,
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
            kp=KP,
            kd=KD,
        )
        self.record.motor_state(self.config, self.telemetry)


SIDES = {"left": 18, "right": 28}


class Motor:
    def __init__(self, path, record):
        self.path = Path(path)
        self.config = load_json(self.path)
        self.config.setdefault("travel_seconds", DEFAULT_TRAVEL_SECONDS)
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
                k: axis.config.get(k) for k in ("opened", "closed", "uid", "jaw_points")
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
                    raise ValueError(f"Gripper {side} ({mid}) absent : vérifier alimentation/CAN")
                self.motors[mid] = uid
                axis = self.axis(mid)
                try:
                    axis.positions()
                except (TypeError, ValueError):
                    raise ValueError(
                        f"Calibrer le gripper {side} ({mid}) : définir ouvert et fermé"
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
                    finite(values.get(side), 0, 1)
                for side, axis in active:
                    axis.trigger(values[side])
            except Exception:
                self.stop()
                raise

    def action(self, name, data):
        with self.lock:
            self.error = ""
            if self.scanning and name != "off":
                raise ValueError("Scan en cours")
            axis = self.axis(self.config["selected"])
            if name == "on":
                self.on()
            elif name == "off":
                self.stop()
            elif name == "vr":
                self.on(vr=True)
            elif name == "toggle":
                if self.vr:
                    raise ValueError("Quitter le mode VR")
                c = axis.config
                position = axis.target if axis.target is not None else axis.telemetry["position"]
                axis.move(
                    c["closed"]
                    if abs(position - c["opened"]) < abs(position - c["closed"])
                    else c["opened"]
                )
            elif name == "speed":
                self.config["travel_seconds"] = finite(data["seconds"], 0.30, 1.20)
                self.save()
            elif name == "select":
                if self.active:
                    raise ValueError("Passer Motor OFF avant de changer de moteur")
                mid = motor_id(data["id"])
                uid = self.bus.ping(mid)
                if not uid:
                    raise ValueError("Moteur absent")
                self.motors[mid] = uid
                self.axis(mid).config["uid"] = uid
                self.config["selected"] = mid
                self.save()
            elif name in ("opened", "closed", "jaw-point"):
                if self.active:
                    raise ValueError("Passer Motor OFF avant de définir les angles")
                position = finite(
                    self.bus.read(axis.config["selected"], 0x7019), -12.5, 12.5
                )
                if name == "jaw-point":
                    percent = str(data["percent"])
                    if percent not in ("25", "50", "75"):
                        raise ValueError("Choisir 25, 50 ou 75 %")
                    opened, closed = axis.positions()
                    points = dict(axis.config.get("jaw_points") or {})
                    points[percent] = position
                    angles = [opened, *(points[k] for k in sorted(points)), closed]
                    if any((b - a) * (closed - opened) <= 0 for a, b in zip(angles, angles[1:])):
                        raise ValueError("Vérifier la position et l'ordre des points de calibration")
                    axis.config["jaw_points"] = points
                else:
                    axis.config[name] = position
                    axis.config["jaw_points"] = {}
                self.save()
            else:
                raise ValueError("Commande inconnue")

    def scan(self):
        with self.lock:
            if self.active or self.scanning:
                raise ValueError("Passer Motor OFF avant le scan")
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
        elapsed = PERIOD if self.last_tick is None else min(now - self.last_tick, 0.02)
        for axis in active:
            stroke = abs(axis.config["closed"] - axis.config["opened"])
            max_step = stroke * elapsed / self.config["travel_seconds"]
            error = axis.target - axis.command_target
            axis.command_target += max(-max_step, min(max_step, error))
        commands = [(a.config["selected"], a.command_target, KP, KD) for a in active]
        self.bus.send_mit_batch(commands)
        stamp = time.monotonic_ns()
        for axis in active:
            self.record.motor_command(axis.config, axis.command_target, stamp)
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
