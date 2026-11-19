import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState, type DragEvent, type FormEvent } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router";
import { uploadFile } from "../api/client";
import {
  keys,
  useCatalog,
  useDeleteProject,
  useImportLinks,
  useLibrary,
  usePostMutations,
  usePosts,
  useProject,
  useUpdateProject,
  useVideoAction,
  useVideos,
} from "../api/queries";
import type { Clip, Project, ProjectSettings, Video } from "../api/types";
import { TopBar } from "../components/AppShell";
import { Copy, Download, Link as LinkIcon, Refresh, Scissors, Trash, Upload } from "../components/icons";
import { Empty, errorText, ProgressBar, Score, Spinner, StatusBadge, useToast } from "../components/ui";
import { fmtCompact, fmtDuration, fmtMoney, fmtTime, plural, timeAgo } from "../lib/format";

const TABS = [
  ["videos", "Videos"],
  ["clips", "Clips"],
  ["posts", "Posts & revenue"],
  ["settings", "Settings"],
] as const;

export default function ProjectPage() {
  const { projectId = "" } = useParams();
  const [params, setParams] = useSearchParams();
  const tab = params.get("tab") ?? "videos";
  const project = useProject(projectId);

  return (
    <div className="shell">
      <TopBar>
        <Link to="/">Clients</Link>
        <span className="sep">/</span>
        <span className="ellipsis" style={{ color: "var(--text)" }}>
          {project.data?.name ?? "…"}
        </span>
      </TopBar>
      <main className="page">
        <div className="container">
          {!project.data ? (
            project.error ? <Empty title="Project not found" /> : <Spinner />
          ) : (
            <>
              <div className="page-head">
                <div className="grow">
                  <h1>{project.data.name}</h1>
                  <div className="muted">
                    {plural(project.data.stats.videos, "video")} · {plural(project.data.stats.clips_saved, "saved clip")} ·{" "}
                    {plural(project.data.stats.views, "tracked view")}
                  </div>
                </div>
              </div>
              <nav className="tabs">
                {TABS.map(([id, label]) => (
                  <button key={id} className={`tab ${tab === id ? "active" : ""}`} onClick={() => setParams({ tab: id })}>
                    {label}
                  </button>
                ))}
              </nav>
              {tab === "videos" && <VideosTab project={project.data} />}
              {tab === "clips" && <ClipsTab project={project.data} />}
              {tab === "posts" && <PostsTab project={project.data} />}
              {tab === "settings" && <SettingsTab project={project.data} />}
            </>
          )}
        </div>
      </main>
    </div>
  );
}

// --- videos --------------------------------------------------------------------------------------

function VideosTab({ project }: { project: Project }) {
  const videos = useVideos(project.id);
  const qc = useQueryClient();
  // Header/dashboard stats change whenever a video lands or finishes: refresh them then.
  const signature = videos.data?.map((v) => `${v.id}:${v.status}`).join(",") ?? "";
  useEffect(() => {
    if (!signature) return;
    qc.invalidateQueries({ queryKey: keys.project(project.id) });
    qc.invalidateQueries({ queryKey: keys.projects });
  }, [signature, project.id, qc]);
  return (
    <>
      <ImportPanel projectId={project.id} />
      {videos.isPending ? (
        <Spinner />
      ) : !videos.data?.length ? (
        <div className="card">
          <Empty icon={<LinkIcon width={26} height={26} />} title="No videos yet">
            Paste a YouTube, Twitch, Kick, TikTok, X or direct video link above — or drop a file.
          </Empty>
        </div>
      ) : (
        <div className="video-list">
          {videos.data.map((v) => (
            <VideoRow key={v.id} video={v} projectId={project.id} />
          ))}
        </div>
      )}
    </>
  );
}

