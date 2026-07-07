let devices = [];
let selectedDevice = "";
let selectedRange = "1h";
let refreshTimer = null;
let diskRefreshTimer = null;
let metricsReqSeq = 0;
let disksReqSeq = 0;
let overviewController = null;
let metricsController = null;
let disksController = null;
let statusHistoryController = null;
let refreshAllPromise = null;
let overviewStateByDevice = new Map();
let selectedView = "status-overview";
let statusOverviewNextRefreshAt = 0;

const OFFLINE_AFTER_SECONDS = 60;
const STATUS_OVERVIEW_REFRESH_MS = 15 * 60 * 1000;

const statusText = document.getElementById("statusText");
const refreshBtn = document.getElementById("refreshBtn");
const overviewCards = document.getElementById("overviewCards");
const summary = document.getElementById("summary");
const userSummary = document.getElementById("userSummary");
const diskMeta = document.getElementById("diskMeta");
const diskBars = document.getElementById("diskBars");
const statusOverviewBtn = document.getElementById("statusOverviewBtn");
const deviceDetailBtn = document.getElementById("deviceDetailBtn");
const statusOverviewView = document.getElementById("statusOverviewView");
const deviceDetailView = document.getElementById("deviceDetailView");
const statusOverviewMeta = document.getElementById("statusOverviewMeta");
const statusOverviewCounts = document.getElementById("statusOverviewCounts");
const statusAlertList = document.getElementById("statusAlertList");
const statusDeviceList = document.getElementById("statusDeviceList");

// Chart instances
let charts = {};

// --- UTILS ---
function setStatus(text) {
  statusText.textContent = text;
}

function fmtPercent(v) {
  if (v === null || v === undefined) return "N/A";
  return `${Number(v).toFixed(1)}%`;
}

function fmtTemp(v) {
  if (v === null || v === undefined) return "N/A";
  return `${Number(v).toFixed(1)} °C`;
}

function fmtKBps(v) {
  if (v === null || v === undefined) return "N/A";
  return `${(Number(v) / 1024).toFixed(1)} KB/s`;
}

function fmtMBps(v) {
  if (v === null || v === undefined) return "N/A";
  return `${(Number(v) / 1024 / 1024).toFixed(2)} MB/s`;
}

function fmtGB(v) {
  if (v === null || v === undefined) return "N/A";
  return `${(Number(v) / 1024 / 1024 / 1024).toFixed(1)} GB`;
}

function fmtOnlineUsers(list) {
  if (!Array.isArray(list) || list.length === 0) return "N/A";
  return list.join(", ");
}

function fmtTopProcessUser(top) {
  if (!top || typeof top !== "object") return "N/A";
  const n = top.name || "unknown";
  if (top.cpu_percent != null && top.cpu_percent !== "") {
    return `${n} (${Number(top.cpu_percent).toFixed(1)}% CPU)`;
  }
  if (top.processes != null && top.processes !== "") {
    return `${n} (${top.processes} proc)`;
  }
  return n;
}

function fmtAgeSeconds(v) {
  if (v === null || v === undefined || Number.isNaN(Number(v))) return "unknown";
  const sec = Math.max(0, Math.floor(Number(v)));
  if (sec < 60) return `${sec}s`;
  if (sec < 3600) return `${Math.floor(sec / 60)}m`;
  if (sec < 86400) return `${Math.floor(sec / 3600)}h`;
  return `${Math.floor(sec / 86400)}d`;
}

function fmtRateByUnit(value, unit) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "N/A";
  if (unit === "%") return fmtPercent(value);
  if (unit === "C") return fmtTemp(value);
  if (unit === "B/s") return fmtMBps(value);
  return String(value);
}

function deviceGroupRank(device) {
  const id = String(device?.device_id || device?.id || "").toLowerCase();
  const name = String(device?.device_name || device?.name || "").toLowerCase();
  if (name.startsWith("cpu") || id.startsWith("cpu")) return 0;
  if (name.startsWith("gpu") || id.startsWith("gpu")) return 1;
  if (id.includes("cluster") || name.includes("cluster") || id.includes("edge") || name.includes("edge")) return 2;
  return 3;
}

function sortDevicesForDisplay(items) {
  return [...(items || [])].sort((a, b) => {
    const rankDiff = deviceGroupRank(a) - deviceGroupRank(b);
    if (rankDiff !== 0) return rankDiff;
    const nameA = String(a?.device_name || a?.name || a?.device_id || a?.id || "");
    const nameB = String(b?.device_name || b?.name || b?.device_id || b?.id || "");
    return nameA.localeCompare(nameB, "en", { numeric: true, sensitivity: "base" });
  });
}

