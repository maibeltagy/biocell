/* ==========================================================================
   viewer.js
   Interactive slice viewer for #viewer-section.

   Responsibilities:
     - t slider (always) and z slider (only in single-slice mode) drive
       which frame to fetch from GET /jobs/{job_id}/frame?t=..&z=..
     - Draws the returned PNG onto #slice-canvas, then overlays small
       circles at the (x, y) position of every detected node whose `t`
       matches the current timepoint (using currentResult.nodes -- no
       re-fetch needed for the overlay itself).
     - Hover/click hit-testing against the drawn node positions, shown via
       a small floating tooltip. (Phase 5 will hook click into the
       lineage tree.)

   Exposes window.initViewer(jobId, result), called by app.js once a job
   finishes and its result JSON has been fetched.
   ========================================================================== */

(function () {
  const canvas = document.getElementById("slice-canvas");
  const ctx = canvas.getContext("2d");

  const tSlider = document.getElementById("t-slider");
  const tValueLabel = document.getElementById("t-value");
  const zSlider = document.getElementById("z-slider");
  const zValueLabel = document.getElementById("z-value");
  const zSliderRow = document.getElementById("z-slider-row");
  const projectionToggle = document.getElementById("projection-toggle");
  const tooltip = document.getElementById("viewer-tooltip");

  const NODE_RADIUS = 4;        // drawn circle radius, in canvas (native image) pixels
  const HIT_RADIUS = 8;         // hover/click hit-test radius, in canvas pixels
  const HIGHLIGHT_RADIUS = 8;   // larger radius for a node highlighted from the lineage tree

  function cssVar(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }

  let jobId = null;
  let result = null;

  // Set by highlightNode() (called from lineage.js) when a node is clicked
  // in the lineage tree. Drawn with a distinct color/larger radius so the
  // two views visibly tie together.
  let highlightedNodeId = null;

  // Screen-space (canvas-pixel-space) positions of the circles currently
  // drawn, rebuilt every time a new frame is drawn. Used for hit-testing
  // hover/click without re-walking the full node list each time.
  let drawnNodePositions = [];

  // -- Public entry point, called by app.js once a job is done ------------

  window.initViewer = function (newJobId, newResult) {
    jobId = newJobId;
    result = newResult;
    highlightedNodeId = null;

    const nFrames = result.stats.cell_count_per_frame.length;
    tSlider.min = "0";
    tSlider.max = String(Math.max(0, nFrames - 1));
    tSlider.value = "0";
    tValueLabel.textContent = "0";

    // volume_shape is [T, Z, Y, X], stashed onto the result by the backend
    // specifically so the viewer can size the Z slider correctly.
    const volumeShape = result.volume_shape;
    if (volumeShape) {
      const nZ = volumeShape[1];
      zSlider.min = "0";
      zSlider.max = String(Math.max(0, nZ - 1));
      zSlider.value = "0";
      zValueLabel.textContent = "0";
    }

    updateZSliderVisibility();
    loadFrame();
  };

  /**
   * Public entry point called from lineage.js when a node is clicked in the
   * lineage tree: jumps the t (and, in single-slice mode, z) slider to that
   * node's position, then redraws with it marked distinctly on the canvas.
   */
  window.highlightNode = function (nodeId) {
    if (!result) return;

    const node = result.nodes.find((n) => n.id === nodeId);
    if (!node) {
      console.warn(`highlightNode: no node with id ${nodeId} in the current result`);
      return;
    }

    highlightedNodeId = nodeId;

    tSlider.value = String(node.t);
    tValueLabel.textContent = String(node.t);

    // A max projection already shows every Z, but in single-slice mode we
    // need to jump to the node's actual Z or its highlight circle would be
    // drawn on a slice that doesn't show it.
    if (!isProjectionMode()) {
      zSlider.value = String(node.z);
      zValueLabel.textContent = String(node.z);
    }

    loadFrame();

    document
      .getElementById("viewer-section")
      .scrollIntoView({ behavior: "smooth", block: "nearest" });
  };

  // -- Control wiring -------------------------------------------------------

  tSlider.addEventListener("input", () => {
    tValueLabel.textContent = tSlider.value;
    loadFrame();
  });

  zSlider.addEventListener("input", () => {
    zValueLabel.textContent = zSlider.value;
    loadFrame();
  });

  projectionToggle.addEventListener("change", () => {
    updateZSliderVisibility();
    loadFrame();
  });

  function updateZSliderVisibility() {
    zSliderRow.classList.toggle("hidden", projectionToggle.checked);
  }

  function isProjectionMode() {
    return projectionToggle.checked;
  }

  // -- Frame fetching + drawing ---------------------------------------------

  async function loadFrame() {
    if (!jobId) return;

    const t = parseInt(tSlider.value, 10);
    let url = `/jobs/${jobId}/frame?t=${t}`;
    if (!isProjectionMode()) {
      url += `&z=${parseInt(zSlider.value, 10)}`;
    }

    let bitmap;
    try {
      const response = await fetch(url);
      if (!response.ok) {
        console.error(`Failed to fetch frame (HTTP ${response.status})`);
        return;
      }
      const blob = await response.blob();
      bitmap = await createImageBitmap(blob);
    } catch (err) {
      console.error("Error loading frame:", err);
      return;
    }

    // Size the canvas's internal pixel buffer to match the image's native
    // resolution exactly. Node (x, y) coordinates are already in that same
    // pixel space, so once the canvas buffer matches, they can be drawn
    // directly with no extra transform. CSS (max-width: 100%; height: auto)
    // controls the *displayed* size separately -- see eventToCanvasCoords()
    // below for how hover/click positions get mapped back.
    canvas.width = bitmap.width;
    canvas.height = bitmap.height;

    ctx.drawImage(bitmap, 0, 0);
    drawOverlay(t);
  }

  function drawOverlay(t) {
    drawnNodePositions = [];

    const nodesAtT = result.nodes.filter((n) => n.t === t);
    const highlightColor = cssVar("--color-highlight") || cssVar("--warning") || "#f4b942";

    ctx.save();

    for (const node of nodesAtT) {
      const isHighlighted = node.id === highlightedNodeId;
      const radius = isHighlighted ? HIGHLIGHT_RADIUS : NODE_RADIUS;

      ctx.beginPath();
      // Use a vibrant teal for normal nodes, warm gold for highlighted
      ctx.fillStyle = isHighlighted ? highlightColor : "rgba(0, 212, 170, 0.85)";
      ctx.strokeStyle = isHighlighted ? "rgba(8,12,20,0.9)" : "rgba(8,12,20,0.7)";
      ctx.lineWidth = isHighlighted ? 2.5 : 1.5;
      ctx.arc(node.x, node.y, radius, 0, Math.PI * 2);
      ctx.fill();
      ctx.stroke();

      drawnNodePositions.push({ node, canvasX: node.x, canvasY: node.y });
    }

    ctx.restore();
  }

  // -- Hover / click hit-testing ---------------------------------------------

  canvas.addEventListener("mousemove", (event) => {
    const hit = findNodeAtEvent(event);
    if (hit) {
      showTooltip(hit.node, event);
      canvas.style.cursor = "pointer";
    } else {
      hideTooltip();
      canvas.style.cursor = "default";
    }
  });

  canvas.addEventListener("mouseleave", hideTooltip);

  canvas.addEventListener("click", (event) => {
    const hit = findNodeAtEvent(event);
    if (!hit) return;

    highlightedNodeId = hit.node.id;
    drawOverlay(parseInt(tSlider.value, 10));
    showTooltip(hit.node, event);
    console.log("Clicked node:", hit.node);

    // Reverse hook: also highlight this node's path in the lineage tree,
    // if it's currently rendered there (lineage.js defines this).
    if (typeof window.highlightNodeInLineage === "function") {
      window.highlightNodeInLineage(hit.node.id);
    }
  });

  function findNodeAtEvent(event) {
    const { x, y } = eventToCanvasCoords(event);

    let closest = null;
    let closestDist = HIT_RADIUS;

    for (const entry of drawnNodePositions) {
      const dx = entry.canvasX - x;
      const dy = entry.canvasY - y;
      const dist = Math.sqrt(dx * dx + dy * dy);
      if (dist <= closestDist) {
        closest = entry;
        closestDist = dist;
      }
    }
    return closest;
  }

  /**
   * Convert a mouse event's client (CSS pixel) coordinates into the
   * canvas's internal pixel coordinate space. Needed because the canvas
   * can be displayed at a CSS size (responsive, via max-width: 100%) that
   * differs from its internal width/height (which match the native image
   * resolution) -- without this conversion, hit-testing would be wrong on
   * any screen where the canvas is scaled down.
   */
  function eventToCanvasCoords(event) {
    const rect = canvas.getBoundingClientRect();
    const scaleX = canvas.width / rect.width;
    const scaleY = canvas.height / rect.height;
    return {
      x: (event.clientX - rect.left) * scaleX,
      y: (event.clientY - rect.top) * scaleY,
    };
  }

  function showTooltip(node, event) {
    tooltip.textContent = `id ${node.id} \u2014 t=${node.t}, z=${node.z}, y=${node.y}, x=${node.x} (voxel indices)`;
    tooltip.classList.remove("hidden");

    const wrapperRect = canvas.parentElement.getBoundingClientRect();
    tooltip.style.left = `${event.clientX - wrapperRect.left + 12}px`;
    tooltip.style.top = `${event.clientY - wrapperRect.top + 12}px`;
  }

  function hideTooltip() {
    tooltip.classList.add("hidden");
  }
})();
