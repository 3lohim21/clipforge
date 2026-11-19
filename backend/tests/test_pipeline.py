"""End-to-end: link -> download -> process -> transcribe -> detect moments -> edit -> render ->
download, through the real API, real job queue, real yt-dlp and real ffmpeg."""

from __future__ import annotations

import time
from pathlib import Path

from clipforge.media.ffmpeg import probe


def _project(client, **settings) -> str:  # type: ignore[no-untyped-def]
    return client.post("/api/projects", json={"name": "Client A", "settings": settings}).json()["id"]


def test_link_to_rendered_clip(auth_client, media_server, fake_whisper, run_jobs, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    pid = _project(
        auth_client, min_clip_s=8, max_clip_s=20, target_clip_s=12, cpm_usd=2.0, watermark="@client"
    )
    r = auth_client.post(f"/api/projects/{pid}/videos", json={"urls": [f"{media_server}/talk.mp4"]})
    assert r.status_code == 201, r.text
    vid = r.json()[0]["id"]

    t0 = time.perf_counter()
    assert run_jobs() >= 3  # ingest -> transcribe -> analyze
    pipeline_s = time.perf_counter() - t0

    video = auth_client.get(f"/api/videos/{vid}").json()
    assert video["status"] == "ready", video["error"]
    assert video["duration_ms"] == 40_000 and (video["width"], video["height"]) == (1280, 720)
    assert video["transcript_source"] == "whisper" and video["has_transcript"]
    assert video["analysis"]["clips"] >= 1 and pipeline_s < 60

    # The editor's data: streamable playback, waveform timeline, transcript, captions.
    playback = auth_client.get(video["playback_url"], headers={"Range": "bytes=0-1023"})
    assert playback.status_code == 206 and len(playback.content) == 1024
    assert auth_client.get(video["poster_url"]).headers["content-type"] == "image/jpeg"
    timeline = auth_client.get(f"/api/videos/{vid}/timeline").json()
    assert timeline["energy_u8"] and timeline["duration_ms"] == 40_000
    assert len(auth_client.get(f"/api/videos/{vid}/transcript").json()["words"]) > 50
    chunks = auth_client.get(f"/api/videos/{vid}/captions?preset=bold").json()["chunks"]
    assert chunks and all(len(c[2]) <= 3 for c in chunks)

    clips = auth_client.get(f"/api/videos/{vid}/clips").json()
    assert clips and all(c["origin"] == "ai" and c["status"] == "suggested" for c in clips)
    best = max(clips, key=lambda c: c["score"])
    assert 7_000 <= best["end_ms"] - best["start_ms"] <= 21_000 and best["title"]

    # Trim, restyle, render.
    r = auth_client.patch(
        f"/api/clips/{best['id']}",
        json={
            "start_ms": best["start_ms"] + 500,
            "title": "Edited title",
            "edit": {
                "layout": "vertical_fill",
                "caption_preset": "karaoke",
                "title_overlay": True,
                "reframe": "center",
            },
        },
    )
    assert r.status_code == 200 and r.json()["edit"]["caption_preset"] == "karaoke"
    bad = auth_client.patch(f"/api/clips/{best['id']}", json={"end_ms": best["start_ms"]})
    assert bad.status_code == 422

    r = auth_client.post(f"/api/clips/{best['id']}/render", json={})
    assert r.status_code == 202
    render = r.json()
    again = auth_client.post(f"/api/clips/{best['id']}/render", json={}).json()
    assert again["id"] == render["id"], "double-click must not queue a duplicate export"
    assert run_jobs() == 1

    done = auth_client.get(f"/api/renders/{render['id']}").json()
    assert done["status"] == "done", done["error"]
    assert (done["width"], done["height"]) == (1080, 1920)
    file = auth_client.get(done["download_url"])
    assert file.status_code == 200 and "attachment" in file.headers["content-disposition"]
    out = tmp_path / "export.mp4"
    out.write_bytes(file.content)
    meta = probe(out)
    assert (meta.width, meta.height, meta.vcodec) == (1080, 1920, "h264")
    assert abs(meta.duration_s * 1000 - (best["end_ms"] - best["start_ms"] - 500)) < 250

    # The clip is now "saved" and shows up in the project library with its export.
    library = auth_client.get(f"/api/projects/{pid}/clips?status=saved").json()
    assert library[0]["latest_render"]["status"] == "done" and library[0]["video_title"]

    # Track where it was posted; stats refresh runs as a job (the local URL has no view count).
    post = auth_client.post(
        f"/api/clips/{best['id']}/posts", json={"url": "https://www.tiktok.com/@client/video/123"}
    )
    assert post.status_code == 201 and post.json()["platform"] == "tiktok"
    assert (
        auth_client.post(
            f"/api/clips/{best['id']}/posts", json={"url": "https://www.tiktok.com/@client/video/123"}
        ).status_code
        == 409
    )
    summary = auth_client.get(f"/api/projects/{pid}/posts").json()
    assert summary["total_views"] == 0 and summary["est_revenue_usd"] == 0.0

    stats = auth_client.get(f"/api/projects/{pid}").json()["stats"]
    assert (
        stats["videos"] == 1 and stats["renders"] == 1 and stats["clips_saved"] >= 1 and stats["posts"] == 1
    )

    # Deleting the video removes rows now and media files via a delayed cleanup job.
    assert auth_client.delete(f"/api/videos/{vid}").status_code == 204
    assert auth_client.get(f"/api/videos/{vid}").status_code == 404
    assert auth_client.get(f"/api/renders/{render['id']}").status_code == 404


def test_manual_clip_and_face_tracked_render(auth_client, media_server, fake_whisper, run_jobs) -> None:  # type: ignore[no-untyped-def]
    pid = _project(auth_client)
    vid = auth_client.post(f"/api/projects/{pid}/videos", json={"urls": [f"{media_server}/talk.mp4"]}).json()[
        0
    ]["id"]
    run_jobs()
    r = auth_client.post(
        f"/api/videos/{vid}/clips", json={"start_ms": 1000, "end_ms": 6000, "title": "Manual"}
    )
    assert r.status_code == 201 and r.json()["origin"] == "manual" and r.json()["status"] == "saved"
    clip = r.json()
    assert (
        auth_client.post(f"/api/videos/{vid}/clips", json={"start_ms": 1000, "end_ms": 1200}).status_code
        == 422
    )
    render = auth_client.post(
        f"/api/clips/{clip['id']}/render", json={"edit": {"layout": "square_fill"}}
    ).json()
    run_jobs()
    done = auth_client.get(f"/api/renders/{render['id']}").json()
    assert done["status"] == "done" and (done["width"], done["height"]) == (1080, 1080)
    thumb = auth_client.get(done["thumbnail_url"])
    assert thumb.status_code == 200 and "immutable" in thumb.headers["cache-control"]


def test_upload_webm_gets_browser_proxy(auth_client, webm_video, fake_whisper, run_jobs) -> None:  # type: ignore[no-untyped-def]
    pid = _project(auth_client)
    r = auth_client.put(
        f"/api/projects/{pid}/videos/upload?filename=My_Stream.webm", content=webm_video.read_bytes()
    )
    assert r.status_code == 201, r.text
    vid = r.json()["id"]
    assert r.json()["title"] == "My Stream" and r.json()["source_type"] == "upload"
    run_jobs()
    video = auth_client.get(f"/api/videos/{vid}").json()
    assert video["status"] == "ready", video["error"]
    head = auth_client.get(video["playback_url"], headers={"Range": "bytes=0-99"})
    assert head.status_code == 206  # an H.264 proxy the browser can play


def test_bad_link_fails_cleanly_and_retry_resumes(auth_client, media_server, fake_whisper, run_jobs) -> None:  # type: ignore[no-untyped-def]
    pid = _project(auth_client)
    vid = auth_client.post(
        f"/api/projects/{pid}/videos", json={"urls": [f"{media_server}/missing.mp4"]}
    ).json()[0]["id"]
    run_jobs()
    video = auth_client.get(f"/api/videos/{vid}").json()
    assert video["status"] == "failed" and "404" in video["error"]
    assert auth_client.post(f"/api/videos/{vid}/retry").json()["status"] == "queued"


def test_reanalyze_keeps_saved_clips(auth_client, media_server, fake_whisper, run_jobs) -> None:  # type: ignore[no-untyped-def]
    pid = _project(auth_client, min_clip_s=8, max_clip_s=20, target_clip_s=12)
    vid = auth_client.post(f"/api/projects/{pid}/videos", json={"urls": [f"{media_server}/talk.mp4"]}).json()[
        0
    ]["id"]
    run_jobs()
    clips = auth_client.get(f"/api/videos/{vid}/clips").json()
    keep = clips[0]["id"]
    auth_client.patch(f"/api/clips/{keep}", json={"status": "saved"})
    assert auth_client.post(f"/api/videos/{vid}/reanalyze").json()["status"] == "analyzing"
    run_jobs()
    after = auth_client.get(f"/api/videos/{vid}/clips").json()
    assert any(c["id"] == keep and c["status"] == "saved" for c in after)