function ImportPanel({ projectId }: { projectId: string }) {
  const [text, setText] = useState("");
  const [range, setRange] = useState({ open: false, start: "", end: "" });
  const [uploads, setUploads] = useState<{ name: string; progress: number }[]>([]);
  const [over, setOver] = useState(false);
  const fileInput = useRef<HTMLInputElement>(null);
  const importLinks = useImportLinks(projectId);
  const qc = useQueryClient();
  const toast = useToast();

  const parseClock = (s: string): number | undefined => {
    if (!s.trim()) return undefined;
    const parts = s.trim().split(":").map(Number);
    if (parts.some(Number.isNaN)) return undefined;
    return parts.reduce((acc, p) => acc * 60 + p, 0);
  };

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    try {
      const vids = await importLinks.mutateAsync({
        text,
        section_start_s: range.open ? parseClock(range.start) : undefined,
        section_end_s: range.open ? parseClock(range.end) : undefined,
      });
      setText("");
      toast(`Importing ${vids.length} video${vids.length === 1 ? "" : "s"}…`);
    } catch (err) {
      toast(errorText(err), true);
    }
  };

  const upload = async (files: FileList | File[]) => {
    for (const file of Array.from(files)) {
      setUploads((u) => [...u, { name: file.name, progress: 0 }]);
      try {
        await uploadFile(`/api/projects/${projectId}/videos/upload?filename=${encodeURIComponent(file.name)}`, file, (p) =>
          setUploads((u) => u.map((x) => (x.name === file.name ? { ...x, progress: p } : x))),
        );
        qc.invalidateQueries({ queryKey: keys.videos(projectId) });
      } catch (err) {
        toast(`${file.name}: ${errorText(err)}`, true);
      } finally {
        setUploads((u) => u.filter((x) => x.name !== file.name));
      }
    }
  };

  const onDrop = (e: DragEvent) => {
    e.preventDefault();
    setOver(false);
    if (e.dataTransfer.files.length) void upload(e.dataTransfer.files);
  };

  return (
    <form className="card import-box" onSubmit={submit}>
      <label className="field">
        <span className="label">Video links — one per line, up to 50 at once</span>
        <textarea
          className="textarea"
          value={text}
          onChange={(e) => setText(e.target.value)}
          placeholder={"https://www.youtube.com/watch?v=…\nhttps://www.twitch.tv/videos/…\nhttps://kick.com/…/videos/…"}
          onKeyDown={(e) => {
            if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) void submit(e as unknown as FormEvent);
          }}
        />
      </label>
      {range.open && (
        <div className="row wrap">
          <label className="field" style={{ width: 160 }}>
            <span className="label">From (h:mm:ss)</span>
            <input className="input mono" value={range.start} onChange={(e) => setRange({ ...range, start: e.target.value })} placeholder="1:00:00" />
          </label>
          <label className="field" style={{ width: 160 }}>
            <span className="label">To (h:mm:ss)</span>
            <input className="input mono" value={range.end} onChange={(e) => setRange({ ...range, end: e.target.value })} placeholder="2:30:00" />
          </label>
          <span className="hint" style={{ alignSelf: "flex-end", marginBottom: 8 }}>
            Downloads only this part of a long stream.
          </span>
        </div>
      )}
      <div className="row wrap">
        <button className="btn primary" disabled={!text.trim() || importLinks.isPending}>
          <Scissors /> Import &amp; find clips
        </button>
        <button type="button" className="btn ghost sm" onClick={() => setRange({ ...range, open: !range.open })}>
          {range.open ? "Whole video" : "Only a time range…"}
        </button>
        <span className="spacer" />
        <div
          className={`drop ${over ? "over" : ""}`}
          onClick={() => fileInput.current?.click()}
          onDragOver={(e) => {
            e.preventDefault();
            setOver(true);
          }}
          onDragLeave={() => setOver(false)}
          onDrop={onDrop}
          role="button"
          tabIndex={0}
        >
          <Upload /> Drop video files or click to upload
        </div>
        <input
          ref={fileInput}
          type="file"
          accept="video/*,.mkv,.ts,.mts"
          multiple
          hidden
          onChange={(e) => e.target.files && void upload(e.target.files)}
        />
      </div>
      {uploads.map((u) => (
        <div key={u.name} className="stack" style={{ gap: 4 }}>
          <div className="row" style={{ fontSize: 13 }}>
            <span className="grow ellipsis">Uploading {u.name}</span>
            <span className="mono muted">{Math.round(u.progress * 100)}%</span>
          </div>
          <ProgressBar value={u.progress} />
        </div>
      ))}
    </form>
  );
}

