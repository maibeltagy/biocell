/* ==========================================================================
   app.js
   Wires up the upload flow: file select / drag-and-drop → POST /jobs →
   poll GET /jobs/{id}/status until done/error → fetch result and initialise
   all result panels (viewer, stats, lineage, export, chat).

   Includes Tab Navigation Manager for single-tab Workspace view (Sidebar).
   ========================================================================== */

const POLL_INTERVAL_MS = 1500;

/** Most-recently fetched job result: { nodes, edges, stats, volume_shape, voxel_scale_um }. */
window.currentResult = null;

/** Job-id of the most-recently completed job (used by export.js, chat.js). */
window.currentJobId = null;

// ---------------------------------------------------------------------------
// DOM references
// ---------------------------------------------------------------------------

const fileInput        = document.getElementById("file-input");
const dropZone         = document.getElementById("drop-zone");
const fileSelectedInfo = document.getElementById("file-selected-info");
const fileNameDisplay  = document.getElementById("file-name-display");
const analyzeButton    = document.getElementById("analyze-button");

const statusSection     = document.getElementById("status-section");
const statusSpinner     = document.getElementById("status-spinner");
const statusText        = document.getElementById("status-text");
const progressBarWrap   = document.getElementById("progress-bar-wrap");

const topBarIcon        = document.getElementById("top-bar-icon");
const topBarHeading     = document.getElementById("top-bar-heading");
const headerStatusText  = document.getElementById("header-status-text");
const sidebarDatasetName = document.getElementById("sidebar-dataset-name");
const statusIndicatorDot = document.getElementById("status-indicator-dot");

const RESULT_SECTION_IDS = [
  "viewer-section",
  "lineage-section",
  "stats-section",
  "export-section",
  "chat-section",
];

const TAB_METADATA = {
  "upload-section" : { title: "Upload Volume", icon: "📁" },
  "viewer-section" : { title: "Slice Viewer", icon: "🖼" },
  "stats-section"  : { title: "Stats Dashboard", icon: "📊" },
  "lineage-section": { title: "Lineage Tree", icon: "🌿" },
  "export-section" : { title: "Export Results", icon: "💾" },
  "chat-section"   : { title: "Ask AI Assistant", icon: "🤖" },
};

let currentTabId = "upload-section";

// ---------------------------------------------------------------------------
// Tab Navigation Handler
// ---------------------------------------------------------------------------

function initTabNavigation() {
  const navItems = document.querySelectorAll(".sidebar-nav .nav-item");

  navItems.forEach((btn) => {
    btn.addEventListener("click", () => {
      if (btn.classList.contains("disabled")) return;
      const targetId = btn.getAttribute("data-target");
      if (targetId) switchTab(targetId);
    });
  });
}

function switchTab(targetId) {
  if (!TAB_METADATA[targetId]) return;

  currentTabId = targetId;

  // 1. Update Nav items active state
  document.querySelectorAll(".sidebar-nav .nav-item").forEach((btn) => {
    const isTarget = btn.getAttribute("data-target") === targetId;
    btn.classList.toggle("active", isTarget);
  });

  // 2. Hide all tab panels and reveal target
  document.querySelectorAll(".tab-panel").forEach((panel) => {
    if (panel.id === targetId) {
      panel.classList.remove("hidden");
      panel.classList.add("active");
    } else {
      panel.classList.add("hidden");
      panel.classList.remove("active");
    }
  });

  // 3. Update Top Header Bar
  const meta = TAB_METADATA[targetId];
  if (topBarIcon) topBarIcon.textContent = meta.icon;
  if (topBarHeading) topBarHeading.textContent = meta.title;

  // 4. Trigger panel-specific updates if needed (e.g. Chart.js resize)
  if (targetId === "stats-section" && window.currentResult) {
    if (typeof window.renderStats === "function") {
      window.renderStats(window.currentResult);
    }
  }
}

// ---------------------------------------------------------------------------
// Drag-and-drop handling
// ---------------------------------------------------------------------------

if (dropZone) {
  ["dragenter", "dragover"].forEach((evt) => {
    dropZone.addEventListener(evt, (e) => {
      e.preventDefault();
      dropZone.classList.add("drag-over");
    });
  });

  ["dragleave", "drop"].forEach((evt) => {
    dropZone.addEventListener(evt, (e) => {
      e.preventDefault();
      dropZone.classList.remove("drag-over");
    });
  });

  dropZone.addEventListener("drop", (e) => {
    const files = e.dataTransfer?.files;
    if (files && files.length > 0) {
      const dt = new DataTransfer();
      dt.items.add(files[0]);
      fileInput.files = dt.files;
      handleFileSelected(files[0]);
    }
  });
}

// ---------------------------------------------------------------------------
// File selection
// ---------------------------------------------------------------------------

if (fileInput) {
  fileInput.addEventListener("change", () => {
    const file = fileInput.files[0];
    if (file) handleFileSelected(file);
  });
}

function handleFileSelected(file) {
  if (fileNameDisplay) fileNameDisplay.textContent = file.name;
  if (fileSelectedInfo) fileSelectedInfo.classList.add("visible");
  if (analyzeButton) analyzeButton.disabled = false;
  if (sidebarDatasetName) sidebarDatasetName.textContent = file.name;
}

// ---------------------------------------------------------------------------
// Analyze button
// ---------------------------------------------------------------------------

