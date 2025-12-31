# YT Mixer

Server-side YouTube Audio Mixer with professional podcast-style audio processing.

## Features

- 🎵 Mix two YouTube playlists (music + speech/podcast)
- 🎚️ Professional audio processing with LUFS normalization and ducking
- 🔊 Real-time volume and EQ control
- 📦 Session-based mixing with bookmarkable URLs
- 🔄 Automatic chunk preparation (1-hour segments)
- 🌐 Web interface with audio player

## Installation

### From Source

```bash
# Clone the repository
git clone https://github.com/yourusername/yt_mixer.git
cd yt_mixer

# Install in development mode
pip install -e .
```

### Requirements

- Python 3.8+
- FFmpeg (for audio processing)
- yt-dlp (installed automatically)

Install FFmpeg:
```bash
# Ubuntu/Debian
sudo apt install ffmpeg

# macOS
brew install ffmpeg
```

## Quick Start

### 1. Start the server directly

```bash
yt-mixer serve
```

Access at `http://localhost:5052`

### 2. Or install as a systemd service

```bash
# Install service files
yt-mixer service --install

# Start and enable service
systemctl --user start yt-mixer
systemctl --user enable yt-mixer

# View logs
yt-mixer service --logs
```

## CLI Commands

### Configuration

```bash
# View all configuration
yt-mixer config --list

# Get specific value
yt-mixer config --get port

# Set a value
yt-mixer config --set port=5053
yt-mixer config --set host=127.0.0.1
```

### Session Management

```bash
# List all sessions
yt-mixer sessions

# Clean up all session data
yt-mixer sessions --clean
```

### Service Management

```bash
# Check service status
yt-mixer service --status

# Start/stop/restart
yt-mixer service --start
yt-mixer service --stop
yt-mixer service --restart

# Enable auto-start on boot
yt-mixer service --enable
```

### Updates

```bash
# Update yt-dlp
yt-mixer update
```

## Environment Variables

You can configure YT Mixer using environment variables:

```bash
export YT_MIXER_HOST=0.0.0.0
export YT_MIXER_PORT=5052
export YT_MIXER_DATA_DIR=/path/to/data
export YT_MIXER_MAX_CHUNKS=3
export YT_MIXER_PRUNE_DAYS=7
export YT_MIXER_CHUNK_DURATION=3600
export YT_MIXER_MUSIC_VOLUME=0.4
export YT_MIXER_SPEECH_VOLUME=1.0
```

## Usage

### Web Interface

1. Navigate to `http://localhost:5052`
2. Enter two YouTube playlist IDs (or full URLs):
   - Music playlist
   - Speech/podcast playlist
3. Click "Start Mixing"
4. Bookmark the generated URL to return to your mix anytime

### Bookmarking

Each combination of playlists gets a unique hash. The URL will look like:
```
http://localhost:5052/?sid=abc123def456
```

You can bookmark this URL and it will remember:
- Your specific playlist combination
- Your position in the mix
- Previously generated chunks

### Audio Processing

The mixer applies professional podcast-style processing:

1. **Speech Enhancement**
   - High-pass filter (removes rumble)
   - Center mono panning (fixes "left ear only" audio)
   - LUFS normalization to -16 (podcast standard)
   - Compression (evens out volume)

2. **Music Processing**
   - LUFS normalization to -23 (background level)

3. **Ducking**
   - Sidechain compression automatically lowers music when speech plays
   - Professional ratio and timing for natural sound

4. **Safety**
   - Final limiter prevents distortion

## Data Storage

YT Mixer stores data in `./data/` by default:

```
data/
├── raw_audio/           # Downloaded audio files (per session)
├── mixed_chunks/        # Generated 1-hour mixed chunks (per session)
└── config.json         # Persistent configuration
```

Sessions are automatically cleaned after 7 days of inactivity (configurable).

## Development

```bash
# Install with dev dependencies
pip install -e ".[dev]"

# Run tests (when implemented)
pytest

# Format code
black src/

# Lint
flake8 src/
```

## Troubleshooting

### yt-dlp errors

yt-dlp needs frequent updates as YouTube changes its API. Update regularly:

```bash
yt-mixer update
```

Or enable automatic updates:
```bash
systemctl --user enable --now yt-dlp-update.timer
```

### Port already in use

Change the port:
```bash
yt-mixer config --set port=5053
```

### FFmpeg not found

Install FFmpeg:
```bash
sudo apt install ffmpeg  # Ubuntu/Debian
brew install ffmpeg      # macOS
```

## License

MIT

## Contributing

Contributions welcome! Please open an issue or PR.