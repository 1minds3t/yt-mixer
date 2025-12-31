# YT Mixer 🎵

Server-side YouTube playlist mixer with **true shuffle** for more unpredictable playback order.

## Features

- Mix music + podcast playlists with automatic ducking  
- **True random shuffle** using Fisher-Yates (avoids YouTube's linear pseudo-shuffle behavior)  
- Three-tier audio quality:
  - ⚡ **Immediate**: Quick playback, no normalization, vocal EQ applied
  - 📊 **Quick Mix**: Per-track normalization + EQ, ready in ~30–60 seconds
  - ✨ **Final Mix**: Full LUFS mastering, professional loudness & sidechain  
- Hour-long chunks streamed seamlessly  
- Session persistence

## Install
```bash
pip install -e .
````

## Usage

```bash
# Start service
yt-mixer service start

# Access at http://localhost:5052
# Enter two YouTube playlist IDs and mix!
```

## How it works

1. Downloads tracks from both playlists
2. **Properly shuffles** them using Fisher-Yates for true randomness
3. Applies vocal EQ + per-track normalization
4. Streams hour-long chunks with incremental quality upgrades (Immediate → Quick → Final)