if (analyzeButton) {
  analyzeButton.addEventListener("click", async () => {
    const file = fileInput.files[0];
    if (!file) return;

    // Reset UI.
    hideResultSections();
    window.currentResult = null;
    window.currentJobId  = null;
    if (typeof window.resetExport === "function") window.resetExport();
    if (typeof window.resetChat   === "function") window.resetChat();

    analyzeButton.disabled = true;
    fileInput.disabled     = true;

    showStatus("Submitting job\u2026", { spinning: true, error: false, progress: true });
    if (headerStatusText) headerStatusText.textContent = "Processing...";

    try {
      const jobId  = await submitJob(file);
      await pollUntilFinished(jobId);
      const result = await fetchResult(jobId);

      window.currentResult = result;
      window.currentJobId  = jobId;
      console.log("Job result:", result);

      if (typeof window.initViewer    === "function") window.initViewer(jobId, result);
      if (typeof window.renderStats   === "function") window.renderStats(result);
      if (typeof window.renderLineage === "function") window.renderLineage(result);

      showStatus("Analysis complete \u2714", { spinning: false, error: false, progress: false });
      if (headerStatusText) headerStatusText.textContent = "Complete";
      if (statusIndicatorDot) statusIndicatorDot.classList.add("active");
      if (sidebarDatasetName) sidebarDatasetName.textContent = file.name;

      revealResultSections();
      switchTab("viewer-section");
    } catch (err) {
      console.error(err);
      showStatus(`Error: ${err.message}`, { spinning: false, error: true, progress: false });
      if (headerStatusText) headerStatusText.textContent = "Error";
      if (statusIndicatorDot) statusIndicatorDot.classList.remove("active");
    } finally {
      analyzeButton.disabled = false;
      fileInput.disabled     = false;
    }
  });
}

// ---------------------------------------------------------------------------
// API calls
// ---------------------------------------------------------------------------

async function submitJob(file) {
  const formData = new FormData();
  formData.append("file", file);

  const response = await fetch("/jobs", { method: "POST", body: formData });
  if (!response.ok) {
    throw new Error(await extractErrorDetail(response, "Failed to submit job"));
  }
  const data = await response.json();
  return data.job_id;
}

async function pollUntilFinished(jobId) {
  while (true) {
    const response = await fetch(`/jobs/${jobId}/status`);
    if (!response.ok) {
      throw new Error(await extractErrorDetail(response, "Failed to fetch job status"));
    }
    const status = await response.json();

    if (status.status === "done")  return;
    if (status.status === "error") throw new Error(status.error || "Job failed with an unknown error.");

    showStatus(formatRunningStatus(status.status), { spinning: true, error: false, progress: true });
    await sleep(POLL_INTERVAL_MS);
  }
}

async function fetchResult(jobId) {
  const response = await fetch(`/jobs/${jobId}/result`);
  if (!response.ok) {
    throw new Error(await extractErrorDetail(response, "Failed to fetch job result"));
  }
  return response.json();
}

// ---------------------------------------------------------------------------
// UI helpers
// ---------------------------------------------------------------------------

async function extractErrorDetail(response, fallbackPrefix) {
  try {
    const data = await response.json();
    if (data && typeof data.detail === "string") return data.detail;
  } catch (_err) { /* not JSON */ }
  return `${fallbackPrefix} (HTTP ${response.status})`;
}

function formatRunningStatus(status) {
  if (status === "pending") return "Waiting to start\u2026";
  if (status === "running") return "Processing volume\u2026 this can take a minute.";
  return status;
}

function showStatus(text, { spinning, error, progress }) {
  if (!statusSection) return;
  statusSection.classList.remove("hidden");
  statusSection.classList.toggle("error", error);
  if (statusSpinner) statusSpinner.classList.toggle("hidden", !spinning);
  if (statusText) statusText.textContent = text;
  if (progressBarWrap) progressBarWrap.style.display = progress ? "block" : "none";
}

function hideResultSections() {
  RESULT_SECTION_IDS.forEach((id) => {
    const navBtn = document.querySelector(`.sidebar-nav .nav-item[data-target="${id}"]`);
    if (navBtn) navBtn.classList.add("disabled");
  });
  if (statusIndicatorDot) statusIndicatorDot.classList.remove("active");
}

function revealResultSections() {
  RESULT_SECTION_IDS.forEach((id) => {
    const navBtn = document.querySelector(`.sidebar-nav .nav-item[data-target="${id}"]`);
    if (navBtn) navBtn.classList.remove("disabled");
  });
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function initTheme() {
  const toggleBtn = document.getElementById("theme-toggle-btn");
  const toggleIcon = document.getElementById("theme-toggle-icon");
  const savedTheme = localStorage.getItem("celltrack-theme") || "dark";

  function applyTheme(theme) {
    document.documentElement.setAttribute("data-theme", theme);
    if (toggleIcon) toggleIcon.textContent = theme === "light" ? "☀️" : "🌙";
    localStorage.setItem("celltrack-theme", theme);
  }

  applyTheme(savedTheme);

  if (toggleBtn) {
    toggleBtn.addEventListener("click", () => {
      const current = document.documentElement.getAttribute("data-theme") || "dark";
      applyTheme(current === "light" ? "dark" : "light");
    });
  }
}

// Initialize Navigation & Theme on page load
document.addEventListener("DOMContentLoaded", () => {
  initTheme();
  initTabNavigation();
  switchTab("upload-section");
  hideResultSections();
});
