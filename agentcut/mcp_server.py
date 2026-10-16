"""Model Context Protocol (MCP) server (`agentcut-mcp`) for AI agents.

Exposes three deterministic tools:
  - `render_timeline`: Render a timeline file path OR a timeline JSON object to MP4.
  - `validate_timeline`: Validate timeline schema and referenced media files.
  - `probe_media`: Inspect a media file via ffprobe and return structured JSON metadata.

Supports the `mcp` Python package (`FastMCP`) when installed, and includes a built-in
standard-library JSON-RPC 2.0 stdio server fallback so `agentcut-mcp` works with zero
third-party dependencies.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from .probe import AgentCutError, probe_media as _probe_media
from .render import render_timeline as _render_timeline
from .schema import validate_timeline as _validate_timeline


def tool_render_timeline(
    timeline: str | dict[str, Any],
    base_dir: str | None = None,
    output_path: str | None = None,
) -> dict[str, Any]:
    """Render an AgentCut timeline (file path or JSON object/string) and return output summary + probe."""
    try:
        parsed_input: str | Path | dict[str, Any] = timeline
        if isinstance(timeline, str):
            stripped = timeline.strip()
            if stripped.startswith("{"):
                parsed_input = json.loads(stripped)
        resolved_base = Path(base_dir).expanduser().resolve() if base_dir else None
        render_summary = _render_timeline(
            parsed_input,
            base_dir=resolved_base,
            output_override=output_path,
        )
        probe_info = _probe_media(render_summary["output_path"])
        return {
            "ok": True,
            "output_path": render_summary["output_path"],
            "render_summary": render_summary,
            "probe_summary": {
                "duration": probe_info["duration"],
                "size_bytes": probe_info["size_bytes"],
                "video": probe_info["video"],
                "audio": probe_info["audio"],
            },
        }
    except AgentCutError as exc:
        return exc.to_dict()
    except Exception as exc:
        return AgentCutError(str(exc), code="MCP_RENDER_ERROR").to_dict()


def tool_validate_timeline(
    timeline: str | dict[str, Any],
    base_dir: str | None = None,
    check_files: bool = True,
) -> dict[str, Any]:
    """Validate an AgentCut timeline file path or JSON object."""
    try:
        parsed_input: str | Path | dict[str, Any] = timeline
        if isinstance(timeline, str):
            stripped = timeline.strip()
            if stripped.startswith("{"):
                parsed_input = json.loads(stripped)
        resolved_base = Path(base_dir).expanduser().resolve() if base_dir else None
        res = _validate_timeline(parsed_input, base_dir=resolved_base, check_files=check_files)
        return res.to_dict()
    except AgentCutError as exc:
        return exc.to_dict()
    except Exception as exc:
        return AgentCutError(str(exc), code="MCP_VALIDATE_ERROR").to_dict()


def tool_probe_media(path: str) -> dict[str, Any]:
    """Probe a video, audio, or image file using ffprobe and return structured JSON."""
    try:
        return _probe_media(path)
    except AgentCutError as exc:
        return exc.to_dict()
    except Exception as exc:
        return AgentCutError(str(exc), code="MCP_PROBE_ERROR").to_dict()


MCP_TOOLS_SPEC: list[dict[str, Any]] = [
    {
        "name": "render_timeline",
        "description": (
            "Render an AgentCut v1.0 timeline (accepts either a file path to timeline.json "
            "or a timeline JSON object) into an MP4 file and run all loudness/channel safety gates."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "timeline": {
                    "description": "Path to a timeline.json file OR a timeline JSON object.",
                    "anyOf": [{"type": "string"}, {"type": "object"}],
                },
                "base_dir": {
                    "type": "string",
                    "description": "Optional directory to resolve relative media paths against.",
                },
                "output_path": {
                    "type": "string",
                    "description": "Optional override path for the rendered MP4 file.",
                },
            },
            "required": ["timeline"],
        },
    },
    {
        "name": "validate_timeline",
        "description": (
            "Validate an AgentCut timeline against the v1.0 schema and verify that all "
            "referenced media files exist and contain valid streams."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "timeline": {
                    "description": "Path to a timeline.json file OR a timeline JSON object.",
                    "anyOf": [{"type": "string"}, {"type": "object"}],
                },
                "base_dir": {
                    "type": "string",
                    "description": "Optional directory to resolve relative media paths against.",
                },
                "check_files": {
                    "type": "boolean",
                    "description": "Whether to verify referenced files exist on disk (default: true).",
                    "default": True,
                },
            },
            "required": ["timeline"],
        },
    },
    {
        "name": "probe_media",
        "description": "Probe a video, audio, or image file using ffprobe and return JSON metadata.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the media file to inspect.",
                }
            },
            "required": ["path"],
        },
    },
]


def handle_jsonrpc_message(msg: dict[str, Any]) -> dict[str, Any] | None:
    """Process a single MCP JSON-RPC 2.0 message and return a response dict (or None for notifications)."""
    method = msg.get("method")
    req_id = msg.get("id")
    params = msg.get("params") or {}

    if req_id is None:
        # Notification (e.g., notifications/initialized)
        return None

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "agentcut-mcp", "version": "1.0.0"},
            },
        }

    if method == "ping":
        return {"jsonrpc": "2.0", "id": req_id, "result": {}}

    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"tools": MCP_TOOLS_SPEC},
        }

    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        if name == "render_timeline":
            res = tool_render_timeline(
                timeline=args.get("timeline"),
                base_dir=args.get("base_dir"),
                output_path=args.get("output_path"),
            )
        elif name == "validate_timeline":
            res = tool_validate_timeline(
                timeline=args.get("timeline"),
                base_dir=args.get("base_dir"),
                check_files=bool(args.get("check_files", True)),
            )
        elif name == "probe_media":
            res = tool_probe_media(path=str(args.get("path", "")))
        else:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32601, "message": f"Unknown tool: {name}"},
            }

        is_error = not bool(res.get("ok", False))
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "content": [{"type": "text", "text": json.dumps(res, indent=2, ensure_ascii=False)}],
                "isError": is_error,
            },
        }

    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": -32601, "message": f"Method not found: {method}"},
    }


def _run_stdio_fallback() -> None:
    """Run the standard-library MCP JSON-RPC 2.0 server over stdin/stdout."""
    for raw_line in sys.stdin:
        line = raw_line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
            resp = handle_jsonrpc_message(msg)
            if resp is not None:
                sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
                sys.stdout.flush()
        except Exception as exc:
            err_resp = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": f"Parse/dispatch error: {exc}"},
            }
            sys.stdout.write(json.dumps(err_resp) + "\n")
            sys.stdout.flush()


def main() -> None:
    """Start the `agentcut-mcp` server via `mcp.server.fastmcp` if installed, else stdio JSON-RPC."""
    try:
        from mcp.server.fastmcp import FastMCP  # type: ignore

        mcp = FastMCP("agentcut")

        @mcp.tool(name="render_timeline")
        def render_timeline(
            timeline: str | dict[str, Any],
            base_dir: str | None = None,
            output_path: str | None = None,
        ) -> str:
            """Render an AgentCut timeline file path or JSON object to MP4."""
            return json.dumps(
                tool_render_timeline(timeline=timeline, base_dir=base_dir, output_path=output_path),
                indent=2,
                ensure_ascii=False,
            )

        @mcp.tool(name="validate_timeline")
        def validate_timeline(
            timeline: str | dict[str, Any],
            base_dir: str | None = None,
            check_files: bool = True,
        ) -> str:
            """Validate an AgentCut timeline file path or JSON object."""
            return json.dumps(
                tool_validate_timeline(timeline=timeline, base_dir=base_dir, check_files=check_files),
                indent=2,
                ensure_ascii=False,
            )

        @mcp.tool(name="probe_media")
        def probe_media(path: str) -> str:
            """Inspect a media file using ffprobe and return structured JSON metadata."""
            return json.dumps(tool_probe_media(path=path), indent=2, ensure_ascii=False)

        mcp.run()
    except ImportError:
        _run_stdio_fallback()


if __name__ == "__main__":
    main()
