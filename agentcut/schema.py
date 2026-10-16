"""Timeline v1.0 schema validation and referenced asset verification."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .probe import AgentCutError, probe_media

SUPPORTED_VERSION = "1.0"
VALID_TRACK_TYPES = {"video", "audio", "captions", "overlay"}
VALID_TRANSITIONS = {"fade", "dissolve", "wipeleft", "slideup"}
VALID_OVERLAY_ANIMATIONS = {"none", "fade", "pop", "bounce"}
VALID_CAPTION_POSITIONS = {"bottom", "center", "middle", "top"}
VALID_AUDIO_ROLES = {"dialogue", "music", "sfx", "ambience"}
VALID_VIDEO_GRADES = {"none", "bw", "grayscale", "sepia", "high_contrast"}
VALID_VIDEO_FITS = {"pad", "crop", "contain", "cover"}


@dataclass
class ValidationResult:
    """Structured result returned by `validate_timeline`."""

    ok: bool
    errors: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    estimated_duration: float = 0.0
    timeline_path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert validation results into a JSON-serializable dict."""
        return {
            "ok": self.ok,
            "timeline_path": self.timeline_path,
            "estimated_duration": round(self.estimated_duration, 3),
            "error_count": len(self.errors),
            "errors": self.errors,
            "warnings": self.warnings,
        }


def resolve_source_path(source: str, base_dir: Path) -> Path:
    """Resolve a clip's `source` path relative to the timeline file directory."""
    p = Path(source).expanduser()
    if not p.is_absolute():
        p = (base_dir / p).resolve()
    return p


def load_timeline_input(
    timeline_input: str | Path | dict[str, Any],
) -> tuple[dict[str, Any], Path, str | None]:
    """Load a timeline from either a JSON file path or an in-memory dict.

    Returns:
        Tuple of (timeline_dict, base_dir, timeline_file_path_or_None).
    """
    if isinstance(timeline_input, dict):
        return timeline_input, Path.cwd().resolve(), None

    path = Path(timeline_input).expanduser().resolve()
    if not path.exists():
        raise AgentCutError(
            f"Timeline file not found: {path}",
            code="TIMELINE_NOT_FOUND",
            details={"path": str(path)},
        )
    try:
        text = path.read_text(encoding="utf-8")
        data = json.loads(text)
    except Exception as exc:
        raise AgentCutError(
            f"Failed to parse timeline JSON '{path.name}': {exc}",
            code="INVALID_TIMELINE_JSON",
            details={"path": str(path), "error": str(exc)},
        ) from exc

    if not isinstance(data, dict):
        raise AgentCutError(
            "Timeline root must be a JSON object.",
            code="INVALID_TIMELINE_ROOT",
            details={"path": str(path)},
        )
    return data, path.parent, str(path)


def _add_err(
    errors: list[dict[str, Any]],
    loc: str,
    message: str,
    code: str = "SCHEMA_VALIDATION_ERROR",
) -> None:
    errors.append({"location": loc, "code": code, "message": message})


