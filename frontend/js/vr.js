class RecordButtons {
  constructor() {
    this.reset();
  }
  reset() {
    this.fail = false;
    this.b = false;
    this.failArmed = false;
    this.bArmed = false;
    this.long = false;
    this.start = 0;
    this.cancel = false;
  }
  update(fail, b, time, recording) {
    let action = null;
    if (!fail) this.failArmed = true;
    if (!b && !this.bArmed) {
      this.bArmed = true;
      this.b = false;
    }
    if (b && !this.b && this.bArmed) {
      this.start = time;
      this.long = false;
      this.cancel = false;
    }
    if (fail && !this.fail && this.failArmed && recording) {
      action = "fail";
      this.cancel = true;
    }
    if (
      b &&
      this.bArmed &&
      !this.long &&
      !this.cancel &&
      time - this.start >= 2000
    ) {
      this.long = true;
      if (recording) action = "not judged";
    }
    if (!b && this.b && this.bArmed && !this.long && !this.cancel)
      action = recording ? "success" : "start";
    this.fail = fail;
    this.b = b;
    return action;
  }
}
class TriggerCalibration {
  constructor(side) {
    this.side = side;
    this.strokes = [];
    this.samples = [];
    this.armed = false;
    this.hint = "Relâche complètement la gâchette";
  }

