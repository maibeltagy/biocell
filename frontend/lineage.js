/* ==========================================================================
   lineage.js
   Reconstructs and renders the cell lineage tree(s) for #lineage-section.

   Features:
     - Interactive D3 tree layout with enlarged node radius, level heights,
       and horizontal spacing for maximum visual clarity.
     - Scrollable container (overflow: auto) allowing full vertical/horizontal
       scrolling across 100+ timepoint generations.
     - D3 Zoom & Pan behavior (mouse wheel / drag) + Zoom In/Out/Reset buttons.
     - Interactive node click to jump slice viewer to node timepoint.
     - Path highlighting from root to selected node.
   ========================================================================== */

(function () {
  const svg = d3.select("#lineage-svg");
  const trackSelect = document.getElementById("track-select");
  const trackCountNote = document.getElementById("track-count-note");
  const btnZoomIn = document.getElementById("btn-zoom-in");
  const btnZoomOut = document.getElementById("btn-zoom-out");
  const btnZoomReset = document.getElementById("btn-zoom-reset");

  // Layout constants (SVG pixel units) - enlarged for visual clarity
  const NODE_RADIUS = 7;     // drawn circle radius (was 5)
  const NODE_SPACING = 50;   // horizontal gap budgeted per sibling (was 30)
  const LEVEL_HEIGHT = 65;   // vertical gap per generation level (was 42)
  const TRACK_PADDING = 40;  // margin around the whole drawing
  const TRACK_GAP = 60;      // horizontal gap between side-by-side tracks

  const MAX_TRACKS_SIDE_BY_SIDE = 8;

  let forest = null; // array of {node, children: [...]}, sorted largest-first
  let currentZoomTransform = d3.zoomIdentity;

  // -- D3 Zoom & Pan setup --------------------------------------------------

  const zoomBehavior = d3.zoom()
    .scaleExtent([0.3, 3])
    .on("zoom", (event) => {
      currentZoomTransform = event.transform;
      svg.select("g.zoom-container").attr("transform", event.transform);
    });

  svg.call(zoomBehavior);

  if (btnZoomIn) {
    btnZoomIn.addEventListener("click", () => {
      svg.transition().duration(300).call(zoomBehavior.scaleBy, 1.3);
    });
  }

  if (btnZoomOut) {
    btnZoomOut.addEventListener("click", () => {
      svg.transition().duration(300).call(zoomBehavior.scaleBy, 1 / 1.3);
    });
  }

  if (btnZoomReset) {
    btnZoomReset.addEventListener("click", () => {
      svg.transition().duration(300).call(zoomBehavior.transform, d3.zoomIdentity);
    });
  }

  // -- Public entry point, called by app.js once a job is done -------------

  window.renderLineage = function (result) {
    forest = buildForest(result.nodes, result.edges);
    forest.sort((a, b) => countNodes(b) - countNodes(a));

    if (trackCountNote) {
      trackCountNote.textContent =
        forest.length === 1 ? "1 track found." : `${forest.length} independent tracks found.`;
    }

    populateTrackSelect(forest);
    render(trackSelect.value);
  };

  if (trackSelect) {
    trackSelect.addEventListener("change", () => render(trackSelect.value));
  }

  // -- Reverse hook: called by viewer.js when a canvas node is clicked -----

  window.highlightNodeInLineage = function (nodeId) {
    const match = svg.selectAll(".node").filter((d) => d.data.node.id === nodeId);
    if (match.empty()) return;
    highlightPathTo(match.datum());
  };

  // -- Forest construction --------------------------------------------------

  function buildForest(nodes, edges) {
    const nodesById = new Map(nodes.map((n) => [n.id, n]));

    const childIdsBySource = new Map();
    const hasIncomingEdge = new Set();
    for (const edge of edges) {
      if (!childIdsBySource.has(edge.source)) {
        childIdsBySource.set(edge.source, []);
      }
      childIdsBySource.get(edge.source).push(edge.target);
      hasIncomingEdge.add(edge.target);
    }

    function buildSubtree(nodeId) {
      const childIds = childIdsBySource.get(nodeId) || [];
      return {
        node: nodesById.get(nodeId),
        children: childIds.map(buildSubtree),
      };
    }

    const rootIds = nodes.filter((n) => !hasIncomingEdge.has(n.id)).map((n) => n.id);
    return rootIds.map(buildSubtree);
  }

  function countNodes(tree) {
    return 1 + tree.children.reduce((sum, child) => sum + countNodes(child), 0);
  }

  // -- Track selector -------------------------------------------------------

  function populateTrackSelect(forest) {
    if (!trackSelect) return;
    trackSelect.innerHTML = "";

    const allOption = document.createElement("option");
    allOption.value = "all";
    allOption.textContent = `All tracks (${forest.length})`;
    trackSelect.appendChild(allOption);

    forest.forEach((tree, i) => {
      const opt = document.createElement("option");
      opt.value = String(i);
      opt.textContent = `Track ${i + 1} \u2014 root id ${tree.node.id} (${countNodes(tree)} cells)`;
      trackSelect.appendChild(opt);
    });

    trackSelect.value = forest.length <= MAX_TRACKS_SIDE_BY_SIDE ? "all" : "0";
  }

  // -- Layout + rendering ---------------------------------------------------

  function layoutTree(tree) {
    const root = d3.hierarchy(tree, (d) => d.children);
    d3.tree().nodeSize([NODE_SPACING, LEVEL_HEIGHT])(root);

    let minX = Infinity;
    let maxX = -Infinity;
    let maxY = 0;
    root.each((d) => {
      if (d.x < minX) minX = d.x;
      if (d.x > maxX) maxX = d.x;
      if (d.y > maxY) maxY = d.y;
    });

    return { root, minX, maxX, maxY, width: maxX - minX + NODE_SPACING };
  }

  function render(selection) {
    if (!forest || forest.length === 0) {
      svg.selectAll("*").remove();
      return;
    }

    const treesToRender =
      selection === "all" ? forest : [forest[parseInt(selection, 10)]];

    const layouts = treesToRender.map(layoutTree);

    let cursorX = TRACK_PADDING;
    let maxDepthHeight = 0;
    const placements = layouts.map((layout) => {
      const offsetX = cursorX - layout.minX;
      cursorX += layout.width + TRACK_GAP;
      maxDepthHeight = Math.max(maxDepthHeight, layout.maxY);
      return { root: layout.root, offsetX };
    });

    const totalWidth = Math.max(cursorX + TRACK_PADDING, 800);
    const totalHeight = maxDepthHeight + LEVEL_HEIGHT + TRACK_PADDING * 2;

    // Set SVG attribute width and height so the parent container scrollbar
    // activates naturally when tree size exceeds container, while viewBox
    // allows D3 zoom scaling.
    svg
      .attr("viewBox", `0 0 ${totalWidth} ${totalHeight}`)
      .attr("width", totalWidth)
      .attr("height", totalHeight);

    svg.selectAll("*").remove();

    // Group container for D3 zoom & pan
    const zoomGroup = svg.append("g").attr("class", "zoom-container");
    zoomGroup.attr("transform", currentZoomTransform);

    const g = zoomGroup.append("g").attr("transform", `translate(0, ${TRACK_PADDING})`);

    for (const { root, offsetX } of placements) {
      const treeG = g.append("g").attr("transform", `translate(${offsetX}, 0)`);

      treeG
        .selectAll(".link")
        .data(root.links())
        .join("path")
        .attr("class", "link")
        .attr(
          "d",
          d3
            .linkVertical()
            .x((d) => d.x)
            .y((d) => d.y)
        );

      const nodeG = treeG
        .selectAll(".node")
        .data(root.descendants())
        .join("g")
        .attr("class", "node")
        .attr("transform", (d) => `translate(${d.x},${d.y})`);

      nodeG.append("circle").attr("r", NODE_RADIUS);

      nodeG
        .append("text")
        .attr("dy", -10)
        .attr("text-anchor", "middle")
        .text((d) => `t${d.data.node.t}`);

      nodeG.append("title").text((d) => {
        const n = d.data.node;
        return `id ${n.id} \u2014 t=${n.t}, z=${n.z}, y=${n.y}, x=${n.x} (voxel indices)`;
      });

      nodeG.on("click", (event, d) => {
        handleNodeClick(d);
      });
    }
  }

  // -- Click handling -------------------------------------------------------

  function handleNodeClick(d3node) {
    highlightPathTo(d3node);

    if (typeof window.highlightNode === "function") {
      window.highlightNode(d3node.data.node.id);
    }
  }

  function highlightPathTo(d3node) {
    const pathNodeIds = new Set(d3node.ancestors().map((a) => a.data.node.id));

    svg.selectAll(".link").classed("highlighted", function (linkDatum) {
      return (
        pathNodeIds.has(linkDatum.source.data.node.id) &&
        pathNodeIds.has(linkDatum.target.data.node.id)
      );
    });

    svg.selectAll(".node").classed("highlighted", function (nodeDatum) {
      return pathNodeIds.has(nodeDatum.data.node.id);
    });
  }
})();
