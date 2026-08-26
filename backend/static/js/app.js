/* app.js
 * ========
 * Single-page dashboard. No build step, no framework - plain DOM + fetch +
 * socket.io, matching the rest of this stack's "no unnecessary moving
 * parts" philosophy. The browser only ever talks to our Flask backend
 * (REST + WebSocket) - never to Redfish/BMCs directly.
 */

const CATEGORY_META = {
  battery:   { icon: "fa-car-battery",        label: "Battery" },
  chassis:   { icon: "fa-cube",               label: "Chassis" },
  fans:      { icon: "fa-fan",                label: "Fans" },
  memory:    { icon: "fa-memory",             label: "Memory" },
  processor: { icon: "fa-microchip",          label: "Processor" },
  storage:   { icon: "fa-database",           label: "Storage" },
  power:     { icon: "fa-plug",               label: "Power" },
  thermal:   { icon: "fa-temperature-half",   label: "Thermal" },
  voltage:   { icon: "fa-bolt",               label: "Voltage" },
  network:   { icon: "fa-network-wired",      label: "Network" },
  pcie:      { icon: "fa-layer-group",        label: "PCI / Cables" },
  firmware:  { icon: "fa-code-branch",        label: "Firmware" },
  security:  { icon: "fa-shield-halved",      label: "Security" },
};
const CATEGORY_ORDER = Object.keys(CATEGORY_META);
// ---------------------------------------------------------------------
// Storage property display: friendly labels + value formatting.
// The storage collector merges the full raw Redfish drive resource with
// a handful of new extracted fields (see storage.py), so buildPropGrid
// needs to know how to relabel/format the ones that would otherwise
// leak through as raw snake_case/PascalCase keys.
// ---------------------------------------------------------------------
const STORAGE_PROPERTY_LABELS = {
  // Newly added fields (storage.py _extract_additional_drive_properties)
  device_description: "Device Description",
  predictive_failure: "Predictive Failure",
  block_size_bytes:   "Block Size",
  product_id:         "Product ID",
  controller:         "Controller",
  // Standard Redfish drive fields that were already being collected but
  // rendered with their raw PascalCase names
  SerialNumber:       "Serial Number",
  PartNumber:         "Part Number",
  CapacityBytes:      "Capacity",
  Model:              "Model",
  Manufacturer:       "Manufacturer",
  Protocol:           "Protocol",
  MediaType:          "Media Type",
  FirmwareVersion:    "Firmware Version",
  RotationSpeedRPM:   "Rotation Speed (RPM)",
  PredictedMediaLifeLeftPercent: "Predicted Life Left",
  PowerOnHours:       "Power On Hours",
  CapableSpeedGbs:    "Capable Speed (Gb/s)",
};

// storage.py merges `extra` on top of the raw drive body, so the raw
// standard-Redfish field and the new snake_case field can both be
// present with the same value. Hide the raw one so it isn't shown twice.
const STORAGE_SUPERSEDED_KEYS = {
  FailurePredicted: "predictive_failure",
  BlockSizeBytes:   "block_size_bytes",
};

const STORAGE_BYTE_SUFFIX_KEYS = new Set(["block_size_bytes"]);

function formatStorageValue(key, value) {
  if (typeof value === "boolean") return value ? "Yes" : "No";
  if (STORAGE_BYTE_SUFFIX_KEYS.has(key) &&
      (typeof value === "number" || /^\d+$/.test(String(value)))) {
    return `${value} bytes`;
  }
  return String(value);
}

// The 9 curated fields for the Storage drive Summary view (iDRAC-style
// "Advanced Properties" subset). Order here = display order.
const STORAGE_SUMMARY_FIELDS = [
  ["device_description", "Device Description"],
  ["Manufacturer",       "Manufacturer"],
  ["product_id",         "Product ID"],
  ["SerialNumber",       "Serial Number"],
  ["PartNumber",         "Part Number"],
  ["CapacityBytes",      "Capacity"],
  ["block_size_bytes",   "Block Size"],
  ["controller",         "Controller"],
  ["predictive_failure", "Predictive Failure"],
];

// Everything already shown in Summary (plus raw duplicate/fallback keys
// like FailurePredicted, BlockSizeBytes, Model, SKU) must NOT reappear
// in Advanced Details.
const STORAGE_SUMMARY_SKIP_KEYS = new Set([
  ...STORAGE_SUMMARY_FIELDS.map(([k]) => k),
  ...Object.keys(STORAGE_SUPERSEDED_KEYS), // FailurePredicted, BlockSizeBytes
  "Model", "SKU",                          // folded into product_id already
]);

// Advanced Details skip set: hide only @odata.*, Links, and boilerplate —
// deliberately do NOT skip "Oem" here, so Dell/HPE/Lenovo/Cisco vendor
// blocks fall through into Advanced automatically, with no per-vendor code.
const STORAGE_ADVANCED_SKIP_KEYS = new Set([
  "@odata.context", "@odata.etag", "@odata.id", "@odata.type",
  "Id", "Name", "Description", "Links", "Actions", "Assembly",
  ...STORAGE_SUMMARY_SKIP_KEYS,
]);



const HISTORY_METRICS = {
  thermal:   [["temperature", "Temperature (C)"]],
  power:     [["power_consumption", "Power (W)"], ["psu_wattage", "PSU Output (W)"]],
  fans:      [["fan_rpm", "Fan RPM"]],
  voltage:   [["voltage", "Voltage (V)"]],
  memory:    [["memory_errors", "Memory Errors"], ["memory_temperature", "DIMM Temp (C)"]],
  storage:   [["disk_wear", "Disk Wear (%)"], ["disk_temperature", "Disk Temp (C)"]],
  processor: [["cpu_temperature", "CPU Temp (C)"]],
};

// Human-readable labels for connection status enum values
const CONNECTION_STATUS_LABELS = {
  connected:   "Connected",
  auth_failed: "Authentication Failed",
  unreachable: "Unreachable",
  unknown:     "Unknown",
};

function formatConnectionStatus(raw) {
  return CONNECTION_STATUS_LABELS[raw] || raw || "Unknown";
}

// Translate raw last_poll_error into a user-friendly message
function formatPollError(error, connectionStatus) {
  if (!error) {
    if (connectionStatus === "auth_failed") return "Invalid username or password, or the BMC rejected the session.";
    if (connectionStatus === "unreachable") return "Unable to reach the BMC. Check the IP address and network connectivity.";
    return null;
  }
  const e = error.toLowerCase();
  if (e.includes("decrypt"))           return "Encryption key changed — re-add this server with its BMC password.";
  if (e.includes("authentication"))    return "Invalid username or password.";
  if (e.includes("timeout"))           return "Connection timed out — the BMC may be slow or unreachable.";
  if (e.includes("unreachable") || e.includes("connect")) return "Unable to reach the BMC — check network connectivity.";
  if (e.includes("ssl") || e.includes("tls") || e.includes("certificate")) return "SSL/TLS certificate error.";
  if (e.includes("refused"))           return "Connection refused — the Redfish service may not be running.";
  if (e.includes("500"))               return "Internal server error on the BMC.";
  return error;
}

let state = {
  currentView: "fleet",              // "fleet" or "server"
  servers: [],
  selectedServerId: null,
  categoryComponents: {},            // category -> latest components array (for the detail popup)
  openCategoryModal: null,           // which category's popup is currently open, or null
  openCards: new Set(),              // all categories collapsed by default
  openComponents: new Set(),         // odata_ids of expanded component items (within a popup)
  alerts: [],
  charts: {},                        // category -> Chart.js instance
  view: "overview",                  // 'overview' | 'nodes' | 'alerts' | 'server'
  nodesFilter: { search: "", tab: "all" },
  alertsFilter: "all",               // 'all' | 'critical' | 'warning' | 'info'
  storageDevices: [],
  selectedStorageDeviceId: null,
};

const SEVERITY_ORDER = ["critical", "warning", "info"];
const SEVERITY_META = {
  critical: { color: "var(--crit)", label: "Critical" },
  warning:  { color: "var(--warn)", label: "Warning" },
  info:     { color: "var(--accent)", label: "Info" },
};

const $ = (sel) => document.querySelector(sel);
const $all = (sel) => Array.from(document.querySelectorAll(sel));

// ---------------------------------------------------------------------
// Edit server modal
// ---------------------------------------------------------------------
let editServerId = null;

function openEditServerModal(server) {
  editServerId = server.id;
  $("#e_display_name").value = server.display_name || server.hostname;
  $("#e_username").value = server.username || "";
  $("#e_password").value = "";
  $("#e_protocol").value = server.management_protocol || "redfish";
  $("#e_site_id").value = server.site_id || "";
  $("#e_agent_id").value = server.agent_id || "";
  $("#e_customer_name").value = server.customer_name || "";
  $("#e_customer_location").value = server.customer_location || "";
  $("#e_maintenance_records").value = server.maintenance_records || "";
  $("#e_interval").value = server.polling_interval_seconds || 30;
  $("#editServerError").style.display = "none";
  $("#editServerModal").classList.add("open");
}

