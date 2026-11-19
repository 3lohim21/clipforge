"""Serving media files.

Development: Starlette's FileResponse (supports HTTP Range, so the player can seek).
Production: the API only authorises the request and answers with an X-Accel-Redirect
header; Caddy/nginx then streams the bytes with sendfile from the shared volume. Python
never shovels video bytes, so one small API instance can back hundreds of viewers.
"""

from __future__ import annotations

import re
import unicodedata
from urllib.parse import quote

from fastapi import HTTPException, Response
from fastapi.responses import FileResponse

from clipforge.config import get_settings
from clipforge.storage import StorageError, get_storage

IMMUTABLE = "private, max-age=31536000, immutable"
REVALIDATE = "private, no-cache"


def _ascii_filename(name: str) -> str:
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    ascii_name = re.sub(r"[^A-Za-z0-9._ -]+", "", ascii_name).strip() or "clip"
    return ascii_name[:120]


def media_response(
    key: str | None,
    *,
    media_type: str,
    cache_control: str = REVALIDATE,
    download_name: str | None = None,
) -> Response:
    storage = get_storage()
    try:
        if not key or not storage.exists(key):
            raise HTTPException(404, "File not found")
        path = storage.path(key)
    except StorageError as exc:
        raise HTTPException(404, "File not found") from exc

    headers = {"Cache-Control": cache_control, "X-Content-Type-Options": "nosniff"}
    if download_name:
        headers["Content-Disposition"] = (
            f'attachment; filename="{_ascii_filename(download_name)}"; '
            f"filename*=UTF-8''{quote(download_name[:120])}"
        )
    settings = get_settings()
    if settings.media_accel_redirect:
        headers["X-Accel-Redirect"] = f"{settings.media_accel_prefix}/{key}"
        return Response(status_code=200, headers=headers, media_type=media_type)
    return FileResponse(path, media_type=media_type, headers=headers)
