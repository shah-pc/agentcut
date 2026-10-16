"""FFmpeg filtergraph builder, video/overlay/caption compositor, and post-render safety gates."""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable

from .audio import render_mixed_audio, verify_stereo_signal
from .captions import generate_ass_file
from .probe import AgentCutError, ensure_ffmpeg_installed, probe_media
from .schema import load_timeline_input, resolve_source_path, validate_timeline


def _escape_filter_path(path: Path) -> str:
    """Escape a filesystem path for safe embedding inside an FFmpeg filter argument."""
    s = path.as_posix()
    for ch in ("\\", ":", "'", ",", "[", "]", ";"):
        s = s.replace(ch, f"\\{ch}")
    return s


def _clip_duration(clip: dict[str, Any]) -> float:
    """Return the playback duration of a video or overlay clip in seconds."""
    if "in" in clip and "out" in clip and clip["in"] is not None and clip["out"] is not None:
        return float(clip["out"]) - float(clip["in"])
    if "duration" in clip and clip["duration"] is not None:
        return float(clip["duration"])
    if "end" in clip and clip["end"] is not None:
        return float(clip["end"]) - float(clip.get("start", 0.0))
    raise AgentCutError("Clip is missing duration or in/out bounds.", code="INVALID_CLIP_DURATION")


def _grade_filter(grade: str) -> str:
    """Return FFmpeg filter string for a requested color grade."""
    g = grade.lower()
    if g in ("bw", "grayscale"):
        return ",hue=s=0"
    if g == "sepia":
        return ",colorchannelmixer=.393:.769:.189:0:.349:.686:.168:0:.272:.534:.131"
    if g == "high_contrast":
        return ",eq=contrast=1.25:saturation=1.15"
    return ""


