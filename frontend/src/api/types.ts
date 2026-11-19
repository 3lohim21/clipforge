// Mirrors backend/clipforge/api/schemas.py — the API contract.

export type Role = "owner" | "admin" | "editor";

export interface User {
  id: string;
  email: string;
  name: string;
  role: Role;
  created_at: string;
  last_login_at: string | null;
}

export interface Me {
  user: User;
  workspace: { id: string; name: string };
}

export interface ProjectSettings {
  caption_preset: string;
  layout: string;
  highlight_color: string | null;
  caption_position: number;
  title_overlay: boolean;
  watermark: string | null;
  min_clip_s: number;
  max_clip_s: number;
  target_clip_s: number;
  max_clips: number | null;
  language: string | null;
  transcript_source: "auto" | "whisper";
  ai_rerank: boolean;
  cpm_usd: number | null;
}

export interface ProjectStats {
  videos: number;
  clips_saved: number;
  clips_suggested: number;
  renders: number;
  posts: number;
  views: number;
  est_revenue_usd: number | null;
}

export interface Project {
  id: string;
  name: string;
  settings: ProjectSettings;
  created_at: string;
  updated_at: string;
  stats: ProjectStats;
}

export type VideoStatus =
  | "queued"
  | "downloading"
  | "processing"
  | "transcribing"
  | "analyzing"
  | "ready"
  | "failed";

export interface Video {
  id: string;
  project_id: string;
  source_type: "url" | "upload";
  source_url: string | null;
  title: string;
  platform: string | null;
  uploader: string | null;
  status: VideoStatus;
  progress: number;
  error: string | null;
  duration_ms: number | null;
  width: number | null;
  height: number | null;
  fps: number | null;
  has_audio: boolean | null;
  language: string | null;
  transcript_source: "whisper" | "captions" | null;
  created_at: string;
  updated_at: string;
  poster_url: string | null;
  playback_url: string | null;
  has_transcript: boolean;
  has_heatmap: boolean;
  analysis: { clips: number; words: number; llm: { used: boolean }; heatmap: boolean } | null;
  clip_count: number | null;
}

export type Layout = "vertical_fill" | "vertical_fit" | "square_fill" | "landscape";

export interface ClipEdit {
  layout: Layout;
  captions: boolean;
  caption_preset: string;
  caption_position: number;
  highlight_color: string | null;
  title_overlay: boolean;
  reframe: "auto" | "center" | "manual";
  crop_x: number | null;
  // computed server-side; read-only
  track?: [number, number][];
  track_range?: [number, number];
}

export type RenderStatus = "queued" | "running" | "done" | "failed";

export interface Render {
  id: string;
  clip_id: string;
  status: RenderStatus;
  progress: number;
  error: string | null;
  width: number | null;
  height: number | null;
  duration_ms: number | null;
  size_bytes: number | null;
  created_at: string;
  finished_at: string | null;
  layout: Layout | null;
  file_url: string | null;
  download_url: string | null;
  thumbnail_url: string | null;
  clip_title: string | null;
}

export type ClipStatus = "suggested" | "saved" | "discarded";

export interface Clip {
  id: string;
  video_id: string;
  project_id: string;
  origin: "ai" | "manual";
  status: ClipStatus;
  start_ms: number;
  end_ms: number;
  title: string;
  hook: string | null;
  reason: string | null;
  hashtags: string[];
  score: number | null;
  score_breakdown: Record<string, number>;
  edit: Partial<ClipEdit>;
  created_at: string;
  updated_at: string;
  latest_render: Render | null;
  video_title: string | null;
}

export interface Post {
  id: string;
  clip_id: string;
  platform: string;
  url: string;
  account: string | null;
  views: number | null;
  likes: number | null;
  comments: number | null;
  stats_error: string | null;
  stats_updated_at: string | null;
  created_at: string;
  clip_title: string | null;
}

export interface PostsSummary {
  posts: Post[];
  total_views: number;
  est_revenue_usd: number | null;
}

export interface CaptionPreset {
  id: string;
  name: string;
  font_family: string;
  font_file: string;
  font_size: number;
  uppercase: boolean;
  strip_punctuation: boolean;
  primary: string;
  highlight: string;
  outline: string;
  outline_width: number;
  shadow: number;
  max_words: number;
  max_chars: number;
  mode: "highlight" | "karaoke" | "plain";
  pop: boolean;
  box: boolean;
  box_color: string;
  box_opacity: number;
  letter_spacing: number;
}

export interface LayoutInfo {
  id: Layout;
  name: string;
  width: number;
  height: number;
  description: string;
}

export interface OverlaySpec {
  font_family: string;
  font_file: string;
  size: number;
  outline?: number;
}

export interface Catalog {
  /** libass size -> CSS em conversion per font file (see rendering/presets.py em_per_size). */
  font_em_per_size: Record<string, number>;
  overlays: { title: OverlaySpec; watermark: OverlaySpec };
  caption_presets: CaptionPreset[];
  layouts: LayoutInfo[];
  defaults: { caption_preset: string; layout: Layout; caption_position: number };
}

/** [start_ms, end_ms, text] */
export type TranscriptWord = [number, number, string];

export interface Transcript {
  version: number;
  source: "whisper" | "captions";
  language: string | null;
  words: TranscriptWord[];
}

export interface Timeline {
  version: number;
  duration_ms: number;
  frame_ms: number;
  energy_u8: string | null;
  heatmap: [number, number, number][] | null;
  chapters: { start_ms: number; end_ms: number; title: string }[];
}

/** [start_ms, end_ms, words[]] */
export type CaptionChunk = [number, number, TranscriptWord[]];
