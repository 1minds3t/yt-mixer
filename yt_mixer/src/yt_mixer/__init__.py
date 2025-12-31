"""
YT Mixer - Server-side YouTube Audio Mixer with Ducking and Podcast Processing
"""

__version__ = "0.1.0"
__author__ = "1minds3t"

from .config import config, Config, DATA_DIR, AUDIO_DIR, CHUNK_DIR

__all__ = [
    'config',
    'Config', 
    'DATA_DIR',
    'AUDIO_DIR',
    'CHUNK_DIR',
]