function statusValueClass(value) {
  return value === null || value === undefined || Number.isNaN(Number(value)) ? " missing" : "";
}

function setView(view) {
  selectedView = view;
  const showOverview = view === "status-overview";
  statusOverviewView.classList.toggle("hidden", !showOverview);
  deviceDetailView.classList.toggle("hidden", showOverview);
  statusOverviewBtn.classList.toggle("active", showOverview);
  deviceDetailBtn.classList.toggle("active", !showOverview);
  if (!showOverview) {
    Object.values(charts).forEach((c) => c.resize());
  }
}

function getDeviceStatus(item) {
  if (!item) {
    return {
      state: "unknown",
      label: "Unknown",
      color: "var(--accent-warning)",
      message: "No overview state has been loaded for this device.",
    };
  }
  if (item.error) {
    return {
      state: "connection_failed",
      label: "Connection Failed",
      color: "var(--accent-danger)",
      message: String(item.error),
    };
  }
  if (!item.latest) {
    return {
      state: "no_data",
      label: "No Data",
      color: "var(--accent-warning)",
      message: "The metrics file exists or was queried, but no valid snapshot was returned.",
    };
  }
  const age = Number(item.file_age_seconds);
  if (Number.isFinite(age) && age >= OFFLINE_AFTER_SECONDS) {
    return {
      state: "offline",
      label: "Offline",
      color: "var(--accent-danger)",
      message: "The latest metrics snapshot is stale.",
    };
  }
  return {
    state: "online",
    label: "Online",
    color: "var(--accent-success)",
    message: "",
  };
}

function isDeviceUnavailable(item) {
  if (!item) return false;
  return getDeviceStatus(item).state !== "online";
}

function parseTimestamp(ts) {
  if (!ts) return null;
  const s = String(ts).trim();
  // If no timezone suffix, treat as UTC (metrics are stored as UTC-naive).
  const hasTz = /([zZ]|[+\-]\d{2}:\d{2})$/.test(s);
  return new Date(hasTz ? s : `${s}Z`);
}

const fmtTimeUTC8 = new Intl.DateTimeFormat("zh-CN", {
  timeZone: "Asia/Shanghai", // UTC+8 with DST-safe rules
  hour: "2-digit",
  minute: "2-digit",
  hour12: false,
});

const fmtDateTimeUTC8 = new Intl.DateTimeFormat("zh-CN", {
  timeZone: "Asia/Shanghai",
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  hour12: false,
});

const fmtDateUTC8 = new Intl.DateTimeFormat("zh-CN", {
  timeZone: "Asia/Shanghai",
  month: "2-digit",
  day: "2-digit",
});

function replaceController(currentController) {
  if (currentController) {
    currentController.abort();
  }
  return new AbortController();
}

async function fetchJson(url, options = {}) {
  const resp = await fetch(url, options);
  if (!resp.ok) {
    const body = await resp.json().catch(() => ({}));
    throw new Error(body.error || `HTTP ${resp.status}`);
  }
  return resp.json();
}

