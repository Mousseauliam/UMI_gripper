const $ = (id) => document.getElementById(id);
let state = null,
  ws = null,
  tasks = null,
  setupData = null,
  episodes = [],
  datasets = [];
const listeners = new Set();
let clockOffset = null,
  clockBestRtt = Infinity,
  clockChecked = 0;
function syncClock() {
  if (ws?.readyState === 1 && performance.now() - clockChecked > 5000) {
    clockChecked = performance.now();
    send({ type: "clock", client: clockChecked });
  }
}
function message(text) {
  $("msg").textContent = text;
}
async function api(path, data) {
  const r = await fetch(
    "/api/" + path,
    data === undefined
      ? {}
      : {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(data),
        },
  );
  const d = await r.json();
  if (!r.ok) throw Error(d.error || r.statusText);
  return d;
}
async function act(path, data = {}) {
  try {
    const d = await api(path, data);
    if (d.motor) render(d);
    message("");
    return d;
  } catch (e) {
    message(e.message);
    throw e;
  }
}
function send(data) {
  if (ws?.readyState !== 1) throw Error("Raspberry disconnected");
  ws.send(JSON.stringify(data));
}
function connect() {
  ws = new WebSocket(
    (location.protocol === "https:" ? "wss://" : "ws://") +
      location.host +
      "/ws",
  );
  ws.onopen = () => {
    $("connection").textContent = "connected";
    $("connection").className = "chip ok";
    clockOffset = null;
    clockBestRtt = Infinity;
    for (let i = 0; i < 5; i++)
      send({ type: "clock", client: performance.now() });
  };
  ws.onmessage = (e) => {
    const d = JSON.parse(e.data);
    if (d.type === "state") render(d);
    else if (d.type === "clock") {
      const now = performance.now(),
        rtt = now - d.client;
      if (rtt <= clockBestRtt + 2) {
        clockBestRtt = rtt;
        clockOffset = d.server - (d.client + now) / 2;
        send({ type: "clock_offset", offset: clockOffset, rtt });
      }
    } else if (d.type === "error") message(d.error);
    for (const f of listeners) f(d);
  };
  ws.onclose = () => {
    $("connection").textContent = "disconnected";
    $("connection").className = "chip bad";
    window.stopVR?.("Connection lost");
    setTimeout(connect, 1500);
  };
}
$("travelTime").onchange = () =>
  act("motor/speed", { seconds: Number($("travelTime").value) }).catch(() => {});

function render(s) {
  state = s;
  const m = s.motor,
    r = s.recording;
  $("position").textContent =
    m.telemetry.position == null
      ? "—"
      : m.telemetry.position.toFixed(3) + " rad";
  $("voltage").textContent =
    m.telemetry.voltage == null ? "—" : m.telemetry.voltage.toFixed(1) + " V";
  $("temperature").textContent =
    m.telemetry.temperature == null
      ? "—"
      : m.telemetry.temperature.toFixed(1) + " °C";
  $("motorState").textContent = m.active ? "ON" : "OFF";
  $("motorState").className = "chip " + (m.active ? "ok" : "");
  $("motorOn").disabled = m.active || m.scanning;
  $("motorOff").disabled = !m.active;
  $("toggle").disabled = !m.active || m.vr;
  $("saveJaw").disabled = $("setOpen").disabled = $("setClosed").disabled = m.active;
  $("triggerMid").disabled = m.active || m.scanning;
  if (document.activeElement !== $("triggerMid"))
    $("triggerMid").value = Math.round((m.config.trigger_midpoint ?? 0.5) * 100);
  $("scan").disabled = m.active || m.scanning;
  $("scan").textContent = m.scanning ? "Detecting…" : "Detect motors";
  $("motorError").textContent = m.error;
  $("angles").textContent =
    "Open: " +
    (m.config.opened?.toFixed(3) ?? "—") +
    " rad · Closed: " +
    (m.config.closed?.toFixed(3) ?? "—") +
    " rad · Jaw calibration: " + (Object.keys(m.config.jaw_points || {}).map(p => p + "%").join(", ") || "estimated");
  if (document.activeElement !== $("travelTime"))
    $("travelTime").value = String(m.config.travel_seconds);
  const select = $("motorSelect");
  if (document.activeElement !== select) {
    const values = Object.keys(m.motors);
    if ([...select.options].map((x) => x.value).join() !== values.join()) {
      select.replaceChildren(
        ...values.map(
          (id) =>
            new Option(
              ({ 18: "Left", 28: "Right" }[id] || "RS05") + " · ID " + id,
              id,
            ),
        ),
      );
    }
    select.value = m.config.selected;
  }
  select.disabled = m.active;
  const text = r.recording
    ? "● recording · " + r.duration_s.toFixed(1) + " s"
    : r.last.outcome
      ? "Last: " + r.last.outcome
      : "not recording";
  $("topRec").textContent = $("recChip").textContent = text;
  $("topRec").className = $("recChip").className =
    "chip " + (r.recording ? "rec" : "");
  $("recStart").disabled = r.recording;
  for (const id of ["recStop", "recFail", "recDiscard"])
    $(id).disabled = !r.recording;
  for (const [name, c] of Object.entries(s.cameras)) {
    const label = $("cam_" + name);
    if (label)
      label.textContent =
        name.replaceAll("_", " ") +
        " · " +
        (c.encoder_error || (c.live ? "live" : c.error || "no frames"));
  }
  if (!r.recording && r.last.data_complete === "false")
    message(
      "Épisode sauvegardé, données incomplètes : " +
        [
          ...JSON.parse(r.last.missing_streams || "[]"),
          ...JSON.parse(r.last.discontinuous_streams || "[]"),
        ].join(", "),
    );
  if (r.error) message(r.error);
}
for (const [id, action] of Object.entries({
  motorOn: "on",
  motorOff: "off",
  toggle: "toggle",
  setOpen: "opened",
  setClosed: "closed",
  scan: "scan",
}))
  $(id).onclick = () => act("motor/" + action).catch(() => {});
