import bisect
import json
from pathlib import Path
import re
import struct
import subprocess
import tempfile
import threading
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
from mcap.reader import make_reader


def dump(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))


def stats(values):
    a = np.asarray(values, dtype=np.float64)
    if a.ndim == 1:
        a = a[:, None]
    return {
        **{
            k: fn(a, axis=0).tolist()
            for k, fn in [("min", np.min), ("max", np.max), ("mean", np.mean), ("std", np.std)]
        },
        "count": [len(a)],
    }


class Converter:
    def __init__(self, root, recorder):
        self.root = Path(root)
        self.root.mkdir(exist_ok=True)
        self.recorder = recorder
        self.job = {"running": False, "error": ""}

    def datasets(self):
        return [json.loads(p.read_text()) for p in self.root.glob("*/*/umi.json")]

    def start(self, options):
        if self.job["running"]:
            raise ValueError("Conversion déjà en cours")
        repo = options.get("repo", "")
        if not re.fullmatch(r"[A-Za-z0-9_-]+/[A-Za-z0-9_-]+", repo):
            raise ValueError("Dataset: owner/name")
        if options.get("frame") != "raw":
            raise ValueError("UMI utilise les angles moteur bruts ; sélectionner raw motor angles")
        if options.get("format") not in ("lerobot_v3", "gr00t_v2"):
            raise ValueError("Format inconnu")
        selected = [
            e
            for e in self.recorder.episodes()
            if not e.get("recording")
            and e.get("outcome", "not judged") in options["outcomes"]
            and (not options.get("group") or e.get("group") == options["group"])
            and (not options.get("session") or e.get("session") == options["session"])
        ]
        target = self.root / repo
        if target.exists():
            raise ValueError("Choisir un nouveau nom de dataset ; le dataset existant est conservé")
        if not selected:
            raise ValueError("Aucun épisode correspondant")
        self.job = {"running": True, "repo": repo, "done": 0, "total": len(selected), "error": ""}
        threading.Thread(target=self.run, args=(options, selected, target), daemon=True).start()

    def run(self, o, episodes, target):
        staging = target.with_name(target.name + ".building")
        try:
            staging.mkdir(parents=True, exist_ok=False)
            self.build(o, episodes, staging)
            staging.rename(target)
        except Exception as exc:
            self.job["error"] = str(exc)
        finally:
            self.job["running"] = False

    def build(self, o, episodes, root):
        fps = 20
        v3 = o["format"] == "lerobot_v3"
        features = {
            "observation.state": {
                "dtype": "float32",
                "shape": [1],
                "names": ["right_gripper.position_rad"],
            },
            "action": {"dtype": "float32", "shape": [1], "names": ["right_gripper.target_rad"]},
        }
        for key in ["timestamp", "frame_index", "episode_index", "index", "task_index"]:
            features[key] = {
                "dtype": "float32" if key == "timestamp" else "int64",
                "shape": [1],
                "names": None,
            }
        task_names = list(dict.fromkeys(e.get("task", "") for e in episodes))
        total = 0
        episode_meta = []
        all_stats = {}
        camera_set = None
        joint_set = None
        for idx, episode in enumerate(episodes):
            points = {"left": [], "right": []}
            commands = {"left": [], "right": []}
            poses = []
            videos = {}
            with tempfile.TemporaryDirectory(dir=root) as temp:
                temp = Path(temp)
                with (self.recorder.root / (episode["id"] + ".mcap")).open("rb") as f:
                    reader = make_reader(f, validate_crcs=True)
                    for _, channel, msg in reader.iter_messages():
                        if channel.topic == "/gripper/telemetry":
                            d = json.loads(msg.data)
                            points[d.get("side", "right")].append((msg.log_time, d["position"]))
                        elif channel.topic == "/gripper/record":
                            for side, value in zip(
                                ("left", "right"), struct.unpack_from("<dd", msg.data, 48)
                            ):
                                if np.isfinite(value):
                                    commands[side].append((msg.log_time, value))
                        elif channel.topic == "/quest/poses":
                            poses.append((msg.log_time, json.loads(msg.data)))
                        elif channel.topic.startswith("/cam/"):
                            _, stamp, w, h, fourcc, size, flags, _ = struct.unpack_from(
                                "<QQII4sIII", msg.data
                            )
                            if fourcc != b"H264":
                                raise ValueError("Vidéo H264 attendue")
                            name = channel.topic.removeprefix("/cam/").replace("/", "_")
                            if name not in videos:
                                if flags & 3 != 3:
                                    continue
                                videos[name] = {
                                    "file": (temp / (name + ".h264")).open("wb"),
                                    "times": [],
                                    "w": w,
                                    "h": h,
                                }
                            videos[name]["file"].write(msg.data[40:])
                            videos[name]["times"].append(stamp)
                for v in videos.values():
                    v["file"].close()
                for name, v in videos.items():
                    if len(v["times"]) < 2 or max(np.diff(v["times"])) > 250_000_000:
                        raise ValueError(
                            episode["id"]
                            + ": vidéo "
                            + name
                            + " discontinue ; conversion refusée pour ne pas répéter artificiellement une image pendant une coupure"
                        )
                joints = [side for side in ("left", "right") if points[side]]
                if not joints or any(not commands[side] for side in joints):
                    raise ValueError(
                        episode["id"] + ": commande manquante pour un gripper enregistré"
                    )
                if joint_set is None:
                    joint_set = joints
                    for key, field in [("observation.state", "position"), ("action", "target")]:
                        features[key] = {
                            "dtype": "float32",
                            "shape": [len(joints)],
                            "names": [side + "_gripper." + field + "_rad" for side in joints],
                        }
                elif joints != joint_set:
                    raise ValueError("Les épisodes doivent contenir les mêmes grippers")
                if not videos:
                    raise ValueError("Aucune vidéo disponible")
                if camera_set is None:
                    camera_set = set(videos)
                elif set(videos) != camera_set:
                    raise ValueError("Les épisodes doivent contenir les mêmes caméras")
                start = max(
                    *(points[side][0][0] for side in joints),
                    *(commands[side][0][0] for side in joints),
                    *(v["times"][0] for v in videos.values()),
                    *([poses[0][0]] if poses else []),
                )
                end = min(
                    *(points[side][-1][0] for side in joints),
                    *(v["times"][-1] for v in videos.values()),
                    *([poses[-1][0]] if poses else []),
                )
                if end <= start:
                    raise ValueError("Pas de durée commune entre les flux")
                step = 1_000_000_000 // fps
                n = (end - start) // step
                times = start + np.arange(n, dtype=np.int64) * step
                if n < 2:
                    raise ValueError("Épisode trop court")
                position = np.column_stack(
                    [
                        np.interp(times, [p[0] for p in points[side]], [p[1] for p in points[side]])
                        for side in joints
                    ]
                ).astype("float32")
                command_times = {side: [p[0] for p in commands[side]] for side in joints}
                command = np.column_stack(
                    [
                        [
                            commands[side][max(0, bisect.bisect_right(command_times[side], t) - 1)][
                                1
                            ]
                            for t in times
                        ]
                        for side in joints
                    ]
                ).astype("float32")
                data = {
                    "observation.state": position.tolist(),
                    "action": command.tolist(),
                    "timestamp": np.arange(n, dtype="float32") / fps,
                    "frame_index": np.arange(n, dtype="int64"),
                    "episode_index": np.full(n, idx, dtype="int64"),
                    "index": np.arange(total, total + n, dtype="int64"),
                    "task_index": np.full(
                        n, task_names.index(episode.get("task", "")), dtype="int64"
                    ),
                }
                epstats = {k: stats(v) for k, v in data.items()}
                chunk, file = divmod(idx, 1000)
                ep = {
                    "episode_index": idx,
                    "tasks": [episode.get("task", "")],
                    "length": n,
                    "dataset_from_index": total,
                    "dataset_to_index": total + n,
                    "data/chunk_index": chunk,
                    "data/file_index": file,
                    "meta/episodes/chunk_index": 0,
                    "meta/episodes/file_index": 0,
                    "source_episode": episode["id"],
                }
                sync = {
                    "fps": fps,
                    "frame_count": n,
                    "common_start_ns": int(start),
                    "common_end_ns": int(start + n * step),
                    "source_overlap_end_ns": int(end),
                    "timestamp_ns": [int(t) for t in times],
                    "cameras": {},
                }
                for name, v in videos.items():
                    key = "observation.images." + name
                    decode = temp / name
                    decode.mkdir()
                    self.ffmpeg(
                        [
                            "-i",
                            str(temp / (name + ".h264")),
                            "-vsync",
                            "0",
                            "-q:v",
                            "3",
                            str(decode / "%06d.jpg"),
                        ]
                    )
                    images = sorted(decode.glob("*.jpg"))
                    if len(images) != len(v["times"]):
                        raise ValueError("Nombre de frames décodées différent du MCAP")
                    selected = self.nearest(v["times"], times)
                    sync["cameras"][name] = {
                        "source_frame_index": selected,
                        "source_timestamp_ns": [v["times"][i] for i in selected],
                        "offset_ns": [int(v["times"][i] - t) for i, t in zip(selected, times)],
                        "reused_frames": n - len(set(selected)),
                    }
                    rel = (
                        f"videos/{key}/chunk-{chunk:03}/file-{file:03}.mp4"
                        if v3
                        else f"videos/chunk-{chunk:03}/{key}/episode_{idx:06}.mp4"
                    )
                    dest = root / rel
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    self.encode_grid(images, selected, dest, fps)
                    features[key] = {
                        "dtype": "video",
                        "shape": [v["h"], v["w"], 3],
                        "names": ["height", "width", "channels"],
                        "info": {
                            "video.fps": fps,
                            "video.codec": "h264",
                            "video.pix_fmt": "yuv420p",
                            "video.is_depth_map": False,
                            "has_audio": False,
                        },
                    }
                    ep.update(
                        {
                            f"videos/{key}/chunk_index": chunk,
                            f"videos/{key}/file_index": file,
                            f"videos/{key}/from_timestamp": 0.0,
                            f"videos/{key}/to_timestamp": n / fps,
                        }
                    )
                    from PIL import Image

                    sample = np.stack(
                        [
                            np.asarray(Image.open(images[i]).resize((64, 64)), dtype=np.float32)
                            / 255
                            for i in selected[:: max(1, n // 20)]
                        ]
                    )
                    flat = sample.reshape(-1, 3)
                    st = {
                        k: np.asarray(value).reshape(3, 1, 1).tolist()
                        for k, value in stats(flat).items()
                        if k != "count"
                    }
                    st["count"] = [n]
                    epstats[key] = st
                table = pa.table(
                    {
                        k: pa.array(
                            v,
                            type=pa.list_(pa.float32(), len(joints))
                            if k in ("action", "observation.state")
                            else pa.float32()
                            if k == "timestamp"
                            else pa.int64(),
                        )
                        for k, v in data.items()
                    }
                )
                path = root / (
                    f"data/chunk-{chunk:03}/file-{file:03}.parquet"
                    if v3
                    else f"data/chunk-{chunk:03}/episode_{idx:06}.parquet"
                )
                path.parent.mkdir(parents=True, exist_ok=True)
                pq.write_table(table, path)
                for key, st in epstats.items():
                    for name, value in st.items():
                        ep[f"stats/{key}/{name}"] = value
                    if key not in all_stats:
                        all_stats[key] = st
                    else:
                        old = all_stats[key]
                        a, b = old["count"][0], st["count"][0]
                        mean = (np.asarray(old["mean"]) * a + np.asarray(st["mean"]) * b) / (a + b)
                        var = (
                            a
                            * (np.asarray(old["std"]) ** 2 + (np.asarray(old["mean"]) - mean) ** 2)
                            + b
                            * (np.asarray(st["std"]) ** 2 + (np.asarray(st["mean"]) - mean) ** 2)
                        ) / (a + b)
                        all_stats[key] = {
                            "min": np.minimum(old["min"], st["min"]).tolist(),
                            "max": np.maximum(old["max"], st["max"]).tolist(),
                            "mean": mean.tolist(),
                            "std": np.sqrt(var).tolist(),
                            "count": [a + b],
                        }
                episode_meta.append(ep)
                total += n
                self.job["done"] = idx + 1
                dump(root / "sync" / f"episode_{idx:06}.json", sync)
                if poses:
                    path = root / "quest" / f"episode_{idx:06}.jsonl"
                    path.parent.mkdir(exist_ok=True)
                    path.write_text(
                        "\n".join(
                            json.dumps(
                                dict(stamp_ns=t, **{k: v for k, v in d.items() if k != "stamp_ns"})
                            )
                            for t, d in poses
                        )
                    )
                    selected = self.nearest([p[0] for p in poses], times)
                    aligned = path.with_name(f"episode_{idx:06}_aligned.jsonl")
                    aligned.write_text(
                        "\n".join(
                            json.dumps(
                                {
                                    "frame_index": j,
                                    "timestamp": j / fps,
                                    "stamp_ns": int(t),
                                    "source_stamp_ns": poses[i][0],
                                    "pose": poses[i][1],
                                }
                            )
                            for j, (t, i) in enumerate(zip(times, selected))
                        )
                    )
        info = dict(
            codebase_version="v3.0" if v3 else "v2.1",
            robot_type="umi_gripper_1",
            total_episodes=len(episodes),
            total_frames=total,
            total_tasks=len(task_names),
            total_videos=len(episodes) * len(camera_set or []),
            total_chunks=(len(episodes) + 999) // 1000,
            chunks_size=1000,
            data_files_size_in_mb=100,
            video_files_size_in_mb=200,
            fps=fps,
            splits={"train": f"0:{len(episodes)}"},
            features=features,
            data_path="data/chunk-{chunk_index:03d}/file-{file_index:03d}.parquet"
            if v3
            else "data/chunk-{episode_chunk:03d}/episode_{episode_index:06d}.parquet",
            video_path="videos/{video_key}/chunk-{chunk_index:03d}/file-{file_index:03d}.mp4"
            if v3
            else "videos/chunk-{episode_chunk:03d}/{video_key}/episode_{episode_index:06d}.mp4",
        )
        dump(root / "meta/info.json", info)
        dump(root / "meta/stats.json", all_stats)
        if v3:
            p = root / "meta/episodes/chunk-000/file-000.parquet"
            p.parent.mkdir(parents=True, exist_ok=True)
            pq.write_table(pa.Table.from_pylist(episode_meta), p)
            tasks = pa.table({"task_index": list(range(len(task_names))), "task": task_names})
            metadata = {
                "index_columns": ["task"],
                "column_indexes": [],
                "columns": [
                    {
                        "name": "task_index",
                        "field_name": "task_index",
                        "pandas_type": "int64",
                        "numpy_type": "int64",
                        "metadata": None,
                    },
                    {
                        "name": "task",
                        "field_name": "task",
                        "pandas_type": "unicode",
                        "numpy_type": "object",
                        "metadata": None,
                    },
                ],
                "creator": {"library": "pyarrow", "version": pa.__version__},
                "pandas_version": "2.2.3",
            }
            pq.write_table(
                tasks.replace_schema_metadata({b"pandas": json.dumps(metadata).encode()}),
                root / "meta/tasks.parquet",
            )
        else:
            (root / "meta/episodes.jsonl").write_text(
                "\n".join(
                    json.dumps({k: e[k] for k in ("episode_index", "tasks", "length")})
                    for e in episode_meta
                )
            )
            (root / "meta/tasks.jsonl").write_text(
                "\n".join(
                    json.dumps({"task_index": i, "task": t}) for i, t in enumerate(task_names)
                )
            )
            (root / "meta/episodes_stats.jsonl").write_text(
                "\n".join(
                    json.dumps(
                        {
                            "episode_index": e["episode_index"],
                            "stats": {
                                key: {
                                    s: e[f"stats/{key}/{s}"]
                                    for s in ("min", "max", "mean", "std", "count")
                                }
                                for key in features
                            },
                        }
                    )
                    for e in episode_meta
                )
            )
            dump(
                root / "meta/modality.json",
                {
                    "state": {"gripper": {"start": 0, "end": len(joint_set)}},
                    "action": {"gripper": {"start": 0, "end": len(joint_set)}},
                    "video": {
                        name: {"original_key": "observation.images." + name} for name in camera_set
                    },
                    "annotation": {"human.task_description": {"original_key": "task_index"}},
                },
            )
        dump(
            root / "umi.json",
            dict(
                repo_id=o["repo"],
                format=o["format"],
                episodes=[e["id"] for e in episodes],
                frames=total,
                pose_data="quest/*.jsonl preserves original timestamps; no inferred arm joints",
                frame="raw",
            ),
        )

    @staticmethod
    def nearest(source, times):
        result = []
        for t in times:
            right = min(bisect.bisect_left(source, int(t)), len(source) - 1)
            left = max(0, right - 1)
            result.append(left if abs(source[left] - t) <= abs(source[right] - t) else right)
        return result

    @staticmethod
    def encode_grid(images, selected, destination, fps):
        with tempfile.TemporaryFile() as errors:
            process = subprocess.Popen(
                [
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-f",
                    "image2pipe",
                    "-vcodec",
                    "mjpeg",
                    "-framerate",
                    str(fps),
                    "-i",
                    "pipe:0",
                    "-an",
                    "-frames:v",
                    str(len(selected)),
                    "-c:v",
                    "libx264",
                    "-preset",
                    "ultrafast",
                    "-threads", 
                    "1",
                    "-pix_fmt",
                    "yuv420p",
                    str(destination),
                ],
                stdin=subprocess.PIPE,
                stderr=errors,
            )
            try:
                for i in selected:
                    process.stdin.write(images[i].read_bytes())
                process.stdin.close()
                code = process.wait()
            except Exception:
                process.kill()
                process.wait()
                raise
            if code:
                errors.seek(0)
                raise RuntimeError(errors.read().decode()[-1000:])
        probe = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_frames",
                "-show_entries",
                "frame=best_effort_timestamp_time",
                "-of",
                "json",
                str(destination),
            ],
            capture_output=True,
            check=True,
        )
        stamps = [
            float(f["best_effort_timestamp_time"]) for f in json.loads(probe.stdout)["frames"]
        ]
        if len(stamps) != len(selected) or any(
            abs(t - i / fps) > 1e-5 for i, t in enumerate(stamps)
        ):
            raise RuntimeError(
                "Export vidéo non aligné : nombre de frames ou timestamps incorrects"
            )

    @staticmethod
    def ffmpeg(args):
        r = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", *args], capture_output=True
        )
        if r.returncode:
            raise RuntimeError(r.stderr.decode()[-1000:])
