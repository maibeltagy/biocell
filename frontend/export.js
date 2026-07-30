/* ==========================================================================
   export.js
   Wires up #export-section: a direct CSV download, and an annotated-video
   export that kicks off its own background job on the backend and polls
   its status (same pattern as the main analysis job in app.js), revealing
   a download link once it's ready.

   Reads window.currentJobId directly at click time rather than needing an
   init() call -- the buttons are static and don't need to react to a new
   result until clicked.
   ========================================================================== */

const VIDEO_POLL_INTERVAL_MS = 1500;

(function () {
  const downloadCsvButton = document.getElementById("download-csv-button");
  const exportVideoButton = document.getElementById("export-video-button");
  const videoStatusText = document.getElementById("video-export-status");
  const videoDownloadLink = document.getElementById("video-download-link");

  // -- Reset, called by app.js whenever a new job is submitted -------------

  window.resetExport = function () {
    videoStatusText.textContent = "";
    videoDownloadLink.classList.add("hidden");
    videoDownloadLink.removeAttribute("href");
    exportVideoButton.disabled = false;

    document.getElementById("report-status").textContent = "";
    document.getElementById("report-panel").classList.add("hidden");
    document.getElementById("report-text").textContent = "";
  };

  // -- CSV download -----------------------------------------------------

  downloadCsvButton.addEventListener("click", () => {
    const jobId = window.currentJobId;
    if (!jobId) return;

    // A plain anchor click (rather than fetch+blob) lets the browser handle
    // the download directly, honoring the backend's Content-Disposition
    // header -- no need to juggle blobs for a simple GET download.
    const link = document.createElement("a");
    link.href = `/jobs/${jobId}/export/csv`;
    link.download = `${jobId}_tracks.csv`;
    document.body.appendChild(link);
    link.click();
    link.remove();
  });

  // -- Video export -------------------------------------------------------

  exportVideoButton.addEventListener("click", async () => {
    const jobId = window.currentJobId;
    if (!jobId) return;

    exportVideoButton.disabled = true;
    videoDownloadLink.classList.add("hidden");
    videoStatusText.textContent = "Starting video export\u2026";

    try {
      await startVideoExport(jobId);
      await pollVideoUntilFinished(jobId);

      videoStatusText.textContent = "Video ready.";
      videoDownloadLink.href = `/jobs/${jobId}/export/video/download`;
      videoDownloadLink.classList.remove("hidden");
    } catch (err) {
      console.error(err);
      videoStatusText.textContent = `Error: ${err.message}`;
    } finally {
      exportVideoButton.disabled = false;
    }
  });

  async function startVideoExport(jobId) {
    const response = await fetch(`/jobs/${jobId}/export/video/start`, { method: "POST" });
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      throw new Error(body.detail || `Failed to start video export (HTTP ${response.status})`);
    }
  }

  async function pollVideoUntilFinished(jobId) {
    while (true) {
      const response = await fetch(`/jobs/${jobId}/export/video/status`);
      if (!response.ok) {
        throw new Error(`Failed to fetch video export status (HTTP ${response.status})`);
      }

      const status = await response.json();

      if (status.status === "done") {
        return;
      }
      if (status.status === "error") {
        throw new Error(status.error || "Video export failed.");
      }

      videoStatusText.textContent =
        status.status === "running"
          ? "Rendering annotated video\u2026 this can take a bit."
          : "Waiting to start\u2026";

      await sleep(VIDEO_POLL_INTERVAL_MS);
    }
  }

  function sleep(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
  }

  // -- Report generation --------------------------------------------------
  // No polling needed here -- unlike the video export, the backend does
  // this as a single request/response (no background job), since it's
  // just one LLM call with the numbers already in hand.

  const generateReportButton = document.getElementById("generate-report-button");
  const reportStatusText = document.getElementById("report-status");
  const reportPanel = document.getElementById("report-panel");
  const reportText = document.getElementById("report-text");
  const copyReportButton = document.getElementById("copy-report-button");
  const downloadReportButton = document.getElementById("download-report-button");

  generateReportButton.addEventListener("click", async () => {
    const jobId = window.currentJobId;
    if (!jobId) return;

    generateReportButton.disabled = true;
    reportPanel.classList.add("hidden");
    reportStatusText.textContent = "Generating report\u2026";

    try {
      const response = await fetch(`/jobs/${jobId}/report`, { method: "POST" });
      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        throw new Error(body.detail || `Failed to generate report (HTTP ${response.status})`);
      }
      const data = await response.json();

      reportText.textContent = data.report;
      reportPanel.classList.remove("hidden");
      reportStatusText.textContent = "";
    } catch (err) {
      console.error(err);
      reportStatusText.textContent = `Error: ${err.message}`;
    } finally {
      generateReportButton.disabled = false;
    }
  });

  copyReportButton.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(reportText.textContent);
      const original = copyReportButton.textContent;
      copyReportButton.textContent = "Copied!";
      setTimeout(() => {
        copyReportButton.textContent = original;
      }, 1500);
    } catch (err) {
      console.error("Clipboard copy failed:", err);
    }
  });

  downloadReportButton.addEventListener("click", () => {
    const jobId = window.currentJobId || "report";
    const blob = new Blob([reportText.textContent], { type: "text/plain" });
    const url = URL.createObjectURL(blob);

    const link = document.createElement("a");
    link.href = url;
    link.download = `${jobId}_report.txt`;
    document.body.appendChild(link);
    link.click();
    link.remove();

    URL.revokeObjectURL(url);
  });
})();