// --- RENDERING ---
function renderOverviewCards(items) {
  const orderedItems = sortDevicesForDisplay(items);
  // IMPORTANT: avoid clearing overviewCards.innerHTML on every refresh,
  // otherwise the whole left panel will visibly "blink".
  // We reuse DOM nodes keyed by device_id and only update their content.
  const keepIds = new Set(orderedItems.map((x) => x.device_id));
  overviewCards.querySelectorAll("#overviewCards .card.active-card").forEach((c) => {
    c.classList.remove("active-card");
  });

  overviewStateByDevice = new Map(orderedItems.map((item) => [item.device_id, item]));

  orderedItems.forEach((item) => {
    let card = overviewCards.querySelector(`#overviewCards .card[data-device-id="${item.device_id}"]`);
    if (!card) {
      card = document.createElement("div");
      card.className = "card";
      card.dataset.deviceId = item.device_id;
      // Bind click handler once when creating the card.
      card.addEventListener("click", async () => {
        setView("device-detail");
        if (selectedDevice !== item.device_id) {
          selectedDevice = item.device_id;
          overviewCards.querySelectorAll("#overviewCards .card").forEach((c) => c.classList.remove("active-card"));
          card.classList.add("active-card");
          setStatus(`Updating ${selectedDevice}...`);
          try {
            await refreshSelectedMetrics();
            setStatus(`Live`);
          } catch (err) {
            if (err.name !== "AbortError") {
              setStatus(`Error: ${err.message}`);
            }
          }
          refreshDisks().catch((err) => {
            if (err.name !== "AbortError") console.error(err);
          });
        }
      });
    }
    overviewCards.appendChild(card);

    const latest = item.latest;
    const status = getDeviceStatus(item);
    if (status.state === "connection_failed") {
      card.innerHTML = `
        <h3>${item.device_name}</h3>
        <div class="kv"><span class="kv-label">Status:</span> <span class="kv-value" style="color:${status.color}">${status.label}</span></div>
        <div class="kv" style="color:var(--text-muted); font-size:12px;">${status.message}</div>
      `;
    } else if (status.state === "offline") {
      const dt = latest ? parseTimestamp(latest.timestamp) : null;
      const displayTime = dt ? fmtDateTimeUTC8.format(dt) : String(latest?.timestamp || "");
      card.innerHTML = `
        <h3>${item.device_name}</h3>
        <div class="kv"><span class="kv-label">Status:</span> <span class="kv-value" style="color:${status.color}">${status.label}</span></div>
        <div class="kv"><span class="kv-label">Last Update</span> <span class="kv-value">${displayTime || "Unknown"}</span></div>
        <div class="kv"><span class="kv-label">Data Age</span> <span class="kv-value">${fmtAgeSeconds(item.file_age_seconds)}</span></div>
      `;
    } else if (status.state === "no_data") {
      card.innerHTML = `
        <h3>${item.device_name}</h3>
        <div class="kv"><span class="kv-label">Status:</span> <span class="kv-value" style="color:${status.color}">${status.label}</span></div>
        <div class="kv" style="color:var(--text-muted); font-size:12px;">${status.message}</div>
      `;
    } else {
      const dt = parseTimestamp(latest.timestamp);
      const displayTime = dt ? fmtDateTimeUTC8.format(dt) : String(latest.timestamp || "");
      card.innerHTML = `
        <h3>${item.device_name}</h3>
        <div class="kv"><span class="kv-label">Time (UTC+8)</span> <span class="kv-value">${displayTime}</span></div>
        <div class="kv"><span class="kv-label">CPU / Temp</span> <span class="kv-value">${fmtPercent(latest.cpu_percent)} / ${fmtTemp(latest.cpu_temp_c)}</span></div>
        <div class="kv"><span class="kv-label">Memory / GPU</span> <span class="kv-value">${fmtPercent(latest.memory_percent)} / ${fmtPercent(latest.gpu_percent)}</span></div>
        <div class="kv"><span class="kv-label">Disk I/O</span> <span class="kv-value">R ${fmtMBps(latest.disk_read_rate)} | W ${fmtMBps(latest.disk_write_rate)}</span></div>
        <div class="kv"><span class="kv-label">Network</span> <span class="kv-value">TX ${fmtKBps(latest.net_tx_rate)} | RX ${fmtKBps(latest.net_rx_rate)}</span></div>
        <div class="kv"><span class="kv-label">Online Users</span> <span class="kv-value">${fmtOnlineUsers(latest.online_users)}</span></div>
      `;
    }
  });

  // Remove cards no longer present.
  overviewCards.querySelectorAll("#overviewCards .card[data-device-id]").forEach((c) => {
    if (!keepIds.has(c.dataset.deviceId)) c.remove();
  });

  // Re-apply selected styling.
  if (selectedDevice) {
    const selectedEl = overviewCards.querySelector(`#overviewCards .card[data-device-id="${selectedDevice}"]`);
    if (selectedEl) selectedEl.classList.add("active-card");
  }
}