function wireEditServerModal() {
  const modal = $("#editServerModal");
  $("#cancelEditServer").addEventListener("click", () => modal.classList.remove("open"));
  
  $("#submitEditServer").addEventListener("click", async () => {
    const errorEl = $("#editServerError");
    errorEl.style.display = "none";
    const payload = {
      display_name: $("#e_display_name").value.trim(),
      username: $("#e_username").value.trim(),
      management_protocol: $("#e_protocol").value,
      polling_interval_seconds: parseInt($("#e_interval").value, 10) || 30,
      customer_name: $("#e_customer_name").value.trim(),
      customer_location: $("#e_customer_location").value.trim(),
      maintenance_records: $("#e_maintenance_records").value.trim(),
    };
    const pwd = $("#e_password").value;
    if (pwd) payload.password = pwd;
    const sid = $("#e_site_id").value.trim();
    if (sid) payload.site_id = sid;
    const aid = $("#e_agent_id").value.trim();
    if (aid) payload.agent_id = aid;
    
    try {
      await api(`/api/servers/${editServerId}`, { method: "PATCH", body: JSON.stringify(payload) });
      modal.classList.remove("open");
      await loadServers();
      if (state.selectedServerId === editServerId) {
        await api(`/api/servers/${editServerId}/poll-now`, { method: "POST" });
        toast("Server details updated - checking connection...");
      } else {
        toast("Server updated");
      }
    } catch (e) {
      errorEl.textContent = e.message;
      errorEl.style.display = "block";
    }
  });

  $("#deleteServerBtn").addEventListener("click", async () => {
    if (!confirm("Are you sure you want to remove this server?")) return;
    try {
      await api(`/api/servers/${editServerId}`, { method: "DELETE" });
      modal.classList.remove("open");
      const wasOpenServer = state.selectedServerId === editServerId;
      if (wasOpenServer) state.selectedServerId = null;
      await loadServers();
      if (wasOpenServer) setView("nodes");
      toast("Server removed");
    } catch (e) {
      const errorEl = $("#editServerError");
      errorEl.textContent = e.message;
      errorEl.style.display = "block";
    }
  });
}

// ---------------------------------------------------------------------
// API helpers
// ---------------------------------------------------------------------
async function api(path, opts = {}) {
  const resp = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  if (!resp.ok) {
    let msg = resp.statusText;
    try { const body = await resp.json(); if (body.error) msg = body.error; } catch (_) {}
    throw new Error(msg);
  }
  if (resp.status === 204) return null;
  return resp.json();
}

// ---------------------------------------------------------------------
// Toasts
// ---------------------------------------------------------------------
function toast(message) {
  const el = document.createElement("div");
  el.className = "toast";
  el.textContent = message;
  $("#toastContainer").appendChild(el);
  setTimeout(() => el.remove(), 4000);
}

// ---------------------------------------------------------------------
// Health / status pill helpers
// ---------------------------------------------------------------------
function healthClass(h) {
  if (h === "OK") return "pill-ok";
  if (h === "Warning") return "pill-warn";
  if (h === "Critical") return "pill-crit";
  return "pill-unknown";
}
function healthDotColor(h) {
  if (h === "OK") return "var(--ok)";
  if (h === "Warning") return "var(--warn)";
  if (h === "Critical") return "var(--crit)";
  return "var(--unknown)";
}
function connDotColor(status) {
  if (status === "connected") return "var(--ok)";
  if (status === "auth_failed" || status === "unreachable") return "var(--crit)";
  return "var(--unknown)";
}

// ---------------------------------------------------------------------
// Servers data load (used by all views)
// ---------------------------------------------------------------------
async function loadServers() {
  state.servers = await api("/api/servers");
  renderNavCounts();
  if (state.view === "overview") renderOverviewView();
  if (state.view === "nodes") updateNodesTable();
}

function renderNavCounts() {
  const nodeCountEl = $("#navNodeCount");
  if (nodeCountEl) nodeCountEl.textContent = state.servers.length || "";
}

function healthBucket(healthStatus) {
  if (healthStatus === "OK") return "ok";
  if (healthStatus === "Warning") return "warn";
  if (healthStatus === "Critical") return "crit";
  return "unknown";
}

// ---------------------------------------------------------------------
// View router
// ---------------------------------------------------------------------
function setView(view) {
  if (state.view !== "server" && view !== "server" && state.selectedServerId) {
    // leaving a server detail view (not just switching within it) - stop
    // getting live component updates for a server we're no longer looking at
  }
  if (view !== "server" && state.selectedServerId) {
    socket.emit("unsubscribe_server", { server_id: state.selectedServerId });
    state.selectedServerId = null;
    closeCategoryModal();
  }
  state.view = view;
  $all(".nav-item").forEach((el) => el.classList.toggle("active", el.dataset.view === view));
  if (view === "overview") renderOverviewView();
  else if (view === "nodes") renderNodesView();
  else if (view === "alerts") renderAlertsView();
  else if (view === "storage-devices") renderStorageDevicesView();
}

function wireNav() {
  $all(".nav-item").forEach((el) => {
    el.addEventListener("click", () => setView(el.dataset.view));
  });
}

// ---------------------------------------------------------------------
// Fleet Overview
// ---------------------------------------------------------------------
function renderOverviewView() {
  const main = $("#main");
  const counts = { ok: 0, warn: 0, crit: 0, unknown: 0 };
  for (const s of state.servers) counts[healthBucket(s.health_status)]++;

  const recentAlerts = [...state.alerts]
    .sort((a, b) => new Date(b.last_occurred_at) - new Date(a.last_occurred_at))
    .slice(0, 6);

  const vendorGroups = {};
  for (const s of state.servers) {
    const key = s.vendor || "Unknown Vendor";
    (vendorGroups[key] = vendorGroups[key] || []).push(s);
  }

  main.innerHTML = `
    <div class="view-header">
      <div>
        <h1>Fleet Overview</h1>
        <div class="sub">${state.servers.length} node${state.servers.length === 1 ? "" : "s"} monitored</div>
      </div>
    </div>
    <div class="stats-strip">
      <div class="stat-card"><div class="label">Total Nodes</div><div class="value">${state.servers.length}</div></div>
      <div class="stat-card ok"><div class="label">Healthy</div><div class="value">${counts.ok}</div></div>
      <div class="stat-card warn"><div class="label">Warning</div><div class="value">${counts.warn}</div></div>
      <div class="stat-card crit"><div class="label">Critical</div><div class="value">${counts.crit}</div></div>
    </div>
    <div class="overview-grid">
      <div class="panel-box">
        <div class="panel-box-header">
          <span>Recent Alerts</span>
          <a href="#" id="viewAllAlertsLink">View all &rarr;</a>
        </div>
        <div class="panel-box-body" id="overviewAlertsPreview"></div>
      </div>
      <div class="panel-box">
        <div class="panel-box-header"><span>Vendor Health</span></div>
        <div class="panel-box-body" id="overviewVendorMatrix"></div>
      </div>
    </div>
  `;

  $("#viewAllAlertsLink").addEventListener("click", (e) => { e.preventDefault(); setView("alerts"); });

  const previewEl = $("#overviewAlertsPreview");
  if (recentAlerts.length === 0) {
    previewEl.innerHTML = `<div class="panel-box-empty">No open alerts.</div>`;
  } else {
    previewEl.innerHTML = "";
    for (const a of recentAlerts) {
      const server = state.servers.find((s) => s.id === a.server_id);
      const sev = SEVERITY_META[a.severity] || SEVERITY_META.info;
      const el = document.createElement("div");
      el.className = "preview-alert-item";
      el.innerHTML = `
        <span class="sev-dot" style="background:${sev.color}"></span>
        <div class="txt">
          <div class="msg">${escapeHtml(server ? (server.display_name || server.hostname) : a.server_id)} &middot; ${escapeHtml(a.message)}</div>
          <div class="meta">${escapeHtml(a.category)} &middot; ${timeAgoOrLocal(a.last_occurred_at)}</div>
        </div>
      `;
      el.addEventListener("click", () => setView("alerts"));
      previewEl.appendChild(el);
    }
  }

  const vendorEl = $("#overviewVendorMatrix");
  const vendorNames = Object.keys(vendorGroups).sort();
  if (vendorNames.length === 0) {
    vendorEl.innerHTML = `<div class="panel-box-empty">No nodes yet.</div>`;
  } else {
    vendorEl.innerHTML = "";
    for (const name of vendorNames) {
      const servers = vendorGroups[name];
      const c = { ok: 0, warn: 0, crit: 0, unknown: 0 };
      for (const s of servers) c[healthBucket(s.health_status)]++;
      const card = document.createElement("div");
      card.className = "vendor-card";
      card.innerHTML = `
        <div class="vendor-card-top">
          <span class="name">${escapeHtml(name)}</span>
          <span class="count">${servers.length} node${servers.length === 1 ? "" : "s"}</span>
        </div>
        <div class="health-bar">
          ${servers.map((s) => `<span class="seg ${healthBucket(s.health_status)}" title="${escapeHtml(s.display_name || s.hostname)}"></span>`).join("")}
        </div>
        <div class="vendor-card-counts">
          <span><span class="dot" style="background:var(--ok)"></span>${c.ok}</span>
          <span><span class="dot" style="background:var(--warn)"></span>${c.warn}</span>
          <span><span class="dot" style="background:var(--crit)"></span>${c.crit}</span>
        </div>
      `;
      vendorEl.appendChild(card);
    }
  }
}

