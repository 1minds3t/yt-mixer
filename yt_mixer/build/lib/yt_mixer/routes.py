import logging
from pathlib import Path
from flask import Flask, render_template, request, send_file, jsonify, redirect
from .session_manager import manager
from .config import config
import time


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
    2. User has session ID -> load that session
    3. No params -> show form
    """
    sid = request.args.get('sid')
    m_id = request.args.get('m')
    s_id = request.args.get('s')
    
    # Scenario 1: Create new session from playlist IDs
    if m_id and s_id:
        new_sid, _ = manager.get_or_create_session(m_id, s_id)
        return redirect(f"/?sid={new_sid}")
    
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
        return jsonify({
            "session_id": sid,
            "chunk_index": worker.chunk_index,
            "current_chunk": str(worker.current_chunk_path) if worker.current_chunk_path else None,
            "current_chunk_quality": worker.current_chunk_quality,  # FIXED
            "preloaded_count": len(worker.preloaded_chunks),
            "music_queue_size": len(worker.music_queue),
            "speech_queue_size": len(worker.speech_queue),
            "mix_progress": worker.mix_progress
        })

@app.route('/api/status/<sid>')
def status_by_id(sid):
    """Get status of a specific session"""
    active = manager.get_active_session()
    
    if not active or active[0] != sid:
        return jsonify(error="Session not active"), 404
    
    _, worker = active
    
    with worker.lock:
        return jsonify({
            "session_id": sid,
            "chunk_index": worker.chunk_index,
            "current_chunk": str(worker.current_chunk_path) if worker.current_chunk_path else None,
            "current_chunk_quality": worker.current_chunk_quality,  # FIXED
            "preloaded_count": len(worker.preloaded_chunks),
            "music_queue_size": len(worker.music_queue),
            "speech_queue_size": len(worker.speech_queue),
            "mix_progress": worker.mix_progress
        })

@app.route('/api/sessions')
def list_sessions():
    """List all sessions on disk"""
    sessions = manager.list_sessions()
    return jsonify(sessions=sessions)

# ============================================================================
# AUDIO STREAMING ROUTES
# ============================================================================

@app.route('/stream')
def stream_current():
    """
    Stream the current chunk of the active session
    NOW: WAITS for chunk to be ready (up to 60 seconds)
    """
    active = manager.get_active_session()
    
    if not active:
        return "No active session", 404
    
    _, worker = active
    
    # WAIT for up to 60 seconds for a chunk to be ready
    max_wait = 60
    wait_interval = 0.5
    waited = 0
    
    while waited < max_wait:
        with worker.lock:
            if worker.current_chunk_path and Path(worker.current_chunk_path).exists():
                chunk_path = Path(worker.current_chunk_path)
                
                # Log quality level for monitoring
                quality_map = {
                    'immediate': '⚡ IMMEDIATE',
                    'quick': '📊 QUICK', 
                    'final': '✨ FINAL'
                }
                quality = quality_map.get(worker.current_chunk_quality, 'UNKNOWN')
                log.info(f"Streaming {quality} quality chunk: {chunk_path.name}")
                
                return send_file(worker.current_chunk_path, mimetype='audio/mpeg')
            
            # Try to promote a preloaded chunk
            if worker.preloaded_chunks:
                chunk_info = worker.preloaded_chunks.pop(0)
                worker.current_chunk_path = chunk_info['path']
                worker.current_chunk_quality = chunk_info.get('quality', 'none')
                log.info(f"Promoted chunk to current: {worker.current_chunk_path}")
                
                if Path(worker.current_chunk_path).exists():
                    return send_file(worker.current_chunk_path, mimetype='audio/mpeg')
        
        # Wait a bit and try again
        time.sleep(wait_interval)
        waited += wait_interval
    
    # Timeout after max_wait
    return jsonify(
        error="Audio not ready yet",
        hint="First chunk is still being prepared, please wait..."
    ), 503

@app.route('/stream/<sid>')
def stream_by_session(sid):
    """Stream current chunk of a specific session"""
    active = manager.get_active_session()
    
    if not active or active[0] != sid:
        # Try to resurrect session from disk
        chunk_dir = Path(config.get('chunk_dir', 'data/mixed_chunks')) / sid
        if chunk_dir.exists():
            chunks = sorted(chunk_dir.glob('*.mp3'))
            if chunks:
                return send_file(chunks[0], mimetype='audio/mpeg')
        
        return "Session not found", 404
    
    return stream_current()

@app.route('/stream/<sid>/<int:chunk_idx>')
def stream_specific_chunk(sid, chunk_idx):
    """Stream a specific chunk by index"""
    active = manager.get_active_session()
    
    if not active or active[0] != sid:
        return "Session not active", 404
    
    _, worker = active
    
    # Try final version first, fall back to quick, then immediate
    final_path = worker.my_chunk_dir / f"{chunk_idx}.mp3"
    quick_path = worker.my_chunk_dir / f"{chunk_idx}_quick.mp3"
    immediate_path = worker.my_chunk_dir / f"{chunk_idx}_immediate.mp3"
    
    if final_path.exists():
        return send_file(final_path, mimetype='audio/mpeg')
    elif quick_path.exists():
        return send_file(quick_path, mimetype='audio/mpeg')
    elif immediate_path.exists():
        return send_file(immediate_path, mimetype='audio/mpeg')
    
    return "Chunk not found", 404

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
                log.info(f"Cleaned up old chunk: {worker.current_chunk_path}")
            except Exception as e:
                log.warning(f"Failed to clean up: {e}")
        
        # Promote next chunk
        if worker.preloaded_chunks:
            chunk_info = worker.preloaded_chunks.pop(0)
            worker.current_chunk_path = chunk_info['path']
            worker.current_chunk_quality = chunk_info.get('quality', 'none')  # FIXED
            worker.chunk_index += 1
            
            log.info(f"Advanced to chunk {worker.chunk_index} (quality={worker.current_chunk_quality})")
            
            return jsonify(
                success=True,
                chunk_index=worker.chunk_index,
                quality=worker.current_chunk_quality,  # FIXED
                session_id=sid
            )
        else:
            return jsonify(
                success=False,
                error="No preloaded chunks available"
            ), 503

@app.route('/next/<sid>')
def next_chunk_by_session(sid):
    """Advance to next chunk in specific session"""
    active = manager.get_active_session()
    
    if not active or active[0] != sid:
        return jsonify(success=False, error="Session not active"), 404
    
    return next_chunk()

# ============================================================================
# MIXING PROGRESS ROUTE
# ============================================================================

@app.route('/api/progress')
def get_mixing_progress():
    """
    Get real-time mixing progress for UI updates
    Returns progress for all chunks being processed
    """
    active = manager.get_active_session()
    
    if not active:
        return jsonify(error="No active session"), 404
    
    _, worker = active
    
    with worker.lock:
        progress_info = {
            "current_chunk": worker.chunk_index,
            "chunks": {}
        }
        
        for chunk_idx, progress in worker.mix_progress.items():
            progress_info["chunks"][chunk_idx] = {
                "stage": progress.get('stage', 'unknown'),
                "percent": progress.get('percent', 0),
                "stage_name": _get_stage_name(progress.get('stage', ''))
            }
        
        return jsonify(progress_info)

def _get_stage_name(stage):
    """Convert stage code to human-readable name"""
    stages = {
        'collecting': '📥 Downloading tracks from playlists',
        'concatenating': '🔗 Combining audio files',
        'immediate_mix': '⚡ Generating IMMEDIATE mix (no normalization)',
        'quick_mix': '📊 Normalizing tracks separately (fast)',
        'final_mix': '✨ Mastering with LUFS (professional quality)'
    }
    return stages.get(stage, stage)

# ============================================================================
# VOLUME & EQ ROUTES (For future implementation)
# ============================================================================

@app.route('/api/volume/music/<int:percent>')
def set_music_volume(percent):
    """Set music volume (0-100)"""
    # TODO: Implement real-time volume control
    # For now, this would require re-mixing chunks
    vol = max(0, min(100, percent)) / 100.0
    return jsonify(success=True, volume=vol, note="Volume changes require remixing")

@app.route('/api/volume/speech/<int:percent>')
def set_speech_volume(percent):
    """Set speech volume (0-100)"""
    vol = max(0, min(100, percent)) / 100.0
    return jsonify(success=True, volume=vol, note="Volume changes require remixing")

# ============================================================================
# SESSION MANAGEMENT ROUTES
# ============================================================================

@app.route('/api/session/<sid>/delete', methods=['POST'])
def delete_session(sid):
    """Delete a session and all its data"""
    try:
        manager.delete_session(sid)
        return jsonify(success=True, message=f"Deleted session {sid}")
    except Exception as e:
        log.error(f"Error deleting session: {e}")
        return jsonify(success=False, error=str(e)), 500

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
            current_chunk_quality=worker.current_chunk_quality,  # FIXED
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
    
    log.info(f"Starting YT Mixer on http://{host}:{actual_port}")
    log.info(f"Access via: http://localhost:{actual_port}")
    log.info(f"NEW: Three-tier streaming! IMMEDIATE → QUICK → FINAL")
    
    # Ensure manager's cleanup thread is running
    manager.start_maintenance()
    
    try:
        app.run(host=host, port=actual_port, debug=debug, use_reloader=False, threaded=True)
    finally:
        log.info("Shutting down YT Mixer...")
        manager.shutdown()
        release_port(actual_port)

if __name__ == '__main__':
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    start_server()