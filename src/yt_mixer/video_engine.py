"""
video_engine.py — Ad-hoc YouTube audio injection engine

Phase A (background, audio keeps playing):
  - Download YouTube audio-only stream via yt-dlp
  - Normalize/probe duration

Phase B (live mix, triggered by client when ready):
  - Slice current background chunk window matching video duration
  - amix the two streams into a single injectable .mp3
  - Serve it at /api/adhoc/stream/<job_id>

On exit:
  - Client reports how many seconds were actually played
  - We advance the background timeline by that amount
  - Caller resumes normal /stream endpoint

One active job per session. No queue.
"""

import json
import logging
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config — tune these if needed
# ---------------------------------------------------------------------------
MAX_VIDEO_DURATION_SEC = 90 * 60   # 90 minutes
CHUNK_DURATION_SEC     = 3600      # must match audio_engine.py's chunk length
JOB_TIMEOUT_SEC        = 15 * 60   # kill jobs stuck > 15 min

# Populated in init() from the app's DATA_DIR
_JOBS_DIR:  Path | None = None
_CHUNK_DIR: Path | None = None
_AUDIO_DIR: Path | None = None   # raw per-session music files live here

# ---------------------------------------------------------------------------
# omnipkg daemon — optional, graceful fallback if not available
# ---------------------------------------------------------------------------
# Single DaemonClient instance shared across all calls.
# Workers are pinned (pin=True) so they survive idle timeout indefinitely.
# Two tagged workers:
#   ytdlp-info   — metadata probes (fast, many small calls)
#   ytdlp-dl     — actual downloads (long-running, needs its own process)
#
# yt-dlp version: update this when you upgrade yt-dlp in the env.
_YTDLP_SPEC = "yt-dlp==2026.3.17"   # no version pin — uses whatever is active in env
_daemon_client = None
_daemon_lock   = threading.Lock()

def _get_daemon():
    """Return the shared DaemonClient, or None if omnipkg is not available."""
    global _daemon_client
    if _daemon_client is not None:
        return _daemon_client
    with _daemon_lock:
        if _daemon_client is not None:   # double-check after lock
            return _daemon_client
        try:
            from omnipkg.isolation.worker_daemon import DaemonClient
            client = DaemonClient()
            status = client.status()
            if status.get("success"):
                _daemon_client = client
                log.info("video_engine: omnipkg daemon connected — yt-dlp workers will be pinned")
            else:
                log.info("video_engine: omnipkg daemon not running — using direct yt-dlp imports")
        except Exception as e:
            log.debug(f"video_engine: omnipkg not available ({e}) — using direct yt-dlp imports")
    return _daemon_client


def _start_daemon_keepalive():
    """
    Ping pinned workers every 4 minutes so they survive the default 5-min
    idle timeout even during long quiet periods (e.g. overnight).
    Only runs if daemon is available.
    """
    def _ping():
        # Wait a bit after startup before first ping
        time.sleep(30)
        while True:
            time.sleep(240)   # every 4 min — well under 5-min default TTL
            client = _get_daemon()
            if client:
                for tag in ("ytdlp-info", "ytdlp-dl"):
                    try:
                        client.execute_smart(
                            _YTDLP_SPEC,
                            "pass",
                            worker_tag=tag,
                            pin=True,
                        )
                    except Exception:
                        pass   # daemon might be restarting — silent, retry next cycle

    t = threading.Thread(target=_ping, daemon=True, name="ytdlp-keepalive")
    t.start()


def init(data_dir: Path, chunk_dir: Path, audio_dir: Path | None = None):
    """Call once at app startup to wire up paths."""
    global _JOBS_DIR, _CHUNK_DIR, _AUDIO_DIR
    _JOBS_DIR  = data_dir / "adhoc_jobs"
    _CHUNK_DIR = chunk_dir
    _AUDIO_DIR = audio_dir          # may be None on first deploy; graceful fallback
    _JOBS_DIR.mkdir(parents=True, exist_ok=True)
    log.info(f"video_engine: jobs dir = {_JOBS_DIR}")
    # Kick off cleanup of any stale jobs from a previous run
    _cleanup_stale()
    # Start daemon keepalive thread (no-op if daemon not available)
    _start_daemon_keepalive()


# ---------------------------------------------------------------------------
# In-memory job registry  { job_id -> dict }
# We write status.json to disk too so the client can always poll.
# ---------------------------------------------------------------------------
_jobs: dict = {}
_jobs_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_active_job_for_session(session_id: str) -> dict | None:
    with _jobs_lock:
        for job in _jobs.values():
            if job["session_id"] == session_id and job["status"] not in (
                "done", "failed", "cancelled", "finalize_failed"
            ):
                return dict(job)
    return None


