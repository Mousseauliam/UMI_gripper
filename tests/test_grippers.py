import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from backend.gripper import Motor


class Bus:
    def __init__(self):
        self.present = {18, 28}
        self.writes = []
        self.stopped = []
        self.fail = None
        self.status = {}
        self.faults = {}
        self.parameters = {}
        self.batches = []

    def ping(self, mid, timeout=None):
        return str(mid) if mid in self.present else None

    def send_mit_batch(self, commands):
        self.batches.append(commands)

    def drain(self):
        pass

    def read(self, mid, index):
        return {18: 2.0, 28: 6.0}[mid] if index == 0x7019 else 53.0

    def write(self, mid, index, value, *args, **kwargs):
        self.writes.append((mid, index, value))

    def request(self, kind, mid, **kwargs):
        if mid == self.fail:
            raise RuntimeError("CAN failure")

    def stop(self, mid):
        self.stopped.append(mid)


class Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "settings.json"
        self.path.write_text(
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
        self.record = SimpleNamespace(motor_command=lambda *a: None, motor_state=lambda *a: None)
        self.motor = Motor(self.path, self.record)
        self.motor.bus = Bus()

    def tearDown(self):
        self.temp.cleanup()

    def test_calibration_survives_selection_and_restart(self):
        m = self.motor
        m.action("select", {"id": 18})
        m.action("closed", {})
        m.action("select", {"id": 28})
        self.assertEqual(m.state()["config"]["closed"], 4.0)
        m.action("select", {"id": 18})
        self.assertEqual(m.state()["config"]["closed"], 2.0)
        restored = Motor(self.path, self.record)
        self.assertEqual(restored.axis(18).config["closed"], 2.0)
        self.assertEqual(restored.axis(28).config["closed"], 4.0)

    def test_triggers_are_independent_of_selection(self):
        m = self.motor
        m.on(True)
        m.trigger({"left": 0, "right": 0})
        m.trigger({"left": 1, "right": 0.25})
        self.assertEqual(m.axis(18).target, 1.0)
        self.assertEqual(m.axis(28).target, 5.5)
        m.trigger({"left": 0.2, "right": 1})
        self.assertEqual(m.axis(18).target, 1.8)
        self.assertEqual(m.axis(28).target, 4.0)

    def test_absent_side_never_routes_to_present_motor(self):
        m = self.motor
        m.bus.present = {28}
        with self.assertRaisesRegex(ValueError, "absent"):
            m.on(True)
        self.assertFalse(m.active)
        self.assertEqual(m.bus.batches, [])

    def test_vr_accepts_trigger_when_open_angle_is_not_reached(self):
        from unittest.mock import patch

        m = self.motor
        with patch("backend.gripper.time.monotonic", return_value=10.0):
            m.on(True)
        for mid in (18, 28):
            a = m.axis(mid)
            m.bus.status[mid] = dict(
                position=a.config["opened"] - 0.15,
                velocity=0.0,
                torque=0.3,
                temperature=30.0,
                faults=0,
                mode=2,
                seen=30.0,
                stamp_ns=30_000_000_000,
                sequence=1,
            )
            with patch("backend.gripper.time.monotonic", return_value=30.0):
                a.trigger(0)
            a.feedback(30.0)
            self.assertFalse(a.opening)
        with patch("backend.gripper.time.monotonic", return_value=30.01):
            m.trigger({"left": 1.0, "right": 0.0})
        self.assertEqual(m.axis(18).target, 1.0)
        self.assertEqual(m.axis(28).target, 6.0)

    def test_vr_requires_each_trigger_to_be_released_once(self):
        m = self.motor
        m.on(True)
        m.trigger({"left": 1.0, "right": 1.0})
        self.assertEqual(m.axis(18).target, 2.0)
        self.assertEqual(m.axis(28).target, 6.0)
        m.trigger({"left": 0.0, "right": 1.0})
        m.trigger({"left": 1.0, "right": 1.0})
        self.assertEqual(m.axis(18).target, 1.0)
        self.assertEqual(m.axis(28).target, 6.0)
        self.assertTrue(m.axis(28).opening)

    def test_controller_loss_stops_both(self):
        m = self.motor
        m.on(True)
        with self.assertRaises((TypeError, ValueError)):
            m.trigger({"right": 0})
        self.assertFalse(m.active)
        self.assertEqual(set(m.bus.stopped), {18, 28})

    def test_second_motor_failure_stops_both(self):
        m = self.motor
        m.bus.fail = 28
        with self.assertRaises(RuntimeError):
            m.on(True)
        self.assertFalse(m.active)
        self.assertEqual(set(m.bus.stopped), {18, 28})

    def test_missing_calibration_does_not_enable_any_motor(self):
        m = self.motor
        m.config["grippers"]["18"] = {}
        with self.assertRaises(ValueError):
            m.on(True)
        self.assertFalse(m.active)
        self.assertEqual(m.bus.writes, [])


if __name__ == "__main__":
    unittest.main()
