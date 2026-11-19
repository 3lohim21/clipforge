import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { Clip, Timeline as TimelineData } from "../api/types";
import { clamp, fmtTime } from "../lib/format";
import { usePlayerStore } from "../lib/player";

interface Props {
  durationMs: number;
  data: TimelineData | undefined;
  clips: Clip[];
  selectedId: string | null;
  selection: [number, number] | null;
  snapPoints: number[]; // sorted word boundaries (ms) for clean cuts
  onSelectClip: (clip: Clip) => void;
  onClipChange: (clip: Clip, start: number, end: number) => void;
  onSelectionChange: (range: [number, number] | null) => void;
}

type Drag =
  | { kind: "start" | "end"; clip: Clip; start: number; end: number }
  | { kind: "select"; anchor: number; x0: number; moved: boolean }
  | { kind: "pan"; x0: number; view0: [number, number] };

const H_RULER = 18;
const COLORS = {
  suggested: "rgba(139, 92, 246, 0.30)",
  suggestedEdge: "rgba(167, 139, 250, 0.9)",
  saved: "rgba(52, 211, 153, 0.28)",
  savedEdge: "rgba(52, 211, 153, 0.95)",
  selected: "rgba(250, 204, 21, 0.30)",
  selectedEdge: "#facc15",
};

