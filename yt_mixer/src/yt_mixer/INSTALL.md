# Installation Guide

## Prerequisites

1. **Python 3.8+**
   ```bash
   python3 --version
   ```

2. **FFmpeg** (required for audio processing)
   ```bash
   # Ubuntu/Debian
   sudo apt update
   sudo apt install ffmpeg
   
   # Fedora/RHEL
   sudo dnf install ffmpeg
   
   # macOS
   brew install ffmpeg
   
   # Verify installation
   ffmpeg -version
   ```

## Installation Methods

### Method 1: From Source (Development)

Best for contributing or customizing the code.

```bash
# Clone repository
git clone https://github.com/yourusername/yt_mixer.git
cd yt_mixer

# Create virtual environment (recommended)
python3 -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install in editable mode
pip install -e .

# Verify installation
yt-mixer --help
```

### Method 2: Direct Install from GitHub

```bash
pip install git+https://github.com/yourusername/yt_mixer.git
```

### Method 3: From PyPI (when published)

```bash
pip install yt-mixer
```

## Post-Installation Setup

### 1. Quick Test

Start the server:
```bash
yt-mixer serve
```

Open browser: `http://localhost:5052`

Press `Ctrl+C` to stop.

### 2. Configure (Optional)

Set your preferences:
```bash
# Change port
yt-mixer config --set port=8080

# Change host (0.0.0.0 for network access, 127.0.0.1 for local only)
yt-mixer config --set host=0.0.0.0

# Set data directory
yt-mixer config --set data_dir=/path/to/data

# View all settings
yt-mixer config --list
```

### 3. Install as Systemd Service (Linux)

This makes YT Mixer run automatically on boot.

```bash
# Install service files
yt-mixer service --install

# Start service
systemctl --user start yt-mixer

# Enable auto-start on boot
systemctl --user enable yt-mixer

# Check status
yt-mixer service --status

# View logs
yt-mixer service --logs
```

**Enable Linger** (allows service to run even when not logged in):
```bash
sudo loginctl enable-linger $USER
```

### 4. Enable Auto-Updates (Optional)

Keep yt-dlp up to date (YouTube frequently changes its API):

```bash
systemctl --user enable --now yt-dlp-update.timer
```

This will update yt-dlp every 3 hours automatically.

## Environment Variables

You can also configure via environment variables:

```bash
# Create a config file
cat > ~/.config/yt-mixer/env << EOF
export YT_MIXER_HOST=0.0.0.0
export YT_MIXER_PORT=5052
export YT_MIXER_DATA_DIR=$HOME/.local/share/yt-mixer
export YT_MIXER_MAX_CHUNKS=3
export YT_MIXER_PRUNE_DAYS=7
EOF

# Source it in your shell
echo "source ~/.config/yt-mixer/env" >> ~/.bashrc
source ~/.bashrc
```

## Updating

### Update YT Mixer

```bash
# From source
cd yt_mixer
git pull
pip install -e .

# From PyPI
pip install --upgrade yt-mixer

# Restart service if running
yt-mixer service --restart
```

### Update yt-dlp

```bash
yt-mixer update
```

## Uninstallation

### Stop and Remove Service

```bash
systemctl --user stop yt-mixer
systemctl --user disable yt-mixer
systemctl --user stop yt-dlp-update.timer
systemctl --user disable yt-dlp-update.timer

rm ~/.config/systemd/user/yt-mixer.service
rm ~/.config/systemd/user/yt-dlp-update.service
rm ~/.config/systemd/user/yt-dlp-update.timer

systemctl --user daemon-reload
```

### Remove Package

```bash
pip uninstall yt-mixer
```

### Clean Data (Optional)

```bash
# Clean all session data
yt-mixer sessions --clean

# Or manually delete
rm -rf ~/data  # or wherever YT_MIXER_DATA_DIR points
```

## Troubleshooting

### Command not found: yt-mixer

The CLI script might not be in your PATH. Try:
```bash
# Find where it was installed
pip show yt-mixer

# Run directly with Python
python -m yt_mixer.cli --help
```

Or add to PATH:
```bash
# Find pip's script directory
pip show yt-mixer | grep Location
# Usually: ~/.local/bin or ~/venv/bin

export PATH="$HOME/.local/bin:$PATH"
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
```

### Permission denied

If installing globally without virtual environment:
```bash
pip install --user yt-mixer
```

### FFmpeg not found

```bash
# Check if installed
which ffmpeg

# Install if missing
sudo apt install ffmpeg  # Ubuntu/Debian
```

### yt-dlp errors

YouTube frequently changes its API. Update yt-dlp:
```bash
yt-mixer update
```

### Port already in use

Change to a different port:
```bash
yt-mixer config --set port=5053
```

Or kill the process using the port:
```bash
# Find process
sudo lsof -i :5052

# Kill it
kill -9 <PID>
```

### Can't access from another device

1. Make sure host is set to `0.0.0.0`:
   ```bash
   yt-mixer config --set host=0.0.0.0
   ```

2. Check firewall:
   ```bash
   sudo ufw allow 5052
   ```

3. Find your IP:
   ```bash
   ip addr show  # Linux
   ifconfig      # macOS
   ```

Access from other device: `http://<YOUR_IP>:5052`

## Getting Help

- GitHub Issues: https://github.com/yourusername/yt_mixer/issues
- Check logs: `yt-mixer service --logs` (if using systemd)
- Verbose output: `yt-mixer serve --debug`