// ---------------------------------------------------------------------
// Nodes page (replaces the old always-visible sidebar server list)
// ---------------------------------------------------------------------
function renderNodesView() {
  const main = $("#main");
  main.innerHTML = `
    <div class="view-header">
      <div>
        <h1>Nodes</h1>
        <div class="sub" id="nodesSubCount"></div>
      </div>
      <button class="btn-primary" id="addServerBtn"><i class="fa-solid fa-plus"></i> Add server</button>
    </div>
    <div class="nodes-toolbar">
      <input type="text" id="nodeSearch" placeholder="Search by name or IP address..." autocomplete="off" value="${escapeHtml(state.nodesFilter.search)}">
    </div>
    <div class="filter-tabs" id="nodeFilterTabs"></div>
    <div class="nodes-table">
      <div class="nodes-table-head">
        <span>Status</span><span>Node</span><span>Vendor / Model</span>
        <span>Power</span><span>Connection</span><span>Last Updated</span>
      </div>
      <div id="nodesTableBody"></div>
    </div>
  `;
  $("#addServerBtn").addEventListener("click", () => $("#addServerModal").classList.add("open"));
  $("#nodeSearch").addEventListener("input", (e) => {
    state.nodesFilter.search = e.target.value;
    updateNodesTable();
  });
  updateNodesTable();
}

function updateNodesTable() {
  if (state.view !== "nodes") return;
  const counts = { all: state.servers.length, ok: 0, warn: 0, crit: 0, unreachable: 0 };
  for (const s of state.servers) {
    const b = healthBucket(s.health_status);
    if (b === "ok") counts.ok++;
    else if (b === "warn") counts.warn++;
    else if (b === "crit") counts.crit++;
    if (s.connection_status === "unreachable" || s.connection_status === "auth_failed") counts.unreachable++;
  }

  const tabs = [
    ["all", `All (${counts.all})`],
    ["crit", `Critical (${counts.crit})`],
    ["warn", `Warning (${counts.warn})`],
    ["ok", `Healthy (${counts.ok})`],
    ["unreachable", `Unreachable (${counts.unreachable})`],
  ];
  const tabsEl = $("#nodeFilterTabs");
  tabsEl.innerHTML = tabs.map(([key, label]) =>
    `<button class="filter-tab${state.nodesFilter.tab === key ? " active" : ""}" data-tab="${key}">${label}</button>`
  ).join("");
  $all(".filter-tab", tabsEl).forEach((btn) => {
    btn.addEventListener("click", () => {
      state.nodesFilter.tab = btn.dataset.tab;
      updateNodesTable();
    });
  });

  const search = state.nodesFilter.search.toLowerCase();
  let filtered = state.servers.filter((s) =>
    ((s.display_name || s.hostname || "") + " " + (s.ip_address || "")).toLowerCase().includes(search)
  );
  if (state.nodesFilter.tab === "crit") filtered = filtered.filter((s) => healthBucket(s.health_status) === "crit");
  else if (state.nodesFilter.tab === "warn") filtered = filtered.filter((s) => healthBucket(s.health_status) === "warn");
  else if (state.nodesFilter.tab === "ok") filtered = filtered.filter((s) => healthBucket(s.health_status) === "ok");
  else if (state.nodesFilter.tab === "unreachable") filtered = filtered.filter((s) => s.connection_status === "unreachable" || s.connection_status === "auth_failed");

  $("#nodesSubCount").textContent = `${filtered.length} of ${state.servers.length} nodes shown`;

  const body = $("#nodesTableBody");
  if (filtered.length === 0) {
    body.innerHTML = `<div class="sidebar-empty">No nodes ${state.servers.length ? "match your search/filter" : "yet. Click \u201cAdd server\u201d to add one."}</div>`;
    return;
  }
  body.innerHTML = "";
  for (const s of filtered) {
    const row = document.createElement("div");
    row.className = "node-row";
    row.innerHTML = `
      <div class="status-cell">
        <span class="health-dot" style="background:${healthDotColor(s.health_status)};width:8px;height:8px;border-radius:50%;display:inline-block;"></span>
        ${escapeHtml(s.health_status || "Unknown")}
      </div>
      <div class="name-cell">
        <div class="name">${escapeHtml(s.display_name || s.hostname)}</div>
        <div class="ip">${escapeHtml(s.ip_address)}</div>
      </div>
      <div class="vendor-cell">${escapeHtml(s.vendor || "Unknown")} ${escapeHtml(s.model || "")}</div>
      <div class="power-cell"><i class="fa-solid ${s.power_state === 'On' ? 'fa-power-off' : 'fa-circle-stop'}"></i> ${escapeHtml(s.power_state || "Unknown")}</div>
      <div class="conn-cell"><span class="pill ${s.connection_status === 'connected' ? 'pill-ok' : 'pill-crit'}"><span class="dot" style="background:${connDotColor(s.connection_status)}"></span>${formatConnectionStatus(s.connection_status)}</span></div>
      <div class="updated-cell">${s.last_successful_poll ? timeAgoOrLocal(s.last_successful_poll) : "never"}</div>
    `;
    row.addEventListener("click", () => selectServer(s.id));
    body.appendChild(row);
  }
}

// ---------------------------------------------------------------------
// Storage Devices (standalone arrays - HPE MSA etc, not server BMCs)
// ---------------------------------------------------------------------
async function loadStorageDevices() {
  state.storageDevices = await api("/api/storage-devices");
  const countEl = $("#navStorageDeviceCount");
  if (countEl) countEl.textContent = state.storageDevices.length || "";
  if (state.view === "storage-devices" && !state.selectedStorageDeviceId) updateStorageDevicesTable();
}

function renderStorageDevicesView() {
  const main = $("#main");
  main.innerHTML = `
    <div class="view-header">
      <div>
        <h1>Storage Devices</h1>
        <div class="sub" id="storageDevicesSubCount"></div>
      </div>
      <button class="btn-primary" id="addStorageDeviceBtn"><i class="fa-solid fa-plus"></i> Add storage device</button>
    </div>
    <div class="nodes-toolbar">
      <input type="text" id="storageDeviceSearch" placeholder="Search by name or IP address..." autocomplete="off">
    </div>
    <div class="nodes-table">
      <div class="nodes-table-head">
        <span>Status</span><span>Node</span><span>Vendor / Model</span>
        <span></span><span>Connection</span><span>Last Updated</span>
      </div>
      <div id="storageDevicesTableBody"></div>
    </div>
  `;
  $("#addStorageDeviceBtn").addEventListener("click", () => openAddStorageDeviceModal());
  $("#storageDeviceSearch").addEventListener("input", () => updateStorageDevicesTable());
  updateStorageDevicesTable();
}

function updateStorageDevicesTable() {
  if (state.view !== "storage-devices") return;
  const search = ($("#storageDeviceSearch")?.value || "").toLowerCase();
  const filtered = state.storageDevices.filter((d) =>
    ((d.display_name || d.hostname || "") + " " + (d.ip_address || "")).toLowerCase().includes(search)
  );

  $("#storageDevicesSubCount").textContent = `${filtered.length} of ${state.storageDevices.length} devices shown`;

  const body = $("#storageDevicesTableBody");
  if (filtered.length === 0) {
    body.innerHTML = `<div class="sidebar-empty">No storage devices ${state.storageDevices.length ? "match your search" : 'yet. Click "Add storage device" to add one.'}</div>`;
    return;
  }
  body.innerHTML = "";
  for (const d of filtered) {
    const row = document.createElement("div");
    row.className = "node-row";
    row.innerHTML = `
      <div class="status-cell">
        <span class="health-dot" style="background:${healthDotColor(d.health_status)};width:8px;height:8px;border-radius:50%;display:inline-block;"></span>
        ${escapeHtml(d.health_status || "Unknown")}
      </div>
      <div class="name-cell">
        <div class="name">${escapeHtml(d.display_name || d.hostname)}</div>
        <div class="ip">${escapeHtml(d.ip_address)}</div>
      </div>
      <div class="vendor-cell">${escapeHtml(d.vendor || "Unknown")} ${escapeHtml(d.model || "")}</div>
      <div></div>
      <div class="conn-cell"><span class="pill ${d.connection_status === 'connected' ? 'pill-ok' : 'pill-crit'}"><span class="dot" style="background:${connDotColor(d.connection_status)}"></span>${formatConnectionStatus(d.connection_status)}</span></div>
      <div class="updated-cell">${d.last_successful_poll ? timeAgoOrLocal(d.last_successful_poll) : "never"}</div>
    `;
    row.addEventListener("click", () => selectStorageDevice(d.id));
    body.appendChild(row);
  }
}

async function selectStorageDevice(deviceId) {
  state.selectedStorageDeviceId = deviceId;
  state.view = "storage-device-detail";
  await renderStorageDeviceDetail();
}

