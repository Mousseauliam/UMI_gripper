(() => {
  let capture = null, video = null, worker = null, uploading = false;
  let cameraWS = null, cameraSequence = 0, recordedEpisode = null;
  let lastCapture = 0, lastMediaTime = -1, lastVideoMeta = null;
  const awaiting = new Map();
  let forceKey = true, encodingMode = "Starting encoder";
  function diagnostic(message) {
    $("questStatus").textContent = message;
    if (ws?.readyState === 1) send({ type: "diagnostic", message });
  }
  function startWorker() {
    if (!window.Worker || !window.VideoFrame)
      throw Error("Camera encoding requires Worker and VideoFrame support");
    worker = new Worker("/static/js/quest_encoder.js");
    const current = worker;
    worker.onmessage = ({ data }) => {
      if (worker !== current) return;
      if (data.type === "status") {
        encodingMode = data.mode;
        diagnostic("Quest encoder: " + data.mode);
        return;
      }
      uploading = false;
      if (data.error) { diagnostic(data.error); return; }
      if (performance.now() - data.submittedAt > 350) {
        forceKey = true;
        return;
      }
      if (!capture || cameraWS?.readyState !== 1) { forceKey = true; return; }
      const header = data.h264 ? 28 : 16;
      const packet = new ArrayBuffer(header + data.bytes.byteLength);
      const view = new DataView(packet);
      view.setFloat64(0, data.stamp, true);
      view.setBigUint64(8, BigInt(++cameraSequence), true);
      if (data.h264) {
        new Uint8Array(packet, 16, 4).set([72, 50, 54, 52]);
        view.setUint32(20, data.width, true);
        view.setUint32(24, data.height, true);
      }
      new Uint8Array(packet, header).set(new Uint8Array(data.bytes));
      cameraWS.send(packet);
      awaiting.set(cameraSequence, performance.now());
    };
    worker.onerror = (event) => {
      uploading = false;
      diagnostic("Quest encoder worker failed: " + event.message);
      current.terminate();
      if (worker === current) worker = null;
    };
  }
  function connectCamera() {
    forceKey = true;
    awaiting.clear();
    cameraWS = new WebSocket(
      (location.protocol === "https:" ? "wss://" : "ws://") +
      location.host + "/camera-ws",
    );
    const socket = cameraWS;
    cameraWS.onmessage = ({ data }) => {
      if (cameraWS !== socket) return;
      const result = JSON.parse(data);
      if (result.sequence !== undefined) {
        for (const sequence of awaiting.keys()) {
          if (sequence <= result.sequence) awaiting.delete(sequence);
        }
      }
      if (result.error) {
        if (result.code === "codec") worker?.postMessage({ type: "jpeg" });
        forceKey = true;
        diagnostic(result.error);
        return;
      }
      if (result.episode !== recordedEpisode) {
        recordedEpisode = result.episode;
        forceKey = true;
      }
      $("questStatus").textContent =
        "Quest: " + result.sequence + " frames sent · " + encodingMode +
        " · network " + result.transport_ms.toFixed(0) + " ms";
    };
  }
  function watchVideo(now, metadata) {
    lastVideoMeta = metadata;
    if (!window.isVRActive()) captureTick();
    if (capture) video.requestVideoFrameCallback(watchVideo);
  }
  $("questCamera").onclick = async () => {
    try {
      $("questStatus").textContent = "Requesting access to the external camera…";
      if (capture) {
        capture.getTracks().forEach((t) => t.stop());
        capture = null;
        worker?.terminate();
        worker = null;
        uploading = false;
        cameraWS?.close();
        $("questCamera").textContent = "Enable Quest camera";
        $("questStatus").textContent = "Camera stopped";
        return;
      }
      if (!isSecureContext || !navigator.mediaDevices?.getUserMedia)
        throw Error("Open this page over HTTPS in Quest Browser");
      try {
        capture = await navigator.mediaDevices.getUserMedia({
          audio: false,
          video: { facingMode: { exact: "environment" } },
        });
      } catch (firstError) {
        if (firstError.name !== "OverconstrainedError") throw firstError;
        $("questStatus").textContent =
          "Allow camera access on the Quest to check for an external camera…";
        const permission = await navigator.mediaDevices.getUserMedia({
          audio: false,
          video: true,
        });
        permission.getTracks().forEach((track) => track.stop());
        capture = await navigator.mediaDevices.getUserMedia({
          audio: false,
          video: { facingMode: { exact: "environment" } },
        });
      }
      video = document.createElement("video");
      video.muted = true;
      video.playsInline = true;
      video.srcObject = capture;
      await video.play();
      lastMediaTime = -1;
      lastVideoMeta = null;
      startWorker();
      connectCamera();
      if (video.requestVideoFrameCallback)
        video.requestVideoFrameCallback(watchVideo);
      $("questCamera").textContent = "Disable Quest camera";
      $("questStatus").textContent = "External Quest camera active";
    } catch (e) {
      worker?.terminate();
      worker = null;
      if (capture) {
        capture.getTracks().forEach((t) => t.stop());
        capture = null;
      }
      let cameras = [];
      try {
        cameras = (await navigator.mediaDevices.enumerateDevices())
          .filter((d) => d.kind === "videoinput")
          .map((d) => d.label || "unidentified camera");
      } catch (_) {}
      const reason =
        e.name === "OverconstrainedError"
          ? "The browser did not provide a compatible external camera."
          : "Camera access denied or unavailable.";
      $("questStatus").textContent =
        reason +
        " " +
        e.name +
        (e.constraint ? " [" + e.constraint + "]" : "") +
        " " +
        (e.message || "") +
        " · Available cameras: " +
        (cameras.join(", ") || "none");
      if (ws?.readyState === 1)
        send({ type: "diagnostic", message: $("questStatus").textContent });
    }
  };
  window.captureTick = function () {
    const now = performance.now();
    if (!capture || !worker || !video?.videoWidth || clockOffset === null) return;
    if (!cameraWS || cameraWS.readyState === 3) { connectCamera(); return; }
    if (awaiting.size && now - awaiting.values().next().value > 1500) {
      cameraWS.close();
      connectCamera();
      return;
    }
    if (uploading || awaiting.size >= 2 || cameraWS.readyState !== 1 ||
        cameraWS.bufferedAmount > 64000 || now - lastCapture < 66 ||
        video.currentTime === lastMediaTime) return;
    try {
      const stamp = lastVideoMeta &&
        Math.abs(lastVideoMeta.mediaTime - video.currentTime) < 0.03 &&
        Number.isFinite(lastVideoMeta.captureTime) ? lastVideoMeta.captureTime : now;
      const timestamp = stamp + clockOffset;
      const frame = new VideoFrame(video, { timestamp: Math.round(timestamp * 1000) });
      uploading = true;
      try {
        worker.postMessage({ type: "frame", frame, stamp: timestamp, submittedAt: now, key: forceKey }, [frame]);
      } finally {
        frame.close();
      }
      forceKey = false;
      lastCapture = now;
      lastMediaTime = video.currentTime;
    } catch (error) {
      uploading = false;
      diagnostic(error.message);
    }
  };
  setInterval(() => {
    if (!window.isVRActive()) { syncClock(); captureTick(); }
  }, 67);
})();