function renderStatusOverview(payload) {
  const devicesPayload = sortDevicesForDisplay(payload.devices || []);
  const alerts = payload.alerts || [];
  const isOffline = (device) => ["offline", "connection_failed"].includes(device.status);
  const hasAlerts = (device) => Array.isArray(device.alerts) && device.alerts.length > 0;
  const offline = devicesPayload.filter(isOffline).length;
  const warning = devicesPayload.filter((device) => !isOffline(device) && (hasAlerts(device) || device.status !== "online")).length;
  const online = devicesPayload.filter((device) => device.status === "online" && !hasAlerts(device)).length;
  const generatedAt = payload.generated_at ? fmtDateTimeUTC8.format(new Date(payload.generated_at)) : "unknown";
  const windowEnd = payload.end_time ? fmtDateTimeUTC8.format(new Date(payload.end_time)) : "unknown";
  const cacheText = payload.cache_hit ? "cached" : "fresh";

  statusOverviewMeta.textContent = `最近 24 小时，${payload.bucket_count || 96} 个已完成的 15 分钟状态槽。窗口截至 ${windowEnd}，生成时间 ${generatedAt} (${cacheText})`;
  statusOverviewCounts.innerHTML = `
    <div class="count-chip online"><strong>${online}</strong><span>Online</span></div>
    <div class="count-chip warning"><strong>${warning}</strong><span>Warning</span></div>
    <div class="count-chip danger"><strong>${offline}</strong><span>Offline</span></div>
  `;

  if (alerts.length === 0) {
    statusAlertList.innerHTML = `<div class="empty-state">No active alerts.</div>`;
  } else {
    statusAlertList.innerHTML = alerts.slice(0, 24).map((alert) => `
      <div class="alert-item ${alert.severity || "warning"}">
        <span class="alert-dot"></span>
        <strong>${alert.device || "Unknown"}</strong>
        <span>${alert.message || ""}</span>
      </div>
    `).join("");
  }

  statusDeviceList.innerHTML = devicesPayload.map((device) => {
    const latest = device.latest || {};
    const statusClass = device.status || "unknown";
    const metricRows = (device.metrics || []).map((metric) => {
      const bars = (metric.bars || []).map((bar) => {
        const title = bar.value === null || bar.value === undefined
          ? `${metric.label}: no data`
          : `${metric.label}: ${fmtRateByUnit(bar.value, metric.unit)}`;
        return `<span class="history-bar ${bar.level || "empty"}" title="${title}"></span>`;
      }).join("");
      return `
        <div class="status-metric-row">
          <div class="status-metric-label">${metric.label}</div>
          <div class="history-bars" aria-label="${metric.label}">${bars}</div>
        </div>
      `;
    }).join("");
    return `
      <article class="status-device-card glass-panel ${statusClass}">
        <header class="status-device-head">
          <div>
            <h3>${device.device_name}</h3>
            <span>${latest.hostname || device.device_id}</span>
          </div>
          <div class="status-pill ${statusClass}">${device.status_label || statusClass}</div>
        </header>
        <div class="status-latest">
          <span class="${statusValueClass(latest.cpu_percent)}">CPU ${fmtPercent(latest.cpu_percent)}</span>
          <span class="${statusValueClass(latest.cpu_temp_c)}">Temp ${fmtTemp(latest.cpu_temp_c)}</span>
          <span class="${statusValueClass(latest.memory_percent)}">Mem ${fmtPercent(latest.memory_percent)}</span>
          <span class="${statusValueClass(latest.disk_read_rate)}">R ${fmtMBps(latest.disk_read_rate)}</span>
          <span class="${statusValueClass(latest.disk_write_rate)}">W ${fmtMBps(latest.disk_write_rate)}</span>
        </div>
        <div class="status-history-block">
          ${metricRows}
        </div>
      </article>
    `;
  }).join("");
}

function renderDiskBars(payload) {
  const disks = payload.disks || [];
  diskMeta.innerHTML = `<span style="color:var(--accent-primary)">Device:</span> ${payload.device_name} &nbsp;|&nbsp; 
                        <span style="color:var(--accent-primary)">Disk Count:</span> ${payload.count} &nbsp;|&nbsp; 
                        <span style="color:var(--accent-primary)">Refreshed:</span> ${new Date().toLocaleTimeString()}`;
  if (disks.length === 0) {
    diskBars.innerHTML = `<div class="disk-item"><div class="kv">No disk info available</div></div>`;
    return;
  }

  // Avoid blinking: reuse existing disk nodes keyed by disk "name".
  const keepNames = new Set(disks.map((d) => d.name));
  diskBars.querySelectorAll(".disk-item[data-disk-name]").forEach((el) => {
    if (!keepNames.has(el.dataset.diskName)) el.remove();
  });

  disks.forEach((d) => {
    const percent = d.percent === null || d.percent === undefined ? null : Number(d.percent);
    const barWidth = percent === null ? 0 : Math.max(0, Math.min(100, percent));
    const usageText = percent === null
        ? "Unmounted or unavailable"
        : `Used ${fmtGB(d.used)} / Total ${fmtGB(d.size)} (${percent.toFixed(1)}%), Free ${fmtGB(d.free)}`;

    const partText = (d.partitions || [])
      .map((p) => `${p.name}${p.mountpoint ? `@${p.mountpoint}` : ""}`)
      .join(", ");

    let item = diskBars.querySelector(`.disk-item[data-disk-name="${d.name}"]`);
    if (!item) {
      item = document.createElement("div");
      item.className = "disk-item";
      item.dataset.diskName = d.name;
      diskBars.appendChild(item);
    }
    
    // Determine bar color based on usage
    let barGradient = "linear-gradient(90deg, var(--accent-secondary), var(--accent-primary))";
    if (percent > 85) barGradient = "linear-gradient(90deg, #f59e0b, #ef4444)";
    else if (percent > 70) barGradient = "linear-gradient(90deg, #10b981, #f59e0b)";
    
    item.innerHTML = `
      <div class="disk-head">
        <strong>/dev/${d.name} ${d.mountpoint ? `(${d.mountpoint})` : ""}</strong>
        <span class="size">${fmtGB(d.size)}</span>
      </div>
      <div class="bar-wrap">
        <div class="bar-fill" style="width:${barWidth}%; background:${barGradient};"></div>
      </div>
      <div class="kv">${usageText}</div>
      <div class="kv" style="color:var(--text-muted); margin-top:4px;">Partitions: ${partText || "None"}</div>
    `;
  });
}

