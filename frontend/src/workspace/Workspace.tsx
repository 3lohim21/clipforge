import { useCallback, useEffect, useMemo, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router";
import {
  useCatalog,
  useClipMutations,
  useClips,
  useProject,
  useTimeline,
  useTranscript,
  useVideo,
  useVideoAction,
} from "../api/queries";
import type { Clip, ClipStatus, Video } from "../api/types";
import { TopBar } from "../components/AppShell";
import { Pause, Play, Refresh, Scissors } from "../components/icons";
import { Empty, errorText, ProgressBar, Spinner, StatusBadge, useToast } from "../components/ui";
import { useCaptionFonts } from "../lib/fonts";
import { fmtDuration, fmtTime, STATUS_LABEL } from "../lib/format";
import { PlayerContext, PlayerStore, usePlayer, usePlayerStore } from "../lib/player";
import { ClipEditor, fullEdit } from "./ClipEditor";
import { MomentsPanel } from "./MomentsPanel";
import { Player } from "./Player";
import { Timeline } from "./Timeline";
import { TranscriptPanel } from "./TranscriptPanel";

export default function WorkspacePage() {
  const { videoId = "" } = useParams();
  const [store] = useState(() => new PlayerStore());
  const video = useVideo(videoId);
  const project = useProject(video.data?.project_id ?? "");

  return (
    <PlayerContext.Provider value={store}>
      <div className="shell">
        <TopBar>
          <Link to="/">Clients</Link>
          <span className="sep">/</span>
          {video.data && (
            <>
              <Link to={`/projects/${video.data.project_id}`} className="ellipsis">
                {project.data?.name ?? "Project"}
              </Link>
              <span className="sep">/</span>
              <span className="ellipsis" style={{ color: "var(--text)", maxWidth: 420 }}>
                {video.data.title}
              </span>
            </>
          )}
        </TopBar>
        {!video.data ? (
          video.error ? (
            <Empty title="Video not found" />
          ) : (
            <div className="auth">
              <Spinner />
            </div>
          )
        ) : video.data.status !== "ready" ? (
          <Processing video={video.data} />
        ) : (
          <Editor video={video.data} />
        )}
      </div>
    </PlayerContext.Provider>
  );
}

function Processing({ video }: { video: Video }) {
  const action = useVideoAction(video.project_id);
  const steps = ["downloading", "processing", "transcribing", "analyzing"];
  const current = steps.indexOf(video.status);
  return (
    <div className="container" style={{ maxWidth: 620 }}>
      <div className="card pad stack" style={{ marginTop: 40 }}>
        <div className="row">
          <h2 style={{ margin: 0, fontSize: 18 }} className="grow ellipsis">
            {video.title}
          </h2>
          <StatusBadge status={video.status} />
        </div>
        {video.status === "failed" ? (
          <>
            <div className="error-text">{video.error}</div>
            <div>
              <button className="btn" onClick={() => action.mutate({ id: video.id, action: "retry" })}>
                <Refresh /> Retry
              </button>
            </div>
          </>
        ) : (
          <>
            <ProgressBar value={(Math.max(0, current) + video.progress) / steps.length} />
            <div className="row wrap muted" style={{ fontSize: 13 }}>
              {steps.map((s, i) => (
                <span key={s} style={{ color: i < current ? "var(--green)" : i === current ? "var(--text)" : undefined }}>
                  {i < current ? "✓ " : i === current ? "● " : "○ "}
                  {STATUS_LABEL[s]}
                </span>
              ))}
            </div>
            <div className="faint" style={{ fontSize: 13 }}>
              You can leave this page — processing continues on the server. The editor opens automatically when
              moments are ready.
            </div>
          </>
        )}
      </div>
    </div>
  );
}

function Editor({ video }: { video: Video }) {
  const toast = useToast();
  const store = usePlayerStore();
  const clips = useClips(video.id);
  const catalog = useCatalog().data;
  const transcript = useTranscript(video);
  const timeline = useTimeline(video);
  const { update, create, render } = useClipMutations(video.id);
  const videoAction = useVideoAction(video.project_id);
  const [params, setParams] = useSearchParams();
  const [leftTab, setLeftTab] = useState<"moments" | "transcript">("moments");
  const [selection, setSelection] = useState<[number, number] | null>(null);
  useCaptionFonts(catalog?.caption_presets);

  const list = useMemo(() => clips.data ?? [], [clips.data]);
  const selected = list.find((c) => c.id === params.get("clip")) ?? null;
  const ordered = useMemo(
    () =>
      list
        .filter((c) => c.status !== "discarded")
        .sort((a, b) => (b.score ?? -1) - (a.score ?? -1) || a.start_ms - b.start_ms),
    [list],
  );
  const words = transcript.data?.words;
  const snapPoints = useMemo(() => {
    if (!words) return [];
    const pts: number[] = [];
    for (const w of words) pts.push(w[0], w[1]);
    return pts.sort((a, b) => a - b);
  }, [words]);

  const fail = (e: unknown) => toast(errorText(e), true);

  const select = useCallback(
    (clip: Clip | null, play = false) => {
      setParams(clip ? { clip: clip.id } : {}, { replace: true });
      if (!clip) {
        store.setLoop(null);
        return;
      }
      setSelection(null);
      if (play) store.playRange([clip.start_ms, clip.end_ms]);
      else {
        store.setLoop([clip.start_ms, clip.end_ms]);
        store.seek(clip.start_ms);
      }
    },
    [setParams, store],
  );

  // Keep the loop in sync with the selected clip, including after its bounds are edited, and
  // jump to it when it is opened from a link (e.g. the clips library).
  const selStart = selected?.start_ms;
  const selEnd = selected?.end_ms;
  useEffect(() => {
    if (selStart != null && selEnd != null) store.setLoop([selStart, selEnd]);
  }, [store, selStart, selEnd]);
  const selId = selected?.id;
  useEffect(() => {
    if (selStart != null) store.seek(selStart);
    // only when a different clip is opened
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selId]);

  const setRange = (clip: Clip, start: number, end: number) =>
    update.mutate({ id: clip.id, body: { start_ms: start, end_ms: end } }, { onError: fail });

  const setStatus = (clip: Clip, status: ClipStatus) => {
    update.mutate({ id: clip.id, body: { status } }, { onError: fail });
    if (status === "discarded" && clip.id === selected?.id) {
      // Fast triage: discarding jumps straight to the next-best moment.
      const i = ordered.findIndex((c) => c.id === clip.id);
      const next = ordered[i + 1] ?? ordered[i - 1];
      select(next && next.id !== clip.id ? next : null, true);
    }
  };

  const setCrop = (clip: Clip, cropX: number) => {
    if (!catalog) return;
    const { track: _t, track_range: _r, ...edit } = fullEdit(clip, catalog);
    update.mutate({ id: clip.id, body: { edit: { ...edit, reframe: "manual", crop_x: cropX } } }, { onError: fail });
  };

  const createClip = async (start: number, end: number) => {
    try {
      const clip = await create.mutateAsync({ start_ms: Math.round(start), end_ms: Math.round(end), title: "" });
      select(clip, true);
    } catch (e) {
      fail(e);
    }
  };

  const createFromSelection = () => {
    if (selection && selection[1] - selection[0] >= 1000) void createClip(selection[0], selection[1]);
  };

  const exportClip = (clip: Clip) =>
    render.mutate(clip.id, { onSuccess: () => toast("Export queued"), onError: fail });

  const step = (dir: 1 | -1) => {
    if (!ordered.length) return;
    const i = selected ? ordered.findIndex((c) => c.id === selected.id) : -1;
    const next = ordered[Math.min(ordered.length - 1, Math.max(0, i + dir))];
    if (next) select(next, true);
  };

  // Keyboard-first review (re-bound each render so handlers always see current state).
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const target = e.target as HTMLElement;
      if (target.closest("input, textarea, select, [contenteditable]") || e.metaKey || e.ctrlKey || e.altKey) return;
      const t = store.state.timeMs;
      const k = e.key.toLowerCase();
      let handled = true;
      if (e.key === " ") store.toggle();
      else if (e.key === "ArrowLeft") store.seek(t - (e.shiftKey ? 100 : 1000));
      else if (e.key === "ArrowRight") store.seek(t + (e.shiftKey ? 100 : 1000));
      else if (e.key === "ArrowUp") step(-1);
      else if (e.key === "ArrowDown") step(1);
      else if (k === "j") store.seek(t - 5000);
      else if (k === "l") store.seek(t + 5000);
      else if (k === "i") setSelection([t, Math.max(t + 1000, selection?.[1] ?? t + 30_000)]);
      else if (k === "o") setSelection([Math.min(selection?.[0] ?? Math.max(0, t - 30_000), t - 1000), t]);
      else if (k === "c" || e.key === "Enter") createFromSelection();
      else if (k === "e" && selected) exportClip(selected);
      else if (k === "s" && selected && selected.status !== "saved") setStatus(selected, "saved");
      else if ((k === "x" || e.key === "Delete") && selected) setStatus(selected, "discarded");
      else if (e.key === "[" && selected) setRange(selected, Math.min(t, selected.end_ms - 1000), selected.end_ms);
      else if (e.key === "]" && selected) setRange(selected, selected.start_ms, Math.max(t, selected.start_ms + 1000));
      else if (e.key === "Escape") setSelection(null);
      else handled = false;
      if (handled) e.preventDefault();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });

  if (!catalog || clips.isPending) {
    return (
      <div className="auth">
        <Spinner />
      </div>
    );
  }
  const layout = selected ? catalog.layouts.find((l) => l.id === fullEdit(selected, catalog).layout) ?? null : null;

  return (
    <div className="ws">
      <aside className="ws-col">
        <nav className="tabs">
          <button className={`tab ${leftTab === "moments" ? "active" : ""}`} onClick={() => setLeftTab("moments")}>
            Moments <span className="badge" style={{ marginLeft: 4 }}>{ordered.length}</span>
          </button>
          <button className={`tab ${leftTab === "transcript" ? "active" : ""}`} onClick={() => setLeftTab("transcript")}>
            Transcript
          </button>
          <span className="spacer" />
          <button
            className="btn sm ghost"
            title="Find moments again (keeps saved clips)"
            style={{ alignSelf: "center" }}
            onClick={() => {
              if (confirm("Find moments again? Saved clips are kept; unreviewed suggestions are replaced.")) {
                videoAction.mutate({ id: video.id, action: "reanalyze" }, { onError: fail });
              }
            }}
          >
            <Refresh />
          </button>
        </nav>
        {leftTab === "moments" ? (
          <MomentsPanel
            clips={list}
            selectedId={selected?.id ?? null}
            analysisUsedLlm={!!video.analysis?.llm?.used}
            onSelect={(c) => select(c, true)}
            onStatus={setStatus}
          />
        ) : (
          <TranscriptPanel
            words={words}
            loading={transcript.isPending && video.has_transcript}
            selectedClip={selected}
            onCreateClip={(s, e) => void createClip(s, e)}
          />
        )}
      </aside>

      <section className="ws-center">
        <Player video={video} clip={selected} layout={layout} onCropDrag={(x) => selected && setCrop(selected, x)} />
        <Transport selection={selection} onCreate={createFromSelection} onClear={() => setSelection(null)} />
        <Timeline
          durationMs={video.duration_ms ?? 0}
          data={timeline.data}
          clips={list}
          selectedId={selected?.id ?? null}
          selection={selection}
          snapPoints={snapPoints}
          onSelectClip={(c) => select(c, false)}
          onClipChange={setRange}
          onSelectionChange={setSelection}
        />
      </section>

      <aside className="ws-col">
        <div className="panel-scroll">
          {selected ? (
            <ClipEditor key={selected.id} video={video} clip={selected} catalog={catalog} onDeleted={() => select(null)} />
          ) : (
            <Shortcuts />
          )}
        </div>
      </aside>
    </div>
  );
}

