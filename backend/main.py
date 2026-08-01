"""
main.py
=======

FastAPI backend for the cell-tracking web app.

Endpoints:
    POST /jobs                  -- upload a volume, start processing, get a job_id
    GET  /jobs/{job_id}/status  -- poll job status
    GET  /jobs/{job_id}/result  -- fetch the finished result (nodes/edges/stats)

Design notes:
    - Each job gets its own directory: jobs/{job_id}/
        jobs/{job_id}/input/...      raw uploaded file (or unzipped .zarr dir)
        jobs/{job_id}/status.json    {"status": ..., "error": ...}
        jobs/{job_id}/result.json    written once the job finishes successfully
    - Job state lives ENTIRELY in status.json on disk, not in a Python dict
      in memory. This means a server restart mid-job just leaves that job
      "running" forever (acceptable for the MVP) but a restart *between*
      jobs loses nothing -- every already-finished job's status/result is
      still readable from disk. It also means multiple worker processes
      could, in principle, share the jobs/ directory.
    - Processing runs via FastAPI's BackgroundTasks. This is a same-process,
      in-memory task queue -- fine for local/single-worker use. If this ever
      needs to scale across multiple server processes/machines, swap this
      for Celery + Redis (or similar) without changing the API surface.
"""

from __future__ import annotations

import io
import json
import traceback
import uuid
import zipfile
from functools import lru_cache
from pathlib import Path
from typing import Optional

import numpy as np
from fastapi import BackgroundTasks, FastAPI, HTTPException, UploadFile, File, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from PIL import Image
from pydantic import BaseModel

import llm
import pipeline
import video_export

# --------------------------------------------------------------------------
# App setup
# --------------------------------------------------------------------------

app = FastAPI(title="Cell Tracking API")

# Allow all origins for local development. The frontend is a handful of
# static files that may be opened directly (file://) or served from a
# different port than the API, so CORS needs to be permissive for now.
# Tighten this (specific origins) before any real deployment.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

JOBS_DIR = Path(__file__).parent / "jobs"
JOBS_DIR.mkdir(exist_ok=True)

# --------------------------------------------------------------------------
# Upload validation
# --------------------------------------------------------------------------
# Basic guardrails so an obviously-wrong upload fails fast with a clear
# message instead of quietly kicking off a background job that's going to
# crash confusingly minutes later (or, for size, potentially filling up
# disk before it even gets that far).

ALLOWED_UPLOAD_EXTENSIONS = {".zip", ".tif", ".tiff"}
MAX_UPLOAD_SIZE_BYTES = 2 * 1024 * 1024 * 1024  # 2 GiB -- generous for a
# microscopy volume at the sizes this app expects (~100 x 64 x 256 x 256
# uint16 is well under 1 GiB), while still bounding worst-case disk usage.
UPLOAD_COPY_CHUNK_BYTES = 1024 * 1024  # 1 MiB per chunk while streaming to disk


# --------------------------------------------------------------------------
# Small helpers for reading/writing per-job state on disk
# --------------------------------------------------------------------------

def _job_dir(job_id: str) -> Path:
    return JOBS_DIR / job_id


def _status_path(job_id: str) -> Path:
    return _job_dir(job_id) / "status.json"


def _result_path(job_id: str) -> Path:
    return _job_dir(job_id) / "result.json"


def _input_path_file(job_id: str) -> Path:
    """Where we record the resolved path to the job's original volume
    (the .tif file, or the extracted .zarr directory) so later endpoints
    like /frame can re-read the raw data without re-deriving it from the
    upload logic."""
    return _job_dir(job_id) / "input_path.txt"


def _write_input_path(job_id: str, path: Path) -> None:
    _input_path_file(job_id).write_text(str(path))


def _read_input_path(job_id: str) -> Optional[Path]:
    path_file = _input_path_file(job_id)
    if not path_file.exists():
        return None
    return Path(path_file.read_text().strip())


def _write_status(job_id: str, status: str, error: Optional[str] = None) -> None:
    """Overwrite status.json for a job. This is the single source of truth
    for job state -- nothing is kept in memory between requests."""
    payload = {"status": status, "error": error}
    _status_path(job_id).write_text(json.dumps(payload))


def _read_status(job_id: str) -> dict:
    path = _status_path(job_id)
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"No such job: {job_id}")
    return json.loads(path.read_text())