$("triggerMid").onchange = () =>
  act("motor/trigger-midpoint", { percent: Number($("triggerMid").value) }).catch(() => {});
$("saveJaw").onclick = () =>
  act("motor/jaw-point", { percent: $("jawPercent").value }).catch(() => {});
$("motorSelect").onchange = () =>
  act("motor/select", { id: Number($("motorSelect").value) }).catch(() => {});
for (const name of [
  "wrist_left",
  "wrist_right",
  "torso_left",
  "torso_right",
  "quest",
]) {
  const figure = document.createElement("figure"),
    img = document.createElement("img"),
    caption = document.createElement("figcaption");
  img.src = "/camera/" + name + ".mjpg";
  img.alt = name;
  caption.id = "cam_" + name;
  caption.textContent = name;
  figure.append(img, caption);
  $("cameras").append(figure);
}
async function loadTasks() {
  tasks = await api("tasks");
  $("task").replaceChildren(
    new Option("— pick the task —", ""),
    ...tasks.tasks.map(
      (t) =>
        new Option(
          (t.group ? t.group + " — " : "") + t.task + " [" + t.category + "]",
          t.task,
        ),
    ),
  );
  $("task").value = tasks.selected || "";
  for (const k of ["operator", "location"]) $(k).value = tasks[k] || "";
  $("newCat").replaceChildren(
    ...Object.entries(tasks.categories).map(
      ([k, v]) => new Option(k + " — " + v, k),
    ),
  );
  $("taskHelp").textContent =
    "One fixed wording per task. Saved locally on the Raspberry.";
}
async function loadSetup() {
  setupData = await api("setup");
  for (const k of ["tools", "objects", "quality"]) {
    const box = $("set_" + k);
    box.replaceChildren();
    const chosen = setupData.setup[k] || [];
    if (!chosen.length) box.textContent = "none";
    chosen.forEach((item, i) => {
      const chip = document.createElement("span");
      chip.className = "chip";
      chip.textContent = item.name + " ";
      const x = document.createElement("span");
      x.className = "x";
      x.textContent = "×";
      x.onclick = async () => {
        chosen.splice(i, 1);
        await act("setup/set", setupData.setup);
        loadSetup();
      };
      chip.append(x);
      box.append(chip);
    });
    $("setAdd_" + k).replaceChildren(
      new Option("+ Add", ""),
      ...(setupData.catalog[k] || [])
        .filter((x) => !chosen.some((y) => y.name === x.name))
        .map((x) => new Option(x.name, x.id || x.name)),
      new Option("Other…", "__other"),
    );
  }
  $("setupNote").textContent = "Local copy of the portal lists.";
}
for (const k of ["tools", "objects", "quality"])
  $("setAdd_" + k).onchange = async () => {
    const value = $("setAdd_" + k).value;
    if (!value) return;
    let item =
      value === "__other"
        ? { id: "", name: (prompt("Name:") || "").trim() }
        : (setupData.catalog[k] || []).find((x) => (x.id || x.name) === value);
    if (item?.name) {
      setupData.setup[k].push(item);
      await act("setup/set", setupData.setup);
    }
    loadSetup();
  };
$("task").onchange = () => act("tasks/select", { task: $("task").value });
for (const k of ["operator", "location"])
  $(k).onchange = () =>
    act("session/set", {
      operator: $("operator").value,
      location: $("location").value,
    });
$("addTask").onclick = async () => {
  await act("tasks/add", {
    task: $("newTask").value,
    category: $("newCat").value,
    group: $("newGroup").value,
  });
  $("newTask").value = "";
  loadTasks();
};
$("recStart").onclick = () => act("record/start");
for (const [id, outcome] of [
  ["recStop", "success"],
  ["recFail", "fail"],
])
  $(id).onclick = async () => {
    await act("record/stop", { outcome, note: $("recNote").value });
    $("recNote").value = "";
    loadEpisodes();
  };
