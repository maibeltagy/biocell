"""
llm.py
======

The "presentation layer" on top of the finished pipeline: a grounded Q&A
chat and an auto-generated plain-language report, both driven by an LLM.

HARD RULE: this module never computes or alters any tracking result. Every
number the LLM can mention comes from either (a) one of the read-only tool
functions below, called against the actual stored `result.json` for a job,
or (b) values placed directly in a prompt from that same file. The tool
functions here have no write path back into a job's stored data -- they
only ever read the `result` dict passed in and return derived values.

Supported LLM providers (pick via LLM_PROVIDER env var or auto-detected by API key):
  - "openrouter" (OpenRouter API - supports any model like openai/gpt-4o-mini, anthropic/claude-3.5-haiku, google/gemini-2.5-flash, deepseek/deepseek-chat)
  - "openai"     (Direct OpenAI API)
  - "anthropic"  (Direct Anthropic Claude API)
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional


def _load_env_files():
    """Automatically load .env from backend/.env or root .env into os.environ."""
    try:
        from dotenv import load_dotenv
        load_dotenv(Path(__file__).parent / ".env")
        load_dotenv(Path(__file__).parent.parent / ".env")
    except ImportError:
        for env_path in [Path(__file__).parent / ".env", Path(__file__).parent.parent / ".env"]:
            if env_path.exists():
                try:
                    with open(env_path, encoding="utf-8") as f:
                        for line in f:
                            line = line.strip()
                            if line and not line.startswith("#") and "=" in line:
                                k, v = line.split("=", 1)
                                os.environ.setdefault(k.strip(), v.strip().strip("'\""))
                except Exception:
                    pass

_load_env_files()


class LLMConfigError(RuntimeError):
    """
    Raised when the LLM layer can't run because it isn't configured --
    specifically a missing API key or invalid provider.
    """


# --------------------------------------------------------------------------
# Read-only data tools
# --------------------------------------------------------------------------

def get_cell_count_at(result: dict, t: int) -> dict:
    """Number of detected cells at a single timepoint t."""
    counts = result["stats"]["cell_count_per_frame"]
    if not (0 <= t < len(counts)):
        return {"error": f"t={t} is out of range for this job (valid range: 0..{len(counts) - 1})."}
    return {"t": t, "cell_count": counts[t]}


def get_division_events(
    result: dict, t_start: Optional[int] = None, t_end: Optional[int] = None
) -> dict:
    """Division events, optionally filtered to timepoints within [t_start, t_end] (inclusive)."""
    events = result["stats"]["division_events"]
    if t_start is not None:
        events = [e for e in events if e["t"] >= t_start]
    if t_end is not None:
        events = [e for e in events if e["t"] <= t_end]
    return {"count": len(events), "events": events}


def get_track_stats(result: dict, track_id: int) -> dict:
    """Stats for the track containing the given node id."""
    nodes_by_id = {n["id"]: n for n in result["nodes"]}
    if track_id not in nodes_by_id:
        return {"error": f"No node with id {track_id} exists in this job's results."}

    children_by_source: Dict[int, List[int]] = {}
    parent_by_target: Dict[int, int] = {}
    for e in result["edges"]:
        children_by_source.setdefault(e["source"], []).append(e["target"])
        parent_by_target[e["target"]] = e["source"]

    root_id = track_id
    while root_id in parent_by_target:
        root_id = parent_by_target[root_id]

    track_node_ids: List[int] = []
    stack = [root_id]
    while stack:
        nid = stack.pop()
        track_node_ids.append(nid)
        stack.extend(children_by_source.get(nid, []))

    track_nodes = [nodes_by_id[nid] for nid in track_node_ids]
    ts = [n["t"] for n in track_nodes]

    vox = result.get("voxel_scale_um", [1.0, 1.0, 1.0])
    if isinstance(vox, dict):
        z_s, y_s, x_s = vox.get("z", 1.0), vox.get("y", 1.0), vox.get("x", 1.0)
    elif hasattr(vox, "__getitem__"):
        z_s, y_s, x_s = vox[0], vox[1], vox[2]
    else:
        z_s, y_s, x_s = 1.0, 1.0, 1.0

    step_speeds = []
    for n in track_nodes:
        pid = parent_by_target.get(n["id"])
        if pid is not None and pid in nodes_by_id:
            p = nodes_by_id[pid]
            dz = (n["z"] - p["z"]) * z_s
            dy = (n["y"] - p["y"]) * y_s
            dx = (n["x"] - p["x"]) * x_s
            step_speeds.append((dz**2 + dy**2 + dx**2) ** 0.5)

    return {
        "track_root_id": root_id,
        "total_nodes": len(track_nodes),
        "t_start": min(ts),
        "t_end": max(ts),
        "n_frames_spanned": max(ts) - min(ts) + 1,
        "avg_speed_um_per_frame": float(sum(step_speeds) / len(step_speeds)) if step_speeds else 0.0,
    }


def get_fastest_tracks(result: dict, limit: int = 5) -> dict:
    """Return top N fastest tracks by average speed."""
    nodes_by_id = {n["id"]: n for n in result["nodes"]}
    has_incoming = {e["target"] for e in result["edges"]}
    root_ids = [n["id"] for n in result["nodes"] if n["id"] not in has_incoming]

    rankings = []
    for rid in root_ids:
        stats = get_track_stats(result, rid)
        if "error" not in stats and stats["total_nodes"] > 1:
            rankings.append(stats)

    rankings.sort(key=lambda s: s["avg_speed_um_per_frame"], reverse=True)
    return {"fastest_tracks": rankings[:limit]}


def get_overall_stats(result: dict) -> dict:
    """Summary metrics for the job."""
    s = result["stats"]
    return {
        "total_cells_detected": len(result["nodes"]),
        "total_edges": len(result["edges"]),
        "n_frames": len(s["cell_count_per_frame"]),
        "total_division_events": len(s["division_events"]),
        "avg_speed_um_per_frame": s["avg_speed_um_per_frame"],
        "volume_shape": result["volume_shape"],
        "voxel_scale_um": result.get("voxel_scale_um"),
    }


TOOL_SPECS = [
    {
        "name": "get_cell_count_at",
        "description": "Returns the number of cell detections at timepoint t.",
        "parameters": {
            "type": "object",
            "properties": {"t": {"type": "integer", "description": "0-indexed timepoint index"}},
            "required": ["t"],
        },
    },
    {
        "name": "get_division_events",
        "description": "Returns cell division events, optionally filtered by time range.",
        "parameters": {
            "type": "object",
            "properties": {
                "t_start": {"type": "integer", "description": "Optional start timepoint"},
                "t_end": {"type": "integer", "description": "Optional end timepoint"},
            },
        },
    },
    {
        "name": "get_track_stats",
        "description": "Returns stats for the track containing node_id.",
        "parameters": {
            "type": "object",
            "properties": {"track_id": {"type": "integer", "description": "Any node ID in the track"}},
            "required": ["track_id"],
        },
    },
    {
        "name": "get_fastest_tracks",
        "description": "Returns top N fastest tracks by average speed.",
        "parameters": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "description": "Number of tracks to return (default 5)"}},
        },
    },
    {
        "name": "get_overall_stats",
        "description": "Returns high-level summary metrics for this job.",
        "parameters": {"type": "object", "properties": {}},
    },
]


def _execute_tool(result: dict, tool_name: str, tool_args: dict) -> dict:
    if tool_name == "get_cell_count_at":
        return get_cell_count_at(result, tool_args.get("t", 0))
    if tool_name == "get_division_events":
        return get_division_events(result, tool_args.get("t_start"), tool_args.get("t_end"))
    if tool_name == "get_track_stats":
        return get_track_stats(result, tool_args.get("track_id", -1))
    if tool_name == "get_fastest_tracks":
        return get_fastest_tracks(result, tool_args.get("limit", 5))
    if tool_name == "get_overall_stats":
        return get_overall_stats(result)
    return {"error": f"Unknown tool: '{tool_name}'"}


# --------------------------------------------------------------------------
# Prompts
# --------------------------------------------------------------------------

CHAT_SYSTEM_PROMPT = """\
You are an expert bioimage analyst assistant embedded in a 3D cell-tracking web app.
Answer user questions about the current job by calling the provided data tools.
Summarize only numbers returned by the tools. Do not speculate or invent numbers."""

REPORT_SYSTEM_PROMPT = """\
You write short, plain-language summary reports of cell-tracking analysis results.
Summarize only the exact numbers provided in the user message in 2 to 4 paragraphs."""


# --------------------------------------------------------------------------
# Provider Dispatch & Auto-Detection
# --------------------------------------------------------------------------

def _detect_provider() -> str:
    env_provider = os.environ.get("LLM_PROVIDER", "").strip().lower()
    if env_provider:
        return env_provider

    if os.environ.get("OPENROUTER_API_KEY"):
        return "openrouter"
    if os.environ.get("OPENAI_API_KEY"):
        return "openai"
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic"

    return "openrouter"


LLM_PROVIDER = _detect_provider()

_DEFAULT_MODELS = {
    "openrouter": "openai/gpt-4o-mini",
    "openai": "gpt-4o-mini",
    "anthropic": "claude-3-5-sonnet-20241022",
}

LLM_MODEL = os.environ.get("LLM_MODEL", _DEFAULT_MODELS.get(LLM_PROVIDER, "openai/gpt-4o-mini"))

MAX_TOOL_ITERATIONS = 6


def run_chat_turn(result: dict, message: str, history: List[dict]) -> str:
    """Run one chat turn against the configured provider."""
    provider = _detect_provider()

    if provider == "openrouter":
        return _run_chat_openrouter(result, message, history)
    if provider == "openai":
        return _run_chat_openai(result, message, history)
    if provider == "anthropic":
        return _run_chat_anthropic(result, message, history)

    raise LLMConfigError(
        f"Unknown LLM_PROVIDER '{provider}'. Supported values: 'openrouter', 'openai', 'anthropic'."
    )


def generate_report(result: dict) -> str:
    """Generate plain-language report against configured provider."""
    provider = _detect_provider()

    if provider == "openrouter":
        return _generate_report_openrouter(result)
    if provider == "openai":
        return _generate_report_openai(result)
    if provider == "anthropic":
        return _generate_report_anthropic(result)

    raise LLMConfigError(
        f"Unknown LLM_PROVIDER '{provider}'. Supported values: 'openrouter', 'openai', 'anthropic'."
    )


def _report_data_prompt(result: dict) -> str:
    overall = get_overall_stats(result)
    data = {
        "overall_stats": overall,
        "division_events": result["stats"]["division_events"],
    }
    return (
        "Here are the complete results of a cell-tracking analysis job. "
        "Write a short summary report based ONLY on these numbers:\n\n"
        + json.dumps(data, indent=2)
    )


# --------------------------------------------------------------------------
# OpenRouter Provider (OpenAI SDK Compatible)
# --------------------------------------------------------------------------

def _get_openrouter_client():
    api_key = os.environ.get("OPENROUTER_API_KEY") or os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise LLMConfigError(
            "OPENROUTER_API_KEY is not set. Set OPENROUTER_API_KEY environment variable to enable OpenRouter."
        )
    import openai
    return openai.OpenAI(
        api_key=api_key,
        base_url="https://openrouter.ai/api/v1",
        default_headers={
            "HTTP-Referer": "https://celltrack.app",
            "X-Title": "CellTrack App",
        },
    )


def _run_chat_openrouter(result: dict, message: str, history: List[dict]) -> str:
    client = _get_openrouter_client()
    return _run_chat_openai_compatible(client, result, message, history)


def _generate_report_openrouter(result: dict) -> str:
    client = _get_openrouter_client()
    return _generate_report_openai_compatible(client, result)


# --------------------------------------------------------------------------
# OpenAI Provider
# --------------------------------------------------------------------------

def _get_openai_client():
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise LLMConfigError(
            "OPENAI_API_KEY is not set. Set OPENAI_API_KEY environment variable to enable OpenAI."
        )
    import openai
    return openai.OpenAI(api_key=api_key)


def _run_chat_openai(result: dict, message: str, history: List[dict]) -> str:
    client = _get_openai_client()
    return _run_chat_openai_compatible(client, result, message, history)


def _generate_report_openai(result: dict) -> str:
    client = _get_openai_client()
    return _generate_report_openai_compatible(client, result)


# --------------------------------------------------------------------------
# OpenAI-Compatible Tool Call Runner (Used by OpenAI & OpenRouter)
# --------------------------------------------------------------------------

def _openai_tool_defs() -> List[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t["description"],
                "parameters": t["parameters"],
            },
        }
        for t in TOOL_SPECS
    ]


def _run_chat_openai_compatible(client, result: dict, message: str, history: List[dict]) -> str:
    messages: List[dict] = [{"role": "system", "content": CHAT_SYSTEM_PROMPT}]
    for turn in history:
        role = turn.get("role")
        content = turn.get("content", "")
        if role in ("user", "assistant") and isinstance(content, str):
            messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": message})

    model_name = LLM_MODEL or "openai/gpt-4o-mini"

    for _ in range(MAX_TOOL_ITERATIONS):
        response = client.chat.completions.create(
            model=model_name,
            max_tokens=1024,
            tools=_openai_tool_defs(),
            messages=messages,
        )
        choice = response.choices[0]
        msg = choice.message

        if not msg.tool_calls:
            return (msg.content or "").strip() or "(The model returned an empty response.)"

        messages.append(
            {
                "role": "assistant",
                "content": msg.content,
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                    }
                    for tc in msg.tool_calls
                ],
            }
        )

        for tc in msg.tool_calls:
            try:
                tool_input = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                tool_input = {}
            output = _execute_tool(result, tc.function.name, tool_input)
            messages.append(
                {"role": "tool", "tool_call_id": tc.id, "content": json.dumps(output)}
            )

    return (
        "I wasn't able to finish answering that within the allowed number of tool calls. "
        "Try asking a more specific question."
    )


def _generate_report_openai_compatible(client, result: dict) -> str:
    model_name = LLM_MODEL or "openai/gpt-4o-mini"
    response = client.chat.completions.create(
        model=model_name,
        max_tokens=800,
        messages=[
            {"role": "system", "content": REPORT_SYSTEM_PROMPT},
            {"role": "user", "content": _report_data_prompt(result)},
        ],
    )
    return (response.choices[0].message.content or "").strip() or "(The model returned an empty response.)"


# --------------------------------------------------------------------------
# Anthropic Provider
# --------------------------------------------------------------------------

def _get_anthropic_client():
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise LLMConfigError(
            "ANTHROPIC_API_KEY is not set. Set ANTHROPIC_API_KEY environment variable to enable Anthropic."
        )
    import anthropic
    return anthropic.Anthropic(api_key=api_key)


def _anthropic_tool_defs() -> List[dict]:
    return [
        {"name": t["name"], "description": t["description"], "input_schema": t["parameters"]}
        for t in TOOL_SPECS
    ]


def _run_chat_anthropic(result: dict, message: str, history: List[dict]) -> str:
    client = _get_anthropic_client()

    messages: List[dict] = []
    for turn in history:
        role = turn.get("role")
        content = turn.get("content", "")
        if role in ("user", "assistant") and isinstance(content, str):
            messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": message})

    model_name = LLM_MODEL or "claude-3-5-sonnet-20241022"

    for _ in range(MAX_TOOL_ITERATIONS):
        response = client.messages.create(
            model=model_name,
            max_tokens=1024,
            system=CHAT_SYSTEM_PROMPT,
            tools=_anthropic_tool_defs(),
            messages=messages,
        )

        if response.stop_reason != "tool_use":
            return _anthropic_extract_text(response)

        messages.append({"role": "assistant", "content": response.content})

        tool_result_blocks = []
        for block in response.content:
            if block.type == "tool_use":
                output = _execute_tool(result, block.name, block.input)
                tool_result_blocks.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": json.dumps(output),
                    }
                )
        messages.append({"role": "user", "content": tool_result_blocks})

    return (
        "I wasn't able to finish answering that within the allowed number of tool calls. "
        "Try asking a more specific question."
    )


def _anthropic_extract_text(response) -> str:
    parts = [block.text for block in response.content if block.type == "text"]
    return "\n".join(parts).strip() or "(The model returned an empty response.)"


def _generate_report_anthropic(result: dict) -> str:
    client = _get_anthropic_client()
    model_name = LLM_MODEL or "claude-3-5-sonnet-20241022"
    response = client.messages.create(
        model=model_name,
        max_tokens=800,
        system=REPORT_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": _report_data_prompt(result)}],
    )
    return _anthropic_extract_text(response)
