"""Unit tests for AgentCut v1.0 timeline schema validation."""

from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

from agentcut.cli import list_bundled_templates
from agentcut.schema import validate_timeline


VALID_TIMELINE_DICT = {
    "version": "1.0",
    "canvas": {"width": 1080, "height": 1920, "fps": 30, "background": "black"},
    "output": {"path": "out.mp4", "loudnorm_lufs": -14},
    "tracks": [
        {
            "type": "video",
            "clips": [
                {
                    "source": "scene1.mp4",
                    "start": 0,
                    "in": 0,
                    "out": 10,
                    "transition_in": {"type": "fade", "duration": 0.5},
                },
                {
                    "source": "scene2.mp4",
                    "start": 10,
                    "in": 0,
                    "out": 10,
                    "transition_in": {"type": "dissolve", "duration": 0.4},
                },
            ],
        },
        {
            "type": "audio",
            "role": "dialogue",
            "clips": [{"source": "vo_scene1.mp3", "start": 0.8, "volume": 1.0}],
        },
        {
            "type": "audio",
            "role": "music",
            "clips": [
                {
                    "source": "music.mp3",
                    "start": 0,
                    "duration": 20,
                    "volume": 0.25,
                    "fade_in": 0.5,
                    "fade_out": 2.0,
                    "duck_under_dialogue": True,
                }
            ],
        },
        {
            "type": "captions",
            "style": {
                "font_size": 64,
                "color": "white",
                "stroke_color": "black",
                "stroke_width": 4,
                "position": "bottom",
                "bold": True,
            },
            "segments": [
                {"start": 0.3, "end": 3.0, "text": "Hook line goes here", "highlight": True},
                {"start": 3.2, "end": 6.0, "text": "Second caption line"},
            ],
        },
        {
            "type": "overlay",
            "clips": [
                {
                    "source": "emoji.png",
                    "start": 2.0,
                    "duration": 1.5,
                    "x": 0.8,
                    "y": 0.15,
                    "scale": 0.18,
                    "animation": "bounce",
                }
            ],
        },
    ],
}


class TestTimelineSchema(unittest.TestCase):
    """Verify valid timelines pass and invalid timelines fail with specific error locations/codes."""

    def test_valid_timeline_passes(self) -> None:
        res = validate_timeline(VALID_TIMELINE_DICT, check_files=False)
        self.assertTrue(res.ok)
        self.assertEqual(res.errors, [])
        self.assertAlmostEqual(res.estimated_duration, 20.0)

    def test_bundled_templates_exist_and_validate(self) -> None:
        info = list_bundled_templates()
        self.assertTrue(info["ok"])
        names = [t["name"] for t in info["templates"]]
        self.assertEqual(sorted(names), ["meme_edit", "title_end_cards", "vertical_talking"])
        for tmpl in info["templates"]:
            res = validate_timeline(tmpl["path"], check_files=False)
            self.assertTrue(res.ok, f"Template {tmpl['name']} failed validation: {res.errors}")

    def test_bad_version_fails(self) -> None:
        bad = copy.deepcopy(VALID_TIMELINE_DICT)
        bad["version"] = "2.0"
        res = validate_timeline(bad, check_files=False)
        self.assertFalse(res.ok)
        self.assertTrue(any(e["location"] == "version" for e in res.errors))

    def test_overlapping_video_clips_fail(self) -> None:
        bad = copy.deepcopy(VALID_TIMELINE_DICT)
        # Clip 0 ends at 10.0s; setting Clip 1 start to 6.0s creates an invalid overlap
        bad["tracks"][0]["clips"][1]["start"] = 6.0
        res = validate_timeline(bad, check_files=False)
        self.assertFalse(res.ok)
        self.assertTrue(any(e["location"] == "tracks[0].clips[1].start" for e in res.errors))

    def test_missing_source_file_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            res = validate_timeline(VALID_TIMELINE_DICT, base_dir=Path(tmp), check_files=True)
            self.assertFalse(res.ok)
            self.assertTrue(any(e["code"] == "FILE_NOT_FOUND" for e in res.errors))

    def test_bad_transition_type_fails(self) -> None:
        bad = copy.deepcopy(VALID_TIMELINE_DICT)
        bad["tracks"][0]["clips"][0]["transition_in"]["type"] = "star_wipe"
        res = validate_timeline(bad, check_files=False)
        self.assertFalse(res.ok)
        self.assertTrue(
            any(e["location"] == "tracks[0].clips[0].transition_in.type" for e in res.errors)
        )

    def test_caption_with_end_less_than_or_equal_to_start_fails(self) -> None:
        bad = copy.deepcopy(VALID_TIMELINE_DICT)
        bad["tracks"][3]["segments"][0]["start"] = 4.0
        bad["tracks"][3]["segments"][0]["end"] = 3.0
        res = validate_timeline(bad, check_files=False)
        self.assertFalse(res.ok)
        self.assertTrue(any(e["location"] == "tracks[3].segments[0]" for e in res.errors))


if __name__ == "__main__":
    unittest.main()
