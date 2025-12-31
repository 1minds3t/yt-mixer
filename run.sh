#!/bin/bash
# yt_mixer launcher for environment

# Activate environment

# Install dependencies if missing
pip install flask flask-socketio eventlet yt-dlp

# Ensure ffmpeg is available
if ! command -v ffmpeg &> /dev/null; then
    echo "Installing ffmpeg..."
    sudo apt install -y ffmpeg
fi

# Start the mixer
python wsgi.py