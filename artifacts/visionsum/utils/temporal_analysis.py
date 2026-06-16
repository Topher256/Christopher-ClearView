"""
temporal_analysis.py — Core temporal analysis engine.

Detects *presence* windows — from when someone enters a scene until they leave
— using frame differencing + MOG2 background subtraction.

Calibration for indoor CCTV:
  • motion_threshold=14.0  — filters camera noise (~3) while catching real
                             motion (person walking scores ~30–50)
  • gap_fill_sec=120.0     — bridge up to 2 min of stillness so a person
                             standing in a room stays as one segment
  • entry/exit buffers     — pad each segment so the exact walk-in/walk-out
                             moment is never cut off
  • MOG2 history=300       — ~75 s learning window @ 4 fps; slow enough that
                             a person standing still isn't absorbed as background
"""

import cv2
import numpy as np
import logging

logger = logging.getLogger(__name__)

MAX_SAMPLE_FPS = 4.0


class TemporalAnalyzer:
    def __init__(self,
                 video_path:        str,
                 motion_threshold:  float = 14.0,
                 min_segment_sec:   float = 1.5,
                 gap_fill_sec:      float = 120.0,
                 entry_buffer_sec:  float = 2.0,
                 exit_buffer_sec:   float = 3.0,
                 blur_size:         int   = 15):
        self.video_path       = video_path
        self.motion_threshold = motion_threshold
        self.min_segment_sec  = min_segment_sec
        self.gap_fill_sec     = gap_fill_sec
        self.entry_buffer_sec = entry_buffer_sec
        self.exit_buffer_sec  = exit_buffer_sec
        self.blur_size        = blur_size
        self.bg_sub = cv2.createBackgroundSubtractorMOG2(
            history=300, varThreshold=20, detectShadows=False
        )

    def analyse(self) -> dict:
        cap = cv2.VideoCapture(self.video_path)
        if not cap.isOpened():
            raise IOError(f"Cannot open video: {self.video_path}")

        fps          = cap.get(cv2.CAP_PROP_FPS) or 25.0
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        width        = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        height       = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        duration     = total_frames / fps if fps > 0 else 0

        step       = max(1, int(round(fps / MAX_SAMPLE_FPS)))
        sample_fps = fps / step

        logger.info(
            f"Temporal analysis: {total_frames} frames @ {fps:.1f} fps "
            f"({duration:.1f}s) | sample every {step} frame(s) "
            f"→ {(total_frames+step-1)//step} samples @ {sample_fps:.2f} fps"
        )

        motion_scores: list[float] = []
        active_flags:  list[bool]  = []
        prev_gray = None
        frame_idx = 0

        while True:
            if frame_idx % step == 0:
                ret, frame = cap.read()
                if not ret:
                    break

                small = cv2.resize(frame, (320, 180))
                gray  = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
                gray  = cv2.GaussianBlur(gray, (self.blur_size, self.blur_size), 0)

                fdiff = float(np.mean(cv2.absdiff(prev_gray, gray))) if prev_gray is not None else 0.0

                fg_mask  = self.bg_sub.apply(small)
                fg_ratio = np.count_nonzero(fg_mask) / (320 * 180) * 100.0

                combined = min((fdiff * 2.0 + fg_ratio) / 3.0 * 4.0, 100.0)

                motion_scores.append(combined)
                active_flags.append(combined >= self.motion_threshold)
                prev_gray = gray
            else:
                if not cap.grab():
                    break

            frame_idx += 1

        cap.release()

        n_active = sum(active_flags)
        n_total  = len(active_flags)
        logger.info(
            f"Raw motion: {n_active}/{n_total} samples active "
            f"({100*n_active/max(n_total,1):.1f}%)"
        )

        segments = self._build_presence_segments(active_flags, sample_fps, duration)
        events   = self._to_events(segments, motion_scores, sample_fps)

        logger.info(f"Presence segments: {len(segments)} | events: {len(events)}")

        return {
            'fps':             fps,
            'total_frames':    total_frames,
            'duration':        duration,
            'width':           width,
            'height':          height,
            'motion_scores':   motion_scores,
            'active_frames':   n_active,
            'active_segments': segments,
            'events':          events,
            'motion_avg':      float(np.mean(motion_scores)) if motion_scores else 0.0,
            'motion_max':      float(np.max(motion_scores))  if motion_scores else 0.0,
        }

    def _build_presence_segments(self, flags: list, sample_fps: float,
                                  total_dur: float) -> list:
        """
        1. Large dilation  — fill gaps ≤ gap_fill_sec (person standing still)
        2. Small erosion   — remove noise bursts shorter than min_segment_sec
        3. Add entry/exit buffers
        4. Merge any overlapping segments
        """
        arr = np.array(flags, dtype=np.uint8)

        gap_samples = max(2, int(self.gap_fill_sec * sample_fps))
        k_big = np.ones(gap_samples, dtype=np.uint8)
        dilated = np.convolve(arr, k_big, mode='same') > 0

        min_samples = max(2, int(self.min_segment_sec * sample_fps))
        k_small = np.ones(min_samples, dtype=np.uint8)
        smoothed = (
            np.convolve(dilated.astype(np.uint8), k_small, mode='same')
            >= max(1, min_samples // 2)
        )

        raw_segs = []
        in_seg, start = False, 0
        for i, active in enumerate(smoothed.tolist()):
            if active and not in_seg:
                start, in_seg = i, True
            elif not active and in_seg:
                raw_segs.append((start, i))
                in_seg = False
        if in_seg:
            raw_segs.append((start, len(smoothed)))

        if not raw_segs:
            return []

        entry_buf = int(self.entry_buffer_sec * sample_fps)
        exit_buf  = int(self.exit_buffer_sec  * sample_fps)
        buffered  = []
        for s, e in raw_segs:
            t_s = max(0.0,       round((s - entry_buf) / sample_fps, 3))
            t_e = min(total_dur, round((e + exit_buf)  / sample_fps, 3))
            buffered.append((t_s, t_e))

        merged: list[list] = []
        for seg in sorted(buffered):
            if merged and seg[0] <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], seg[1])
            else:
                merged.append(list(seg))

        return [(float(s), float(e)) for s, e in merged]

    def _to_events(self, segments: list, scores: list, sample_fps: float) -> list:
        events = []
        for start_s, end_s in segments:
            s         = int(start_s * sample_fps)
            e         = int(end_s   * sample_fps)
            chunk     = scores[s:e] if e <= len(scores) else scores[s:]
            dur       = end_s - start_s
            intensity = float(np.mean(chunk)) if chunk else 0.0
            peak      = float(np.max(chunk))  if chunk else 0.0

            if   peak      > 70: etype = 'Sudden Movement'
            elif dur       > 10: etype = 'Long-Duration Activity'
            elif intensity > 40: etype = 'Moving Object'
            elif dur        < 2: etype = 'Brief Motion'
            else:                etype = 'General Activity'

            events.append({
                'type':      etype,
                'start':     round(start_s,   2),
                'end':       round(end_s,     2),
                'duration':  round(dur,       2),
                'intensity': round(intensity, 2),
            })
        return events


def analyse_video(video_path: str, threshold: float = 14.0) -> dict:
    return TemporalAnalyzer(video_path, motion_threshold=threshold).analyse()
