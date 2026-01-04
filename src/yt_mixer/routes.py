import logging
from pathlib import Path
from flask import Flask, render_template, request, send_file, jsonify, redirect
from .session_manager import manager
from .config import config
import time
from flask import Response

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

@app.route('/api/logs/stream')
def stream_logs():
    """Stream log file using Server-Sent Events (SSE)."""
    def generate_log_stream():
        log_file = manager.log_file
        if not log_file.exists():
            yield f"data: LOG FILE NOT FOUND: {log_file}\n\n"
            return

        with open(log_file, 'r') as f:
            # Go to the end of the file
            f.seek(0, 2)
            
            while True:
                line = f.readline()
                if line:
                    # SSE format: data: {message}\n\n
                    # We'll send JSON so the frontend can easily parse it
                    data = {"line": line.strip()}
                    yield f"data: {json.dumps(data)}\n\n"
                else:
                    # Send a ping every 10 seconds to keep connection alive
                    yield ": ping\n\n"
                    time.sleep(1) # Sleep when no new lines
                    
    # The mimetype for SSE is 'text/event-stream'
    return Response(generate_log_stream(), mimetype='text/event-stream')

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