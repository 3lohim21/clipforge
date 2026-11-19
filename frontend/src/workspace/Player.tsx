import { useCallback, useEffect, useRef } from "react";
import type { Clip, LayoutInfo, Video } from "../api/types";
import { cropCenterAt } from "../lib/crop";
import { usePlayerStore } from "../lib/player";

interface Props {
  video: Video;
  clip: Clip | null;
  layout: LayoutInfo | null;
  onCropDrag: (cropX: number) => void;
}

/** The single <video> element of the editor. Every other view (timeline, transcript, 9:16
 * preview) reads from it through the PlayerStore, so there is exactly one decoder running. */
export function Player({ video, clip, layout, onCropDrag }: Props) {
  const store = usePlayerStore();
  const box = useRef<HTMLDivElement>(null);
  const guide = useRef<HTMLDivElement>(null);
  const dragging = useRef(false);
  const attachVideo = useCallback((el: HTMLVideoElement | null) => store.attach(el), [store]);

  const faceLayout = layout && (layout.id === "vertical_fill" || layout.id === "square_fill");
  const srcAspect = (video.width ?? 16) / (video.height ?? 9);
  const cropFrac = faceLayout ? Math.min(1, layout.width / layout.height / srcAspect) : 1;
  const showGuide = !!clip && !!faceLayout && cropFrac < 0.999;

  // Position the crop guide imperatively (60 fps without React re-renders).
  useEffect(() => {
    if (!showGuide || !clip) return;
    const update = () => {
      if (!guide.current || dragging.current) return;
      const t = store.state.timeMs;
      const inClip = t >= clip.start_ms && t <= clip.end_ms;
      const cx = cropCenterAt(clip, inClip ? t : clip.start_ms);
      const left = Math.min(Math.max(cx - cropFrac / 2, 0), 1 - cropFrac);
      guide.current.style.left = `${left * 100}%`;
      guide.current.style.opacity = inClip ? "1" : "0.55";
    };
    update();
    return store.subscribe(update);
  }, [store, clip, showGuide, cropFrac]);

  const manual = clip?.edit.reframe === "manual";
  const onGuideDown = (e: React.PointerEvent) => {
    if (!manual) return;
    e.stopPropagation();
    dragging.current = true;
    (e.target as HTMLElement).setPointerCapture(e.pointerId);
  };
  const onGuideMove = (e: React.PointerEvent) => {
    if (!dragging.current || !box.current || !guide.current) return;
    const rect = box.current.getBoundingClientRect();
    const cx = Math.min(Math.max((e.clientX - rect.left) / rect.width, cropFrac / 2), 1 - cropFrac / 2);
    guide.current.style.left = `${(cx - cropFrac / 2) * 100}%`;
    guide.current.dataset.cx = String(cx);
  };
  const onGuideUp = () => {
    if (!dragging.current) return;
    dragging.current = false;
    const cx = Number(guide.current?.dataset.cx);
    if (Number.isFinite(cx)) onCropDrag(Math.round(cx * 1000) / 1000);
  };

  return (
    <div className="player-wrap">
      <div className="player-box" ref={box} style={{ ["--ar" as string]: `${video.width ?? 16} / ${video.height ?? 9}` }}>
        <video
          ref={attachVideo}
          src={video.playback_url ?? undefined}
          poster={video.poster_url ?? undefined}
          preload="auto"
          playsInline
          onClick={() => store.toggle()}
        />
        {showGuide && (
          <div
            ref={guide}
            className={`crop-guide ${manual ? "draggable" : ""}`}
            style={{ width: `${cropFrac * 100}%` }}
            title={manual ? "Drag to frame the shot" : "9:16 export frame (auto face tracking)"}
            onPointerDown={onGuideDown}
            onPointerMove={onGuideMove}
            onPointerUp={onGuideUp}
          />
        )}
      </div>
    </div>
  );
}
