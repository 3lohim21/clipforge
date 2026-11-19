import { useEffect, useMemo, useRef, useState, type CSSProperties } from "react";
import { useCaptions, useClipMutations, useClipRenders, usePostMutations } from "../api/queries";
import type { CaptionChunk, CaptionPreset, Catalog, Clip, ClipEdit, Layout, LayoutInfo, Render, Video } from "../api/types";
import { Copy, Download, Link as LinkIcon, Scissors, Trash } from "../components/icons";
import { errorText, ProgressBar, StatusBadge, useToast } from "../components/ui";
import { cropCenterAt, cropRect } from "../lib/crop";
import { fontFamilyFor } from "../lib/fonts";
import { clamp, fmtBytes, fmtDuration, fmtTime, lastStartingBefore } from "../lib/format";
import { usePlayer, usePlayerStore } from "../lib/player";

interface Props {
  video: Video;
  clip: Clip;
  catalog: Catalog;
  onDeleted: () => void;
}

const LAYOUT_LABEL: Record<Layout, string> = {
  vertical_fill: "9:16 Face",
  vertical_fit: "9:16 Full",
  square_fill: "1:1",
  landscape: "16:9",
};

export function fullEdit(clip: Clip, catalog: Catalog): ClipEdit {
  return {
    layout: catalog.defaults.layout,
    captions: true,
    caption_preset: catalog.defaults.caption_preset,
    caption_position: catalog.defaults.caption_position,
    highlight_color: null,
    title_overlay: false,
    reframe: "auto",
    crop_x: null,
    ...clip.edit,
  } as ClipEdit;
}

