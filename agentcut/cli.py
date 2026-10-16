"""Command-line interface (`agentcut`) for rendering, validating, probing, and listing templates."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .probe import AgentCutError, ensure_ffmpeg_installed, probe_media
from .render import render_timeline
from .schema import validate_timeline


def get_templates_dir() -> Path:
    """Locate the bundled `templates/` directory inside the installed package first."""
    pkg_dir = Path(__file__).resolve().parent
    candidates = [
        pkg_dir / "templates",
        pkg_dir.parent / "templates",
        Path.cwd() / "templates",
    ]
    for c in candidates:
        if c.is_dir():
            return c
    return candidates[0]


def list_bundled_templates() -> dict[str, Any]:
    """Return metadata for all bundled JSON timeline templates."""
    t_dir = get_templates_dir()
    items: list[dict[str, Any]] = []
    if t_dir.is_dir():
        for p in sorted(t_dir.glob("*.json")):
            try:
                raw = json.loads(p.read_text(encoding="utf-8"))
                canvas = raw.get("canvas", {})
                tracks = raw.get("tracks", [])
                items.append(
                    {
                        "name": p.stem,
                        "filename": p.name,
                        "path": str(p.resolve()),
                        "description": raw.get("_description", f"AgentCut {p.stem} template"),
                        "canvas": canvas,
                        "track_types": [t.get("type") for t in tracks if isinstance(t, dict)],
                    }
                )
            except Exception as exc:
                items.append({"name": p.stem, "filename": p.name, "path": str(p), "error": str(exc)})
    return {"ok": True, "templates_dir": str(t_dir), "templates": items}


def _emit_json(payload: dict[str, Any], exit_code: int = 0) -> int:
    """Print deterministic JSON to stdout and return the process exit code."""
    print(json.dumps(payload, indent=2, ensure_ascii=False), file=sys.stdout, flush=True)
    return exit_code


def main(argv: list[str] | None = None) -> int:
    """Entry point for the `agentcut` CLI."""
    parser = argparse.ArgumentParser(
        prog="agentcut",
        description="AgentCut — Deterministic headless FFmpeg video editor for AI agents.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # agentcut render <timeline.json> [-o out.mp4]
    p_render = subparsers.add_parser("render", help="Render a timeline JSON file to MP4.")
    p_render.add_argument("timeline", help="Path to timeline.json file.")
    p_render.add_argument("-o", "--output", default=None, help="Optional override for output MP4 path.")

    # agentcut validate <timeline.json> [--no-check-files]
    p_validate = subparsers.add_parser(
        "validate", help="Validate timeline schema and check referenced files."
    )
    p_validate.add_argument("timeline", help="Path to timeline.json file.")
    p_validate.add_argument(
        "--no-check-files",
        action="store_true",
        help="Validate schema structure only without checking referenced media files on disk.",
    )

    # agentcut probe <media.mp4>
    p_probe = subparsers.add_parser("probe", help="Inspect a media file with ffprobe and emit JSON.")
    p_probe.add_argument("media", help="Path to media file (video, audio, or image).")

    # agentcut templates
    subparsers.add_parser("templates", help="List bundled AgentCut timeline templates as JSON.")

    args = parser.parse_args(argv)

    try:
        if args.command == "templates":
            return _emit_json(list_bundled_templates(), exit_code=0)

        ensure_ffmpeg_installed()

        if args.command == "probe":
            info = probe_media(args.media)
            return _emit_json(info, exit_code=0)

        if args.command == "validate":
            res = validate_timeline(args.timeline, check_files=not args.no_check_files)
            payload = res.to_dict()
            if not res.ok:
                for err in res.errors:
                    print(
                        f"[validate error] {err['location']}: {err['message']}",
                        file=sys.stderr,
                        flush=True,
                    )
                return _emit_json(payload, exit_code=1)
            print(
                f"[agentcut] Timeline valid (estimated duration: {res.estimated_duration:.2f}s)",
                file=sys.stderr,
                flush=True,
            )
            return _emit_json(payload, exit_code=0)

        if args.command == "render":
            summary = render_timeline(args.timeline, output_override=args.output)
            return _emit_json(summary, exit_code=0)

    except AgentCutError as exc:
        print(f"[agentcut error] [{exc.code}] {exc.message}", file=sys.stderr, flush=True)
        return _emit_json(exc.to_dict(), exit_code=1)
    except Exception as exc:
        err = AgentCutError(str(exc), code="UNEXPECTED_ERROR")
        print(f"[agentcut error] {exc}", file=sys.stderr, flush=True)
        return _emit_json(err.to_dict(), exit_code=1)

    return 0


if __name__ == "__main__":
    sys.exit(main())