def validate_timeline(
    timeline_input: str | Path | dict[str, Any],
    base_dir: Path | None = None,
    check_files: bool = True,
) -> ValidationResult:
    """Validate a timeline object or file against the AgentCut v1.0 schema.

    Args:
        timeline_input: Path to a timeline `.json` file or a parsed dict.
        base_dir: Base directory for resolving relative asset paths.
        check_files: Whether to verify that referenced media files exist and probe cleanly.

    Returns:
        ValidationResult with human- and machine-readable errors and estimated duration.
    """
    data, inferred_base_dir, timeline_path = load_timeline_input(timeline_input)
    root_dir = (base_dir or inferred_base_dir).resolve()

    errors: list[dict[str, Any]] = []
    warnings: list[str] = []
    max_end_time = 0.0

    # 1. Version
    version = data.get("version")
    if version != SUPPORTED_VERSION:
        _add_err(
            errors,
            "version",
            f"Unsupported or missing version '{version}'. Expected '{SUPPORTED_VERSION}'.",
        )

    # 2. Canvas
    canvas = data.get("canvas")
    if not isinstance(canvas, dict):
        _add_err(errors, "canvas", "Missing or invalid 'canvas' object.")
    else:
        for dim in ("width", "height"):
            val = canvas.get(dim)
            if not isinstance(val, int) or isinstance(val, bool) or val < 16 or val % 2 != 0:
                _add_err(
                    errors,
                    f"canvas.{dim}",
                    f"Canvas '{dim}' must be an even integer >= 16 (got {val!r}).",
                )
        fps = canvas.get("fps")
        if not isinstance(fps, (int, float)) or isinstance(fps, bool) or fps <= 0 or fps > 240:
            _add_err(
                errors,
                "canvas.fps",
                f"Canvas 'fps' must be a number between 1 and 240 (got {fps!r}).",
            )
        bg = canvas.get("background", "black")
        if not isinstance(bg, str) or not bg.strip():
            _add_err(errors, "canvas.background", "Canvas 'background' must be a non-empty string.")

    # 3. Output
    output = data.get("output")
    if not isinstance(output, dict):
        _add_err(errors, "output", "Missing or invalid 'output' object.")
    else:
        out_path = output.get("path")
        if not isinstance(out_path, str) or not out_path.strip():
            _add_err(errors, "output.path", "Output 'path' must be a non-empty string.")
        lufs = output.get("loudnorm_lufs", -14)
        if not isinstance(lufs, (int, float)) or isinstance(lufs, bool) or not (-70.0 <= float(lufs) <= -5.0):
            _add_err(
                errors,
                "output.loudnorm_lufs",
                f"Output 'loudnorm_lufs' must be a number between -70 and -5 (got {lufs!r}).",
            )

    # 4. Tracks
    tracks = data.get("tracks")
    if not isinstance(tracks, list) or len(tracks) == 0:
        _add_err(errors, "tracks", "'tracks' must be a non-empty list of track objects.")
        return ValidationResult(
            ok=False,
            errors=errors,
            warnings=warnings,
            estimated_duration=0.0,
            timeline_path=timeline_path,
        )

    video_track_count = 0
    for t_idx, track in enumerate(tracks):
        t_loc = f"tracks[{t_idx}]"
        if not isinstance(track, dict):
            _add_err(errors, t_loc, "Track must be a JSON object.")
            continue

        t_type = track.get("type")
        if t_type not in VALID_TRACK_TYPES:
            _add_err(
                errors,
                f"{t_loc}.type",
                f"Invalid track type {t_type!r}. Must be one of {sorted(VALID_TRACK_TYPES)}.",
            )
            continue

        if t_type == "video":
            video_track_count += 1
            grade = track.get("grade", "none")
            if grade not in VALID_VIDEO_GRADES:
                _add_err(
                    errors,
                    f"{t_loc}.grade",
                    f"Unsupported video grade {grade!r}. Allowed: {sorted(VALID_VIDEO_GRADES)}.",
                )
            fit = track.get("fit", "pad")
            if fit not in VALID_VIDEO_FITS:
                _add_err(
                    errors,
                    f"{t_loc}.fit",
                    f"Unsupported video fit {fit!r}. Allowed: {sorted(VALID_VIDEO_FITS)}.",
                )
            clips = track.get("clips")
            if not isinstance(clips, list) or len(clips) == 0:
                _add_err(errors, f"{t_loc}.clips", "Video track must contain a non-empty 'clips' list.")
                continue

            prev_end = 0.0
            for c_idx, clip in enumerate(clips):
                c_loc = f"{t_loc}.clips[{c_idx}]"
                if not isinstance(clip, dict):
                    _add_err(errors, c_loc, "Video clip must be an object.")
                    continue

                source = clip.get("source")
                if not isinstance(source, str) or not source.strip():
                    _add_err(errors, f"{c_loc}.source", "Video clip requires a non-empty 'source' path.")
                    continue

                start = clip.get("start")
                if not isinstance(start, (int, float)) or isinstance(start, bool) or start < 0:
                    _add_err(errors, f"{c_loc}.start", "Video clip 'start' must be a number >= 0.")
                    start_f = 0.0
                else:
                    start_f = float(start)

                if c_idx > 0 and start_f < prev_end - 1e-3:
                    _add_err(
                        errors,
                        f"{c_loc}.start",
                        f"Video clip starts at {start_f}s before previous clip ends at {prev_end:.3f}s.",
                    )

                in_pt = clip.get("in")
                out_pt = clip.get("out")
                duration = clip.get("duration")

                clip_dur: float | None = None
                if in_pt is not None or out_pt is not None:
                    if (
                        not isinstance(in_pt, (int, float))
                        or isinstance(in_pt, bool)
                        or not isinstance(out_pt, (int, float))
                        or isinstance(out_pt, bool)
                        or in_pt < 0
                        or out_pt <= in_pt
                    ):
                        _add_err(
                            errors,
                            c_loc,
                            f"Video clip 'in' ({in_pt!r}) and 'out' ({out_pt!r}) must satisfy 0 <= in < out.",
                        )
                    else:
                        clip_dur = float(out_pt) - float(in_pt)
                elif duration is not None:
                    if not isinstance(duration, (int, float)) or isinstance(duration, bool) or duration <= 0:
                        _add_err(errors, f"{c_loc}.duration", "Video clip 'duration' must be > 0.")
                    else:
                        clip_dur = float(duration)
                else:
                    _add_err(
                        errors,
                        c_loc,
                        "Video clip must specify either ('in' and 'out') or 'duration'.",
                    )

                if check_files:
                    resolved = resolve_source_path(source, root_dir)
                    if not resolved.exists():
                        _add_err(
                            errors,
                            f"{c_loc}.source",
                            f"Referenced video/image file not found: {resolved}",
                            code="FILE_NOT_FOUND",
                        )
                    else:
                        try:
                            info = probe_media(resolved)
                            if not info["has_video"]:
                                _add_err(
                                    errors,
                                    f"{c_loc}.source",
                                    f"File '{resolved.name}' has no video or image stream.",
                                )
                            elif (
                                not info["is_image"]
                                and info["duration"] is not None
                                and out_pt is not None
                                and isinstance(out_pt, (int, float))
                                and float(out_pt) > float(info["duration"]) + 0.15
                            ):
                                _add_err(
                                    errors,
                                    f"{c_loc}.out",
                                    f"Clip 'out' ({out_pt}s) exceeds source duration ({info['duration']}s) in '{resolved.name}'.",
                                )
                        except AgentCutError as exc:
                            _add_err(errors, f"{c_loc}.source", exc.message, code=exc.code)

                trans = clip.get("transition_in")
                if trans is not None:
                    if not isinstance(trans, dict):
                        _add_err(errors, f"{c_loc}.transition_in", "'transition_in' must be an object.")
                    else:
                        tr_type = trans.get("type")
                        tr_dur = trans.get("duration")
                        if tr_type not in VALID_TRANSITIONS:
                            _add_err(
                                errors,
                                f"{c_loc}.transition_in.type",
                                f"Invalid transition type {tr_type!r}. Allowed: {sorted(VALID_TRANSITIONS)}.",
                            )
                        if (
                            not isinstance(tr_dur, (int, float))
                            or isinstance(tr_dur, bool)
                            or tr_dur <= 0
                        ):
                            _add_err(
                                errors,
                                f"{c_loc}.transition_in.duration",
                                "'transition_in.duration' must be a number > 0.",
                            )
                        elif clip_dur is not None and float(tr_dur) >= clip_dur:
                            _add_err(
                                errors,
                                f"{c_loc}.transition_in.duration",
                                f"Transition duration ({tr_dur}s) must be smaller than clip duration ({clip_dur}s).",
                            )

                if clip_dur is not None:
                    prev_end = start_f + clip_dur
                    max_end_time = max(max_end_time, prev_end)

        elif t_type == "audio":
            role = track.get("role", "music")
            if role not in VALID_AUDIO_ROLES:
                _add_err(
                    errors,
                    f"{t_loc}.role",
                    f"Invalid audio role {role!r}. Allowed: {sorted(VALID_AUDIO_ROLES)}.",
                )
            clips = track.get("clips")
            if not isinstance(clips, list) or len(clips) == 0:
                _add_err(errors, f"{t_loc}.clips", "Audio track must contain a non-empty 'clips' list.")
                continue

            for c_idx, clip in enumerate(clips):
                c_loc = f"{t_loc}.clips[{c_idx}]"
                if not isinstance(clip, dict):
                    _add_err(errors, c_loc, "Audio clip must be an object.")
                    continue
                source = clip.get("source")
                if not isinstance(source, str) or not source.strip():
                    _add_err(errors, f"{c_loc}.source", "Audio clip requires a non-empty 'source' path.")
                    continue

                start = clip.get("start", 0)
                if not isinstance(start, (int, float)) or isinstance(start, bool) or start < 0:
                    _add_err(errors, f"{c_loc}.start", "Audio clip 'start' must be a number >= 0.")
                    start_f = 0.0
                else:
                    start_f = float(start)

                vol = clip.get("volume", 1.0)
                if not isinstance(vol, (int, float)) or isinstance(vol, bool) or vol < 0 or vol > 10.0:
                    _add_err(errors, f"{c_loc}.volume", "Audio clip 'volume' must be between 0.0 and 10.0.")

                for fade_key in ("fade_in", "fade_out"):
                    f_val = clip.get(fade_key, 0.0)
                    if not isinstance(f_val, (int, float)) or isinstance(f_val, bool) or f_val < 0:
                        _add_err(errors, f"{c_loc}.{fade_key}", f"'{fade_key}' must be a number >= 0.")

                duck = clip.get("duck_under_dialogue", False)
                if not isinstance(duck, bool):
                    _add_err(errors, f"{c_loc}.duck_under_dialogue", "'duck_under_dialogue' must be a boolean.")

                in_pt = clip.get("in", 0.0)
                out_pt = clip.get("out")
                duration = clip.get("duration")

                clip_dur: float | None = None
                if out_pt is not None:
                    if (
                        not isinstance(in_pt, (int, float))
                        or not isinstance(out_pt, (int, float))
                        or in_pt < 0
                        or out_pt <= in_pt
                    ):
                        _add_err(errors, c_loc, "Audio clip 'in' and 'out' must satisfy 0 <= in < out.")
                    else:
                        clip_dur = float(out_pt) - float(in_pt)
                elif duration is not None:
                    if not isinstance(duration, (int, float)) or isinstance(duration, bool) or duration <= 0:
                        _add_err(errors, f"{c_loc}.duration", "Audio clip 'duration' must be > 0.")
                    else:
                        clip_dur = float(duration)

                if check_files:
                    resolved = resolve_source_path(source, root_dir)
                    if not resolved.exists():
                        _add_err(
                            errors,
                            f"{c_loc}.source",
                            f"Referenced audio file not found: {resolved}",
                            code="FILE_NOT_FOUND",
                        )
                    else:
                        try:
                            info = probe_media(resolved)
                            if not info["has_audio"]:
                                _add_err(
                                    errors,
                                    f"{c_loc}.source",
                                    f"File '{resolved.name}' does not contain an audio stream.",
                                )
                            if clip_dur is None and info["duration"] is not None:
                                clip_dur = max(0.0, float(info["duration"]) - float(in_pt))
                        except AgentCutError as exc:
                            _add_err(errors, f"{c_loc}.source", exc.message, code=exc.code)

                if clip_dur is not None:
                    max_end_time = max(max_end_time, start_f + clip_dur)

        elif t_type == "captions":
            style = track.get("style", {})
            if not isinstance(style, dict):
                _add_err(errors, f"{t_loc}.style", "Captions 'style' must be an object.")
            else:
                fs = style.get("font_size", 64)
                if not isinstance(fs, (int, float)) or isinstance(fs, bool) or fs <= 8:
                    _add_err(errors, f"{t_loc}.style.font_size", "'font_size' must be a number > 8.")
                sw = style.get("stroke_width", 4)
                if not isinstance(sw, (int, float)) or isinstance(sw, bool) or sw < 0:
                    _add_err(errors, f"{t_loc}.style.stroke_width", "'stroke_width' must be >= 0.")
                pos = style.get("position", "bottom")
                if pos not in VALID_CAPTION_POSITIONS:
                    _add_err(
                        errors,
                        f"{t_loc}.style.position",
                        f"Invalid caption position {pos!r}. Allowed: {sorted(VALID_CAPTION_POSITIONS)}.",
                    )

            segments = track.get("segments")
            if not isinstance(segments, list) or len(segments) == 0:
                _add_err(errors, f"{t_loc}.segments", "Captions track requires a non-empty 'segments' list.")
                continue

            for s_idx, seg in enumerate(segments):
                s_loc = f"{t_loc}.segments[{s_idx}]"
                if not isinstance(seg, dict):
                    _add_err(errors, s_loc, "Caption segment must be an object.")
                    continue
                s_start = seg.get("start")
                s_end = seg.get("end")
                text = seg.get("text")
                if (
                    not isinstance(s_start, (int, float))
                    or isinstance(s_start, bool)
                    or not isinstance(s_end, (int, float))
                    or isinstance(s_end, bool)
                    or s_start < 0
                    or s_end <= s_start
                ):
                    _add_err(errors, s_loc, "Caption segment requires 0 <= start < end.")
                else:
                    max_end_time = max(max_end_time, float(s_end))
                if not isinstance(text, str) or not text.strip():
                    _add_err(errors, f"{s_loc}.text", "Caption segment 'text' must be a non-empty string.")
                if "highlight" in seg and not isinstance(seg["highlight"], bool):
                    _add_err(errors, f"{s_loc}.highlight", "'highlight' must be a boolean.")

        elif t_type == "overlay":
            clips = track.get("clips")
            if not isinstance(clips, list) or len(clips) == 0:
                _add_err(errors, f"{t_loc}.clips", "Overlay track requires a non-empty 'clips' list.")
                continue

            for c_idx, clip in enumerate(clips):
                c_loc = f"{t_loc}.clips[{c_idx}]"
                if not isinstance(clip, dict):
                    _add_err(errors, c_loc, "Overlay clip must be an object.")
                    continue
                source = clip.get("source")
                text = clip.get("text")
                if (not isinstance(source, str) or not source.strip()) and (
                    not isinstance(text, str) or not text.strip()
                ):
                    _add_err(
                        errors,
                        c_loc,
                        "Overlay clip must specify either an image 'source' or a 'text' string.",
                    )

                start = clip.get("start")
                duration = clip.get("duration")
                end = clip.get("end")
                if not isinstance(start, (int, float)) or isinstance(start, bool) or start < 0:
                    _add_err(errors, f"{c_loc}.start", "Overlay clip 'start' must be >= 0.")
                    start_f = 0.0
                else:
                    start_f = float(start)

                if duration is not None:
                    if not isinstance(duration, (int, float)) or isinstance(duration, bool) or duration <= 0:
                        _add_err(errors, f"{c_loc}.duration", "Overlay clip 'duration' must be > 0.")
                    else:
                        max_end_time = max(max_end_time, start_f + float(duration))
                elif end is not None:
                    if not isinstance(end, (int, float)) or isinstance(end, bool) or end <= start_f:
                        _add_err(errors, f"{c_loc}.end", "Overlay clip 'end' must be > 'start'.")
                    else:
                        max_end_time = max(max_end_time, float(end))
                else:
                    _add_err(errors, c_loc, "Overlay clip requires 'duration' or 'end'.")

                for coord in ("x", "y"):
                    val = clip.get(coord, 0.5)
                    if not isinstance(val, (int, float)) or isinstance(val, bool) or not (0.0 <= float(val) <= 1.0):
                        _add_err(
                            errors,
                            f"{c_loc}.{coord}",
                            f"Overlay '{coord}' must be relative between 0.0 and 1.0 (got {val!r}).",
                        )

                scale = clip.get("scale", 0.2)
                if not isinstance(scale, (int, float)) or isinstance(scale, bool) or not (0.01 <= float(scale) <= 2.0):
                    _add_err(errors, f"{c_loc}.scale", "Overlay 'scale' must be between 0.01 and 2.0.")

                anim = clip.get("animation", "none")
                if anim not in VALID_OVERLAY_ANIMATIONS:
                    _add_err(
                        errors,
                        f"{c_loc}.animation",
                        f"Invalid overlay animation {anim!r}. Allowed: {sorted(VALID_OVERLAY_ANIMATIONS)}.",
                    )

                if check_files and isinstance(source, str) and source.strip():
                    resolved = resolve_source_path(source, root_dir)
                    if not resolved.exists():
                        _add_err(
                            errors,
                            f"{c_loc}.source",
                            f"Referenced overlay file not found: {resolved}",
                            code="FILE_NOT_FOUND",
                        )
                    else:
                        try:
                            info = probe_media(resolved)
                            if not info["has_video"]:
                                _add_err(
                                    errors,
                                    f"{c_loc}.source",
                                    f"Overlay file '{resolved.name}' is not a valid image or video.",
                                )
                        except AgentCutError as exc:
                            _add_err(errors, f"{c_loc}.source", exc.message, code=exc.code)

    if video_track_count == 0:
        _add_err(errors, "tracks", "Timeline must contain at least one 'video' track.")

    return ValidationResult(
        ok=len(errors) == 0,
        errors=errors,
        warnings=warnings,
        estimated_duration=max_end_time,
        timeline_path=timeline_path,
    )