def _video_status_path(job_id: str) -> Path:
    return _job_dir(job_id) / "video_status.json"


def _video_path(job_id: str) -> Path:
    return _job_dir(job_id) / "annotated.mp4"


def _write_video_status(job_id: str, status: str, error: Optional[str] = None) -> None:
    """Separate from the main job's status.json -- video export is its own,
    optional, potentially slow background job layered on top of an already-
    finished analysis job, so it gets its own state file rather than
    overloading the main job status."""
    payload = {"status": status, "error": error}
    _video_status_path(job_id).write_text(json.dumps(payload))


def _read_video_status(job_id: str) -> dict:
    path = _video_status_path(job_id)
    if not path.exists():
        # Distinguished from a 404: the job exists, a video export just
        # hasn't been started for it yet.
        return {"status": "not_started", "error": None}
    return json.loads(path.read_text())


# --------------------------------------------------------------------------
# Background job processing
# --------------------------------------------------------------------------

def _friendly_error_message(exc: Exception) -> str:
    """
    Turn a raw exception into a short, human-readable message that's safe
    to show directly in the UI -- no stack trace, no internal file paths.
    The full exception (with traceback) is always printed to the server
    log separately via traceback.print_exc(), so nothing is lost for
    debugging; this is purely about what the *client* sees.
    """
    if isinstance(exc, FileNotFoundError):
        return "The uploaded file could not be found on the server. Please try uploading again."
    if isinstance(exc, MemoryError):
        return "This volume is too large to process with the available memory."
    if type(exc) is ValueError:
        # load_volume / run_pipeline raise a plain ValueError for
        # recognized-but-wrong shapes or formats, and those messages are
        # already written to be user-facing (e.g. "Unrecognized volume
        # format..."). Checking the EXACT type (not isinstance) matters:
        # some third-party libraries raise ValueError SUBCLASSES with
        # internal, technical messages (e.g. tifffile.TiffFileError is
        # secretly a ValueError) that we do NOT want passed through as if
        # they were our own polished text.
        return str(exc)
    return (
        "Something went wrong while processing this file. It may be an "
        "unsupported or corrupted volume -- please check the format and try again."
    )


def _resolve_input_path(dest_path: Path) -> Path:
    """
    If dest_path is a .zip, extract it alongside dest_path and return the
    path to the extracted .zarr directory (or extract dir).
    """
    lower = dest_path.name.lower()
    if lower.endswith(".zip"):
        input_dir = dest_path.parent
        extract_dir = input_dir / "extracted"
        extract_dir.mkdir(exist_ok=True)
        with zipfile.ZipFile(dest_path) as zf:
            zf.extractall(extract_dir)

        zarr_candidates = list(extract_dir.glob("*.zarr")) + list(
            extract_dir.glob("*/*.zarr")
        )
        if zarr_candidates:
            return zarr_candidates[0]
        return extract_dir

    return dest_path


def _process_job(job_id: str, dest_path: Path) -> None:
    """
    Runs in the background after a job is submitted. Extracts zip if needed,
    loads volume, runs pipeline, and writes result.json / status.json.
    """
    try:
        _write_status(job_id, "running")

        input_path = _resolve_input_path(dest_path)
        _write_input_path(job_id, input_path)

        volume = pipeline.load_volume(str(input_path))
        result = pipeline.run_pipeline(volume, voxel_scale=pipeline.DEFAULT_VOXEL_SCALE)

        result["volume_shape"] = list(volume.shape)  # [T, Z, Y, X]
        result["voxel_scale_um"] = list(pipeline.DEFAULT_VOXEL_SCALE)  # [z, y, x]

        _result_path(job_id).write_text(json.dumps(result))
        _write_status(job_id, "done")

    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        _write_status(job_id, "error", error=_friendly_error_message(exc))


def _process_video_export(job_id: str, input_path: Path) -> None:
    """
    Runs in the background after POST /jobs/{job_id}/export/video/start.
    Reuses the already-cached volume and the finished result.json to render
    an annotated MP4 via video_export.render_annotated_video(). This is
    separate from _process_job -- it's an optional extra export step on top
    of an already-completed analysis job, not part of the main pipeline.
    """
    try:
        _write_video_status(job_id, "running")

        volume = _load_volume_cached(str(input_path))
        result = json.loads(_result_path(job_id).read_text())

        video_export.render_annotated_video(volume, result, _video_path(job_id), fps=5)

        _write_video_status(job_id, "done")

    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        _write_video_status(job_id, "error", error=_friendly_error_message(exc))


