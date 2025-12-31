import hashlib
import time
import shutil
import logging
import threading
from pathlib import Path
from .audio_engine import AudioWorker
from .config import CHUNK_DIR, AUDIO_DIR, config

log = logging.getLogger(__name__)

class SessionManager:
    """
    Simplified session manager - keeps one active session at a time.
    When a new session is requested, the old one is cleaned up.
    """
    def __init__(self):
        self.active_session = None  # (session_id, AudioWorker)
        self.lock = threading.Lock()
        
        # Background maintenance thread (started on demand)
        self.running = False
        self.cleaner_thread = None
    
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
            
            # If we already have this session, return it
            if self.active_session and self.active_session[0] == sid:
                log.info(f"Reusing existing session: {sid}")
                return sid, self.active_session[1]
            
            # Different session requested - clean up old one
            if self.active_session:
                old_sid, old_worker = self.active_session
                log.info(f"Switching from session {old_sid} to {sid}")
                self._cleanup_session(old_sid, old_worker)
            
            # Create new session
            log.info(f"Starting new session: {sid}")
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
            
            log.info(f"Stopped worker for session {session_id}")
            
            # Keep chunks but clean up raw audio to save space
            audio_dir = AUDIO_DIR / session_id
            if audio_dir.exists():
                shutil.rmtree(audio_dir)
                log.info(f"Cleaned up raw audio for session {session_id}")
            
        except Exception as e:
            log.error(f"Error cleaning up session {session_id}: {e}")
    
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
            
            # Delete all data
            chunk_dir = CHUNK_DIR / session_id
            audio_dir = AUDIO_DIR / session_id
            
            for directory in [chunk_dir, audio_dir]:
                if directory.exists():
                    shutil.rmtree(directory)
                    log.info(f"Deleted {directory}")
    
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
                
                sessions.append({
                    'id': session_dir.name,
                    'chunks': len(chunks),
                    'size_mb': size_mb,
                    'is_active': is_active
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