import type { Clip, LayoutInfo } from "../api/types";

/** Horizontal crop centre (0..1) for a clip at absolute time `ms`, mirroring the renderer:
 * manual crop_x > centre > the stored face track (piecewise-constant shots). */
export function cropCenterAt(clip: Clip, ms: number): number {
  const edit = clip.edit;
  if (edit.reframe === "manual" && edit.crop_x != null) return edit.crop_x;
  if (edit.reframe === "center") return 0.5;
  const track = edit.track;
  const range = edit.track_range;
  if (!track?.length || !range || range[0] !== clip.start_ms || range[1] !== clip.end_ms) return 0.5;
  const rel = (ms - clip.start_ms) / 1000;
  let cx = track[0][1];
  for (const [t, x] of track) {
    if (t <= rel) cx = x;
    else break;
  }
  return cx;
}

export interface CropRect {
  sx: number;
  sy: number;
  sw: number;
  sh: number;
}

/** Source rectangle (in video pixels) shown by a face-following layout. */
export function cropRect(layout: LayoutInfo, srcW: number, srcH: number, centerX: number): CropRect {
  const aspect = layout.width / layout.height;
  if (srcW / srcH > aspect) {
    const sw = srcH * aspect;
    const half = sw / 2 / srcW;
    const cx = Math.min(Math.max(centerX, half), 1 - half);
    return { sx: cx * srcW - sw / 2, sy: 0, sw, sh: srcH };
  }
  const sh = srcW / aspect;
  return { sx: 0, sy: (srcH - sh) / 2, sw: srcW, sh };
}