def render_timeline(
    timeline_input: str | Path | dict[str, Any],
    base_dir: Path | None = None,
    output_override: str | Path | None = None,
    progress_callback: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Validate and render an AgentCut timeline into an H.264 + AAC MP4 file.

    Executes all mandatory post-render safety gates:
      - Verifies output file exists and probes cleanly.
      - Verifies rendered duration matches the timeline duration within 0.5s.
      - Verifies stereo audio output where BOTH Left and Right channels carry active signal.

    Args:
        timeline_input: Path to a timeline `.json` file or a parsed timeline dictionary.
        base_dir: Optional directory for resolving relative paths when input is a dict.
        output_override: Optional override for `output.path`.
        progress_callback: Optional callback receiving human-readable progress messages.

    Returns:
        Structured dictionary with `output_path`, `duration`, `target_duration`,
        `lufs`, `true_peak_dbtp`, `size_bytes`, and `safety_gates`.
    """
    def _log(msg: str) -> None:
        if progress_callback is not None:
            progress_callback(msg)
        else:
            print(f"[agentcut] {msg}", file=sys.stderr, flush=True)

    ensure_ffmpeg_installed()
    data, inferred_base_dir, _ = load_timeline_input(timeline_input)
    root_dir = (base_dir or inferred_base_dir).resolve()

    _log("Step 1/5: Validating timeline schema and media assets...")
    validation = validate_timeline(data, base_dir=root_dir, check_files=True)
    if not validation.ok:
        first_err = validation.errors[0]["message"] if validation.errors else "Invalid timeline"
        raise AgentCutError(
            f"Timeline validation failed ({len(validation.errors)} error(s)): {first_err}",
            code="VALIDATION_FAILED",
            details=validation.to_dict(),
        )

    canvas = data["canvas"]
    width = int(canvas["width"])
    height = int(canvas["height"])
    fps = int(round(float(canvas["fps"])))
    bg_color = str(canvas.get("background", "black"))

    output_cfg = data["output"]
    raw_out_path = str(output_override or output_cfg["path"])
    out_path = resolve_source_path(raw_out_path, root_dir)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    target_lufs = float(output_cfg.get("loudnorm_lufs", -14.0))

    tracks: list[dict[str, Any]] = data["tracks"]
    video_tracks = [t for t in tracks if t.get("type") == "video"]
    audio_tracks = [t for t in tracks if t.get("type") == "audio"]
    caption_tracks = [t for t in tracks if t.get("type") == "captions"]
    overlay_tracks = [t for t in tracks if t.get("type") == "overlay"]

    # Compute exact video timeline duration from primary video track(s)
    video_end = 0.0
    for vt in video_tracks:
        for clip in vt.get("clips", []):
            video_end = max(video_end, float(clip["start"]) + _clip_duration(clip))
    target_duration = video_end if video_end > 0 else validation.estimated_duration

    with tempfile.TemporaryDirectory(prefix="agentcut_") as tmp_str:
        temp_dir = Path(tmp_str)

        # Step 2: Audio mixing, dialogue ducking, and 2-pass EBU R128 loudnorm
        _log(f"Step 2/5: Mixing audio tracks and normalizing loudness to {target_lufs:.1f} LUFS...")
        mixed_audio_wav = render_mixed_audio(
            audio_tracks=audio_tracks,
            total_duration=target_duration,
            target_lufs=target_lufs,
            base_dir=root_dir,
            temp_dir=temp_dir,
        )

        # Step 3: Captions ASS generation
        ass_path: Path | None = None
        if caption_tracks:
            _log("Step 3/5: Generating ASS subtitles for burned-in captions...")
            ass_path = generate_ass_file(
                caption_tracks=caption_tracks,
                canvas_width=width,
                canvas_height=height,
                output_ass_path=temp_dir / "captions.ass",
            )
        else:
            _log("Step 3/5: No caption tracks present; skipping ASS generation.")

        # Step 4: Build FFmpeg video + transition + overlay + caption filtergraph
        _log("Step 4/5: Building FFmpeg filtergraph and rendering H.264/AAC MP4...")
        inputs_cmd: list[str] = []
        filter_parts: list[str] = []
        input_idx = 0

        # Render primary video track (and any additional video tracks layered on top)
        track_labels: list[str] = []
        for vt_idx, vtrack in enumerate(video_tracks):
            clips = sorted(vtrack["clips"], key=lambda c: float(c["start"]))
            track_grade = str(vtrack.get("grade", "none"))
            track_fit = str(vtrack.get("fit", "pad"))

            clip_labels: list[str] = []
            clip_durations: list[float] = []
            clip_starts: list[float] = []
            clip_transitions: list[dict[str, Any] | None] = []

            for c_idx, clip in enumerate(clips):
                src_path = resolve_source_path(str(clip["source"]), root_dir)
                info = probe_media(src_path)
                c_dur = _clip_duration(clip)
                c_start = float(clip["start"])
                in_pt = float(clip.get("in", 0.0))

                if info["is_image"]:
                    inputs_cmd.extend(["-loop", "1", "-t", f"{c_dur:.4f}", "-i", str(src_path)])
                else:
                    inputs_cmd.extend(["-ss", f"{in_pt:.4f}", "-t", f"{c_dur:.4f}", "-i", str(src_path)])

                cur_in = input_idx
                input_idx += 1

                fit_mode = str(clip.get("fit", track_fit)).lower()
                if fit_mode in ("crop", "cover"):
                    scale_chain = (
                        f"scale=w={width}:h={height}:force_original_aspect_ratio=increase,"
                        f"crop={width}:{height}"
                    )
                else:
                    scale_chain = (
                        f"scale=w={width}:h={height}:force_original_aspect_ratio=decrease,"
                        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color={bg_color}"
                    )

                clip_grade = str(clip.get("grade", track_grade))
                grade_str = _grade_filter(clip_grade)

                trans = clip.get("transition_in")
                first_fade = ""
                if c_idx == 0 and isinstance(trans, dict):
                    tr_d = float(trans["duration"])
                    first_fade = f",fade=t=in:st=0:d={tr_d:.4f}"

                c_lbl = f"[vt{vt_idx}_c{c_idx}]"
                filter_parts.append(
                    f"[{cur_in}:v]{scale_chain},setsar=1,fps={fps},format=yuv420p,"
                    f"settb=1/{fps},setpts=PTS-STARTPTS{grade_str}{first_fade}{c_lbl}"
                )
                clip_labels.append(c_lbl)
                clip_durations.append(c_dur)
                clip_starts.append(c_start)
                clip_transitions.append(trans if c_idx > 0 and isinstance(trans, dict) else None)

            # Combine clips on this video track sequentially using xfade or concat
            acc_lbl = clip_labels[0]
            acc_dur = clip_durations[0]

            # If the first clip starts after t=0, prepend a background color pad
            if clip_starts[0] > 1e-3:
                pre_dur = clip_starts[0]
                pre_lbl = f"[vt{vt_idx}_pre]"
                joined_pre = f"[vt{vt_idx}_init]"
                filter_parts.append(
                    f"color=c={bg_color}:s={width}x{height}:r={fps}:d={pre_dur:.4f},"
                    f"format=yuv420p,settb=1/{fps}{pre_lbl}"
                )
                filter_parts.append(f"{pre_lbl}{acc_lbl}concat=n=2:v=1:a=0{joined_pre}")
                acc_lbl = joined_pre
                acc_dur += pre_dur

            for c_idx in range(1, len(clip_labels)):
                next_lbl = clip_labels[c_idx]
                next_dur = clip_durations[c_idx]
                next_start = clip_starts[c_idx]
                trans = clip_transitions[c_idx]

                # Fill any explicit gap between clips with canvas background
                gap = next_start - acc_dur
                if gap > 0.02:
                    gap_lbl = f"[vt{vt_idx}_gap{c_idx}]"
                    gap_join = f"[vt{vt_idx}_gseq{c_idx}]"
                    filter_parts.append(
                        f"color=c={bg_color}:s={width}x{height}:r={fps}:d={gap:.4f},"
                        f"format=yuv420p,settb=1/{fps}{gap_lbl}"
                    )
                    filter_parts.append(f"{acc_lbl}{gap_lbl}concat=n=2:v=1:a=0{gap_join}")
                    acc_lbl = gap_join
                    acc_dur += gap

                out_seq_lbl = f"[vt{vt_idx}_seq{c_idx}]"
                if trans is not None:
                    tr_type = str(trans["type"])
                    tr_dur = float(trans["duration"])
                    pad_lbl = f"[vt{vt_idx}_pad{c_idx}]"
                    # Extend outgoing stream by tr_dur via tpad so xfade preserves exact timeline sync
                    filter_parts.append(
                        f"{acc_lbl}tpad=stop_mode=clone:stop_duration={tr_dur:.4f},"
                        f"settb=1/{fps},fps={fps}{pad_lbl}"
                    )
                    filter_parts.append(
                        f"{pad_lbl}{next_lbl}xfade=transition={tr_type}:duration={tr_dur:.4f}:"
                        f"offset={acc_dur:.4f},settb=1/{fps},fps={fps},format=yuv420p{out_seq_lbl}"
                    )
                else:
                    filter_parts.append(
                        f"{acc_lbl}{next_lbl}concat=n=2:v=1:a=0,settb=1/{fps},fps={fps},format=yuv420p{out_seq_lbl}"
                    )

                acc_lbl = out_seq_lbl
                acc_dur += next_dur

            # If video track ends before target_duration, pad tail with canvas background
            if acc_dur < target_duration - 0.02:
                tail_dur = target_duration - acc_dur
                tail_lbl = f"[vt{vt_idx}_tail]"
                full_lbl = f"[vt{vt_idx}_full]"
                filter_parts.append(
                    f"color=c={bg_color}:s={width}x{height}:r={fps}:d={tail_dur:.4f},"
                    f"format=yuv420p,settb=1/{fps}{tail_lbl}"
                )
                filter_parts.append(f"{acc_lbl}{tail_lbl}concat=n=2:v=1:a=0{full_lbl}")
                acc_lbl = full_lbl

            track_labels.append(acc_lbl)

        current_video_lbl = track_labels[0]
        for extra_idx, extra_lbl in enumerate(track_labels[1:], start=1):
            comp_lbl = f"[vtrack_comp{extra_idx}]"
            filter_parts.append(
                f"{current_video_lbl}{extra_lbl}overlay=x=0:y=0:format=auto{comp_lbl}"
            )
            current_video_lbl = comp_lbl

        # Apply overlay tracks (images with alpha and/or styled text overlays)
        ov_counter = 0
        for otrack in overlay_tracks:
            for oclip in otrack.get("clips", []):
                ov_counter += 1
                o_start = float(oclip["start"])
                o_dur = _clip_duration(oclip)
                o_end = o_start + o_dur
                rel_x = float(oclip.get("x", 0.5))
                rel_y = float(oclip.get("y", 0.5))
                scale = float(oclip.get("scale", 0.2))
                anim = str(oclip.get("animation", "none")).lower()

                cx = width * rel_x
                cy = height * rel_y
                target_w = max(4, int(round(width * scale)))
                bounce_amp = max(8.0, height * 0.025)
                fade_d = min(0.30, o_dur / 3.0)
                pop_d = min(0.22, o_dur * 0.45)

                source_val = oclip.get("source")
                text_val = oclip.get("text")

                if isinstance(source_val, str) and source_val.strip():
                    ov_path = resolve_source_path(source_val, root_dir)
                    ov_info = probe_media(ov_path)
                    if ov_info["is_image"]:
                        inputs_cmd.extend(
                            ["-loop", "1", "-t", f"{target_duration:.4f}", "-i", str(ov_path)]
                        )
                    else:
                        inputs_cmd.extend(["-i", str(ov_path)])
                    ov_in = input_idx
                    input_idx += 1

                    ov_prep = f"[ov_prep{ov_counter}]"
                    ov_out = f"[v_ov{ov_counter}]"

                    if anim == "pop":
                        scale_expr = (
                            f"scale=w='max(4,{target_w}*(0.35+0.65*min(1,max(0,(t-{o_start:.4f})/{pop_d:.4f}))))':"
                            f"h='-1':eval=frame"
                        )
                    else:
                        scale_expr = f"scale=w={target_w}:h=-1"

                    fade_chain = ""
                    if anim == "fade":
                        fade_chain = (
                            f",fade=t=in:st={o_start:.4f}:d={fade_d:.4f}:alpha=1,"
                            f"fade=t=out:st={max(o_start, o_end - fade_d):.4f}:d={fade_d:.4f}:alpha=1"
                        )

                    filter_parts.append(f"[{ov_in}:v]format=rgba,{scale_expr}{fade_chain}{ov_prep}")

                    if anim == "bounce":
                        y_expr = f"{cy:.1f}-h/2-abs(sin((t-{o_start:.4f})*7.5))*{bounce_amp:.1f}"
                    else:
                        y_expr = f"{cy:.1f}-h/2"

                    filter_parts.append(
                        f"{current_video_lbl}{ov_prep}overlay=x='{cx:.1f}-w/2':y='{y_expr}':"
                        f"enable='between(t,{o_start:.4f},{o_end:.4f})':format=auto{ov_out}"
                    )
                    current_video_lbl = ov_out

                if isinstance(text_val, str) and text_val.strip():
                    txt_file = temp_dir / f"overlay_text_{ov_counter}.txt"
                    txt_file.write_text(text_val.strip(), encoding="utf-8")
                    esc_txt_path = _escape_filter_path(txt_file)

                    font_size = int(oclip.get("font_size", max(24, round(target_w * 0.28))))
                    font_color = str(oclip.get("color", "white"))
                    stroke_color = str(oclip.get("stroke_color", "black"))
                    stroke_w = int(oclip.get("stroke_width", 3))

                    if anim == "bounce":
                        y_txt_expr = f"{cy:.1f}-text_h/2-abs(sin((t-{o_start:.4f})*7.5))*{bounce_amp:.1f}"
                    else:
                        y_txt_expr = f"{cy:.1f}-text_h/2"

                    alpha_expr = "1.0"
                    if anim == "fade":
                        alpha_expr = (
                            f"min(1,min((t-{o_start:.4f})/{fade_d:.4f},({o_end:.4f}-t)/{fade_d:.4f}))"
                        )

                    ov_txt_out = f"[v_ovtxt{ov_counter}]"
                    filter_parts.append(
                        f"{current_video_lbl}drawtext=textfile='{esc_txt_path}':"
                        f"fontsize={font_size}:fontcolor={font_color}:borderw={stroke_w}:"
                        f"bordercolor={stroke_color}:alpha='{alpha_expr}':"
                        f"x='{cx:.1f}-text_w/2':y='{y_txt_expr}':"
                        f"enable='between(t,{o_start:.4f},{o_end:.4f})'{ov_txt_out}"
                    )
                    current_video_lbl = ov_txt_out

        # Burn in ASS captions if present
        if ass_path is not None:
            esc_ass = _escape_filter_path(ass_path)
            cap_out = "[v_cap]"
            filter_parts.append(f"{current_video_lbl}ass=filename='{esc_ass}'{cap_out}")
            current_video_lbl = cap_out

        # Final pixel format normalization
        final_v_lbl = "[vfinal]"
        filter_parts.append(f"{current_video_lbl}format=yuv420p{final_v_lbl}")

        # Add the normalized stereo audio WAV as the final input
        audio_input_idx = input_idx
        inputs_cmd.extend(["-i", str(mixed_audio_wav)])

        render_cmd = [
            "ffmpeg",
            "-y",
            "-hide_banner",
            *inputs_cmd,
            "-filter_complex",
            ";".join(filter_parts),
            "-map",
            final_v_lbl,
            "-map",
            f"{audio_input_idx}:a:0",
            "-t",
            f"{target_duration:.4f}",
            "-c:v",
            "libx264",
            "-preset",
            "fast",
            "-crf",
            "20",
            "-pix_fmt",
            "yuv420p",
            "-r",
            str(fps),
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-movflags",
            "+faststart",
            str(out_path),
        ]

        proc = subprocess.run(render_cmd, capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            raise AgentCutError(
                f"FFmpeg video render failed: {proc.stderr.strip()[-800:]}",
                code="RENDER_FAILED",
                details={"stderr": proc.stderr.strip()[-1200:]},
            )

    # Step 5: Mandatory post-render safety gates
    _log("Step 5/5: Running post-render safety gates (duration, stereo channels, LUFS)...")
    out_probe = probe_media(out_path)
    actual_duration = float(out_probe["duration"] or 0.0)
    duration_diff = abs(actual_duration - target_duration)
    if duration_diff > 0.5:
        raise AgentCutError(
            f"Safety gate failed: Rendered duration ({actual_duration:.3f}s) deviates from "
            f"timeline duration ({target_duration:.3f}s) by {duration_diff:.3f}s (max allowed: 0.5s).",
            code="DURATION_MISMATCH_GATE_FAILED",
            details={
                "actual_duration": actual_duration,
                "target_duration": target_duration,
                "difference_seconds": round(duration_diff, 4),
            },
        )

    audio_gate = verify_stereo_signal(out_path, target_lufs=target_lufs)
    _log(f"Render complete: {out_path} ({actual_duration:.2f}s, {audio_gate['integrated_lufs']} LUFS)")

    return {
        "ok": True,
        "output_path": str(out_path),
        "duration": round(actual_duration, 3),
        "target_duration": round(target_duration, 3),
        "lufs": audio_gate["integrated_lufs"],
        "target_lufs": target_lufs,
        "true_peak_dbtp": audio_gate["true_peak_dbtp"],
        "size_bytes": out_probe["size_bytes"],
        "canvas": {"width": width, "height": height, "fps": fps},
        "safety_gates": {
            "duration_within_0_5s": True,
            "duration_diff_seconds": round(duration_diff, 4),
            "stereo_both_channels_active": True,
            "channel_peaks_db": audio_gate["channel_peaks_db"],
            "channel_rms_db": audio_gate["channel_rms_db"],
        },
    }
