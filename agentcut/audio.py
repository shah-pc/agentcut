"""Audio mixing, dialogue ducking, EBU R128 loudness normalization, and channel verification."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

from .probe import AgentCutError, probe_media
from .schema import resolve_source_path


def merge_intervals(intervals: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Merge overlapping or adjacent `(start, end)` time intervals."""
    if not intervals:
        return []
    sorted_iv = sorted(intervals, key=lambda x: x[0])
    merged: list[tuple[float, float]] = [sorted_iv[0]]
    for cur_start, cur_end in sorted_iv[1:]:
        prev_start, prev_end = merged[-1]
        if cur_start <= prev_end + 0.05:
            merged[-1] = (prev_start, max(prev_end, cur_end))
        else:
            merged.append((cur_start, cur_end))
    return merged


def build_ducking_volume_expr(
    base_volume: float,
    clip_timeline_start: float,
    dialogue_intervals: list[tuple[float, float]],
    duck_ratio: float = 0.28,
    attack: float = 0.20,
    release: float = 0.30,
) -> str:
    """Build a deterministic FFmpeg `volume` expression that ducks music under dialogue.

    Because the expression is evaluated after `atrim`/`asetpts` (where `t` is clip-local time
    starting at 0), each dialogue window `[d_start, d_end]` on the timeline is shifted by
    `-clip_timeline_start` into clip-local coordinates.

    During each dialogue interval `[s, e]`, the multiplier smoothly ramps down from 1.0 to
    `duck_ratio` over `attack` seconds, holds at `duck_ratio`, and ramps back to 1.0 over
    `release` seconds.
    """
    if not dialogue_intervals:
        return f"{base_volume:.4f}"

    terms: list[str] = []
    for d_start, d_end in dialogue_intervals:
        s = round(d_start - clip_timeline_start, 4)
        e = round(d_end - clip_timeline_start, 4)
        if e <= 0:
            continue
        ramp_up = f"((t-({s - attack:.4f}))/{attack:.4f})"
        ramp_down = f"((({e + release:.4f})-t)/{release:.4f})"
        window = f"max(0,min(1,min({ramp_up},{ramp_down})))"
        terms.append(window)

    if not terms:
        return f"{base_volume:.4f}"

    combined_window = terms[0]
    for term in terms[1:]:
        combined_window = f"max({combined_window},{term})"

    drop = round(1.0 - duck_ratio, 4)
    return f"{base_volume:.4f}*(1.0-{drop}*{combined_window})"