def _validate_upload_or_raise(upload: UploadFile) -> None:
    """
    Reject obviously-invalid uploads immediately, before any job directory
    is created or any background work is kicked off:
      - unrecognized file extension
      - declared size over MAX_UPLOAD_SIZE_BYTES (when the client sends a
        Content-Length; not all clients do, so this is a fast-path check,
        not the only enforcement -- see _copy_upload_with_size_limit for
        the actual backstop that works regardless).
    """
    filename = upload.filename or ""
    suffix = Path(filename).suffix.lower()

    if suffix not in ALLOWED_UPLOAD_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Unsupported file type '{suffix or '(none)'}'. "
                "Please upload a .tif/.tiff file, or a .zip containing a .zarr directory."
            ),
        )

    # UploadFile.size reflects the request's Content-Length when the client
    # sent one; it's None otherwise, so this is a best-effort early check.
    if upload.size is not None and upload.size > MAX_UPLOAD_SIZE_BYTES:
        raise HTTPException(
            status_code=413,
            detail=(
                f"File is too large ({upload.size / 1024**3:.2f} GiB). "
                f"The limit is {MAX_UPLOAD_SIZE_BYTES / 1024**3:.0f} GiB."
            ),
        )


def _copy_upload_with_size_limit(upload: UploadFile, dest_path: Path, max_bytes: int) -> None:
    """
    Stream the upload to disk in chunks, aborting (and cleaning up the
    partial file) if it exceeds max_bytes. This is the authoritative size
    enforcement -- unlike the Content-Length check in
    _validate_upload_or_raise, it holds even if the client never declared
    a size, and it bounds disk usage instead of writing an unbounded
    stream first and checking after the fact.
    """
    total_bytes = 0
    try:
        with dest_path.open("wb") as f:
            while True:
                chunk = upload.file.read(UPLOAD_COPY_CHUNK_BYTES)
                if not chunk:
                    break
                total_bytes += len(chunk)
                if total_bytes > max_bytes:
                    raise HTTPException(
                        status_code=413,
                        detail=(
                            f"File is too large (exceeded {max_bytes / 1024**3:.0f} GiB "
                            "while uploading)."
                        ),
                    )
                f.write(chunk)
    except HTTPException:
        dest_path.unlink(missing_ok=True)
        raise


# --------------------------------------------------------------------------
# Endpoints
# --------------------------------------------------------------------------

@app.post("/jobs")
async def create_job(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
) -> JSONResponse:
    """
    Accept a volume upload (.tif/.tiff, or a .zip containing a .zarr dir),
    save raw bytes to disk, and kick off background extraction & processing.
    Returns immediately with job_id so HTTP request finishes fast.
    """
    _validate_upload_or_raise(file)

    job_id = str(uuid.uuid4())
    _job_dir(job_id).mkdir(parents=True, exist_ok=True)
    _write_status(job_id, "pending")

    input_dir = _job_dir(job_id) / "input"
    input_dir.mkdir(parents=True, exist_ok=True)
    filename = file.filename or "upload"
    dest_path = input_dir / filename

    _copy_upload_with_size_limit(file, dest_path, MAX_UPLOAD_SIZE_BYTES)

    background_tasks.add_task(_process_job, job_id, dest_path)

    return JSONResponse({"job_id": job_id})


@app.get("/jobs/{job_id}/status")
async def get_job_status(job_id: str) -> JSONResponse:
    """Return the current status of a job: pending | running | done | error."""
    status = _read_status(job_id)
    return JSONResponse(status)


@app.get("/jobs/{job_id}/result")
async def get_job_result(job_id: str) -> JSONResponse:
    """
    Return the full result (nodes, edges, stats) for a finished job.
    404s if the job doesn't exist, 409s if it exists but isn't done yet.
    """
    status = _read_status(job_id)

    if status["status"] == "error":
        raise HTTPException(status_code=500, detail=status["error"])

    if status["status"] != "done":
        raise HTTPException(
            status_code=409,
            detail=f"Job is not finished yet (status: {status['status']}).",
        )

    result_path = _result_path(job_id)
    if not result_path.exists():
        raise HTTPException(status_code=500, detail="Job marked done but result.json is missing.")

    return JSONResponse(json.loads(result_path.read_text()))


