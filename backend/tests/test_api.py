"""HTTP API: auth, tenancy isolation, validation, security headers."""

from __future__ import annotations

import uuid

from sqlalchemy import select

from clipforge.core.security import hash_password
from clipforge.db import session_scope
from clipforge.db.models import Project, User, Workspace
from tests.conftest import ADMIN


def test_health_and_docs_are_public(client) -> None:  # type: ignore[no-untyped-def]
    assert client.get("/api/healthz").json()["ok"] is True
    assert client.get("/api/readyz").status_code == 200
    spec = client.get("/api/openapi.json").json()
    assert len(spec["paths"]) >= 30
    metrics = client.get("/metrics")
    assert metrics.status_code == 200 and "clipforge_http_requests_total" in metrics.text


def test_login_logout_and_session_cookie(client) -> None:  # type: ignore[no-untyped-def]
    assert client.get("/api/projects").status_code == 401
    bad = client.post("/api/auth/login", json={"email": ADMIN[0], "password": "wrong"})
    assert bad.status_code == 401
    ok = client.post("/api/auth/login", json={"email": ADMIN[0].upper(), "password": ADMIN[1]})
    assert ok.status_code == 200 and ok.json()["user"]["role"] == "owner"
    cookie = ok.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie
    assert client.get("/api/auth/me").json()["user"]["email"] == ADMIN[0]
    assert client.post("/api/auth/logout").status_code == 204
    assert client.get("/api/auth/me").status_code == 401


def test_login_is_rate_limited(client) -> None:  # type: ignore[no-untyped-def]
    from clipforge.api.routes import auth

    auth._FAILS.clear()
    codes = [client.post("/api/auth/login", json={"email": "x@y.z", "password": "nope"}).status_code for _ in range(11)]
    assert codes[:10] == [401] * 10 and codes[10] == 429
    auth._FAILS.clear()


