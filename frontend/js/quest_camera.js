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
  function connectCamera() {
    cameraWS = new WebSocket(
      (location.protocol === "https:" ? "wss://" : "ws://") +
        location.host +
        "/camera-ws",
    );
    cameraWS.onmessage = (e) => {
      uploading = false;
      const d = JSON.parse(e.data);
      $("questStatus").textContent =
        d.error ||
        "Quest : " +
          d.sequence +
          " frames sent · network " +
          d.transport_ms.toFixed(0) +
          " ms";
    };
    cameraWS.onclose = () => {
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
      const jpeg = atob(canvas.toDataURL("image/jpeg", 0.8).split(",")[1]);
      const packet = new ArrayBuffer(16 + jpeg.length),
        view = new DataView(packet),
        bytes = new Uint8Array(packet, 16);
      view.setFloat64(0, stamp + clockOffset, true);
      view.setBigUint64(8, BigInt(++cameraSequence), true);
      for (let i = 0; i < jpeg.length; i++) bytes[i] = jpeg.charCodeAt(i);
      cameraWS.send(packet);
      uploading = true;
      sentAt = now;
      lastCapture = now;
      lastMediaTime = video.currentTime;
    } catch (e) {
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
