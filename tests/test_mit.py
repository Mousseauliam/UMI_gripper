import json
import math
from pathlib import Path
import struct
import tempfile
from types import SimpleNamespace
import unittest
from backend.canbus import CanBus
from backend.gripper import Motor, KP, KD


class Bus:
    def __init__(self):
        self.status = {}
        self.faults = {}
        self.parameters = {}
        self.batches = []

    def drain(self):
        pass

    def send_mit_batch(self, commands):
        self.batches.append(commands)

    def send(self, *args):
        pass


class Tests(unittest.TestCase):
    def test_rs05_frame(self):
        frame = CanBus.mit_frame(28, 0)
        self.assertEqual(frame[:10], b"T017FFF1C8")
        self.assertEqual(
            struct.unpack(">4H", bytes.fromhex(frame[10:-1].decode())), (32767, 32767, 267, 2340)
        )
        with self.assertRaises(ValueError):
            CanBus.mit_frame(18, math.nan)
        with self.assertRaises(ValueError):
            CanBus.mit_frame(18, 13.0)

    def test_feedback_temperature_and_rs05_ranges(self):
        bus = CanBus()
        data = struct.pack(">4H", 65535, 65535, 65535, 660)
        bus._decode(f"T02801CFF8{data.hex()}".encode())
        s = bus.status[28]
        self.assertAlmostEqual(s["position"], 4 * math.pi)
        self.assertEqual((s["velocity"], s["torque"], s["temperature"]), (50.0, 5.5, 66.0))

    def test_200hz_both_axes_and_feedback_loss(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            path.write_text(
                json.dumps(
                    dict(
                        selected=28,
                        opened=6.0,
                        closed=4.0,
                        grippers={
                            "18": dict(opened=2.0, closed=1.0),
                            "28": dict(opened=6.0, closed=4.0),
                        },
                    )
                )
            )
            records = []
            recorder = SimpleNamespace(
                motor_command=lambda *a: records.append(a), motor_state=lambda *a: None
            )
            motor = Motor(path, recorder)
            motor.bus = Bus()
            for mid in (18, 28):
                axis = motor.axis(mid)
                axis.active = True
                axis.target = axis.config["opened"]
                axis.started = 10.0
            for i in range(401):
                now = 10 + i * 0.005
                for mid in (18, 28):
                    motor.bus.status[mid] = dict(
                        position=motor.axis(mid).target,
                        velocity=0.0,
                        torque=0.0,
                        temperature=42.0,
                        faults=0,
                        seen=now,
                        stamp_ns=int(now * 1e9),
                        sequence=i + 1,
                    )
                motor.control_tick(now)
            self.assertAlmostEqual(motor.timing["measured_hz"], 200.0)
            self.assertEqual(len(motor.bus.batches), 401)
            self.assertEqual(len(records), 802)
            self.assertEqual(motor.bus.batches[-1], [(18, 2.0, KP, KD), (28, 6.0, KP, KD)])
            motor.axis(28).move(4.0)
            self.assertEqual(len(motor.bus.batches), 401)
            motor.control_tick(12.005)
            self.assertEqual(motor.bus.batches[-1], [(18, 2.0, KP, KD), (28, 4.0, KP, KD)])
            with self.assertRaisesRegex(RuntimeError, "Retour CAN perdu"):
                motor.control_tick(13.0)
            self.assertEqual(len(motor.bus.batches), 402)


if __name__ == "__main__":
    unittest.main()
