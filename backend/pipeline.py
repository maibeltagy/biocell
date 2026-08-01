"""
pipeline.py
===========

This module is the interface between the web application and the actual
cell-detection/tracking logic.

For the MVP / web-app-development phase, this file contains a STUB
implementation:
    - simple percentile threshold + local-maxima blob detection per frame
    - naive nearest-neighbor linking between consecutive frames

The stub exists purely so the rest of the system (FastAPI backend, job
queue, frontend viewer/lineage tree/stats dashboard) can be built and
tested end-to-end against data of the *correct shape* before the real
detection/tracking pipeline is dropped in. When the real pipeline is
ready, replace the body of `run_pipeline` (and, if needed, `load_volume`)
and keep the function signatures identical so nothing else has to change.

Public interface (do not change signatures without updating main.py):
    load_volume(path) -> np.ndarray            # (T, Z, Y, X) uint16
    run_pipeline(volume, voxel_scale) -> dict   # {"nodes", "edges", "stats"}
"""

from __future__ import annotations

import os
from typing import Dict, List, Optional, Tuple

import numpy as np
from scipy import ndimage as ndi
from skimage.feature import peak_local_max

# Default physical voxel scale in microns: (z, y, x).
# z-steps are ~4x larger physically than the in-plane (y, x) sampling.
DEFAULT_VOXEL_SCALE: Tuple[float, float, float] = (1.625, 0.40625, 0.40625)


# --------------------------------------------------------------------------
# Volume loading
# --------------------------------------------------------------------------

def load_volume(path: str) -> np.ndarray:
    """
    Load a 3D+time microscopy volume from disk.

    Accepts either:
      - a `.zarr` directory (detected by the path being a directory, or
        ending in `.zarr`)
      - a `.tif` / `.tiff` stack (detected by file extension)

    Returns a single numpy array of shape (T, Z, Y, X), dtype uint16.

    Raises:
        FileNotFoundError: if `path` does not exist.
        ValueError: if the format cannot be determined or the loaded data
            does not have 4 dimensions.
    """
    if not os.path.exists(path):
        raise FileNotFoundError(f"No such file or directory: {path}")

    lower = path.lower()

    if os.path.isdir(path) or lower.endswith(".zarr"):
        # --- Zarr branch (Lazy array access to prevent RAM spikes) ---
        import zarr

        z = zarr.open(path, mode="r")
        if hasattr(z, "shape"):
            volume = z
        else:
            if "volume" in z:
                volume = z["volume"]
            else:
                first_key = next(iter(z.array_keys()))
                volume = z[first_key]

    elif lower.endswith(".tif") or lower.endswith(".tiff"):
        # --- TIFF branch ---
        import tifffile

        volume = tifffile.imread(path)

    else:
        raise ValueError(
            f"Unrecognized volume format for '{path}'. "
            "Expected a .zarr directory or a .tif/.tiff file."
        )

    if volume.ndim != 4:
        raise ValueError(
            f"Expected a 4D (T, Z, Y, X) volume, got array with shape "
            f"{volume.shape} (ndim={volume.ndim})."
        )

    return volume


# --------------------------------------------------------------------------
# Stub detection + tracking pipeline
# --------------------------------------------------------------------------

def _detect_blobs_in_frame(
    frame: np.ndarray,
    threshold_percentile: float = 99.5,
    min_distance: int = 5,
) -> np.ndarray:
    """
    Very simple blob detector for one 3D frame (Z, Y, X):
      1. Threshold at a high intensity percentile to get candidate foreground.
      2. Smooth slightly to reduce noise-driven local maxima.
      3. Find local intensity maxima within the thresholded region.

    Returns an (N, 3) array of (z, y, x) integer coordinates, one row per
    detected blob centroid.
    """
    if frame.size == 0:
        return np.empty((0, 3), dtype=int)

    smoothed = ndi.gaussian_filter(frame.astype(np.float32), sigma=1.0)
    threshold = np.percentile(smoothed, threshold_percentile)

    # peak_local_max returns coordinates of local maxima above threshold_abs,
    # respecting a minimum separation between peaks (min_distance).
    coords = peak_local_max(
        smoothed,
        min_distance=min_distance,
        threshold_abs=threshold,
        exclude_border=False,
    )
    return coords  # shape (N, 3) -> columns are (z, y, x)


