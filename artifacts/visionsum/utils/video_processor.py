"""
video_processor.py — Orchestrates the full processing pipeline.
"""

import os
import time
import logging
import cv2

from utils.temporal_analysis import analyse_video
from utils.summarizer         import summarize_video
from utils.database           import (
    update_video_meta, update_summary, update_summary_stage,
    save_events, log_action
)

logger = logging.getLogger(__name__)

THUMB_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'static', 'img', 'thumbs')
os.makedirs(THUMB_DIR, exist_ok=True)


def extract_thumbnail(video_path: str, video_id: int) -> str:
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        return ''
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    cap.set(cv2.CAP_PROP_POS_FRAMES, total // 2)
    ret, frame = cap.read()
    cap.release()
    if not ret:
        return ''
    thumb_path = os.path.join(THUMB_DIR, f'thumb_{video_id}.jpg')
    cv2.imwrite(thumb_path, cv2.resize(frame, (320, 180)))
    return f'img/thumbs/thumb_{video_id}.jpg'


def process_video(video_id, summary_id, user_id, input_path, output_path, compression):
    t0 = time.time()

    try:
        log_action(user_id, video_id, 'process_start', 'Processing started')

        # ── Stage 1: Temporal analysis ────────────────────────────────────────
        update_summary_stage(summary_id, 'analyzing')
        logger.info(f"[video {video_id}] Stage 1 — temporal analysis")
        analysis = analyse_video(input_path)

        update_video_meta(
            video_id,
            analysis['duration'],
            analysis['fps'],
            analysis['width'],
            analysis['height'],
        )

        seg_count   = len(analysis['active_segments'])
        motion_pct  = round(analysis['active_frames'] / max(analysis['total_frames'], 1) * 100, 1)
        logger.info(
            f"[video {video_id}] Analysis done: {seg_count} motion segments, "
            f"{motion_pct}% active frames, avg score={analysis['motion_avg']:.1f}"
        )

        # ── Stage 2: Build summary ────────────────────────────────────────────
        update_summary_stage(summary_id, 'summarizing')
        logger.info(f"[video {video_id}] Stage 2 — summarizing at {compression}% compression")
        stats = summarize_video(input_path, output_path, analysis, compression)

        # ── Stage 3: Save results ─────────────────────────────────────────────
        if analysis['events']:
            save_events(summary_id, analysis['events'])

        update_summary(
            summary_id,
            stats['original_dur'],
            stats['summary_dur'],
            len(analysis['events']),
            round(time.time() - t0, 2),
            analysis['motion_avg'],
            status='done',
        )

        log_action(
            user_id, video_id, 'process_done',
            f"Done: {stats['summary_dur']:.1f}s summary from {stats['original_dur']:.1f}s "
            f"({stats['compression']}% compression, {len(stats['segments'])} segments)"
        )

        extract_thumbnail(input_path, video_id)

        return {'success': True, 'analysis': analysis, 'stats': stats}

    except Exception as exc:
        logger.exception(f"[video {video_id}] Processing failed: {exc}")
        update_summary(summary_id, 0, 0, 0, round(time.time() - t0, 2), 0, status='error')
        log_action(user_id, video_id, 'process_error', str(exc))
        return {'success': False, 'error': str(exc)}