def render_mixed_audio(
    audio_tracks: list[dict[str, Any]],
    total_duration: float,
    target_lufs: float,
    base_dir: Path,
    temp_dir: Path,
) -> Path:
    """Mix all timeline audio tracks, apply ducking and fades, and normalize to target LUFS.

    Uses a deterministic 2-stage pipeline:
      1. Mix all positioned stereo clips onto a stereo base bed of `total_duration`.
      2. Measure EBU R128 integrated loudness and apply 2-pass `loudnorm` (with linear gain
         fallback for synthetic/pure-tone signals) to hit `target_lufs`.

    Returns:
        Path to the normalized stereo WAV file in `temp_dir`.
    """
    temp_dir.mkdir(parents=True, exist_ok=True)
    raw_mix_wav = temp_dir / "audio_mix_raw.wav"
    norm_wav = temp_dir / "audio_mix_norm.wav"

    dialogue_intervals: list[tuple[float, float]] = []
    resolved_clips: list[dict[str, Any]] = []

    for track in audio_tracks:
        role = str(track.get("role", "music"))
        track_duck = bool(track.get("duck_under_dialogue", False))
        for clip in track.get("clips", []):
            src_path = resolve_source_path(str(clip["source"]), base_dir)
            info = probe_media(src_path)
            src_dur = float(info["duration"] or total_duration)

            in_pt = float(clip.get("in", 0.0))
            if "out" in clip and clip["out"] is not None:
                out_pt = float(clip["out"])
            elif "duration" in clip and clip["duration"] is not None:
                out_pt = in_pt + float(clip["duration"])
            else:
                out_pt = src_dur

            clip_dur = max(0.01, min(out_pt - in_pt, total_duration - float(clip.get("start", 0.0))))
            start_t = float(clip.get("start", 0.0))
            end_t = start_t + clip_dur

            if role == "dialogue":
                dialogue_intervals.append((start_t, end_t))

            resolved_clips.append(
                {
                    "path": src_path,
                    "start": start_t,
                    "in": in_pt,
                    "out": in_pt + clip_dur,
                    "duration": clip_dur,
                    "volume": float(clip.get("volume", 1.0)),
                    "fade_in": float(clip.get("fade_in", 0.0)),
                    "fade_out": float(clip.get("fade_out", 0.0)),
                    "duck": bool(clip.get("duck_under_dialogue", track_duck)) and role != "dialogue",
                }
            )

    merged_dialogue = merge_intervals(dialogue_intervals)

    inputs_cmd: list[str] = [
        "-f",
        "lavfi",
        "-i",
        f"anullsrc=r=48000:cl=stereo:d={total_duration:.4f}",
    ]
    filter_parts: list[str] = [
        "[0:a]aformat=sample_rates=48000:channel_layouts=stereo[bed]"
    ]
    mix_labels: list[str] = ["[bed]"]

    for idx, rc in enumerate(resolved_clips, start=1):
        inputs_cmd.extend(["-i", str(rc["path"])])
        chain: list[str] = [
            f"atrim=start={rc['in']:.4f}:end={rc['out']:.4f}",
            "asetpts=PTS-STARTPTS",
            "aformat=sample_rates=48000:channel_layouts=stereo",
        ]

        if rc["duck"] and merged_dialogue:
            vol_expr = build_ducking_volume_expr(
                base_volume=rc["volume"],
                clip_timeline_start=rc["start"],
                dialogue_intervals=merged_dialogue,
            )
            chain.append(f"volume='{vol_expr}':eval=frame")
        else:
            chain.append(f"volume={rc['volume']:.4f}")

        if rc["fade_in"] > 0:
            fi_d = min(rc["fade_in"], rc["duration"])
            chain.append(f"afade=t=in:st=0:d={fi_d:.4f}")
        if rc["fade_out"] > 0:
            fo_d = min(rc["fade_out"], rc["duration"])
            fo_st = max(0.0, rc["duration"] - fo_d)
            chain.append(f"afade=t=out:st={fo_st:.4f}:d={fo_d:.4f}")

        delay_ms = max(0, int(round(rc["start"] * 1000)))
        if delay_ms > 0:
            chain.append(f"adelay={delay_ms}|{delay_ms}")

        chain.append(f"apad=whole_dur={total_duration:.4f}")
        chain.append(f"atrim=0:{total_duration:.4f}")

        lbl = f"[a{idx}]"
        filter_parts.append(f"[{idx}:a]{','.join(chain)}{lbl}")
        mix_labels.append(lbl)

    num_inputs = len(mix_labels)
    filter_parts.append(
        f"{''.join(mix_labels)}amix=inputs={num_inputs}:duration=first:dropout_transition=0:normalize=0,"
        f"aformat=sample_rates=48000:channel_layouts=stereo[aout]"
    )

    mix_cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        *inputs_cmd,
        "-filter_complex",
        ";".join(filter_parts),
        "-map",
        "[aout]",
        "-t",
        f"{total_duration:.4f}",
        "-c:a",
        "pcm_s16le",
        "-ar",
        "48000",
        "-ac",
        "2",
        str(raw_mix_wav),
    ]
    proc = subprocess.run(mix_cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise AgentCutError(
            f"FFmpeg audio mix failed: {proc.stderr.strip()[-600:]}",
            code="AUDIO_MIX_FAILED",
            details={"stderr": proc.stderr.strip()[-1000:]},
        )

    measure_stats = measure_audio_stats(raw_mix_wav, target_lufs=target_lufs)
    input_i = measure_stats["integrated_lufs"]
    input_tp = measure_stats["true_peak_dbtp"]
    input_lra = measure_stats["lra_lu"]
    input_thresh = measure_stats["threshold_lufs"]
    target_offset = measure_stats["target_offset"]

    if resolved_clips and input_i > -70.0:
        loudnorm_filter = (
            f"loudnorm=I={target_lufs:.1f}:TP=-1.5:LRA=11:"
            f"measured_I={input_i:.2f}:measured_TP={input_tp:.2f}:"
            f"measured_LRA={input_lra:.2f}:measured_thresh={input_thresh:.2f}:"
            f"offset={target_offset:.2f}:linear=true,"
            "aformat=sample_rates=48000:channel_layouts=stereo"
        )
        norm_cmd = [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-i",
            str(raw_mix_wav),
            "-af",
            loudnorm_filter,
            "-t",
            f"{total_duration:.4f}",
            "-c:a",
            "pcm_s16le",
            "-ar",
            "48000",
            "-ac",
            "2",
            str(norm_wav),
        ]
        norm_proc = subprocess.run(norm_cmd, capture_output=True, text=True, check=False)
        if norm_proc.returncode != 0:
            raise AgentCutError(
                f"FFmpeg loudnorm pass failed: {norm_proc.stderr.strip()[-600:]}",
                code="LOUDNORM_FAILED",
                details={"stderr": norm_proc.stderr.strip()[-1000:]},
            )
        return norm_wav

    return raw_mix_wav


def measure_audio_stats(media_path: Path, target_lufs: float = -14.0) -> dict[str, Any]:
    """Measure per-channel peak/RMS levels and EBU R128 loudness metrics using FFmpeg."""
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-i",
        str(media_path),
        "-af",
        (
            "astats=measure_perchannel=Peak_level+RMS_level:measure_overall=0,"
            f"loudnorm=I={target_lufs:.1f}:TP=-1.5:LRA=11:print_format=json"
        ),
        "-f",
        "null",
        "-",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    stderr = proc.stderr

    channel_peaks: dict[int, float] = {}
    channel_rms: dict[int, float] = {}
    current_ch: int | None = None
    for line in stderr.splitlines():
        ch_match = re.search(r"Channel:\s*(\d+)", line)
        if ch_match:
            current_ch = int(ch_match.group(1))
            continue
        if current_ch is not None:
            pk_match = re.search(r"Peak level dB:\s*([-\d.infINF]+)", line)
            if pk_match:
                val_str = pk_match.group(1).lower()
                channel_peaks[current_ch] = -120.0 if "inf" in val_str else float(val_str)
            rms_match = re.search(r"RMS level dB:\s*([-\d.infINF]+)", line)
            if rms_match:
                val_str = rms_match.group(1).lower()
                channel_rms[current_ch] = -120.0 if "inf" in val_str else float(val_str)

    json_match = re.search(r"\{\s*\"input_i\"\s*:.*?\}", stderr, re.DOTALL)
    integrated_lufs = -99.0
    true_peak = -99.0
    lra = 0.0
    thresh = -99.0
    offset = 0.0
    if json_match:
        try:
            ln_data = json.loads(json_match.group(0))

            def _safe_float(v: Any, default: float = -99.0) -> float:
                s = str(v).lower()
                return default if "inf" in s or "nan" in s else float(s)

            integrated_lufs = _safe_float(ln_data.get("input_i"), -99.0)
            true_peak = _safe_float(ln_data.get("input_tp"), -99.0)
            lra = _safe_float(ln_data.get("input_lra"), 0.0)
            thresh = _safe_float(ln_data.get("input_thresh"), -99.0)
            offset = _safe_float(ln_data.get("target_offset"), 0.0)
        except (ValueError, json.JSONDecodeError):
            pass

    return {
        "channel_peaks_db": channel_peaks,
        "channel_rms_db": channel_rms,
        "integrated_lufs": round(integrated_lufs, 2),
        "true_peak_dbtp": round(true_peak, 2),
        "lra_lu": round(lra, 2),
        "threshold_lufs": round(thresh, 2),
        "target_offset": round(offset, 2),
    }


def verify_stereo_signal(media_path: Path, target_lufs: float = -14.0) -> dict[str, Any]:
    """Verify that a rendered media file has 2 stereo channels and BOTH carry active signal.

    Raises:
        AgentCutError: If either audio channel is silent or missing.
    """
    info = probe_media(media_path)
    if not info["has_audio"] or not info["audio"]:
        raise AgentCutError(
            f"Safety gate failed: Output '{media_path.name}' has no audio stream.",
            code="SILENT_AUDIO_GATE_FAILED",
            details={"path": str(media_path)},
        )

    channels = info["audio"].get("channels")
    if channels != 2:
        raise AgentCutError(
            f"Safety gate failed: Expected stereo (2 channels) output, got {channels} channel(s).",
            code="STEREO_CHANNEL_COUNT_ERROR",
            details={"path": str(media_path), "channels": channels},
        )

    stats = measure_audio_stats(media_path, target_lufs=target_lufs)
    peaks = stats["channel_peaks_db"]
    rms = stats["channel_rms_db"]

    silent_channels: list[int] = []
    for ch in (1, 2):
        ch_peak = peaks.get(ch, -120.0)
        ch_rms = rms.get(ch, -120.0)
        if ch_peak <= -65.0 or ch_rms <= -75.0:
            silent_channels.append(ch)

    if silent_channels:
        raise AgentCutError(
            f"Safety gate failed: Audio channel(s) {silent_channels} are silent in '{media_path.name}' "
            f"(peaks dB: {peaks}, RMS dB: {rms}).",
            code="SILENT_AUDIO_CHANNEL",
            details={
                "path": str(media_path),
                "silent_channels": silent_channels,
                "channel_peaks_db": peaks,
                "channel_rms_db": rms,
            },
        )

    return stats
