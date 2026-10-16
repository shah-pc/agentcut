"""AgentCut — Deterministic headless video editor for AI agents."""

from __future__ import annotations

__version__ = "1.0.1"

from .probe import AgentCutError, ensure_ffmpeg_installed, probe_media
from .render import render_timeline
from .schema import ValidationResult, validate_timeline

__all__ = [
    "__version__",
    "AgentCutError",
    "ValidationResult",
    "ensure_ffmpeg_installed",
    "probe_media",
    "render_timeline",
    "validate_timeline",
]