function VideoRow({ video, projectId }: { video: Video; projectId: string }) {
  const action = useVideoAction(projectId);
  const navigate = useNavigate();
  const ready = video.status === "ready";
  const working = !ready && video.status !== "failed";
  const remove = () => {
    if (confirm(`Delete "${video.title}" and all its clips and exports?`)) action.mutate({ id: video.id, action: "delete" });
  };
  return (
    <div className="card video-row" style={{ cursor: ready ? "pointer" : "default" }} onClick={() => ready && navigate(`/videos/${video.id}`)}>
      <div className="thumb" style={video.poster_url ? { backgroundImage: `url(${video.poster_url})` } : undefined}>
        {video.duration_ms != null && <span className="dur">{fmtTime(video.duration_ms)}</span>}
      </div>
      <div className="grow">
        <h4 className="ellipsis" title={video.title}>
          {video.title}
        </h4>
        <div className="row wrap muted" style={{ fontSize: 12.5 }}>
          <StatusBadge status={video.status} />
          {video.platform && <span className="badge">{video.platform}</span>}
          {video.uploader && <span className="ellipsis">{video.uploader}</span>}
          {ready && <span>{video.clip_count ?? 0} clips</span>}
          {video.transcript_source === "captions" && <span title="Used the platform's captions — no transcription needed">⚡ fast transcript</span>}
          {video.has_heatmap && <span title="YouTube 'Most replayed' data used for ranking">🔥 audience heatmap</span>}
          <span className="faint">{timeAgo(video.created_at)}</span>
        </div>
        {working && (
          <div style={{ marginTop: 8, maxWidth: 360 }}>
            <ProgressBar value={video.progress} />
          </div>
        )}
        {video.status === "failed" && video.error && (
          <div className="error-text" style={{ marginTop: 6 }}>
            {video.error}
          </div>
        )}
      </div>
      <div className="row" onClick={(e) => e.stopPropagation()}>
        {video.status === "failed" && (
          <button className="btn sm" onClick={() => action.mutate({ id: video.id, action: "retry" })}>
            <Refresh /> Retry
          </button>
        )}
        {ready && (
          <Link className="btn sm primary" to={`/videos/${video.id}`}>
            <Scissors /> Open editor
          </Link>
        )}
        <button className="btn sm icon ghost" title="Delete" onClick={remove}>
          <Trash />
        </button>
      </div>
    </div>
  );
}

// --- clips library ----------------------------------------------------------------------------------

function ClipsTab({ project }: { project: Project }) {
  const [status, setStatus] = useState("saved");
  const clips = useLibrary(project.id, status);
  return (
    <>
      <div className="row" style={{ marginBottom: 14 }}>
        <div className="seg" style={{ width: 280 }}>
          {["saved", "suggested", "all"].map((s) => (
            <button key={s} className={status === s ? "on" : ""} onClick={() => setStatus(s)}>
              {s[0].toUpperCase() + s.slice(1)}
            </button>
          ))}
        </div>
      </div>
      {clips.isPending ? (
        <Spinner />
      ) : !clips.data?.length ? (
        <div className="card">
          <Empty title="No clips here yet">Open a video in the editor, review the AI moments, and export the ones you like.</Empty>
        </div>
      ) : (
        <div className="library">
          {clips.data.map((c) => (
            <LibraryCard key={c.id} clip={c} projectId={project.id} />
          ))}
        </div>
      )}
    </>
  );
}

function captionText(clip: Clip) {
  return [clip.title, clip.hashtags.map((h) => `#${h}`).join(" ")].filter(Boolean).join("\n\n");
}

function LibraryCard({ clip, projectId }: { clip: Clip; projectId: string }) {
  const toast = useToast();
  const posts = usePostMutations(projectId);
  const r = clip.latest_render;
  const addPost = () => {
    const url = prompt("Paste the URL where this clip was posted (TikTok, Shorts, Reels, X…)");
    if (url) posts.add.mutate({ clipId: clip.id, url }, { onSuccess: () => toast("Tracking views for this post"), onError: (e) => toast(errorText(e), true) });
  };
  return (
    <div className="card lib-card">
      <Link to={`/videos/${clip.video_id}?clip=${clip.id}`} className="vthumb" style={r?.thumbnail_url ? { backgroundImage: `url(${r.thumbnail_url})` } : undefined}>
        {!r?.thumbnail_url && <span>{r ? <StatusBadge status={r.status} /> : "Not exported"}</span>}
      </Link>
      <div className="body">
        <div className="row">
          <Score value={clip.score} />
          <span className="mono muted" style={{ fontSize: 12 }}>
            {fmtDuration(clip.end_ms - clip.start_ms)}
          </span>
        </div>
        <div style={{ fontWeight: 650, fontSize: 13.5 }} className="ellipsis" title={clip.title}>
          {clip.title}
        </div>
        <div className="faint ellipsis" style={{ fontSize: 12 }}>
          {clip.video_title}
        </div>
        <div className="row">
          {r?.download_url && (
            <a className="btn sm primary" href={r.download_url} download>
              <Download /> MP4
            </a>
          )}
          <button
            className="btn sm icon"
            title="Copy title + hashtags"
            onClick={() => navigator.clipboard.writeText(captionText(clip)).then(() => toast("Caption copied"))}
          >
            <Copy />
          </button>
          <button className="btn sm icon" title="Track a posted link" onClick={addPost}>
            <LinkIcon />
          </button>
        </div>
      </div>
    </div>
  );
}