def prepare_job(session_id: str, youtube_url: str) -> dict:
    """
    Validate, create job, start Phase A in background.
    Returns { job_id, status } immediately.
    Raises ValueError for bad input or 409-style conflict.
    """
    if not _JOBS_DIR:
        raise RuntimeError("video_engine.init() not called")

    # One active job per session
    existing = get_active_job_for_session(session_id)
    if existing:
        raise ConflictError(f"Job {existing['job_id']} is already active for this session")

    # Normalise URL
    url = _normalise_url(youtube_url)

    job_id = uuid.uuid4().hex[:12]
    job_dir = _JOBS_DIR / job_id
    job_dir.mkdir(parents=True)

    job = {
        "job_id":     job_id,
        "session_id": session_id,
        "url":        url,
        "status":     "preparing_external",
        "created_at": time.time(),
        "job_dir":    str(job_dir),
        # filled in by Phase A
        "title":      None,
        "duration":   None,
        # filled in by Phase B
        "anchor_absolute_sec": None,
        # subprocess handles for cancellation
        "_proc": None,
    }

    with _jobs_lock:
        _jobs[job_id] = job

    _write_status(job)

    t = threading.Thread(target=_run_phase_a, args=(job_id,), daemon=True)
    t.start()

    log.info(f"[{job_id}] Job created, Phase A started (session={session_id})")
    return {"job_id": job_id, "status": "preparing_external"}


def get_job_status(job_id: str) -> dict:
    with _jobs_lock:
        job = _jobs.get(job_id)
    if not job:
        # Try reading from disk (server restart scenario)
        status_path = _JOBS_DIR / job_id / "status.json"
        if status_path.exists():
            return json.loads(status_path.read_text())
        raise NotFoundError(f"Job {job_id} not found")
    return _public_status(job)


def mark_rendering(job_id: str):
    """
    Atomically transition a job from 'finalizing' -> 'rendering'.
    Called by the route handler immediately before spawning the Phase B thread,
    so the route can return 202 without waiting for ffmpeg to finish.
    Raises StateError if the job is not in 'finalizing' state.
    """
    with _jobs_lock:
        job = _jobs.get(job_id)
    if not job:
        raise NotFoundError(f"Job {job_id} not found")
    if job["status"] != "finalizing":
        raise StateError(
            f"Job {job_id} is in state '{job['status']}', expected 'finalizing'"
        )
    job["status"] = "rendering"
    _write_status(job)
    log.info(f"[{job_id}] Marked rendering (async Phase B will follow)")


def finalize_job(job_id: str, absolute_sec: float, chunk_dir_for_session: Path) -> dict:
    """
    Phase B — runs in a background thread (spawned by the route after mark_rendering).
    Accepts status 'rendering' (set by mark_rendering) as well as the legacy
    'finalizing' path so the function stays usable if called directly in tests.
    """
    with _jobs_lock:
        job = _jobs.get(job_id)
    if not job:
        raise NotFoundError(f"Job {job_id} not found")
    if job["status"] not in ("finalizing", "rendering"):
        raise StateError(
            f"Job {job_id} is in state '{job['status']}', expected 'finalizing' or 'rendering'"
        )

    # Ensure rendering state + anchor are persisted (idempotent if mark_rendering ran first)
    job["status"]              = "rendering"
    job["anchor_absolute_sec"] = absolute_sec
    _write_status(job)

    try:
        _run_phase_b(job, absolute_sec, chunk_dir_for_session)
        job["status"] = "ready"
        _write_status(job)
        log.info(f"[{job_id}] Phase B complete — mix ready")
        return _public_status(job)
    except Exception as e:
        log.error(f"[{job_id}] Phase B failed: {e}")
        job["status"] = "finalize_failed"
        job["error"]  = str(e)
        _write_status(job)
        raise


def stream_path(job_id: str) -> tuple[Path, str]:
    """Return (path, mimetype) for the final media file."""
    with _jobs_lock:
        job = _jobs.get(job_id)
    if not job or job["status"] != "ready":
        raise StateError(f"Job {job_id} not ready for streaming")

    job_dir = Path(job["job_dir"])

    # Prefer the muxed MP4 (video + mixed audio)
    mp4 = job_dir / "final.mp4"
    if mp4.exists() and mp4.stat().st_size > 0:
        return mp4, "video/mp4"

    # Fall back to audio-only if mux failed or video wasn't available
    mp3 = job_dir / "final_mix.mp3"
    if mp3.exists():
        return mp3, "audio/mpeg"

    raise NotFoundError(f"No streamable file found for job {job_id}")