async function renderStorageDeviceDetail() {
  const main = $("#main");
  const device = await api(`/api/storage-devices/${state.selectedStorageDeviceId}`);
  const componentsByCategory = await api(`/api/storage-devices/${state.selectedStorageDeviceId}/components`);

  main.innerHTML = `
    <div class="back-link" id="backToStorageDevicesLink"><i class="fa-solid fa-arrow-left"></i> Back to Storage Devices</div>
    <div class="server-header" id="storageDeviceHeader"></div>
    <div class="cards-grid" id="storageDeviceCardsGrid"></div>
  `;
  $("#backToStorageDevicesLink").addEventListener("click", () => {
    state.selectedStorageDeviceId = null;
    setView("storage-devices");
  });

  const header = $("#storageDeviceHeader");
  header.innerHTML = `
    <div class="server-header-top">
      <div>
        <h1>${escapeHtml(device.display_name || device.hostname)}</h1>
        <div class="sub">${escapeHtml(device.ip_address)} &middot; ${escapeHtml(device.vendor || "unknown vendor")} ${escapeHtml(device.model || "")}</div>
      </div>
      <span class="pill ${healthClass(device.health_status)}"><span class="dot" style="background:${healthDotColor(device.health_status)}"></span>${device.health_status || "Unknown"}</span>
      <span class="pill ${device.connection_status === 'connected' ? 'pill-ok' : 'pill-crit'}"><span class="dot" style="background:${connDotColor(device.connection_status)}"></span>${formatConnectionStatus(device.connection_status)}</span>
    </div>
    <div class="header-stats">
      <div class="stat"><div class="label">Firmware</div><div class="value">${escapeHtml(device.firmware_version || "-")}</div></div>
      <div class="stat"><div class="label">Serial Number</div><div class="value">${escapeHtml(device.serial_number || "-")}</div></div>
      <div class="stat"><div class="label">Device ID</div><div class="value" style="font-family:monospace;">${device.id}</div></div>
      <div class="stat"><div class="label">Last Updated</div><div class="value">${device.last_successful_poll ? timeAgoOrLocal(device.last_successful_poll) : "never"}</div></div>
    </div>
  `;

    const STORAGE_CATEGORY_META = {
    storage_device_disk:       { icon: "fa-hard-drive", label: "Disks" },
    storage_device_controller: { icon: "fa-microchip",  label: "Controllers" },
    storage_device_power:      { icon: "fa-plug",        label: "Power" },
    storage_device_fan:        { icon: "fa-fan",         label: "Fans" },
    storage_device_port:       { icon: "fa-ethernet",   label: "Ports" },
    storage_device_enclosure:  { icon: "fa-server",     label: "Enclosure" },
  };

  const grid = $("#storageDeviceCardsGrid");
  grid.innerHTML = "";
  for (const [category, meta] of Object.entries(STORAGE_CATEGORY_META)) {
    const comps = componentsByCategory[category] || [];
    const card = document.createElement("div");
    card.className = "card";
    const worst = worstHealth(comps);
    card.innerHTML = `
      <div class="card-header">
        <i class="fa-solid ${meta.icon} icon"></i>
        <span class="title">${meta.label}</span>
        ${worst ? `<span class="dot" style="width:8px;height:8px;border-radius:50%;background:${healthDotColor(worst)}"></span>` : ""}
        <span class="count">${comps.length || "–"}</span>
        <i class="fa-solid fa-chevron-right chevron"></i>
      </div>
    `;
    card.querySelector(".card-header").addEventListener("click", () => openStorageCategoryModal(category, meta, comps));
    grid.appendChild(card);
  }
}

function openStorageCategoryModal(category, meta, comps) {
  state.openCategoryModal = category;
  $("#categoryModalIcon").className = `fa-solid ${meta.icon}`;
  $("#categoryModalTitle").textContent = meta.label;
  $("#categoryModalCount").textContent = comps.length ? `${comps.length} item${comps.length === 1 ? "" : "s"}` : "";
  renderCategoryBody($("#categoryModalBody"), category, comps);
  $("#categoryModalOverlay").classList.add("open");
}

function openAddStorageDeviceModal() {
  const hostname = prompt("Hostname / display name:");
  if (!hostname) return;
  const ip_address = prompt("IP address:");
  if (!ip_address) return;
  const username = prompt("Username:");
  const password = prompt("Password:");
  api("/api/storage-devices", {
    method: "POST",
    body: JSON.stringify({ hostname, ip_address, username, password }),
  }).then(() => {
    loadStorageDevices();
    updateStorageDevicesTable();
  }).catch((e) => toast(`Failed to add storage device: ${e.message || e}`));
}

function timeAgoOrLocal(isoString) {
  const withZ = isoString + (isoString.endsWith("Z") ? "" : "Z");
  return new Date(withZ).toLocaleString("en-IN", { timeZone: "Asia/Kolkata" });
}

function startClock() {
  const tick = () => {
    const el = $("#sysTime");
    if (el) el.textContent = new Date().toLocaleTimeString("en-IN", { timeZone: "Asia/Kolkata", hour12: false });
  };
  tick();
  setInterval(tick, 1000);
}

