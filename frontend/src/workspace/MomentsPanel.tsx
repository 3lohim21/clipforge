import { memo, useMemo, useState } from "react";
import type { Clip, ClipStatus } from "../api/types";
import { Check, Sparkles, X } from "../components/icons";
import { Empty, Score, StatusBadge } from "../components/ui";
import { fmtDuration, fmtTime } from "../lib/format";

interface Props {
  clips: Clip[];
  selectedId: string | null;
  analysisUsedLlm: boolean;
  onSelect: (clip: Clip) => void;
  onStatus: (clip: Clip, status: ClipStatus) => void;
}

type Filter = "all" | "suggested" | "saved";

export function MomentsPanel({ clips, selectedId, analysisUsedLlm, onSelect, onStatus }: Props) {
  const [filter, setFilter] = useState<Filter>("all");
  const [sort, setSort] = useState<"score" | "time">("score");
  const discarded = clips.filter((c) => c.status === "discarded").length;

  const visible = useMemo(() => {
    const list = clips.filter((c) => (filter === "all" ? c.status !== "discarded" : c.status === filter));
    return [...list].sort((a, b) => (sort === "score" ? (b.score ?? -1) - (a.score ?? -1) || a.start_ms - b.start_ms : a.start_ms - b.start_ms));
  }, [clips, filter, sort]);

  return (
    <>
      <div className="filter-row">
        <div className="seg grow">
          {(["all", "suggested", "saved"] as Filter[]).map((f) => (
            <button key={f} className={filter === f ? "on" : ""} onClick={() => setFilter(f)}>
              {f === "all" ? "All" : f === "saved" ? "Saved" : "AI picks"}
            </button>
          ))}
        </div>
        <button className="btn sm ghost" onClick={() => setSort(sort === "score" ? "time" : "score")} title="Sort order">
          {sort === "score" ? "By score" : "By time"}
        </button>
      </div>
      <div className="panel-scroll">
        {!visible.length ? (
          <Empty icon={<Sparkles width={24} height={24} />} title="No clips here">
            Drag on the timeline or select words in the transcript to make one.
          </Empty>
        ) : (
          visible.map((c) => <MomentRow key={c.id} clip={c} active={c.id === selectedId} onSelect={onSelect} onStatus={onStatus} />)
        )}
        <div className="faint" style={{ fontSize: 12, padding: "10px 12px" }}>
          {analysisUsedLlm ? "Ranked by AI + signal analysis." : "Ranked by hook, energy, pace, emotion & audience signals."}
          {discarded > 0 && ` ${discarded} discarded hidden.`}
        </div>
      </div>
    </>
  );
}

const MomentRow = memo(function MomentRow({
  clip,
  active,
  onSelect,
  onStatus,
}: {
  clip: Clip;
  active: boolean;
  onSelect: (c: Clip) => void;
  onStatus: (c: Clip, s: ClipStatus) => void;
}) {
  const r = clip.latest_render;
  return (
    <div className={`moment ${active ? "active" : ""} ${clip.status === "discarded" ? "discarded" : ""}`} onClick={() => onSelect(clip)}>
      <Score value={clip.score} />
      <div style={{ minWidth: 0 }}>
        <h4>{clip.title || "Untitled"}</h4>
        <div className="meta">
          <span className="mono">
            {fmtTime(clip.start_ms)} · {fmtDuration(clip.end_ms - clip.start_ms)}
          </span>
          {clip.status === "saved" && <StatusBadge status="saved" />}
          {r && r.status !== "done" && <StatusBadge status={r.status} />}
          {r?.status === "done" && <span className="badge green">Exported</span>}
        </div>
        {clip.reason && (
          <div className="chips">
            {clip.reason.split(" · ").map((x) => (
              <span key={x} className="chip">
                {x}
              </span>
            ))}
          </div>
        )}
        {active && clip.status !== "discarded" && (
          <div className="actions" onClick={(e) => e.stopPropagation()}>
            {clip.status === "suggested" && (
              <button className="btn sm" onClick={() => onStatus(clip, "saved")}>
                <Check /> Keep <kbd>S</kbd>
              </button>
            )}
            <button className="btn sm ghost" onClick={() => onStatus(clip, "discarded")}>
              <X /> Discard <kbd>X</kbd>
            </button>
          </div>
        )}
      </div>
    </div>
  );
});