def test_cross_site_writes_are_blocked(auth_client) -> None:  # type: ignore[no-untyped-def]
    r = auth_client.post("/api/projects", json={"name": "x"}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    same = auth_client.post("/api/projects", json={"name": "x"}, headers={"Origin": "http://testserver"})
    assert same.status_code == 201


def test_project_crud_and_settings_validation(auth_client) -> None:  # type: ignore[no-untyped-def]
    r = auth_client.post("/api/projects", json={"name": " Podcast client ", "settings": {"caption_preset": "beast", "cpm_usd": 1.5}})
    assert r.status_code == 201
    p = r.json()
    assert p["name"] == "Podcast client" and p["settings"]["caption_preset"] == "beast"
    assert p["stats"]["videos"] == 0
    r = auth_client.patch(f"/api/projects/{p['id']}", json={"settings": {"min_clip_s": 20, "max_clip_s": 45}})
    assert r.json()["settings"]["max_clip_s"] == 45
    assert auth_client.patch(f"/api/projects/{p['id']}", json={"settings": {"caption_position": 5}}).status_code == 422
    assert [x["id"] for x in auth_client.get("/api/projects").json()] == [p["id"]]
    assert auth_client.delete(f"/api/projects/{p['id']}").status_code == 204
    assert auth_client.get(f"/api/projects/{p['id']}").status_code == 404


def test_other_workspaces_are_invisible(auth_client) -> None:  # type: ignore[no-untyped-def]
    with session_scope() as s:
        other = Workspace(name="Rival agency")
        s.add(other)
        s.flush()
        project = Project(workspace_id=other.id, name="secret")
        s.add(project)
        s.flush()
        pid = project.id
    assert auth_client.get(f"/api/projects/{pid}").status_code == 404
    assert auth_client.get(f"/api/projects/{pid}/videos").status_code == 404
    assert auth_client.post(f"/api/projects/{pid}/videos", json={"urls": ["https://youtu.be/x"]}).status_code == 404
    assert auth_client.get(f"/api/videos/{uuid.uuid4()}").status_code == 404
    assert all(p["name"] != "secret" for p in auth_client.get("/api/projects").json())


def test_import_validation(auth_client) -> None:  # type: ignore[no-untyped-def]
    pid = auth_client.post("/api/projects", json={"name": "p"}).json()["id"]
    r = auth_client.post(f"/api/projects/{pid}/videos", json={"urls": ["ftp://nope/x", "https://ok.example.com/v"]})
    assert r.status_code == 422
    r = auth_client.post(f"/api/projects/{pid}/videos", json={"text": ""})
    assert r.status_code == 422
    r = auth_client.post(
        f"/api/projects/{pid}/videos",
        json={"text": "https://youtu.be/a\nhttps://youtu.be/a https://www.twitch.tv/videos/1", "section_start_s": 60, "section_end_s": 30},
    )
    assert r.status_code == 422  # end before start
    r = auth_client.post(f"/api/projects/{pid}/videos", json={"text": "https://youtu.be/a\nhttps://youtu.be/a https://www.twitch.tv/videos/1"})
    assert r.status_code == 201
    vids = r.json()
    assert [v["platform"] for v in vids] == ["youtube", "twitch"]  # deduped
    assert all(v["status"] == "queued" for v in vids)


def test_upload_rejects_wrong_types_and_empty_bodies(auth_client) -> None:  # type: ignore[no-untyped-def]
    pid = auth_client.post("/api/projects", json={"name": "p"}).json()["id"]
    assert auth_client.put(f"/api/projects/{pid}/videos/upload?filename=evil.exe", content=b"MZ").status_code == 415
    assert auth_client.put(f"/api/projects/{pid}/videos/upload?filename=a.mp4", content=b"").status_code == 400


def test_team_management_requires_admin(auth_client) -> None:  # type: ignore[no-untyped-def]
    r = auth_client.post("/api/users", json={"email": "Editor@Example.com", "password": "long-enough-pw", "role": "editor"})
    assert r.status_code == 201 and r.json()["email"] == "editor@example.com"
    assert auth_client.post("/api/users", json={"email": "editor@example.com", "password": "long-enough-pw"}).status_code == 409
    owner_id = next(u["id"] for u in auth_client.get("/api/users").json() if u["role"] == "owner")
    assert auth_client.delete(f"/api/users/{owner_id}").status_code == 400

    auth_client.post("/api/auth/logout")
    assert auth_client.post("/api/auth/login", json={"email": "editor@example.com", "password": "long-enough-pw"}).status_code == 200
    assert auth_client.post("/api/users", json={"email": "z@z.co", "password": "long-enough-pw"}).status_code == 403


def test_password_change_signs_out_other_sessions(client) -> None:  # type: ignore[no-untyped-def]
    with session_scope() as s:
        ws = s.scalar(select(Workspace))
        s.add(User(workspace_id=ws.id, email="pw@example.com", password_hash=hash_password("first-password"), role="editor"))
    from fastapi.testclient import TestClient

    other = TestClient(client.app, base_url="http://testserver")
    for c in (client, other):
        assert c.post("/api/auth/login", json={"email": "pw@example.com", "password": "first-password"}).status_code == 200
    r = client.post("/api/auth/password", json={"current_password": "first-password", "new_password": "second-password"})
    assert r.status_code == 204
    from clipforge.api.deps import auth_cache

    auth_cache._data.clear()
    assert client.get("/api/auth/me").status_code == 200
    assert other.get("/api/auth/me").status_code == 401


def test_presets_and_fonts(auth_client) -> None:  # type: ignore[no-untyped-def]
    catalog = auth_client.get("/api/presets").json()
    assert {p["id"] for p in catalog["caption_presets"]} >= {"bold", "beast", "karaoke"}
    font = catalog["caption_presets"][0]["font_file"]
    r = auth_client.get(f"/api/fonts/{font}")
    assert r.status_code == 200 and "immutable" in r.headers["cache-control"]
    assert auth_client.get("/api/fonts/..%2F..%2Fconfig.py").status_code == 404
