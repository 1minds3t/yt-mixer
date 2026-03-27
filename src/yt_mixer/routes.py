import logging
from pathlib import Path
from flask import Flask, render_template, request, send_file, jsonify, redirect
from .session_manager import manager
from .config import config
import time
from flask import Response
import json
log = logging.getLogger(__name__)

app = Flask(__name__)

# ============================================================================
# MAIN PAGE ROUTES
# ============================================================================

@app.route('/')
def index():
    """
    Main page - handles three scenarios:
    1. User provides music + speech playlist IDs -> create session and redirect
    2. User has session ID (bookmark) -> restore that session
    3. No params -> show form
    """
    sid = request.args.get('sid')
    m_id = request.args.get('m')
    s_id = request.args.get('s')
    
    # Scenario 1: Create new session from playlist IDs
    if m_id and s_id:
        log.info(f"Creating new session: music={m_id}, speech={s_id}")
        new_sid, worker = manager.get_or_create_session(m_id, s_id)
        if worker:
            log.info(f"Session created: {new_sid}")
            return redirect(f"/?sid={new_sid}")
        else:
            return "Error creating session", 500
    
    # Scenario 2: Load existing session from bookmark
    if sid:
        log.info(f"Loading session from bookmark: {sid}")
        loaded_sid, worker = manager.load_session_by_id(sid)
        
        if not worker:
            log.error(f"Failed to load session {sid}")
            return render_template('mixer.html', error=f"Session {sid} not found or expired")
        
        log.info(f"Session {sid} loaded successfully")
    
    # Scenario 2 & 3: Show page (with or without session ID)
    return render_template('mixer.html', sid=sid)

# ============================================================================
# SESSION/STATUS ROUTES
# ============================================================================

@app.route('/api/status')
def status_global():
    """Get status of currently active session with mixing progress"""
    active = manager.get_active_session()
    
    if not active:
        return jsonify(error="No active session"), 404
    
    sid, worker = active
    
    with worker.lock:
        # Get error log if available
        errors = getattr(worker, 'error_log', [])
        
        return jsonify({
            "session_id": sid,
            "chunk_index": worker.chunk_index,
            "current_chunk": str(worker.current_chunk_path) if worker.current_chunk_path else None,
            "current_chunk_quality": worker.current_chunk_quality,
            "preloaded_count": len(worker.preloaded_chunks),
            "music_queue_size": len(worker.music_queue),
            "speech_queue_size": len(worker.speech_queue),
            "mix_progress": worker.mix_progress,
            "errors": errors[-5:]  # Last 5 errors
        })

@app.route('/api/status/<sid>')
def status_by_id(sid):
    """Get status of a specific session"""
    active = manager.get_active_session()
    
    if not active or active[0] != sid:
        return jsonify(error="Session not active"), 404
    
    _, worker = active
    
    with worker.lock:
        errors = getattr(worker, 'error_log', [])
        
        return jsonify({
            "session_id": sid,
            "chunk_index": worker.chunk_index,
            "current_chunk": str(worker.current_chunk_path) if worker.current_chunk_path else None,
            "current_chunk_quality": worker.current_chunk_quality,
            "preloaded_count": len(worker.preloaded_chunks),
            "music_queue_size": len(worker.music_queue),
            "speech_queue_size": len(worker.speech_queue),
            "mix_progress": worker.mix_progress,
            "errors": errors[-5:]
        })

@app.route('/api/sessions')
def list_sessions():
    """List all sessions on disk"""
    sessions = manager.list_sessions()
    return jsonify(sessions=sessions)

@app.route('/api/client-log', methods=['POST'])
def client_log():
    """Receive browser/mobile console logs and write them to the server log file.
    This is the only way to see what's happening on iOS where DevTools aren't available."""
    data = request.get_json(silent=True) or {}
    level   = data.get('level', 'info').lower()
    message = data.get('message', '')
    if not message:
        return jsonify(ok=True)
    prefix = f"[CLIENT-{level.upper()}]"
    if level == 'error':
        log.error(f"{prefix} {message}")
    elif level == 'warn':
        log.warning(f"{prefix} {message}")
    else:
        log.info(f"{prefix} {message}")
    return jsonify(ok=True)