@lru_cache(maxsize=4)
def _load_volume_cached(path_str: str) -> np.ndarray:
    """
    Small in-memory cache so scrubbing the t/z sliders doesn't re-read and
    re-decode the whole volume from disk on every single frame request.
    Keyed by path string (paths are immutable once a job is created) and
    capped at a handful of volumes so memory doesn't grow unbounded across
    many different jobs in one server process.
    """
    return pipeline.load_volume(path_str)


@app.get("/jobs/{job_id}/frame")
async def get_frame(
    job_id: str,
    t: int = Query(..., description="Timepoint index (0-based)"),
    z: Optional[int] = Query(
        None, description="Z-slice index (0-based); omit for a max-intensity projection"
    ),
) -> Response:
    """
    Render a single 2D frame from the ORIGINAL uploaded volume as an 8-bit
    PNG, for the slice viewer: either a specific Z slice at timepoint `t`,
    or (if `z` is omitted) a max-intensity projection across Z. Always
    percentile-rescaled from the raw 16-bit data before encoding, since the
    raw dynamic range is far wider than a screen can show.
    """
    # Confirms the job exists (raises 404 otherwise); doesn't require the
    # job to be "done" -- the raw volume is available as soon as it's
    # uploaded, independent of pipeline processing.
    _read_status(job_id)

    input_path = _read_input_path(job_id)
    if input_path is None or not input_path.exists():
        raise HTTPException(status_code=404, detail="Input volume not found for this job.")

    try:
        volume = _load_volume_cached(str(input_path))
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=_friendly_error_message(exc)) from exc

    n_frames, n_z = volume.shape[0], volume.shape[1]

    if not (0 <= t < n_frames):
        raise HTTPException(status_code=400, detail=f"t={t} out of range [0, {n_frames - 1}]")
    if z is not None and not (0 <= z < n_z):
        raise HTTPException(status_code=400, detail=f"z={z} out of range [0, {n_z - 1}]")

    slice_2d = pipeline.extract_display_slice(volume, t, z)

    image = Image.fromarray(slice_2d, mode="L")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")

    return Response(content=buffer.getvalue(), media_type="image/png")


@app.get("/jobs/{job_id}/export/csv")
async def export_csv(job_id: str) -> Response:
    """
    Export the job's tracking graph as a CSV in the competition submission
    schema: id,dataset,row_type,node_id,t,z,y,x,source_id,target_id.
    Each node becomes a "node" row (coordinates filled, source/target
    blank); each edge becomes an "edge" row (source_id/target_id filled,
    coordinates blank). `dataset` is the job_id, since that's the only
    per-upload identifier this app currently tracks.
    """
    status = _read_status(job_id)
    if status["status"] != "done":
        raise HTTPException(
            status_code=409,
            detail=f"Job is not finished yet (status: {status['status']}).",
        )

    result = json.loads(_result_path(job_id).read_text())

    csv_text = pipeline.build_submission_csv(result, dataset=job_id)
    csv_bytes = csv_text.encode("utf-8")

    headers = {"Content-Disposition": f'attachment; filename="{job_id}_tracks.csv"'}
    return Response(content=csv_bytes, media_type="text/csv", headers=headers)


@app.post("/jobs/{job_id}/export/video/start")
async def start_video_export(job_id: str, background_tasks: BackgroundTasks) -> JSONResponse:
    """
    Kick off annotated-video rendering as its own background task -- this
    can be slow for long videos, so (like the main analysis job) the client
    is expected to poll GET /jobs/{job_id}/export/video/status rather than
    wait on this request.
    """
    status = _read_status(job_id)
    if status["status"] != "done":
        raise HTTPException(
            status_code=409,
            detail=f"Job must finish processing before exporting a video (status: {status['status']}).",
        )

    input_path = _read_input_path(job_id)
    if input_path is None or not input_path.exists():
        raise HTTPException(status_code=404, detail="Input volume not found for this job.")

    _write_video_status(job_id, "pending")
    background_tasks.add_task(_process_video_export, job_id, input_path)

    return JSONResponse({"status": "pending"})


