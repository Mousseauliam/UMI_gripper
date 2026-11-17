let canvas, ctx, encoder = null, jpegOnly = false, lastKey = 0;
let output = null, encoderError = null;

function fallback(reason) {
  if (encoder && encoder.state !== "closed") encoder.close();
  encoder = null;
  jpegOnly = true;
  postMessage({ type: "status", mode: "JPEG worker (" + reason + ")" });
}

async function setup(width, height) {
  canvas = new OffscreenCanvas(width, height);
  ctx = canvas.getContext("2d");
  if (jpegOnly || !self.VideoEncoder) { fallback("H.264 unavailable"); return; }
  const config = {
    codec: "avc1.42E01E", width, height, bitrate: 2000000, framerate: 15,
    latencyMode: "realtime", hardwareAcceleration: "prefer-hardware",
    avc: { format: "annexb" },
  };
  try {
    if (!(await VideoEncoder.isConfigSupported(config)).supported) {
      fallback("H.264 configuration unsupported");
      return;
    }
    encoder = new VideoEncoder({
      output(chunk) {
        const bytes = new Uint8Array(chunk.byteLength);
        chunk.copyTo(bytes);
        output = bytes.buffer;
      },
      error(error) { encoderError = error; },
    });
    encoder.configure(config);
    postMessage({ type: "status", mode: "H.264 worker" });
  } catch (error) { fallback(error.message); }
}

self.onmessage = async ({ data }) => {
  if (data.type === "jpeg") { fallback("server rejected H.264"); return; }
  const frame = data.frame;
  let timer;
  try {
    const width = 640, height = Math.round(frame.displayHeight / frame.displayWidth * 320) * 2;
    if (!canvas || canvas.width !== width || canvas.height !== height) {
      if (encoder && encoder.state !== "closed") encoder.close();
      await setup(width, height);
      lastKey = 0;
    }
    ctx.drawImage(frame, 0, 0, width, height);
    let bytes, h264 = false;
    if (encoder) {
      output = null;
      encoderError = null;
      const resized = new VideoFrame(canvas, { timestamp: frame.timestamp });
      const keyFrame = data.key || !lastKey || performance.now() - lastKey >= 1000;
      try {
        encoder.encode(resized, { keyFrame });
        await Promise.race([
          encoder.flush(),
          new Promise((_, reject) => {
            timer = setTimeout(() => reject(Error("H.264 encode timed out")), 2000);
          }),
        ]);
        if (encoderError) throw encoderError;
        if (!output) throw Error("No H.264 output");
        bytes = output;
        h264 = true;
        if (keyFrame) lastKey = performance.now();
      } catch (error) { fallback(error.message); }
      finally { resized.close(); clearTimeout(timer); }
    }
    if (!h264) {
      const blob = await canvas.convertToBlob({ type: "image/jpeg", quality: 0.8 });
      bytes = await blob.arrayBuffer();
    }
    postMessage({ bytes, h264, stamp: data.stamp, width, height }, [bytes]);
  } catch (error) {
    postMessage({ error: "Quest encoding failed: " + error.message });
  } finally {
    frame.close();
  }
};
