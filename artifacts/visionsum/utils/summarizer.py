"""
summarizer.py — Video summarization engine.

Selects which segments to keep, re-encodes each with ffmpeg (ultrafast),
then concatenates them into the final summary.

Segment selection strategy
──────────────────────────
• If total motion ≤ budget  → keep ALL detected presence segments as-is.
• If total motion > budget  → split every segment into CHUNK_SEC windows,
  score each window by average motion intensity, keep the highest-scoring
  windows until the budget is filled, then merge adjacent windows back
  into contiguous clips.
  This ensures the output is always close to the requested duration —
  it never silently falls back to returning the whole video.
"""

import os
import json
import time
import shutil
import logging
import subprocess
import tempfile

import numpy as np

logger = logging.getLogger(__name__)

FFMPEG   = 'ffmpeg'
FFPROBE  = 'ffprobe'
CHUNK_SEC = 5.0   # granularity for intensity scoring when trimming


# ── Segment selection ─────────────────────────────────────────────────────────

def build_segments(analysis: dict, compression_pct: int) -> list:
    """
    Return a sorted list of (start_sec, end_sec) tuples to keep.
    Always returns segments totalling roughly target_keep seconds.
    """
    active_segs   = analysis.get('active_segments', [])
    total_dur     = analysis.get('duration', 0.0)
    motion_scores = analysis.get('motion_scores', [])
    sample_fps    = len(motion_scores) / total_dur if total_dur > 0 else 4.0

    if not active_segs:
        logger.warning("No motion detected — video appears static")
        return [(0.0, min(5.0, total_dur))]

    motion_total = sum(e - s for s, e in active_segs)
    target_keep  = total_dur * (1.0 - compression_pct / 100.0)

    logger.info(
        f"Motion: {motion_total:.1f}s in {len(active_segs)} segment(s) | "
        f"budget ({100 - compression_pct}% keep): {target_keep:.1f}s"
    )

    # ── Case 1: all motion fits the budget ────────────────────────────────────
    if motion_total <= target_keep * 1.05:
        logger.info("All motion fits budget — keeping all segments")
        return [(float(s), float(e)) for s, e in active_segs]

    # ── Case 2: too much motion → score mini-chunks, keep best ───────────────
    logger.info(
        f"Motion ({motion_total:.0f}s) exceeds budget ({target_keep:.0f}s) — "
        f"chunking at {CHUNK_SEC}s and keeping highest-intensity windows"
    )

    chunks = []
    for seg_start, seg_end in active_segs:
        t = seg_start
        while t < seg_end:
            chunk_end = min(t + CHUNK_SEC, seg_end)
            si  = int(t         * sample_fps)
            ei  = int(chunk_end * sample_fps)
            slc = motion_scores[si:ei] if motion_scores and ei <= len(motion_scores) else []
            avg = float(np.mean(slc)) if slc else 0.0
            chunks.append((t, chunk_end, avg))
            t = chunk_end

    # Sort by intensity (highest first), fill budget
    chunks.sort(key=lambda c: c[2], reverse=True)
    kept_chunks: list[tuple] = []
    kept_dur = 0.0
    for start, end, score in chunks:
        dur = end - start
        if kept_dur + dur <= target_keep * 1.1:
            kept_chunks.append((start, end))
            kept_dur += dur
        if kept_dur >= target_keep:
            break

    if not kept_chunks:
        # Absolute fallback: first N seconds of the first segment
        s0 = float(active_segs[0][0])
        return [(s0, min(s0 + target_keep, float(active_segs[0][1])))]

    # Sort kept chunks chronologically and merge adjacent ones (gap ≤ 0.5 s)
    kept_chunks.sort(key=lambda x: x[0])
    merged: list[list] = []
    for seg in kept_chunks:
        if merged and seg[0] <= merged[-1][1] + 0.5:
            merged[-1][1] = max(merged[-1][1], seg[1])
        else:
            merged.append(list(seg))

    logger.info(
        f"Kept {len(merged)} segment(s) totalling {kept_dur:.1f}s "
        f"from {len(chunks)} scored chunks"
    )
    return [(float(s), float(e)) for s, e in merged]


# ── ffmpeg helpers ────────────────────────────────────────────────────────────

def _run(cmd: list, timeout: int = 900) -> None:
    proc = subprocess.run(cmd, capture_output=True, timeout=timeout)
    if proc.returncode != 0:
        err = proc.stderr.decode('utf-8', errors='replace')
        logger.error(f"ffmpeg error (rc={proc.returncode}):\n{err[-1000:]}")
        raise RuntimeError(f"ffmpeg exited with code {proc.returncode}")


def _has_audio(path: str) -> bool:
    try:
        r = subprocess.run(
            [FFPROBE, '-v', 'quiet', '-print_format', 'json',
             '-show_streams', '-select_streams', 'a', path],
            capture_output=True, timeout=10
        )
        return bool(json.loads(r.stdout).get('streams'))
    except Exception:
        return False


def _trim_segment(input_path: str, start: float, end: float,
                  out_path: str, has_audio: bool) -> None:
    audio_args = ['-c:a', 'aac', '-b:a', '64k'] if has_audio else ['-an']
    _run([
        FFMPEG, '-y',
        '-ss', f'{start:.6f}',
        '-to', f'{end:.6f}',
        '-i', input_path,
        '-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '23',
        *audio_args,
        '-movflags', '+faststart',
        out_path,
    ])


# ── Main entry point ──────────────────────────────────────────────────────────

def summarize_video(input_path: str, output_path: str,
                    analysis: dict, compression_pct: int) -> dict:
    t_start  = time.time()
    segments = build_segments(analysis, compression_pct)
    fps      = analysis.get('fps', 25.0)
    total    = analysis.get('duration', 0.0)

    if not segments:
        raise RuntimeError("No segments to write")

    audio   = _has_audio(input_path)
    tmp_dir = tempfile.mkdtemp(prefix='visionsum_')

    try:
        if len(segments) == 1:
            start, end = segments[0]
            logger.info(f"Single segment {start:.2f}–{end:.2f}s → encoding")
            _trim_segment(input_path, start, end, output_path, audio)
        else:
            seg_files = []
            for i, (start, end) in enumerate(segments):
                seg_path = os.path.join(tmp_dir, f'seg_{i:04d}.mp4')
                logger.info(
                    f"Encoding segment {i+1}/{len(segments)}: "
                    f"{start:.2f}–{end:.2f}s ({end-start:.1f}s)"
                )
                _trim_segment(input_path, start, end, seg_path, audio)
                seg_files.append(seg_path)

            concat_path = os.path.join(tmp_dir, 'concat.txt')
            with open(concat_path, 'w') as f:
                for sf in seg_files:
                    f.write(f"file '{sf}'\n")

            logger.info(f"Concatenating {len(seg_files)} segments → {output_path}")
            _run([
                FFMPEG, '-y',
                '-f', 'concat', '-safe', '0',
                '-i', concat_path,
                '-c', 'copy',
                '-movflags', '+faststart',
                output_path,
            ])

    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    summary_dur = sum(e - s for s, e in segments)
    compression = round((1.0 - summary_dur / total) * 100.0, 1) if total > 0 else 0.0

    logger.info(
        f"Done: {summary_dur:.1f}s kept from {total:.1f}s original "
        f"({compression:.0f}% compressed) in {time.time()-t_start:.1f}s"
    )

    return {
        'original_dur': round(total, 2),
        'summary_dur':  round(summary_dur, 2),
        'compression':  compression,
        'proc_time':    round(time.time() - t_start, 2),
        'frames':       int(summary_dur * fps),
        'segments':     segments,
    }
