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


def finalize_job(job_id: str, absolute_sec: float, chunk_dir_for_session: Path) -> dict:
    """
    Phase B — called by the route when client reports its freeze position.
    Runs synchronously (should be < 60s for up to 90-min video).
    Returns updated status dict.
    """
    with _jobs_lock:
        job = _jobs.get(job_id)
    if not job:
        raise NotFoundError(f"Job {job_id} not found")
    if job["status"] != "finalizing":
        raise StateError(f"Job {job_id} is in state '{job['status']}', expected 'finalizing'")

    job["status"]             = "rendering"
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


def stream_path(job_id: str) -> Path:
    """Return path to final mixed .mp3 for serving."""
    with _jobs_lock:
        job = _jobs.get(job_id)
    if not job or job["status"] != "ready":
        raise StateError(f"Job {job_id} not ready for streaming")
    p = Path(job["job_dir"]) / "final_mix.mp3"
    if not p.exists():
        raise NotFoundError(f"final_mix.mp3 missing for job {job_id}")
    return p


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
        # While yt-dlp downloads, we pull from the session music queue
        # and build a clean music-only mp3 sized to this video's duration.
        # This runs concurrently so it costs zero extra wall-clock time.
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
                music_bg_ready.set()   # unblock Phase B either way

        music_bg_thread = threading.Thread(target=_build_music_bg, daemon=True)
        music_bg_thread.start()

        # ── 2b. Download YT audio-only ─────────────────────────────
        log.info(f"[{job_id}] Phase A: downloading audio ({duration:.0f}s)...")
        yt_audio_path = job_dir / "yt_audio.m4a"
        _yt_dlp_download(job_id, url, yt_audio_path, job)

        if job["status"] == "cancelled":
            return

        # ── 3. Convert to mp3 at 44100 Hz for reliable concat ──────
        log.info(f"[{job_id}] Phase A: converting to mp3...")
        yt_mp3_path = job_dir / "yt_audio.mp3"
        _ffmpeg(
            job_id,
            ["-i", str(yt_audio_path),
             "-ar", "44100", "-ac", "2", "-b:a", "192k",
             "-y", str(yt_mp3_path)],
            job
        )

        if job["status"] == "cancelled":
            return

        # ── 4. Probe actual duration of the converted file ──────────
        real_dur = _probe_duration(yt_mp3_path)
        job["duration"] = real_dur

        # ── 5. Wait for music-bg (usually already done by now) ──────
        log.info(f"[{job_id}] Phase A: waiting for music-bg concat...")
        music_bg_ready.wait(timeout=300)   # generous; yt download usually takes longer
        job["music_bg_ok"] = (
            music_bg_path.exists()
            and music_bg_path.stat().st_size > 0
            and not music_bg_error
        )
        if job["music_bg_ok"]:
            log.info(f"[{job_id}] Music-bg ready ({music_bg_path.stat().st_size/1e6:.1f} MB)")
        else:
            log.warning(f"[{job_id}] Music-bg not ready — Phase B will fall back to chunk slice")

        log.info(f"[{job_id}] Phase A complete. Duration: {real_dur:.1f}s, title: {job['title']}")
        job["status"] = "finalizing"   # Signal client to trigger Phase B
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

    Files are named music_{video_id}.mp3 by AudioWorker._download_audio().
    We read whatever is on disk right now — no new downloads.
    """
    audio_dir = (_AUDIO_DIR / session_id) if _AUDIO_DIR else None
    if not audio_dir or not audio_dir.exists():
        raise FileNotFoundError(f"Audio dir not found: {audio_dir}")

    # Gather all music files for this session, sorted for determinism
    music_files = sorted(audio_dir.glob("music_*.mp3"))
    if not music_files:
        raise FileNotFoundError(f"No music files found in {audio_dir}")

    log.info(f"[{job_id}] Music-bg: {len(music_files)} source files available, "
             f"need {target_duration:.0f}s")

    # Build a looped playlist that covers target_duration
    playlist: list[Path] = []
    total = 0.0
    pool = list(music_files)
    import random as _random
    _random.shuffle(pool)   # randomise order each time

    while total < target_duration:
        if not pool:
            pool = list(music_files)   # loop the pool
            _random.shuffle(pool)
        f = pool.pop(0)
        dur = _probe_duration(f)
        if dur <= 0:
            continue
        playlist.append(f)
        total += dur

    log.info(f"[{job_id}] Music-bg playlist: {len(playlist)} tracks = {total:.0f}s")

    # Write concat list
    concat_txt = out_path.parent / "music_bg_list.txt"
    with open(concat_txt, "w") as fh:
        for p in playlist:
            fh.write(f"file '{p}'\n")

    # Concat + trim to exact duration + quick loudnorm
    result = subprocess.run(
        ["ffmpeg", "-y",
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
    duration = job["duration"]   # seconds of YT audio we need to cover

    yt_mp3       = job_dir / "yt_audio.mp3"
    music_bg_mp3 = job_dir / "music_bg.mp3"
    bg_mp3       = job_dir / "bg_window.mp3"   # used only on fallback
    mix_mp3      = job_dir / "final_mix.mp3"

    # ── 1. Choose background source ────────────────────────────────
    # Prefer the fresh dedicated music-bg built during Phase A.
    # Fall back to slicing the existing mixed chunk if music-bg failed.
    if job.get("music_bg_ok") and music_bg_mp3.exists() and music_bg_mp3.stat().st_size > 0:
        bg_source = music_bg_mp3
        log.info(f"[{job_id}] Phase B: using fresh music-bg ({bg_source.stat().st_size/1e6:.1f} MB)")
    else:
        log.warning(f"[{job_id}] Phase B: music-bg unavailable — falling back to chunk slice")
        log.info(f"[{job_id}] Phase B: slicing background from abs={absolute_sec:.1f}s, dur={duration:.1f}s")
        _build_bg_window(job_id, absolute_sec, duration, chunk_dir_for_session, bg_mp3)
        bg_source = bg_mp3

    # ── 2. amix YT audio + music-only background ───────────────────
    # YT audio: dynaudnorm so quiet lectures and loud YT uploads land at same level.
    # Background music: already loudnorm'd, keep at 0.35 weight (clear but under voice).
    # Final limiter prevents clipping on hot sources.
    log.info(f"[{job_id}] Phase B: mixing streams...")
    _ffmpeg(
        job_id,
        ["-i", str(yt_mp3),
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


def _build_bg_window(job_id: str, start_abs: float, duration: float,
                     chunk_dir: Path, out_path: Path):
    """
    Slice `duration` seconds of background audio starting at `start_abs`.

    Prefers music-only chunks ({N}_music.mp3) so that the adhoc video audio
    mixes with music only — no competing speech track.
    Falls back to the full mixed chunk ({N}.mp3) if music-only isn't ready yet.

    Uses assumed CHUNK_DURATION_SEC for interior chunks; only probes edge chunks.
    Falls back to re-encode if copy-only produces a broken file.
    """
    end_abs = start_abs + duration

    # Prefer music-only ({N}_music.mp3), fall back to full mix ({N}.mp3)
    def best_chunk_for(n_stem: str, chunk_dir: Path):
        music_only = chunk_dir / f"{n_stem}_music.mp3"
        if music_only.exists() and music_only.stat().st_size > 0:
            return music_only, True
        full_mix = chunk_dir / f"{n_stem}.mp3"
        if full_mix.exists() and full_mix.stat().st_size > 0:
            return full_mix, False
        return None, False

    # Enumerate all final chunk stems (purely numeric)
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

    # Determine which chunk indices we'll actually need (for selective probing)
    first_needed_idx = max(0, int(start_abs // CHUNK_DURATION_SEC))
    last_needed_idx  = min(len(chunk_stems) - 1,
                           int(end_abs // CHUNK_DURATION_SEC) + 1)

    timeline = []  # list of (chunk_path, is_music_only, chunk_start_abs, chunk_end_abs)
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

    # Find all chunks overlapping [start_abs, end_abs)
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

    # Write concat list
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
            ["ffmpeg",
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
        ["ffmpeg",
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


# ---------------------------------------------------------------------------
# FFmpeg helpers
# ---------------------------------------------------------------------------

def _ffmpeg(job_id: str, args: list, job: dict, timeout: int = 600):
    cmd = ["ffmpeg"] + args
    log.debug(f"[{job_id}] ffmpeg: {' '.join(cmd)}")
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    job["_proc"] = proc
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        raise RuntimeError(f"FFmpeg timed out after {timeout}s")
    finally:
        job["_proc"] = None

    if proc.returncode != 0:
        raise RuntimeError(f"FFmpeg failed (rc={proc.returncode}): {stderr.decode()[-400:]}")


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