def _link_frames_nearest_neighbor(
    detections_per_frame: List[np.ndarray],
    max_link_distance: float = 25.0,
) -> Tuple[List[dict], List[dict]]:
    """
    Naive nearest-neighbor linking between consecutive frames.

    For each detection in frame t+1, link it to the closest unclaimed
    detection in frame t (in voxel-index space) if the distance is within
    `max_link_distance`. This is intentionally simple -- it does not handle
    divisions or merges specially, it just produces a plausible-looking
    graph so the frontend (lineage tree, stats) has real structure to
    render while the real tracking algorithm is developed separately.

    Returns:
        nodes: list of {"id": int, "t": int, "z": int, "y": int, "x": int}
        edges: list of {"source": int, "target": int}
    """
    nodes: List[dict] = []
    edges: List[dict] = []

    node_id_counter = 0
    # ids_by_frame[t] = list of node ids, parallel to detections_per_frame[t]
    ids_by_frame: List[List[int]] = []

    for t, coords in enumerate(detections_per_frame):
        frame_ids = []
        for z, y, x in coords:
            nodes.append(
                {"id": node_id_counter, "t": t, "z": int(z), "y": int(y), "x": int(x)}
            )
            frame_ids.append(node_id_counter)
            node_id_counter += 1
        ids_by_frame.append(frame_ids)

    # Link consecutive frames with greedy nearest-neighbor matching.
    for t in range(len(detections_per_frame) - 1):
        coords_a = detections_per_frame[t]
        coords_b = detections_per_frame[t + 1]
        ids_a = ids_by_frame[t]
        ids_b = ids_by_frame[t + 1]

        if len(coords_a) == 0 or len(coords_b) == 0:
            continue

        # Pairwise distance matrix (N_a x N_b).
        diffs = coords_a[:, None, :] - coords_b[None, :, :]
        dists = np.sqrt((diffs.astype(np.float64) ** 2).sum(axis=-1))

        claimed_b = set()
        # Greedy: for each point in frame t, claim its nearest unclaimed
        # point in frame t+1 (allows one-to-many, i.e. divisions, since we
        # don't remove points from frame t's pool).
        for i in range(len(coords_a)):
            order = np.argsort(dists[i])
            for j in order:
                if j in claimed_b:
                    continue
                if dists[i, j] <= max_link_distance:
                    edges.append({"source": ids_a[i], "target": ids_b[j]})
                    claimed_b.add(j)
                break  # only take the single nearest candidate per point

    return nodes, edges


def _compute_stats(
    nodes: List[dict],
    edges: List[dict],
    n_frames: int,
    voxel_scale: Tuple[float, float, float],
) -> dict:
    """
    Derive summary statistics from the node/edge graph:
      - cell_count_per_frame: number of detections in each frame
      - division_events: frames where one node links to two+ children
        (a naive proxy for a division/mitosis event)
      - avg_speed_um_per_frame: mean physical displacement per linked step
    """
    cell_count_per_frame = [0] * n_frames
    for n in nodes:
        cell_count_per_frame[n["t"]] += 1

    nodes_by_id = {n["id"]: n for n in nodes}

    # Group edges by source to find nodes with multiple children (divisions).
    children_by_source: Dict[int, List[int]] = {}
    for e in edges:
        children_by_source.setdefault(e["source"], []).append(e["target"])

    division_events = []
    for mother_id, daughter_ids in children_by_source.items():
        if len(daughter_ids) >= 2:
            division_events.append(
                {
                    "t": nodes_by_id[mother_id]["t"],
                    "mother_id": mother_id,
                    "daughter_ids": daughter_ids[:2],  # keep it to a pair
                }
            )

    # Average physical speed (um/frame) across all linked steps.
    zs, ys, xs = voxel_scale
    speeds = []
    for e in edges:
        a = nodes_by_id[e["source"]]
        b = nodes_by_id[e["target"]]
        dz = (a["z"] - b["z"]) * zs
        dy = (a["y"] - b["y"]) * ys
        dx = (a["x"] - b["x"]) * xs
        speeds.append(float(np.sqrt(dz**2 + dy**2 + dx**2)))

    avg_speed_um_per_frame = float(np.mean(speeds)) if speeds else 0.0

    return {
        "cell_count_per_frame": cell_count_per_frame,
        "division_events": division_events,
        "avg_speed_um_per_frame": avg_speed_um_per_frame,
    }


