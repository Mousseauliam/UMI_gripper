import glob
import io
import os
import queue
import re
import subprocess
import threading
import time
from PIL import Image

TOPICS = {
    "torso_left": "/cam/head_stereo/left",
    "torso_right": "/cam/head_stereo/right",
    "wrist_left": "/cam/wrist_left",
    "wrist_right": "/cam/wrist_right",
    "quest": "/cam/quest",
}


class Encoder:
    def __init__(self, camera, recorder):
        self.camera, self.recorder = camera, recorder
        self.queue = queue.Queue(maxsize=2)
        self.running = True
        self.codec = None
        self.episode = None
        threading.Thread(target=self.feed, daemon=True).start()

    def submit(self, image, stamp):
        try:
            self.queue.put_nowait((image, stamp))
        except queue.Full:
            self.camera.dropped += 1

    def feed(self):
        import av
        from fractions import Fraction

        sequence = 0
        while self.running:
            item = self.queue.get()
            if item is None:
                break
            image, stamp = item
            if not self.recorder.active or stamp < self.recorder.started:
                continue
            try:
                with Image.open(io.BytesIO(image)) as im:
                    frame = av.VideoFrame.from_image(im.convert("RGB"))
                if self.codec is None:
                    self.codec = av.CodecContext.create("libx264", "w")
                    self.codec.width, self.codec.height = frame.width, frame.height
                    self.codec.pix_fmt = "yuv420p"
                    self.codec.time_base = Fraction(1, 1_000_000_000)
                    self.codec.framerate = Fraction(20, 1)
                    self.codec.thread_count = 1
                    self.codec.options = {
                        "preset": "ultrafast",
                        "tune": "zerolatency",
                        "x264-params": "aud=1:repeat-headers=1:scenecut=0:keyint=20:bframes=0",
                    }
                    self.codec.open()
                frame.pts = stamp
                frame.time_base = self.codec.time_base
                episode = (
                    getattr(self.recorder, "episode", None)
                    if self.recorder.active and stamp >= self.recorder.started
                    else None
                )
                if episode and episode != self.episode:
                    frame.pict_type = av.video.frame.PictureType.I
                self.episode = episode
                sequence += 1
                packets = self.codec.encode(frame)
                if len(packets) != 1:
                    raise RuntimeError("Encodeur non immédiat : une frame doit produire un paquet")
                packet = packets[0]
                data = bytes(packet)
                types = [m.group(1)[0] & 31 for m in re.finditer(b"\x00\x00\x01(.)", data, re.S)]
                flags = int(5 in types) | (2 if 7 in types and 8 in types else 0)
                encoded_stamp = round(packet.pts * packet.time_base * 1_000_000_000)
                if encoded_stamp != stamp:
                    raise RuntimeError("Horodatage encodeur différent de la frame")
                self.recorder.video(
                    TOPICS[self.camera.name],
                    sequence,
                    stamp,
                    frame.width,
                    frame.height,
                    data,
                    flags,
                )
                self.camera.encode_ms = (time.monotonic_ns() - stamp) / 1e6
                self.camera.encoded += 1
                self.camera.encoder_error = ""
            except Exception as exc:
                self.camera.encoder_error = str(exc)

    def close(self):
        self.running = False
        try:
            self.queue.put_nowait(None)
        except queue.Full:
            pass


