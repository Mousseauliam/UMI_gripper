"""RobStride private CAN protocol over CANable2 SLCAN. No motion on connect."""

import glob
import math
import struct
import time


from .utils import finite, motor_id


class CanBus:
    host = 255

    def __init__(self, port=None):
        self.port = port
        self.serial = None
        self.buffer = b""
        self.status = {}
        self.parameters = {}
        self.faults = {}

    def connect(self):
        if self.serial and self.serial.is_open:
            return
        import serial

        candidates = glob.glob("/dev/serial/by-id/*CANable*")
        port = self.port or (candidates[0] if candidates else "/dev/ttyACM0")
        self.serial = serial.Serial(port, 115200, timeout=0.001, write_timeout=0.3, exclusive=True)
        self.port = port
        for command in (b"C\r", b"S8\r", b"M0\r", b"A0\r", b"O\r"):
            self.serial.write(command)
            time.sleep(0.05)
        time.sleep(0.15)
        self.serial.reset_input_buffer()
        self.buffer = b""

    def close(self):
        if self.serial:
            try:
                self.serial.write(b"C\r")
            finally:
                self.serial.close()
                self.serial = None

    def send(self, kind, mid, data=b"\0" * 8, extra=None):
        self.connect()
        mid = motor_id(mid)
        if len(data) != 8:
            raise ValueError("DLC must be 8")
        arb = (kind << 24) | ((self.host if extra is None else extra) << 8) | mid
        self.serial.write(f"T{arb:08X}8{data.hex().upper()}\r".encode())

    @staticmethod
    def mit_frame(mid, position, kp=2.04, kd=0.178571, velocity=0.0, torque=0.0):
        def u(value, low, high):
            value = finite(value, low, high)
            return int(65535 * (value - low) / (high - low))

        payload = struct.pack(
            ">4H",
            u(position, -4 * math.pi, 4 * math.pi),
            u(velocity, -50, 50),
            u(kp, 0, 500),
            u(kd, 0, 5),
        )
        arb = (1 << 24) | (u(torque, -5.5, 5.5) << 8) | motor_id(mid)
        return f"T{arb:08X}8{payload.hex().upper()}\r".encode()

    def send_mit_batch(self, commands):
        self.connect()
        frames = b"".join(self.mit_frame(*command) for command in commands)
        if frames:
            self.serial.write(frames)

    def _decode(self, line):
        line = line.lstrip(b"\n")
        if not line.startswith(b"T") or len(line) < 26:
            return None
        try:
            arb, dlc = int(line[1:9], 16), int(line[9:10], 16)
            data = bytes.fromhex(line[10 : 10 + 2 * dlc].decode())
        except (ValueError, UnicodeError):
            return None
        if dlc != 8 or len(data) != 8:
            return None
        kind, mid, host = (arb >> 24) & 31, (arb >> 8) & 255, arb & 255
        if host == self.host:
            if kind == 2:
                p, v, t, temp = struct.unpack(">4H", data)
                self.status[mid] = dict(
                    position=p / 65535 * 8 * math.pi - 4 * math.pi,
                    velocity=v / 65535 * 100 - 50,
                    torque=t / 65535 * 11 - 5.5,
                    temperature=temp / 10,
                    faults=(arb >> 16) & 63,
                    mode=(arb >> 22) & 3,
                    seen=time.monotonic(),
                    stamp_ns=time.monotonic_ns(),
                    sequence=self.status.get(mid, {}).get("sequence", 0) + 1,
                )
            elif kind == 17:
                index = struct.unpack_from("<H", data)[0]
                self.parameters[mid, index] = (data[4:], time.monotonic())
            elif kind == 21:
                self.faults[mid] = data.hex()
        return kind, mid, host, data, arb

    def drain(self):
        if not self.serial:
            return
        waiting = self.serial.in_waiting
        if waiting:
            self.buffer += self.serial.read(min(waiting, 16384))
        while b"\r" in self.buffer:
            line, self.buffer = self.buffer.split(b"\r", 1)
            self._decode(line)
        if len(self.buffer) > 65536:
            raise RuntimeError("CAN receive buffer overflow")

    def receive(self, deadline):
        while time.monotonic() < deadline:
            if b"\r" not in self.buffer:
                self.buffer += self.serial.read(max(1, min(self.serial.in_waiting, 4096)))
                continue
            line, self.buffer = self.buffer.split(b"\r", 1)
            frame = self._decode(line)
            if frame is not None:
                yield frame

    def request(self, kind, mid, data=b"\0" * 8, response=None, timeout=0.25, extra=None):
        self.connect()
        # Discard queued traffic before issuing a request; match full response fields.
        list(self.receive(time.monotonic() + 0.001))
        self.send(kind, mid, data, extra)
        expected = kind if response is None else response
        for frame in self.receive(time.monotonic() + timeout):
            typ, source, host, payload, arb = frame
            if typ == 21 and source == mid and host == self.host:
                raise RuntimeError(f"Défaut moteur : {payload.hex()}")
            if (
                typ != expected
                or source != mid
                or host not in ((self.host, 0xFE) if expected == 0 else (self.host,))
            ):
                continue
            if expected == 17 and payload[:2] != data[:2]:
                continue
            if expected == 2 and (arb >> 16) & 63:
                raise RuntimeError(f"Défaut moteur : masque {(arb >> 16) & 63}")
            return payload
        raise TimeoutError(f"Pas de réponse du moteur {mid} (type {kind})")

    def ping(self, mid, timeout=0.07):
        try:
            return self.request(0, mid, timeout=timeout).hex()
        except TimeoutError:
            return None

    def read(self, mid, index, fmt="f"):
        payload = self.request(17, mid, struct.pack("<HHI", index, 0, 0))
        value = struct.unpack_from("<" + fmt, payload, 4)[0]
        if isinstance(value, float) and not math.isfinite(value):
            raise RuntimeError("Télémétrie non finie")
        return value

    def write(self, mid, index, value, fmt="f", verify=True):
        packed = struct.pack("<" + fmt, value).ljust(4, b"\0")
        self.request(18, mid, struct.pack("<HH", index, 0) + packed, response=2)
        if verify:
            # A status ACK can precede application of a configuration value.
            # Retry readback only: never resend a write or enable the motor here.
            for delay in (0, 0.01, 0.03, 0.06):
                if delay:
                    time.sleep(delay)
                actual = self.read(mid, index, fmt)
                if math.isclose(actual, value, rel_tol=1e-4, abs_tol=1e-5):
                    return
            raise RuntimeError(f"Paramètre {index:#x} non confirmé : attendu {value}, lu {actual}")

    def stop(self, mid):
        return self.request(4, mid, response=2)