@app.route('/api/logs')
def get_recent_logs():
    """Get recent log entries for debugging"""
    try:
        log_file = manager.log_file
        if not log_file.exists():
            return jsonify(logs=[])
        
        # Read last 100 lines
        with open(log_file, 'r') as f:
            lines = f.readlines()
            recent = lines[-100:]
        
        return jsonify(logs=recent)
    except Exception as e:
        return jsonify(error=str(e)), 500

# ============================================================================
# AUDIO STREAMING ROUTES
# ============================================================================

@app.route('/stream')
def stream_current():
    """
    Stream the current chunk of the active session
    WAITS for chunk to be ready (up to 60 seconds)
    """
    active = manager.get_active_session()
    
    if not active:
        log.error("Stream request with no active session")
        return jsonify(error="No active session"), 404
    
    sid, worker = active
    
    # WAIT for up to 60 seconds for a chunk to be ready
    max_wait = 60
    wait_interval = 0.5
    waited = 0
    
    log.info(f"[{sid}] Stream request - waiting for chunk...")
    
    while waited < max_wait:
        with worker.lock:
            # Check if current chunk exists
            if worker.current_chunk_path and Path(worker.current_chunk_path).exists():
                chunk_path = Path(worker.current_chunk_path)
                
                quality_map = {
                    'immediate': '⚡ IMMEDIATE',
                    'quick': '📊 QUICK', 
                    'final': '✨ FINAL'
                }
                quality = quality_map.get(worker.current_chunk_quality, 'UNKNOWN')
                log.info(f"[{sid}] Streaming {quality} quality: {chunk_path.name}")
                
                return send_file(worker.current_chunk_path, mimetype='audio/mpeg')
            
            # Try to promote a preloaded chunk
            if worker.preloaded_chunks:
                chunk_info = worker.preloaded_chunks.pop(0)
                worker.current_chunk_path = chunk_info['path']
                worker.current_chunk_quality = chunk_info.get('quality', 'none')
                worker.chunk_index += 1
                log.info(f"[{sid}] Promoted chunk {worker.chunk_index} to current (quality={worker.current_chunk_quality})")
                
                if Path(worker.current_chunk_path).exists():
                    return send_file(worker.current_chunk_path, mimetype='audio/mpeg')
        
        # Wait a bit and try again
        time.sleep(wait_interval)
        waited += wait_interval
        
        if waited % 5 == 0:  # Log every 5 seconds
            log.info(f"[{sid}] Still waiting for chunk... ({waited}s)")
    
    # Timeout after max_wait
    log.error(f"[{sid}] Stream timeout after {max_wait}s - no chunk ready")
    return jsonify(
        error="Audio not ready yet",
        hint="First chunk is still being prepared. This can take 10-30 seconds.",
        waited=waited
    ), 503

@app.route('/stream/<sid>')
def stream_by_session(sid):
    """Stream current chunk of a specific session"""
    active = manager.get_active_session()
    
    if not active or active[0] != sid:
        log.warning(f"Stream request for inactive session {sid}")
        return jsonify(error="Session not active"), 404
    
    return stream_current()

# ============================================================================
# VIDEO ROUTES
# ============================================================================

@app.route('/api/video/resolve', methods=['POST'])
def resolve_video():
    """
    Resolve a YouTube video URL to metadata for the video overlay panel.
    Returns: video_id, title, thumbnail, duration, has_captions.
    The client uses video_id to build a muted iframe embed (YouTube player),
    so we get CC + YouTube UI for free without any audio routing on the server.
    """
    import yt_dlp

    data = request.get_json()
    url = (data or {}).get('url', '').strip()

    if not url:
        return jsonify(error="Missing url"), 400

    # Accept full URLs or bare video IDs
    if not url.startswith('http'):
        url = f'https://www.youtube.com/watch?v={url}'

    # Strip tracking params
    if '&si=' in url:
        url = url.split('&si=')[0]

    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'skip_download': True,
        'extract_flat': False,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)

        if not info:
            return jsonify(error="Could not extract video info"), 400

        video_id = info.get('id')
        title = info.get('title', 'Unknown')
        duration = info.get('duration', 0)
        thumbnail = info.get('thumbnail', '')
        # Check for subtitles/captions
        subtitles = info.get('subtitles', {})
        auto_captions = info.get('automatic_captions', {})
        has_captions = bool(subtitles) or bool(auto_captions)
        caption_langs = list(subtitles.keys()) + [f"{k} (auto)" for k in auto_captions.keys()]

        log.info(f"Resolved video: {video_id} - {title} ({duration}s) captions={has_captions}")

        return jsonify(
            video_id=video_id,
            title=title,
            duration=duration,
            thumbnail=thumbnail,
            has_captions=has_captions,
            caption_langs=caption_langs[:5],  # first 5 for display
        )

    except Exception as e:
        log.error(f"Video resolve error: {e}")
        return jsonify(error=str(e)), 500