def deactivate_job(job_id: str, played_seconds: float) -> dict:
    """
    Called when user exits ad-hoc audio mode.
    Returns { resume_absolute_sec } so the route can advance the session timeline.
    """
    with _jobs_lock:
        job = _jobs.get(job_id)
    if not job:
        raise NotFoundError(f"Job {job_id} not found")

    anchor = job.get("anchor_absolute_sec")
    if anchor is None:
        raise StateError("Job was never finalized — no anchor position")

    resume_at = anchor + max(0.0, float(played_seconds))
    job["status"]        = "done"
    job["played_seconds"] = played_seconds
    job["resume_at"]     = resume_at
    _write_status(job)

    log.info(f"[{job_id}] Deactivated. Played {played_seconds:.1f}s. "
             f"Resume timeline at {resume_at:.1f}s")
    return {"resume_absolute_sec": resume_at}


def cancel_job(job_id: str):
    """Kill any running subprocess, mark cancelled."""
    with _jobs_lock:
        job = _jobs.get(job_id)
    if not job:
        return

    proc = job.get("_proc")
    if proc:
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        job["_proc"] = None

    job["status"] = "cancelled"
    _write_status(job)
    log.info(f"[{job_id}] Cancelled")


# ---------------------------------------------------------------------------
# Phase A — download YT audio (background thread, user's audio keeps playing)
# ---------------------------------------------------------------------------

def _run_phase_a(job_id: str):
    with _jobs_lock:
        job = _jobs[job_id]

    job_dir = Path(job["job_dir"])
    url     = job["url"]

    try:
        # ── 1. Metadata probe ──────────────────────────────────────
        log.info(f"[{job_id}] Phase A: probing metadata...")
        info = _yt_dlp_info(url)

        if info.get("is_live"):
            raise ValueError("Livestreams are not supported")

        duration = info.get("duration") or 0
        if duration > MAX_VIDEO_DURATION_SEC:
            raise ValueError(
                f"Video too long ({duration//60:.0f} min). Max is {MAX_VIDEO_DURATION_SEC//60} min."
            )

        job["title"]    = info.get("title", "Unknown")
        job["duration"] = duration
        _write_status(job)

        # ── 2a. Start fresh music-bg build in parallel ─────────────
        music_bg_path = job_dir / "music_bg.mp3"
        music_bg_ready = threading.Event()
        music_bg_error = []

        def _build_music_bg():
            try:
                session_id = job["session_id"]
                _collect_music_for_video(
                    job_id, session_id, duration, music_bg_path
                )
                music_bg_ready.set()
            except Exception as e:
                log.warning(f"[{job_id}] Music-bg build failed: {e} — will fall back to chunk slice")
                music_bg_error.append(str(e))
                music_bg_ready.set()

        music_bg_thread = threading.Thread(target=_build_music_bg, daemon=True)
        music_bg_thread.start()

        # ── 2b. Download YT audio-only AND video-only in parallel ─────
        log.info(f"[{job_id}] Phase A: downloading audio + video ({duration:.0f}s)...")
        yt_audio_path = job_dir / "yt_audio.m4a"
        yt_video_path = job_dir / "yt_video.mp4"

        video_dl_error = []
        video_dl_done  = threading.Event()

        def _download_video():
            try:
                _yt_dlp_download_video(job_id, url, yt_video_path, job)
                video_dl_done.set()
            except Exception as e:
                log.warning(f"[{job_id}] Video download failed: {e} — will serve audio-only")
                video_dl_error.append(str(e))
                video_dl_done.set()

        video_dl_thread = threading.Thread(target=_download_video, daemon=True)
        video_dl_thread.start()

        # Audio download runs in main Phase A thread
        _yt_dlp_download(job_id, url, yt_audio_path, job)

        if job["status"] == "cancelled":
            return

        # ── 3. Convert to mp3 at 44100 Hz for reliable concat ──────
        log.info(f"[{job_id}] Phase A: converting to mp3...")
        yt_mp3_path = job_dir / "yt_audio.mp3"
        _ffmpeg(
            job_id,
            ["-threads", "2",
             "-i", str(yt_audio_path),
             "-ar", "44100", "-ac", "2", "-b:a", "192k",
             "-y", str(yt_mp3_path)],
            job
        )

        if job["status"] == "cancelled":
            return

        # ── 4. Probe actual duration of the converted file ──────────
        real_dur = _probe_duration(yt_mp3_path)
        job["duration"] = real_dur

        # ── 5. Wait for music-bg and video download ──────────────────
        log.info(f"[{job_id}] Phase A: waiting for music-bg concat...")
        music_bg_ready.wait(timeout=300)
        job["music_bg_ok"] = (
            music_bg_path.exists()
            and music_bg_path.stat().st_size > 0
            and not music_bg_error
        )
        if job["music_bg_ok"]:
            log.info(f"[{job_id}] Music-bg ready ({music_bg_path.stat().st_size/1e6:.1f} MB)")
        else:
            log.warning(f"[{job_id}] Music-bg not ready — Phase B will fall back to chunk slice")

        log.info(f"[{job_id}] Phase A: waiting for video download...")
        video_dl_done.wait(timeout=600)
        job["video_ok"] = (
            yt_video_path.exists()
            and yt_video_path.stat().st_size > 0
            and not video_dl_error
        )
        if job["video_ok"]:
            log.info(f"[{job_id}] Video ready ({yt_video_path.stat().st_size/1e6:.1f} MB)")
        else:
            log.warning(f"[{job_id}] Video not ready — Phase B will produce audio-only")

        log.info(f"[{job_id}] Phase A complete. Duration: {real_dur:.1f}s, title: {job['title']}")
        job["status"] = "finalizing"
        _write_status(job)

    except Exception as e:
        if job["status"] != "cancelled":
            log.error(f"[{job_id}] Phase A failed: {e}")
            job["status"] = "failed"
            job["error"]  = str(e)
            _write_status(job)


