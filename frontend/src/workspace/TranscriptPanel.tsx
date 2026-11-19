import { Fragment, memo, useEffect, useMemo, useRef, useState } from "react";
import type { Clip, TranscriptWord } from "../api/types";
import { Scissors } from "../components/icons";
import { Empty, Spinner } from "../components/ui";
import { fmtDuration, fmtTime, lastStartingBefore } from "../lib/format";
import { usePlayerStore } from "../lib/player";

interface Props {
  words: TranscriptWord[] | undefined;
  loading: boolean;
  selectedClip: Clip | null;
  onCreateClip: (start: number, end: number) => void;
}

interface Paragraph {
  i0: number;
  i1: number; // exclusive
}

function paragraphs(words: TranscriptWord[]): Paragraph[] {
  const out: Paragraph[] = [];
  let start = 0;
  for (let i = 0; i < words.length; i++) {
    const next = words[i + 1];
    const len = i - start + 1;
    const sentenceEnd = /[.?!]["')\]]?$/.test(words[i][2]);
    const gap = next ? next[0] - words[i][1] : Infinity;
    if (!next || gap > 1500 || (len > 60 && sentenceEnd) || len >= 130) {
      out.push({ i0: start, i1: i + 1 });
      start = i + 1;
    }
  }
  return out;
}

/**
 * Text-based clipping. Rendering 30k word spans is fine for the browser as long as React
 * never re-renders them: paragraphs are memoised, off-screen ones are skipped by
 * `content-visibility: auto`, and the playing word is highlighted by toggling one class.
 */
export function TranscriptPanel({ words, loading, selectedClip, onCreateClip }: Props) {
  const store = usePlayerStore();
  const container = useRef<HTMLDivElement>(null);
  const [follow, setFollow] = useState(true);
  const [selection, setSelection] = useState<[number, number] | null>(null);
  const [query, setQuery] = useState("");
  const [hitIndex, setHitIndex] = useState(0);
  const paras = useMemo(() => (words ? paragraphs(words) : []), [words]);
  const userScrolledAt = useRef(0);

  // Active-word highlight + auto-follow, outside React.
  useEffect(() => {
    if (!words?.length) return;
    let active = -1;
    return store.subscribe(() => {
      const idx = lastStartingBefore(words, store.state.timeMs, (w) => w[0]);
      if (idx === active) return;
      const root = container.current;
      root?.querySelector(`[data-i="${active}"]`)?.classList.remove("active");
      active = idx;
      const el = root?.querySelector(`[data-i="${idx}"]`) as HTMLElement | null;
      el?.classList.add("active");
      if (el && follow && store.state.playing && Date.now() - userScrolledAt.current > 4000) {
        const box = root!.getBoundingClientRect();
        const r = el.getBoundingClientRect();
        if (r.top < box.top + 40 || r.bottom > box.bottom - 60) el.scrollIntoView({ block: "center", behavior: "smooth" });
      }
    });
  }, [store, words, follow]);

  // Tint the words inside the selected clip.
  useEffect(() => {
    const root = container.current;
    if (!root || !words || !selectedClip) return;
    const a = lastStartingBefore(words, selectedClip.start_ms, (w) => w[0]) + 1;
    const b = lastStartingBefore(words, selectedClip.end_ms - 1, (w) => w[0]);
    const els: Element[] = [];
    for (let i = Math.max(0, a); i <= b; i++) {
      const el = root.querySelector(`[data-i="${i}"]`);
      if (el) {
        el.classList.add("inclip");
        els.push(el);
      }
    }
    const first = root.querySelector(`[data-i="${Math.max(0, a)}"]`) as HTMLElement | null;
    first?.scrollIntoView({ block: "center" });
    return () => els.forEach((el) => el.classList.remove("inclip"));
  }, [selectedClip?.id, selectedClip?.start_ms, selectedClip?.end_ms, words, selectedClip]);

  // Search: mark hits, jump between them.
  const hits = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!words || q.length < 2) return [];
    const terms = q.split(/\s+/);
    const out: number[] = [];
    for (let i = 0; i < words.length; i++) {
      if (terms.every((t, k) => words[i + k]?.[2].toLowerCase().replace(/[^\p{L}\p{N}']/gu, "").includes(t))) out.push(i);
    }
    return out;
  }, [query, words]);

  useEffect(() => {
    const root = container.current;
    if (!root) return;
    const els = hits.slice(0, 500).map((i) => root.querySelector(`[data-i="${i}"]`)).filter(Boolean) as Element[];
    els.forEach((el) => el.classList.add("hit"));
    return () => els.forEach((el) => el.classList.remove("hit"));
  }, [hits]);

  const jumpToHit = (dir: 1 | -1) => {
    if (!hits.length || !words) return;
    const next = (hitIndex + dir + hits.length) % hits.length;
    setHitIndex(next);
    store.seek(words[hits[next]][0]);
    container.current?.querySelector(`[data-i="${hits[next]}"]`)?.scrollIntoView({ block: "center" });
  };

  const onMouseUp = () => {
    const sel = window.getSelection();
    if (!sel || sel.isCollapsed || !words) {
      setSelection(null);
      return;
    }
    const idx = (node: Node | null) => {
      const el = (node instanceof Element ? node : node?.parentElement)?.closest("[data-i]");
      return el ? Number(el.getAttribute("data-i")) : null;
    };
    const a = idx(sel.anchorNode);
    const b = idx(sel.focusNode);
    if (a == null || b == null) return;
    const [i, j] = a <= b ? [a, b] : [b, a];
    setSelection([words[i][0], words[j][1]]);
  };

  const onClick = (e: React.MouseEvent) => {
    const sel = window.getSelection();
    if (sel && !sel.isCollapsed) return;
    const t = (e.target as HTMLElement).closest("[data-i],[data-t]") as HTMLElement | null;
    if (!t || !words) return;
    const ms = t.dataset.i != null ? words[Number(t.dataset.i)][0] : Number(t.dataset.t);
    store.seek(ms);
  };

  if (loading) return <div style={{ padding: 20 }}><Spinner /></div>;
  if (!words?.length) return <Empty title="No transcript">This video has no speech, or transcription hasn't finished.</Empty>;

  return (
    <>
      <div className="filter-row">
        <input
          className="input"
          style={{ height: 30, padding: "4px 8px" }}
          placeholder="Search words…"
          value={query}
          onChange={(e) => {
            setQuery(e.target.value);
            setHitIndex(-1);
          }}
          onKeyDown={(e) => e.key === "Enter" && jumpToHit(e.shiftKey ? -1 : 1)}
        />
        {query.trim().length >= 2 && <span className="mono muted" style={{ fontSize: 12, whiteSpace: "nowrap" }}>{hits.length} hits</span>}
        <label className="toggle" title="Scroll with playback">
          <input type="checkbox" checked={follow} onChange={(e) => setFollow(e.target.checked)} /> Follow
        </label>
      </div>
      <div
        className="panel-scroll"
        ref={container}
        onMouseUp={onMouseUp}
        onClick={onClick}
        onWheel={() => (userScrolledAt.current = Date.now())}
      >
        <TranscriptBody words={words} paras={paras} />
      </div>
      {selection && (
        <div className="sel-bar">
          <span className="mono grow" style={{ fontSize: 12.5 }}>
            {fmtTime(selection[0])} → {fmtTime(selection[1])} · {fmtDuration(selection[1] - selection[0])}
          </span>
          <button
            className="btn sm primary"
            onClick={() => {
              onCreateClip(Math.max(0, selection[0] - 120), selection[1] + 250);
              setSelection(null);
              window.getSelection()?.removeAllRanges();
            }}
          >
            <Scissors /> Clip it
          </button>
        </div>
      )}
    </>
  );
}

const TranscriptBody = memo(function TranscriptBody({ words, paras }: { words: TranscriptWord[]; paras: Paragraph[] }) {
  return (
    <div className="transcript">
      {paras.map((p) => (
        <p key={p.i0}>
          <span className="ts" data-t={words[p.i0][0]}>
            {fmtTime(words[p.i0][0])}
          </span>
          {words.slice(p.i0, p.i1).map((w, k) => (
            <Fragment key={k}>
              <span className="w" data-i={p.i0 + k}>
                {w[2]}
              </span>{" "}
            </Fragment>
          ))}
        </p>
      ))}
    </div>
  );
});
