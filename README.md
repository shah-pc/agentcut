# AgentCut

**AgentCut** is a deterministic, headless video editor built for AI agents rather than human GUI clicks. An agent writes a declarative JSON timeline file, invokes `agentcut` (via CLI, Python API, or Model Context Protocol server), and receives a broadcast-normalized H.264/AAC MP4 verified by automated post-render safety gates.

## Key Features

- **Agent-First & Deterministic:** Every input is JSON and every output or error is machine-readable JSON on `stdout` (progress logs go to `stderr`). The same timeline and media always produce the exact same output.
- **Direct FFmpeg Orchestration:** Zero fragile wrapper libraries (no MoviePy). Uses Python 3.10+ standard library and direct `subprocess` calls to `ffmpeg` and `ffprobe`.
- **Duration-Preserving `xfade` Transitions:** Supports `fade`, `dissolve`, `wipeleft`, and `slideup` transitions between clips. Outgoing clips are automatically extended via `tpad=stop_mode=clone` by the transition duration so `xfade` overlaps never shrink the timeline or desynchronize downstream audio and captions.
- **Burned-In Captions via `libass`:** Generates `.ass` subtitles with customizable font size, color, stroke outline, vertical alignment (`bottom`, `center`, `top`), automatic balanced 2-line wrapping, per-segment `"highlight": true` scaling for hook lines, and full Unicode/RTL shaping (including Urdu and Arabic scripts).
- **Animated Image & Text Overlays:** Supports transparent PNGs and styled text overlays with center-based relative coordinates (`x`, `y` in `0.0–1.0`), canvas-relative `scale`, and deterministic per-frame animations (`none`, `fade`, `pop`, `bounce`).
- **Multi-Track Audio Mixing & Dialogue Ducking:** Mixes unlimited audio tracks (`dialogue`, `music`, `sfx`, `ambience`) with per-clip `volume`, `fade_in`, and `fade_out`. Music clips with `"duck_under_dialogue": true` automatically lower their volume during active dialogue windows via a computed trapezoidal volume envelope (`28%` ratio with `0.20s` attack and `0.30s` release ramps).
- **Mandatory Loudness & Stereo Safety Gates:** Performs 2-pass EBU R128 `loudnorm` to the timeline's target LUFS (default `-14 LUFS`) and verifies after rendering via `ffprobe` and `astats` that **both** Left and Right stereo channels carry active signal and that the rendered duration matches the timeline within `0.5s`.

## Installation

### Prerequisites

Ensure `ffmpeg` (compiled with `libx264`, `libass`, and `libfreetype`) and `ffprobe` are available on your `PATH`:

```bash
# macOS (Homebrew)
brew install ffmpeg

# Ubuntu / Debian
sudo apt-get update && sudo apt-get install -y ffmpeg

# Windows (Winget)
winget install Gyan.FFmpeg
```

### Install AgentCut

```bash
# Standard installation (zero third-party Python dependencies)
pip install -e .

# Optional: include the official `mcp` SDK extra (AgentCut also includes a built-in stdlib JSON-RPC stdio MCP server)
pip install -e ".[mcp,dev]"
```

## Quickstart

Run the self-contained demo (synthesizes test video, dialogue, music, and transparent PNG assets using FFmpeg `lavfi`, renders a 15-second vertical video, and runs all quality gates):

```bash
python3 examples/run_demo.py
```

Or use the CLI directly on any timeline JSON file:

```bash
# 1. Validate timeline schema and verify all referenced media files exist
agentcut validate examples/demo_timeline.json

# 2. Render timeline to MP4 (progress on stderr, final JSON report on stdout)
agentcut render examples/demo_timeline.json

# 3. Inspect any media file with ffprobe as structured JSON
agentcut probe examples/out/demo_output.mp4

# 4. List bundled starter templates
agentcut templates
```

## Full Timeline Schema Reference (`v1.0`)

All time values (`start`, `in`, `out`, `duration`, `end`, `fade_in`, `fade_out`) are floating-point seconds. Relative coordinates (`x`, `y`) are normalized `0.0–1.0` and center-anchored.

