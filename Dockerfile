# Cell Tracking Web App -- single-container image.
#
# Runs the FastAPI backend, which also serves the static frontend
# (backend/main.py mounts ../frontend via StaticFiles), so `docker run`
# alone gives you the whole app on one port.
#
# Build:
#   docker build -t cell-tracking-app .
#
# Run:
#   docker run --rm -p 8000:8000 cell-tracking-app
# Then open http://localhost:8000
#
# The chat/report features need an LLM API key, passed as an env var (the
# rest of the app works fine without it):
#   docker run --rm -p 8000:8000 -e ANTHROPIC_API_KEY=sk-ant-... cell-tracking-app
#
# To persist job data (uploads + results) across container restarts,
# mount a volume over the jobs directory:
#   docker run --rm -p 8000:8000 -v cell-tracking-jobs:/app/backend/jobs cell-tracking-app

FROM python:3.11-slim

# opencv-python-headless still links against a couple of shared libraries
# that aren't in the slim base image even though it avoids the full GUI/X11
# stack; libglib2.0-0 covers the common "cv2 import fails" case.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install dependencies first (separate layer) so `docker build` doesn't
# re-install every package on every code change -- only on requirements.txt
# changes.
COPY backend/requirements.txt backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt

# Backend + frontend need to land at /app/backend and /app/frontend
# specifically: main.py locates the frontend via
# `Path(__file__).parent.parent / "frontend"`, i.e. one level up from
# wherever main.py itself lives.
COPY backend/ backend/
COPY frontend/ frontend/

WORKDIR /app/backend

# jobs/ holds all per-job state (uploads, results, exports) as plain files
# on disk -- see main.py's JOBS_DIR. Not declared as a VOLUME by default
# so a plain `docker run` just works; mount one explicitly (see the `Run`
# note above) if you want job data to survive a container restart/rebuild.

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
