import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { del, get, patch, post } from "./client";
import type {
  CaptionChunk,
  Catalog,
  Clip,
  ClipEdit,
  Me,
  PostsSummary,
  Project,
  ProjectSettings,
  Render,
  Timeline,
  Transcript,
  User,
  Video,
} from "./types";

const PROCESSING = new Set(["queued", "downloading", "processing", "transcribing", "analyzing"]);
const RENDERING = new Set(["queued", "running"]);

export const keys = {
  me: ["me"] as const,
  catalog: ["catalog"] as const,
  projects: ["projects"] as const,
  project: (id: string) => ["project", id] as const,
  videos: (projectId: string) => ["videos", projectId] as const,
  video: (id: string) => ["video", id] as const,
  clips: (videoId: string) => ["clips", videoId] as const,
  library: (projectId: string, status: string) => ["library", projectId, status] as const,
  renders: (projectId: string) => ["renders", projectId] as const,
  posts: (projectId: string) => ["posts", projectId] as const,
  users: ["users"] as const,
  clipRenders: (clipId: string) => ["clip-renders", clipId] as const,
};

// --- session -----------------------------------------------------------------------------
export const useMe = () => useQuery({ queryKey: keys.me, queryFn: () => get<Me>("/api/auth/me"), retry: false });
export const useCatalog = () =>
  useQuery({ queryKey: keys.catalog, queryFn: () => get<Catalog>("/api/presets"), staleTime: Infinity });

// --- projects ------------------------------------------------------------------------------
export const useProjects = () => useQuery({ queryKey: keys.projects, queryFn: () => get<Project[]>("/api/projects") });
export const useProject = (id: string) =>
  useQuery({ queryKey: keys.project(id), queryFn: () => get<Project>(`/api/projects/${id}`), enabled: !!id });

export function useCreateProject() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (name: string) => post<Project>("/api/projects", { name }),
    onSuccess: () => qc.invalidateQueries({ queryKey: keys.projects }),
  });
}

export function useUpdateProject(id: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: { name?: string; settings?: ProjectSettings }) => patch<Project>(`/api/projects/${id}`, body),
    onSuccess: (p) => {
      qc.setQueryData(keys.project(id), p);
      qc.invalidateQueries({ queryKey: keys.projects });
    },
  });
}

