"use strict";
const $ = (id) => document.getElementById(id);
let state,
  token,
  initialised = false,
  dirty = false,
  previousEvents = "",
  previousJobs = "";
const labels = {
  received: "Received",
  uploaded: "Uploaded",
  accepted: "Accepted; verify",
  captured: "Captured only",
  queued: "Queued",
  retry: "Retry scheduled",
  uploading: "Uploading",
  failed: "Failed",
  duplicate: "Duplicate",
  invalid: "Invalid packet",
  ignored: "Ignored",
  review: "Review changes",
};
function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}
function notice(message, error = false) {
  $("notice").hidden = false;
  $("notice").textContent = message;
  $("notice").className = error ? "error" : "";
}
async function api(path, body) {
  const response = await fetch(
    "/api/" + path,
    body === undefined
      ? {}
      : {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-Bridge-Token": token,
          },
          body: JSON.stringify(body),
        },
  );
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || "Request failed");
  return result;
}
function statusNode(status) {
  return element("span", labels[status] || status, "status " + status);
}
function rawDetails(title, text, id) {
  const details = element("details");
  details.dataset.id = id;
  details.append(element("summary", title), element("pre", text));
  return details;
}
function replacePreservingOpen(container, fragment) {
  const open = new Set(
    [...container.querySelectorAll("details[open]")].map((x) => x.dataset.id),
  );
  const scroll = container.scrollTop;
  container.replaceChildren(fragment);
  for (const details of container.querySelectorAll("details"))
    details.open = open.has(details.dataset.id);
  container.scrollTop = scroll;
}
function renderEvents(events) {
  const signature = JSON.stringify(events);
  if (signature === previousEvents) return;
  previousEvents = signature;
  if (!events.length) return;
  const fragment = document.createDocumentFragment();
  for (const item of events) {
    const card = element("article", undefined, "event"),
      row = element("div", undefined, "row");
    row.append(
      element("span", item.call || item.protocol, "call"),
      statusNode(item.status === "queued" ? "received" : item.status),
    );
    const when = element(
      "time",
      new Date(item.created * 1000).toLocaleTimeString("en-GB"),
    );
    when.dateTime = new Date(item.created * 1000).toISOString();
    row.append(when);
    card.append(
      row,
      element(
        "p",
        item.status === "queued"
          ? "Received for automatic upload; delivery results below."
          : item.detail,
        "detail",
      ),
    );
    if (item.job_id)
      card.append(
        element(
          "p",
          `CloudLog: ${labels[item.upload_status] || item.upload_status} · ${item.upload_detail}`,
          "detail",
        ),
      );
    if (item.clublog_job_id)
      card.append(
        element(
          "p",
          `Club Log: ${labels[item.clublog_status] || item.clublog_status} · ${item.clublog_detail}`,
          "detail",
        ),
      );
    card.append(
      element("p", `${item.protocol} · from ${item.source}`, "detail"),
      rawDetails("View received packet", item.raw, "event-" + item.id),
    );
    fragment.append(card);
  }
  replacePreservingOpen($("events"), fragment);
}
function renderJobs(jobs) {
  const signature = JSON.stringify(jobs);
  if (signature === previousJobs) return;
  previousJobs = signature;
  if (!jobs.length) return;
  const fragment = document.createDocumentFragment();
  for (const item of jobs) {
    const card = element("article", undefined, "job"),
      row = element("div", undefined, "row");
    row.append(
      element("span", item.call, "call"),
      element(
        "span",
        item.service === "clublog" ? "Club Log" : "CloudLog",
        "pill",
      ),
      statusNode(item.status),
    );
    if (["failed", "retry"].includes(item.status)) {
      const retry = element("button", "Retry", "secondary");
      retry.addEventListener("click", () =>
        action(retry, async () => {
          await api("retry", { id: item.id });
          notice("Retry queued. Uploads must be enabled for it to run.");
        }),
      );
      row.append(retry);
    }
    card.append(
      row,
      element("p", item.detail, "detail"),
      element(
        "p",
        `#${item.id} · ${item.attempts} attempt(s) · ${new Date(item.created * 1000).toLocaleString("en-GB")}`,
        "detail",
      ),
      element(
        "p",
        `${item.target} · ${item.service === "clublog" ? "callsign" : "station"} ${item.station}`,
        "detail",
      ),
      rawDetails("View outgoing ADIF", item.adif, "job-" + item.id),
    );
    fragment.append(card);
  }
  replacePreservingOpen($("jobs"), fragment);
}
function populate(config) {
  for (const key of [
    "udp_host",
    "udp_port",
    "cloudlog_url",
    "station_id",
    "clublog_email",
    "clublog_callsign",
  ])
    $(key).value = config[key];
  $("uploads_enabled").checked = config.uploads_enabled;
  $("clublog_enabled").checked = config.clublog_enabled;
  for (const name of ["clublog_password", "clublog_api_key"]) {
    $(name).value = "";
    $("clear_" + name).checked = false;
    $(name).placeholder = config["has_" + name]
      ? "Saved · leave blank to keep"
      : "Not yet configured";
  }
  $("api_key").value = "";
  $("clear_api_key").checked = false;
  $("api_key").placeholder = config.has_api_key
    ? "Saved key · leave blank to keep"
    : "Enter API key";
  $("key-help").textContent = config.has_api_key
    ? "A key is saved in macOS Keychain. It is never sent back to this page."
    : "Stored in macOS Keychain.";
  dirty = false;
  $("saved").textContent = "Saved locally";
}
async function refresh() {
  try {
    state = await api("state");
    token = state.token;
    if (!initialised) {
      populate(state.config);
      initialised = true;
    }
    $("connection").textContent = "Local service connected";
    $("connection").className = "pill live";
    $("listener-status").textContent = state.listening
      ? "Listening"
      : "Stopped";
    $("listener-address").textContent =
      `${state.config.udp_host}:${state.config.udp_port}`;
    $("setup-address").textContent = $("listener-address").textContent;
    $("toggle").textContent = state.listening
      ? "Stop listener"
      : "Start listener";
    $("toggle").className = state.listening ? "secondary" : "primary";
    $("upload-mode").textContent = state.config.uploads_enabled
      ? "Automatic"
      : "Capture only";
    $("upload-hint").textContent = state.config.uploads_enabled
      ? state.config.clublog_enabled
        ? "CloudLog and Club Log enabled"
        : "CloudLog enabled"
      : "Outbox paused; new packets captured only";
    $("uploaded").textContent = state.counts.uploaded || 0;
    $("delivery-counts").textContent =
      `CloudLog ${state.service_counts.cloudlog.uploaded || 0} · Club Log ${state.service_counts.clublog.uploaded || 0}`;
    $("clublog-key-warning").hidden = !state.config.clublog_key_format_warning;
    $("clublog-summary").textContent = state.config.clublog_enabled
      ? "Club Log enabled. Automatic uploads must also be on. Switching Club Log off pauses its queued uploads."
      : "Club Log is off; existing CloudLog uploads continue.";
    $("clublog-pause").hidden = !state.clublog_block;
    if (state.clublog_block) {
      $("clublog-pause-text").textContent =
        "Club Log uploads paused. " +
        state.clublog_block.detail +
        (state.clublog_block.auth
          ? " Correct and save your Club Log credentials, then retry the failed delivery."
          : " Resolve the error, resume Club Log, then retry failed deliveries. Backlog uploads may wait up to five minutes.");
      $("clublog-resume").hidden = state.clublog_block.auth;
    }
    $("pending").textContent =
      (state.counts.queued || 0) +
      (state.counts.retry || 0) +
      (state.counts.uploading || 0);
    $("failed").textContent =
      `${state.counts.failed || 0} failed · ${state.counts.accepted || 0} accepted; verify`;
    $("listener-error").hidden = !state.listener_error;
    $("listener-error").textContent = state.listener_error;
    $("held").hidden = !state.held;
    $("held").textContent =
      `${state.held} upload(s) belong to another destination. Restore their original server/account and station/callsign settings to retry them.`;
    renderEvents(state.events);
    renderJobs(state.jobs);
  } catch (error) {
    $("connection").textContent = "Local service disconnected";
    $("connection").className = "pill";
    notice(error.message + ". Check the console app is running.", true);
  }
}
async function action(button, callback) {
  button.disabled = true;
  try {
    await callback();
    await refresh();
  } catch (error) {
    notice(error.message, true);
  } finally {
    button.disabled = false;
  }
}
$("settings").addEventListener("input", () => {
  dirty = true;
  $("saved").textContent = "Unsaved changes";
});
$("settings").addEventListener("submit", (event) => {
  event.preventDefault();
  action(event.submitter, async () => {
    const body = {};
    for (const key of [
      "udp_host",
      "udp_port",
      "cloudlog_url",
      "api_key",
      "station_id",
      "clublog_email",
      "clublog_callsign",
      "clublog_password",
      "clublog_api_key",
    ])
      body[key] = $(key).value;
    body.clublog_enabled = $("clublog_enabled").checked;
    for (const name of ["clublog_password", "clublog_api_key"])
      body["clear_" + name] = $("clear_" + name).checked;
    body.uploads_enabled = $("uploads_enabled").checked;
    body.clear_api_key = $("clear_api_key").checked;
    await api("config", body);
    const updated = await api("state");
    populate(updated.config);
    notice(
      body.uploads_enabled
        ? "Settings saved. New QSOs will upload automatically."
        : "Settings saved. Capture-only mode is active; the outbox is paused.",
    );
  });
});
$("toggle").addEventListener("click", (event) =>
  action(event.currentTarget, async () => {
    if (dirty)
      throw new Error("Save your settings before changing the listener state");
    const stopping = state.listening;
    await api(stopping ? "stop" : "start", {});
    notice(
      stopping
        ? "Listener stopped. The outbox continues if automatic uploads are enabled."
        : "Listening for SDR-Control broadcasts.",
    );
  }),
);
$("load-stations").addEventListener("click", (event) =>
  action(event.currentTarget, async () => {
    if (dirty)
      throw new Error(
        "Save the URL and API key before checking the connection",
      );
    const result = await api("stations", {});
    $("stations").replaceChildren(element("option", "Choose a station"));
    $("stations").firstChild.value = "";
    for (const station of result.stations) {
      const option = element(
        "option",
        `${station.station_id} · ${station.station_profile_name} · ${station.station_callsign}`,
      );
      option.value = station.station_id;
      $("stations").append(option);
    }
    $("stations").hidden = false;
    notice(
      `Read/write key verified. ${result.stations.length} station profile(s) found.`,
    );
  }),
);
$("stations").addEventListener("change", (event) => {
  if (event.target.value) {
    $("station_id").value = event.target.value;
    dirty = true;
    $("saved").textContent = "Unsaved changes";
  }
});
$("clublog-resume").addEventListener("click", (event) =>
  action(event.currentTarget, async () => {
    await api("clublog/resume", {});
    notice("Club Log resumed. Retry any failed deliveries separately.");
  }),
);
async function poll() {
  await refresh();
  setTimeout(poll, 1000);
}
poll();