export function ClipEditor({ video, clip, catalog, onDeleted }: Props) {
  const { update, remove, render } = useClipMutations(video.id);
  const renders = useClipRenders(clip.id);
  const posts = usePostMutations(clip.project_id);
  const toast = useToast();
  const store = usePlayerStore();
  const edit = fullEdit(clip, catalog);
  const preset = catalog.caption_presets.find((p) => p.id === edit.caption_preset) ?? catalog.caption_presets[0];
  const layout = catalog.layouts.find((l) => l.id === edit.layout) ?? catalog.layouts[0];
  const [title, setTitle] = useState(clip.title);
  const [tags, setTags] = useState(clip.hashtags.join(" "));
  const [postUrl, setPostUrl] = useState("");

  useEffect(() => {
    setTitle(clip.title);
    setTags(clip.hashtags.join(" "));
  }, [clip.id, clip.title, clip.hashtags]);

  const save = (body: Parameters<typeof update.mutate>[0]["body"]) =>
    update.mutate({ id: clip.id, body }, { onError: (e) => toast(errorText(e), true) });
  const setEdit = (patch: Partial<ClipEdit>) => {
    const { track: _t, track_range: _r, ...rest } = { ...edit, ...patch };
    save({ edit: rest as ClipEdit });
  };
  const setRange = (start: number, end: number) => {
    const s = clamp(Math.round(start), 0, (video.duration_ms ?? end) - 1000);
    const e = clamp(Math.round(end), s + 1000, video.duration_ms ?? end);
    save({ start_ms: s, end_ms: e });
    store.setLoop([s, e]);
  };

  // Debounced autosave for typed fields. Pending edits are flushed on unmount (switching
  // clips) so nothing typed is ever lost.
  const pending = useRef<{ title?: string; hashtags?: string[] }>({});
  const flush = () => {
    const body = pending.current;
    pending.current = {};
    if (body.title !== undefined || body.hashtags !== undefined) save(body);
  };
  useEffect(() => {
    if (title === clip.title) return;
    pending.current.title = title;
    const t = window.setTimeout(flush, 500);
    return () => window.clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [title]);
  useEffect(() => {
    const list = tags.split(/[\s,]+/).map((t) => t.replace(/^#/, "")).filter(Boolean);
    if (list.join(" ") === clip.hashtags.join(" ")) return;
    pending.current.hashtags = list;
    const t = window.setTimeout(flush, 600);
    return () => window.clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tags]);
  useEffect(() => () => flush(), []); // eslint-disable-line react-hooks/exhaustive-deps

  const doExport = () =>
    render.mutate(clip.id, {
      onSuccess: () => toast("Export queued — it renders in the background"),
      onError: (e) => toast(errorText(e), true),
    });

  const copyCaption = () =>
    navigator.clipboard
      .writeText([title, clip.hashtags.map((h) => `#${h}`).join(" ")].filter(Boolean).join("\n\n"))
      .then(() => toast("Title + hashtags copied"));

  const faceLayout = edit.layout === "vertical_fill" || edit.layout === "square_fill";

  return (
    <div className="editor">
      <VerticalPreview video={video} clip={clip} layout={layout} preset={preset} edit={edit} catalog={catalog} />

      <div className="row">
        <button className="btn primary grow" onClick={doExport} disabled={render.isPending}>
          <Scissors /> Export {LAYOUT_LABEL[edit.layout]} <kbd style={{ color: "white", borderColor: "rgba(255,255,255,0.4)", background: "transparent" }}>E</kbd>
        </button>
        <button className="btn icon" title="Copy title + hashtags" onClick={copyCaption}>
          <Copy />
        </button>
      </div>
      {renders.data?.length ? (
        <div className="stack" style={{ gap: 6 }}>
          {renders.data.slice(0, 4).map((r) => (
            <ExportRow key={r.id} r={r} />
          ))}
        </div>
      ) : null}

      <div className="section-title">Clip</div>
      <label className="field">
        <span className="label">Title</span>
        <input className="input" value={title} maxLength={200} onChange={(e) => setTitle(e.target.value)} />
      </label>
      <label className="field">
        <span className="label">Hashtags</span>
        <input className="input" value={tags} onChange={(e) => setTags(e.target.value)} placeholder="podcast money mindset" />
      </label>
      <TrimControls clip={clip} onChange={setRange} />

      <div className="section-title">Format</div>
      <div className="seg">
        {catalog.layouts.map((l) => (
          <button key={l.id} className={edit.layout === l.id ? "on" : ""} title={l.description} onClick={() => setEdit({ layout: l.id })}>
            {LAYOUT_LABEL[l.id]}
          </button>
        ))}
      </div>
      {faceLayout && (
        <div className="field">
          <span className="label">Framing</span>
          <div className="seg">
            {(["auto", "center", "manual"] as const).map((m) => (
              <button key={m} className={edit.reframe === m ? "on" : ""} onClick={() => setEdit({ reframe: m, crop_x: m === "manual" ? edit.crop_x ?? 0.5 : edit.crop_x })}>
                {m === "auto" ? "Auto face" : m === "center" ? "Center" : "Manual"}
              </button>
            ))}
          </div>
          {edit.reframe === "manual" && (
            <input type="range" min={0} max={1} step={0.005} value={edit.crop_x ?? 0.5} onChange={(e) => setEdit({ crop_x: Number(e.target.value) })} style={{ marginTop: 8 }} />
          )}
          {edit.reframe === "auto" && !edit.track && <span className="hint">Face tracking is computed during export.</span>}
        </div>
      )}

      <div className="section-title">Captions</div>
      <label className="toggle">
        <input type="checkbox" checked={edit.captions} onChange={(e) => setEdit({ captions: e.target.checked })} />
        Animated captions
      </label>
      {edit.captions && (
        <>
          <div className="presets">
            {catalog.caption_presets.map((p) => (
              <button
                key={p.id}
                className={`preset ${p.id === edit.caption_preset ? "active" : ""}`}
                onClick={() => setEdit({ caption_preset: p.id })}
                style={{ fontFamily: `"${fontFamilyFor(p)}", sans-serif`, color: p.primary }}
                title={p.name}
              >
                {p.uppercase ? "HEY " : "Hey "}
                <span style={{ color: p.highlight }}>{p.uppercase ? "YOU" : "you"}</span>
              </button>
            ))}
          </div>
          <div className="row">
            <input type="color" value={edit.highlight_color ?? preset.highlight} onChange={(e) => setEdit({ highlight_color: e.target.value })} title="Highlight colour" />
            <input
              type="range"
              min={0.4}
              max={0.9}
              step={0.01}
              value={edit.caption_position}
              onChange={(e) => setEdit({ caption_position: Number(e.target.value) })}
              title="Caption height"
            />
          </div>
        </>
      )}
      <label className="toggle">
        <input type="checkbox" checked={edit.title_overlay} onChange={(e) => setEdit({ title_overlay: e.target.checked })} />
        Title hook on screen for the first 3 seconds
      </label>

      <div className="section-title">Track a post</div>
      <form
        className="row"
        onSubmit={(e) => {
          e.preventDefault();
          if (!postUrl.trim()) return;
          posts.add.mutate(
            { clipId: clip.id, url: postUrl.trim() },
            {
              onSuccess: () => {
                setPostUrl("");
                toast("Tracking views — see the project's Posts tab");
              },
              onError: (err) => toast(errorText(err), true),
            },
          );
        }}
      >
        <input className="input" placeholder="https://www.tiktok.com/@you/video/…" value={postUrl} onChange={(e) => setPostUrl(e.target.value)} />
        <button className="btn icon" title="Track">
          <LinkIcon />
        </button>
      </form>

      <button
        className="btn danger ghost sm"
        style={{ justifySelf: "start" }}
        onClick={() => {
          if (!confirm("Delete this clip and its exports?")) return;
          remove.mutate(clip.id, { onSuccess: onDeleted });
        }}
      >
        <Trash /> Delete clip
      </button>
    </div>
  );
}

function ExportRow({ r }: { r: Render }) {
  return (
    <div className="export-row">
      <div className="mini" style={r.thumbnail_url ? { backgroundImage: `url(${r.thumbnail_url})` } : undefined} />
      <div style={{ minWidth: 0 }}>
        <div className="row" style={{ fontSize: 12.5 }}>
          <StatusBadge status={r.status} />
          <span className="muted">{r.layout ? LAYOUT_LABEL[r.layout] : ""}</span>
          {r.status === "done" && <span className="faint">{fmtBytes(r.size_bytes)}</span>}
        </div>
        {(r.status === "queued" || r.status === "running") && (
          <div style={{ marginTop: 6 }}>
            <ProgressBar value={r.progress} />
          </div>
        )}
        {r.status === "failed" && <div className="error-text" style={{ fontSize: 12 }}>{r.error}</div>}
      </div>
      {r.download_url && (
        <a className="btn sm icon primary" href={r.download_url} download title="Download MP4">
          <Download />
        </a>
      )}
    </div>
  );
}

function TrimControls({ clip, onChange }: { clip: Clip; onChange: (start: number, end: number) => void }) {
  const store = usePlayerStore();
  const now = () => store.state.timeMs;
  const row = (label: string, value: number, set: (v: number) => void) => (
    <div className="nudge">
      <div>
        <span className="label" style={{ marginBottom: 2 }}>{label}</span>
        <span className="mono">{fmtTime(value, true)}</span>
      </div>
      <div className="btns">
        <button className="btn sm" onClick={() => set(value - 1000)}>-1s</button>
        <button className="btn sm" onClick={() => set(value - 100)}>-.1</button>
        <button className="btn sm" onClick={() => set(value + 100)}>+.1</button>
        <button className="btn sm" onClick={() => set(value + 1000)}>+1s</button>
        <button className="btn sm" title="Set to playhead" onClick={() => set(now())}>⌖</button>
      </div>
    </div>
  );
  return (
    <div className="stack" style={{ gap: 8 }}>
      {row("Start", clip.start_ms, (v) => onChange(v, clip.end_ms))}
      {row("End", clip.end_ms, (v) => onChange(clip.start_ms, v))}
      <div className="row muted" style={{ fontSize: 12.5 }}>
        Length <b className="mono" style={{ color: "var(--text)" }}>{fmtDuration(clip.end_ms - clip.start_ms)}</b>
        <span className="spacer" />
        <button className="btn sm ghost" onClick={() => store.playRange([clip.start_ms, clip.end_ms])}>
          ▶ Play clip
        </button>
      </div>
    </div>
  );
}

// --- live preview ------------------------------------------------------------------------------------

function VerticalPreview({
  video,
  clip,
  layout,
  preset,
  edit,
  catalog,
}: {
  video: Video;
  clip: Clip;
  layout: LayoutInfo;
  preset: CaptionPreset;
  edit: ClipEdit;
  catalog: Catalog;
}) {
  const store = usePlayerStore();
  const canvas = useRef<HTMLCanvasElement>(null);
  const captions = useCaptions(video.id, preset.id, edit.captions);
  const W = 540;
  const H = Math.round((W * layout.height) / layout.width);

  useEffect(() => {
    const c = canvas.current;
    if (!c) return;
    c.width = W;
    c.height = H;
    const ctx = c.getContext("2d")!;
    const draw = () => {
      const v = store.video;
      if (!v || v.readyState < 2 || !v.videoWidth) return;
      const vw = v.videoWidth;
      const vh = v.videoHeight;
      const t = store.state.timeMs;
      ctx.fillStyle = "black";
      ctx.fillRect(0, 0, W, H);
      if (layout.id === "vertical_fill" || layout.id === "square_fill") {
        const r = cropRect(layout, vw, vh, cropCenterAt(clip, clamp(t, clip.start_ms, clip.end_ms)));
        ctx.drawImage(v, r.sx, r.sy, r.sw, r.sh, 0, 0, W, H);
      } else if (layout.id === "vertical_fit") {
        const cover = Math.max(W / vw, H / vh);
        ctx.filter = "blur(10px) brightness(0.85) saturate(1.15)";
        ctx.drawImage(v, (W - vw * cover) / 2, (H - vh * cover) / 2, vw * cover, vh * cover);
        ctx.filter = "none";
        const fh = (W * vh) / vw;
        ctx.drawImage(v, 0, (H - fh) / 2, W, fh);
      } else {
        const fit = Math.min(W / vw, H / vh);
        ctx.drawImage(v, (W - vw * fit) / 2, (H - vh * fit) / 2, vw * fit, vh * fit);
      }
    };
    draw();
    const v = store.video;
    v?.addEventListener("loadeddata", draw);
    v?.addEventListener("seeked", draw);
    const unsub = store.subscribe(draw);
    return () => {
      unsub();
      v?.removeEventListener("loadeddata", draw);
      v?.removeEventListener("seeked", draw);
    };
  }, [store, clip, layout, H]);

  const cls = layout.id === "square_fill" ? "square" : layout.id === "landscape" ? "landscape" : "";
  return (
    <div className={`preview-916 ${cls}`}>
      <canvas ref={canvas} />
      <CaptionOverlay
        clip={clip}
        layout={layout}
        preset={preset}
        edit={edit}
        catalog={catalog}
        chunks={edit.captions ? captions.data?.chunks : undefined}
      />
    </div>
  );
}

function CaptionOverlay({
  clip,
  layout,
  preset,
  edit,
  catalog,
  chunks,
}: {
  clip: Clip;
  layout: LayoutInfo;
  preset: CaptionPreset;
  edit: ClipEdit;
  catalog: Catalog;
  chunks: CaptionChunk[] | undefined;
}) {
  const store = usePlayerStore();
  // Re-render only when the visible chunk / active word changes (a few times per second).
  const [pos, setPos] = useState<{ chunk: number; word: number; rel: number }>({ chunk: -1, word: -1, rel: -1 });
  const timeBucket = usePlayer((s) => Math.floor((s.timeMs - clip.start_ms) / 250));

  useEffect(() => {
    const update = () => {
      const t = store.state.timeMs;
      const inClip = t >= clip.start_ms && t < clip.end_ms;
      let chunk = -1;
      let word = -1;
      if (inClip && chunks?.length) {
        const i = lastStartingBefore(chunks, t, (c) => c[0]);
        if (i >= 0 && t < chunks[i][1]) {
          chunk = i;
          word = lastStartingBefore(chunks[i][2], t, (w) => w[0]);
        }
      }
      const rel = inClip ? t - clip.start_ms : -1;
      setPos((p) => (p.chunk === chunk && p.word === word && (p.rel < 0) === (rel < 0) ? p : { chunk, word, rel }));
    };
    update();
    return store.subscribe(update);
  }, [store, chunks, clip.start_ms, clip.end_ms]);

  const outW = layout.width;
  const scale = (Math.min(layout.width, layout.height) / 1080) * (layout.width > layout.height ? 0.8 : 1);
  // Lengths are output pixels (as in the ASS script) expressed as a share of the preview width.
  const unit = (px: number) => `${((px * scale) / outW) * 100}cqw`;
  // libass font sizes are "cell heights"; convert to CSS em with the font's measured ratio.
  const emOf = (size: number, file: string) => unit(size * (catalog.font_em_per_size[file] ?? 0.6));
  const title = catalog.overlays.title;
  const hl = edit.highlight_color ?? preset.highlight;
  const base: CSSProperties = useMemo(
    () => ({
      top: `${edit.caption_position * 100}%`,
      fontFamily: `"${fontFamilyFor(preset)}", sans-serif`,
      fontSize: emOf(preset.font_size, preset.font_file),
      color: preset.primary,
      letterSpacing: preset.letter_spacing ? unit(preset.letter_spacing) : undefined,
      WebkitTextStroke: preset.box ? undefined : `${unit(preset.outline_width * 2)} ${preset.outline}`,
      paintOrder: "stroke fill",
      textShadow: preset.shadow ? `${unit(preset.shadow)} ${unit(preset.shadow)} 0 rgba(0,0,0,0.55)` : undefined,
    }),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [preset, edit.caption_position, outW, scale, catalog],
  );

  const chunk = pos.chunk >= 0 && chunks ? chunks[pos.chunk] : null;
  const showTitle = edit.title_overlay && timeBucket >= 0 && timeBucket < 14 && pos.rel >= 0;
  return (
    <div className="cap-layer">
      {showTitle && (
        <div
          className="cap-title"
          style={{ fontSize: emOf(title.size, title.font_file), WebkitTextStroke: `${unit((title.outline ?? 5) * 2)} black` }}
        >
          {clip.title}
        </div>
      )}
      {chunk && (
        <div className="cap" style={base}>
          <span
            style={
              preset.box
                ? { background: `${preset.box_color}${Math.round(preset.box_opacity * 255).toString(16).padStart(2, "0")}`, padding: "0.1em 0.3em", boxDecorationBreak: "clone", WebkitBoxDecorationBreak: "clone", borderRadius: "0.12em" }
                : undefined
            }
          >
            {chunk[2].map((w, i) => {
              const active = preset.mode === "highlight" && i === pos.word;
              const sung = preset.mode === "karaoke" && i <= pos.word;
              return (
                <span key={i}>
                  {/* font-size (not transform) so the pop reflows the line exactly like libass \fscx112 */}
                  <span style={active || sung ? { color: hl, fontSize: active && preset.pop ? "1.12em" : undefined } : undefined}>
                    {w[2]}
                  </span>
                  {i < chunk[2].length - 1 ? " " : ""}
                </span>
              );
            })}
          </span>
        </div>
      )}
    </div>
  );
}
