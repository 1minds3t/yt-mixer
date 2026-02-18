# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
