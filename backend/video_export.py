"""
video_export.py
================

Renders an annotated MP4 for a finished job: for each timepoint, take the
max-intensity-projection frame (reusing pipeline.extract_display_slice, the
same rendering used by the slice viewer's /frame endpoint), draw the
detected cells as circles, and draw a short track line connecting each node
to its parent's position in the previous frame (i.e. following the same
edges the lineage tree is built from).

Kept as its own module (rather than folded into pipeline.py) because this
is export/visualization logic layered on top of the pipeline's output, not
detection/tracking logic itself.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict

import cv2
import numpy as np

import pipeline

# Colors are BGR (OpenCV convention), chosen to match the frontend's own
# detection-dot / accent colors so the exported video looks consistent with
# the in-browser slice viewer.
NODE_FILL_COLOR = (58, 92, 255)     # matches CSS rgba(255, 92, 58, ...) detection dot
NODE_OUTLINE_COLOR = (255, 255, 255)
TRACK_LINE_COLOR = (107, 111, 47)   # matches CSS --color-accent #2f6f6b (muted teal)

NODE_RADIUS = 4
NODE_OUTLINE_THICKNESS = 1
TRACK_LINE_THICKNESS = 1


def render_annotated_video(
    volume: np.ndarray,
    result: dict,
    output_path: Path,
    fps: int = 5,
) -> None:
    """
    Render a max-projection video with detection + track-line overlays and
    write it to `output_path` as an H.264-ish MP4 (via OpenCV's mp4v
    fourcc). Raises RuntimeError if the video writer can't be opened.

    Args:
        volume: (T, Z, Y, X) uint16 array (the original raw volume).
        result: the job's result dict (nodes, edges, stats) as loaded from
            result.json.
        output_path: where to write the .mp4 file.
        fps: playback frame rate of the exported video.
    """
    n_frames = volume.shape[0]

    nodes_by_id: Dict[int, dict] = {n["id"]: n for n in result["nodes"]}

    # Each node has at most one incoming edge under the current linker, so a
    # simple target -> source map is enough to find "where was this cell in
    # the previous frame" for drawing a track line. (If a future pipeline
    # ever produces multiple incoming edges for a node -- which shouldn't
    # happen for a proper tracking graph -- this just keeps the last one
    # seen, which is an acceptable degradation for a visualization export.)
    parent_id_by_target: Dict[int, int] = {e["target"]: e["source"] for e in result["edges"]}

    nodes_by_frame: Dict[int, list] = {}
    for node in result["nodes"]:
        nodes_by_frame.setdefault(node["t"], []).append(node)

    first_slice = pipeline.extract_display_slice(volume, t=0)  # max projection, (Y, X) uint8
    height, width = first_slice.shape

    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(str(output_path), fourcc, fps, (width, height))

    if not writer.isOpened():
        raise RuntimeError(f"Failed to open video writer for '{output_path}'")

    try:
        for t in range(n_frames):
            gray = pipeline.extract_display_slice(volume, t=t)
            frame_bgr = cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)

            for node in nodes_by_frame.get(t, []):
                x, y = int(node["x"]), int(node["y"])

                parent_id = parent_id_by_target.get(node["id"])
                if parent_id is not None and parent_id in nodes_by_id:
                    parent = nodes_by_id[parent_id]
                    cv2.line(
                        frame_bgr,
                        (int(parent["x"]), int(parent["y"])),
                        (x, y),
                        TRACK_LINE_COLOR,
                        TRACK_LINE_THICKNESS,
                        lineType=cv2.LINE_AA,
                    )

                cv2.circle(frame_bgr, (x, y), NODE_RADIUS, NODE_FILL_COLOR, thickness=-1, lineType=cv2.LINE_AA)
                cv2.circle(
                    frame_bgr,
                    (x, y),
                    NODE_RADIUS,
                    NODE_OUTLINE_COLOR,
                    thickness=NODE_OUTLINE_THICKNESS,
                    lineType=cv2.LINE_AA,
                )

            writer.write(frame_bgr)
    finally:
        writer.release()