def _collect_music_for_video(job_id: str, session_id: str,
                              target_duration: float, out_path: Path):
    """
    Pull music tracks from AUDIO_DIR/session_id (already downloaded by AudioWorker),
    concat enough to cover target_duration seconds, looping the available pool if
    needed. Applies a quick dynaudnorm pass so levels are consistent.
    """
    audio_dir = (_AUDIO_DIR / session_id) if _AUDIO_DIR else None
    if not audio_dir or not audio_dir.exists():
        raise FileNotFoundError(f"Audio dir not found: {audio_dir}")

    music_files = sorted(audio_dir.glob("music_*.mp3"))
    if not music_files:
        raise FileNotFoundError(f"No music files found in {audio_dir}")

    log.info(f"[{job_id}] Music-bg: {len(music_files)} source files available, "
             f"need {target_duration:.0f}s")

    playlist: list[Path] = []
    total = 0.0
    pool = list(music_files)
    import random as _random
    _random.shuffle(pool)

    while total < target_duration:
        if not pool:
            pool = list(music_files)
            _random.shuffle(pool)
        f = pool.pop(0)
        dur = _probe_duration(f)
        if dur <= 0:
            continue
        playlist.append(f)
        total += dur

    log.info(f"[{job_id}] Music-bg playlist: {len(playlist)} tracks = {total:.0f}s")

    concat_txt = out_path.parent / "music_bg_list.txt"
    with open(concat_txt, "w") as fh:
        for p in playlist:
            fh.write(f"file '{p}'\n")

    result = subprocess.run(
        ["ffmpeg", "-y", "-threads", "2",
         "-f", "concat", "-safe", "0", "-i", str(concat_txt),
         "-t", str(target_duration),
         "-filter_complex",
         "[0:a]aresample=44100,dynaudnorm=f=150:g=15[out]",
         "-map", "[out]",
         "-c:a", "libmp3lame", "-b:a", "128k",
         str(out_path)],
        capture_output=True, timeout=600
    )
    concat_txt.unlink(missing_ok=True)

    if result.returncode != 0 or not out_path.exists() or out_path.stat().st_size == 0:
        raise RuntimeError(
            f"Music-bg ffmpeg failed: {result.stderr.decode()[-300:]}"
        )
    log.info(f"[{job_id}] Music-bg written: {out_path.stat().st_size/1e6:.1f} MB")


# ---------------------------------------------------------------------------
# Phase B — slice background + mix (runs synchronously in route handler)
# ---------------------------------------------------------------------------