function escapeHtml(str) {
  if (str === null || str === undefined) return "";
  return String(str).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// ---------------------------------------------------------------------
// Server selection + header + cards
// ---------------------------------------------------------------------
async function selectServer(serverId) {
  if (state.selectedServerId && state.selectedServerId !== serverId) {
    socket.emit("unsubscribe_server", { server_id: state.selectedServerId });
  }
  closeCategoryModal();
  state.selectedServerId = serverId;
  state.view = "server";
  $all(".nav-item").forEach((el) => el.classList.toggle("active", el.dataset.view === "nodes"));
  socket.emit("subscribe_server", { server_id: serverId });
  await renderMain();
}

async function renderMain() {
  const main = $("#main");
  const server = await api(`/api/servers/${state.selectedServerId}`);
  const componentsByCategory = await api(`/api/servers/${state.selectedServerId}/components`);

  main.innerHTML = `
    <div class="back-link" id="backToNodesLink"><i class="fa-solid fa-arrow-left"></i> Back to Nodes</div>
    <div class="component-search-wrap">
      <i class="fa-solid fa-magnifying-glass component-search-icon"></i>
      <input type="text" id="componentSearch" class="component-search-input" placeholder="Search across Battery, Chassis, Fans, Memory, Storage..." autocomplete="off">
      <button class="icon-btn component-search-clear" id="componentSearchClear" style="display:none;"><i class="fa-solid fa-xmark"></i></button>
      <div class="component-search-results" id="componentSearchResults" style="display:none;"></div>
    </div>
    <div class="server-header" id="serverHeader"></div>
    <div class="cards-grid" id="cardsGrid"></div>
  `;
  $("#backToNodesLink").addEventListener("click", () => setView("nodes"));
  wireComponentSearch();
  renderHeader(server);
  renderCards(componentsByCategory, server);
}

// ---------------------------------------------------------------------
// Component search: typeahead across every category card for the
// currently open server (Battery, Chassis, Fans, ...), not just the one
// popup you happen to have open. Picking a result opens that category's
// popup and jumps straight to (and expands) the matching component.
// ---------------------------------------------------------------------
function wireComponentSearch() {
  const input = $("#componentSearch");
  const clearBtn = $("#componentSearchClear");
  const results = $("#componentSearchResults");

  input.addEventListener("input", () => {
    clearBtn.style.display = input.value ? "flex" : "none";
    updateComponentSearchResults(input.value);
  });
  clearBtn.addEventListener("click", () => {
    input.value = "";
    clearBtn.style.display = "none";
    results.style.display = "none";
    input.focus();
  });
  document.addEventListener("click", (e) => {
    if (!e.target.closest(".component-search-wrap")) results.style.display = "none";
  });
  input.addEventListener("focus", () => {
    if (input.value) updateComponentSearchResults(input.value);
  });
}

function updateComponentSearchResults(query) {
  const results = $("#componentSearchResults");
  const q = query.trim().toLowerCase();
  if (!q) { results.style.display = "none"; return; }

  const matches = [];
  for (const category of CATEGORY_ORDER) {
    const comps = (state.categoryComponents[category] || []).filter((c) => c.odata_id !== "meta:unsupported");
    for (const c of comps) {
      const name = getComponentDisplayName(c);
      const haystack = `${name} ${c.location || ""} ${CATEGORY_META[category].label}`.toLowerCase();
      if (haystack.includes(q)) matches.push({ category, component: c, name });
      if (matches.length >= 40) break;
    }
    if (matches.length >= 40) break;
  }

  if (matches.length === 0) {
    results.innerHTML = `<div class="search-empty">No components match "${escapeHtml(query)}"</div>`;
    results.style.display = "block";
    return;
  }

  results.innerHTML = "";
  for (const m of matches) {
    const row = document.createElement("div");
    row.className = "search-result-row";
    row.innerHTML = `
      <span class="search-cat-badge">${escapeHtml(CATEGORY_META[m.category].label)}</span>
      <div class="search-result-info">
        <div class="name">${escapeHtml(m.name)}</div>
        <div class="sub">${m.component.location ? escapeHtml(m.component.location) + " &middot; " : ""}Health: ${escapeHtml(m.component.health || "Unknown")}</div>
      </div>
      <span class="dot" style="background:${healthDotColor(m.component.health)}"></span>
      <i class="fa-solid fa-arrow-right"></i>
    `;
    row.addEventListener("click", () => {
      results.style.display = "none";
      $("#componentSearch").value = "";
      $("#componentSearchClear").style.display = "none";
      openCategoryModal(m.category);
      jumpToComponentInModal(m.component);
    });
    results.appendChild(row);
  }
  results.style.display = "block";
}

function jumpToComponentInModal(component) {
  const itemId = component.odata_id || component.name || "";
  requestAnimationFrame(() => {
    const el = $(`#categoryModalBody .component-item[data-odata-id="${CSS.escape(itemId)}"]`);
    if (!el) return;
    if (!el.classList.contains("open")) el.querySelector(".component-item-header").click();
    el.scrollIntoView({ block: "center" });
  });
}

function renderHeader(server) {
  const header = $("#serverHeader");
  const lastUpdated = server.last_successful_poll ? new Date(server.last_successful_poll + (server.last_successful_poll.endsWith('Z') ? '' : 'Z')).toLocaleString('en-IN', { timeZone: 'Asia/Kolkata' }) : "never";
  const connLabel = formatConnectionStatus(server.connection_status);
  const errorDetail = formatPollError(server.last_poll_error, server.connection_status);
  const connPillClass = server.connection_status === 'connected' ? 'pill-ok' : 'pill-crit';
  const supportsDiagnostics = (server.vendor || "").toLowerCase().includes("dell") && !server.agent_id;
  const isDell = (server.vendor || "").toLowerCase().includes("dell");
  const identityLabel = isDell ? "Service Tag" : "Serial Number";
  const identityValue = isDell
    ? (server.service_tag || "—")
    : (server.serial_number || "—");
  header.innerHTML = `
    <div class="server-header-top">
      <div>
        <h1>${escapeHtml(server.display_name || server.hostname)}</h1>
        <div class="sub">${escapeHtml(server.ip_address)} &middot; ${escapeHtml(server.vendor || "unknown vendor")} ${escapeHtml(server.model || "")}</div>
      </div>
      <span class="pill ${healthClass(server.health_status)}"><span class="dot" style="background:${healthDotColor(server.health_status)}"></span>${server.health_status || "Unknown"}</span>
      <span class="pill ${server.power_state === 'On' ? 'pill-ok' : 'pill-unknown'}"><i class="fa-solid fa-power-off"></i> ${server.power_state || "Unknown"}</span>
      <span class="pill ${connPillClass}" ${errorDetail ? `title="${escapeHtml(errorDetail)}"` : ''}><span class="dot" style="background:${connDotColor(server.connection_status)}"></span>${connLabel}</span>
      <div class="header-actions">
        <button class="btn-secondary" id="editServerBtn"><i class="fa-solid fa-pen-to-square"></i> Edit</button>
        <button class="btn-secondary" id="pollNowBtn"><i class="fa-solid fa-rotate"></i> Poll now</button>
        ${supportsDiagnostics ? '<button class="btn-secondary" id="supportBundleBtn"><i class="fa-solid fa-file-zipper"></i> Support bundle</button>' : ''}
      </div>
    </div>
    <div class="header-stats">
      ${server.agent_id ? `<div class="stat"><div class="label">Managed By</div><div class="value">Remote Agent</div></div>` : `<div class="stat"><div class="label">Managed By</div><div class="value">Central Server</div></div>`}
      <div class="stat"><div class="label">Firmware</div><div class="value">${escapeHtml(server.firmware_version || "-")}</div></div>
      <div class="stat">
        <div class="label">${identityLabel}</div>
        <div class="value">${escapeHtml(identityValue)}</div>
      </div>
      <div class="stat">
        <div class="label">Server ID</div>
        <div class="value" style="user-select:all; font-family:monospace; cursor:pointer;" onclick="navigator.clipboard.writeText('${server.id}');toast('Server ID copied!');" title="Click to copy">${server.id}</div>
      </div>
      <div class="stat"><div class="label">Last Updated</div><div class="value">${lastUpdated}</div></div>
    </div>
  `;
  if (errorDetail && server.connection_status !== 'connected') {
    const errBanner = document.createElement('div');
    errBanner.className = 'header-error-banner';
    errBanner.innerHTML = `<i class="fa-solid fa-circle-exclamation"></i> ${escapeHtml(errorDetail)} <button class="btn-secondary" id="errUpdateCredsBtn" style="margin-left:10px;">Update Credentials</button>`;
    header.appendChild(errBanner);
    $("#errUpdateCredsBtn").addEventListener("click", () => {
      openEditServerModal(server);
      setTimeout(() => $("#e_password").focus(), 100);
    });
  }
  $("#editServerBtn").addEventListener("click", () => openEditServerModal(server));
  $("#pollNowBtn").addEventListener("click", async () => {
    await api(`/api/servers/${state.selectedServerId}/poll-now`, { method: "POST" });
    toast("Poll queued");
  });
  if (supportsDiagnostics) {
    $("#supportBundleBtn").addEventListener("click", startSupportBundle);
  }
}

async function startSupportBundle() {
  const button = $("#supportBundleBtn");
  if (!button || !state.selectedServerId) return;
  button.disabled = true;
  button.textContent = "Starting support bundle…";
  try {
    const operation = await api(
      `/api/servers/${state.selectedServerId}/diagnostics/support-bundle`,
      { method: "POST" },
    );
    monitorSupportBundle(operation.id, button);
  } catch (e) {
    button.disabled = false;
    button.innerHTML = '<i class="fa-solid fa-file-zipper"></i> Support bundle';
    toast(`Support bundle failed to start: ${e.message}`);
  }
}

async function monitorSupportBundle(operationId, button) {
  try {
    const operation = await api(`/api/operations/${operationId}`);
    if (operation.status === "completed") {
      const downloadButton = button.cloneNode(true);
      downloadButton.disabled = false;
      downloadButton.innerHTML = '<i class="fa-solid fa-download"></i> Download support bundle';
      button.replaceWith(downloadButton);
      downloadButton.addEventListener("click", () => {
        window.location.href = `/api/operations/${operationId}/download`;
      });
      toast("Support bundle is ready to download");
      return;
    }
    if (operation.status === "failed") {
      button.disabled = false;
      button.innerHTML = '<i class="fa-solid fa-file-zipper"></i> Support bundle';
      toast(`Support bundle failed: ${operation.error_message || "Unknown error"}`);
      return;
    }

    const progress = operation.progress_percent;
    button.textContent = progress === null || progress === undefined
      ? "Preparing support bundle…"
      : `Preparing support bundle… ${progress}%`;
    setTimeout(() => monitorSupportBundle(operationId, button), 2000);
  } catch (e) {
    button.disabled = false;
    button.innerHTML = '<i class="fa-solid fa-file-zipper"></i> Support bundle';
    toast(`Unable to check support bundle: ${e.message}`);
  }
}

function buildCustomerCard(server) {
  const card = document.createElement("div");
  card.className = "card";
  card.dataset.category = "customer_info";

  const isOpen = state.openCards.has("customer_info");
  if (isOpen) {
    card.classList.add("open");
  }

  const name = server.customer_name || "Not specified";
  const location = server.customer_location || "Not specified";
  const records = server.maintenance_records || "No maintenance records filed.";

  card.innerHTML = `
    <div class="card-header">
      <i class="fa-solid fa-id-card icon" style="color:var(--accent);"></i>
      <span class="title">Customer & Maintenance Info</span>
      <i class="fa-solid fa-chevron-right chevron"></i>
    </div>
    <div class="card-body" style="padding: 16px; display: ${isOpen ? 'block' : 'none'}; border-top: 1px solid var(--border);">
      <div style="margin-bottom: 12px;">
        <div style="font-size: 11px; text-transform: uppercase; color: var(--text-dim); margin-bottom: 4px; font-weight: 700;">Customer Name</div>
        <div style="font-size: 13.5px; color: var(--text-bright); font-weight: 600;">${escapeHtml(name)}</div>
      </div>
      <div style="margin-bottom: 12px;">
        <div style="font-size: 11px; text-transform: uppercase; color: var(--text-dim); margin-bottom: 4px; font-weight: 700;">Customer Location</div>
        <div style="font-size: 13.5px; color: var(--text-bright); font-weight: 600;">${escapeHtml(location)}</div>
      </div>
      <div>
        <div style="font-size: 11px; text-transform: uppercase; color: var(--text-dim); margin-bottom: 6px; font-weight: 700;">Maintenance Records</div>
        <div style="background: var(--panel-alt); border: 1px solid var(--border); border-radius: var(--radius-md); padding: 10px 12px; font-size: 12px; line-height: 1.5; color: var(--text-bright); white-space: pre-wrap; font-family: var(--mono); max-height: 180px; overflow-y: auto;">${escapeHtml(records)}</div>
      </div>
    </div>
  `;

  card.querySelector(".card-header").addEventListener("click", () => {
    if (state.openCards.has("customer_info")) {
      state.openCards.delete("customer_info");
      card.classList.remove("open");
      card.querySelector(".card-body").style.display = "none";
    } else {
      state.openCards.add("customer_info");
      card.classList.add("open");
      card.querySelector(".card-body").style.display = "block";
    }
  });

  return card;
}

function renderCards(componentsByCategory, server) {
  const grid = $("#cardsGrid");
  grid.innerHTML = "";
  
  if (server) {
    grid.appendChild(buildCustomerCard(server));
  }

  for (const category of CATEGORY_ORDER) {
    let comps = componentsByCategory[category] || [];
    if (category === "storage") {
      comps = comps.concat(
        componentsByCategory["storage_controller"] || [],
        componentsByCategory["storage_drive"] || [],
        componentsByCategory["storage_volume"] || []
      );
    }
    state.categoryComponents[category] = comps;
    grid.appendChild(buildCategoryCard(category, comps));
  }
  // logs card gets its own custom body
  grid.appendChild(buildLogsCard());
}

function worstHealth(components) {
  const order = { OK: 0, Warning: 1, Critical: 2 };
  let worst = null;
  for (const c of components) {
    if (c.health && (worst === null || order[c.health] > order[worst])) worst = c.health;
  }
  return worst;
}

function buildCategoryCard(category, components) {
  const meta = CATEGORY_META[category];
  const card = document.createElement("div");
  card.className = "card";
  card.dataset.category = category;

  // Filter out unsupported markers before computing display count
  const realComps = components.filter(c => c.odata_id !== "meta:unsupported");
  const isUnsupported = components.length > 0 && realComps.length === 0;
  const countLabel = isUnsupported ? "–" : String(realComps.length);

  const worst = worstHealth(realComps);
  card.innerHTML = `
    <div class="card-header">
      <i class="fa-solid ${meta.icon} icon"></i>
      <span class="title">${meta.label}</span>
      ${worst ? `<span class="dot" style="width:8px;height:8px;border-radius:50%;background:${healthDotColor(worst)}"></span>` : ""}
      <span class="count">${countLabel}</span>
      <i class="fa-solid fa-chevron-right chevron"></i>
    </div>
  `;
  card.querySelector(".card-header").addEventListener("click", () => openCategoryModal(category));
  return card;
}

// ---------------------------------------------------------------------
// Category detail popup - shows one category's full component list in
// a scrollable overlay instead of expanding the card inline (inline
// expansion stretched every other card in the same CSS Grid row).
// ---------------------------------------------------------------------
function openCategoryModal(category) {
  state.openCategoryModal = category;
  const meta = CATEGORY_META[category];
  $("#categoryModalIcon").className = `fa-solid ${meta.icon}`;
  $("#categoryModalTitle").textContent = meta.label;
  const comps = state.categoryComponents[category] || [];
  const realComps = comps.filter((c) => c.odata_id !== "meta:unsupported");
  $("#categoryModalCount").textContent = realComps.length ? `${realComps.length} item${realComps.length === 1 ? "" : "s"}` : "";
  renderCategoryBody($("#categoryModalBody"), category, comps);
  $("#categoryModalOverlay").classList.add("open");
}

function closeCategoryModal() {
  state.openCategoryModal = null;
  $("#categoryModalOverlay").classList.remove("open");
}

function wireCategoryModal() {
  $("#categoryModalClose").addEventListener("click", closeCategoryModal);
  $("#categoryModalOverlay").addEventListener("click", (e) => {
    if (e.target.id === "categoryModalOverlay") closeCategoryModal();
  });
}

function renderCategoryBody(body, category, components) {
  body.innerHTML = "";

  // Check for unsupported-marker components
  const realComps = components.filter(c => c.odata_id !== "meta:unsupported");
  if (components.length > 0 && realComps.length === 0) {
    const msg = document.createElement("div");
    msg.className = "card-empty";
    msg.textContent = "Not supported or not available on this server.";
    body.appendChild(msg);
    return;
  }

  if (realComps.length === 0) {
    const empty = document.createElement("div");
    empty.className = "card-empty";
    empty.textContent = "No data reported by Redfish for this category on this server.";
    body.appendChild(empty);
    return;
  }

  body.appendChild(buildComponentTableHead());
  for (const c of realComps) {
    body.appendChild(buildComponentItem(c));
  }
}

function buildComponentTableHead() {
  const head = document.createElement("div");
  head.className = "component-table-head";
  head.innerHTML = `<span></span><span>Name</span><span>Location</span><span></span>`;
  return head;
}

function renderComponentProperties(c) {
  if (c.category === "storage_drive") {
    return buildStorageDriveProperties(c.properties);
  }
  return buildPropGrid(c.properties);
}


function getComponentDisplayName(c) {
  const p = c.properties || {};
  const baseName = c.name || "";
  
  const identifiers = [
    p.Id,
    p.DeviceLocator,
    p.Socket,
    p.PhysicalPortNumber,
    p.PortId,
    p.MACAddress,
    p.InterfaceName,
    p.FQDD,
    p.SerialNumber
  ];
  
  for (const id of identifiers) {
    if (id !== undefined && id !== null && String(id) !== baseName) {
      return `${baseName} (${id})`;
    }
  }
  
  return baseName || c.odata_id;
}

function buildComponentItem(c) {
  const item = document.createElement("div");
  const itemId = c.odata_id || c.name || '';
  const wasOpen = state.openComponents.has(itemId);
  item.className = "component-item" + (wasOpen ? " open" : "");
  item.dataset.odataId = itemId;
  item.innerHTML = `
    <div class="component-item-header">
      <span class="dot" style="width:7px;height:7px;border-radius:50%;background:${healthDotColor(c.health)};flex-shrink:0;"></span>
      <span class="name">${escapeHtml(getComponentDisplayName(c))}</span>
      ${c.location ? `<span class="loc">${escapeHtml(c.location)}</span>` : ""}
      <i class="fa-solid fa-chevron-right chevron" style="font-size:10px;"></i>
    </div>
    <div class="component-props"></div>
  `;
  const itemHeader = item.querySelector(".component-item-header");
  const props = item.querySelector(".component-props");
  itemHeader.addEventListener("click", () => {
    const nowOpen = !item.classList.contains("open");
    item.classList.toggle("open");
    if (nowOpen) {
      state.openComponents.add(itemId);
      if (!props.dataset.rendered) {
        props.appendChild(renderComponentProperties(c));
        props.dataset.rendered = "1";
      }
    } else {
      state.openComponents.delete(itemId);
    }
  });
  // If it was previously open, render the property grid immediately
  if (wasOpen && !props.dataset.rendered) {
    props.appendChild(renderComponentProperties(c));
    props.dataset.rendered = "1";
  }
  return item;
}

// Flattens a Redfish resource JSON into a flat key -> value property
// grid so "every available property" is genuinely visible, including
// nested objects/arrays (rendered as compact JSON) and OEM extensions.

const DEFAULT_SKIP_TOP_LEVEL_KEYS = new Set([
  "@odata.context", "@odata.etag", "@odata.id", "@odata.type",
  "Id", "Name", "Description", "Links", "Actions", "Oem", "Assembly"
]);

function buildPropGrid(obj, prefix = "", skipTopLevelKeys = DEFAULT_SKIP_TOP_LEVEL_KEYS) {
  const grid = document.createElement("div");
  grid.className = "prop-grid";

  function formatLabel(path) {
    if (path.includes(" ")) return path; // Already formatted
    let label = path.replace(/\bAttributes\./g, '');
    label = label.replace(/\./g, ' ');
    label = label.replace(/([a-z])([A-Z])/g, '$1 $2');
    label = label.replace(/([A-Z])([A-Z][a-z])/g, '$1 $2');
    label = label.replace(/([a-zA-Z])([0-9]+)/g, '$1 $2');
    label = label.replace(/([0-9]+)([a-zA-Z])/g, '$1 $2');
    return label.trim();
  }

  function walk(value, path) {
    if (value === null || value === undefined) {
      addRow(path, "null", true);
      return;
    }

    const pathParts = path.split(".");
    const lastPart = pathParts[pathParts.length - 1];
    if (lastPart && (lastPart.includes("@odata") || lastPart.includes("@Redfish.Deprecated") || lastPart === "Links" || lastPart === "Actions")) {
      return;
    }

    if (Array.isArray(value)) {
      if (value.length === 0) { addRow(path, "[]", true); return; }
      const allPrimitive = value.every((v) => typeof v !== "object" || v === null);
      if (allPrimitive) {
        addRow(path, JSON.stringify(value).replace(/,/g, ", "));
      } else {
        value.forEach((v, i) => walk(v, `${path}[${i}]`));
      }
      return;
    }
    if (typeof value === "object") {
      const keys = Object.keys(value).filter((k) => {
        if (!path && skipTopLevelKeys.has(k)) return false;
        if (k.includes("@odata") || k.includes("@Redfish.Deprecated") || k === "Links" || k === "Actions") return false;
        return true;
      });
      if (keys.length === 0) {
        if (path) addRow(path, "{}", true);
        return;
      }
      for (const k of keys) {
        if (!path) {
          if (STORAGE_SUPERSEDED_KEYS[k] && value[STORAGE_SUPERSEDED_KEYS[k]] !== undefined) {
            continue;
          }
          if (Object.prototype.hasOwnProperty.call(STORAGE_PROPERTY_LABELS, k)) {
            const v = value[k];
            if (v === null || v === undefined) continue;
            addRow(STORAGE_PROPERTY_LABELS[k], formatStorageValue(k, v));
            continue;
          }
        }
        walk(value[k], path ? `${path}.${k}` : k);
      }
      return;
    }
    if (value === "") { addRow(path, "(empty)", true); return; }
    addRow(path, String(value));
  }

  function addRow(path, value, isEmpty = false) {
    const k = document.createElement("div");
    k.className = "k";
    k.textContent = formatLabel(path);
    const v = document.createElement("div");
    v.className = "v";
    if (isEmpty) {
      v.innerHTML = `<span class="v-empty-note">Not provided by this BMC</span>`;
    } else {
      v.textContent = value;
    }
    grid.appendChild(k);
    grid.appendChild(v);
  }

  walk(obj, prefix);
  return grid;
}

function buildStorageDriveProperties(obj) {
  const container = document.createElement("div");

  // ---- Summary ----
  const summaryGrid = document.createElement("div");
  summaryGrid.className = "prop-grid";
  for (const [key, label] of STORAGE_SUMMARY_FIELDS) {
    const v = obj[key];
    if (v === null || v === undefined) continue; // hide missing fields
    const k = document.createElement("div");
    k.className = "k";
    k.textContent = label;
    const vEl = document.createElement("div");
    vEl.className = "v";
    vEl.textContent = formatStorageValue(key, v);
    summaryGrid.appendChild(k);
    summaryGrid.appendChild(vEl);
  }
  container.appendChild(summaryGrid);

  // ---- Advanced Details (collapsed by default) ----
  const advancedGrid = buildPropGrid(obj, "", STORAGE_ADVANCED_SKIP_KEYS);
  if (advancedGrid.children.length > 0) {
    const section = document.createElement("div");
    section.className = "component-item"; // reuse existing collapse styling/CSS
    section.innerHTML = `
      <div class="component-item-header">
        <span class="name">Advanced Details</span>
        <span class="count">${advancedGrid.children.length / 2}</span>
        <i class="fa-solid fa-chevron-right chevron" style="font-size:10px;"></i>
      </div>
      <div class="component-props"></div>
    `;
    section.querySelector(".component-props").appendChild(advancedGrid);
    section.querySelector(".component-item-header").addEventListener("click", () => {
      section.classList.toggle("open");
    });
    container.appendChild(section);
  }

  return container;
}


// ---------------------------------------------------------------------
// History charts (per category, when it has chartable metrics)
// ---------------------------------------------------------------------
function buildHistorySection(category) {
  const wrap = document.createElement("div");
  wrap.className = "card";
  wrap.style.margin = "10px 16px";
  wrap.style.border = "1px dashed var(--border)";

  const metrics = HISTORY_METRICS[category];
  const metricOptions = metrics.map(([m, label]) => `<option value="${m}">${label}</option>`).join("");

  wrap.innerHTML = `
    <div class="history-toolbar">
      <select class="metricSelect">${metricOptions}</select>
      <div style="flex:1"></div>
      <button class="btn-secondary range-btn active" data-range="1h">1H</button>
      <button class="btn-secondary range-btn" data-range="24h">24H</button>
      <button class="btn-secondary range-btn" data-range="7d">7D</button>
      <button class="btn-secondary range-btn" data-range="30d">30D</button>
    </div>
    <div class="chart-wrap"><canvas height="90"></canvas></div>
  `;

  const canvas = wrap.querySelector("canvas");
  const metricSelect = wrap.querySelector(".metricSelect");
  let currentRange = "1h";

  async function refresh() {
    const metric = metricSelect.value;
    const data = await api(`/api/servers/${state.selectedServerId}/history/${metric}?range=${currentRange}`);
    drawChart(canvas, category, data);
  }

  wrap.querySelectorAll(".range-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      wrap.querySelectorAll(".range-btn").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      currentRange = btn.dataset.range;
      refresh();
    });
  });
  metricSelect.addEventListener("change", refresh);

  refresh();
  return wrap;
}