// --- CHART.JS SETUP ---
Chart.defaults.color = '#94a3b8';
Chart.defaults.font.family = "'Inter', sans-serif";

function getCommonChartOptions(yMax = null, yTitle = '') {
  return {
    responsive: true,
    maintainAspectRatio: false,
    animation: { duration: 400 },
    interaction: { mode: 'index', intersect: false },
    plugins: {
      legend: { display: false },
      tooltip: {
        backgroundColor: 'rgba(15, 23, 42, 0.9)',
        titleColor: '#f8fafc',
        bodyColor: '#e2e8f0',
        borderColor: 'rgba(255,255,255,0.1)',
        borderWidth: 1,
        padding: 10,
        cornerRadius: 8,
        displayColors: true,
      }
    },
    scales: {
      x: {
        grid: { display: false, drawBorder: false },
        ticks: { maxTicksLimit: 6, maxRotation: 0 }
      },
      y: {
        max: yMax,
        min: 0,
        grid: { color: 'rgba(255, 255, 255, 0.05)', drawBorder: false },
        title: { display: !!yTitle, text: yTitle, color: '#64748b' }
      }
    },
    elements: {
      point: { radius: 0, hitRadius: 10, hoverRadius: 4 },
      line: { tension: 0.4, borderWidth: 2 }
    }
  };
}

function initChart(canvasId, label, colorHex, fillHex, bounds = null) {
  const ctx = document.getElementById(canvasId).getContext('2d');
  
  // Create gradient
  const gradient = ctx.createLinearGradient(0, 0, 0, 240);
  gradient.addColorStop(0, fillHex);
  gradient.addColorStop(1, 'rgba(0,0,0,0)');

  const options = getCommonChartOptions(bounds);

  const chart = new Chart(ctx, {
    type: 'line',
    data: {
      labels: [],
      datasets: [{
        label: label,
        data: [],
        borderColor: colorHex,
        backgroundColor: gradient,
        fill: true,
      }]
    },
    options: options
  });
  
  charts[canvasId] = chart;
  return chart;
}

function initMultiChart(canvasId, datasetsConfig) {
  const ctx = document.getElementById(canvasId).getContext('2d');
  
  const datasets = datasetsConfig.map(c => {
    const gradient = ctx.createLinearGradient(0, 0, 0, 240);
    gradient.addColorStop(0, c.fillHex);
    gradient.addColorStop(1, 'rgba(0,0,0,0)');
    return {
      label: c.label,
      data: [],
      borderColor: c.colorHex,
      backgroundColor: gradient,
      fill: true,
    };
  });

  const options = getCommonChartOptions(null);
  options.plugins.legend.display = true;
  options.plugins.legend.position = 'top';
  options.plugins.legend.align = 'end';
  options.plugins.legend.labels = { boxWidth: 12, usePointStyle: true };

  const chart = new Chart(ctx, {
    type: 'line',
    data: { labels: [], datasets: datasets },
    options: options
  });
  
  charts[canvasId] = chart;
  return chart;
}

