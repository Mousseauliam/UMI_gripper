import json
import math
from pathlib import Path
import struct
import tempfile
import time
import unittest
from mcap.reader import make_reader
from backend.recording import Recorder
from backend.gripper import Axis
import threading


class Bus:
    def __init__(self):
        self.operations = []
        self.status = {}
        self.faults = {}
        self.parameters = {}
        self.batches = []

    def send_mit_batch(self, commands):
        self.batches.append(commands)

    def drain(self):
        pass

    def read(self, mid, index):
        return 2.0 if index == 0x7019 else 54.0

    def write(self, *a, **k):
        self.operations.append(("write", a, k))

    def request(self, *a, **k):
        self.operations.append(("request", a, k))

    def stop(self, mid):
        self.operations.append(("stop", mid))

    def close(self):
        pass


class Tests(unittest.TestCase):
    def test_two_grippers_keep_separate_mcap_slots(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = Recorder(tmp)
            r.start({"task": "two grippers"})
            left = dict(selected=18, opened=2.0, closed=1.0)
            right = dict(selected=28, opened=6.0, closed=4.0)
            r.motor_state(left, dict(position=1.5, voltage=53.0, stamp_ns=time.monotonic_ns()))
            r.motor_state(right, dict(position=4.0, voltage=53.0, stamp_ns=time.monotonic_ns()))
            r.motor_command(left, 1.5)
            r.motor_command(right, 4.0)
            r.stop()
            with next(Path(tmp).glob("*.mcap")).open("rb") as f:
                messages = list(make_reader(f).iter_messages())
            states = [m for _, c, m in messages if c.topic == "/gripper/state"]
            commands = [m for _, c, m in messages if c.topic == "/gripper/record"]
            self.assertEqual(struct.unpack_from("<ddBB", states[-1].data, 32), (0.5, 1.0, 1, 1))
            self.assertEqual(
                struct.unpack_from("<dddd", commands[-1].data, 32), (0.5, 1.0, 1.5, 4.0)
            )
            telemetry = [
                json.loads(m.data) for _, c, m in messages if c.topic == "/gripper/telemetry"
            ]
            self.assertEqual(
                [(d["side"], d["motor_id"]) for d in telemetry], [("left", 18), ("right", 28)]
            )

    def test_preview_does_not_encode_until_recording(self):
        import io
        from PIL import Image
        from backend.cameras import Camera

        with tempfile.TemporaryDirectory() as tmp:
            r = Recorder(tmp)
            camera = Camera("quest", r)
            image = io.BytesIO()
            Image.new("RGB", (64, 48), "red").save(image, "JPEG")
            camera.publish(image.getvalue())
            self.assertIsNone(camera.encoder)
            self.assertEqual(camera.sequence, 1)
            self.assertTrue(camera.frame)

    def test_single_camera_frame_is_immediate(self):
        import io
        from PIL import Image
        from backend.cameras import Camera

        with tempfile.TemporaryDirectory() as tmp:
            r = Recorder(tmp)
            r.start({"task": "single frame regression"})
            camera = Camera("quest", r)
            image = io.BytesIO()
            Image.new("RGB", (64, 48), "red").save(image, "JPEG")
            stamp = time.monotonic_ns()
            camera.publish(image.getvalue(), stamp)
            deadline = time.monotonic() + 2
            while not camera.encoded and not camera.encoder_error and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(camera.encoder_error, "")
            self.assertEqual(r.counts["/cam/quest"], 1)
            r.stop()
            camera.encoder.close()
            with next(Path(tmp).glob("*.mcap")).open("rb") as f:
                _, _, m = next(make_reader(f).iter_messages(topics=["/cam/quest"]))
                self.assertEqual(m.log_time, stamp)
                self.assertLess(m.publish_time - stamp, 2_000_000_000)

    def test_old_frames_and_sparse_streams(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = Recorder(tmp)
            r.start({"task": "timing regression"})
            r.video("/cam/quest", 1, r.started - 1, 64, 48, b"IDR", 3)
            self.assertEqual(r.counts["/cam/quest"], 0)
            self.assertNotIn("/cam/quest", r.video_ready)
            r.started -= 3_000_000_000
            r.video("/cam/quest", 2, r.started, 64, 48, b"IDR", 3)
            r.stop()
            self.assertIn("/cam/quest", json.loads(r.last["discontinuous_streams"]))
            self.assertEqual(r.last["data_complete"], "false")

    def test_record_origin(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = Recorder(tmp)
            r.start({"task": "test", "operator": "test"})
            c = dict(selected=127, opened=0.4, closed=-1.8)
            r.motor_state(c, dict(position=0.4, voltage=54.0, stamp_ns=time.monotonic_ns()))
            r.motor_command(c, -1.8)
            r.video("/cam/wrist_left", 1, time.monotonic_ns(), 640, 480, b"bad", 0)
            r.video("/cam/wrist_left", 2, time.monotonic_ns(), 640, 480, b"IDR", 3)
            r.stop("success")
            with next(Path(tmp).glob("*.mcap")).open("rb") as f:
                reader = make_reader(f, validate_crcs=True)
                self.assertEqual(reader.get_header().profile, "origin")
                messages = list(reader.iter_messages())
                state = next(m for _, c, m in messages if c.topic == "/gripper/state")
                self.assertEqual(len(state.data), 50)
                self.assertEqual(state.data[:4], b"NIRO")
                self.assertEqual(struct.unpack_from("<Q", state.data, 16)[0], state.log_time)
                values = struct.unpack_from("<ddBB", state.data, 32)
                self.assertTrue(math.isnan(values[0]))
                self.assertEqual(values[1:], (0, 0, 1))
                videos = [m for _, c, m in messages if c.topic == "/cam/wrist_left"]
                self.assertEqual(len(videos), 1)
                self.assertEqual(videos[0].data[24:28], b"H264")
                self.assertEqual(videos[0].data[40:], b"IDR")
                meta = {m.name: m.metadata for m in reader.iter_metadata()}
                self.assertEqual(meta["origin/episode_end"]["outcome"], "success")

    def test_vr_opens_from_outside_calibrated_range(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "settings.json"
            p.write_text(
                json.dumps(
                    dict(
                        selected=127,
                        opened=0.4,
                        closed=-1.8,
                        current=2.4,
                        speed=50.0,
                        acceleration=10.0,
                    )
                )
            )
            motor = Axis(
                json.loads(p.read_text()),
                Bus(),
                threading.RLock(),
                Recorder(Path(tmp) / "episodes"),
            )
            self.assertFalse(motor.active)
            motor.on(True)
            self.assertEqual(motor.bus.batches[0][0][1], 2.0)
            self.assertEqual(motor.target, 0.4)
            motor.trigger(1)
            self.assertEqual(motor.target, 0.4)
            motor.opening = False
            motor.trigger(1)
            self.assertEqual(motor.target, 0.4)
            motor.trigger(0)
            motor.trigger(1)
            self.assertAlmostEqual(motor.target, -1.8)
            motor.stop()
            self.assertFalse(motor.active)


if __name__ == "__main__":
    unittest.main()