// --- posts ----------------------------------------------------------------------------------------

function PostsTab({ project }: { project: Project }) {
  const posts = usePosts(project.id);
  const m = usePostMutations(project.id);
  const data = posts.data;
  return (
    <>
      <div className="kpis">
        <div className="card kpi">
          <div className="v">{fmtCompact(data?.total_views ?? 0)}</div>
          <div className="k">Total views</div>
        </div>
        <div className="card kpi">
          <div className="v">{data?.posts.length ?? 0}</div>
          <div className="k">Posts tracked</div>
        </div>
        <div className="card kpi">
          <div className="v">{fmtMoney(data?.est_revenue_usd)}</div>
          <div className="k">{project.settings.cpm_usd != null ? `Est. revenue @ $${project.settings.cpm_usd}/1K views` : "Set a CPM in Settings to estimate revenue"}</div>
        </div>
      </div>
      {!data?.posts.length ? (
        <div className="card">
          <Empty title="No posts tracked">After you publish a clip, add its link from the Clips tab or the editor. Views refresh every 6 hours.</Empty>
        </div>
      ) : (
        <div className="card" style={{ overflowX: "auto" }}>
          <table className="table">
            <thead>
              <tr>
                <th>Post</th>
                <th>Clip</th>
                <th>Views</th>
                <th>Likes</th>
                <th>Updated</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {data.posts.map((p) => (
                <tr key={p.id}>
                  <td>
                    <a href={p.url} target="_blank" rel="noreferrer" className="row">
                      <span className="badge">{p.platform}</span>
                      <span className="ellipsis" style={{ maxWidth: 220 }}>
                        {p.account ?? p.url}
                      </span>
                    </a>
                  </td>
                  <td className="ellipsis" style={{ maxWidth: 240 }}>
                    {p.clip_title}
                  </td>
                  <td className="mono">{fmtCompact(p.views)}</td>
                  <td className="mono">{fmtCompact(p.likes)}</td>
                  <td className="muted" title={p.stats_error ?? undefined}>
                    {p.stats_updated_at ? timeAgo(p.stats_updated_at) : "pending"}
                    {p.stats_error && " ⚠"}
                  </td>
                  <td>
                    <div className="row">
                      <button className="btn sm icon ghost" title="Refresh stats" onClick={() => m.refreshStats.mutate(p.id)}>
                        <Refresh />
                      </button>
                      <button className="btn sm icon ghost" title="Stop tracking" onClick={() => m.remove.mutate(p.id)}>
                        <Trash />
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}

// --- settings --------------------------------------------------------------------------------------

function SettingsTab({ project }: { project: Project }) {
  const catalog = useCatalog();
  const update = useUpdateProject(project.id);
  const del = useDeleteProject();
  const navigate = useNavigate();
  const toast = useToast();
  const [name, setName] = useState(project.name);
  const [s, setS] = useState<ProjectSettings>(project.settings);
  useEffect(() => setS(project.settings), [project.settings]);
  const set = <K extends keyof ProjectSettings>(k: K, v: ProjectSettings[K]) => setS((prev) => ({ ...prev, [k]: v }));

  const save = async (e: FormEvent) => {
    e.preventDefault();
    try {
      await update.mutateAsync({ name, settings: s });
      toast("Settings saved");
    } catch (err) {
      toast(errorText(err), true);
    }
  };

  return (
    <form className="stack" style={{ gap: 18 }} onSubmit={save}>
      <div className="card pad stack">
        <div className="section-title">Project</div>
        <label className="field" style={{ maxWidth: 420 }}>
          <span className="label">Name</span>
          <input className="input" value={name} onChange={(e) => setName(e.target.value)} />
        </label>
      </div>
      <div className="card pad stack">
        <div className="section-title">Brand kit — defaults for new clips</div>
        <div className="settings-grid">
          <label className="field">
            <span className="label">Caption style</span>
            <select className="select" value={s.caption_preset} onChange={(e) => set("caption_preset", e.target.value)}>
              {catalog.data?.caption_presets.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.name}
                </option>
              ))}
            </select>
          </label>
          <label className="field">
            <span className="label">Layout</span>
            <select className="select" value={s.layout} onChange={(e) => set("layout", e.target.value)}>
              {catalog.data?.layouts.map((l) => (
                <option key={l.id} value={l.id}>
                  {l.name}
                </option>
              ))}
            </select>
          </label>
          <label className="field">
            <span className="label">Highlight colour</span>
            <div className="row">
              <input type="color" value={s.highlight_color ?? "#ffe600"} onChange={(e) => set("highlight_color", e.target.value)} />
              <button type="button" className="btn sm ghost" onClick={() => set("highlight_color", null)}>
                Use style default
              </button>
            </div>
          </label>
          <label className="field">
            <span className="label">Watermark / handle</span>
            <input className="input" maxLength={40} value={s.watermark ?? ""} onChange={(e) => set("watermark", e.target.value || null)} placeholder="@creator" />
          </label>
          <label className="field">
            <span className="label">Caption height ({Math.round(s.caption_position * 100)}% down)</span>
            <input type="range" min={0.4} max={0.9} step={0.01} value={s.caption_position} onChange={(e) => set("caption_position", Number(e.target.value))} />
          </label>
          <label className="toggle" style={{ alignSelf: "end", marginBottom: 6 }}>
            <input type="checkbox" checked={s.title_overlay} onChange={(e) => set("title_overlay", e.target.checked)} />
            Show title hook for the first seconds
          </label>
        </div>
      </div>
      <div className="card pad stack">
        <div className="section-title">Moment detection</div>
        <div className="settings-grid">
          <NumberField label="Min clip length (s)" value={s.min_clip_s} min={5} max={170} onChange={(v) => set("min_clip_s", v)} />
          <NumberField label="Target length (s)" value={s.target_clip_s} min={5} max={180} onChange={(v) => set("target_clip_s", v)} />
          <NumberField label="Max clip length (s)" value={s.max_clip_s} min={10} max={180} onChange={(v) => set("max_clip_s", v)} />
          <label className="field">
            <span className="label">Clips per video</span>
            <input className="input" type="number" min={1} max={50} placeholder="Auto (1 per ~4 min)" value={s.max_clips ?? ""} onChange={(e) => set("max_clips", e.target.value ? Number(e.target.value) : null)} />
          </label>
          <label className="field">
            <span className="label">Language</span>
            <input className="input" maxLength={8} placeholder="Auto-detect" value={s.language ?? ""} onChange={(e) => set("language", e.target.value || null)} />
          </label>
          <label className="field">
            <span className="label">Transcript</span>
            <select className="select" value={s.transcript_source} onChange={(e) => set("transcript_source", e.target.value as "auto" | "whisper")}>
              <option value="auto">Platform captions when available (fastest)</option>
              <option value="whisper">Always transcribe locally (most accurate)</option>
            </select>
          </label>
          <label className="toggle" style={{ alignSelf: "end", marginBottom: 6 }}>
            <input type="checkbox" checked={s.ai_rerank} onChange={(e) => set("ai_rerank", e.target.checked)} />
            AI titles &amp; re-ranking (needs API key)
          </label>
        </div>
      </div>
      <div className="card pad stack">
        <div className="section-title">Business</div>
        <label className="field" style={{ maxWidth: 260 }}>
          <span className="label">Payout per 1,000 views (USD)</span>
          <input className="input" type="number" min={0} step={0.01} placeholder="e.g. 1.50" value={s.cpm_usd ?? ""} onChange={(e) => set("cpm_usd", e.target.value ? Number(e.target.value) : null)} />
          <span className="hint">Used to estimate campaign revenue from tracked views.</span>
        </label>
      </div>
      <div className="row">
        <button className="btn primary" disabled={update.isPending}>
          Save settings
        </button>
        <span className="spacer" />
        <button
          type="button"
          className="btn danger"
          onClick={async () => {
            if (!confirm(`Delete "${project.name}" with all its videos, clips and exports? This can't be undone.`)) return;
            await del.mutateAsync(project.id);
            navigate("/");
          }}
        >
          <Trash /> Delete project
        </button>
      </div>
    </form>
  );
}

function NumberField(props: { label: string; value: number; min: number; max: number; onChange: (v: number) => void }) {
  return (
    <label className="field">
      <span className="label">{props.label}</span>
      <input
        className="input"
        type="number"
        min={props.min}
        max={props.max}
        value={props.value}
        onChange={(e) => props.onChange(Number(e.target.value))}
      />
    </label>
  );
}
