"""End-to-end self-test rendering tiny lavfi media in a temporary directory."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from agentcut.audio import verify_stereo_signal
from agentcut.probe import probe_media
from agentcut.render import render_timeline


def _ffmpeg_available() -> bool:
    return bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


@unittest.skipUnless(_ffmpeg_available(), "ffmpeg and ffprobe are required for end-to-end render tests")
class TestEndToEndSelfTest(unittest.TestCase):
    """Generate tiny lavfi media in a temp dir, render a short timeline, and verify all gates."""

    def test_short_lavfi_timeline_renders_and_passes_gates(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agentcut_selftest_") as tmp_str:
            tmp = Path(tmp_str)
            clip1 = tmp / "clip1.mp4"
            clip2 = tmp / "clip2.mp4"
            vo = tmp / "vo.mp3"
            music = tmp / "music.mp3"
            badge = tmp / "badge.png"
            out_mp4 = tmp / "rendered.mp4"

            # Generate two 2s video clips
            for path, color in ((clip1, "0x1E40AF"), (clip2, "0x047857")):
                subprocess.run(
                    [
                        "ffmpeg",
                        "-y",
                        "-hide_banner",
                        "-loglevel",
                        "error",
                        "-f",
                        "lavfi",
                        "-i",
                        f"color=c={color}:s=360x640:r=30:d=2.0",
                        "-c:v",
                        "libx264",
                        "-pix_fmt",
                        "yuv420p",
                        str(path),
                    ],
                    check=True,
                )

            # Generate transparent PNG overlay
            subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "color=c=0xF59E0B@0.9:s=64x64:d=1,format=rgba",
                    "-frames:v",
                    "1",
                    str(badge),
                ],
                check=True,
            )

            # Generate dialogue and music stereo audio clips
            subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "sine=frequency=500:sample_rate=48000:duration=1.5",
                    "-af",
                    "aformat=channel_layouts=stereo",
                    "-c:a",
                    "libmp3lame",
                    str(vo),
                ],
                check=True,
            )
            subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "sine=frequency=250:sample_rate=48000:duration=4.0",
                    "-af",
                    "aformat=channel_layouts=stereo",
                    "-c:a",
                    "libmp3lame",
                    str(music),
                ],
                check=True,
            )

            timeline = {
                "version": "1.0",
                "canvas": {"width": 360, "height": 640, "fps": 30, "background": "black"},
                "output": {"path": str(out_mp4), "loudnorm_lufs": -14},
                "tracks": [
                    {
                        "type": "video",
                        "clips": [
                            {
                                "source": str(clip1),
                                "start": 0,
                                "in": 0,
                                "out": 2,
                                "transition_in": {"type": "fade", "duration": 0.3},
                            },
                            {
                                "source": str(clip2),
                                "start": 2,
                                "in": 0,
                                "out": 2,
                                "transition_in": {"type": "dissolve", "duration": 0.3},
                            },
                        ],
                    },
                    {
                        "type": "audio",
                        "role": "dialogue",
                        "clips": [{"source": str(vo), "start": 0.3, "volume": 1.0}],
                    },
                    {
                        "type": "audio",
                        "role": "music",
                        "clips": [
                            {
                                "source": str(music),
                                "start": 0,
                                "duration": 4,
                                "volume": 0.3,
                                "fade_in": 0.2,
                                "fade_out": 0.4,
                                "duck_under_dialogue": True,
                            }
                        ],
                    },
                    {
                        "type": "captions",
                        "style": {
                            "font_size": 32,
                            "color": "white",
                            "stroke_color": "black",
                            "stroke_width": 2,
                            "position": "bottom",
                            "bold": True,
                        },
                        "segments": [
                            {"start": 0.2, "end": 1.8, "text": "Self-test hook line", "highlight": True},
                            {"start": 2.1, "end": 3.8, "text": "Second caption segment"},
                        ],
                    },
                    {
                        "type": "overlay",
                        "clips": [
                            {
                                "source": str(badge),
                                "start": 0.5,
                                "duration": 1.5,
                                "x": 0.8,
                                "y": 0.2,
                                "scale": 0.18,
                                "animation": "bounce",
                            }
                        ],
                    },
                ],
            }

            summary = render_timeline(timeline, base_dir=tmp)
            self.assertTrue(summary["ok"])
            self.assertTrue(out_mp4.exists())

            info = probe_media(out_mp4)
            self.assertTrue(info["has_video"])
            self.assertTrue(info["has_audio"])
            self.assertIsNotNone(info["duration"])
            self.assertLessEqual(abs(float(info["duration"]) - 4.0), 0.5)

            audio_stats = verify_stereo_signal(out_mp4, target_lufs=-14.0)
            self.assertIn(1, audio_stats["channel_peaks_db"])
            self.assertIn(2, audio_stats["channel_peaks_db"])


if __name__ == "__main__":
    unittest.main()
