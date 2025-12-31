import os
from pathlib import Path
import json

# Base Paths
BASE_DIR = Path(os.path.dirname(os.path.abspath(__file__)))
ROOT_DIR = BASE_DIR.parent.parent  # Go up to project root
DATA_DIR = Path(os.getenv("YT_MIXER_DATA_DIR", ROOT_DIR / "data"))

# Sub-directories (will be created per session)
AUDIO_DIR = DATA_DIR / "raw_audio"
CHUNK_DIR = DATA_DIR / "mixed_chunks"
CONFIG_FILE = DATA_DIR / "config.json"

# Server Configuration
HOST = os.getenv("YT_MIXER_HOST", "0.0.0.0")
PORT = int(os.getenv("YT_MIXER_PORT", "5052"))

# Session Settings
MAX_KEEP_CHUNKS = int(os.getenv("YT_MIXER_MAX_CHUNKS", "3"))
PRUNE_AGE_DAYS = int(os.getenv("YT_MIXER_PRUNE_DAYS", "7"))
TARGET_CHUNK_DURATION = int(os.getenv("YT_MIXER_CHUNK_DURATION", "3600"))  # 1 hour

# Audio Processing Settings
DEFAULT_MUSIC_VOLUME = float(os.getenv("YT_MIXER_MUSIC_VOLUME", "0.4"))
DEFAULT_SPEECH_VOLUME = float(os.getenv("YT_MIXER_SPEECH_VOLUME", "1.0"))

# Ensure base dirs exist
for p in [AUDIO_DIR, CHUNK_DIR]:
    p.mkdir(parents=True, exist_ok=True)

class Config:
    """
    Persistent configuration manager.
    Allows runtime changes to be saved and loaded.
    """
    def __init__(self):
        self.config_path = CONFIG_FILE
        self.settings = self._load()
    
    def _load(self):
        """Load config from disk or create default"""
        if self.config_path.exists():
            try:
                with open(self.config_path, 'r') as f:
                    return json.load(f)
            except Exception as e:
                print(f"Warning: Could not load config: {e}")
        
        # Default config
        return {
            "host": HOST,
            "port": PORT,
            "max_keep_chunks": MAX_KEEP_CHUNKS,
            "prune_age_days": PRUNE_AGE_DAYS,
            "target_chunk_duration": TARGET_CHUNK_DURATION,
            "default_music_volume": DEFAULT_MUSIC_VOLUME,
            "default_speech_volume": DEFAULT_SPEECH_VOLUME,
            "default_playlists": {
                "music": "",
                "speech": ""
            }
        }
    
    def save(self):
        """Persist config to disk"""
        try:
            with open(self.config_path, 'w') as f:
                json.dump(self.settings, f, indent=2)
            return True
        except Exception as e:
            print(f"Error saving config: {e}")
            return False
    
    def get(self, key, default=None):
        """Get a config value"""
        return self.settings.get(key, default)
    
    def set(self, key, value):
        """Set a config value and save"""
        self.settings[key] = value
        return self.save()
    
    def update(self, updates):
        """Update multiple config values"""
        self.settings.update(updates)
        return self.save()

# Global config instance
config = Config()