function Transport({
  selection,
  onCreate,
  onClear,
}: {
  selection: [number, number] | null;
  onCreate: () => void;
  onClear: () => void;
}) {
  const store = usePlayerStore();
  const playing = usePlayer((s) => s.playing);
  const tenths = usePlayer((s) => Math.floor(s.timeMs / 100)); // re-render 10x/s, not 60x
  const duration = usePlayer((s) => s.durationMs);
  const looping = usePlayer((s) => !!s.loop);
  const [rate, setRate] = useState(1);
  useEffect(() => {
    if (store.video) store.video.playbackRate = rate;
  }, [rate, store]);

  return (
    <div className="transport">
      <button className="btn icon primary" onClick={() => store.toggle()} title="Play / pause (space)">
        {playing ? <Pause /> : <Play />}
      </button>
      <span className="timecode">
        <b>{fmtTime(tenths * 100, true)}</b> / {fmtTime(duration)}
      </span>
      <select
        className="select"
        style={{ width: 74, height: 30, padding: "2px 6px" }}
        value={rate}
        onChange={(e) => setRate(Number(e.target.value))}
        title="Review speed"
      >
        {[1, 1.25, 1.5, 2].map((r) => (
          <option key={r} value={r}>
            {r}×
          </option>
        ))}
      </select>
      {looping && <span className="badge violet">Looping clip</span>}
      <span className="spacer" />
      {selection ? (
        <>
          <span className="mono muted" style={{ fontSize: 12.5 }}>
            {fmtTime(selection[0])} → {fmtTime(selection[1])} · {fmtDuration(selection[1] - selection[0])}
          </span>
          <button className="btn sm primary" onClick={onCreate} disabled={selection[1] - selection[0] < 1000}>
            <Scissors /> Create clip <kbd>C</kbd>
          </button>
          <button className="btn sm ghost" onClick={onClear}>
            Clear
          </button>
        </>
      ) : (
        <span className="faint" style={{ fontSize: 12 }}>
          <kbd>I</kbd> / <kbd>O</kbd> mark in &amp; out · drag on the timeline · select transcript words
        </span>
      )}
    </div>
  );
}

function Shortcuts() {
  const rows: [string, string][] = [
    ["Space", "Play / pause"],
    ["↑ / ↓", "Previous / next moment"],
    ["S", "Keep the moment"],
    ["X", "Discard (jumps to next)"],
    ["E", "Export the selected clip"],
    ["I / O", "Mark in / out"],
    ["C", "Create clip from marks"],
    ["[ / ]", "Set clip start / end to playhead"],
    ["← / →", "Seek 1s (shift: 0.1s)"],
    ["J / L", "Seek 5s"],
  ];
  return (
    <div className="editor">
      <div className="empty" style={{ padding: "26px 8px 8px" }}>
        <Scissors width={26} height={26} />
        <h3>Pick a moment to edit</h3>
        <div>Choose an AI pick on the left, drag on the timeline, or select transcript words.</div>
      </div>
      <div className="section-title">Keyboard</div>
      <table className="table">
        <tbody>
          {rows.map(([k, v]) => (
            <tr key={k}>
              <td style={{ width: 90 }}>
                <kbd>{k}</kbd>
              </td>
              <td className="muted">{v}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