@app.route('/api/video/stream-url', methods=['POST'])
def video_stream_url():
    """
    Extract a direct streamable video URL for a YouTube video.
    Returns the best combined video+audio stream URL that the browser
    can use as a <video src="..."> directly — no download, no proxy.
    Also returns metadata (title, duration, thumbnail) in one call.
    """
    import yt_dlp

    data = request.get_json()
    url = (data or {}).get('url', '').strip()

    if not url:
        return jsonify(error="Missing url"), 400

    if not url.startswith('http'):
        url = f'https://www.youtube.com/watch?v={url}'

    # Normalise: strip tracking/playlist params, keep only video
    from urllib.parse import urlparse, parse_qs, urlencode
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    vid = (qs.get('v') or [None])[0]
    if vid:
        url = f'https://www.youtube.com/watch?v={vid}'

    # Extract video-only stream + audio-only stream separately.
    # Video element plays muted. Audio element plays the audio stream.
    # This gives iOS two native <audio> elements we fully control,
    # instead of one <video> that iOS audio session treats as king.
    ydl_opts_video = {
        'quiet': True,
        'no_warnings': True,
        'skip_download': True,
        'format': 'bestvideo[ext=mp4]/bestvideo',
    }
    ydl_opts_audio = {
        'quiet': True,
        'no_warnings': True,
        'skip_download': True,
        'format': 'bestaudio[ext=m4a]/bestaudio',
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts_video) as ydl:
            info = ydl.extract_info(url, download=False)

        if not info:
            return jsonify(error="Could not extract video info"), 400

        stream_url = info.get('url')
        if not stream_url:
            return jsonify(error="Could not extract video stream URL"), 400

        # Extract audio-only stream
        with yt_dlp.YoutubeDL(ydl_opts_audio) as ydl:
            audio_info = ydl.extract_info(url, download=False)
        audio_url = audio_info.get('url') if audio_info else None

        video_id  = info.get('id', '')
        title     = info.get('title', 'Unknown')
        duration  = info.get('duration', 0)
        thumbnail = info.get('thumbnail', '')
        subtitles = info.get('subtitles', {})
        auto_captions = info.get('automatic_captions', {})
        has_captions = bool(subtitles) or bool(auto_captions)

        log.info(f"Stream URL extracted: {video_id} - {title} ({duration}s) audio={'yes' if audio_url else 'no'}")

        return jsonify(
            stream_url=stream_url,
            audio_url=audio_url,
            video_id=video_id,
            title=title,
            duration=duration,
            thumbnail=thumbnail,
            has_captions=has_captions,
        )

    except Exception as e:
        log.error(f"Video stream-url error: {e}")
        return jsonify(error=str(e)), 500

from . import video_engine as _ve
from .video_engine import ConflictError, NotFoundError, StateError
 
@app.route('/api/adhoc/prepare', methods=['POST'])
def adhoc_prepare():
    """
    Phase A — validate URL, start background download.
    Body: { "url": "<youtube url or video id>", "session_id": "<sid>" }
    Returns: { "job_id": "...", "status": "preparing_external" }
    """
    data       = request.get_json() or {}
    url        = data.get('url', '').strip()
    session_id = data.get('session_id', '').strip()
 
    if not url:
        return jsonify(error="Missing url"), 400
    if not session_id:
        return jsonify(error="Missing session_id"), 400
 
    # Verify session exists
    active = manager.get_active_session()
    if not active or active[0] != session_id:
        return jsonify(error="Session not active"), 404
 
    try:
        result = _ve.prepare_job(session_id, url)
        return jsonify(result), 202
    except ConflictError as e:
        return jsonify(error=str(e)), 409
    except ValueError as e:
        return jsonify(error=str(e)), 400
    except Exception as e:
        log.error(f"adhoc_prepare error: {e}")
        return jsonify(error=str(e)), 500
 
 