  update(time, value) {
    if (this.saving) return;
    const samples = this.samples;
    const last = samples.at(-1);
    if (value == null || (last && time - last.time > 150)) {
      this.samples = [];
      this.armed = false;
      this.hint = "Reprends la manette et relâche la gâchette";
      return;
    }
    if (value <= 0.01) {
      this.armed = true;
      this.samples = [];
      this.restTime = time;
      this.hint = "Presse lentement et régulièrement jusqu’au bout";
      if (this.strokes.length === 3) {
        this.saving = true;
        return this.strokes[0].map((_, i) =>
          this.strokes.map(stroke => stroke[i]).sort((a, b) => a - b)[1]);
      }
      return;
    }
    if (!this.armed) return;
    if (!last) samples.push({time: this.restTime, value: 0});
    samples.push({time, value});
    const duration = time - samples[0].time;
    if (duration > 5000 || (last && value < samples[0].peak - 0.05)) {
      this.armed = false;
      this.samples = [];
      this.hint = "Essai interrompu · relâche et recommence";
      return;
    }
    samples[0].peak = Math.max(samples[0].peak || 0, value);
    if (value < 0.99) return;
    this.armed = false;
    this.samples = [];
    if (duration < 700 || samples.length < 15) {
      this.hint = "Trop rapide · relâche et recommence";
      return;
    }
    const curve = [];
    for (let i = 1; i < 10; i++) {
      const t = samples[0].time + duration * i / 10;
      const j = samples.findIndex(p => p.time >= t);
      const a = samples[j - 1], b = samples[j];
      curve.push(a.value + (b.value - a.value) * (t - a.time) / (b.time - a.time));
    }
    if (curve.some((v, i) => v <= (curve[i - 1] || 0) || v >= 1)) {
      this.hint = "Mouvement irrégulier · relâche et recommence";
      return;
    }
    this.strokes.push(curve);
    this.hint = "Pression validée · relâche complètement";
  }
}
if (typeof module !== "undefined") module.exports = RecordButtons;
else
  (() => {
    let calibration = null;
    const triggerLocks = new Map();

    function triggerCommand(source, willSend) {
      const { handedness, gamepad } = source;
      const value = gamepad.buttons[0].value;
      const pressed = !!gamepad.buttons[3]?.pressed;
      let lock = triggerLocks.get(handedness);
      if (!lock || lock.source !== source) {
        lock = { source, pressed, locked: false, value };
        triggerLocks.set(handedness, lock);
      }
      if (pressed && !lock.pressed) lock.locked = !lock.locked;
      lock.pressed = pressed;
      if (willSend && !lock.locked) lock.value = value;
      return lock.value;
    }

    let session = null,
      gl = null,
      layer = null,
      world = null,
      viewer = null,
      frameName = "local-floor";
    let ready = false,
      pending = false,
      starting = false,
      seq = 0,
      lastFrame = 0;
    const recordButtons = new RecordButtons();
    let program,
      vertices,
      locationRect,
      panelDepth,
      projection,
      eyeFromWorld,
      worldFromHead,
      textures,
      hud,
      hudContext;
    let framesStarted = false;
    let originEpoch = 0;
    const cameraVisible = { left: true, right: true },
      cameraPressed = { left: true, right: true };
    const images = ["wrist_left", "wrist_right"].map((name) =>
      document.querySelector('img[alt="' + name + '"]'),
    );
    const info = (text) => ($("vrStatus").textContent = text);

    function shader(type, source) {
      const s = gl.createShader(type);
      gl.shaderSource(s, source);
      gl.compileShader(s);
      if (!gl.getShaderParameter(s, gl.COMPILE_STATUS))
        throw Error(gl.getShaderInfoLog(s));
      return s;
    }
    function renderer() {
      const canvas = document.createElement("canvas");
      gl = canvas.getContext("webgl", { xrCompatible: true, alpha: true });
      if (!gl) throw Error("WebGL unavailable");
      program = gl.createProgram();
      gl.attachShader(
        program,
        shader(
          gl.VERTEX_SHADER,
          "attribute vec2 p; uniform vec4 rect; uniform float panelDepth; uniform mat4 projection; uniform mat4 eyeFromWorld; uniform mat4 worldFromHead; varying vec2 uv; void main(){ uv=vec2((p.x+1.0)*0.5,(1.0-p.y)*0.5); vec4 point=vec4(rect.xy+p*rect.zw,-panelDepth,1.0); gl_Position=projection*eyeFromWorld*worldFromHead*point; }",
        ),
      );
      gl.attachShader(
        program,
        shader(
          gl.FRAGMENT_SHADER,
          "precision mediump float; varying vec2 uv; uniform sampler2D image; void main(){gl_FragColor=texture2D(image,uv);}",
        ),
      );
      gl.linkProgram(program);
      if (!gl.getProgramParameter(program, gl.LINK_STATUS))
        throw Error("WebGL link failed");
      gl.useProgram(program);
      vertices = gl.createBuffer();
      gl.bindBuffer(gl.ARRAY_BUFFER, vertices);
      gl.bufferData(
        gl.ARRAY_BUFFER,
        new Float32Array([-1, -1, 1, -1, -1, 1, 1, 1]),
        gl.STATIC_DRAW,
      );
      const loc = gl.getAttribLocation(program, "p");
      gl.enableVertexAttribArray(loc);
      gl.vertexAttribPointer(loc, 2, gl.FLOAT, false, 0, 0);
      locationRect = gl.getUniformLocation(program, "rect");
      panelDepth = gl.getUniformLocation(program, "panelDepth");
      projection = gl.getUniformLocation(program, "projection");
      eyeFromWorld = gl.getUniformLocation(program, "eyeFromWorld");
      worldFromHead = gl.getUniformLocation(program, "worldFromHead");
      textures = [0, 1, 2].map(() => {
        const t = gl.createTexture();
        gl.bindTexture(gl.TEXTURE_2D, t);
        gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
        gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
        gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
        gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
        return t;
      });
      hud = document.createElement("canvas");
      hud.width = 1024;
      hud.height = 192;
      hudContext = hud.getContext("2d");
    }
    function quad(texture, source, rect, depth = 3) {
      gl.bindTexture(gl.TEXTURE_2D, texture);
      gl.texImage2D(
        gl.TEXTURE_2D,
        0,
        gl.RGBA,
        gl.RGBA,
        gl.UNSIGNED_BYTE,
        source,
      );
      gl.uniform1f(panelDepth, depth);
      gl.uniform4fv(locationRect, rect);
      gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
    }
    function draw(pose) {
      gl.bindFramebuffer(gl.FRAMEBUFFER, layer.framebuffer);
      gl.clearColor(0, 0, 0, 0);
      gl.clear(gl.COLOR_BUFFER_BIT);
      hudContext.clearRect(0, 0, 1024, 192);
      hudContext.fillStyle = "rgba(18,21,26,.92)";
      hudContext.fillRect(0, 0, 1024, 192);
      const r = state?.recording;
      hudContext.font = "bold 58px sans-serif";
      hudContext.textAlign = "center";
      hudContext.fillStyle = r?.recording ? "#ff6258" : "#ffffff";
      hudContext.fillText(
        calibration ? (calibration.side === "left" ? "Gauche" : "Droite") + " · " + calibration.strokes.length + "/3" : r?.recording
          ? "● REC " + r.duration_s.toFixed(1) + " s"
          : r?.last.outcome
            ? "Saved · " + r.last.outcome
            : "B: record · Y: fail",
        512,
        59,
      );
      hudContext.font = "36px sans-serif";
      hudContext.fillStyle = "#d1d6df";
      hudContext.fillText(
        calibration ? calibration.hint : state?.motor.opening
          ? "Release both triggers to control"
          : state?.recording.error || "B hold 2 s: not judged",
        512,
        118,
        980,
      );
      const triggerText = side => {
        const controller = [...session.inputSources].find(s => s.handedness === side && s.gamepad?.mapping === "xr-standard");
        const value = controller?.gamepad.buttons[0]?.value;
        if (triggerLocks.get(side)?.locked) return (100 * triggerLocks.get(side).value).toFixed(1) + " % 🔒";
        return value == null ? "—" : (100 * value).toFixed(1) + " %";
      };
      hudContext.fillText("Gâchette G : " + triggerText("left") + "     D : " + triggerText("right"), 512, 178);
      gl.uniformMatrix4fv(worldFromHead, false, pose.transform.matrix);
      for (const view of pose.views) {
        const vp = layer.getViewport(view);
        gl.viewport(vp.x, vp.y, vp.width, vp.height);
        gl.uniformMatrix4fv(projection, false, view.projectionMatrix);
        gl.uniformMatrix4fv(eyeFromWorld, false, view.transform.inverse.matrix);
        if (cameraVisible.left && images[0].naturalWidth)
          quad(textures[0], images[0], [
            -1.25,
            -0.195,
            0.8,
            (0.8 * images[0].naturalHeight) / images[0].naturalWidth,
          ],3.5);
        if (cameraVisible.right && images[1].naturalWidth)
          quad(textures[1], images[1], [
            1.25,
            -0.195,
            0.8,
            (0.8 * images[1].naturalHeight) / images[1].naturalWidth,
          ],3.5);
        quad(textures[2], hud, [0, 0.8166667, 1.225, 0.2296875], 4.0);
      }
    }
    function poseData(p) {
      return p
        ? {
            position: [
              p.transform.position.x,
              p.transform.position.y,
              p.transform.position.z,
            ],
            orientation: [
              p.transform.orientation.x,
              p.transform.orientation.y,
              p.transform.orientation.z,
              p.transform.orientation.w,
            ],
          }
        : null;
    }
    function record(action, outcome) {
      send({ type: "record", action, outcome });
    }
    function buttons(right, left, time) {
      for (const [side, gamepad] of [
        ["right", right],
        ["left", left],
      ]) {
        if (!gamepad) {
          cameraPressed[side] = true;
          continue;
        }
        const pressed = !!gamepad.buttons[4]?.pressed;
        if (pressed && !cameraPressed[side])
          cameraVisible[side] = !cameraVisible[side];
        cameraPressed[side] = pressed;
      }
      const fail = !!left?.buttons[5]?.pressed,
        b = !!right.buttons[5]?.pressed;
      const action = recordButtons.update(
        fail,
        b,
        time,
        !!state?.recording.recording,
      );
      if (action)
        record(
          action === "start" ? "start" : "stop",
          action === "start" ? undefined : action,
        );
    }
    async function finish(reason = "VR ended") {
      const old = session;
      calibration = null;
      triggerLocks.clear();
      session = null;
      ready = false;
      pending = false;
      starting = false;
      framesStarted = false;
      if (ws?.readyState === 1) {
        send({ type: "diagnostic", message: reason });
        send({ type: "vr_stop" });
      }
      if (old)
        try {
          await old.end();
        } catch (_) {}
      if (gl) {
        gl.getExtension("WEBGL_lose_context")?.loseContext();
        gl = null;
      }
      $("enterVR").disabled = false;
      info(reason);
    }
    window.stopVR = finish;
    listeners.add((d) => {
      if (d.type === "vr_ready") {
        ready = true;
        starting = false;
      }
      if (d.type === "ack" && d.seq === seq) pending = false;
      if (d.type === "error" && session) finish(d.error);
    });
    function frame(time, xrFrame) {
      if (!session) return;
      session.requestAnimationFrame(frame);
      lastFrame = performance.now();
      let pose = null;
      try {
        syncClock();
        if (session.visibilityState !== "visible") {
          if (ready || starting) {
            send({ type: "vr_stop" });
            ready = starting = pending = false;
          }
          info("VR en pause : fermer le menu du casque pour reprendre");
          return;
        }
        pose = xrFrame.getViewerPose(world);
        if (!pose) return;
        if (calibration) {
          const source = [...session.inputSources].find(s => s.handedness === calibration.side && s.gamepad?.mapping === "xr-standard");
          const curve = calibration.update(time, source?.gamepad.buttons[0]?.value);
          if (curve) {
            const current = calibration;
            current.hint = "Enregistrement…";
            api("motor/trigger-curve", {side: current.side, curve})
              .then(() => { if (calibration === current) finish("Calibration enregistrée"); })
              .catch(e => { if (calibration === current) finish(e.message); });
          }
          return;
        }
        const right = [...session.inputSources].find(
          (s) =>
            s.handedness === "right" && s.gamepad?.mapping === "xr-standard",
        );
        if (!right) {
          if (ready) finish("Right controller lost · Motor OFF");
          return;
        }
        if (!ready && !starting) {
          triggerLocks.clear();
          starting = true;
          send({ type: "vr_start" });
          return;
        }
        if (!ready) return;
        const left = [...session.inputSources].find(
          (s) =>
            s.handedness === "left" && s.gamepad?.mapping === "xr-standard",
        );
        buttons(right.gamepad, left?.gamepad, time);
        const triggers = { right: triggerCommand(right, !pending) };
        if (left) triggers.left = triggerCommand(left, !pending);
        else triggerLocks.delete('left');
        if (pending) return;
        const hands = {};
        for (const source of session.inputSources) {
          if (!source.gripSpace) continue;
          hands[source.handedness] = {
            world: poseData(xrFrame.getPose(source.gripSpace, world)),
            head: poseData(xrFrame.getPose(source.gripSpace, viewer)),
          };
        }
        send({
          type: "sample",
          seq: ++seq,
          time: performance.now(),
          triggers: Object.fromEntries(
            [...session.inputSources]
              .filter((s) => s.gamepad?.mapping === "xr-standard")
              .map((s) => [s.handedness, s.gamepad.buttons[0].value]),
          ),
          reference: frameName,
          origin_epoch: originEpoch,
          recorded_frames: ["world", "head"],
          head: poseData(pose),
          head_frames: {
            world: poseData(pose),
            head: poseData(xrFrame.getViewerPose(viewer)),
          },
          controllers: hands,
        });
        pending = true;
      } catch (e) {
        finish(e.message);
      } finally {
        if (session && pose && session.visibilityState === "visible") {
          try {
            draw(pose);
            if (!calibration) captureTick();
          } catch (e) {
            finish(e.message);
          }
        }
      }
    }
    setInterval(() => {
      if (session && framesStarted && performance.now() - lastFrame > 1500)
        finish("VR frames lost · Motor OFF");
    }, 200);
    let chosenSide;
    for (const [id, side] of [["calLeft", "left"], ["calRight", "right"]]) {
      $(id).onclick = () => {
        chosenSide = side;
        $("triggerTitle").textContent = $(id).textContent;
        $("triggerDialog").showModal();
      };
    }
    $("calCancel").onclick = () => $("triggerDialog").close();
    $("calStart").onclick = () => {
      $("triggerDialog").close();
      startSession(chosenSide);
    };
    $("enterVR").onclick = () => startSession();
    async function startSession(side) {
      try {
        if (session) {
          await finish();
          return;
        }
        if (!navigator.xr || !isSecureContext)
          throw Error("Open the HTTPS address in Quest Browser");
        if (ws?.readyState !== 1) throw Error("Raspberry disconnected");
        $("enterVR").disabled = true;
        calibration = side ? new TriggerCalibration(side) : null;
        session = await navigator.xr.requestSession("immersive-ar", {
          optionalFeatures: ["local-floor"],
        });
        renderer();
        await gl.makeXRCompatible();
        layer = new XRWebGLLayer(session, gl, { alpha: true });
        session.updateRenderState({ baseLayer: layer });
        try {
          world = await session.requestReferenceSpace("local-floor");
          frameName = "local-floor";
        } catch (_) {
          world = await session.requestReferenceSpace("local");
          frameName = "local";
        }
        viewer = await session.requestReferenceSpace("viewer");
        originEpoch = 0;
        world.addEventListener("reset", (event) => {
          originEpoch++;
          if (ws?.readyState === 1)
            send({
              type: "tracking_reset",
              origin_epoch: originEpoch,
              reference: frameName,
              transform: event.transform
                ? Array.from(event.transform.matrix)
                : null,
            });
          info("Repère du casque recalé · VR active");
        });
        session.addEventListener("end", () => {
          if (session) finish();
        });
        ready = pending = starting = false;
        recordButtons.reset();
        cameraVisible.left = cameraVisible.right = false;
        cameraPressed.left = cameraPressed.right = true;
        lastFrame = performance.now();
        framesStarted = true;
        session.requestAnimationFrame(frame);
        info("Waiting for right controller…");
      } catch (e) {
        finish(e.message);
      }
    };
    window.isVRActive = () => !!session;
  })();