class Camera:
    def __init__(self, name, recorder):
        self.name, self.recorder = name, recorder
        self.frame = None
        self.sequence = 0
        self.updated = 0
        self.width = self.height = 0
        self.encoder = None
        self.error = "Caméra absente"
        self.dropped = 0
        self.encoded = 0
        self.encode_ms = 0
        self.encoder_error = ""
        self.condition = threading.Condition()

    def publish(self, frame, stamp=None):
        stamp = stamp or time.monotonic_ns()
        with self.condition:
            if not self.width:
                with Image.open(io.BytesIO(frame)) as im:
                    self.width, self.height = im.size
            self.frame = frame
            self.sequence += 1
            self.updated = time.monotonic()
            self.error = ""
            self.condition.notify_all()
            if self.recorder.active:
                if not self.encoder:
                    self.encoder = Encoder(self, self.recorder)
                self.encoder.submit(frame, stamp)

    def state(self):
        return dict(
            live=time.monotonic() - self.updated < 3,
            error=self.error,
            frames=self.sequence,
            width=self.width,
            height=self.height,
            dropped=self.dropped,
            encoded=self.encoded,
            encode_ms=self.encode_ms,
            encoder_error=self.encoder_error,
        )


class Cameras:
    def __init__(self, recorder):
        self.items = {k: Camera(k, recorder) for k in TOPICS}
        self.processes = []
        self.running = True

    def start(self):
        threading.Thread(target=self.capture_csi, daemon=True).start()
        threading.Thread(target=self.usb_loop, daemon=True).start()

    def jpeg_stream(self, command, callback):
        p = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self.processes.append(p)
        buffer = b""
        try:
            while self.running:
                data = p.stdout.read1(65536)
                if not data:
                    break
                buffer += data
                while True:
                    start = buffer.find(b"\xff\xd8")
                    end = buffer.find(b"\xff\xd9", max(0, start) + 2)
                    if start < 0 or end < 0:
                        break
                    frame, buffer = buffer[start : end + 2], buffer[end + 2 :]
                    callback(frame)
                if len(buffer) > 8_000_000:
                    raise RuntimeError("Image invalide")
        finally:
            if p.poll() is None:
                p.terminate()
            p.wait()
            self.processes.remove(p)

    def capture_csi(self):
        while self.running:
            try:
                self.jpeg_stream(
                    [
                        "rpicam-vid",
                        "-n",
                        "-t",
                        "0",
                        "--codec",
                        "mjpeg",
                        "--width",
                        "1280",
                        "--height",
                        "400",
                        "--framerate",
                        "20",
                        "-o",
                        "-",
                    ],
                    self.split,
                )
            except Exception as exc:
                for name in ("torso_left", "torso_right"):
                    self.items[name].error = str(exc)
            time.sleep(2)

    def split(self, frame):
        stamp = time.monotonic_ns()
        with Image.open(io.BytesIO(frame)) as im:
            w, h = im.size
            for name, x in [("torso_right", 0), ("torso_left", w // 2)]:
                out = io.BytesIO()
                im.crop((x, 0, x + w // 2, h)).save(out, "JPEG", quality=85)
                self.items[name].publish(out.getvalue(), stamp)

    def usb_loop(self):
        workers = {}
        while self.running:
            candidates = sorted(glob.glob("/dev/v4l/by-path/*usb*-video-index0"))
            paths = []
            seen = set()
            for path in candidates:
                device = os.path.realpath(path)
                if device not in seen:
                    paths.append(path)
                    seen.add(device)
            for name, path in zip(("wrist_right", "wrist_left"), paths):
                if name not in workers or not workers[name].is_alive():
                    worker = threading.Thread(
                        target=self.capture_usb, args=(name, path), daemon=True
                    )
                    workers[name] = worker
                    worker.start()
            time.sleep(3)

    def capture_usb(self, name, path):
        try:
            self.jpeg_stream(
                [
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-f",
                    "v4l2",
                    "-input_format",
                    "mjpeg",
                    "-framerate",
                    "30",
                    "-video_size",
                    "640x480",
                    "-i",
                    path,
                    "-an",
                    "-c:v",
                    "copy",
                    "-f",
                    "image2pipe",
                    "pipe:1",
                ],
                self.items[name].publish,
            )
        except Exception as exc:
            self.items[name].error = str(exc)

    def close(self):
        self.running = False
        for p in list(self.processes):
            if p.poll() is None:
                p.terminate()
        for c in self.items.values():
            if c.encoder:
                c.encoder.close()