def _run_phase_b(job: dict, absolute_sec: float, chunk_dir_for_session: Path):
    job_id   = job["job_id"]
    job_dir  = Path(job["job_dir"])
    duration = job["duration"]

    yt_mp3       = job_dir / "yt_audio.mp3"
    music_bg_mp3 = job_dir / "music_bg.mp3"
    bg_mp3       = job_dir / "bg_window.mp3"
    mix_mp3      = job_dir / "final_mix.mp3"

    # ── 1. Choose background source ────────────────────────────────
    if job.get("music_bg_ok") and music_bg_mp3.exists() and music_bg_mp3.stat().st_size > 0:
        bg_source = music_bg_mp3
        log.info(f"[{job_id}] Phase B: using fresh music-bg ({bg_source.stat().st_size/1e6:.1f} MB)")
    else:
        log.warning(f"[{job_id}] Phase B: music-bg unavailable — falling back to chunk slice")
        log.info(f"[{job_id}] Phase B: slicing background from abs={absolute_sec:.1f}s, dur={duration:.1f}s")
        _build_bg_window(job_id, absolute_sec, duration, chunk_dir_for_session, bg_mp3)
        bg_source = bg_mp3

    # ── 2. amix YT audio + music-only background ───────────────────
    log.info(f"[{job_id}] Phase B: mixing streams...")
    _ffmpeg(
        job_id,
        ["-threads", "2",
         "-i", str(yt_mp3),
         "-i", str(bg_source),
         "-filter_complex",
         "[0:a]aresample=44100,dynaudnorm=f=150:g=15[yt_norm];"
         "[1:a]aresample=44100[bg];"
         "[yt_norm][bg]amix=inputs=2:weights=1 0.35:duration=first,"
         "alimiter=limit=0.9:attack=5:release=50[out]",
         "-map", "[out]",
         "-c:a", "libmp3lame", "-b:a", "192k",
         "-y", str(mix_mp3)],
        job
    )
    log.info(f"[{job_id}] Phase B: mix written → {mix_mp3}")

    # ── 3. Mux mixed audio into video if video was downloaded ──────
    yt_video_path = job_dir / "yt_video.mp4"
    final_mp4     = job_dir / "final.mp4"

    if job.get("video_ok") and yt_video_path.exists() and yt_video_path.stat().st_size > 0:
        dur_min = job.get("duration", 0) / 60
        log.info(f"[{job_id}] Phase B: transcoding to H.264 ({dur_min:.1f} min video) — this takes 2-5 min, DO NOT click again")
        job["status"] = "rendering"
        job["render_phase"] = "transcode"
        _ffmpeg(
            job_id,
            ["-i", str(yt_video_path),
             "-i", str(mix_mp3),
             "-c:v", "h264_nvenc",
             "-preset", "p4",
             "-cq", "23",
             "-c:a", "aac",
             "-b:a", "192k",
             "-map", "0:v:0",
             "-map", "1:a:0",
             "-shortest",
             "-movflags", "+faststart",
             "-y", str(final_mp4)],
            job,
            timeout=900
        )
        job["has_video"] = True
        log.info(f"[{job_id}] Phase B: final.mp4 ready ({final_mp4.stat().st_size/1e6:.1f} MB)")
    else:
        log.warning(f"[{job_id}] Phase B: no video available — serving audio-only")
        job["has_video"] = False