```json
{
  "version": "1.0",
  "canvas": {
    "width": 1080,
    "height": 1920,
    "fps": 30,
    "background": "black"
  },
  "output": {
    "path": "out.mp4",
    "loudnorm_lufs": -14
  },
  "tracks": [
    {
      "type": "video",
      "fit": "pad",
      "grade": "none",
      "clips": [
        {
          "source": "scene1.mp4",
          "start": 0,
          "in": 0,
          "out": 10,
          "transition_in": { "type": "fade", "duration": 0.5 }
        },
        {
          "source": "scene2.mp4",
          "start": 10,
          "in": 0,
          "out": 10,
          "transition_in": { "type": "dissolve", "duration": 0.4 }
        }
      ]
    },
    {
      "type": "audio",
      "role": "dialogue",
      "clips": [
        { "source": "vo_scene1.mp3", "start": 0.8, "volume": 1.0 }
      ]
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
          "duck_under_dialogue": true
        }
      ]
    },
    {
      "type": "captions",
      "style": {
        "font_size": 64,
        "color": "white",
        "stroke_color": "black",
        "stroke_width": 4,
        "position": "bottom",
        "bold": true
      },
      "segments": [
        { "start": 0.3, "end": 3.0, "text": "Hook line goes here", "highlight": true },
        { "start": 3.2, "end": 6.0, "text": "Second caption line" }
      ]
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
          "animation": "bounce"
        }
      ]
    }
  ]
}
```

### Top-Level Fields

| Field | Type | Required | Description |
| :--- | :--- | :--- | :--- |
| `version` | `string` | Yes | Must be `"1.0"`. |
| `canvas.width` | `integer` | Yes | Output width in pixels (even integer $\ge 16$, e.g. `1080`). |
| `canvas.height` | `integer` | Yes | Output height in pixels (even integer $\ge 16$, e.g. `1920`). |
| `canvas.fps` | `number` | Yes | Target frame rate (`1–240`, e.g. `30` or `60`). |
| `canvas.background` | `string` | No | FFmpeg color string for letterbox/pillarbox padding (default `"black"`). |
| `output.path` | `string` | Yes | Relative or absolute destination path for the rendered `.mp4`. |
| `output.loudnorm_lufs` | `number` | No | Target EBU R128 integrated loudness in LUFS (`-70` to `-5`, default `-14`). |
| `tracks` | `array` | Yes | Non-empty list of track objects (`video`, `audio`, `captions`, `overlay`). |

### Track Types & Properties

1. **`video` Track**
   - `fit`: `"pad"` / `"contain"` (default, scales inside canvas with background bars) or `"crop"` / `"cover"` (scales to fill canvas and center-crops).
   - `grade`: `"none"` (default), `"bw"` / `"grayscale"`, `"sepia"`, or `"high_contrast"`.
   - `clips[]`:
     - `source` (`string`, required): Video or still image file path.
     - `start` (`number`, required): Timeline start timestamp in seconds.
     - `in` / `out` (`number`): Source trim points in seconds (`0 <= in < out`). Still images or full clips may alternatively specify `duration` (`number > 0`).
     - `transition_in` (`object`, optional): `{ "type": "fade" | "dissolve" | "wipeleft" | "slideup", "duration": 0.5 }`.
2. **`audio` Track**
   - `role`: `"dialogue"`, `"music"`, `"sfx"`, or `"ambience"`.
   - `clips[]`:
     - `source` (`string`, required): Audio file path.
     - `start` (`number`, default `0`): Timeline start timestamp in seconds.
     - `in` / `out` or `duration` (`number`, optional): Defaults to full source duration.
     - `volume` (`number`, default `1.0`): Linear amplitude multiplier (`0.0–10.0`).
     - `fade_in` / `fade_out` (`number`, default `0.0`): Audio fade duration in seconds.
     - `duck_under_dialogue` (`boolean`, default `false`): Automatically lowers clip gain during any `"role": "dialogue"` clip.
