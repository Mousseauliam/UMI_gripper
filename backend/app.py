import asyncio
import io
import json
from pathlib import Path
import struct
import math
import time
from aiohttp import web, WSMsgType
from PIL import Image
from .cameras import Cameras
from .gripper import Motor
from .recording import Recorder
from .export import Converter

from .utils import DATA_DIR, prepare_data
from .utils import load_json, save_json


class App:
    def __init__(self):
        prepare_data()
        self.recorder = Recorder(DATA_DIR / "episodes")
        self.converter = Converter(DATA_DIR / "datasets", self.recorder)
        self.motor = Motor(DATA_DIR / "settings.json", self.recorder)
        self.cameras = Cameras(self.recorder)
        self.tasks = load_json(DATA_DIR / "tasks.json")
        self.setup = load_json(DATA_DIR / "setup.json")
        self.vr_owner = None
        self.session = str(int(time.time()))

    def persist(self):
        for name, data in [("tasks", self.tasks), ("setup", self.setup)]:
            save_json(DATA_DIR / (name + ".json"), data)

    def state(self):
        return dict(
            motor=self.motor.state(),
            recording=self.recorder.state(),
            cameras={k: c.state() for k, c in self.cameras.items.items()},
        )

    def metadata(self):
        task = self.tasks.get("selected", "")
        d = next((t for t in self.tasks["tasks"] if t["task"] == task), {})
        return dict(
            task=task,
            operator=self.tasks.get("operator", ""),
            location=self.tasks.get("location", ""),
            group=d.get("group", ""),
            category=d.get("category", ""),
            setup=self.setup["setup"],
            session=self.session,
            gripper_config=self.motor.config,
            motor_control=dict(mode="MIT", kp=2.04, kd=0.178571, target_hz=200),
            available_streams={k: c.state()["live"] for k, c in self.cameras.items.items()},
        )

    async def get(self, r):
        path = r.match_info["path"]
        if path == "state":
            return web.json_response(self.state())
        if path == "tasks":
            return web.json_response(self.tasks)
        if path == "setup":
            return web.json_response(self.setup)
        if path == "episodes":
            return web.json_response(self.recorder.episodes())
        if path == "datasets":
            return web.json_response(
                {"datasets": self.converter.datasets(), "job": self.converter.job}
            )
        raise web.HTTPNotFound()

    async def action(self, r):
        origin = r.headers.get("Origin")
        if origin and origin != str(r.url.origin()):
            raise web.HTTPForbidden()
        d = await r.json() if r.can_read_body else {}
        path = r.match_info["path"]
        try:
            if path.startswith("motor/"):
                action = path.split("/")[1]
                if action == "scan":
                    if self.motor.active or self.motor.scanning:
                        raise ValueError("Motor OFF requis")
                    asyncio.create_task(asyncio.to_thread(self.motor.scan))
                else:
                    await asyncio.to_thread(self.motor.action, action, d)
            elif path == "record/start":
                await asyncio.to_thread(self.recorder.start, self.metadata())
            elif path == "record/stop":
                await asyncio.to_thread(
                    self.recorder.stop, d.get("outcome", "not judged"), d.get("note", "")
                )
            elif path == "record/discard":
                await asyncio.to_thread(self.recorder.stop, "discard")
            elif path == "episode/discard":
                await asyncio.to_thread(self.recorder.discard, d["id"])
            elif path == "convert":
                if self.recorder.active:
                    raise ValueError("Terminer l’enregistrement avant la conversion")
                self.converter.start(d)
            elif path == "tasks/add":
                task = " ".join(d["task"].split())[:300]
                if not task:
                    raise ValueError("Task vide")
                self.tasks["tasks"].append(
                    dict(task=task, category=d["category"], group=d.get("group", ""))
                )
                self.tasks["selected"] = task
                self.persist()
            elif path == "tasks/select":
                self.tasks["selected"] = d["task"]
                self.persist()
            elif path == "session/set":
                self.tasks.update({k: str(d.get(k, ""))[:80] for k in ("operator", "location")})
                self.persist()
            elif path == "setup/set":
                self.setup["setup"] = {k: d.get(k, []) for k in ("tools", "objects", "quality")}
                self.persist()
            else:
                raise ValueError("Commande inconnue")
            return web.json_response(self.state())
        except Exception as exc:
            return web.json_response({"error": str(exc)}, status=400)

    async def stream(self, r):
        camera = self.cameras.items.get(r.match_info["name"])
        if not camera:
            raise web.HTTPNotFound()
        response = web.StreamResponse(
            headers={
                "Content-Type": "multipart/x-mixed-replace; boundary=frame",
                "Cache-Control": "no-store",
            }
        )
        await response.prepare(r)
        sequence = -1
        try:
            while True:
                if camera.frame is not None and sequence != camera.sequence:
                    sequence = camera.sequence
                    data = camera.frame
                    await response.write(
                        b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                        + str(len(data)).encode()
                        + b"\r\n\r\n"
                        + data
                        + b"\r\n"
                    )
                await asyncio.sleep(0.025)
        except (ConnectionError, asyncio.CancelledError):
            pass
        return response

    async def websocket(self, r):
        origin = r.headers.get("Origin")
        if origin and origin != str(r.url.origin()):
            raise web.HTTPForbidden()
        ws = web.WebSocketResponse(heartbeat=2, max_msg_size=65536)
        await ws.prepare(r)

        async def updates():
            while not ws.closed:
                snapshot = await asyncio.to_thread(self.state)
                await ws.send_json({"type": "state", **snapshot})
                await asyncio.sleep(0.1)

        task = asyncio.create_task(updates())
        offset = None
        try:
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                d = json.loads(msg.data)
                try:
                    kind = d["type"]
                    if kind == "diagnostic":
                        print("Quest:", str(d.get("message", ""))[:500], flush=True)
                    elif kind == "tracking_reset":
                        print("Quest: tracking origin reset; session kept open", flush=True)
                        if self.vr_owner is ws:
                            self.recorder.json("/umi/events", dict(d, stamp_ns=time.monotonic_ns()))
                    elif kind == "clock":
                        await ws.send_json(
                            {
                                "type": "clock",
                                "client": d["client"],
                                "server": time.monotonic_ns() / 1e6,
                            }
                        )
                    elif kind == "clock_offset":
                        offset = float(d["offset"])
                    elif kind == "vr_start":
                        if self.vr_owner is not None and self.vr_owner is not ws:
                            raise ValueError("Autre session VR active")
                        self.vr_owner = ws
                        await asyncio.to_thread(self.motor.on, True)
                        await ws.send_json({"type": "vr_ready"})
                    elif kind == "vr_stop" and self.vr_owner is ws:
                        await asyncio.to_thread(self.motor.stop)
                        self.vr_owner = None
                    elif kind == "sample" and self.vr_owner is ws:
                        received = time.monotonic_ns()
                        await asyncio.to_thread(self.motor.trigger, d["triggers"])
                        d["control_received_ns"] = received
                        d["control_target_updated_ns"] = time.monotonic_ns()
                        stamp = (
                            round((float(d["time"]) + offset) * 1e6)
                            if offset is not None
                            else time.monotonic_ns()
                        )
                        if abs(stamp - time.monotonic_ns()) > 2_000_000_000:
                            stamp = time.monotonic_ns()
                        d["stamp_ns"] = stamp
                        self.recorder.json("/quest/poses", d, stamp)
                    elif kind == "record" and self.vr_owner is ws:
                        if d["action"] == "start":
                            await asyncio.to_thread(self.recorder.start, self.metadata())
                        else:
                            await asyncio.to_thread(
                                self.recorder.stop, d.get("outcome", "not judged"), "", "quest"
                            )
                    await ws.send_json({"type": "ack", "seq": d.get("seq")})
                except Exception as exc:
                    print("VR command error:", str(exc), flush=True)
                    await ws.send_json({"type": "error", "error": str(exc)})
        finally:
            task.cancel()
            if self.vr_owner is ws:
                await asyncio.to_thread(self.motor.stop)
                self.vr_owner = None
                if self.recorder.active:
                    await asyncio.to_thread(
                        self.recorder.stop, "not judged", "VR disconnected", "system"
                    )
        return ws

    async def camera_socket(self, r):
        origin = r.headers.get("Origin")
        if origin and origin != str(r.url.origin()):
            raise web.HTTPForbidden()
        ws = web.WebSocketResponse(max_msg_size=1_500_000)
        await ws.prepare(r)
        async for msg in ws:
            if msg.type != WSMsgType.BINARY:
                continue
            try:
                if len(msg.data) < 17:
                    raise ValueError("Frame invalide")
                milliseconds, sequence = struct.unpack_from("<dQ", msg.data)
                if not math.isfinite(milliseconds):
                    raise ValueError("Horodatage invalide")
                stamp = round(milliseconds * 1e6)
                received = time.monotonic_ns()
                if not -100_000_000 <= received - stamp <= 2_000_000_000:
                    raise ValueError("Horloge caméra non synchronisée ou frame trop ancienne")
                image = msg.data[16:]
                with Image.open(io.BytesIO(image)) as im:
                    if im.format != "JPEG" or max(im.size) > 1920:
                        raise ValueError("JPEG invalide")
                    im.verify()
                await asyncio.to_thread(self.cameras.items["quest"].publish, image, stamp)
                self.recorder.json(
                    "/umi/events",
                    dict(
                        type="quest_frame",
                        sequence=sequence,
                        stamp_ns=stamp,
                        received_ns=received,
                        transport_ms=(received - stamp) / 1e6,
                    ),
                    stamp,
                )
                await ws.send_json(
                    {"ok": True, "sequence": sequence, "transport_ms": (received - stamp) / 1e6}
                )
            except Exception as exc:
                await ws.send_json({"error": str(exc)})
        return ws

    async def download(self, r):
        name = r.match_info["name"]
        if Path(name).name != name or not name.endswith(".mcap"):
            raise web.HTTPNotFound()
        if self.recorder.active and name == self.recorder.episode + ".mcap":
            raise web.HTTPConflict(text="Recording active")
        return web.FileResponse(
            self.recorder.root / name,
            headers={"Content-Disposition": f'attachment; filename="{name}"'},
        )
