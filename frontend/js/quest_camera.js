(() => {
  let capture = null,
    video = null,
    uploading = false,
    cameraWS = null,
    cameraSequence = 0;
  let lastCapture = 0,
    lastMediaTime = -1,
    sentAt = 0,
    lastVideoMeta = null;
  let encoder = null, forceKey = true, lastKey = 0, recordedEpisode = null;
  let encodingMode = "JPEG";
  function useJPEG(reason) {
    if (encoder && encoder.state !== "closed") encoder.close();
    encoder = null;
    uploading = false;
    encodingMode = "JPEG fallback";
    if (ws?.readyState === 1) send({ type: "diagnostic", message: "Quest encoder: " + reason });
  }
  async function configureEncoder() {
    if (!window.VideoEncoder || !window.VideoFrame) return;
    const config = {
      codec: "avc1.42E01E", width: 640,
      height: Math.round(video.videoHeight / video.videoWidth * 320) * 2,
      bitrate: 2000000, framerate: 15, latencyMode: "realtime",
      hardwareAcceleration: "prefer-hardware", avc: { format: "annexb" },
    };
    try {
      const result = await VideoEncoder.isConfigSupported(config);
      if (!result.supported) { useJPEG("H.264 configuration unsupported"); return; }
      encoder = new VideoEncoder({
        output(chunk) {
          if (!capture || cameraWS?.readyState !== 1) { uploading = false; forceKey = true; return; }
          const data = new Uint8Array(chunk.byteLength);
          chunk.copyTo(data);
          sendFrame(data, chunk.timestamp / 1000, true);
        },
        error(error) { useJPEG(error.message); },
      });
      encoder.configure(config);
      encodingMode = "H.264 on Quest";
      forceKey = true;
    } catch (error) { useJPEG(error.message); }
  }
  function sendFrame(data, stamp, h264 = false) {
    const offset = h264 ? 28 : 16;
    const packet = new ArrayBuffer(offset + data.byteLength);
    const view = new DataView(packet);
    view.setFloat64(0, stamp, true);
    view.setBigUint64(8, BigInt(++cameraSequence), true);
    if (h264) {
      new Uint8Array(packet, 16, 4).set([72, 50, 54, 52]);
      view.setUint32(20, canvas.width, true);
      view.setUint32(24, canvas.height, true);
    }
    new Uint8Array(packet, offset).set(data);
    cameraWS.send(packet);
  }
  function connectCamera() {
    forceKey = true;
    cameraWS = new WebSocket(
      (location.protocol === "https:" ? "wss://" : "ws://") +
        location.host +
        "/camera-ws",
    );
    const socket = cameraWS;
    cameraWS.onmessage = (e) => {
      if (cameraWS !== socket) return;
      uploading = false;
      const d = JSON.parse(e.data);
      if (d.error && encoder) useJPEG(d.error);
      if (d.episode !== recordedEpisode) {
        recordedEpisode = d.episode;
        forceKey = true;
      }
      $("questStatus").textContent =
        d.error ||
        "Quest : " +
          d.sequence +
          " frames sent · " + encodingMode + " · network " +
          d.transport_ms.toFixed(0) +
          " ms";
    };
    cameraWS.onclose = () => {
      if (cameraWS !== socket) return;
      uploading = false;
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
        if (encoder && encoder.state !== "closed") encoder.close();
        encoder = null;
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
      await configureEncoder();
      connectCamera();
      if (video.requestVideoFrameCallback)
        video.requestVideoFrameCallback(watchVideo);
      $("questCamera").textContent = "Disable Quest camera";
      $("questStatus").textContent = "External Quest camera active";
    } catch (e) {
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
  const canvas = document.createElement("canvas"),
    ctx = canvas.getContext("2d");
  window.captureTick = function () {
    const now = performance.now();
    if (!capture || !video?.videoWidth || clockOffset === null) return;
    if (uploading && now - sentAt > 1500) {
      if (encoder) useJPEG("Encoder or transfer timed out");
      cameraWS?.close();
      uploading = false;
      connectCamera();
      return;
    }
    if (!cameraWS || cameraWS.readyState === 3) {
      connectCamera();
      return;
    }
    if (
      uploading ||
      cameraWS.readyState !== 1 ||
      now - lastCapture < 66 ||
      video.currentTime === lastMediaTime
    )
      return;
    try {
      canvas.width = 640;
      canvas.height =
        Math.round(((video.videoHeight / video.videoWidth) * 640) / 2) * 2;
      ctx.drawImage(video, 0, 0, canvas.width, canvas.height);
      const stamp =
        lastVideoMeta &&
        Math.abs(lastVideoMeta.mediaTime - video.currentTime) < 0.03 &&
        Number.isFinite(lastVideoMeta.captureTime)
          ? lastVideoMeta.captureTime
          : now;
      uploading = true;
      sentAt = now;
      lastCapture = now;
      lastMediaTime = video.currentTime;
      const timestamp = stamp + clockOffset;
      if (encoder) {
        const frame = new VideoFrame(canvas, { timestamp: Math.round(timestamp * 1000) });
        const keyFrame = forceKey || now - lastKey >= 1000;
        try { encoder.encode(frame, { keyFrame }); }
        finally { frame.close(); }
        if (keyFrame) { forceKey = false; lastKey = now; }
      } else {
        const socket = cameraWS;
        canvas.toBlob(async (blob) => {
          try {
            if (!blob) throw Error("JPEG encoding failed");
            const data = new Uint8Array(await blob.arrayBuffer());
            if (!capture || cameraWS !== socket || socket.readyState !== 1) return;
            sendFrame(data, timestamp);
          } catch (error) {
            uploading = false;
            $("questStatus").textContent = error.message;
          }
        }, "image/jpeg", 0.8);
      }
    } catch (e) {
      if (encoder) useJPEG(e.message);
      uploading = false;
      $("questStatus").textContent = e.message;
    }
  };
  setInterval(() => {
    if (!window.isVRActive()) {
      syncClock();
      captureTick();
    }
  }, 67);
})();