function initAllCharts() {
  initChart('cpuChart', 'CPU (%)', '#38bdf8', 'rgba(56, 189, 248, 0.4)', 100);
  initChart('memChart', 'Memory (%)', '#10b981', 'rgba(16, 185, 129, 0.4)', 100);
  initChart('gpuChart', 'GPU (%)', '#a855f7', 'rgba(168, 85, 247, 0.4)', 100);
  
  // Temp doesn't have fixed 100 max
  const tempOptions = getCommonChartOptions(null);
  tempOptions.scales.y.suggestedMin = 20;
  tempOptions.scales.y.suggestedMax = 80;
  const tempCtx = document.getElementById('tempChart').getContext('2d');
  const tempGrad = tempCtx.createLinearGradient(0,0,0,240);
  tempGrad.addColorStop(0, 'rgba(245, 158, 11, 0.4)');
  tempGrad.addColorStop(1, 'rgba(0,0,0,0)');
  charts['tempChart'] = new Chart(tempCtx, {
    type: 'line',
    data: {
      labels: [],
      datasets: [{ label: 'Temp (°C)', data: [], borderColor: '#f59e0b', backgroundColor: tempGrad, fill: true }]
    },
    options: tempOptions
  });

  initMultiChart('diskChart', [
    { label: 'Read MB/s', colorHex: '#0ea5e9', fillHex: 'rgba(14, 165, 233, 0.3)' },
    { label: 'Write MB/s', colorHex: '#8b5cf6', fillHex: 'rgba(139, 92, 246, 0.3)' }
  ]);
  
  initMultiChart('netChart', [
    { label: 'TX KB/s', colorHex: '#14b8a6', fillHex: 'rgba(20, 184, 166, 0.3)' },
    { label: 'RX KB/s', colorHex: '#f43f5e', fillHex: 'rgba(244, 63, 94, 0.3)' }
  ]);
}

function updateChart(canvasId, labels, dataArray) {
  if (!charts[canvasId]) return;
  charts[canvasId].data.labels = labels;
  charts[canvasId].data.datasets[0].data = dataArray;
  charts[canvasId].update();
}

function updateMultiChart(canvasId, labels, dataArrays) {
  if (!charts[canvasId]) return;
  charts[canvasId].data.labels = labels;
  dataArrays.forEach((arr, i) => {
    if (charts[canvasId].data.datasets[i]) {
      charts[canvasId].data.datasets[i].data = arr;
    }
  });
  charts[canvasId].update();
}

function clearAllCharts() {
  updateChart('cpuChart', [], []);
  updateChart('memChart', [], []);
  updateChart('gpuChart', [], []);
  updateChart('tempChart', [], []);
  updateMultiChart('diskChart', [], [[], []]);
  updateMultiChart('netChart', [], [[], []]);
}

function renderSelectedDeviceUnavailable(item) {
  clearAllCharts();
  const status = getDeviceStatus(item);
  const latest = item?.latest || null;
  const dt = latest ? parseTimestamp(latest.timestamp) : null;
  const displayTime = dt ? fmtDateTimeUTC8.format(dt) : String(latest?.timestamp || "Unknown");
  const facts = [];
  if (status.state === "connection_failed") {
    facts.push(`<div><span style="color:#64748b">Error:</span> ${status.message || "Unknown connection error"}</div>`);
  }
  if (status.state === "offline") {
    facts.push(`<div><span style="color:#64748b">Last Update:</span> ${displayTime}</div>`);
    facts.push(`<div><span style="color:#64748b">Data Age:</span> ${fmtAgeSeconds(item?.file_age_seconds)}</div>`);
  }
  if (status.state === "no_data" || status.state === "unknown") {
    facts.push(`<div><span style="color:#64748b">Reason:</span> ${status.message}</div>`);
  }
  summary.innerHTML = `
    <div style="display:flex; flex-direction:column; gap:8px;">
      <div><strong style="color:${status.color}; font-size:16px;">${item?.device_name || selectedDevice} ${status.label}</strong></div>
      <div style="font-size:13px; color:#cbd5e1;">${status.message}</div>
    </div>
  `;
  userSummary.innerHTML = `
    <div style="display:flex; flex-direction:column; gap:8px;">
      <div style="color:var(--text-bright)">Status</div>
      <div style="display:flex; flex-wrap:wrap; gap:16px; font-size:13px; color:#cbd5e1;">
        ${facts.join("")}
      </div>
    </div>
  `;
}

function renderSelectedDeviceOffline(item) {
  renderSelectedDeviceUnavailable(item);
}

function formatLabel(ts) {
  if (!ts) return "";
  const d = parseTimestamp(ts);
  if (!d) return "";
  if (selectedRange === "1w" || selectedRange === "1m") {
    return fmtDateUTC8.format(d);
  }
  return fmtTimeUTC8.format(d);
}

