/* ==========================================================================
   stats.js
   Populates #stats-section: three metric cards, a line chart of
   cell_count_per_frame, and a bar chart of division events by timepoint.

   Exposes window.renderStats(result), called by app.js once a job's
   result JSON has been fetched. Reads only from result.stats and
   result.nodes -- no additional network requests.
   ========================================================================== */

(function () {
  // Read CSS custom properties at render time so chart colors always stay
  // in sync with the dark theme defined in style.css.
  function cssVar(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }

  // Apply global Chart.js defaults for the dark theme once.
  if (window.Chart) {
    Chart.defaults.color = "#7a899e";
    Chart.defaults.borderColor = "rgba(255,255,255,0.08)";
    Chart.defaults.font.family = "'Inter', sans-serif";
  }

  // Chart.js instances are kept here so re-running an analysis (a second
  // job in the same page session) destroys the old chart before drawing a
  // new one -- Chart.js throws if you re-init a canvas that already has a
  // live chart attached to it.
  let cellCountChart = null;
  let divisionEventsChart = null;

  window.renderStats = function (result) {
    const stats = result.stats;

    renderMetricCards(result, stats);
    renderVoxelScaleNote(result);
    renderCellCountChart(stats);
    renderDivisionEventsChart(stats);
  };

  // -- Metric cards -----------------------------------------------------

  function renderMetricCards(result, stats) {
    // "Total cells detected" = total number of detections across all
    // frames, i.e. every node in the graph (not a per-frame count).
    const totalCells = result.nodes.length;
    const totalDivisions = stats.division_events.length;
    const avgSpeed = stats.avg_speed_um_per_frame;

    document.getElementById("metric-total-cells").textContent = totalCells.toLocaleString();
    document.getElementById("metric-division-events").textContent = totalDivisions.toLocaleString();
    document.getElementById("metric-avg-speed").textContent = avgSpeed.toFixed(2);
  }

  // -- Voxel scale note ---------------------------------------------------
  // All node/edge coordinates elsewhere in the app (viewer tooltips,
  // lineage tree labels, exported CSV) are in voxel indices, NOT physical
  // units -- only this dashboard's "avg. speed" is converted to µm, using
  // the anisotropic voxel spacing below. Surfacing that spacing here makes
  // the conversion factor visible instead of an implicit backend detail.
  function renderVoxelScaleNote(result) {
    const note = document.getElementById("voxel-scale-note");
    const scale = result.voxel_scale_um; // [z, y, x], in microns/voxel

    if (!scale) {
      note.textContent = "";
      return;
    }

    const [z, y, x] = scale;
    note.textContent =
      `Voxel scale used for physical units: z=${z} \u00b5m, y=${y} \u00b5m, x=${x} \u00b5m per voxel. ` +
      `All other coordinates shown in this app (viewer, lineage tree, CSV export) are raw voxel indices.`;
  }

  // -- Cell count over time (line chart) ---------------------------------

  function renderCellCountChart(stats) {
    const counts = stats.cell_count_per_frame;
    const labels = counts.map((_, t) => t);

    const ctx = document.getElementById("cell-count-chart").getContext("2d");

    if (cellCountChart) {
      cellCountChart.destroy();
    }

    const accent  = cssVar("--accent")     || "#00d4aa";
    const accent2  = cssVar("--accent2")    || "#4f8ef7";
    const muted    = cssVar("--text-muted") || "#7a899e";
    const border   = cssVar("--border")     || "rgba(255,255,255,0.08)";
    const bgDeep   = cssVar("--bg-deep")    || "#080c14";

    cellCountChart = new Chart(ctx, {
      type: "line",
      data: {
        labels,
        datasets: [
          {
            label: "Cell count",
            data: counts,
            borderColor: accent,
            backgroundColor: (context) => {
              const gradient = context.chart.ctx.createLinearGradient(0, 0, 0, context.chart.height);
              gradient.addColorStop(0, "rgba(0,212,170,0.25)");
              gradient.addColorStop(1, "rgba(0,212,170,0.01)");
              return gradient;
            },
            pointRadius: 3,
            pointHoverRadius: 6,
            pointBackgroundColor: accent,
            pointBorderColor: bgDeep,
            pointBorderWidth: 1.5,
            borderWidth: 2,
            tension: 0.35,
            fill: true,
          },
        ],
      },
      options: {
        responsive: true,
        animation: { duration: 700, easing: "easeOutQuart" },
        plugins: {
          legend: { display: false },
          tooltip: {
            backgroundColor: "rgba(8,12,20,0.92)",
            titleColor: muted,
            bodyColor: accent,
            borderColor: "rgba(0,212,170,0.35)",
            borderWidth: 1,
          },
        },
        scales: {
          x: {
            title: { display: true, text: "Timepoint (t)", color: muted, font: { size: 11 } },
            grid: { color: border },
            ticks: { color: muted, maxTicksLimit: 12 },
          },
          y: {
            title: { display: true, text: "Cell count", color: muted, font: { size: 11 } },
            beginAtZero: true,
            grid: { color: border },
            ticks: { color: muted, precision: 0 },
          },
        },
      },
    });
  }

  // -- Division events by timepoint (bar chart) ---------------------------

  function renderDivisionEventsChart(stats) {
    const events = stats.division_events;
    const canvas = document.getElementById("division-events-chart");
    const emptyNote = document.getElementById("no-divisions-note");

    if (divisionEventsChart) {
      divisionEventsChart.destroy();
      divisionEventsChart = null;
    }

    if (events.length === 0) {
      canvas.classList.add("hidden");
      emptyNote.classList.remove("hidden");
      return;
    }

    canvas.classList.remove("hidden");
    emptyNote.classList.add("hidden");

    // Count events per timepoint (more than one division can happen in
    // the same frame), then present as a sparse bar chart over the
    // timepoints that actually had one.
    const countsByT = {};
    for (const event of events) {
      countsByT[event.t] = (countsByT[event.t] || 0) + 1;
    }
    const sortedTs = Object.keys(countsByT)
      .map(Number)
      .sort((a, b) => a - b);
    const counts = sortedTs.map((t) => countsByT[t]);

    const ctx = canvas.getContext("2d");

    const accent  = cssVar("--accent")     || "#00d4aa";
    const accent2  = cssVar("--accent2")    || "#4f8ef7";
    const muted    = cssVar("--text-muted") || "#7a899e";
    const border   = cssVar("--border")     || "rgba(255,255,255,0.08)";

    divisionEventsChart = new Chart(ctx, {
      type: "bar",
      data: {
        labels: sortedTs,
        datasets: [
          {
            label: "Division events",
            data: counts,
            backgroundColor: accent2,
            borderRadius: 5,
            maxBarThickness: 32,
          },
        ],
      },
      options: {
        responsive: true,
        animation: { duration: 700, easing: "easeOutQuart" },
        plugins: {
          legend: { display: false },
          tooltip: {
            backgroundColor: "rgba(8,12,20,0.92)",
            titleColor: muted,
            bodyColor: accent2,
            borderColor: "rgba(79,142,247,0.35)",
            borderWidth: 1,
          },
        },
        scales: {
          x: {
            title: { display: true, text: "Timepoint (t)", color: muted, font: { size: 11 } },
            grid: { display: false },
            ticks: { color: muted },
          },
          y: {
            title: { display: true, text: "Events", color: muted, font: { size: 11 } },
            beginAtZero: true,
            grid: { color: border },
            ticks: { color: muted, precision: 0, stepSize: 1 },
          },
        },
      },
    });
  }
})();