@app.route('/api/adhoc/status/<job_id>')
def adhoc_status(job_id):
    """
    Poll this every 3 seconds.
    Returns: { job_id, status, title, duration, error? }
    Statuses:
      preparing_external  → Phase A running (audio keeps playing)
      finalizing          → Phase A done; client should pause + POST to /finalize
      rendering           → Phase B running
      ready               → final_mix.mp3 ready; show Play button
      failed              → Phase A failed; audio never interrupted
      finalize_failed     → Phase B failed; client should resume from anchor
      cancelled           → user cancelled
    """
    try:
        status = _ve.get_job_status(job_id)
        return jsonify({
            "job_id":   status["job_id"],
            "status":   status["status"],
            "title":    status.get("title"),
            "duration": status.get("duration"),
            "error":    status.get("error"),
        })
    except NotFoundError as e:
        return jsonify(error=str(e)), 404
    except Exception as e:
        return jsonify(error=str(e)), 500
 
 
@app.route('/api/adhoc/finalize/<job_id>', methods=['POST'])
def adhoc_finalize(job_id):
    """
    Phase B — client sends its current absolute timeline position.
    Server slices the background, mixes it with YT audio, produces final_mix.mp3.
 
    Body: { "absolute_sec": <float> }
 
    On success: { status: "ready", job_id, anchor_absolute_sec }
    On failure: { status: "finalize_failed", error }
    """
    data = request.get_json() or {}
 
    try:
        absolute_sec = float(data['absolute_sec'])
    except (KeyError, TypeError, ValueError):
        return jsonify(error="Missing or invalid absolute_sec"), 400
 
    try:
        status = _ve.get_job_status(job_id)
    except NotFoundError as e:
        return jsonify(error=str(e)), 404
 
    session_id = status.get("session_id")
    chunk_dir  = manager.get_chunk_dir_for_session(session_id)
 
    try:
        result = _ve.finalize_job(job_id, absolute_sec, chunk_dir)
        return jsonify(result)
    except StateError as e:
        return jsonify(error=str(e)), 409
    except Exception as e:
        log.error(f"adhoc_finalize error: {e}")
        return jsonify(error=str(e), status="finalize_failed"), 500
 
 
@app.route('/api/adhoc/stream/<job_id>')
def adhoc_stream(job_id):
    """
    Serve the final mixed .mp3 for the <audio> element.
    The client sets audio.src = this URL after seeing status = "ready".
    """
    try:
        path = _ve.stream_path(job_id)
        return send_file(path, mimetype='audio/mpeg')
    except (NotFoundError, StateError) as e:
        return jsonify(error=str(e)), 404
    except Exception as e:
        log.error(f"adhoc_stream error: {e}")
        return jsonify(error=str(e)), 500
 
 
@app.route('/api/adhoc/deactivate/<job_id>', methods=['POST'])
def adhoc_deactivate(job_id):
    """
    User finished listening to the ad-hoc mix.
    Body: { "played_seconds": <float> }
 
    Returns: { "resume_absolute_sec": <float> }
    Route advances the session timeline, client switches back to /stream.
    """
    data = request.get_json() or {}
    played = float(data.get('played_seconds', 0))
 
    try:
        result = _ve.deactivate_job(job_id, played)
    except (NotFoundError, StateError) as e:
        return jsonify(error=str(e)), 404
    except Exception as e:
        return jsonify(error=str(e)), 500
 
    # Persist the advanced position to session_manager so /stream resumes correctly
    try:
        status     = _ve.get_job_status(job_id)
        session_id = status.get("session_id")
        if session_id:
            manager.advance_timeline_to(session_id, result["resume_absolute_sec"])
    except Exception as e:
        log.warning(f"Could not advance timeline: {e}")
 
    return jsonify(result)
 
 
@app.route('/api/adhoc/cancel/<job_id>', methods=['POST'])
def adhoc_cancel(job_id):
    """Kill Phase A subprocess if running, clean up, restore user to audio mode."""
    try:
        _ve.cancel_job(job_id)
        return jsonify(ok=True)
    except Exception as e:
        return jsonify(error=str(e)), 500
 
# ============================================================================
# PLAYBACK CONTROL ROUTES
# ============================================================================

