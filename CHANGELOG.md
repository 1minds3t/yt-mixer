# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.0] — 2026-03-27

iOS Native Video, GPU Transcoding & Daemon Integration

## 🚀 Features

- **GPU-Accelerated H.264 Transcoding:** Ad-hoc videos are now rendered into iOS-native MP4s by combining a freshly mixed background soundtrack with a full H.264 video transcode. This ensures maximum compatibility with Safari and other mobile browsers while offloading the heavy video work to **NVENC** instead of the CPU.
- **Dedicated iOS Player:** Added a new, isolated `/video/<job_id>` playback page containing only a `<video>` element. This avoids media-session conflicts with the main mixer page and enables reliable iOS behavior including **Picture-in-Picture, lock-screen controls, and AirPlay**.
- **HTTP 206 Range Support:** The stream endpoint now properly handles HTTP Range requests, which are required for video seeking and stable playback on Safari.
- **Asynchronous Final Rendering:** The final `ffmpeg` render now runs in a background thread, so the UI remains responsive and background audio plays uninterrupted while a video is being prepared.

- **`omnipkg` Daemon Integration:** All `yt-dlp` operations are now offloaded to persistent, RAM-resident worker processes. This cuts metadata probe latency from cold Python startup overhead (~400ms) to hot worker dispatch (**~2ms**).
- **Tagged Persistent Workers:** Separate `ytdlp-info` and `ytdlp-dl` worker tags keep fast metadata probes isolated from longer-running download tasks, ensuring the UI is never blocked by I/O.
- **Server-Side Job Recovery:** Ad-hoc job state is now recoverable on page refresh. The UI automatically detects and reconnects to in-progress server-side work, so a browser reload no longer orphans a render.

- **Music-Only Background Generation:** The audio engine now creates a dedicated music-only backing track for ad-hoc videos, so injected video audio is mixed against clean ambience rather than competing speech.
- **Server-Side Client Logging:** Added `/api/client-log` support to capture browser-side logs, which is invaluable for debugging mobile Safari and other environments without easy devtools access.
- **Robust State Handling:** The frontend now handles `409 Conflict` responses more gracefully by reconnecting to existing server-side jobs instead of forcing the user to restart.

- **Complete `README.md` Overhaul:** Reworked documentation to cover installation, usage, the ad-hoc video flow, `omnipkg` integration, and troubleshooting.
- **Project Packaging Cleanup:** Updated `pyproject.toml`, added dependency extras (`[full]`, `[dev]`), and configured `black`, `ruff`, and `pytest`.
- **Version Bump:** Project version updated to `v0.2.0`.

## ⚠️ Notable Changes
- **License Change:** The project license is now **AGPL-3.0**.
- **iOS Video Playback:** Video playback is now handled by a dedicated `/video/<job_id>` route to ensure native compatibility.
- **Optional Dependencies:** `omnipkg` integration is optional and enabled via `pip install -e ".[full]"`.

---

**📝 Code Changes:**
- UPDATE: src/yt_mixer/audio_engine.py (60 lines changed)
- UPDATE: src/yt_mixer/routes.py (474 lines changed)
- UPDATE: src/yt_mixer/session_manager.py (32 lines changed)
- NEW: src/yt_mixer/video_engine.py (1049 lines changed)

**📚 Documentation:**
- README.md (191 lines)
- THIRD_PARTY_NOTICES.txt (4 lines)
- docs/
- mkdocs.yml (6 lines)

**⚙️ Configuration:**
- pyproject.toml (48 lines)

**Additional Changes:**
- docs: setup basic doc template
- docs(project): overhaul documentation, update dependencies, and formalize project structure
- feat(adhoc-video): implement zero-copy video muxing, iOS native playback, and omnipkg daemon integration
- feat(adhoc): implement ad-hoc audio injection engine and UI
- feat: add recent sessions UI, direct streaming APIs, and robust URL handling

_13 files changed, 2917 insertions(+), 108 deletions(-)_

## [0.1.0] — 2026-02-18

**YT Mixer** is now public! This server-side audio engine mixes YouTube music and podcast playlists with professional broadcasting logic.

## Key Features
*   **True Shuffle:** Implements Fisher-Yates randomization to prevent the repetitive "pseudo-shuffle" found in YouTube's native player.
*   **3-Tier Audio Pipeline:**
    *   ⚡ **Immediate:** Starts playback instantly with basic mixing.
    *   📊 **Quick:** Background normalization for consistent volume.
    *   ✨ **Final:** Full LUFS mastering (-16 LUFS speech / -23 LUFS music) with sidechain ducking.
*   **Smart Ducking:** Automatically lowers music volume when speech/podcast tracks are active.
*   **Session Persistence:** Browser-independent playback state (resumes exactly where you left off, even after restarts).
*   **Web Interface:** Real-time mixing status, error logs, and audio player.

## System Integration
*   **CLI Tool:** Full `yt-mixer` command-line interface for configuration and management.
*   **Systemd Support:** Includes `yt-mixer service --install` for auto-start and background execution.
*   **Auto-Updates:** Optional timer to keep `yt-dlp` fresh against YouTube API changes.

## Installation
```bash
pip install yt-mixer
yt-mixer service --install

---

**📚 Documentation:**
- THIRD_PARTY_NOTICES.txt
- requirements.txt

**⚙️ Configuration:**
- pyproject.toml

**Additional Changes:**
- fix: Changed license to AGPL.
- add GitHub Actions workflow for PyPI deployment
- chore: initialize project config and licensing

**New Features:**
- feat: Add playback state persistence and improve UX
- feat: 3-tier audio pipeline + fix LUFS deadlock + SSE log streaming

**Updates:**
- Update .gitignore to exclude egg-info
- Update README.md
