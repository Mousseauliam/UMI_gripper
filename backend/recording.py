import collections
import datetime
import json
import math
from pathlib import Path
import queue
import socket
import struct
import threading
import time
from mcap.writer import Writer, CompressionType


def utc():
    return datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")


class Recorder:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(exist_ok=True)
        self.schemas = json.loads(Path(__file__).with_name("schemas.json").read_text())
        self.lock = threading.RLock()
        self.active = False
        self.last = {}
        self.error = ""
        self.channels = {}
        self.counts = collections.Counter()
        self.sequences = collections.Counter()
        self.video_ready = set()
        self.stream_stats = {}
        self.motor_values = {}
        self.motor_targets = {}
        self.jobs = queue.Queue(maxsize=512)
        self.worker = threading.Thread(target=self._write, daemon=True)
        self.worker.start()

    def start(self, metadata):
        with self.lock:
            if self.active:
                return self.state()
            self.error = ""
            self.counts.clear()
            self.sequences.clear()
            self.video_ready.clear()
            self.stream_stats.clear()
            stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
            self.episode = stamp + "_umi"
            self.started = time.monotonic_ns()
            self.anchor = time.time_ns() - self.started
            self.file = (self.root / (self.episode + ".mcap")).open("xb")
            self.writer = Writer(self.file, compression=CompressionType.ZSTD)
            self.writer.start(profile="origin", library="umi-gripper/1")
            self.channels = {}
            for topic in [
                "/gripper/state",
                "/gripper/record",
                "/cam/head_stereo/left",
                "/cam/head_stereo/right",
                "/cam/wrist_left",
                "/cam/wrist_right",
                "/origin/episode",
            ]:
                s = self.schemas[topic]
                sid = self.writer.register_schema(s["name"], s["encoding"], s["schema"].encode())
                self.channels[topic] = self.writer.register_channel(
                    topic, s["message_encoding"], sid
                )
            s = self.schemas["/cam/wrist_left"]
            sid = self.writer.register_schema(s["name"], s["encoding"], s["schema"].encode())
            self.channels["/cam/quest"] = self.writer.register_channel(
                "/cam/quest", "origin/wire", sid
            )
            for topic in ["/quest/poses", "/gripper/telemetry", "/umi/events"]:
                sid = self.writer.register_schema(
                    "umi." + topic.strip("/").replace("/", "."),
                    "jsonschema",
                    json.dumps(
                        {
                            "type": "object",
                            "description": "SI units; position metres, quaternion xyzw; pose reference local-floor or local; timestamps monotonic nanoseconds on Pi",
                        }
                    ).encode(),
                )
                self.channels[topic] = self.writer.register_channel(topic, "json", sid)
            self.meta = {
                k: (json.dumps(v) if isinstance(v, (dict, list)) else str(v))
                for k, v in metadata.items()
            }
            self.meta.update(
                episode=self.episode,
                index="1",
                robot="umi_gripper_1",
                mode="episodes",
                started_wall=utc(),
                start_mono_ns=str(self.started),
            )
            self.writer.add_metadata(
                "origin/session_anchor",
                dict(
                    hostname=socket.gethostname(),
                    mono_ns=str(self.started),
                    real_ns=str(self.started + self.anchor),
                ),
            )
            self.writer.add_metadata("origin/episode", self.meta)
            self.writer.add_metadata(
                "origin/provenance",
                {
                    "robot": "umi_gripper_1",
                    "software": "umi-gripper/1",
                    "camera_timestamps": "Pi host capture/arrival, not hardware exposure timestamps",
                    "quest_pose": "WebXR head and controller grip poses; no inferred elbow/arm skeleton",
                },
            )
            self.active = True
            self.put("/origin/episode", struct.pack("<QBI", self.started, 1, 1), self.started)
            self._sidecar({"recording": True})
            return self.state()

    def _sidecar(self, extra):
        value = dict(self.meta, **extra)
        path = self.root / (self.episode + ".json")
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(value, indent=2))
        temp.replace(path)

    def put(self, topic, payload, stamp=None):
        with self.lock:
            if not self.active:
                return
            stamp = stamp or time.monotonic_ns()
            if stamp < self.started:
                return
            self.sequences[topic] += 1
            try:
                self.jobs.put_nowait((self.channels[topic], stamp, payload, self.sequences[topic]))
                self.counts[topic] += 1
                if topic.startswith("/cam/") or topic == "/quest/poses":
                    stat = self.stream_stats.setdefault(
                        topic, dict(first_ns=stamp, last_ns=stamp, max_gap_ns=0, count=0)
                    )
                    stat["max_gap_ns"] = max(stat["max_gap_ns"], stamp - stat["last_ns"])
                    stat["last_ns"] = stamp
                    stat["count"] += 1
            except queue.Full:
                self.error = "Recording too slow: data lost"

    def json(self, topic, data, stamp=None):
        self.put(topic, json.dumps(data, allow_nan=False, separators=(",", ":")).encode(), stamp)

    def _write(self):
        while True:
            channel, stamp, payload, seq = self.jobs.get()
            try:
                self.writer.add_message(channel, stamp, payload, time.monotonic_ns(), seq)
            except Exception as exc:
                self.error = str(exc)
            finally:
                self.jobs.task_done()

    def video(self, topic, sequence, stamp, width, height, data, flags):
        with self.lock:
            if not self.active or stamp < self.started:
                return
            if topic not in self.video_ready:
                if flags & 3 != 3:
                    return
                self.video_ready.add(topic)
            self.put(
                topic,
                struct.pack(
                    "<QQII4sIII", sequence, stamp, width, height, b"H264", len(data), flags, 0
                )
                + data,
                stamp,
            )

    def wire(self, topic, payload, mode, stamp=None):
        stamp = stamp or time.monotonic_ns()
        seq = self.sequences[topic] + 1
        header = struct.pack(
            "<IHHQQHBBI", 0x4F52494E, 1, 5, seq, stamp, len(payload), mode, 0, 0x300
        )
        self.put(topic, header + payload, stamp)

    def motor_state(self, c, telemetry):
        with self.lock:
            side = "left" if c["selected"] == 18 else "right"
            opened, closed = c.get("opened"), c.get("closed")
            valid = opened is not None and closed is not None and abs(closed - opened) > 0.001
            value = (telemetry["position"] - opened) / (closed - opened) if valid else math.nan
            stamp = telemetry["stamp_ns"]
            self.motor_values[side] = (value, valid, stamp)
            values, flags = [], []
            for name in ("left", "right"):
                v, ok, t = self.motor_values.get(name, (math.nan, False, 0))
                ok = ok and 0 <= stamp - t < 500_000_000
                values.append(v if ok else math.nan)
                flags.append(int(ok))
            self.wire("/gripper/state", struct.pack("<ddBB", *values, *flags), 0, stamp)
            self.json(
                "/gripper/telemetry", dict(telemetry, motor_id=c["selected"], side=side), stamp
            )

    def motor_command(self, c, target, stamp=None):
        with self.lock:
            side = "left" if c["selected"] == 18 else "right"
            norm = (target - c["opened"]) / (c["closed"] - c["opened"])
            self.motor_targets[side] = (norm, target)
            left = self.motor_targets.get("left", (math.nan, math.nan))
            right = self.motor_targets.get("right", (math.nan, math.nan))
            self.wire(
                "/gripper/record",
                struct.pack("<dddd", left[0], right[0], left[1], right[1]),
                8,
                stamp,
            )

    def stop(self, outcome="not judged", note="", source="web"):
        with self.lock:
            if not self.active:
                return self.state()
            if outcome not in ("success", "fail", "not judged", "discard"):
                raise ValueError("Invalid outcome")
            end = time.monotonic_ns()
            self.put("/origin/episode", struct.pack("<QBI", end, 0, 1), end)
            self.active = False
            self.jobs.join()
            missing = [
                t
                for t in self.channels
                if (t.startswith("/cam/") or t == "/quest/poses") and not self.counts[t]
            ]
            discontinuous = []
            for topic, stat in self.stream_stats.items():
                if (
                    max(stat["first_ns"] - self.started, end - stat["last_ns"], stat["max_gap_ns"])
                    > 1_000_000_000
                ):
                    discontinuous.append(topic)
            metadata = dict(
                episode=self.episode,
                complete=str(not bool(self.error)).lower(),
                kept=str(outcome != "discard").lower(),
                outcome=outcome,
                outcome_source=source,
                note=note,
                duration_s=str((end - self.started) / 1e9),
                end_mono_ns=str(end),
                finished_wall=utc(),
                missing_streams=json.dumps(missing),
                discontinuous_streams=json.dumps(discontinuous),
                stream_stats=json.dumps(self.stream_stats),
                data_complete=str(not missing and not discontinuous and not self.error).lower(),
                error=self.error,
            )
            metadata.update({"count/" + k.lstrip("/"): str(v) for k, v in self.counts.items()})
            self.writer.add_metadata("origin/episode_end", metadata)
            self.writer.finish()
            self.file.close()
            self.last = dict(metadata, recording=False)
            self._sidecar(self.last)
            if outcome == "discard":
                self.discard(self.episode)
            return self.state()

    def state(self):
        with self.lock:
            return dict(
                recording=self.active,
                episode=getattr(self, "episode", ""),
                duration_s=(time.monotonic_ns() - self.started) / 1e9 if self.active else 0,
                counts=dict(self.counts),
                last=self.last,
                error=self.error,
            )

    def episodes(self):
        rows = []
        for p in sorted(self.root.glob("*.json"), reverse=True):
            d = json.loads(p.read_text())
            f = p.with_suffix(".mcap")
            d.update(id=p.stem, bytes=f.stat().st_size if f.exists() else 0)
            rows.append(d)
        return rows

    def discard(self, episode):
        if Path(episode).name != episode:
            raise ValueError("Invalid episode")
        if self.active and episode == self.episode:
            raise ValueError("Stop recording first")
        trash = self.root / "trash"
        trash.mkdir(exist_ok=True)
        for ext in (".mcap", ".json"):
            p = self.root / (episode + ext)
            if p.exists():
                p.rename(trash / p.name)