// --- DATA FETCHING ---
async function refreshOverview() {
  overviewController = replaceController(overviewController);
  const controller = overviewController;
  const data = await fetchJson("/api/overview", { signal: controller.signal });
  renderOverviewCards(data.devices || []);
}

async function refreshStatusOverview(force = false) {
  if (!force && statusOverviewNextRefreshAt && Date.now() < statusOverviewNextRefreshAt) {
    return;
  }
  statusHistoryController = replaceController(statusHistoryController);
  const controller = statusHistoryController;
  statusOverviewMeta.textContent = force ? "Refreshing one-day history..." : "Loading one-day history...";
  const url = force ? "/api/status-history?force=1" : "/api/status-history";
  const payload = await fetchJson(url, { signal: controller.signal });
  renderStatusOverview(payload);
  statusOverviewNextRefreshAt = payload.cache_slot && payload.bucket_seconds
    ? (Number(payload.cache_slot) + 1) * Number(payload.bucket_seconds) * 1000 + 1000
    : Date.now() + STATUS_OVERVIEW_REFRESH_MS;
}

async function refreshDisks() {
  if (!selectedDevice) return;
  const overviewItem = overviewStateByDevice.get(selectedDevice);
  const status = getDeviceStatus(overviewItem);
  if (overviewItem && status.state !== "online") {
    diskMeta.innerHTML = `<span style="color:${status.color}">Device:</span> ${overviewItem?.device_name || selectedDevice} &nbsp;|&nbsp; <span style="color:${status.color}">Status:</span> ${status.label}`;
    diskBars.innerHTML = `<div class="disk-item"><div class="kv">Disk details are unavailable: ${status.message}</div></div>`;
    return;
  }
  disksController = replaceController(disksController);
  const controller = disksController;
  const seq = ++disksReqSeq;
  const deviceId = selectedDevice;
  const payload = await fetchJson(`/api/device/${encodeURIComponent(deviceId)}/disks`, { signal: controller.signal });
  // If a newer request started (e.g., user clicked another device), drop stale results.
  if (seq !== disksReqSeq || deviceId !== selectedDevice) return;
  renderDiskBars(payload);
}

async function refreshSelectedMetrics() {
  if (!selectedDevice) return;
  const overviewItem = overviewStateByDevice.get(selectedDevice);
  if (overviewItem && isDeviceUnavailable(overviewItem)) {
    renderSelectedDeviceUnavailable(overviewItem);
    return;
  }
  metricsController = replaceController(metricsController);
  const controller = metricsController;
  const seq = ++metricsReqSeq;
  const deviceId = selectedDevice;
  const payload = await fetchJson(`/api/device/${encodeURIComponent(deviceId)}/metrics?range=${selectedRange}`, {
    signal: controller.signal
  });
  // If a newer request started (e.g., user clicked another device), drop stale results.
  if (seq !== metricsReqSeq || deviceId !== selectedDevice) return;
  const points = payload.points || [];
  
  const labels = points.map(p => formatLabel(p.timestamp));
  
  updateChart('cpuChart', labels, points.map(p => p.cpu_percent));
  updateChart('memChart', labels, points.map(p => p.memory_percent));
  updateChart('gpuChart', labels, points.map(p => p.gpu_percent));
  updateChart('tempChart', labels, points.map(p => p.cpu_temp_c));
  
  updateMultiChart('diskChart', labels, [
    points.map(p => (p.disk_read_rate || 0) / 1024 / 1024),
    points.map(p => (p.disk_write_rate || 0) / 1024 / 1024)
  ]);
  
  updateMultiChart('netChart', labels, [
    points.map(p => (p.net_tx_rate || 0) / 1024),
    points.map(p => (p.net_rx_rate || 0) / 1024)
  ]);

  const latest = payload.latest;
  if (!latest) {
    const maybeOffline = overviewStateByDevice.get(deviceId);
    if (isDeviceUnavailable(maybeOffline)) {
      renderSelectedDeviceUnavailable(maybeOffline);
      return;
    }
    clearAllCharts();
    summary.innerHTML = `<span style="color:var(--accent-warning)">No data in selected range.</span>`;
    userSummary.innerHTML = "";
    return;
  }
  const memTotalGb = latest.memory_total ? (latest.memory_total / 1024 / 1024 / 1024).toFixed(1) : "?";
  const memUsedGb = latest.memory_used ? (latest.memory_used / 1024 / 1024 / 1024).toFixed(1) : "?";

  summary.innerHTML = `
    <div style="display:flex; flex-direction:column; gap:8px;">
        <div><strong style="color:var(--text-bright); font-size:16px;">${payload.device_name} Hardware Specs</strong></div>
        <div style="display:flex; flex-wrap:wrap; gap:16px; font-size:13px; color:#cbd5e1;">
            <div><span style="color:#64748b">CPU:</span> ${latest.cpu_model || "Unknown"} (${latest.cpu_cores || "?"} Cores)</div>
            <div><span style="color:#64748b">RAM:</span> ${memUsedGb} GB / ${memTotalGb} GB</div>
            <div><span style="color:#64748b">GPU:</span> ${latest.gpu_name || "None"}</div>
        </div>
    </div>
  `;
  userSummary.innerHTML = `
    <div style="display:flex; flex-direction:column; gap:8px;">
        <div style="color:var(--text-bright)">Status & Processes</div>
        <div style="display:flex; flex-wrap:wrap; gap:16px; font-size:13px; color:#cbd5e1;">
            <div><span style="color:#64748b">Last Updated (UTC+8):</span> ${(() => { const d = parseTimestamp(latest.timestamp); return d ? fmtDateTimeUTC8.format(d) : String(latest.timestamp || ''); })()}</div>
            <div><span style="color:#64748b">Top Process User:</span> ${fmtTopProcessUser(latest.top_process_user)}</div>
        </div>
    </div>
  `;
}

