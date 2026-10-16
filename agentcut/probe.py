"""FFmpeg/FFprobe binary checks and media probing utilities."""

from __future__ import annotations

import json
import shutil
import subprocess
from fractions import Fraction
from pathlib import Path
from typing import Any

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tiff", ".gif"}


class AgentCutError(Exception):
    """Structured exception for all deterministic AgentCut errors."""

    def __init__(
        self,
        message: str,
        code: str = "AGENTCUT_ERROR",
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        """Serialize the error into a machine-readable JSON dictionary."""
        return {
            "ok": False,
            "error": {
                "code": self.code,
                "message": self.message,
                "details": self.details,
            },
        }


def ensure_ffmpeg_installed() -> dict[str, str]:
    """Verify that both `ffmpeg` and `ffprobe` are available on PATH.

    Raises:
        AgentCutError: If either binary is missing, with OS-specific install hints.
    """
    ffmpeg_bin = shutil.which("ffmpeg")
    ffprobe_bin = shutil.which("ffprobe")
    missing = []
    if not ffmpeg_bin:
        missing.append("ffmpeg")
    if not ffprobe_bin:
        missing.append("ffprobe")

    if missing:
        hint = (
            f"Missing required binary on PATH: {', '.join(missing)}. "
            "Install FFmpeg before running AgentCut:\n"
            "  • macOS (Homebrew): brew install ffmpeg\n"
            "  • Ubuntu / Debian:  sudo apt-get update && sudo apt-get install -y ffmpeg\n"
            "  • Windows (Winget): winget install Gyan.FFmpeg"
        )
        raise AgentCutError(
            hint,
            code="FFMPEG_NOT_FOUND",
            details={"missing_binaries": missing},
        )
    return {"ffmpeg": str(ffmpeg_bin), "ffprobe": str(ffprobe_bin)}


def _parse_frame_rate(rate_str: str | None) -> float | None:
    """Parse an ffprobe rational frame rate string like '30/1' into a float."""
    if not rate_str or rate_str in ("0/0", "N/A"):
        return None
    try:
        frac = Fraction(rate_str)
        return round(float(frac), 4) if frac.denominator != 0 else None
    except (ValueError, ZeroDivisionError):
        return None


def probe_media(path: str | Path) -> dict[str, Any]:
    """Inspect a media file using `ffprobe` and return structured JSON metadata.

    Args:
        path: Path to the media file (video, audio, or image).

    Returns:
        Dictionary containing normalized media properties and raw stream info.

    Raises:
        AgentCutError: If the file does not exist or `ffprobe` fails.
    """
    ensure_ffmpeg_installed()
    file_path = Path(path).expanduser().resolve()
    if not file_path.exists():
        raise AgentCutError(
            f"Media file does not exist: {file_path}",
            code="FILE_NOT_FOUND",
            details={"path": str(file_path)},
        )
    if not file_path.is_file():
        raise AgentCutError(
            f"Path is not a regular file: {file_path}",
            code="INVALID_FILE_PATH",
            details={"path": str(file_path)},
        )

    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(file_path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise AgentCutError(
            f"ffprobe failed on '{file_path.name}': {proc.stderr.strip() or 'unrecognized media format'}",
            code="FFPROBE_FAILED",
            details={"path": str(file_path), "stderr": proc.stderr.strip()},
        )

    try:
        raw = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise AgentCutError(
            f"Failed to parse ffprobe JSON output for '{file_path.name}'",
            code="FFPROBE_PARSE_ERROR",
            details={"path": str(file_path), "error": str(exc)},
        ) from exc

    streams: list[dict[str, Any]] = raw.get("streams", [])
    fmt: dict[str, Any] = raw.get("format", {})

    video_streams = [s for s in streams if s.get("codec_type") == "video"]
    audio_streams = [s for s in streams if s.get("codec_type") == "audio"]

    ext = file_path.suffix.lower()
    format_name = str(fmt.get("format_name", ""))
    is_image = ext in IMAGE_EXTENSIONS or "image2" in format_name or "png_pipe" in format_name

    duration: float | None = None
    if not is_image:
        if "duration" in fmt:
            try:
                duration = float(fmt["duration"])
            except (TypeError, ValueError):
                duration = None
        if duration is None:
            stream_durations = []
            for s in streams:
                if "duration" in s:
                    try:
                        stream_durations.append(float(s["duration"]))
                    except (TypeError, ValueError):
                        pass
            if stream_durations:
                duration = max(stream_durations)

    v_stream = video_streams[0] if video_streams else {}
    a_stream = audio_streams[0] if audio_streams else {}

    width = int(v_stream["width"]) if "width" in v_stream else None
    height = int(v_stream["height"]) if "height" in v_stream else None
    fps = _parse_frame_rate(v_stream.get("avg_frame_rate")) or _parse_frame_rate(
        v_stream.get("r_frame_rate")
    )
    if is_image:
        fps = None

    channels = int(a_stream["channels"]) if "channels" in a_stream else None
    sample_rate = int(a_stream["sample_rate"]) if "sample_rate" in a_stream else None

    size_bytes = file_path.stat().st_size

    return {
        "ok": True,
        "path": str(file_path),
        "filename": file_path.name,
        "format_name": format_name,
        "duration": round(duration, 4) if duration is not None else None,
        "size_bytes": size_bytes,
        "is_image": is_image,
        "has_video": bool(video_streams),
        "has_audio": bool(audio_streams),
        "video": {
            "codec": v_stream.get("codec_name"),
            "width": width,
            "height": height,
            "fps": fps,
            "pix_fmt": v_stream.get("pix_fmt"),
        }
        if video_streams
        else None,
        "audio": {
            "codec": a_stream.get("codec_name"),
            "channels": channels,
            "channel_layout": a_stream.get("channel_layout"),
            "sample_rate": sample_rate,
        }
        if audio_streams
        else None,
        "streams": streams,
        "format": fmt,
    }
