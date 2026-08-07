"""Local, one-track-at-a-time broad genre review."""

import json
import sqlite3
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from crate_digger.collection.genre_review import TAXONOMY, decide, list_reviews
from crate_digger.web.templating import render_template


def pending_count(db_path: Path) -> int:
    if not db_path.is_file():
        return 0
    with sqlite3.connect(db_path) as conn:
        if (
            conn.execute(
                "select 1 from sqlite_master where name='genre_reviews'"
            ).fetchone()
            is None
        ):
            return 0
        return int(
            conn.execute(
                "select count(*) from genre_reviews where status='pending'"
            ).fetchone()[0]
        )


def create_genres_router(db_path: Path) -> APIRouter:
    router = APIRouter()

    @router.get("/genres", response_class=HTMLResponse)
    def genre_page(status: str = "pending", path: str = "") -> HTMLResponse:
        try:
            return HTMLResponse(_page(db_path, status, path))
        except ValueError as error:
            return HTMLResponse(
                _page(db_path, "pending", "", str(error)), status_code=400
            )

    @router.post("/genres/decide")
    async def genre_decision(request: Request) -> Response:
        form = {
            key: values[-1]
            for key, values in parse_qs(
                (await request.body()).decode("utf-8"), keep_blank_values=True
            ).items()
        }
        path = form.get("path", "")
        status = form.get("status", "pending")
        if status not in {"pending", "approved", "deferred", "baseline", "all"}:
            status = "pending"
        rows = list_reviews(db_path, status)
        paths = [row["track_path"] for row in rows]
        next_path = (
            paths[paths.index(path) + 1]
            if path in paths and paths.index(path) + 1 < len(paths)
            else ""
        )
        try:
            decide(
                db_path,
                path,
                form.get("action", ""),
                form.get("actor", ""),
                form.get("genre") or None,
                form.get("note", ""),
            )
        except ValueError as error:
            return HTMLResponse(
                _page(db_path, status, path, str(error)), status_code=400
            )
        query = {"status": status}
        if next_path:
            query["path"] = next_path
        return RedirectResponse("/genres?" + urlencode(query), status_code=303)

    @router.get("/api/genres")
    def genre_api(status: str = "pending") -> dict:
        rows = list_reviews(db_path, status)
        return {
            "status": status,
            "count": len(rows),
            "pending": pending_count(db_path),
            "tracks": rows,
        }

    return router


def _safe_link(value: str | None) -> str:
    if not value:
        return ""
    for kind in ("track", "artist", "album"):
        prefix = f"spotify:{kind}:"
        if value.startswith(prefix):
            identifier = value.removeprefix(prefix)
            if identifier.isalnum():
                return f"https://open.spotify.com/{kind}/{identifier}"
    parts = urlsplit(value)
    if parts.scheme == "https" and parts.netloc in {
        "open.spotify.com",
        "soundcloud.com",
        "www.soundcloud.com",
        "music.apple.com",
    }:
        return value
    return ""


def _page(db_path: Path, status: str, path: str, error: str = "") -> str:
    rows = list_reviews(db_path, status)
    pending = pending_count(db_path)
    selected = next(
        (index for index, row in enumerate(rows) if row["track_path"] == path), 0
    )
    row = rows[selected] if rows else None
    source_links: list[str] = []
    previous_href = ""
    next_href = ""
    spotify = ""
    soundcloud = ""
    local_status = ""
    spotify_status = ""
    evidence_pretty = ""
    newer_evidence_pretty = None
    if row is not None:
        if selected:
            previous_href = "/genres?" + urlencode(
                {"status": status, "path": rows[selected - 1]["track_path"]}
            )
        if selected + 1 < len(rows):
            next_href = "/genres?" + urlencode(
                {"status": status, "path": rows[selected + 1]["track_path"]}
            )

        def collect(value: object) -> None:
            if isinstance(value, dict):
                for nested in value.values():
                    collect(nested)
            elif isinstance(value, list):
                for nested in value:
                    collect(nested)
            elif isinstance(value, str):
                link = _safe_link(value)
                if link and link not in source_links:
                    source_links.append(link)

        evidence = row["evidence"]
        collect(evidence)
        if row["newer_evidence"] is not None:
            collect(row["newer_evidence"])
            newer_evidence_pretty = json.dumps(
                row["newer_evidence"], indent=2, ensure_ascii=False
            )
        spotify = _safe_link(row["spotify_uri"])
        soundcloud = _safe_link(row["soundcloud_url"])
        local_status = (
            "Local file linked"
            if Path(row["track_path"]).is_file()
            else "Local file unavailable"
        )
        spotify_status = str(evidence.get("spotify_link_status", "unknown"))
        evidence_pretty = json.dumps(evidence, indent=2, ensure_ascii=False)

    return render_template(
        "genres.html",
        statuses=(
            ("pending", f"Pending ({pending})"),
            ("approved", "Reviewed"),
            ("deferred", "Deferred"),
            ("baseline", "Baseline"),
            ("all", "All"),
        ),
        status=status,
        error=error,
        row=row,
        selected=selected,
        rows_count=len(rows),
        previous_href=previous_href,
        next_href=next_href,
        source_links=source_links,
        spotify=spotify,
        soundcloud=soundcloud,
        local_status=local_status,
        spotify_status=spotify_status,
        evidence_pretty=evidence_pretty,
        newer_evidence_pretty=newer_evidence_pretty,
        taxonomy=TAXONOMY,
    )