async function refreshAll() {
  if (refreshAllPromise) return refreshAllPromise;
  setStatus(`Fetching...`);
  refreshAllPromise = (async () => {
    try {
      await refreshOverview();
      if (selectedView === "status-overview") {
        await refreshStatusOverview(false);
      } else {
        await refreshSelectedMetrics();
      }
      setStatus(`Live`);
    } catch (err) {
      if (err.name === "AbortError") {
        return;
      }
      setStatus(`Error: ${err.message}`);
    } finally {
      refreshAllPromise = null;
    }
  })();
  return refreshAllPromise;
}

// --- INITIALIZATION ---
function bindEvents() {
  refreshBtn.addEventListener("click", async () => {
    if (selectedView === "status-overview") {
      setStatus("Refreshing status history...");
      try {
        statusOverviewNextRefreshAt = 0;
        await Promise.all([refreshOverview(), refreshStatusOverview(false)]);
        setStatus("Live");
      } catch (err) {
        if (err.name !== "AbortError") setStatus(`Error: ${err.message}`);
      }
      return;
    }
    refreshAll();
  });
  statusOverviewBtn.addEventListener("click", async () => {
    setView("status-overview");
    try {
      await refreshStatusOverview(false);
      setStatus("Live");
    } catch (err) {
      if (err.name !== "AbortError") setStatus(`Error: ${err.message}`);
    }
  });
  deviceDetailBtn.addEventListener("click", async () => {
    setView("device-detail");
    try {
      await Promise.all([refreshSelectedMetrics(), refreshDisks()]);
      setStatus("Live");
    } catch (err) {
      if (err.name !== "AbortError") setStatus(`Error: ${err.message}`);
    }
  });
  document.querySelectorAll(".range-btn").forEach((btn) => {
    btn.addEventListener("click", async () => {
      document.querySelectorAll(".range-btn").forEach((x) => x.classList.remove("active"));
      btn.classList.add("active");
      selectedRange = btn.dataset.range;
      try {
        await refreshSelectedMetrics();
      } catch (err) {
        if (err.name !== "AbortError") throw err;
      }
    });
  });
  window.addEventListener("resize", () => {
    Object.values(charts).forEach(c => c.resize());
  });
}

async function init() {
  initAllCharts();
  bindEvents();
  setView("status-overview");
  
  try {
    const deviceResp = await fetchJson("/api/devices");
    devices = deviceResp.devices || [];
    if (devices.length > 0 && !selectedDevice) {
      selectedDevice = devices[0].id;
    }
  } catch (e) {
    setStatus(`Failed: ${e.message}`);
    return;
  }
  
  try {
    await refreshAll();
  } catch (err) {
    if (err.name !== "AbortError") {
      setStatus(`Failed: ${err.message}`);
      return;
    }
  }
  
  if (refreshTimer) clearInterval(refreshTimer);
  refreshTimer = setInterval(refreshAll, 10000);
  
  if (diskRefreshTimer) clearInterval(diskRefreshTimer);
  diskRefreshTimer = setInterval(() => {
    if (selectedView !== "device-detail") return;
    refreshDisks().catch((err) => {
      if (err.name !== "AbortError") console.error(err);
    });
  }, 60000);
}

document.addEventListener("DOMContentLoaded", init);