@app.get("/jobs/{job_id}/export/video/status")
async def get_video_export_status(job_id: str) -> JSONResponse:
    """Poll the status of a video export: not_started | pending | running | done | error."""
    _read_status(job_id)  # 404s if the job itself doesn't exist
    return JSONResponse(_read_video_status(job_id))


@app.get("/jobs/{job_id}/export/video/download")
async def download_video_export(job_id: str) -> FileResponse:
    """Download the finished annotated MP4. 409s if it isn't done yet."""
    _read_status(job_id)  # 404s if the job itself doesn't exist

    video_status = _read_video_status(job_id)
    if video_status["status"] != "done":
        raise HTTPException(
            status_code=409,
            detail=f"Video is not ready yet (status: {video_status['status']}).",
        )

    video_path = _video_path(job_id)
    if not video_path.exists():
        raise HTTPException(status_code=500, detail="Video marked done but the file is missing.")

    return FileResponse(
        path=video_path,
        media_type="video/mp4",
        filename=f"{job_id}_annotated.mp4",
    )


# --------------------------------------------------------------------------
# LLM layer: grounded Q&A chat + auto-generated report (llm.py)
# --------------------------------------------------------------------------
# HARD RULE, enforced by construction: these endpoints only ever hand the
# model (a) the ability to call the read-only tools in llm.py, which read
# straight from a job's stored result.json, or (b) values pulled directly
# from that same file into the prompt. Neither endpoint computes, alters,
# or writes back to a job's tracking results -- they're a narration layer
# on top of numbers the deterministic pipeline already produced.

class ChatRequest(BaseModel):
    message: str
    history: list = []  # [{"role": "user"|"assistant", "content": str}, ...]


def _load_finished_result(job_id: str) -> dict:
    """Shared by /chat and /report: 404s if the job doesn't exist, 409s if
    it hasn't finished yet, otherwise returns the parsed result.json."""
    status = _read_status(job_id)
    if status["status"] != "done":
        raise HTTPException(
            status_code=409,
            detail=f"Job is not finished yet (status: {status['status']}).",
        )
    return json.loads(_result_path(job_id).read_text())


@app.post("/jobs/{job_id}/chat")
async def chat_with_job(job_id: str, payload: ChatRequest) -> JSONResponse:
    """
    Grounded Q&A chat about one job's results. Loads the job's real
    result.json, runs the model with llm.py's read-only tools available,
    and returns its final natural-language answer once it stops calling
    tools. `history` is the client-side conversation so far (this endpoint
    is stateless server-side -- nothing about the chat is persisted).
    """
    result = _load_finished_result(job_id)

    if not payload.message.strip():
        raise HTTPException(status_code=400, detail="'message' must not be empty.")

    try:
        reply = llm.run_chat_turn(result, payload.message, payload.history)
    except llm.LLMConfigError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=_friendly_error_message(exc)) from exc

    return JSONResponse({"reply": reply})


@app.post("/jobs/{job_id}/report")
async def generate_job_report(job_id: str) -> JSONResponse:
    """
    Auto-generated plain-language summary report for one job. No tool
    loop -- llm.py puts the overall stats, per-frame counts, and division
    event list directly in the prompt, and the model is instructed to
    summarize only those numbers.
    """
    result = _load_finished_result(job_id)

    try:
        report_text = llm.generate_report(result)
    except llm.LLMConfigError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=_friendly_error_message(exc)) from exc

    return JSONResponse({"report": report_text})


@app.get("/health")
async def health() -> dict:
    """Simple liveness check (moved off '/' now that '/' serves the frontend)."""
    return {"status": "ok", "service": "cell-tracking-api"}


# --------------------------------------------------------------------------
# Static frontend
# --------------------------------------------------------------------------
# Mounted LAST and at "/" so it acts as a catch-all fallback: FastAPI/
# Starlette matches routes in registration order, so the specific API
# routes above are always tried first, and only unmatched paths fall
# through to StaticFiles. html=True makes it serve frontend/index.html for
# "/" itself. This lets the whole app -- API + frontend -- run from a
# single `uvicorn main:app` process during development.
FRONTEND_DIR = Path(__file__).parent.parent / "frontend"

@app.get("/favicon.ico", include_in_schema=False)
async def favicon_ico():
    fav = FRONTEND_DIR / "favicon.svg"
    if fav.exists():
        return FileResponse(fav, media_type="image/svg+xml")
    return Response(status_code=404)

if FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