function decodeEnergy(b64: string | null | undefined): Uint8Array | null {
  if (!b64) return null;
  const bin = atob(b64);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

function tickStep(spanMs: number, width: number): number {
  const steps = [1e3, 5e3, 10e3, 30e3, 60e3, 300e3, 600e3, 1800e3, 3600e3];
  return steps.find((s) => (s / spanMs) * width >= 70) ?? 3600e3;
}

export function Timeline(props: Props) {
  const { durationMs, data, clips, selectedId, selection, snapPoints } = props;
  const store = usePlayerStore();
  const wrap = useRef<HTMLDivElement>(null);
  const canvas = useRef<HTMLCanvasElement>(null);
  const staticLayer = useRef<HTMLCanvasElement | null>(null);
  const [size, setSize] = useState({ w: 800, h: 128, dpr: 1 });
  const [view, setView] = useState<[number, number]>([0, durationMs || 1]);
  const drag = useRef<Drag | null>(null);
  const hover = useRef<number | null>(null);
  const energy = useMemo(() => decodeEnergy(data?.energy_u8), [data?.energy_u8]);
  const latest = useRef(props);
  latest.current = props;

  useEffect(() => setView([0, durationMs || 1]), [durationMs]);

  // Zoom to the selected clip (with context on both sides) when the selection changes.
  const selected = clips.find((c) => c.id === selectedId);
  useEffect(() => {
    if (!selected || !durationMs) return;
    const len = selected.end_ms - selected.start_ms;
    const pad = Math.max(5_000, len * 0.6);
    setView([Math.max(0, selected.start_ms - pad), Math.min(durationMs, selected.end_ms + pad)]);
    // only when a different clip is picked, not when its bounds are edited
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedId, durationMs]);

  useEffect(() => {
    const el = wrap.current;
    if (!el) return;
    const ro = new ResizeObserver(([entry]) => {
      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      setSize({ w: Math.max(100, entry.contentRect.width), h: entry.contentRect.height, dpr });
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const xToMs = useCallback((x: number) => view[0] + (x / size.w) * (view[1] - view[0]), [view, size.w]);
  const msToX = useCallback((ms: number) => ((ms - view[0]) / (view[1] - view[0])) * size.w, [view, size.w]);

  // --- static layer: waveform, heatmap, chapters, ruler (recomputed only on zoom/resize/data) ---
  useEffect(() => {
    const layer = staticLayer.current ?? document.createElement("canvas");
    staticLayer.current = layer;
    const { w, h, dpr } = size;
    layer.width = Math.round(w * dpr);
    layer.height = Math.round(h * dpr);
    const ctx = layer.getContext("2d")!;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    const span = view[1] - view[0];
    const bodyH = h - H_RULER;
    const mid = bodyH / 2 + 4;

    if (data?.heatmap?.length) {
      ctx.beginPath();
      ctx.moveTo(0, bodyH);
      for (const [s, e, v] of data.heatmap) {
        if (e < view[0] || s > view[1]) continue;
        const y = bodyH - v * (bodyH * 0.85);
        ctx.lineTo(msToX(s), y);
        ctx.lineTo(msToX(e), y);
      }
      ctx.lineTo(w, bodyH);
      ctx.closePath();
      const grad = ctx.createLinearGradient(0, 0, 0, bodyH);
      grad.addColorStop(0, "rgba(249, 115, 22, 0.35)");
      grad.addColorStop(1, "rgba(249, 115, 22, 0.02)");
      ctx.fillStyle = grad;
      ctx.fill();
    }

    if (energy && data) {
      const frameMs = data.frame_ms;
      ctx.fillStyle = "rgba(148, 163, 184, 0.55)";
      for (let x = 0; x < w; x += 2) {
        const f0 = Math.floor((view[0] + (x / w) * span) / frameMs);
        const f1 = Math.max(f0 + 1, Math.floor((view[0] + ((x + 2) / w) * span) / frameMs));
        let peak = 0;
        for (let f = f0; f < f1 && f < energy.length; f++) if (energy[f] > peak) peak = energy[f];
        const amp = (peak / 255) ** 1.6 * (bodyH * 0.46);
        ctx.fillRect(x, mid - amp, 1.5, amp * 2 || 1);
      }
    }

    ctx.fillStyle = "rgba(255,255,255,0.35)";
    ctx.font = "10px ui-monospace, monospace";
    for (const ch of data?.chapters ?? []) {
      const x = msToX(ch.start_ms);
      if (x < 0 || x > w) continue;
      ctx.fillRect(x, 0, 1, bodyH);
      ctx.fillText(ch.title.slice(0, 40), x + 4, 11);
    }

    ctx.fillStyle = "#0a0b10";
    ctx.fillRect(0, bodyH, w, H_RULER);
    ctx.fillStyle = "#5d667c";
    const step = tickStep(span, w);
    for (let t = Math.ceil(view[0] / step) * step; t <= view[1]; t += step) {
      const x = msToX(t);
      ctx.fillRect(x, bodyH, 1, 5);
      ctx.fillText(fmtTime(t), x + 3, bodyH + 13);
    }
  }, [size, view, data, energy, msToX]);

  // --- dynamic layer: clips, selection, playhead, hover (redrawn every frame while playing) ---
  const draw = useCallback(() => {
    const c = canvas.current;
    const layer = staticLayer.current;
    if (!c || !layer) return;
    const { w, h, dpr } = size;
    if (c.width !== Math.round(w * dpr)) {
      c.width = Math.round(w * dpr);
      c.height = Math.round(h * dpr);
    }
    const ctx = c.getContext("2d")!;
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.clearRect(0, 0, c.width, c.height);
    ctx.drawImage(layer, 0, 0);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    const bodyH = h - H_RULER;
    const p = latest.current;
    const d = drag.current;

    for (const clip of p.clips) {
      if (clip.status === "discarded") continue;
      let s = clip.start_ms;
      let e = clip.end_ms;
      if (d && (d.kind === "start" || d.kind === "end") && d.clip.id === clip.id) {
        s = d.start;
        e = d.end;
      }
      if (e < view[0] || s > view[1]) continue;
      const x0 = msToX(s);
      const x1 = msToX(e);
      const isSel = clip.id === p.selectedId;
      ctx.fillStyle = isSel ? COLORS.selected : clip.status === "saved" ? COLORS.saved : COLORS.suggested;
      ctx.fillRect(x0, 4, Math.max(2, x1 - x0), bodyH - 8);
      ctx.fillStyle = isSel ? COLORS.selectedEdge : clip.status === "saved" ? COLORS.savedEdge : COLORS.suggestedEdge;
      ctx.fillRect(x0, 4, isSel ? 3 : 1.5, bodyH - 8);
      ctx.fillRect(x1 - (isSel ? 3 : 1.5), 4, isSel ? 3 : 1.5, bodyH - 8);
      if (x1 - x0 > 34 && clip.score != null) {
        ctx.font = "bold 11px ui-sans-serif, system-ui";
        ctx.fillText(String(clip.score), x0 + 5, 17);
      }
    }

    const sel = d?.kind === "select" ? null : p.selection;
    if (sel) {
      ctx.fillStyle = "rgba(34, 211, 238, 0.18)";
      ctx.fillRect(msToX(sel[0]), 0, msToX(sel[1]) - msToX(sel[0]), bodyH);
      ctx.fillStyle = "#22d3ee";
      ctx.fillRect(msToX(sel[0]), 0, 1.5, bodyH);
      ctx.fillRect(msToX(sel[1]), 0, 1.5, bodyH);
    }

    const t = store.state.timeMs;
    const px = msToX(t);
    ctx.fillStyle = "#22d3ee";
    ctx.fillRect(px - 1, 0, 2, h);
    ctx.beginPath();
    ctx.moveTo(px - 5, 0);
    ctx.lineTo(px + 5, 0);
    ctx.lineTo(px, 6);
    ctx.fill();

    if (hover.current != null) {
      const hx = hover.current;
      ctx.fillStyle = "rgba(255,255,255,0.25)";
      ctx.fillRect(hx, 0, 1, bodyH);
      const label = fmtTime(xToMs(hx), true);
      ctx.font = "11px ui-monospace, monospace";
      const tw = ctx.measureText(label).width + 8;
      const lx = clamp(hx + 6, 0, w - tw);
      ctx.fillStyle = "rgba(10,11,16,0.9)";
      ctx.fillRect(lx, bodyH - 20, tw, 16);
      ctx.fillStyle = "#e9ecf4";
      ctx.fillText(label, lx + 4, bodyH - 8);
    }
  }, [size, view, msToX, xToMs, store]);

  useEffect(() => {
    draw();
  }, [draw, clips, selectedId, selection, data]);

  // Follow the playhead: redraw on every tick; page the view forward when it runs off-screen.
  useEffect(() => {
    let lastT = -1;
    return store.subscribe(() => {
      const t = store.state.timeMs;
      if (t === lastT) return;
      lastT = t;
      const [a, b] = view;
      if (store.state.playing && (t > b || t < a) && b - a < durationMs) {
        const span = b - a;
        const start = clamp(t - span * 0.1, 0, Math.max(0, durationMs - span));
        setView([start, start + span]);
        return;
      }
      draw();
    });
  }, [store, draw, view, durationMs]);

  const snap = (ms: number, alt: boolean) => {
    if (alt || !snapPoints.length) return ms;
    let lo = 0;
    let hi = snapPoints.length - 1;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (snapPoints[mid] < ms) lo = mid + 1;
      else hi = mid;
    }
    const cands = [snapPoints[lo], snapPoints[lo - 1]].filter((v) => v != null);
    const best = cands.reduce((a, b) => (Math.abs(b - ms) < Math.abs(a - ms) ? b : a), ms);
    return Math.abs(best - ms) <= ((view[1] - view[0]) / size.w) * 8 ? best : ms;
  };

  const localX = (e: { clientX: number }) => e.clientX - canvas.current!.getBoundingClientRect().left;

  const onPointerDown = (e: React.PointerEvent) => {
    canvas.current!.setPointerCapture(e.pointerId);
    const x = localX(e);
    const ms = xToMs(x);
    if (e.button === 1 || e.shiftKey) {
      drag.current = { kind: "pan", x0: x, view0: view };
      return;
    }
    const sel = clips.find((c) => c.id === selectedId);
    if (sel) {
      if (Math.abs(msToX(sel.start_ms) - x) < 7) {
        drag.current = { kind: "start", clip: sel, start: sel.start_ms, end: sel.end_ms };
        return;
      }
      if (Math.abs(msToX(sel.end_ms) - x) < 7) {
        drag.current = { kind: "end", clip: sel, start: sel.start_ms, end: sel.end_ms };
        return;
      }
    }
    drag.current = { kind: "select", anchor: ms, x0: x, moved: false };
  };

  const onPointerMove = (e: React.PointerEvent) => {
    const x = localX(e);
    hover.current = x;
    const d = drag.current;
    const ms = clamp(xToMs(x), 0, durationMs);
    if (d?.kind === "pan") {
      const dt = ((d.x0 - x) / size.w) * (d.view0[1] - d.view0[0]);
      const span = d.view0[1] - d.view0[0];
      const start = clamp(d.view0[0] + dt, 0, Math.max(0, durationMs - span));
      setView([start, start + span]);
    } else if (d?.kind === "start") {
      d.start = clamp(snap(ms, e.altKey), 0, d.end - 1000);
    } else if (d?.kind === "end") {
      d.end = clamp(snap(ms, e.altKey), d.start + 1000, durationMs);
    } else if (d?.kind === "select" && Math.abs(x - d.x0) > 3) {
      d.moved = true;
      const a = Math.min(d.anchor, ms);
      const b = Math.max(d.anchor, ms);
      latest.current = { ...latest.current, selection: [a, b] };
      props.onSelectionChange([snap(a, e.altKey), snap(b, e.altKey)]);
    }
    draw();
  };

  const onPointerUp = (e: React.PointerEvent) => {
    const d = drag.current;
    drag.current = null;
    if (!d) return;
    if (d.kind === "start" || d.kind === "end") {
      if (d.start !== d.clip.start_ms || d.end !== d.clip.end_ms) props.onClipChange(d.clip, Math.round(d.start), Math.round(d.end));
    } else if (d.kind === "select" && !d.moved) {
      const ms = clamp(xToMs(localX(e)), 0, durationMs);
      const hit = clips.find((c) => c.status !== "discarded" && ms >= c.start_ms && ms <= c.end_ms);
      props.onSelectionChange(null);
      if (hit && hit.id !== selectedId) props.onSelectClip(hit);
      store.seek(ms);
    }
    draw();
  };

  const onWheel = (e: React.WheelEvent) => {
    if (!durationMs) return;
    const x = localX(e);
    const span = view[1] - view[0];
    if (Math.abs(e.deltaX) > Math.abs(e.deltaY) || e.shiftKey) {
      const dt = ((e.deltaX || e.deltaY) / size.w) * span;
      const start = clamp(view[0] + dt, 0, Math.max(0, durationMs - span));
      setView([start, start + span]);
      return;
    }
    const factor = Math.exp(e.deltaY * 0.0015);
    const newSpan = clamp(span * factor, 3_000, durationMs);
    const anchor = xToMs(x);
    const ratio = x / size.w;
    const start = clamp(anchor - ratio * newSpan, 0, Math.max(0, durationMs - newSpan));
    setView([start, start + newSpan]);
  };

  return (
    <div
      className="timeline"
      ref={wrap}
      onWheel={onWheel}
      title="Scroll to zoom · drag to select a range · drag clip edges to trim · shift-drag to pan · hold Alt to disable snapping"
    >
      <canvas
        ref={canvas}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onPointerLeave={() => {
          hover.current = null;
          draw();
        }}
        onDoubleClick={(e) => {
          const ms = xToMs(localX(e));
          const hit = clips.find((c) => ms >= c.start_ms && ms <= c.end_ms);
          if (hit) store.playRange([hit.start_ms, hit.end_ms]);
        }}
      />
    </div>
  );
}
