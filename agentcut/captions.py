"""ASS subtitle generation for burned-in captions with styling, auto-wrap, and RTL support."""

from __future__ import annotations

from pathlib import Path
from typing import Any

NAMED_COLORS_HEX: dict[str, str] = {
    "white": "#FFFFFF",
    "black": "#000000",
    "yellow": "#FFD600",
    "red": "#FF2A2A",
    "cyan": "#00E5FF",
    "green": "#00E676",
    "blue": "#2979FF",
    "orange": "#FF9100",
    "magenta": "#FF00FF",
    "pink": "#FF4081",
    "gray": "#9E9E9E",
    "grey": "#9E9E9E",
}


def color_to_ass_bgr(color_str: str, alpha: int = 0) -> str:
    """Convert a CSS color name or `#RRGGBB` string into ASS `&HAABBGGRR` format."""
    cleaned = color_str.strip().lower()
    hex_str = NAMED_COLORS_HEX.get(cleaned, cleaned)
    if hex_str.startswith("#"):
        hex_str = hex_str[1:]
    if len(hex_str) == 3:
        hex_str = "".join(ch * 2 for ch in hex_str)
    if len(hex_str) != 6:
        hex_str = "FFFFFF"
    try:
        r = int(hex_str[0:2], 16)
        g = int(hex_str[2:4], 16)
        b = int(hex_str[4:6], 16)
    except ValueError:
        r, g, b = 255, 255, 255
    a = max(0, min(255, alpha))
    return f"&H{a:02X}{b:02X}{g:02X}{r:02X}"


def format_ass_timestamp(seconds: float) -> str:
    """Format seconds as an ASS timestamp `H:MM:SS.cc`."""
    total_cs = max(0, int(round(seconds * 100)))
    cs = total_cs % 100
    total_s = total_cs // 100
    s = total_s % 60
    total_m = total_s // 60
    m = total_m % 60
    h = total_m // 60
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def wrap_caption_two_lines(text: str, max_chars_per_line: int = 28) -> str:
    """Auto-wrap caption text into at most 2 balanced lines for ASS rendering.

    Preserves Unicode and RTL scripts (Arabic/Urdu) cleanly and escapes ASS braces.
    """
    raw_lines = [ln.strip() for ln in text.strip().splitlines() if ln.strip()]
    if not raw_lines:
        return ""
    if len(raw_lines) >= 2:
        joined = [raw_lines[0], " ".join(raw_lines[1:])]
        return r"\N".join(_escape_ass_text(part) for part in joined)

    single = raw_lines[0]
    words = single.split()
    if len(single) <= max_chars_per_line or len(words) <= 1:
        return _escape_ass_text(single)

    # Split into 2 balanced lines near the character midpoint
    best_idx = 1
    best_diff = float("inf")
    for idx in range(1, len(words)):
        left = " ".join(words[:idx])
        right = " ".join(words[idx:])
        diff = abs(len(left) - len(right))
        if diff < best_diff:
            best_diff = diff
            best_idx = idx

    line1 = _escape_ass_text(" ".join(words[:best_idx]))
    line2 = _escape_ass_text(" ".join(words[best_idx:]))
    return rf"{line1}\N{line2}"


def _escape_ass_text(text: str) -> str:
    """Escape literal curly braces so libass does not treat them as override tags."""
    return text.replace("{", r"\{").replace("}", r"\}")


def generate_ass_file(
    caption_tracks: list[dict[str, Any]],
    canvas_width: int,
    canvas_height: int,
    output_ass_path: Path,
) -> Path:
    """Generate an ASS subtitle file from one or more timeline `captions` tracks.

    Supports custom styles, outline stroke, vertical positioning (`bottom`, `center`, `top`),
    auto-wrapping to max 2 lines, per-segment `"highlight": true` (rendered 22% larger
    and bold), and UTF-8 / RTL shaping via libass.
    """
    styles_lines: list[str] = []
    events_lines: list[str] = []

    for t_idx, track in enumerate(caption_tracks):
        style = track.get("style", {})
        font_family = str(style.get("font_family", "DejaVu Sans"))
        font_size = int(round(float(style.get("font_size", 64))))
        highlight_font_size = int(round(font_size * 1.22))
        primary_color = color_to_ass_bgr(str(style.get("color", "white")))
        stroke_color = color_to_ass_bgr(str(style.get("stroke_color", "black")))
        back_color = color_to_ass_bgr("black", alpha=160)
        stroke_width = float(style.get("stroke_width", 4))
        bold_flag = -1 if style.get("bold", True) else 0

        position = str(style.get("position", "bottom")).lower()
        if position == "top":
            alignment = 8  # Top-center
            margin_v = int(round(canvas_height * 0.10))
        elif position in ("center", "middle"):
            alignment = 5  # Middle-center
            margin_v = 0
        else:
            alignment = 2  # Bottom-center
            margin_v = int(round(canvas_height * 0.12))

        margin_lr = int(round(canvas_width * 0.07))
        style_name = f"Track{t_idx}Default"
        hi_style_name = f"Track{t_idx}Highlight"

        styles_lines.append(
            f"Style: {style_name},{font_family},{font_size},{primary_color},{primary_color},"
            f"{stroke_color},{back_color},{bold_flag},0,0,0,100,100,0,0,1,"
            f"{stroke_width:.1f},1.5,{alignment},{margin_lr},{margin_lr},{margin_v},1"
        )
        styles_lines.append(
            f"Style: {hi_style_name},{font_family},{highlight_font_size},{primary_color},{primary_color},"
            f"{stroke_color},{back_color},-1,0,0,0,100,100,0,0,1,"
            f"{stroke_width * 1.2:.1f},2.0,{alignment},{margin_lr},{margin_lr},{margin_v},1"
        )

        # Estimate max characters per line from canvas width and font size
        usable_width = canvas_width - 2 * margin_lr
        approx_char_width = max(12.0, font_size * 0.52)
        max_chars = max(14, min(42, int(usable_width / approx_char_width)))

        for seg in track.get("segments", []):
            start_str = format_ass_timestamp(float(seg["start"]))
            end_str = format_ass_timestamp(float(seg["end"]))
            is_hi = bool(seg.get("highlight", False))
            chosen_style = hi_style_name if is_hi else style_name
            line_limit = max(12, int(max_chars * 0.85)) if is_hi else max_chars
            wrapped = wrap_caption_two_lines(str(seg["text"]), max_chars_per_line=line_limit)
            events_lines.append(
                f"Dialogue: 0,{start_str},{end_str},{chosen_style},,0,0,0,,{wrapped}"
            )

    ass_content = "\n".join(
        [
            "[Script Info]",
            "Title: AgentCut Burned-In Captions",
            "ScriptType: v4.00+",
            f"PlayResX: {canvas_width}",
            f"PlayResY: {canvas_height}",
            "WrapStyle: 2",
            "ScaledBorderAndShadow: yes",
            "YCbCr Matrix: TV.709",
            "",
            "[V4+ Styles]",
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
            "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, "
            "Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
            *styles_lines,
            "",
            "[Events]",
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
            *events_lines,
            "",
        ]
    )

    output_ass_path.parent.mkdir(parents=True, exist_ok=True)
    output_ass_path.write_text(ass_content, encoding="utf-8")
    return output_ass_path