$("recDiscard").onclick = async () => {
  await act("record/discard");
  loadEpisodes();
};
function cell(row, text) {
  const td = document.createElement("td");
  td.textContent = text;
  row.append(td);
  return td;
}
async function loadEpisodes() {
  const result = await Promise.all([api("episodes"), api("datasets")]);
  episodes = result[0];
  datasets = result[1].datasets;
  $("hwepsNew").replaceChildren();
  $("hwepsDone").replaceChildren();
  let converted = 0;
  for (const e of episodes) {
    const sets = datasets.filter((d) => d.episodes.includes(e.id));
    if (sets.length) converted++;
    const row = document.createElement("tr");
    const info = cell(row, e.task || "No task");
    const small = document.createElement("div");
    small.className = "muted";
    small.textContent =
      e.id +
      " · " +
      Number(e.duration_s || 0).toFixed(0) +
      " s · " +
      (e.bytes / 1048576).toFixed(1) +
      " MB";
    info.append(small);
    cell(row, e.recording ? "recording" : e.outcome || "not judged");
    if (sets.length) cell(row, sets.map((d) => d.repo_id).join(", "));
    const actions = cell(row, "");
    const link = document.createElement("a");
    link.className = "dl";
    link.textContent = "raw";
    link.href = "/episodes/" + e.id + ".mcap";
    if (!e.recording) actions.append(link);
    const del = document.createElement("button");
    del.textContent = "discard";
    del.className = "tiny ghost";
    del.disabled = !!e.recording;
    del.onclick = async () => {
      await act("episode/discard", { id: e.id });
      loadEpisodes();
    };
    actions.append(del);
    $(sets.length ? "hwepsDone" : "hwepsNew").append(row);
  }
  $("tabNew").textContent =
    "To convert (" + (episodes.length - converted) + ")";
  $("tabDone").textContent = "Converted (" + converted + ")";
  $("tabSets").textContent = "Datasets (" + datasets.length + ")";
  $("nohwepsNew").hidden = episodes.length > converted;
  $("nohwepsDone").hidden = converted > 0;
  $("hwdirs").textContent =
    "Recordings: data/episodes · Datasets: data/datasets · Discarded: data/episodes/trash";
  $("hwsets").replaceChildren();
  for (const d of datasets) {
    const row = document.createElement("tr");
    cell(row, d.repo_id);
    cell(row, d.format);
    cell(row, String(d.episodes.length));
    cell(row, d.frames + " frames");
    $("hwsets").append(row);
  }
  const job = result[1].job;
  $("cvGo").disabled = job.running;
  $("cvJobs").textContent =
    job.error ||
    (job.repo
      ? job.repo +
        " · " +
        job.done +
        "/" +
        job.total +
        (job.running ? " converting…" : " complete")
      : "");
  if (document.activeElement !== $("cvGroup")) {
    const keep = $("cvGroup").value;
    $("cvGroup").replaceChildren(
      new Option("any", ""),
      ...[...new Set(episodes.map((e) => e.group).filter(Boolean))].map(
        (g) => new Option(g, g),
      ),
    );
    $("cvGroup").value = keep;
  }
  preview();
}
function options() {
  return {
    repo: $("hwrepo").value.trim(),
    format: $("cvFormat").value,
    frame: $("cvFrame").value,
    raw: $("cvRaw").checked,
    group: $("cvGroup").value,
    session: $("cvSession").value.trim(),
    outcomes: [
      ["cvOk", "success"],
      ["cvFail", "fail"],
      ["cvNone", "not judged"],
    ]
      .filter(([id]) => $(id).checked)
      .map(([, v]) => v),
  };
}
function preview() {
  const o = options();
  const count = episodes.filter(
    (e) =>
      !e.recording &&
      o.outcomes.includes(e.outcome || "not judged") &&
      (!o.group || e.group === o.group) &&
      (!o.session || e.session === o.session),
  ).length;
  $("cvPreview").textContent =
    count + " episode(s) match · local conversion · raw gripper angles";
}
for (const id of ["tabNew", "tabDone", "tabSets"])
  $(id).onclick = () => {
    for (const other of ["tabNew", "tabDone", "tabSets"]) {
      $(other).classList.toggle("on", id === other);
      $($(other).dataset.tab).hidden = id !== other;
    }
  };
$("cvGo").onclick = async () => {
  await act("convert", options());
  loadEpisodes();
};
$("cvFrame").value = "raw";
$("cvRaw").disabled = true;
for (const id of ["cvOk", "cvFail", "cvNone", "cvGroup", "cvSession"])
  $(id).onchange = preview;
Promise.all([loadTasks(), loadSetup(), loadEpisodes()]).catch((e) =>
  message(e.message),
);
setInterval(() => loadEpisodes().catch(() => {}), 3000);
connect();
