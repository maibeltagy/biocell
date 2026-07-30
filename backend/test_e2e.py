"""
test_e2e.py
===========

Standalone end-to-end smoke test for Phase 1.

1. Generates a small synthetic (T, Z, Y, X) volume with a handful of
   Gaussian blobs drifting over time, and saves it as a .tif stack.
2. Starts the FastAPI app in-process via TestClient (no need for a
   separately running uvicorn server).
3. Exercises the full job lifecycle:
     POST /jobs            -> job_id
     GET  /jobs/{id}/status -> poll until "done" (or "error")
     GET  /jobs/{id}/result -> nodes / edges / stats

Run with: python3 test_e2e.py
"""

import json
import sys
import time
from pathlib import Path

import numpy as np
import tifffile
from fastapi.testclient import TestClient

# Make sure we import the app fresh, with jobs/ relative to this file.
import main as backend_main

TEST_DIR = Path(__file__).parent
SYNTHETIC_TIF_PATH = TEST_DIR / "synthetic_test_volume.tif"


def make_synthetic_volume(
    n_frames: int = 10,
    shape_zyx=(16, 128, 128),
    n_blobs: int = 4,
    seed: int = 0,
) -> np.ndarray:
    """
    Build a small synthetic (T, Z, Y, X) uint16 volume: a handful of
    Gaussian blobs on a noisy background, each drifting with a random
    constant velocity across frames. This is enough to exercise the stub
    detector/linker without needing real microscopy data.
    """
    rng = np.random.default_rng(seed)
    Z, Y, X = shape_zyx
    volume = np.zeros((n_frames, Z, Y, X), dtype=np.float32)

    # Background noise so thresholding has something realistic to work with.
    volume += rng.normal(loc=200, scale=15, size=volume.shape).clip(min=0)

    zz, yy, xx = np.meshgrid(
        np.arange(Z), np.arange(Y), np.arange(X), indexing="ij"
    )

    for _ in range(n_blobs):
        pos = np.array(
            [
                rng.uniform(2, Z - 2),
                rng.uniform(10, Y - 10),
                rng.uniform(10, X - 10),
            ]
        )
        velocity = rng.uniform(-1.5, 1.5, size=3)
        amplitude = rng.uniform(3000, 6000)
        sigma = rng.uniform(1.5, 2.5)

        for t in range(n_frames):
            center = pos + velocity * t
            center = np.clip(center, [1, 1, 1], [Z - 2, Y - 2, X - 2])
            blob = amplitude * np.exp(
                -(
                    (zz - center[0]) ** 2
                    + (yy - center[1]) ** 2
                    + (xx - center[2]) ** 2
                )
                / (2 * sigma**2)
            )
            volume[t] += blob

    volume = np.clip(volume, 0, 65535).astype(np.uint16)
    return volume


def main() -> int:
    print("1. Generating synthetic test volume...")
    volume = make_synthetic_volume()
    print(f"   shape={volume.shape}, dtype={volume.dtype}")
    tifffile.imwrite(SYNTHETIC_TIF_PATH, volume)
    print(f"   saved to {SYNTHETIC_TIF_PATH}")

    print("\n2. Starting API client (in-process TestClient)...")
    client = TestClient(backend_main.app)

    resp = client.get("/health")
    assert resp.status_code == 200, resp.text
    print(f"   liveness check: {resp.json()}")

    print("\n3. Submitting job (POST /jobs)...")
    with open(SYNTHETIC_TIF_PATH, "rb") as f:
        resp = client.post(
            "/jobs",
            files={"file": ("synthetic_test_volume.tif", f, "image/tiff")},
        )
    assert resp.status_code == 200, resp.text
    job_id = resp.json()["job_id"]
    print(f"   job_id = {job_id}")

    print("\n4. Polling GET /jobs/{job_id}/status...")
    deadline = time.time() + 60
    status = None
    while time.time() < deadline:
        resp = client.get(f"/jobs/{job_id}/status")
        assert resp.status_code == 200, resp.text
        status = resp.json()
        print(f"   status = {status}")
        if status["status"] in ("done", "error"):
            break
        time.sleep(0.5)

    if status is None or status["status"] != "done":
        print(f"\nFAILED: job did not complete successfully. Final status: {status}")
        return 1

    print("\n5. Fetching GET /jobs/{job_id}/result...")
    resp = client.get(f"/jobs/{job_id}/result")
    assert resp.status_code == 200, resp.text
    result = resp.json()

    n_nodes = len(result["nodes"])
    n_edges = len(result["edges"])
    stats = result["stats"]

    print(f"   nodes: {n_nodes}")
    print(f"   edges: {n_edges}")
    print(f"   cell_count_per_frame: {stats['cell_count_per_frame']}")
    print(f"   division_events: {len(stats['division_events'])}")
    print(f"   avg_speed_um_per_frame: {stats['avg_speed_um_per_frame']:.4f}")

    # Sanity checks on shape/consistency of the output.
    assert n_nodes > 0, "expected at least some detections"
    assert len(stats["cell_count_per_frame"]) == volume.shape[0], (
        "cell_count_per_frame length should match number of frames"
    )
    assert all(
        set(n.keys()) == {"id", "t", "z", "y", "x"} for n in result["nodes"]
    ), "node schema mismatch"
    assert all(
        set(e.keys()) == {"source", "target"} for e in result["edges"]
    ), "edge schema mismatch"

    print("\n6. Testing 404 on unknown job id...")
    resp = client.get("/jobs/does-not-exist/status")
    assert resp.status_code == 404, resp.text
    print(f"   got expected 404: {resp.json()}")

    print("\nALL CHECKS PASSED.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
