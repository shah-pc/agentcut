#!/usr/bin/env python3
"""Self-contained AgentCut demo & quality-gate verification script.

Generates synthetic test media using FFmpeg `lavfi` (zero external assets required)
next to `demo_timeline.json`, renders the 12-second `examples/demo_timeline.json`,
verifies all quality gates (duration within 0.5s, stereo channels active, EBU R128
loudness), and prints a PASS/FAIL summary.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agentcut.audio import verify_stereo_signal
from agentcut.probe import AgentCutError, ensure_ffmpeg_installed, probe_media
from agentcut.render import render_timeline
from agentcut.schema import validate_timeline


def _run_ffmpeg_gen(args: list[str]) -> None:
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *args]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"Failed generating synthetic media: {proc.stderr.strip()}")


def generate_synthetic_media(examples_dir: Path) -> None:
    """Generate deterministic synthetic video, audio, and transparent PNG assets next to demo_timeline.json."""
    examples_dir.mkdir(parents=True, exist_ok=True)

    # 1. Two video clips (6s each) with distinct colors and moving boxes
    scenes = [
        ("scene1.mp4", "0x1E3A8A", "SCENE 1"),
        ("scene2.mp4", "0x065F46", "SCENE 2"),
    ]
    for filename, hex_color, label in scenes:
        _run_ffmpeg_gen(
            [
                "-f",
                "lavfi",
                "-i",
                f"color=c={hex_color}:s=1080x1920:r=30:d=6.0",
                "-vf",
                (
                    f"drawbox=x='mod(t*180,880)':y=420:w=200:h=200:color=white@0.25:t=fill,"
                    f"drawtext=text='{label}':fontsize=96:fontcolor=white:x=(w-text_w)/2:y=280"
                ),
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                str(examples_dir / filename),
            ]
        )

    # 2. Still image end card (1080x1920 PNG)
    _run_ffmpeg_gen(
        [
            "-f",
            "lavfi",
            "-i",
            "color=c=0x111827:s=1080x1920:d=1",
            "-vf",
            "drawbox=x=90:y=300:w=900:h=1320:color=0x3B82F6@0.35:t=12",
            "-frames:v",
            "1",
            str(examples_dir / "end_card.png"),
        ]
    )

    # 3. Transparent PNG overlay badge (256x256 RGBA PNG)
    _run_ffmpeg_gen(
        [
            "-f",
            "lavfi",
            "-i",
            "color=c=black@0.0:s=256x256:d=1,format=rgba",
            "-vf",
            (
                "drawbox=x=16:y=16:w=224:h=224:color=0xF59E0B@0.95:t=fill,"
                "drawbox=x=36:y=36:w=184:h=184:color=0x1F2937@0.9:t=10,"
                "drawtext=text='AI':fontsize=96:fontcolor=white:x=(w-text_w)/2:y=(h-text_h)/2"
            ),
            "-frames:v",
            "1",
            str(examples_dir / "emoji.png"),
        ]
    )

    # 4. Dialogue voiceover clips (stereo MP3s)
    _run_ffmpeg_gen(
        [
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=520:sample_rate=48000:duration=3.0",
            "-af",
            "aformat=channel_layouts=stereo,volume=0.6,afade=t=in:st=0:d=0.05,afade=t=out:st=2.8:d=0.2",
            "-c:a",
            "libmp3lame",
            "-b:a",
            "192k",
            str(examples_dir / "vo_scene1.mp3"),
        ]
    )
    _run_ffmpeg_gen(
        [
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=660:sample_rate=48000:duration=3.0",
            "-af",
            "aformat=channel_layouts=stereo,volume=0.6,afade=t=in:st=0:d=0.05,afade=t=out:st=2.8:d=0.2",
            "-c:a",
            "libmp3lame",
            "-b:a",
            "192k",
            str(examples_dir / "vo_scene2.mp3"),
        ]
    )

    # 5. Stereo background music bed (15s MP3)
    _run_ffmpeg_gen(
        [
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=220:sample_rate=48000:duration=15.0",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=330:sample_rate=48000:duration=15.0",
            "-filter_complex",
            "[0:a][1:a]amerge=inputs=2,volume=0.45[aout]",
            "-map",
            "[aout]",
            "-c:a",
            "libmp3lame",
            "-b:a",
            "192k",
            str(examples_dir / "music.mp3"),
        ]
    )


def run_self_test() -> int:
    """Execute the full self-test demo and verify all quality gates."""
    try:
        ensure_ffmpeg_installed()
        examples_dir = Path(__file__).resolve().parent
        timeline_path = examples_dir / "demo_timeline.json"

        print("[demo] Generating synthetic lavfi test media next to demo_timeline.json...", file=sys.stderr)
        generate_synthetic_media(examples_dir)

        print("[demo] Validating demo_timeline.json...", file=sys.stderr)
        val_res = validate_timeline(timeline_path)
        if not val_res.ok:
            print(json.dumps(val_res.to_dict(), indent=2))
            print("FAIL: Timeline validation failed.", file=sys.stderr)
            return 1

        print("[demo] Rendering 12s demo timeline...", file=sys.stderr)
        summary = render_timeline(timeline_path)
        out_mp4 = Path(summary["output_path"])

        probe_info = probe_media(out_mp4)
        actual_dur = float(probe_info["duration"] or 0.0)
        target_dur = 12.0
        dur_ok = abs(actual_dur - target_dur) <= 0.5
        audio_stats = verify_stereo_signal(out_mp4, target_lufs=-14.0)
        v_codec = (probe_info.get("video") or {}).get("codec")
        a_codec = (probe_info.get("audio") or {}).get("codec")
        pix_fmt = (probe_info.get("video") or {}).get("pix_fmt")

        gates_passed = (
            dur_ok
            and v_codec == "h264"
            and a_codec == "aac"
            and pix_fmt == "yuv420p"
            and probe_info["size_bytes"] > 10_000
        )

        report = {
            "status": "PASS" if gates_passed else "FAIL",
            "render_summary": summary,
            "verification": {
                "video_codec": v_codec,
                "audio_codec": a_codec,
                "pix_fmt": pix_fmt,
                "duration_seconds": actual_dur,
                "expected_duration_seconds": target_dur,
                "duration_gate_passed": dur_ok,
                "stereo_peaks_db": audio_stats["channel_peaks_db"],
                "integrated_lufs": audio_stats["integrated_lufs"],
            },
        }
        print(json.dumps(report, indent=2, ensure_ascii=False))
        if gates_passed:
            print("PASS: All AgentCut quality gates succeeded.", file=sys.stderr)
            return 0
        print("FAIL: One or more quality gates did not pass.", file=sys.stderr)
        return 1

    except AgentCutError as exc:
        print(json.dumps({"status": "FAIL", **exc.to_dict()}, indent=2))
        print(f"FAIL: {exc.message}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(json.dumps({"status": "FAIL", "error": str(exc)}, indent=2))
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(run_self_test())