@app.route('/next')
def next_chunk():
    """Advance to next chunk in active session"""
    active = manager.get_active_session()
    
    if not active:
        return jsonify(success=False, error="No active session"), 404
    
    sid, worker = active
    
    with worker.lock:
        # Clean up old current chunk
        if worker.current_chunk_path and Path(worker.current_chunk_path).exists():
            try:
                Path(worker.current_chunk_path).unlink()
                log.info(f"[{sid}] Cleaned up old chunk")
            except Exception as e:
                log.warning(f"[{sid}] Failed to clean up: {e}")
        
        # Promote next chunk
        if worker.preloaded_chunks:
            chunk_info = worker.preloaded_chunks.pop(0)
            worker.current_chunk_path = chunk_info['path']
            worker.current_chunk_quality = chunk_info.get('quality', 'none')
            worker.chunk_index += 1
            
            log.info(f"[{sid}] Advanced to chunk {worker.chunk_index} (quality={worker.current_chunk_quality})")
            
            return jsonify(
                success=True,
                chunk_index=worker.chunk_index,
                quality=worker.current_chunk_quality,
                session_id=sid
            )
        else:
            log.warning(f"[{sid}] No preloaded chunks available")
            return jsonify(
                success=False,
                error="No preloaded chunks available"
            ), 503

# ============================================================================
# SESSION MANAGEMENT ROUTES
# ============================================================================

@app.route('/api/session/<sid>/delete', methods=['POST'])
def delete_session(sid):
    """Delete a session and all its data"""
    try:
        log.info(f"Deleting session {sid}")
        manager.delete_session(sid)
        return jsonify(success=True, message=f"Deleted session {sid}")
    except Exception as e:
        log.error(f"Error deleting session {sid}: {e}")
        return jsonify(success=False, error=str(e)), 500

# ============================================================================
# PLAYBACK STATE PERSISTENCE ROUTES
# ============================================================================

@app.route('/api/playback/position', methods=['POST'])
def update_position():
    """Update playback position (called periodically from client every 5s)"""
    data = request.get_json()
    sid = data.get('session_id')
    chunk_idx = data.get('chunk_index', 0)
    position = data.get('position', 0)
    
    if not sid:
        return jsonify(error="Missing session_id"), 400
    
    try:
        manager.update_playback_position(sid, chunk_idx, position)
        return jsonify(success=True)
    except Exception as e:
        log.error(f"Error updating playback position: {e}")
        return jsonify(error=str(e)), 500

@app.route('/api/playback/position/<sid>')
def get_position(sid):
    """Get saved playback position for resuming after browser crash/close"""
    try:
        position = manager.get_playback_position(sid)
        return jsonify(position)
    except Exception as e:
        log.error(f"Error getting playback position: {e}")
        return jsonify(error=str(e)), 500

@app.route('/api/session/active')
def get_active_session():
    """Get info about currently active session"""
    active = manager.get_active_session()
    
    if not active:
        return jsonify(active=False)
    
    sid, worker = active
    
    with worker.lock:
        return jsonify(
            active=True,
            session_id=sid,
            chunk_index=worker.chunk_index,
            current_chunk_quality=worker.current_chunk_quality,
            ready_chunks=len(worker.preloaded_chunks)
        )

# ============================================================================
# STARTUP/SHUTDOWN
# ============================================================================

def start_server(host=None, port=None, debug=False):
    """Start the Flask server"""
    from .port_finder import get_available_port, release_port
    
    host = host or config.get('host', '0.0.0.0')
    preferred_port = port or config.get('port', 5052)
    
    # Find available port
    try:
        actual_port = get_available_port(preferred_port=preferred_port, start_range=5000)
        if actual_port != preferred_port:
            log.warning(f"Port {preferred_port} in use, using {actual_port} instead")
            config.set('port', actual_port)
    except RuntimeError as e:
        log.error(f"Could not find available port: {e}")
        return
    
    log.info(f"=== YT MIXER SERVER STARTING ===")
    log.info(f"URL: http://{host}:{actual_port}")
    log.info(f"Local: http://localhost:{actual_port}")
    log.info(f"Log file: {manager.log_file}")
    log.info(f"Three-tier streaming: IMMEDIATE → QUICK → FINAL")
    
    # Ensure manager's cleanup thread is running
    manager.start_maintenance()
    
    try:
        app.run(host=host, port=actual_port, debug=debug, use_reloader=False, threaded=True)
    finally:
        log.info("Shutting down YT Mixer...")
        manager.shutdown()
        release_port(actual_port)

if __name__ == '__main__':
    start_server()