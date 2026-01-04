import hashlib
import time
import shutil
import logging
import threading
import sys
import json
from pathlib import Path
from .audio_engine import AudioWorker
from .config import CHUNK_DIR, AUDIO_DIR, DATA_DIR, config

log = logging.getLogger(__name__)

def setup_logging():
    """Setup file logging - MUST be called before any logging"""
    log_file = DATA_DIR / "yt-mixer.log"
    
    # Ensure data dir exists
    DATA_DIR.mkdir(exist_ok=True)
    
    # Remove any existing handlers to avoid duplicates
    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    
    # Console handler (for terminal output)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    console_handler.setFormatter(console_formatter)
    
    # File handler (for log file)
    file_handler = logging.FileHandler(log_file, mode='a')
    file_handler.setLevel(logging.INFO)
    file_formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    file_handler.setFormatter(file_formatter)
    
    # Add both handlers
    root_logger.addHandler(console_handler)
    root_logger.addHandler(file_handler)
    root_logger.setLevel(logging.INFO)
    
    log.info(f"=== YT-MIXER LOGGING STARTED === Log file: {log_file}")
    return log_file

class SessionManager:
    """
    Simplified session manager - keeps one active session at a time.
    When a new session is requested, the old one is cleaned up.
    """
    def __init__(self):
        # Setup logging FIRST THING
        self.log_file = setup_logging()
        
        self.active_session = None  # (session_id, AudioWorker)
        self.lock = threading.Lock()
        
        # Session metadata persistence
        self.config_file = DATA_DIR / "sessions.json"
        self.session_metadata = {}  # {sid: {music_pid, speech_pid}}
        self._load_session_metadata()
        
        # Background maintenance thread (started on demand)
        self.running = False
        self.cleaner_thread = None
        
        log.info("SessionManager initialized")
    
    def _load_session_metadata(self):
        """Load session metadata from disk"""
        if self.config_file.exists():
            try:
                with open(self.config_file, 'r') as f:
                    self.session_metadata = json.load(f)
                log.info(f"Loaded metadata for {len(self.session_metadata)} sessions")
            except Exception as e:
                log.error(f"Error loading session metadata: {e}")
                self.session_metadata = {}
    
    def _save_session_metadata(self):
        """Save session metadata to disk"""
        try:
            with open(self.config_file, 'w') as f:
                json.dump(self.session_metadata, f, indent=2)
        except Exception as e:
            log.error(f"Error saving session metadata: {e}")
    
    def start_maintenance(self):
        """Start the background maintenance thread"""
        if self.cleaner_thread is None or not self.cleaner_thread.is_alive():
            self.running = True
            self.cleaner_thread = threading.Thread(target=self._cleanup_loop, daemon=True)
            self.cleaner_thread.start()
            log.info("Started maintenance thread")
    
    def get_session_id(self, music_id, speech_id):
        """Generate a consistent hash for a playlist combo"""
        raw = f"{music_id.strip()}|{speech_id.strip()}"
        return hashlib.md5(raw.encode()).hexdigest()[:12]  # Short 12-char hash
    
    def get_or_create_session(self, music_id, speech_id):
        """
        Get or create a session.
        If a different session is requested, clean up the old one.
        """
        with self.lock:
            sid = self.get_session_id(music_id, speech_id)
            
            # Store metadata
            self.session_metadata[sid] = {
                'music_pid': music_id,
                'speech_pid': speech_id,
                'created': time.time()
            }
            self._save_session_metadata()
            
            # If we already have this session, return it
            if self.active_session and self.active_session[0] == sid:
                log.info(f"[{sid}] Reusing existing session")
                return sid, self.active_session[1]
            
            # Different session requested - clean up old one
            if self.active_session:
                old_sid, old_worker = self.active_session
                log.info(f"Switching from session {old_sid} to {sid}")
                self._cleanup_session(old_sid, old_worker)
            
            # Create new session
            log.info(f"[{sid}] Starting new session: music={music_id}, speech={speech_id}")
            worker = AudioWorker(sid, music_id, speech_id)
            self.active_session = (sid, worker)
            
            return sid, worker
    
    def load_session_by_id(self, sid):
        """
        Load a session by ID from bookmark.
        This restores the session from metadata if it exists.
        """
        with self.lock:
            # If already active, return it
            if self.active_session and self.active_session[0] == sid:
                log.info(f"[{sid}] Session already active")
                return sid, self.active_session[1]
            
            # Try to get metadata
            if sid not in self.session_metadata:
                log.error(f"[{sid}] No metadata found for this session!")
                return None, None
            
            metadata = self.session_metadata[sid]
            music_id = metadata.get('music_pid')
            speech_id = metadata.get('speech_pid')
            
            if not music_id or not speech_id:
                log.error(f"[{sid}] Invalid metadata: {metadata}")
                return None, None
            
            # Clean up old active session if exists
            if self.active_session:
                old_sid, old_worker = self.active_session
                log.info(f"Cleaning up old session {old_sid}")
                self._cleanup_session(old_sid, old_worker)
            
            # Create worker for this session
            log.info(f"[{sid}] Restoring session from bookmark: music={music_id}, speech={speech_id}")
            worker = AudioWorker(sid, music_id, speech_id)
            self.active_session = (sid, worker)
            
            return sid, worker
    
    def get_active_session(self):
        """Get the currently active session"""
        with self.lock:
            return self.active_session
    
    def _cleanup_session(self, session_id, worker):
        """
        Stop a worker and optionally delete its data.
        By default, keeps the chunks so user can return to them.
        """
        try:
            # Stop the worker thread
            worker.running = False
            if hasattr(worker, 'thread'):
                worker.thread.join(timeout=5)
            
            log.info(f"[{session_id}] Stopped worker")
            
            # Keep chunks but clean up raw audio to save space
            audio_dir = AUDIO_DIR / session_id
            if audio_dir.exists():
                shutil.rmtree(audio_dir)
                log.info(f"[{session_id}] Cleaned up raw audio")
            
        except Exception as e:
            log.error(f"[{session_id}] Error cleaning up: {e}")
    
    def delete_session(self, session_id):
        """
        Completely delete a session's data.
        Useful for freeing up disk space.
        """
        with self.lock:
            # Stop worker if it's the active one
            if self.active_session and self.active_session[0] == session_id:
                _, worker = self.active_session
                worker.running = False
                self.active_session = None
            
            # Delete metadata
            if session_id in self.session_metadata:
                del self.session_metadata[session_id]
                self._save_session_metadata()
            
            # Delete all data
            chunk_dir = CHUNK_DIR / session_id
            audio_dir = AUDIO_DIR / session_id
            
            for directory in [chunk_dir, audio_dir]:
                if directory.exists():
                    shutil.rmtree(directory)
                    log.info(f"[{session_id}] Deleted {directory}")
    
    def list_sessions(self):
        """
        List all sessions on disk (active or cached).
        Returns list of (session_id, chunk_count, size_mb, is_active)
        """
        sessions = []
        
        for session_dir in CHUNK_DIR.glob('*'):
            if session_dir.is_dir():
                chunks = list(session_dir.glob('*.mp3'))
                size_mb = sum(f.stat().st_size for f in chunks) / (1024 * 1024)
                
                is_active = False
                with self.lock:
                    if self.active_session and self.active_session[0] == session_dir.name:
                        is_active = True
                
                metadata = self.session_metadata.get(session_dir.name, {})
                
                sessions.append({
                    'id': session_dir.name,
                    'chunks': len(chunks),
                    'size_mb': size_mb,
                    'is_active': is_active,
                    'music_pid': metadata.get('music_pid'),
                    'speech_pid': metadata.get('speech_pid')
                })
        
        return sessions
    
    def _cleanup_loop(self):
        """
        Background thread that periodically cleans up old sessions.
        Runs every hour.
        """
        while self.running:
            try:
                time.sleep(3600)  # 1 hour
                self._prune_old_sessions()
            except Exception as e:
                log.error(f"Error in cleanup loop: {e}")
    
    def _prune_old_sessions(self):
        """
        Delete sessions that haven't been accessed in PRUNE_AGE_DAYS.
        """
        max_age_seconds = config.get('prune_age_days', 7) * 86400
        now = time.time()
        
        log.info("Running session cleanup...")
        
        for session_dir in CHUNK_DIR.glob('*'):
            if not session_dir.is_dir():
                continue
            
            # Skip active session
            with self.lock:
                if self.active_session and self.active_session[0] == session_dir.name:
                    continue
            
            # Check age based on last modification time
            age = now - session_dir.stat().st_mtime
            
            if age > max_age_seconds:
                log.info(f"Pruning old session: {session_dir.name} (age: {age/86400:.1f} days)")
                self.delete_session(session_dir.name)
    
    def shutdown(self):
        """Gracefully shutdown all workers"""
        log.info("Shutting down SessionManager...")
        self.running = False
        
        with self.lock:
            if self.active_session:
                _, worker = self.active_session
                worker.running = False
                log.info("Stopped active worker")
        
        if self.cleaner_thread and self.cleaner_thread.is_alive():
            self.cleaner_thread.join(timeout=5)
            log.info("Stopped maintenance thread")

# Global instance
manager = SessionManager()