function drawChart(canvas, category, readings) {
  const key = category + canvas.dataset.chartKey;
  if (state.charts[canvas]) {
    state.charts[canvas].destroy();
  }
  // group by source_name into separate lines
  const bySource = {};
  for (const r of readings) {
    const name = r.source_name || "value";
    if (!bySource[name]) bySource[name] = [];
    bySource[name].push({ x: r.recorded_at, y: r.value });
  }
  const palette = ["#3fb6ff", "#2ecc71", "#f1c40f", "#e74c3c", "#a78bfa", "#fb923c"];
  const datasets = Object.entries(bySource).map(([name, points], i) => ({
    label: name,
    data: points,
    borderColor: palette[i % palette.length],
    backgroundColor: "transparent",
    tension: 0.25,
    pointRadius: 0,
    borderWidth: 1.6,
  }));

  state.charts[canvas] = new Chart(canvas, {
    type: "line",
    data: { datasets },
    options: {
      responsive: true,
      animation: false,
      scales: {
        x: { type: "time", ticks: { color: "#8b96a8", maxTicksLimit: 6 }, grid: { color: "rgba(255,255,255,0.04)" } },
        y: { ticks: { color: "#8b96a8" }, grid: { color: "rgba(255,255,255,0.04)" } },
      },
      plugins: {
        legend: { labels: { color: "#d7dee8", boxWidth: 10, font: { size: 10 } } },
      },
    },
  });
}

