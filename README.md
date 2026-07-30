# Cell Tracking Web App

A web application for detecting and tracking individual cells in 3D + time
microscopy videos. Upload a volume and get:

- an interactive slice viewer (single Z-slice or max-intensity projection,
  scrubbable by timepoint) with detection overlays,
- a lineage tree showing how tracked cells branch over time,
- a stats dashboard (cell counts over time, division events, average
  migration speed),
- CSV export in a submission-schema format, and an annotated MP4 export,
- a grounded Q&A chat and an auto-generated plain-language report, both
  backed by an LLM that reads the actual stored results rather than
  guessing (see "LLM features" below).

Backend: Python + FastAPI. Frontend: plain HTML/CSS/vanilla JS (D3.js and
Chart.js via CDN, no build step). No database -- job results are stored as
files on disk under `backend/jobs/`.

> **Note on the detection/tracking pipeline:** `backend/pipeline.py`
> currently contains a simple stub (threshold + local-maxima detection,
> nearest-neighbor linking) so the rest of the app can be built and tested
> end to end. It's meant to be swapped for a real pipeline later --
> `load_volume()` and `run_pipeline()` keep the same signatures either way,
> so nothing else in the app needs to change when that happens.

## LLM features

The chat and "Generate report" button (`backend/llm.py`) are a narration
layer on top of the pipeline's output -- they never compute or alter any
tracking result. The chat runs a tool-use loop where the model calls
read-only functions (`get_cell_count_at`, `get_division_events`,
`get_track_stats`, `get_fastest_tracks`, `get_overall_stats`) that read
straight from a job's stored `result.json`; the report prompt embeds the
overall stats and division-event list directly. Either way, every number
the model can mention came from that file, not from memory.

These features need an API key, set as an environment variable (never
hardcoded):

```bash
export ANTHROPIC_API_KEY=sk-ant-...      # default provider
# or, to use OpenAI instead:
export LLM_PROVIDER=openai
export OPENAI_API_KEY=sk-...
```

With Docker, pass it through with `-e`:

```bash
docker run --rm -p 8000:8000 -e ANTHROPIC_API_KEY=sk-ant-... cell-tracking-app
```

If the relevant key isn't set, the chat/report endpoints return a clear
503 error explaining what's missing, rather than crashing -- the rest of
the app (upload, viewer, lineage, stats, exports) works fine without it.

> Only the Anthropic path has been exercised against a live API while
> building this. The OpenAI path is implemented against its standard
> tool-calling API shape but hasn't been run against a real key -- treat
> `LLM_PROVIDER=openai` as unverified until you've tried it once yourself.

## What file formats it accepts

- `.tif` / `.tiff` -- a single file, shape `(T, Z, Y, X)`, 16-bit.
- `.zip` containing a `.zarr` directory -- since a directory-based format
  can't be uploaded as a single file over HTTP, zip it first. The `.zarr`
  folder can sit at the root of the zip or one level down.

Uploads are capped at 2 GiB and validated by extension before any
processing starts; anything else is rejected immediately with a clear
error message rather than failing partway through.

## Chat & report (LLM features)

Two optional features sit on top of the deterministic pipeline results:
a grounded Q&A chat about a job, and an auto-generated plain-language
summary report. Both are narration only -- they read a job's already-
computed `result.json` (via read-only tool calls, for chat; via numbers
placed directly in the prompt, for the report) and never compute or alter
any tracking result themselves. See `backend/llm.py` for details.

They need an LLM API key, set via environment variable:

```bash
export ANTHROPIC_API_KEY=sk-ant-...   # default provider
# or, to use OpenAI instead:
export LLM_PROVIDER=openai
export OPENAI_API_KEY=sk-...
```

For Docker, pass it through with `-e`:

```bash
docker run --rm -p 8000:8000 -e ANTHROPIC_API_KEY=sk-ant-... cell-tracking-app
```

Without a key set, the rest of the app works normally -- the chat and
report endpoints just return a clear "not configured" error instead of
crashing.

## Running locally

### Option A: Docker (recommended -- no local Python setup needed)

```bash
docker build -t cell-tracking-app .
docker run --rm -p 8000:8000 cell-tracking-app
```

Then open **http://localhost:8000**.

To keep job data (uploads/results) across container restarts, mount a
volume over the jobs directory:

```bash
docker run --rm -p 8000:8000 -v cell-tracking-jobs:/app/backend/jobs cell-tracking-app
```

### Option B: Plain Python (for development)

```bash
cd backend
pip install -r requirements.txt
uvicorn main:app --reload
```

Then open **http://localhost:8000** -- `main.py` serves the frontend
directly (mounted as static files), so there's nothing separate to start
on the frontend side.

## Project layout

```
.
├── Dockerfile
├── README.md
├── backend/
│   ├── main.py            FastAPI app: job lifecycle, slice/export endpoints
│   ├── pipeline.py         Volume loading + detection/tracking (currently a stub)
│   ├── video_export.py     Annotated-MP4 rendering for the video export
│   ├── llm.py               Grounded chat + report generation (read-only tools)
│   ├── requirements.txt
│   └── jobs/                Per-job data (created at runtime, not checked in)
└── frontend/
    ├── index.html
    ├── style.css
    ├── app.js               Upload flow, job polling
    ├── viewer.js             Slice viewer (Canvas)
    ├── stats.js              Stats dashboard (Chart.js)
    ├── lineage.js             Lineage tree (D3.js)
    ├── export.js              CSV / video export UI + report generation
    └── chat.js                Grounded Q&A chat UI
```

## Deploying a demo

This needs a real running Python process (background jobs, file storage,
CPU-bound image processing) -- it won't run on static-only hosting
(GitHub Pages, Netlify, Vercel's static tier, etc.). For a low-effort,
low/no-cost portfolio demo, two options that work well with the Docker
image above:

- **[Fly.io](https://fly.io)** -- `fly launch` picks up the Dockerfile
  automatically; free/low-cost tier is enough for occasional demo traffic.
  Attach a small persistent volume if you want job data to survive
  restarts (see the `-v` flag above, translated to `fly volumes`).
- **[Render](https://render.com)** -- "New Web Service" from this repo,
  it detects the Dockerfile and builds/deploys automatically. The free
  tier works for a demo, with the caveat that free services spin down
  after inactivity, so the first request after idling will be slow
  (cold start + the container coming back up).

A small VPS (e.g. a $4-6/mo DigitalOcean/Hetzner box) is the other
straightforward option if you'd rather not depend on a platform's free
tier -- `docker run` there is identical to running it locally.
