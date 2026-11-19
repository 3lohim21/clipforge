import { createContext, useContext, useSyncExternalStore } from "react";

export interface PlayerState {
  timeMs: number;
  durationMs: number;
  playing: boolean;
  loop: [number, number] | null;
}

/**
 * One store per editor session wrapping the single <video> element.
 *
 * Performance: the playhead moves 60x/second. React components subscribe through
 * `usePlayer(selector)` and only re-render when *their* selected slice changes (e.g. the
 * time display re-renders 10x/s, the play button only on play/pause). Canvas and DOM-heavy
 * views (timeline, transcript, 9:16 preview) subscribe imperatively and redraw without React.
 */
export class PlayerStore {
  video: HTMLVideoElement | null = null;
  state: PlayerState = { timeMs: 0, durationMs: 0, playing: false, loop: null };
  private listeners = new Set<() => void>();
  private raf = 0;
  private detach: (() => void) | null = null;

  subscribe = (fn: () => void): (() => void) => {
    this.listeners.add(fn);
    return () => {
      this.listeners.delete(fn);
    };
  };

  private set(patch: Partial<PlayerState>) {
    this.state = { ...this.state, ...patch };
    this.listeners.forEach((fn) => fn());
  }

  attach(video: HTMLVideoElement | null) {
    if (video === this.video) return; // React may re-invoke ref callbacks; don't drop the tick loop
    this.detach?.();
    this.detach = null;
    this.video = video;
    if (!video) return;
    const sync = () =>
      this.set({
        timeMs: video.currentTime * 1000,
        durationMs: Number.isFinite(video.duration) ? video.duration * 1000 : this.state.durationMs,
        playing: !video.paused,
      });
    const onPlay = () => {
      sync();
      this.tick();
    };
    const events = ["loadedmetadata", "seeked", "pause", "ended", "timeupdate"] as const;
    events.forEach((e) => video.addEventListener(e, sync));
    video.addEventListener("play", onPlay);
    this.detach = () => {
      events.forEach((e) => video.removeEventListener(e, sync));
      video.removeEventListener("play", onPlay);
      cancelAnimationFrame(this.raf);
    };
    sync();
    if (!video.paused) this.tick();
  }

  private tick = () => {
    const v = this.video;
    if (!v || v.paused) return;
    const t = v.currentTime * 1000;
    const loop = this.state.loop;
    if (loop && (t >= loop[1] - 20 || t < loop[0] - 500)) {
      v.currentTime = loop[0] / 1000;
    }
    this.set({ timeMs: v.currentTime * 1000 });
    this.raf = requestAnimationFrame(this.tick);
  };

  seek(ms: number) {
    const v = this.video;
    if (!v) return;
    const d = this.state.durationMs || Infinity;
    v.currentTime = Math.max(0, Math.min(ms, d - 50)) / 1000;
    this.set({ timeMs: v.currentTime * 1000 });
  }

  play() {
    void this.video?.play().catch(() => undefined);
  }

  pause() {
    this.video?.pause();
  }

  toggle() {
    if (!this.video) return;
    if (this.video.paused) this.play();
    else this.pause();
  }

  setLoop(range: [number, number] | null) {
    this.set({ loop: range });
  }

  /** Jump into a range and play it on loop (preview a clip). */
  playRange(range: [number, number]) {
    this.setLoop(range);
    this.seek(range[0]);
    this.play();
  }
}

export const PlayerContext = createContext<PlayerStore | null>(null);

export function usePlayerStore(): PlayerStore {
  const store = useContext(PlayerContext);
  if (!store) throw new Error("PlayerContext missing");
  return store;
}

export function usePlayer<T>(selector: (s: PlayerState) => T): T {
  const store = usePlayerStore();
  return useSyncExternalStore(store.subscribe, () => selector(store.state));
}
