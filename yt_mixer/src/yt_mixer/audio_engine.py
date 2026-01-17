import os
import logging
import subprocess
import threading
import random
import time
import shutil
from pathlib import Path
import yt_dlp
from .config import AUDIO_DIR, CHUNK_DIR

log = logging.getLogger(__name__)

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
        self.current_chunk_quality = 'none'  # 'immediate', 'quick', 'final'
        self.target_chunk_duration = 3600  # 1 hour
        
        # Progress tracking for UI
        self.mix_progress = {}  # {chunk_idx: {'stage': str, 'percent': int}}
        
        # Locks
        self.lock = threading.Lock()
        
        # Start background thread
        self.thread = threading.Thread(target=self._background_loop, daemon=True)
        self.thread.start()

    def get_video_ids(self, playlist_url, max_fetch=None):
        """Extract video IDs from entire playlist"""
        if "&si=" in playlist_url:
            playlist_url = playlist_url.split("&si=")[0]
        if "youtube.com" not in playlist_url:
            playlist_id = playlist_url.split("&")[0]
            playlist_url = f"https://www.youtube.com/playlist?list={playlist_id}"
        
        ydl_opts = {
            'quiet': True,
            'extract_flat': True,
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
                log.info(f"[{self.session_id}] Got {len(video_ids)} video IDs from playlist")
                return video_ids
        except Exception as e:
            log.error(f"[{self.session_id}] Playlist Error: {e}", exc_info=True)
            return []

    def _download_audio(self, video_id, output_path):
        """Download audio from YouTube video"""
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
        """Get duration of audio file in seconds"""
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
        """Ensure queue has content - MUST be called with lock held"""
        queue = self.music_queue if queue_type == 'music' else self.speech_queue
        
        if len(queue) < 10:
            playlist_url = self.music_pid if queue_type == 'music' else self.speech_pid
            new_ids = self.get_video_ids(playlist_url)
            if new_ids:
                queue.extend(new_ids)
                log.info(f"[{self.session_id}] Refilled {queue_type} queue: {len(new_ids)} new videos")

    def _collect_tracks_for_chunk(self, queue_type, target_duration):
        """Collect tracks until target duration is reached - DON'T download huge tracks if we're close"""
        collected_tracks = []
        total_duration = 0.0
        
        while total_duration < target_duration:
            with self.lock:
                self._ensure_queue_filled(queue_type)
                queue = self.music_queue if queue_type == 'music' else self.speech_queue
                if not queue:
                    log.error(f"[{self.session_id}] {queue_type} queue empty")
                    break
                video_id = queue.pop(0)
            
            audio_path = self.my_audio_dir / f"{queue_type}_{video_id}.mp3"
            
            # Check if we're close to target - if so, stop before downloading
            if total_duration > (target_duration * 0.9):  # At 90% of target
                log.info(f"[{self.session_id}] At {total_duration:.1f}s (~90% of target), stopping before downloading more")
                with self.lock:
                    queue.insert(0, video_id)  # Put back for next chunk
                break
            
            # Download the track
            if not self._download_audio(video_id, audio_path):
                continue
            
            duration = self._get_audio_duration(audio_path)
            if duration > 5:
                collected_tracks.append(audio_path)
                total_duration += duration
                log.info(f"[{self.session_id}] Added {queue_type} track {video_id} ({duration:.1f}s) - Total: {total_duration:.1f}s")
        
        log.info(f"[{self.session_id}] Collected {len(collected_tracks)} {queue_type} tracks totaling {total_duration:.1f}s")
        return collected_tracks

    def _concat(self, tracks, output_path):
        """Concatenate multiple audio tracks into one file"""
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
                log.error(f"[{self.session_id}] FFmpeg concat failed: {result.stderr}")
                return False
        except Exception as e:
            log.error(f"[{self.session_id}] Concat error: {e}")
            return False

    def _update_progress(self, index, stage, percent=None):
        """Update mix progress for UI"""
        with self.lock:
            if index not in self.mix_progress:
                self.mix_progress[index] = {}
            self.mix_progress[index]['stage'] = stage
            if percent is not None:
                self.mix_progress[index]['percent'] = percent

    def _prepare_immediate_mix(self, index, m_concat, s_concat):
        """
        IMMEDIATE: Stream in 5-10 seconds!
        - 40% music, 100% speech
        - Vocal EQ boost for clarity
        - NO normalization (pure speed)
        """
        self._update_progress(index, 'immediate_mix', 0)
        
        immediate_path = self.my_chunk_dir / f"{index}_immediate.mp3"
        
        # Vocal EQ: Boost mids, reduce lows/highs for speech clarity
        vocal_eq = (
            'equalizer=f=100:width_type=o:width=2:g=-6,'   # Reduce bass rumble
            'equalizer=f=800:width_type=o:width=2:g=4,'    # Boost low-mids (warmth)
            'equalizer=f=2000:width_type=o:width=2:g=6,'   # Boost vocal presence
            'equalizer=f=8000:width_type=o:width=2:g=-4'   # Reduce harsh highs
        )
        
        # PURE MIXING - No normalization!
        filter_complex = (
            f'[0:a]volume=0.4[m];'                         # Music: 40% 
            f'[1:a]highpass=f=80,{vocal_eq}[s];'           # Speech: cleanup + EQ
            f'[m][s]amix=inputs=2:duration=shortest:dropout_transition=2[out]'
        )
        
        cmd = [
            'ffmpeg', '-y', '-i', str(m_concat), '-i', str(s_concat),
            '-filter_complex', filter_complex, '-map', '[out]',
            '-c:a', 'libmp3lame', '-b:a', '128k',
            str(immediate_path)
        ]
        
        log.info(f"[{self.session_id}] Creating IMMEDIATE mix (no normalization)...")
        
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
            
            if result.returncode != 0:
                log.error(f"[{self.session_id}] Immediate mix failed: {result.stderr[:500]}")
                return None
            
            self._update_progress(index, 'immediate_mix', 100)
            log.info(f"[{self.session_id}] ⚡ IMMEDIATE mix ready: {immediate_path.stat().st_size / 1024 / 1024:.1f}MB")
            return str(immediate_path)
            
        except subprocess.TimeoutExpired:
            log.error(f"[{self.session_id}] Immediate mix timed out")
            return None
        except Exception as e:
            log.error(f"[{self.session_id}] Immediate mix error: {e}", exc_info=True)
            return None

    def _prepare_quick_mix(self, index, m_concat, s_concat):
        """
        QUICK: Normalize each track separately, then mix
        - Fast single-pass normalization on EACH track
        - Then mix at 40% music / 100% speech
        - Ready in 30-60 seconds
        - PREVENTS volume yo-yo effect!
        """
        self._update_progress(index, 'quick_mix', 0)
        
        quick_path = self.my_chunk_dir / f"{index}_quick.mp3"
        
        # Vocal EQ for speech
        vocal_eq = (
            'equalizer=f=100:width_type=o:width=2:g=-6,'
            'equalizer=f=800:width_type=o:width=2:g=4,'
            'equalizer=f=2000:width_type=o:width=2:g=6,'
            'equalizer=f=8000:width_type=o:width=2:g=-4'
        )
        
        # KEY FIX: Normalize EACH track separately, THEN mix
        filter_complex = (
            # Music: normalize first, then reduce to 40%
            f'[0:a]dynaudnorm=f=150:g=11:r=0.9[m_norm];'
            f'[m_norm]volume=0.4[m_ready];'
            
            # Speech: cleanup + EQ + normalize (keeps at 100%)
            f'[1:a]highpass=f=80,{vocal_eq},dynaudnorm=f=200:g=15:r=0.9[s_ready];'
            
            # Mix at consistent levels (no more volume fluctuation!)
            f'[m_ready][s_ready]amix=inputs=2:duration=shortest:dropout_transition=2[out]'
        )
        
        cmd = [
            'ffmpeg', '-y', '-i', str(m_concat), '-i', str(s_concat),
            '-filter_complex', filter_complex, '-map', '[out]',
            '-c:a', 'libmp3lame', '-b:a', '128k',
            str(quick_path)
        ]
        
        log.info(f"[{self.session_id}] Creating QUICK mix (separate normalization)...")
        
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=6000)
            
            if result.returncode != 0:
                log.error(f"[{self.session_id}] Quick mix failed: {result.stderr[:500]}")
                return None
            
            self._update_progress(index, 'quick_mix', 100)
            log.info(f"[{self.session_id}] ✓ Quick mix ready: {quick_path.stat().st_size / 1024 / 1024:.1f}MB")
            return str(quick_path)
            
        except subprocess.TimeoutExpired:
            log.error(f"[{self.session_id}] Quick mix timed out")
            return None
        except Exception as e:
            log.error(f"[{self.session_id}] Quick mix error: {e}", exc_info=True)
            return None

    def _prepare_final_mix(self, index, m_concat, s_concat):
        """
        FINAL: LUFS normalization on each track separately
        - Music gets LUFS normalized (I=-20, suitable for background)
        - Speech gets LUFS normalized (I=-16, podcast standard)
        - Mix at 55% music / 100% speech (more balanced than 40%)
        - GENTLE sidechain if speech is very loud (optional)
        - Takes 2-5 minutes but sounds professional
        """
        self._update_progress(index, 'final_mix', 0)
        
        final_path = self.my_chunk_dir / f"{index}.mp3"
        
        # FIXED: Normalize each track separately, then mix
        filter_complex = (
            # Music: LUFS normalize for background music level
            f'[0:a]loudnorm=I=-20:TP=-2:LRA=11[m_norm];'
            f'[m_norm]volume=0.55[m_ready];'  # 55% instead of 40% for better balance
            
            # Speech: LUFS normalize for podcast standard
            f'[1:a]highpass=f=80,pan=stereo|c0=c0|c1=c0,'
            f'loudnorm=I=-16:TP=-1.5:LRA=11[s_ready];'
            
            # Mix with gentle limiter (no aggressive sidechain!)
            f'[m_ready][s_ready]amix=inputs=2:duration=shortest:dropout_transition=2[mixed];'
            f'[mixed]alimiter=limit=0.95:attack=5:release=50[out]'
        )
        
        cmd = [
            'ffmpeg', '-y', '-i', str(m_concat), '-i', str(s_concat),
            '-filter_complex', filter_complex, '-map', '[out]',
            '-c:a', 'libmp3lame', '-b:a', '128k',
            '-progress', 'pipe:1',
            str(final_path)
        ]
        
        log.info(f"[{self.session_id}] Creating FINAL mix (LUFS on each track)...")
        
        try:
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1
            )
            
            last_log = time.time()
            while True:
                if process.poll() is not None:
                    break
                
                if time.time() - last_log > 30:
                    log.info(f"[{self.session_id}] LUFS processing (two-pass per track)...")
                    self._update_progress(index, 'final_mix', 50)
                    last_log = time.time()
                
                time.sleep(1)
            
            stdout, stderr = process.communicate()
            
            if process.returncode != 0:
                log.error(f"[{self.session_id}] Final mix failed: {stderr[:500]}")
                return None
            
            self._update_progress(index, 'final_mix', 100)
            log.info(f"[{self.session_id}] ✨ FINAL mix ready: {final_path.stat().st_size / 1024 / 1024:.1f}MB")
            return str(final_path)
            
        except Exception as e:
            log.error(f"[{self.session_id}] Final mix error: {e}", exc_info=True)
            return None

    def _background_loop(self):
        """Main loop: ensures chunks are ready"""
        while self.running:
            with self.lock:
                # Promote preloaded chunk to current if needed
                if not self.current_chunk_path and self.preloaded_chunks:
                    chunk_info = self.preloaded_chunks.pop(0)
                    self.current_chunk_path = chunk_info['path']
                    self.current_chunk_quality = chunk_info.get('quality', 'none')
                    log.info(f"[{self.session_id}] Promoted chunk: {self.current_chunk_path} ({self.current_chunk_quality})")
                
                # Keep 2 chunks ahead
                should_prepare = len(self.preloaded_chunks) < 2
                if should_prepare:
                    next_idx = self.chunk_index + len(self.preloaded_chunks) + 1
            
            if should_prepare:
                try:
                    chunk_info = self.prepare_chunk(next_idx)
                    if chunk_info:
                        with self.lock:
                            self.preloaded_chunks.append(chunk_info)
                            log.info(f"[{self.session_id}] Preloaded: {chunk_info}")
                except Exception as e:
                    log.error(f"[{self.session_id}] Chunk prep error: {e}", exc_info=True)
            
            time.sleep(5)

    def prepare_chunk(self, index):
        """
        THREE-TIER chunk preparation:
        1. Immediate mix → 5-10 seconds, no normalization
        2. Quick mix → 30-60 seconds, fast normalization
        3. Final mix → 2-5 minutes, LUFS perfection
        """
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
        
        def collect_speech():
            try:
                s_tracks.extend(self._collect_tracks_for_chunk("speech", self.target_chunk_duration))
            except Exception as e:
                s_error.append(e)
        
        music_thread = threading.Thread(target=collect_music)
        speech_thread = threading.Thread(target=collect_speech)
        
        music_thread.start()
        speech_thread.start()
        
        music_thread.join()
        speech_thread.join()
        
        if m_error or s_error:
            log.error(f"[{self.session_id}] Collection errors: music={m_error}, speech={s_error}")
            return None
        
        if not m_tracks or not s_tracks: 
            log.error(f"[{self.session_id}] Failed to collect tracks for chunk {index}")
            return None

        # Temp files
        m_concat = self.my_chunk_dir / f"m_tmp_{index}.mp3"
        s_concat = self.my_chunk_dir / f"s_tmp_{index}.mp3"

        # Concatenate
        self._update_progress(index, 'concatenating', 0)
        if not self._concat(m_tracks, m_concat):
            return None
        if not self._concat(s_tracks, s_concat):
            return None
        
        # === THREE-TIER MIXING ===
        
        # 1. IMMEDIATE mix (streams in seconds!)
        immediate_path = self._prepare_immediate_mix(index, m_concat, s_concat)
    
        if not immediate_path:
            log.error(f"[{self.session_id}] Immediate mix failed completely!")
            # Cleanup
            m_concat.unlink(missing_ok=True)
            s_concat.unlink(missing_ok=True)
            for t in m_tracks + s_tracks: 
                t.unlink(missing_ok=True)
            return None  # DON'T return a chunk info if we have nothing
        
        log.info(f"[{self.session_id}] ⚡ Chunk {index} ready for IMMEDIATE streaming")
        
        # 2 & 3. Launch upgrade pipeline in background
        def upgrade_pipeline():
            quick_path = self._prepare_quick_mix(index, m_concat, s_concat)
            
            if quick_path:
                with self.lock:
                    # Upgrade current chunk if it's the immediate version
                    if self.current_chunk_path == immediate_path:
                        log.info(f"[{self.session_id}] Upgrading current chunk to QUICK")
                        self.current_chunk_path = quick_path
                        self.current_chunk_quality = 'quick'
                    
                    # Update preloaded chunks
                    for i, chunk_info in enumerate(self.preloaded_chunks):
                        if chunk_info['path'] == immediate_path:
                            self.preloaded_chunks[i] = {
                                'path': quick_path,
                                'quality': 'quick',
                                'index': index
                            }
                            break
                
                # Delete immediate version
                if immediate_path:
                    Path(immediate_path).unlink(missing_ok=True)
                log.info(f"[{self.session_id}] ✓ Upgraded to QUICK mix")
                
                # Now start FINAL mix
                final_path = self._prepare_final_mix(index, m_concat, s_concat)
                
                if final_path:
                    with self.lock:
                        # Upgrade current chunk
                        if self.current_chunk_path == quick_path:
                            log.info(f"[{self.session_id}] Upgrading current chunk to FINAL")
                            self.current_chunk_path = final_path
                            self.current_chunk_quality = 'final'
                        
                        # Update preloaded chunks
                        for i, chunk_info in enumerate(self.preloaded_chunks):
                            if chunk_info['path'] == quick_path:
                                self.preloaded_chunks[i] = {
                                    'path': final_path,
                                    'quality': 'final',
                                    'index': index
                                }
                                break
                    
                    # Cleanup
                    Path(quick_path).unlink(missing_ok=True)
                    log.info(f"[{self.session_id}] ✨ Upgraded to FINAL mix")
                
                # Cleanup temps
                m_concat.unlink(missing_ok=True)
                s_concat.unlink(missing_ok=True)
                for t in m_tracks + s_tracks: 
                    t.unlink(missing_ok=True)
        
        # Launch upgrade pipeline in background
        threading.Thread(target=upgrade_pipeline, daemon=True).start()
        
        # Return immediate mix ONLY if it exists
        return {
            'path': immediate_path,
            'quality': 'immediate',
            'index': index
        }
    
    def stop(self):
        """Stop the worker thread"""
        self.running = False
        if self.thread.is_alive():
            self.thread.join(timeout=5)
    
    def get_current_chunk(self):
        """Get the current chunk path for streaming"""
        with self.lock:
            return self.current_chunk_path
    
    def advance_to_next_chunk(self):
        """Move to the next chunk"""
        with self.lock:
            # Delete old chunk
            if self.current_chunk_path and Path(self.current_chunk_path).exists():
                try:
                    Path(self.current_chunk_path).unlink()
                    log.info(f"[{self.session_id}] Cleaned up old chunk")
                except Exception as e:
                    log.warning(f"[{self.session_id}] Failed to cleanup: {e}")
            
            # Advance
            if self.preloaded_chunks:
                chunk_info = self.preloaded_chunks.pop(0)
                self.current_chunk_path = chunk_info['path']
                self.current_chunk_quality = chunk_info.get('quality', 'none')
                self.chunk_index += 1
                log.info(f"[{self.session_id}] Advanced to chunk {self.chunk_index}: {self.current_chunk_quality}")
                return True
            else:
                log.error(f"[{self.session_id}] No preloaded chunks available")
                self.current_chunk_path = None
                return False
    
    def get_status(self):
        """Get worker status with quality info"""
        with self.lock:
            return {
                "chunk_index": self.chunk_index,
                "current_chunk": self.current_chunk_path,
                "current_chunk_quality": self.current_chunk_quality,
                "preloaded_count": len(self.preloaded_chunks),
                "music_queue_size": len(self.music_queue),
                "speech_queue_size": len(self.speech_queue),
                "mix_progress": dict(self.mix_progress)
            }