# --------------------------------------------------------------------------
# Display-slice extraction (used by the /jobs/{job_id}/frame endpoint)
# --------------------------------------------------------------------------
# The raw volumes are 16-bit and have far more dynamic range than a screen
# can show, so any 2D slice pulled out for display must be percentile-
# rescaled to 8-bit first (see shared project context). This lives here
# rather than in main.py because it's volume-manipulation logic, same as
# the rest of this module.

def rescale_to_uint8(
    image: np.ndarray,
    low_percentile: float = 1.0,
    high_percentile: float = 99.5,
) -> np.ndarray:
    """
    Percentile-rescale a 2D (or any-shape) numeric array to 8-bit for
    display: clip to [low_percentile, high_percentile] of the image's own
    intensity distribution, then linearly stretch that range to 0-255.
    """
    lo, hi = np.percentile(image, [low_percentile, high_percentile])
    if hi <= lo:
        # Degenerate (flat) image -- avoid divide-by-zero, just return black.
        hi = lo + 1.0

    scaled = (image.astype(np.float32) - lo) / (hi - lo)
    scaled = np.clip(scaled, 0.0, 1.0) * 255.0
    return scaled.astype(np.uint8)


def extract_display_slice(
    volume: Any,
    t: int,
    z: Optional[int] = None,
) -> np.ndarray:
    """
    Extract a single 2D (Y, X) uint8 image from a (T, Z, Y, X) volume at
    timepoint `t`, ready to render as PNG. Slice is loaded lazily from disk.
    """
    frame = np.asarray(volume[t])

    if z is None:
        slice_2d = frame.max(axis=0)
    else:
        slice_2d = frame[z]

    return rescale_to_uint8(np.asarray(slice_2d))


def run_pipeline(
    volume: Any,
    voxel_scale: Tuple[float, float, float] = DEFAULT_VOXEL_SCALE,
) -> dict:
    """
    Run detection + tracking over a (T, Z, Y, X) volume lazily frame-by-frame.
    """
    if volume.ndim != 4:
        raise ValueError(f"Expected (T, Z, Y, X) volume, got shape {volume.shape}")

    n_frames = volume.shape[0]
    detections_per_frame = []

    for t in range(n_frames):
        frame_t = np.asarray(volume[t], dtype=np.uint16)
        detections = _detect_blobs_in_frame(frame_t)
        detections_per_frame.append(detections)
        del frame_t

    nodes, edges = _link_frames_nearest_neighbor(detections_per_frame)
    stats = _compute_stats(nodes, edges, n_frames, voxel_scale)

    return {"nodes": nodes, "edges": edges, "stats": stats}


# --------------------------------------------------------------------------
# CSV export (submission schema)
# --------------------------------------------------------------------------

CSV_COLUMNS = [
    "id",
    "dataset",
    "row_type",
    "node_id",
    "t",
    "z",
    "y",
    "x",
    "source_id",
    "target_id",
]


def build_submission_csv(result: dict, dataset: str) -> str:
    """
    Build a CSV string in the competition's submission schema:
        id,dataset,row_type,node_id,t,z,y,x,source_id,target_id

    Every node becomes one "node" row (node_id/t/z/y/x populated,
    source_id/target_id left blank); every edge becomes one "edge" row
    (source_id/target_id populated, the node-position columns left blank).
    `id` is just a running row counter across the whole file. `dataset` is
    a caller-supplied identifier for which volume this submission came from
    -- callers currently pass the job_id, since that's the only stable
    identifier this app has for "which upload produced this result."

    Returns the CSV as a single string (including header row), ready to be
    written to a file or returned directly as an HTTP response body.
    """
    import csv
    import io

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(CSV_COLUMNS)

    row_id = 0
    for node in result["nodes"]:
        writer.writerow(
            [row_id, dataset, "node", node["id"], node["t"], node["z"], node["y"], node["x"], "", ""]
        )
        row_id += 1

    for edge in result["edges"]:
        writer.writerow(
            [row_id, dataset, "edge", "", "", "", "", "", edge["source"], edge["target"]]
        )
        row_id += 1

    return buffer.getvalue()