export function useDeleteProject() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => del(`/api/projects/${id}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: keys.projects }),
  });
}

// --- videos --------------------------------------------------------------------------------
// Poll only while something is processing; idle pages make zero background requests.
export const useVideos = (projectId: string) =>
  useQuery({
    queryKey: keys.videos(projectId),
    queryFn: () => get<Video[]>(`/api/projects/${projectId}/videos`),
    refetchInterval: (q) => (q.state.data?.some((v) => PROCESSING.has(v.status)) ? 2000 : false),
  });

export const useVideo = (id: string) =>
  useQuery({
    queryKey: keys.video(id),
    queryFn: () => get<Video>(`/api/videos/${id}`),
    refetchInterval: (q) => (q.state.data && PROCESSING.has(q.state.data.status) ? 1500 : false),
  });

export function useImportLinks(projectId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: { text: string; section_start_s?: number; section_end_s?: number }) =>
      post<Video[]>(`/api/projects/${projectId}/videos`, body),
    onSuccess: () => qc.invalidateQueries({ queryKey: keys.videos(projectId) }),
  });
}

export function useVideoAction(projectId?: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async ({ id, action }: { id: string; action: "retry" | "reanalyze" | "delete" }): Promise<Video | null> => {
      if (action === "delete") {
        await del(`/api/videos/${id}`);
        return null;
      }
      return post<Video>(`/api/videos/${id}/${action}`);
    },
    onSuccess: (_d, { id }) => {
      qc.invalidateQueries({ queryKey: keys.video(id) });
      qc.invalidateQueries({ queryKey: keys.clips(id) });
      if (projectId) qc.invalidateQueries({ queryKey: keys.videos(projectId) });
    },
  });
}

// Immutable once the video is processed (re-analysis doesn't change them) -> cache forever.
export const useTranscript = (video?: Video) =>
  useQuery({
    queryKey: ["transcript", video?.id, video?.transcript_source],
    queryFn: () => get<Transcript>(`/api/videos/${video!.id}/transcript`),
    enabled: !!video?.has_transcript,
    staleTime: Infinity,
  });

export const useTimeline = (video?: Video) =>
  useQuery({
    queryKey: ["timeline", video?.id],
    queryFn: () => get<Timeline>(`/api/videos/${video!.id}/timeline`),
    enabled: !!video && video.status === "ready",
    staleTime: Infinity,
  });

export const useCaptions = (videoId: string | undefined, preset: string, enabled: boolean) =>
  useQuery({
    queryKey: ["captions", videoId, preset],
    queryFn: () => get<{ preset: string; chunks: CaptionChunk[] }>(`/api/videos/${videoId}/captions?preset=${preset}`),
    enabled: !!videoId && enabled,
    staleTime: Infinity,
    placeholderData: keepPreviousData,
  });

// --- clips ---------------------------------------------------------------------------------
const rendering = (clips?: Clip[]) => clips?.some((c) => c.latest_render && RENDERING.has(c.latest_render.status));

export const useClips = (videoId: string, enabled = true) =>
  useQuery({
    queryKey: keys.clips(videoId),
    queryFn: () => get<Clip[]>(`/api/videos/${videoId}/clips`),
    enabled,
    refetchInterval: (q) => (rendering(q.state.data) ? 1500 : false),
  });

export const useLibrary = (projectId: string, status: string) =>
  useQuery({
    queryKey: keys.library(projectId, status),
    queryFn: () => get<Clip[]>(`/api/projects/${projectId}/clips?status=${status}`),
    refetchInterval: (q) => (rendering(q.state.data) ? 2000 : false),
  });

export type ClipPatch = Partial<Pick<Clip, "start_ms" | "end_ms" | "title" | "status" | "hashtags">> & {
  edit?: ClipEdit;
};

export function useClipMutations(videoId: string) {
  const qc = useQueryClient();
  const setClip = (clip: Clip) =>
    qc.setQueryData<Clip[]>(keys.clips(videoId), (old) => old?.map((c) => (c.id === clip.id ? clip : c)));

  const update = useMutation({
    mutationFn: ({ id, body }: { id: string; body: ClipPatch }) => patch<Clip>(`/api/clips/${id}`, body),
    // Optimistic: the timeline/list respond instantly; the server response reconciles.
    onMutate: async ({ id, body }) => {
      await qc.cancelQueries({ queryKey: keys.clips(videoId) });
      const prev = qc.getQueryData<Clip[]>(keys.clips(videoId));
      qc.setQueryData<Clip[]>(keys.clips(videoId), (old) =>
        old?.map((c) => (c.id === id ? { ...c, ...body, edit: { ...c.edit, ...(body.edit ?? {}) } } : c)),
      );
      return { prev };
    },
    onError: (_e, _v, ctx) => ctx?.prev && qc.setQueryData(keys.clips(videoId), ctx.prev),
    onSuccess: setClip,
  });

  const create = useMutation({
    mutationFn: (body: { start_ms: number; end_ms: number; title?: string }) =>
      post<Clip>(`/api/videos/${videoId}/clips`, body),
    onSuccess: (clip) => qc.setQueryData<Clip[]>(keys.clips(videoId), (old) => [...(old ?? []), clip]),
  });

  const remove = useMutation({
    mutationFn: (id: string) => del(`/api/clips/${id}`),
    onSuccess: (_d, id) => qc.setQueryData<Clip[]>(keys.clips(videoId), (old) => old?.filter((c) => c.id !== id)),
  });

  // Every export path (button, keyboard, library) refreshes both the clip list badge and
  // the per-clip exports list, which then polls until the render finishes.
  const render = useMutation({
    mutationFn: (id: string) => post<Render>(`/api/clips/${id}/render`, {}),
    onSuccess: (_r, id) => {
      qc.invalidateQueries({ queryKey: keys.clips(videoId) });
      qc.invalidateQueries({ queryKey: keys.clipRenders(id) });
    },
  });

  return { update, create, remove, render };
}

export const useClipRenders = (clipId: string | undefined) =>
  useQuery({
    queryKey: keys.clipRenders(clipId ?? ""),
    queryFn: () => get<Render[]>(`/api/clips/${clipId}/renders`),
    enabled: !!clipId,
    refetchInterval: (q) => (q.state.data?.some((r) => RENDERING.has(r.status)) ? 1500 : false),
  });

// --- posts ---------------------------------------------------------------------------------
export const usePosts = (projectId: string) =>
  useQuery({ queryKey: keys.posts(projectId), queryFn: () => get<PostsSummary>(`/api/projects/${projectId}/posts`) });

export function usePostMutations(projectId: string) {
  const qc = useQueryClient();
  const refresh = () => qc.invalidateQueries({ queryKey: keys.posts(projectId) });
  return {
    add: useMutation({
      mutationFn: ({ clipId, url }: { clipId: string; url: string }) => post(`/api/clips/${clipId}/posts`, { url }),
      onSuccess: refresh,
    }),
    remove: useMutation({ mutationFn: (id: string) => del(`/api/posts/${id}`), onSuccess: refresh }),
    refreshStats: useMutation({ mutationFn: (id: string) => post(`/api/posts/${id}/refresh`), onSuccess: refresh }),
  };
}

// --- team ----------------------------------------------------------------------------------
export const useUsers = () => useQuery({ queryKey: keys.users, queryFn: () => get<User[]>("/api/users") });
