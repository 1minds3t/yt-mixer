import os
import logging
import subprocess
import threading
import random
import time
import shutil
import traceback
from pathlib import Path
import yt_dlp
from .config import AUDIO_DIR, CHUNK_DIR

log = logging.getLogger(__name__)

# GLOBAL LUFS LOCK - only ONE LUFS process across ALL workers
LUFS_LOCK = threading.Lock()
LUFS_QUEUE = []  # Queue of (worker_id, chunk_idx) waiting for LUFS

class AudioWorker:
    def __init__(self, session_id, music_pid, speech_pid):
        self.session_id = session_id
        self.music_pid = music_pid
        self.speech_pid = speech_pid
        self.running = True
        
        # Session specific paths
        self.my_audio_dir = AUDIO_DIR / session_id
        self.my_chunk_dir = CHUNK_DIR / session_id
        self.my_audio_dir.mkdir(exist_ok=True)
        self.my_chunk_dir.mkdir(exist_ok=True)
        
        # State
        self.music_queue = []
        self.speech_queue = []
        self.preloaded_chunks = []
        self.chunk_index = 0
        self.current_chunk_path = None
        self.current_chunk_quality = 'none'
        self.target_chunk_duration = 3600  # 1 hour
        
        # Progress tracking
        self.mix_progress = {}
        self.error_log = []
        
        # LUFS tracking
        self.lufs_in_progress = set()  # Track which chunks are doing LUFS
        
        # Locks
        self.lock = threading.Lock()
        
        # Start background thread
        self.thread = threading.Thread(target=self._background_loop, daemon=True)
        self.thread.start()
        
        log.info(f"[{self.session_id}] AudioWorker initialized")

    def _log_error(self, message, exc=None):
        """Log error and store for UI display"""
        full_message = f"[{self.session_id}] {message}"
        if exc:
            full_message += f"\n{traceback.format_exc()}"
            log.error(full_message)
        else:
            log.error(full_message)
        
        with self.lock:
            self.error_log.append({
                'time': time.strftime('%H:%M:%S'),
                'message': message
            })
            self.error_log = self.error_log[-10:]

    def get_video_ids(self, playlist_url, max_fetch=50):
        """Extract video IDs"""
        if "&si=" in playlist_url:
            playlist_url = playlist_url.split("&si=")[0]
        if "youtube.com" not in playlist_url:
            playlist_id = playlist_url.split("&")[0]
            playlist_url = f"https://www.youtube.com/playlist?list={playlist_id}"
        
        ydl_opts = {
            'quiet': True,
            'extract_flat': True,
            'playlistend': max_fetch,
            'no_warnings': True
        }
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(playlist_url, download=False)
                if not info:
                    return []
                entries = info.get('entries', [])
                video_ids = [e['id'] for e in entries if e and 'id' in e]
                random.shuffle(video_ids)
                log.info(f"[{self.session_id}] Got {len(video_ids)} video IDs")
                return video_ids
        except Exception as e:
            self._log_error(f"Playlist fetch error: {e}", exc=True)
            return []

    def _download_audio(self, video_id, output_path):
        """Download audio from YouTube"""
        ydl_opts = {
            'format': 'bestaudio/best',
            'outtmpl': str(output_path.with_suffix('')),
            'quiet': True,
            'no_warnings': True,
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '128',
            }],
            'noplaylist': True,
        }
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([f'https://www.youtube.com/watch?v={video_id}'])
            return output_path.exists() and output_path.stat().st_size > 1024
        except Exception as e:
            log.error(f"[{self.session_id}] Error downloading {video_id}: {e}")
            return False

    def _get_audio_duration(self, path):
        """Get duration in seconds"""
        cmd = [
            'ffprobe', '-v', 'error', '-show_entries', 'format=duration',
            '-of', 'default=noprint_wrappers=1:nokey=1', str(path)
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True)
            return float(result.stdout.strip())
        except Exception:
            return 0.0

    def _ensure_queue_filled(self, queue_type):
        """Ensure queue has content"""
        queue = self.music_queue if queue_type == 'music' else self.speech_queue
        
        if len(queue) < 10:
            playlist_url = self.music_pid if queue_type == 'music' else self.speech_pid
            new_ids = self.get_video_ids(playlist_url, max_fetch=50)
            if new_ids:
                queue.extend(new_ids)
                log.info(f"[{self.session_id}] Refilled {queue_type} queue: {len(new_ids)}")

    def _collect_tracks_for_chunk(self, queue_type, target_duration):
        """Collect tracks until target duration"""
        collected_tracks = []
        total_duration = 0.0
        
        while total_duration < target_duration:
            with self.lock:
                self._ensure_queue_filled(queue_type)
                queue = self.music_queue if queue_type == 'music' else self.speech_queue
                if not queue:
                    self._log_error(f"{queue_type} queue empty")
                    break
                video_id = queue.pop(0)
            
            audio_path = self.my_audio_dir / f"{queue_type}_{video_id}.mp3"
            
            if total_duration > (target_duration * 0.9):
                log.info(f"[{self.session_id}] At 90% of target, stopping")
                with self.lock:
                    queue.insert(0, video_id)
                break
            
            if not self._download_audio(video_id, audio_path):
                continue
            
            duration = self._get_audio_duration(audio_path)
            if duration > 5:
                collected_tracks.append(audio_path)
                total_duration += duration
                log.info(f"[{self.session_id}] Added {queue_type} ({duration:.1f}s) - Total: {total_duration:.1f}s")
        
        log.info(f"[{self.session_id}] Collected {len(collected_tracks)} {queue_type} tracks = {total_duration:.1f}s")
        return collected_tracks

    def _concat(self, tracks, output_path):
        """Concatenate audio tracks"""
        if not tracks:
            return False
        
        list_file = output_path.parent / f"{output_path.stem}_list.txt"
        try:
            with open(list_file, 'w') as f:
                for track in tracks:
                    f.write(f"file '{track.resolve()}'\n")
            
            cmd = [
                'ffmpeg', '-y', '-f', 'concat', '-safe', '0',
                '-i', str(list_file), '-c', 'copy', str(output_path)
            ]
            
            result = subprocess.run(cmd, capture_output=True, text=True)
            list_file.unlink(missing_ok=True)
            
            if result.returncode == 0:
                log.info(f"[{self.session_id}] Concatenated {len(tracks)} tracks")
                return True
            else:
                self._log_error(f"Concat failed: {result.stderr[:200]}")
                return False
        except Exception as e:
            self._log_error(f"Concat error: {e}", exc=True)
            return False

    def _update_progress(self, index, stage, percent=None):
        """Update progress for UI"""
        with self.lock:
            if index not in self.mix_progress:
                self.mix_progress[index] = {}
            self.mix_progress[index]['stage'] = stage
            if percent is not None:
                self.mix_progress[index]['percent'] = percent

    def _prepare_immediate_mix(self, index, m_concat, s_concat):
        """IMMEDIATE: 5-10 seconds, NO normalization, WITH LIMITER"""
        self._update_progress(index, 'immediate_mix', 0)
        
        immediate_path = self.my_chunk_dir / f"{index}_immediate.mp3"
        
        vocal_eq = (
            'equalizer=f=100:width_type=o:width=2:g=-6,'
            'equalizer=f=800:width_type=o:width=2:g=4,'
            'equalizer=f=2000:width_type=o:width=2:g=6,'
            'equalizer=f=8000:width_type=o:width=2:g=-4'
        )
        
        # ADD HARD LIMITER to prevent clipping
        filter_complex = (
            f'[0:a]volume=0.4[m];'
            f'[1:a]highpass=f=80,{vocal_eq}[s];'
            f'[m][s]amix=inputs=2:duration=shortest:dropout_transition=2,'
            f'alimiter=limit=0.9:attack=5:release=50[out]'  # LIMITER prevents clipping
        )
        
        cmd = [
            'ffmpeg', '-y', '-i', str(m_concat), '-i', str(s_concat),
            '-filter_complex', filter_complex, '-map', '[out]',
            '-c:a', 'libmp3lame', '-b:a', '128k',
            str(immediate_path)
        ]
        
        log.info(f"[{self.session_id}] Creating IMMEDIATE mix (with limiter)...")
        
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
            
            if result.returncode != 0:
                self._log_error(f"Immediate mix failed: {result.stderr[:300]}")
                return None
            
            self._update_progress(index, 'immediate_mix', 100)
            log.info(f"[{self.session_id}] ⚡ IMMEDIATE mix ready: {immediate_path.stat().st_size / 1024 / 1024:.1f}MB")
            return str(immediate_path)
            
        except Exception as e:
            self._log_error(f"Immediate mix error: {e}", exc=True)
            return None

    def _prepare_quick_mix(self, index, m_concat, s_concat):
        """QUICK: Fast normalization WITH LIMITER"""
        self._update_progress(index, 'quick_mix', 0)
        
        quick_path = self.my_chunk_dir / f"{index}_quick.mp3"
        
        vocal_eq = (
            'equalizer=f=100:width_type=o:width=2:g=-6,'
            'equalizer=f=800:width_type=o:width=2:g=4,'
            'equalizer=f=2000:width_type=o:width=2:g=6,'
            'equalizer=f=8000:width_type=o:width=2:g=-4'
        )
        
        # WITH LIMITER
        filter_complex = (
            f'[0:a]dynaudnorm=f=150:g=11:r=0.9[m_norm];'
            f'[m_norm]volume=0.4[m_ready];'
            f'[1:a]highpass=f=80,{vocal_eq},dynaudnorm=f=200:g=15:r=0.9[s_ready];'
            f'[m_ready][s_ready]amix=inputs=2:duration=shortest:dropout_transition=2,'
            f'alimiter=limit=0.9:attack=5:release=50[out]'  # LIMITER
        )
        
        cmd = [
            'ffmpeg', '-y', '-i', str(m_concat), '-i', str(s_concat),
            '-filter_complex', filter_complex, '-map', '[out]',
            '-c:a', 'libmp3lame', '-b:a', '128k',
            str(quick_path)
        ]
        
        log.info(f"[{self.session_id}] Creating QUICK mix (with limiter)...")
        
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=6000)
            
            if result.returncode != 0:
                self._log_error(f"Quick mix failed: {result.stderr[:300]}")
                return None
            
            self._update_progress(index, 'quick_mix', 100)
            log.info(f"[{self.session_id}] ✓ QUICK mix ready: {quick_path.stat().st_size / 1024 / 1024:.1f}MB")
            return str(quick_path)
            
        except Exception as e:
            self._log_error(f"Quick mix error: {e}", exc=True)
            return None

    def _prepare_final_mix(self, index, m_concat, s_concat):
        """FINAL: LUFS normalization - SERIALIZED, only ONE at a time globally"""
        
        # WAIT for LUFS lock - only one LUFS process at a time
        log.info(f"[{self.session_id}] Chunk {index} waiting for LUFS lock...")
        
        with LUFS_LOCK:
            log.info(f"[{self.session_id}] Chunk {index} acquired LUFS lock - starting FINAL mix")
            
            with self.lock:
                self.lufs_in_progress.add(index)
            
            self._update_progress(index, 'final_mix', 0)
            
            final_path = self.my_chunk_dir / f"{index}.mp3"
            
            # LUFS with limiter
            filter_complex = (
                f'[0:a]loudnorm=I=-20:TP=-2:LRA=11:print_format=summary[m_norm];'
                f'[m_norm]volume=0.55[m_ready];'
                f'[1:a]highpass=f=80,pan=stereo|c0=c0|c1=c0,'
                f'loudnorm=I=-16:TP=-1.5:LRA=11:print_format=summary[s_ready];'
                f'[m_ready][s_ready]amix=inputs=2:duration=shortest:dropout_transition=2[mixed];'
                f'[mixed]alimiter=limit=0.9:attack=5:release=50[out]'
            )
            
            cmd = [
                'ffmpeg', '-y', '-i', str(m_concat), '-i', str(s_concat),
                '-filter_complex', filter_complex, '-map', '[out]',
                '-c:a', 'libmp3lame', '-b:a', '128k',
                str(final_path)
            ]
            
            log.info(f"[{self.session_id}] [CHUNK {index}] Starting LUFS processing...")
            start_time = time.time()
            
            try:
                # FIX: Use Popen but read continuously to prevent buffer deadlock
                process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    bufsize=1  # Line buffered
                )
                
                # Helper to read stderr without blocking main thread logic
                def reader(pipe, log_list):
                    for line in pipe:
                        # We can parse progress here if we want, but mostly we just need to DRAIN the pipe
                        # FFmpeg outputs to stderr.
                        if "time=" in line:
                            log_list.append(line) 

                # Store last status line
                status_bucket = []
                reader_thread = threading.Thread(target=reader, args=(process.stderr, status_bucket))
                reader_thread.daemon = True
                reader_thread.start()
                
                # Main loop monitors the process
                while True:
                    if process.poll() is not None:
                        break
                    
                    elapsed = time.time() - start_time
                    
                    # Log every 30 seconds
                    if int(elapsed) % 30 == 0 and int(elapsed) > 0:
                        # Try to find duration in status bucket to calc real %
                        current_status = status_bucket[-1] if status_bucket else "Processing..."
                        log.info(f"[{self.session_id}] [CHUNK {index}] LUFS Running... ({elapsed:.0f}s)")
                        
                        # Fake progress for now since LUFS 2-pass is hard to predict
                        progress = min(95, int((elapsed / 300) * 100))
                        self._update_progress(index, 'final_mix', progress)
                        
                        # Empty bucket to save RAM
                        if len(status_bucket) > 10:
                            status_bucket[:] = status_bucket[-1:]
                            
                    time.sleep(1)
                
                # Process finished
                reader_thread.join(timeout=5)
                
                if process.returncode != 0:
                    # Read any remaining error output
                    err_out = process.stderr.read() if process.stderr else "Unknown error"
                    self._log_error(f"[CHUNK {index}] FINAL mix failed with code {process.returncode}")
                    return None
                
                elapsed = time.time() - start_time
                self._update_progress(index, 'final_mix', 100)
                file_size = final_path.stat().st_size / 1024 / 1024
                log.info(f"[{self.session_id}] [CHUNK {index}] ✨ FINAL LUFS mix done in {elapsed:.0f}s ({file_size:.1f}MB)")
                
                with self.lock:
                    self.lufs_in_progress.discard(index)
                
                return str(final_path)
                
            except Exception as e:
                self._log_error(f"[CHUNK {index}] FINAL mix error: {e}", exc=True)
                with self.lock:
                    self.lufs_in_progress.discard(index)
                return None

    def _background_loop(self):
        """Main loop: keeps 2 chunks preloaded"""
        while self.running:
            with self.lock:
                should_prepare = len(self.preloaded_chunks) < 2
                if should_prepare:
                    next_idx = self.chunk_index + len(self.preloaded_chunks) + 1
            
            if should_prepare:
                try:
                    chunk_info = self.prepare_chunk(next_idx)
                    if chunk_info:
                        with self.lock:
                            self.preloaded_chunks.append(chunk_info)
                            log.info(f"[{self.session_id}] Preloaded chunk {chunk_info['index']}")
                except Exception as e:
                    self._log_error(f"Chunk prep error: {e}", exc=True)
            
            time.sleep(5)

    def prepare_chunk(self, index):
        """THREE-TIER preparation: IMMEDIATE → QUICK → FINAL"""
        log.info(f"[{self.session_id}] === Preparing chunk {index} ===")
        self._update_progress(index, 'collecting', 0)
        
        # Collect tracks in parallel
        m_tracks = []
        s_tracks = []
        m_error = []
        s_error = []
        
        def collect_music():
            try:
                m_tracks.extend(self._collect_tracks_for_chunk("music", self.target_chunk_duration))
            except Exception as e:
                m_error.append(e)
                self._log_error(f"Music collection failed: {e}", exc=True)
        
        def collect_speech():
            try:
                s_tracks.extend(self._collect_tracks_for_chunk("speech", self.target_chunk_duration))
            except Exception as e:
                s_error.append(e)
                self._log_error(f"Speech collection failed: {e}", exc=True)
        
        music_thread = threading.Thread(target=collect_music)
        speech_thread = threading.Thread(target=collect_speech)
        
        music_thread.start()
        speech_thread.start()
        
        music_thread.join()
        speech_thread.join()
        
        if m_error or s_error or not m_tracks or not s_tracks:
            self._log_error(f"Failed to collect tracks for chunk {index}")
            return None

        # Temp files
        m_concat = self.my_chunk_dir / f"m_tmp_{index}.mp3"
        s_concat = self.my_chunk_dir / f"s_tmp_{index}.mp3"

        # Concatenate
        self._update_progress(index, 'concatenating', 0)
        if not self._concat(m_tracks, m_concat) or not self._concat(s_tracks, s_concat):
            return None
        
        # 1. IMMEDIATE mix
        immediate_path = self._prepare_immediate_mix(index, m_concat, s_concat)
    
        if not immediate_path:
            self._log_error(f"Immediate mix failed for chunk {index}")
            m_concat.unlink(missing_ok=True)
            s_concat.unlink(missing_ok=True)
            for t in m_tracks + s_tracks: 
                t.unlink(missing_ok=True)
            return None
        
        log.info(f"[{self.session_id}] ⚡ Chunk {index} ready for streaming")
        
        # 2 & 3. Upgrade pipeline in background
        def upgrade_pipeline():
            try:
                log.info(f"[{self.session_id}] Starting upgrade pipeline for chunk {index}")
                
                # QUICK mix
                quick_path = self._prepare_quick_mix(index, m_concat, s_concat)
                
                if quick_path:
                    with self.lock:
                        if self.current_chunk_path == immediate_path:
                            log.info(f"[{self.session_id}] Upgrading current chunk to QUICK")
                            self.current_chunk_path = quick_path
                            self.current_chunk_quality = 'quick'
                        
                        for i, chunk_info in enumerate(self.preloaded_chunks):
                            if chunk_info['path'] == immediate_path:
                                self.preloaded_chunks[i] = {
                                    'path': quick_path,
                                    'quality': 'quick',
                                    'index': index
                                }
                                break
                    
                    if immediate_path:
                        Path(immediate_path).unlink(missing_ok=True)
                    log.info(f"[{self.session_id}] ✓ Upgraded to QUICK mix")
                    
                    # FINAL mix - ONLY if not too many in progress
                    with self.lock:
                        lufs_count = len(self.lufs_in_progress)
                    
                    if lufs_count >= 2:
                        log.warning(f"[{self.session_id}] Skipping FINAL mix for chunk {index} - {lufs_count} LUFS already in progress")
                    else:
                        log.info(f"[{self.session_id}] Starting FINAL mix for chunk {index}")
                        final_path = self._prepare_final_mix(index, m_concat, s_concat)
                        
                        if final_path:
                            with self.lock:
                                if self.current_chunk_path == quick_path:
                                    log.info(f"[{self.session_id}] Upgrading current chunk to FINAL")
                                    self.current_chunk_path = final_path
                                    self.current_chunk_quality = 'final'
                                
                                for i, chunk_info in enumerate(self.preloaded_chunks):
                                    if chunk_info['path'] == quick_path:
                                        self.preloaded_chunks[i] = {
                                            'path': final_path,
                                            'quality': 'final',
                                            'index': index
                                        }
                                        break
                            
                            Path(quick_path).unlink(missing_ok=True)
                            log.info(f"[{self.session_id}] ✨ Upgraded to FINAL mix")
                
                # Cleanup
                m_concat.unlink(missing_ok=True)
                s_concat.unlink(missing_ok=True)
                for t in m_tracks + s_tracks: 
                    t.unlink(missing_ok=True)
                    
                log.info(f"[{self.session_id}] Upgrade pipeline complete for chunk {index}")
                
            except Exception as e:
                self._log_error(f"Upgrade pipeline error for chunk {index}: {e}", exc=True)
        
        threading.Thread(target=upgrade_pipeline, daemon=True).start()
        
        return {
            'path': immediate_path,
            'quality': 'immediate',
            'index': index
        }
    
    def stop(self):
        """Stop worker"""
        self.running = False
        if self.thread.is_alive():
            self.thread.join(timeout=5)
    
    def get_status(self):
        """Get status"""
        with self.lock:
            return {
                "chunk_index": self.chunk_index,
                "current_chunk": self.current_chunk_path,
                "current_chunk_quality": self.current_chunk_quality,
                "preloaded_count": len(self.preloaded_chunks),
                "music_queue_size": len(self.music_queue),
                "speech_queue_size": len(self.speech_queue),
                "mix_progress": dict(self.mix_progress),
                "errors": self.error_log[-5:],
                "lufs_in_progress": len(self.lufs_in_progress),  # NEW
                "lufs_chunks": list(self.lufs_in_progress)  # NEW
            }