// ---------------------------------------------------------------------
// Logs card
// ---------------------------------------------------------------------
function buildLogsCard() {
  const card = document.createElement("div");
  card.className = "card";
  card.style.gridColumn = "1 / -1";
  card.innerHTML = `
    <div class="card-header">
      <i class="fa-solid fa-scroll icon"></i>
      <span class="title">Logs</span>
      <i class="fa-solid fa-chevron-right chevron"></i>
    </div>
    <div class="card-body">
      <div class="logs-toolbar">
        <input type="text" placeholder="Search messages..." id="logSearch">
        <select id="logSeverity">
          <option value="">All severities</option>
          <option value="OK">Informational</option>
          <option value="Warning">Warning</option>
          <option value="Critical">Critical</option>
        </select>
        <button class="btn-secondary" id="logRefresh"><i class="fa-solid fa-rotate"></i></button>
      </div>
      <div id="logRows"></div>
    </div>
  `;
  const header = card.querySelector(".card-header");
  const body = card.querySelector(".card-body");
  let loaded = false;

  header.addEventListener("click", () => {
    card.classList.toggle("open");
    if (card.classList.contains("open") && !loaded) {
      loaded = true;
      loadLogs();
    }
  });

  async function loadLogs() {
    const q = card.querySelector("#logSearch").value;
    const sev = card.querySelector("#logSeverity").value;
    const params = new URLSearchParams();
    if (q) params.set("q", q);
    if (sev) params.set("severity", sev);
    const entries = await api(`/api/servers/${state.selectedServerId}/logs?${params}`);
    const rowsEl = card.querySelector("#logRows");
    if (entries.length === 0) {
      rowsEl.innerHTML = `<div class="card-empty">No log entries found.</div>`;
      return;
    }
    rowsEl.innerHTML = entries.map((e) => `
      <div class="log-row">
        <span class="sev" style="color:${healthDotColor(e.severity === 'OK' ? 'OK' : e.severity)}">${escapeHtml(e.severity)}</span>
        <span class="ts">${e.created_at ? new Date(e.created_at + (e.created_at.endsWith('Z') ? '' : 'Z')).toLocaleString('en-IN', { timeZone: 'Asia/Kolkata' }) : ""}</span>
        <span>${escapeHtml(e.message || e.message_id || "")}</span>
      </div>
    `).join("");
  }

  card.querySelector("#logRefresh")?.addEventListener?.("click", loadLogs);
  card.addEventListener("click", (e) => {
    if (e.target.id === "logRefresh") loadLogs();
  });
  card.querySelector("#logSearch").addEventListener("keydown", (e) => { if (e.key === "Enter") loadLogs(); });
  card.querySelector("#logSeverity").addEventListener("change", loadLogs);

  return card;
}

// ---------------------------------------------------------------------
// Alerts: shared data load + nav badge + full page (sorted critical -> warning -> info)
// ---------------------------------------------------------------------
async function loadAlerts() {
  state.alerts = await api("/api/alerts?resolved=false");
  renderAlertsNavBadge();
  if (state.view === "overview") renderOverviewView();
  if (state.view === "alerts") updateAlertsList();
}

function renderAlertsNavBadge() {
  const badge = $("#navAlertCount");
  const count = state.alerts.length;
  badge.style.display = count > 0 ? "inline-block" : "none";
  badge.textContent = count > 99 ? "99+" : count;
}

function renderAlertsView() {
  const main = $("#main");
  main.innerHTML = `
    <div class="view-header">
      <div>
        <h1>Alerts</h1>
        <div class="sub" id="alertsSubCount"></div>
      </div>
    </div>
    <div class="alerts-summary" id="alertsSummary"></div>
    <div id="alertsGroupedList"></div>
  `;
  updateAlertsList();
}

