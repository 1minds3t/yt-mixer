# YT Mixer

Server-side YouTube audio/video mixer with professional podcast-style processing, ad-hoc video injection, and iOS-native playback.

## Features

- 🎵 Mix two YouTube playlists (music + speech/podcast) into continuous 1-hour chunks
- 🎚️ **Podcast-quality audio:** Automatic sidechain ducking (music lowers for voice), LUFS normalization to broadcast standards (-16 for speech, -23 for music), and a final mastering limiter
- 🎬 Ad-hoc video injection — queue any YouTube video and it plays with a freshly mixed background track, then seamlessly returns to your stream
- 📱 iOS-native video playback with HTTP 206 range support and a dedicated video page (no competing `<audio>` session)
- 🔁 Persistent server-side job state — close the browser mid-render and the Play button is waiting when you come back
- 📦 Session-based mixing with bookmarkable URLs
- ⚡ Optional [omnipkg](https://github.com/1minds3t/omnipkg) integration for pinned yt-dlp worker processes (~2ms probe vs ~400ms cold import)

## Installation

### From Source

```bash
git clone https://github.com/1minds3t/yt-mixer.git
cd yt-mixer
pip install -e .
```

With omnipkg daemon support (optional, recommended on Linux):

```bash
pip install -e ".[full]"
```

### Requirements

- Python 3.8+
- FFmpeg

```bash
# Ubuntu/Debian
sudo apt install ffmpeg

# macOS
brew install ffmpeg
```

## Quick Start

```bash
yt-mixer serve
```

Access at `http://localhost:5052`

### Install as a systemd service

```bash
yt-mixer service --install
systemctl --user start yt-mixer
systemctl --user enable yt-mixer
yt-mixer service --logs
```

## CLI

```bash
# Configuration
yt-mixer config --list
yt-mixer config --set port=5053
yt-mixer config --get port

# Sessions
yt-mixer sessions
yt-mixer sessions --clean

# Service
yt-mixer service --status
yt-mixer service --start | --stop | --restart | --enable

# Update yt-dlp
yt-mixer update
```

## Environment Variables

```bash
export YT_MIXER_HOST=0.0.0.0          # default: 0.0.0.0
export YT_MIXER_PORT=5052              # default: 5052
export YT_MIXER_DATA_DIR=./data        # default: ./data
export YT_MIXER_MAX_CHUNKS=3           # default: 3 (chunks kept in memory)
export YT_MIXER_PRUNE_DAYS=7           # default: 7 (days before session cleanup)
export YT_MIXER_CHUNK_DURATION=3600    # default: 3600 (seconds per chunk)
export YT_MIXER_MUSIC_VOLUME=0.4       # default: 0.4
export YT_MIXER_SPEECH_VOLUME=1.0      # default: 1.0
```

## Usage

### Background Mix

1. Open `http://localhost:5052`
2. Enter two YouTube playlist IDs or full URLs — one music, one speech/podcast
3. Click **Create Mix** and bookmark the generated `/?sid=...` URL

The mixer produces continuous 1-hour chunks. Each chunk goes through three quality tiers as processing completes: **Immediate** (raw) → **Quick** (normalized) → **Final** (LUFS mastered).

### Ad-hoc Video Injection

While the background mix is playing, paste any YouTube URL into the **Add Video Audio** panel and hit **Prepare**. The server downloads audio and video in parallel in the background — your mix keeps playing uninterrupted. When ready, hit **Play video** to watch with a freshly mixed background track underneath. On exit, the background stream resumes where it left off.

State is held server-side: close the tab mid-render, reopen it, and the **Play video** button appears automatically without re-hitting Prepare.

### Bookmarking

```
http://localhost:5052/?sid=abc123def456
```

Bookmarking this URL restores your session including playlist configuration and playback position.

## Audio Processing

**Speech:**
- High-pass filter (removes rumble)
- Center mono panning
- LUFS normalization to -16 (podcast standard)
- Compression

**Music:**
- LUFS normalization to -23 (background level)

**Mix:**
- Sidechain ducking — music automatically lowers when speech plays
- Final limiter prevents clipping

## omnipkg Integration

When `omnipkg` is installed (`pip install -e ".[full]"`), yt_mixer uses its daemon to keep two tagged persistent worker processes alive:

- `ytdlp-info` — metadata probes (title, duration, livestream check)
- `ytdlp-dl` — audio and video downloads

Workers are pinned (survive idle timeout) and kept alive by a keepalive thread that pings every 4 minutes. This cuts yt-dlp startup from ~400ms to ~2ms and avoids repeated Python interpreter cold-starts for every download. Falls back transparently to direct `import yt_dlp` if the daemon is unavailable.

## Data Storage

```
data/
├── raw_audio/       # Downloaded audio per session
├── mixed_chunks/    # Generated 1-hour mixed chunks per session
├── adhoc_jobs/      # Ad-hoc video job state and final renders
└── config.json      # Persistent configuration
```

Sessions auto-clean after 7 days of inactivity. Ad-hoc jobs GC after 2 hours (ready) or 30 minutes (failed/cancelled).

## Development

```bash
pip install -e ".[dev]"
pytest
black src/
ruff src/
```

## Troubleshooting

**yt-dlp errors** — YouTube changes its API frequently; update regularly:
```bash
yt-mixer update
```

**Port in use:**
```bash
yt-mixer config --set port=5053
```

**FFmpeg not found:**
```bash
sudo apt install ffmpeg   # Ubuntu/Debian
brew install ffmpeg       # macOS
```

**Video won't play on iOS** — This is usually caused by an audio session conflict. The **Play video** button solves this automatically by navigating to a [dedicated player page](#ad-hoc-video-injection) that has no competing `<audio>` element. If you've modified the frontend and are trying to play a `<video>` element on the main mixer page, it will fail on iOS — the dedicated page is required.

## License

AGPL-3.0