3. **`captions` Track**
   - `style`: `font_size` (default `64`), `color` (default `"white"`), `stroke_color` (default `"black"`), `stroke_width` (default `4`), `position` (`"bottom"` | `"center"` | `"top"`), `bold` (default `true`), `font_family` (default `"DejaVu Sans"`).
   - `segments[]`:
     - `start` / `end` (`number`, required): Caption display window in seconds.
     - `text` (`string`, required): UTF-8 text (auto-wrapped to max 2 balanced lines; supports RTL scripts).
     - `highlight` (`boolean`, default `false`): Renders segment 22% larger and bold for hook lines.
4. **`overlay` Track**
   - `clips[]`:
     - `source` (`string`, optional): Image/PNG path with alpha transparency.
     - `text` (`string`, optional): Text overlay string (at least one of `source` or `text` is required).
     - `start` (`number`, required) and `duration` (`number`, required): Overlay active window.
     - `x`, `y` (`number`, default `0.5`): Center-anchored relative coordinates (`0.0–1.0`).
     - `scale` (`number`, default `0.2`): Target width as a fraction of `canvas.width`.
     - `animation`: `"none"`, `"fade"`, `"pop"` (scales up from 35% to 100% on entrance), or `"bounce"` (continuous vertical sine bounce).

## CLI Usage

```bash
# Render a timeline and capture the JSON summary
agentcut render timeline.json

# Validate a timeline (exits 0 if valid, 1 with structured JSON errors if invalid)
agentcut validate timeline.json

# Validate schema structure only (without checking media files on disk)
agentcut validate templates/vertical_talking.json --no-check-files

# Probe a media file
agentcut probe out.mp4

# List bundled templates
agentcut templates
```

## MCP Server Usage (`agentcut-mcp`)

Start the MCP server over `stdio`:

```bash
agentcut-mcp
```

Exposed MCP tools:
- `render_timeline(timeline, base_dir=None, output_path=None)` — Accepts either a file path to `timeline.json` or a raw JSON timeline object. Returns `output_path`, `render_summary`, and `probe_summary`.
- `validate_timeline(timeline, base_dir=None, check_files=True)` — Validates schema and file existence; returns structured errors.
- `probe_media(path)` — Returns `ffprobe` stream and container metadata as JSON.

### Example MCP Client Configuration (`claude_desktop_config.json` / `.cursor/mcp.json`)

```json
{
  "mcpServers": {
    "agentcut": {
      "command": "agentcut-mcp",
      "args": []
    }
  }
}
```

## Architectural & Design Notes

1. **Computed Trapezoidal Ducking Envelope (`agentcut/audio.py`):** Rather than routing a `sidechaincompress` filter across arbitrary numbers of dialogue clips (which requires splitting and merging sidechain buses and introduces threshold-dependent pumping), AgentCut resolves the exact active intervals `[(d_start, d_end), ...]` of all `"role": "dialogue"` clips, merges overlapping windows, and compiles a deterministic per-frame FFmpeg `volume` expression with smooth `0.20s` attack and `0.30s` release ramps down to `28%` gain (`~ -11 dB`).
2. **Timeline-Sync `xfade` via `tpad` (`agentcut/render.py`):** Standard FFmpeg `xfade` overlaps two streams and shortens the total video duration by `duration` on every transition. AgentCut prepends `tpad=stop_mode=clone:stop_duration=<d>` to the outgoing stream right before each `xfade`, keeping total duration and every clip's `start` timestamp 100% aligned with audio, captions, and overlays.
3. **Two-Pass EBU R128 Loudnorm + Stereo Gate (`agentcut/audio.py`):** Audio tracks are mixed at 48 kHz stereo, measured in Pass 1 with `loudnorm=print_format=json`, normalized in Pass 2 to the timeline's `loudnorm_lufs`, muxed into the H.264 (`yuv420p`, `+faststart`) container, and verified post-render with `astats` per-channel peak/RMS inspection.