function updateAlertsList() {
  if (state.view !== "alerts") return;
  const counts = { all: state.alerts.length, critical: 0, warning: 0, info: 0 };
  for (const a of state.alerts) if (counts[a.severity] !== undefined) counts[a.severity]++;

  const summaryEl = $("#alertsSummary");
  const cards = [
    ["all", "All", counts.all, ""],
    ["critical", "Critical", counts.critical, "crit"],
    ["warning", "Warning", counts.warning, "warn"],
    ["info", "Info", counts.info, ""],
  ];
  summaryEl.innerHTML = cards.map(([key, label, count, cls]) =>
    `<div class="stat-card ${cls}${state.alertsFilter === key ? " selected" : ""}" data-filter="${key}">
      <div class="label">${label}</div><div class="value">${count}</div>
    </div>`
  ).join("");
  $all(".stat-card", summaryEl).forEach((card) => {
    card.addEventListener("click", () => {
      state.alertsFilter = card.dataset.filter;
      updateAlertsList();
    });
  });

  const filtered = state.alertsFilter === "all"
    ? state.alerts
    : state.alerts.filter((a) => a.severity === state.alertsFilter);
  $("#alertsSubCount").textContent = `${filtered.length} open alert${filtered.length === 1 ? "" : "s"}`;

  const listEl = $("#alertsGroupedList");
  if (filtered.length === 0) {
    listEl.innerHTML = `<div class="panel-box"><div class="panel-box-empty">No open alerts.</div></div>`;
    return;
  }

  listEl.innerHTML = "";
  const severities = state.alertsFilter === "all" ? SEVERITY_ORDER : [state.alertsFilter];
  for (const sev of severities) {
    const group = filtered
      .filter((a) => a.severity === sev)
      .sort((a, b) => new Date(b.last_occurred_at) - new Date(a.last_occurred_at));
    if (group.length === 0) continue;
    if (state.alertsFilter === "all") {
      const label = document.createElement("div");
      label.className = "alerts-group-label";
      label.textContent = `${SEVERITY_META[sev].label} (${group.length})`;
      listEl.appendChild(label);
    }
    const box = document.createElement("div");
    box.className = "alerts-page-list";
    for (const a of group) box.appendChild(buildAlertItemEl(a));
    listEl.appendChild(box);
  }
}

function buildAlertItemEl(a) {
  const server = state.servers.find((s) => s.id === a.server_id);
  const el = document.createElement("div");
  el.className = "alert-item";
  el.innerHTML = `
    <div class="top">
      <span class="pill ${healthClass(a.severity === 'critical' ? 'Critical' : a.severity === 'warning' ? 'Warning' : 'Unknown')}">${a.severity}</span>
      <strong>${escapeHtml(server ? (server.display_name || server.hostname) : a.server_id)}</strong>
    </div>
    <div class="msg">${escapeHtml(a.message)}</div>
    <div class="meta">${escapeHtml(a.category)} &middot; ${a.occurrence_count}x &middot; last ${timeAgoOrLocal(a.last_occurred_at)}</div>
    <div class="alert-actions">
      ${!a.acknowledged ? `<button class="btn-secondary ack-btn">Acknowledge</button>` : `<span class="meta">Acknowledged</span>`}
      <button class="btn-secondary resolve-btn">Resolve</button>
    </div>
  `;
  el.querySelector(".ack-btn")?.addEventListener("click", async () => {
    await api(`/api/alerts/${a.id}/acknowledge`, { method: "POST", body: JSON.stringify({}) });
    loadAlerts();
  });
  el.querySelector(".resolve-btn").addEventListener("click", async () => {
    await api(`/api/alerts/${a.id}/resolve`, { method: "POST" });
    loadAlerts();
  });
  if (server) {
    el.style.cursor = "pointer";
    el.addEventListener("click", (e) => {
      if (e.target.closest("button")) return;
      selectServer(server.id);
    });
  }
  return el;
}

// ---------------------------------------------------------------------
// Add server modal
// ---------------------------------------------------------------------
function wireAddServerModal() {
  const modal = $("#addServerModal");
  // Note: the "Add server" button lives inside the dynamically-rendered
  // Nodes view (see renderNodesView), not in the static page shell, so
  // it's wired there instead of here.
  $("#cancelAddServer").addEventListener("click", () => modal.classList.remove("open"));
  $("#submitAddServer").addEventListener("click", async () => {
    const errorEl = $("#addServerError");
    errorEl.style.display = "none";
    const payload = {
      hostname: $("#f_hostname").value.trim(),
      display_name: $("#f_hostname").value.trim(),
      ip_address: $("#f_ip").value.trim(),
      username: $("#f_username").value.trim(),
      password: $("#f_password").value,
      management_protocol: $("#f_protocol").value,
      polling_interval_seconds: parseInt($("#f_interval").value, 10) || 30,
      customer_name: $("#f_customer_name").value.trim(),
      customer_location: $("#f_customer_location").value.trim(),
      maintenance_records: $("#f_maintenance_records").value.trim(),
    };
    const sid = $("#f_site_id").value.trim();
    if (sid) payload.site_id = sid;
    const aid = $("#f_agent_id").value.trim();
    if (aid) payload.agent_id = aid;
    try {
      await api("/api/servers", { method: "POST", body: JSON.stringify(payload) });
      modal.classList.remove("open");
      ["f_hostname", "f_ip", "f_username", "f_password", "f_site_id", "f_agent_id", "f_customer_name", "f_customer_location", "f_maintenance_records"].forEach((id) => {
        const el = $(`#${id}`);
        if (el) el.value = "";
      });
      $("#f_protocol").value = "redfish";
      await loadServers();
      toast("Server added - discovery in progress");
    } catch (e) {
      errorEl.textContent = e.message;
      errorEl.style.display = "block";
    }
  });
}

// ---------------------------------------------------------------------
// WebSocket wiring
// ---------------------------------------------------------------------
let socket;
function wireSocket() {
  socket = io();

  socket.on("connect", () => {
    $("#wsStatus").className = "pill pill-ok";
    $("#wsStatus").innerHTML = `<span class="dot" style="background:var(--ok)"></span> live`;
    const fs = $("#footerStreamStatus");
    if (fs) {
      fs.className = "footer-stream live";
      fs.innerHTML = `<span class="dot"></span> Telemetry Streaming`;
    }
    if (state.selectedServerId) socket.emit("subscribe_server", { server_id: state.selectedServerId });
  });

  socket.on("disconnect", () => {
    $("#wsStatus").className = "pill pill-crit";
    $("#wsStatus").innerHTML = `<span class="dot" style="background:var(--crit)"></span> disconnected`;
    const fs = $("#footerStreamStatus");
    if (fs) {
      fs.className = "footer-stream down";
      fs.innerHTML = `<span class="dot"></span> Disconnected`;
    }
  });

  socket.on("server_summary_update", (summary) => {
    const idx = state.servers.findIndex((s) => s.id === summary.id);
    if (idx >= 0) state.servers[idx] = summary; else state.servers.push(summary);
    renderNavCounts();
    if (state.view === "nodes") updateNodesTable();
    if (state.view === "overview") renderOverviewView();
    if (summary.id === state.selectedServerId) {
      const headerEl = $("#serverHeader");
      if (headerEl) renderHeader({ ...summary, redfish_service_root: null });
    }
  });

  socket.on("component_update", async (payload) => {
    if (payload.server_id !== state.selectedServerId) return;

    // Only update the specific category that changed, not all categories
    const category = payload.category;
    const card = document.querySelector(`.card[data-category="${category}"]`);
    if (!card) return;

    // For storage, we need to fetch all sub-categories
    let comps;
    if (category === "storage") {
      const componentsByCategory = await api(`/api/servers/${state.selectedServerId}/components`);
      comps = (componentsByCategory["storage"] || []).concat(
        componentsByCategory["storage_controller"] || [],
        componentsByCategory["storage_drive"] || [],
        componentsByCategory["storage_volume"] || []
      );
    } else {
      comps = payload.components || [];
    }

    state.categoryComponents[category] = comps;
    const realCompsNow = comps.filter((c) => c.odata_id !== "meta:unsupported");
    const isUnsupportedNow = comps.length > 0 && realCompsNow.length === 0;

    // Update the count badge on the grid tile
    const countEl = card.querySelector(".count");
    if (countEl) countEl.textContent = isUnsupportedNow ? "–" : String(realCompsNow.length);

    // Update the health dot on the card header
    const worst = worstHealth(comps);
    const existingDot = card.querySelector(".card-header .dot[style]");
    if (existingDot && worst) {
      existingDot.style.background = healthDotColor(worst);
    }

    // If this category's popup is currently open, refresh its contents live
    if (state.openCategoryModal === category) {
      const body = $("#categoryModalBody");
      const scrollTop = body.scrollTop;
      renderCategoryBody(body, category, comps);
      body.scrollTop = scrollTop;
      $("#categoryModalCount").textContent = realCompsNow.length ? `${realCompsNow.length} item${realCompsNow.length === 1 ? "" : "s"}` : "";
    }
  });

  socket.on("alert", () => {
    loadAlerts();
  });

  socket.on("log_entries", (payload) => {
    if (payload.server_id === state.selectedServerId) {
      toast(`${payload.entries.length} new log entr${payload.entries.length === 1 ? "y" : "ies"}`);
    }
  });
}

// ---------------------------------------------------------------------
// Boot
// ---------------------------------------------------------------------
document.addEventListener("DOMContentLoaded", async () => {
  wireAddServerModal();
  wireEditServerModal();
  wireCategoryModal();
  wireNav();
  wireSocket();
  startClock();

  await loadServers();
  await loadAlerts();
  await loadStorageDevices();
  setView("overview");
  setInterval(loadAlerts, 30000);
});