def _build_bg_window(job_id: str, start_abs: float, duration: float,
                     chunk_dir: Path, out_path: Path):
    """
    Slice `duration` seconds of background audio starting at `start_abs`.
    Prefers music-only chunks, falls back to full mixed chunk.
    """
    end_abs = start_abs + duration

    def best_chunk_for(n_stem: str, chunk_dir: Path):
        music_only = chunk_dir / f"{n_stem}_music.mp3"
        if music_only.exists() and music_only.stat().st_size > 0:
            return music_only, True
        full_mix = chunk_dir / f"{n_stem}.mp3"
        if full_mix.exists() and full_mix.stat().st_size > 0:
            return full_mix, False
        return None, False

    chunk_stems = sorted(
        [p.stem for p in chunk_dir.glob("*.mp3")
         if p.suffix == ".mp3" and p.stem.isdigit()],
        key=lambda s: int(s)
    )

    if not chunk_stems:
        raise ValueError(
            f"No final background chunks found in {chunk_dir}. "
            f"Expected files named like 33.mp3, 34.mp3 etc."
        )

    first_needed_idx = max(0, int(start_abs // CHUNK_DURATION_SEC))
    last_needed_idx  = min(len(chunk_stems) - 1,
                           int(end_abs // CHUNK_DURATION_SEC) + 1)

    timeline = []
    t = 0.0
    for i, stem in enumerate(chunk_stems):
        chunk_path, is_music_only = best_chunk_for(stem, chunk_dir)
        if chunk_path is None:
            log.warning(f"[{job_id}] Missing chunk {stem}.mp3, skipping")
            t += CHUNK_DURATION_SEC
            continue

        is_edge = (i == 0 or i == len(chunk_stems) - 1 or
                   i == first_needed_idx or i == first_needed_idx - 1 or
                   i == last_needed_idx  or i == last_needed_idx  + 1)
        dur = _probe_duration(chunk_path) if is_edge else CHUNK_DURATION_SEC
        if dur <= 0:
            log.warning(f"[{job_id}] Could not probe {chunk_path.name}, using assumed duration")
            dur = CHUNK_DURATION_SEC

        if is_music_only:
            log.debug(f"[{job_id}] chunk {stem}: using music-only")
        else:
            log.debug(f"[{job_id}] chunk {stem}: using full mix (music-only not ready)")

        timeline.append((chunk_path, t, t + dur))
        t += dur

    log.info(f"[{job_id}] Building bg window: {len(timeline)} chunks, "
             f"need abs={start_abs:.1f}-{end_abs:.1f}s")

    chunks_needed = []
    for (chunk_path, chunk_start, chunk_end) in timeline:
        if chunk_end <= start_abs:
            continue
        if chunk_start >= end_abs:
            break
        inpoint  = max(0.0, start_abs - chunk_start)
        outpoint = min(chunk_end - chunk_start, end_abs - chunk_start)
        chunks_needed.append((chunk_path, inpoint, outpoint))

    if not chunks_needed:
        raise ValueError(
            f"No background chunks cover abs={start_abs:.1f}-{end_abs:.1f}s "
            f"in {chunk_dir}. Timeline covers 0-{t:.1f}s across {len(timeline)} chunks."
        )

    concat_txt = out_path.parent / "concat_list.txt"
    with open(concat_txt, "w") as f:
        for (path, inpoint, outpoint) in chunks_needed:
            f.write(f"file '{path}'\n")
            if inpoint > 0:
                f.write(f"inpoint {inpoint:.6f}\n")
            f.write(f"outpoint {outpoint:.6f}\n")

    log.info(f"[{job_id}] concat list: {len(chunks_needed)} chunk(s)")

    # Attempt copy-only first
    try:
        result = subprocess.run(
            ["ffmpeg", "-threads", "2",
             "-f", "concat", "-safe", "0",
             "-i", str(concat_txt),
             "-c", "copy",
             "-y", str(out_path)],
            capture_output=True, timeout=120
        )
        if result.returncode == 0 and out_path.exists() and out_path.stat().st_size > 0:
            log.info(f"[{job_id}] bg_window sliced (copy mode)")
            return
        log.warning(f"[{job_id}] copy-mode failed, falling back to re-encode: "
                    f"{result.stderr.decode()[-200:]}")
    except subprocess.TimeoutExpired:
        log.warning(f"[{job_id}] copy-mode timeout, falling back")

    # Re-encode fallback
    result = subprocess.run(
        ["ffmpeg", "-threads", "2",
         "-f", "concat", "-safe", "0",
         "-i", str(concat_txt),
         "-ar", "44100", "-ac", "2", "-b:a", "192k",
         "-y", str(out_path)],
        capture_output=True, timeout=300
    )
    if result.returncode != 0:
        raise RuntimeError(f"FFmpeg bg_window re-encode failed: {result.stderr.decode()[-400:]}")
    log.info(f"[{job_id}] bg_window sliced (re-encode fallback)")


# ---------------------------------------------------------------------------
# yt-dlp helpers
# ---------------------------------------------------------------------------

def _normalise_url(url: str) -> str:
    from urllib.parse import urlparse, parse_qs
    url = url.strip()
    if not url.startswith("http"):
        url = f"https://www.youtube.com/watch?v={url}"
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    vid = (qs.get("v") or [None])[0]
    if vid:
        return f"https://www.youtube.com/watch?v={vid}"
    return url


def _yt_dlp_info(url: str) -> dict:
    """
    Probe YouTube URL for metadata.
    Uses pinned daemon worker when available (~2ms hot dispatch vs ~400ms cold import).
    Falls back to direct inline import if daemon not running.
    """
    client = _get_daemon()
    if client:
        # Embed URL directly — it's already a clean https:// string from _normalise_url()
        safe_url = url.replace("'", "\\'")
        code = f"""
import yt_dlp
ydl_opts = {{"quiet": True, "no_warnings": True, "skip_download": True, "extract_flat": False}}
with yt_dlp.YoutubeDL(ydl_opts) as ydl:
    info = ydl.extract_info('{safe_url}', download=False)
if not info:
    raise ValueError("Could not extract video info from URL")
result = {{
    "title":    info.get("title"),
    "duration": info.get("duration"),
    "is_live":  info.get("is_live"),
    "id":       info.get("id"),
}}
"""
        try:
            res = client.execute_smart(
                _YTDLP_SPEC,
                code,
                worker_tag="ytdlp-info",
                pin=True,
            )
            if res.get("success"):
                # daemon puts worker's `result = {...}` assignment into res["meta"]
                data = res.get("meta") or {}
                if isinstance(data, dict) and "title" in data:
                    log.info(f"_yt_dlp_info via daemon: {data.get('title')}")
                    return data
                log.warning(f"_yt_dlp_info daemon unexpected shape: {repr(res)[:200]} — falling back")
            else:
                log.warning(f"_yt_dlp_info daemon call failed: {res.get('error')} — falling back")
        except Exception as e:
            log.warning(f"_yt_dlp_info daemon exception: {e} — falling back")

    # Fallback: direct import
    import yt_dlp
    ydl_opts = {
        "quiet": True, "no_warnings": True,
        "skip_download": True, "extract_flat": False,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
    if not info:
        raise ValueError("Could not extract video info from URL")
    return info


def _yt_dlp_download(job_id: str, url: str, out_path: Path, job: dict):
    """
    Download audio-only stream.
    Uses pinned daemon worker when available.
    Falls back to direct inline import if daemon not running.

    NOTE: The daemon path cannot observe job["status"] == "cancelled" in real time
    (the job dict lives in a different process). Cancellation during download
    via the daemon has ~poll-interval latency rather than instant. This is
    acceptable — cancel() will terminate the ffmpeg subprocess in Phase B
    regardless, and the download output is simply discarded.
    """
    client = _get_daemon()
    if client:
        safe_url      = url.replace("'", "\'")
        safe_out_path = str(out_path).replace("'", "\'")
        code = (
            "import yt_dlp\n"
            "ydl_opts = {\n"
            "    'quiet': True,\n"
            "    'no_warnings': True,\n"
            "    'format': 'bestaudio[ext=m4a]/bestaudio',\n"
            f"    'outtmpl': '{safe_out_path}',\n"
            "    'noplaylist': True,\n"
            "}\n"
            "with yt_dlp.YoutubeDL(ydl_opts) as ydl:\n"
            f"    ydl.download(['{safe_url}'])\n"
            "result = {'done': True}\n"
        )
        try:
            res = client.execute_smart(
                _YTDLP_SPEC,
                code,
                worker_tag="ytdlp-dl",
                pin=True,
            )
            if res.get("success"):
                log.info(f"[{job_id}] _yt_dlp_download via daemon ✓")
                return
            log.warning(f"[{job_id}] _yt_dlp_download daemon failed: {res.get('error')} — falling back")
        except Exception as e:
            log.warning(f"[{job_id}] _yt_dlp_download daemon exception: {e} — falling back")

    # Fallback: direct import
    import yt_dlp

    class _CancelCheck(yt_dlp.postprocessor.common.PostProcessor):
        def run(self, info):
            if job.get("status") == "cancelled":
                raise yt_dlp.utils.DownloadError("Cancelled by user")
            return [], info

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "format": "bestaudio[ext=m4a]/bestaudio",
        "outtmpl": str(out_path),
        "noplaylist": True,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.add_post_processor(_CancelCheck())
        ydl.download([url])


def _yt_dlp_download_video(job_id: str, url: str, out_path: Path, job: dict):
    """
    Download best H.264 video-only stream (no audio — we supply our own mixed audio).
    Uses pinned daemon worker when available.
    Falls back to direct inline import if daemon not running.
    """
    client = _get_daemon()
    if client:
        safe_url      = url.replace("'", "\'")
        safe_out_path = str(out_path).replace("'", "\'")
        code = (
            "import yt_dlp\n"
            "ydl_opts = {\n"
            "    'quiet': True,\n"
            "    'no_warnings': True,\n"
            "    'format': 'bestvideo[vcodec^=avc1][height<=1080]/bestvideo[vcodec^=avc1]/bestvideo[ext=mp4]/bestvideo',\n"
            f"    'outtmpl': '{safe_out_path}',\n"
            "    'noplaylist': True,\n"
            "}\n"
            "with yt_dlp.YoutubeDL(ydl_opts) as ydl:\n"
            f"    ydl.download(['{safe_url}'])\n"
            "result = {'done': True}\n"
        )
        try:
            res = client.execute_smart(
                _YTDLP_SPEC,
                code,
                worker_tag="ytdlp-dl",
                pin=True,
            )
            if res.get("success"):
                log.info(f"[{job_id}] _yt_dlp_download_video via daemon ✓")
                return
            log.warning(f"[{job_id}] _yt_dlp_download_video daemon failed: {res.get('error')} — falling back")
        except Exception as e:
            log.warning(f"[{job_id}] _yt_dlp_download_video daemon exception: {e} — falling back")

    # Fallback: direct import
    import yt_dlp

    class _CancelCheck(yt_dlp.postprocessor.common.PostProcessor):
        def run(self, info):
            if job.get("status") == "cancelled":
                raise yt_dlp.utils.DownloadError("Cancelled by user")
            return [], info

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "format": "bestvideo[vcodec^=avc1][height<=1080]/bestvideo[vcodec^=avc1]/bestvideo[ext=mp4]/bestvideo",
        "outtmpl": str(out_path),
        "noplaylist": True,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.add_post_processor(_CancelCheck())
        ydl.download([url])


# ---------------------------------------------------------------------------
# FFmpeg helpers  (unchanged — ffmpeg is a binary, not daemon-able)
# ---------------------------------------------------------------------------

def _ffmpeg(job_id: str, args: list, job: dict, timeout: int = 600):
    import re, threading, time as _time
    cmd = ["ffmpeg", "-progress", "pipe:2", "-nostats"] + args
    log.debug(f"[{job_id}] ffmpeg: {chr(32).join(cmd)}")
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    job["_proc"] = proc

    stderr_lines = []
    last_log = [0.0]
    duration_sec = [None]

    def _read_stderr():
        for raw in proc.stderr:
            line = raw.decode(errors="replace").rstrip()
            stderr_lines.append(line)
            if line.startswith("out_time_ms="):
                try:
                    ms = int(line.split("=", 1)[1])
                    elapsed_sec = ms / 1_000_000
                    now = _time.monotonic()
                    if now - last_log[0] >= 15:
                        last_log[0] = now
                        dur = job.get("duration", 0)
                        pct = f"{elapsed_sec/dur*100:.0f}%" if dur else f"{elapsed_sec:.0f}s"
                        log.info(f"[{job_id}] ffmpeg transcode: {elapsed_sec:.0f}s / {pct}")
                        job["transcode_progress"] = pct
                except (ValueError, ZeroDivisionError):
                    pass

    t = threading.Thread(target=_read_stderr, daemon=True)
    t.start()

    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        raise RuntimeError(f"FFmpeg timed out after {timeout}s")
    finally:
        t.join(timeout=5)
        job["_proc"] = None

    if proc.returncode != 0:
        tail = "\n".join(stderr_lines[-20:])
        raise RuntimeError(f"FFmpeg failed (rc={proc.returncode}): {tail[-600:]}")


def _probe_duration(path: Path) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "error",
         "-show_entries", "format=duration",
         "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, timeout=30
    )
    try:
        return float(result.stdout.strip())
    except (ValueError, AttributeError):
        return 0.0


# ---------------------------------------------------------------------------
# Status persistence
# ---------------------------------------------------------------------------

def _public_status(job: dict) -> dict:
    return {k: v for k, v in job.items() if not k.startswith("_")}


def _write_status(job: dict):
    try:
        status_path = Path(job["job_dir"]) / "status.json"
        status_path.write_text(json.dumps(_public_status(job), indent=2))
    except Exception as e:
        log.warning(f"Could not write status.json: {e}")


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------

def _cleanup_stale():
    """Delete orphaned job dirs at startup."""
    if not _JOBS_DIR:
        return
    for job_dir in _JOBS_DIR.iterdir():
        if not job_dir.is_dir():
            continue
        status_file = job_dir / "status.json"
        if not status_file.exists():
            shutil.rmtree(job_dir, ignore_errors=True)
            log.info(f"Cleaned orphaned job dir: {job_dir.name}")


def cleanup_old_jobs(now: float | None = None):
    """
    TTL-based GC. Call periodically (e.g. every 30 min from SessionManager).
      - ready/done:            2 hours
      - failed/cancelled/etc:  30 minutes
    """
    now = now or time.time()
    LONG_TTL  = 2 * 3600
    SHORT_TTL = 30 * 60

    for job_dir in (_JOBS_DIR or Path("/dev/null")).iterdir():
        if not job_dir.is_dir():
            continue
        status_file = job_dir / "status.json"
        if not status_file.exists():
            shutil.rmtree(job_dir, ignore_errors=True)
            continue
        try:
            job_data = json.loads(status_file.read_text())
            created  = job_data.get("created_at", 0)
            status   = job_data.get("status", "")
            ttl = LONG_TTL if status in ("ready", "done") else SHORT_TTL
            if now - created > ttl:
                shutil.rmtree(job_dir, ignore_errors=True)
                log.info(f"GC: removed job {job_dir.name} (status={status}, age={now-created:.0f}s)")
        except Exception as e:
            log.warning(f"GC: error processing {job_dir.name}: {e}")


# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------

class ConflictError(Exception):
    pass

class NotFoundError(Exception):
    pass

class StateError(Exception):
    pass