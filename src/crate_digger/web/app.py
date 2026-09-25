import json
import shutil
import subprocess
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from queue import Empty, Queue
from tempfile import TemporaryDirectory
from threading import Lock, Thread
from typing import Any, Literal, TypeVar, cast
from urllib.parse import parse_qs, parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import Request as UrlRequest, urlopen

from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from crate_digger.collection.index import (
    DEFAULT_COLLECTION_DB_PATH,
    delete_track,
    get_track_artwork,
    get_track_for_spotify_linking,
    list_tracks_for_comment_cleanup,
    list_tracks_missing_spotify_artwork,
    list_tracks_pending_spotify_linking,
    query_tracks,
    refresh_collection_index,
    refresh_track_metadata,
    set_track_soundcloud_url,
    set_track_spotify_uri,
    skip_track_spotify_link,
)
from crate_digger.collection.comments import (
    CommentCleanupWriteResult,
    clear_comment,
    write_rekordbox_comment_tags_only,
)
from crate_digger.collection.models import LocalTrack
from crate_digger.collection.scanner import overwrite_embedded_artwork
from crate_digger.utils.config import get_settings
from crate_digger.utils.logging import get_logger
from crate_digger.utils.spotify import get_spotify_client
from crate_digger.web.discover import create_discover_router
from crate_digger.web.collections import create_saved_collections_router
from crate_digger.web.genres import create_genres_router, pending_count
from crate_digger.web.templating import STATIC_DIR, render_template

logger = get_logger(__name__)
T = TypeVar("T")

SortKey = Literal[
    "title",
    "artist",
    "album",
    "genre",
    "release_date",
    "file_created_at",
    "format",
    "bitrate",
    "duration",
    "path",
]
SortDirection = Literal["asc", "desc"]
MetadataFilter = Literal["all", "missing", "complete"]
SpotifyFilter = Literal["all", "unlinked", "linked", "skipped"]

DEFAULT_PAGE_SIZE = 10
SPOTIFY_LINK_LIMIT = 5
SPOTIFY_LINK_LOOKUP_TIMEOUT_SECONDS = 12
SPOTIFY_ARTWORK_DOWNLOAD_TIMEOUT_SECONDS = 20
SPOTIFY_SWEEP_TRACK_TIMEOUT_SECONDS = 45
MAX_ARTWORK_DOWNLOAD_BYTES = 8 * 1024 * 1024
SORT_LABELS: dict[SortKey, str] = {
    "title": "Title",
    "artist": "Artist",
    "album": "Album",
    "genre": "Genre",
    "release_date": "Released",
    "file_created_at": "Created",
    "format": "Format",
    "bitrate": "Bitrate",
    "duration": "Duration",
    "path": "Path",
}
METADATA_FILTER_LABELS: dict[MetadataFilter, str] = {
    "all": "All metadata",
    "missing": "Missing tags",
    "complete": "Complete tags",
}
SPOTIFY_FILTER_LABELS: dict[SpotifyFilter, str] = {
    "all": "All Spotify",
    "unlinked": "Unlinked",
    "linked": "Linked",
    "skipped": "Skipped",
}


@dataclass(frozen=True)
class CollectionQuery:
    q: str
    audio_format: str
    metadata: MetadataFilter
    spotify: SpotifyFilter
    sort: SortKey
    direction: SortDirection
    page: int
    page_size: int


@dataclass(frozen=True)
class CollectionView:
    query: CollectionQuery
    tracks: list[LocalTrack]
    filtered_count: int
    total_count: int
    total_pages: int
    formats: list[str]


@dataclass(frozen=True)
class SpotifyCandidate:
    uri: str
    name: str
    artists: str
    album: str
    image_url: str | None
    external_url: str | None


@dataclass(frozen=True)
class AutoArtworkTrackResult:
    linked: bool = False
    artwork_updated: bool = False
    no_results: bool = False


@dataclass
class AutoArtworkRefreshState:
    running: bool = False
    total: int = 0
    processed: int = 0
    linked: int = 0
    artwork_updated: int = 0
    no_results: int = 0
    failed: int = 0
    current: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    lock: Lock = field(default_factory=Lock, repr=False)


@dataclass
class CommentCleanupState:
    running: bool = False
    total: int = 0
    processed: int = 0
    cleaned: int = 0
    skipped: int = 0
    failed: int = 0
    current: str | None = None
    last_result: dict[str, object] | None = None
    started_at: str | None = None
    finished_at: str | None = None
    lock: Lock = field(default_factory=Lock, repr=False)


def create_app(
    config_path: str = "config.toml",
    db_path: Path = DEFAULT_COLLECTION_DB_PATH,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        collection = get_settings(config_path)["collection"]
        app.state.collection_index_stats = refresh_collection_index(
            collection["music_dirs"],
            db_path=db_path,
        )
        app.state.auto_artwork_refresh_state = AutoArtworkRefreshState()
        app.state.comment_cleanup_state = CommentCleanupState()
        yield

    app = FastAPI(title="Crate Digger Dashboard", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.include_router(create_discover_router(db_path, config_path))
    app.include_router(create_saved_collections_router(db_path))
    app.include_router(create_genres_router(db_path))

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/tracks")
    def tracks(
        q: str = "",
        audio_format: str = Query("", alias="format"),
        metadata: str = "all",
        spotify: str = "all",
        sort: str = "title",
        direction: str = "asc",
        page: int = 1,
    ) -> dict[str, object]:
        view = _build_collection_view(
            db_path,
            q=q,
            audio_format=audio_format,
            metadata=metadata,
            spotify=spotify,
            sort=sort,
            direction=direction,
            page=page,
            page_size=DEFAULT_PAGE_SIZE,
        )
        return {
            "tracks": [_track_to_json(track) for track in view.tracks],
            "filtered_count": view.filtered_count,
            "total_count": view.total_count,
            "page": view.query.page,
            "page_size": view.query.page_size,
            "total_pages": view.total_pages,
        }

    @app.get("/", response_class=HTMLResponse)
    def index(
        q: str = "",
        audio_format: str = Query("", alias="format"),
        metadata: str = "all",
        spotify: str = "all",
        sort: str = "title",
        direction: str = "asc",
        page: int = 1,
    ) -> HTMLResponse:
        collection = get_settings(config_path)["collection"]
        view = _build_collection_view(
            db_path,
            q=q,
            audio_format=audio_format,
            metadata=metadata,
            spotify=spotify,
            sort=sort,
            direction=direction,
            page=page,
            page_size=DEFAULT_PAGE_SIZE,
        )
        return HTMLResponse(
            _render_index(
                view,
                collection["music_dirs"],
                genre_pending=pending_count(db_path),
                auto_artwork_status=_auto_artwork_refresh_snapshot(
                    _get_auto_artwork_refresh_state(app)
                ),
                comment_cleanup_status=_comment_cleanup_snapshot(
                    _get_comment_cleanup_state(app)
                ),
            )
        )

    @app.get("/api/spotify-artwork-refresh")
    def spotify_artwork_refresh_status() -> dict[str, object]:
        return _auto_artwork_refresh_snapshot(_get_auto_artwork_refresh_state(app))

    @app.get("/api/comment-cleanup")
    def comment_cleanup_status() -> dict[str, object]:
        return _comment_cleanup_snapshot(_get_comment_cleanup_state(app))

    @app.post("/spotify-artwork-refresh")
    async def start_spotify_artwork_refresh(request: Request) -> RedirectResponse:
        form = _parse_urlencoded_form(await request.body())
        return_to = _safe_return_to(form.get("return_to"))
        _start_auto_artwork_refresh(
            config_path=config_path,
            db_path=db_path,
            state=_get_auto_artwork_refresh_state(app),
        )
        return RedirectResponse(return_to, status_code=303)

    @app.post("/comment-cleanup")
    async def start_comment_cleanup(request: Request) -> RedirectResponse:
        form = _parse_urlencoded_form(await request.body())
        return_to = _safe_return_to(form.get("return_to"))
        _start_comment_cleanup(
            db_path=db_path,
            state=_get_comment_cleanup_state(app),
        )
        return RedirectResponse(return_to, status_code=303)

    @app.post("/comment-cleanup/track")
    async def start_track_comment_cleanup(request: Request) -> RedirectResponse:
        form = _parse_urlencoded_form(await request.body())
        path = form["path"]
        return_to = _safe_return_to(form.get("return_to"))
        _start_single_track_comment_cleanup(
            db_path=db_path,
            path=path,
            state=_get_comment_cleanup_state(app),
        )
        return RedirectResponse(return_to, status_code=303)

    @app.post("/comment-clear")
    async def start_comment_clear(request: Request) -> RedirectResponse:
        form = _parse_urlencoded_form(await request.body())
        return_to = _safe_return_to(form.get("return_to"))
        _start_comment_clear(
            db_path=db_path,
            state=_get_comment_cleanup_state(app),
        )
        return RedirectResponse(return_to, status_code=303)

    @app.post("/comment-clear/track")
    async def start_track_comment_clear(request: Request) -> RedirectResponse:
        form = _parse_urlencoded_form(await request.body())
        path = form["path"]
        return_to = _safe_return_to(form.get("return_to"))
        _start_single_track_comment_clear(
            db_path=db_path,
            path=path,
            state=_get_comment_cleanup_state(app),
        )
        return RedirectResponse(return_to, status_code=303)

    @app.get("/spotify-link", response_class=HTMLResponse)
    async def spotify_link(
        path: str | None = None,
        offset: int = 0,
        partial: bool = False,
        return_to: str = "/",
    ) -> HTMLResponse:
        safe_return_to = _safe_return_to(return_to)
        collection = get_settings(config_path)["collection"]
        if path is None:
            return HTMLResponse(_render_spotify_link_idle(collection["music_dirs"]))

        track = get_track_for_spotify_linking(db_path, path=path)
        if track is None:
            return HTMLResponse(_render_spotify_link_done(collection["music_dirs"]))
        if not _track_file_exists(db_path, path=path):
            return HTMLResponse(_render_spotify_link_done(collection["music_dirs"]))

        candidates, lookup_error = await _search_spotify_candidates_for_track(
            config_path=config_path,
            track=track,
            offset=max(0, offset),
            limit=SPOTIFY_LINK_LIMIT,
        )
        if partial:
            return HTMLResponse(
                _render_spotify_link_content(
                    track=track,
                    candidates=candidates,
                    offset=max(0, offset),
                    partial=True,
                    return_to=safe_return_to,
                    lookup_error=lookup_error,
                )
            )

        return HTMLResponse(
            _render_spotify_link_page(
                track=track,
                candidates=candidates,
                offset=max(0, offset),
                music_dirs=collection["music_dirs"],
                return_to=safe_return_to,
                lookup_error=lookup_error,
            )
        )

    @app.post("/spotify-link/link")
    async def link_spotify_track(
        request: Request,
    ) -> RedirectResponse:
        form = _parse_urlencoded_form(await request.body())
        path = form["path"]
        spotify_uri = form["spotify_uri"]
        return_to = _safe_return_to(form.get("return_to"))
        if not _track_file_exists(db_path, path=path):
            return RedirectResponse(return_to, status_code=303)

        set_track_spotify_uri(db_path, path=path, spotify_uri=spotify_uri)
        art_started = _start_track_artwork_replacement(
            db_path,
            path=path,
            image_url=form.get("image_url"),
        )
        if art_started:
            return_to = _with_art_refresh(return_to, path=path)
        return RedirectResponse(return_to, status_code=303)

    @app.post("/spotify-link/quick-link")
    async def quick_link_spotify_track(
        request: Request,
    ) -> RedirectResponse:
        form = _parse_urlencoded_form(await request.body())
        return_to = _safe_return_to(form.get("return_to"))
        path = form["path"]
        track = get_track_for_spotify_linking(db_path, path=path)
        if track is None or not _track_file_exists(db_path, path=path):
            return RedirectResponse(return_to, status_code=303)

        candidates, _lookup_error = await _search_spotify_candidates_for_track(
            config_path=config_path,
            track=track,
            offset=0,
            limit=1,
        )
        if not candidates:
            fallback = _spotify_link_href(
                track=track,
                offset=0,
                return_to=return_to,
                partial=False,
            )
            return RedirectResponse(fallback, status_code=303)

        set_track_spotify_uri(db_path, path=path, spotify_uri=candidates[0].uri)
        art_started = _start_track_artwork_replacement(
            db_path,
            path=path,
            image_url=candidates[0].image_url,
        )
        if art_started:
            return_to = _with_art_refresh(return_to, path=path)
        return RedirectResponse(return_to, status_code=303)

    @app.post("/spotify-link/manual")
    async def manual_link_spotify_track(
        request: Request,
    ) -> RedirectResponse:
        form = _parse_urlencoded_form(await request.body())
        path = form["path"]
        return_to = _safe_return_to(form.get("return_to"))
        spotify_uri = _spotify_uri_from_input(form.get("spotify_url", ""))
        if spotify_uri is None:
            return RedirectResponse(return_to, status_code=303)

        track = get_track_for_spotify_linking(db_path, path=path)
        if track is None or not _track_file_exists(db_path, path=path):
            return RedirectResponse(return_to, status_code=303)

        set_track_spotify_uri(db_path, path=path, spotify_uri=spotify_uri)
        art_started = _start_track_artwork_replacement_from_spotify_uri(
            config_path=config_path,
            db_path=db_path,
            path=path,
            spotify_uri=spotify_uri,
        )
        if art_started:
            return_to = _with_art_refresh(return_to, path=path)
        return RedirectResponse(return_to, status_code=303)

    @app.post("/soundcloud-link/manual")
    async def manual_link_soundcloud_track(
        request: Request,
    ) -> RedirectResponse:
        form = _parse_urlencoded_form(await request.body())
        path = form["path"]
        return_to = _safe_return_to(form.get("return_to"))
        soundcloud_url = _soundcloud_url_from_input(form.get("soundcloud_url", ""))
        if soundcloud_url is None:
            return RedirectResponse(return_to, status_code=303)

        track = get_track_for_spotify_linking(db_path, path=path)
        if track is None or not _track_file_exists(db_path, path=path):
            return RedirectResponse(return_to, status_code=303)

        set_track_soundcloud_url(db_path, path=path, soundcloud_url=soundcloud_url)
        art_started = _start_track_artwork_replacement_from_soundcloud_url(
            db_path=db_path,
            path=path,
            soundcloud_url=soundcloud_url,
        )
        if art_started:
            return_to = _with_art_refresh(return_to, path=path)
        return RedirectResponse(return_to, status_code=303)

    @app.post("/soundcloud-link/refresh-art")
    async def refresh_soundcloud_art(request: Request) -> RedirectResponse:
        form = _parse_urlencoded_form(await request.body())
        path = form["path"]
        return_to = _safe_return_to(form.get("return_to"))
        track = get_track_for_spotify_linking(db_path, path=path)
        if (
            track is None
            or not track.soundcloud_url
            or not _track_file_exists(db_path, path=path)
        ):
            return RedirectResponse(return_to, status_code=303)

        art_started = _start_track_artwork_replacement_from_soundcloud_url(
            db_path=db_path,
            path=path,
            soundcloud_url=track.soundcloud_url,
        )
        if art_started:
            return_to = _with_art_refresh(return_to, path=path)
        return RedirectResponse(return_to, status_code=303)

    @app.post("/spotify-link/skip")
    async def skip_spotify_track(request: Request) -> RedirectResponse:
        form = _parse_urlencoded_form(await request.body())
        path = form["path"]
        if not _track_file_exists(db_path, path=path):
            return RedirectResponse(
                _safe_return_to(form.get("return_to")), status_code=303
            )
        skip_track_spotify_link(db_path, path=path)
        return RedirectResponse(_safe_return_to(form.get("return_to")), status_code=303)

    @app.post("/spotify-link/refresh-art")
    async def refresh_spotify_art(request: Request) -> RedirectResponse:
        form = _parse_urlencoded_form(await request.body())
        path = form["path"]
        return_to = _safe_return_to(form.get("return_to"))
        track = get_track_for_spotify_linking(db_path, path=path)
        if (
            track is None
            or not track.spotify_uri
            or not _track_file_exists(db_path, path=path)
        ):
            return RedirectResponse(return_to, status_code=303)

        art_started = _start_track_artwork_replacement_from_spotify_uri(
            config_path=config_path,
            db_path=db_path,
            path=path,
            spotify_uri=track.spotify_uri,
        )
        if art_started:
            return_to = _with_art_refresh(return_to, path=path)
        return RedirectResponse(return_to, status_code=303)

    @app.get("/art")
    def artwork(track_path: str = Query(..., alias="path")) -> Response:
        if not _track_file_exists(db_path, path=track_path):
            return Response(status_code=404)
        result = get_track_artwork(db_path, path=track_path)
        if result is None:
            return Response(status_code=404)
        mime, data = result
        return Response(
            content=data,
            media_type=mime,
            headers={"Cache-Control": "no-store, max-age=0"},
        )

    @app.post("/reindex")
    def reindex() -> RedirectResponse:
        collection = get_settings(config_path)["collection"]
        app.state.collection_index_stats = refresh_collection_index(
            collection["music_dirs"],
            db_path=db_path,
        )
        return RedirectResponse("/", status_code=303)

    return app


def _track_to_json(track: LocalTrack) -> dict[str, object]:
    return {
        "path": str(track.path),
        "title": track.title,
        "artist": track.artist,
        "album": track.album,
        "comment": track.comment,
        "genre": track.genre,
        "release_date": track.release_date,
        "file_created_at": track.file_created_at,
        "duration_seconds": track.duration_seconds,
        "bitrate": track.bitrate,
        "audio_format": track.audio_format,
        "has_artwork": track.artwork_mime is not None,
        "spotify_uri": track.spotify_uri,
        "soundcloud_url": track.soundcloud_url,
        "spotify_link_skipped_at": track.spotify_link_skipped_at,
        "indexed_at": track.indexed_at,
    }


def _parse_urlencoded_form(body: bytes) -> dict[str, str]:
    parsed = parse_qs(body.decode("utf-8"), keep_blank_values=True)
    return {key: values[0] for key, values in parsed.items() if values}


def _track_file_exists(db_path: Path, *, path: str) -> bool:
    if Path(path).is_file():
        return True
    delete_track(db_path, path=path)
    return False


def _get_auto_artwork_refresh_state(app: FastAPI) -> AutoArtworkRefreshState:
    state = getattr(app.state, "auto_artwork_refresh_state", None)
    if isinstance(state, AutoArtworkRefreshState):
        return state

    state = AutoArtworkRefreshState(finished_at=_now_iso())
    app.state.auto_artwork_refresh_state = state
    return state


def _get_comment_cleanup_state(app: FastAPI) -> CommentCleanupState:
    state = getattr(app.state, "comment_cleanup_state", None)
    if isinstance(state, CommentCleanupState):
        return state

    state = CommentCleanupState(finished_at=_now_iso())
    app.state.comment_cleanup_state = state
    return state


def _start_auto_artwork_refresh(
    *,
    config_path: str,
    db_path: Path,
    state: AutoArtworkRefreshState,
) -> bool:
    with state.lock:
        if state.running:
            return False
        state.running = True
        state.total = 0
        state.processed = 0
        state.linked = 0
        state.artwork_updated = 0
        state.no_results = 0
        state.failed = 0
        state.current = None
        state.started_at = _now_iso()
        state.finished_at = None

    Thread(
        target=_auto_refresh_spotify_artwork_safely,
        kwargs={"config_path": config_path, "db_path": db_path, "state": state},
        name="spotify-artwork-auto-refresh",
        daemon=True,
    ).start()
    return True


def _start_comment_cleanup(
    *,
    db_path: Path,
    state: CommentCleanupState,
) -> bool:
    return _start_comment_job(
        db_path=db_path,
        state=state,
        writer=write_rekordbox_comment_tags_only,
        thread_name="comment-cleanup",
        clear_all=False,
    )


def _start_single_track_comment_cleanup(
    *,
    db_path: Path,
    path: str,
    state: CommentCleanupState,
) -> bool:
    return _start_single_track_comment_job(
        db_path=db_path,
        path=path,
        state=state,
        writer=write_rekordbox_comment_tags_only,
        thread_name="comment-cleanup-track",
        clear_all=False,
    )


def _start_comment_clear(
    *,
    db_path: Path,
    state: CommentCleanupState,
) -> bool:
    return _start_comment_job(
        db_path=db_path,
        state=state,
        writer=clear_comment,
        thread_name="comment-clear",
        clear_all=True,
    )


def _start_single_track_comment_clear(
    *,
    db_path: Path,
    path: str,
    state: CommentCleanupState,
) -> bool:
    return _start_single_track_comment_job(
        db_path=db_path,
        path=path,
        state=state,
        writer=clear_comment,
        thread_name="comment-clear-track",
        clear_all=True,
    )


def _start_comment_job(
    *,
    db_path: Path,
    state: CommentCleanupState,
    writer: Callable[[Path], CommentCleanupWriteResult],
    thread_name: str,
    clear_all: bool,
) -> bool:
    with state.lock:
        if state.running:
            return False
        state.running = True
        state.total = 0
        state.processed = 0
        state.cleaned = 0
        state.skipped = 0
        state.failed = 0
        state.current = None
        state.last_result = None
        state.started_at = _now_iso()
        state.finished_at = None

    Thread(
        target=_cleanup_comment_metadata_safely,
        kwargs={
            "db_path": db_path,
            "state": state,
            "writer": writer,
            "clear_all": clear_all,
        },
        name=thread_name,
        daemon=True,
    ).start()
    return True


def _start_single_track_comment_job(
    *,
    db_path: Path,
    path: str,
    state: CommentCleanupState,
    writer: Callable[[Path], CommentCleanupWriteResult],
    thread_name: str,
    clear_all: bool,
) -> bool:
    with state.lock:
        if state.running:
            return False
        state.running = True
        state.total = 1
        state.processed = 0
        state.cleaned = 0
        state.skipped = 0
        state.failed = 0
        state.current = Path(path).stem
        state.last_result = None
        state.started_at = _now_iso()
        state.finished_at = None

    Thread(
        target=_cleanup_single_track_comment_metadata_safely,
        kwargs={
            "db_path": db_path,
            "path": path,
            "state": state,
            "writer": writer,
            "clear_all": clear_all,
        },
        name=thread_name,
        daemon=True,
    ).start()
    return True


def _cleanup_comment_metadata_safely(
    *,
    db_path: Path,
    state: CommentCleanupState,
    writer: Callable[[Path], CommentCleanupWriteResult],
    clear_all: bool,
) -> None:
    try:
        _cleanup_comment_metadata(
            db_path=db_path,
            state=state,
            writer=writer,
            clear_all=clear_all,
        )
    except Exception:
        logger.exception("Comment cleanup sweep failed")
    finally:
        with state.lock:
            state.running = False
            state.current = None
            state.finished_at = _now_iso()


def _cleanup_single_track_comment_metadata_safely(
    *,
    db_path: Path,
    path: str,
    state: CommentCleanupState,
    writer: Callable[[Path], CommentCleanupWriteResult],
    clear_all: bool,
) -> None:
    try:
        _cleanup_single_track_comment_metadata(
            db_path=db_path,
            path=path,
            state=state,
            writer=writer,
            clear_all=clear_all,
        )
    except Exception:
        logger.exception("Single-track comment cleanup failed")
    finally:
        with state.lock:
            state.running = False
            state.current = None
            state.finished_at = _now_iso()


def _cleanup_comment_metadata(
    *,
    db_path: Path,
    state: CommentCleanupState,
    writer: Callable[[Path], CommentCleanupWriteResult] | None = None,
    clear_all: bool = False,
) -> None:
    tracks = list_tracks_for_comment_cleanup(db_path)
    comment_writer = writer or write_rekordbox_comment_tags_only
    with state.lock:
        state.total = len(tracks)

    for track in tracks:
        path = str(track.path)
        with state.lock:
            state.current = f"{track.display_artist} - {track.display_title}"

        if not _track_file_exists(db_path, path=path):
            with state.lock:
                state.processed += 1
                state.skipped += 1
            continue

        try:
            result = comment_writer(track.path)
            refreshed = (
                refresh_track_metadata(db_path, path=path) if result.cleaned else False
            )
        except Exception:
            logger.exception("Failed comment cleanup for %s", path)
            with state.lock:
                state.processed += 1
                state.failed += 1
            continue

        with state.lock:
            state.processed += 1
            state.last_result = _comment_cleanup_result_snapshot(
                track=track,
                result=result,
                refreshed=refreshed,
                clear_all=clear_all,
            )
            if result.cleaned and refreshed:
                state.cleaned += 1
            elif result.cleaned:
                state.failed += 1
            else:
                state.skipped += 1


def _cleanup_single_track_comment_metadata(
    *,
    db_path: Path,
    path: str,
    state: CommentCleanupState,
    writer: Callable[[Path], CommentCleanupWriteResult] | None = None,
    clear_all: bool = False,
) -> None:
    comment_writer = writer or write_rekordbox_comment_tags_only
    with state.lock:
        state.total = 1

    if not _track_file_exists(db_path, path=path):
        with state.lock:
            state.processed = 1
            state.skipped = 1
        return

    try:
        result = comment_writer(Path(path))
        refreshed = (
            refresh_track_metadata(db_path, path=path) if result.cleaned else False
        )
    except Exception:
        logger.exception("Failed comment cleanup for %s", path)
        with state.lock:
            state.processed = 1
            state.failed = 1
        return

    with state.lock:
        state.processed = 1
        state.last_result = _comment_cleanup_result_snapshot(
            track=None,
            path=path,
            result=result,
            refreshed=refreshed,
            clear_all=clear_all,
        )
        if result.cleaned and refreshed:
            state.cleaned = 1
        elif result.cleaned:
            state.failed = 1
        else:
            state.skipped = 1


def _auto_refresh_spotify_artwork_safely(
    *,
    config_path: str,
    db_path: Path,
    state: AutoArtworkRefreshState,
) -> None:
    try:
        _auto_refresh_spotify_artwork(
            config_path=config_path,
            db_path=db_path,
            state=state,
        )
    except Exception:
        logger.exception("Automatic Spotify artwork refresh failed")
    finally:
        with state.lock:
            state.running = False
            state.current = None
            state.finished_at = _now_iso()


def _auto_refresh_spotify_artwork(
    *,
    config_path: str,
    db_path: Path,
    state: AutoArtworkRefreshState,
) -> None:
    tracks_to_link = list_tracks_pending_spotify_linking(db_path)
    linked_tracks_without_art = list_tracks_missing_spotify_artwork(db_path)
    with state.lock:
        state.total = len(tracks_to_link) + len(linked_tracks_without_art)

    if not tracks_to_link and not linked_tracks_without_art:
        return

    sp = _get_spotify_linking_client(config_path)

    for track in tracks_to_link:
        path = str(track.path)
        with state.lock:
            state.current = f"{track.display_artist} - {track.display_title}"

        try:
            result = _run_with_timeout(
                lambda track=track: _auto_link_and_refresh_spotify_artwork(
                    sp,
                    db_path=db_path,
                    track=track,
                ),
                timeout_seconds=SPOTIFY_SWEEP_TRACK_TIMEOUT_SECONDS,
                thread_name="spotify-sweep-track",
            )
        except TimeoutError:
            logger.warning("Timed out automatic Spotify link/art refresh for %s", path)
            with state.lock:
                state.processed += 1
                state.failed += 1
            continue
        except Exception:
            logger.exception("Failed automatic Spotify link/art refresh for %s", path)
            with state.lock:
                state.processed += 1
                state.failed += 1
            continue

        with state.lock:
            state.processed += 1
            if result.linked:
                state.linked += 1
            if result.artwork_updated:
                state.artwork_updated += 1
            if result.no_results:
                state.no_results += 1
            if result.linked and not result.artwork_updated:
                state.failed += 1

    for track in linked_tracks_without_art:
        spotify_uri = track.spotify_uri
        if not spotify_uri:
            continue

        path = str(track.path)
        with state.lock:
            state.current = f"{track.display_artist} - {track.display_title}"

        try:
            result = _run_with_timeout(
                lambda track=track, spotify_uri=spotify_uri: (
                    _auto_refresh_linked_spotify_artwork(
                        sp,
                        db_path=db_path,
                        track=track,
                        spotify_uri=spotify_uri,
                    )
                ),
                timeout_seconds=SPOTIFY_SWEEP_TRACK_TIMEOUT_SECONDS,
                thread_name="spotify-sweep-track",
            )
        except TimeoutError:
            logger.warning("Timed out automatic artwork refresh for %s", path)
            with state.lock:
                state.processed += 1
                state.failed += 1
            continue
        except Exception:
            logger.exception("Failed automatic artwork refresh for %s", path)
            with state.lock:
                state.processed += 1
                state.failed += 1
            continue

        with state.lock:
            state.processed += 1
            if result.artwork_updated:
                state.artwork_updated += 1
            else:
                state.failed += 1


def _auto_link_and_refresh_spotify_artwork(
    sp: Any,
    *,
    db_path: Path,
    track: LocalTrack,
) -> AutoArtworkTrackResult:
    path = str(track.path)
    if not _track_file_exists(db_path, path=path):
        return AutoArtworkTrackResult()

    candidates = _run_spotify_lookup_with_timeout(
        lambda: _search_spotify_candidates(
            sp,
            _spotify_search_query(track),
            offset=0,
            limit=1,
        ),
        timeout_seconds=SPOTIFY_LINK_LOOKUP_TIMEOUT_SECONDS,
    )
    if not candidates:
        skip_track_spotify_link(db_path, path=path)
        return AutoArtworkTrackResult(no_results=True)

    candidate = candidates[0]
    set_track_spotify_uri(db_path, path=path, spotify_uri=candidate.uri)
    return AutoArtworkTrackResult(
        linked=True,
        artwork_updated=_replace_track_artwork_from_url(
            db_path,
            path=path,
            image_url=candidate.image_url,
        ),
    )


def _auto_refresh_linked_spotify_artwork(
    sp: Any,
    *,
    db_path: Path,
    track: LocalTrack,
    spotify_uri: str,
) -> AutoArtworkTrackResult:
    if not _track_file_exists(db_path, path=str(track.path)):
        return AutoArtworkTrackResult()

    image_url = _spotify_image_url_for_track_uri_with_client(sp, spotify_uri)
    return AutoArtworkTrackResult(
        artwork_updated=_replace_track_artwork_from_url(
            db_path,
            path=str(track.path),
            image_url=image_url,
        )
    )


def _auto_artwork_refresh_snapshot(
    state: AutoArtworkRefreshState,
) -> dict[str, object]:
    with state.lock:
        return {
            "running": state.running,
            "total": state.total,
            "processed": state.processed,
            "linked": state.linked,
            "artwork_updated": state.artwork_updated,
            "no_results": state.no_results,
            "failed": state.failed,
            "current": state.current,
            "started_at": state.started_at,
            "finished_at": state.finished_at,
        }


def _comment_cleanup_result_snapshot(
    *,
    result: CommentCleanupWriteResult,
    refreshed: bool,
    clear_all: bool = False,
    track: LocalTrack | None = None,
    path: str | None = None,
) -> dict[str, object]:
    track_path = str(track.path) if track is not None else str(path or "")
    label = (
        f"{track.display_artist} - {track.display_title}"
        if track is not None
        else Path(track_path).stem
    )
    return {
        "path": track_path,
        "label": label,
        "cleaned": result.cleaned,
        "clear_all": clear_all,
        "refreshed": refreshed,
        "before_comment": result.before_comment,
        "after_comment": result.after_comment,
    }


def _comment_cleanup_snapshot(
    state: CommentCleanupState,
) -> dict[str, object]:
    with state.lock:
        return {
            "running": state.running,
            "total": state.total,
            "processed": state.processed,
            "cleaned": state.cleaned,
            "skipped": state.skipped,
            "failed": state.failed,
            "current": state.current,
            "last_result": state.last_result,
            "started_at": state.started_at,
            "finished_at": state.finished_at,
        }


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _start_track_artwork_replacement(
    db_path: Path,
    *,
    path: str,
    image_url: str | None,
) -> bool:
    if not image_url:
        return False

    Thread(
        target=_replace_track_artwork_from_url_safely,
        args=(db_path,),
        kwargs={"path": path, "image_url": image_url},
        name="spotify-artwork-replacement",
        daemon=True,
    ).start()
    return True


def _replace_track_artwork_from_url_safely(
    db_path: Path,
    *,
    path: str,
    image_url: str,
) -> None:
    try:
        replaced = _replace_track_artwork_from_url(
            db_path,
            path=path,
            image_url=image_url,
        )
    except Exception:
        logger.exception("Failed to replace artwork for %s", path)
        return

    if not replaced:
        logger.warning("Artwork replacement did not update %s", path)


def _start_track_artwork_replacement_from_spotify_uri(
    *,
    config_path: str,
    db_path: Path,
    path: str,
    spotify_uri: str,
) -> bool:
    if not spotify_uri:
        return False

    Thread(
        target=_replace_track_artwork_from_spotify_uri_safely,
        kwargs={
            "config_path": config_path,
            "db_path": db_path,
            "path": path,
            "spotify_uri": spotify_uri,
        },
        name="spotify-artwork-uri-replacement",
        daemon=True,
    ).start()
    return True


def _replace_track_artwork_from_spotify_uri_safely(
    *,
    config_path: str,
    db_path: Path,
    path: str,
    spotify_uri: str,
) -> None:
    try:
        image_url = _spotify_image_url_for_track_uri(config_path, spotify_uri)
        replaced = _replace_track_artwork_from_url(
            db_path,
            path=path,
            image_url=image_url,
        )
    except Exception:
        logger.exception("Failed to replace artwork for %s from %s", path, spotify_uri)
        return

    if not replaced:
        logger.warning(
            "Artwork replacement did not update %s from %s", path, spotify_uri
        )


def _start_track_artwork_replacement_from_soundcloud_url(
    *,
    db_path: Path,
    path: str,
    soundcloud_url: str,
) -> bool:
    if not soundcloud_url:
        return False

    Thread(
        target=_replace_track_artwork_from_soundcloud_url_safely,
        kwargs={
            "db_path": db_path,
            "path": path,
            "soundcloud_url": soundcloud_url,
        },
        name="soundcloud-artwork-url-replacement",
        daemon=True,
    ).start()
    return True


def _replace_track_artwork_from_soundcloud_url_safely(
    *,
    db_path: Path,
    path: str,
    soundcloud_url: str,
) -> None:
    try:
        image_url = _soundcloud_artwork_url_for_track_url(soundcloud_url)
        replaced = _replace_track_artwork_from_url(
            db_path,
            path=path,
            image_url=image_url,
            normalize_for_rekordbox=True,
        )
    except Exception:
        logger.exception(
            "Failed to replace artwork for %s from %s", path, soundcloud_url
        )
        return

    if not replaced:
        logger.warning(
            "Artwork replacement did not update %s from %s", path, soundcloud_url
        )


def _soundcloud_artwork_url_for_track_url(soundcloud_url: str) -> str | None:
    endpoint = "https://soundcloud.com/oembed?" + urlencode(
        {"format": "json", "url": soundcloud_url}
    )
    request = UrlRequest(
        endpoint,
        headers={"User-Agent": "crate-digger/1.0"},
    )
    with urlopen(request, timeout=SPOTIFY_LINK_LOOKUP_TIMEOUT_SECONDS) as response:
        payload = response.read(512 * 1024)

    try:
        data = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None

    if not isinstance(data, dict):
        return None
    thumbnail_url = data.get("thumbnail_url")
    if not isinstance(thumbnail_url, str):
        return None
    parsed = urlsplit(thumbnail_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return thumbnail_url


def _spotify_image_url_for_track_uri(config_path: str, spotify_uri: str) -> str | None:
    return _spotify_image_url_for_track_uri_with_client(
        _get_spotify_linking_client(config_path),
        spotify_uri,
    )


def _get_spotify_linking_client(config_path: str) -> Any:
    spotify_config = get_settings(config_path)["spotify"]
    return get_spotify_client(
        " ".join(spotify_config["scopes"]),
        allow_browser_auth=False,
    )


def _spotify_image_url_for_track_uri_with_client(
    sp: Any,
    spotify_uri: str,
) -> str | None:
    track = _run_with_timeout(
        lambda: sp.track(spotify_uri),
        timeout_seconds=SPOTIFY_LINK_LOOKUP_TIMEOUT_SECONDS,
        thread_name="spotify-track-artwork-lookup",
    )
    album = track.get("album") if isinstance(track, dict) else None
    if not isinstance(album, dict):
        return None
    return _spotify_album_image_url(album)


def _replace_track_artwork_from_url(
    db_path: Path,
    *,
    path: str,
    image_url: str | None,
    normalize_for_rekordbox: bool = False,
) -> bool:
    if not _track_file_exists(db_path, path=path):
        return False

    if not image_url:
        return False

    artwork = _download_spotify_artwork(image_url)
    if artwork is None:
        return False

    mime, data = artwork
    if normalize_for_rekordbox:
        mime, data = _normalize_artwork_for_rekordbox(mime=mime, data=data)
    if overwrite_embedded_artwork(Path(path), mime=mime, data=data):
        return refresh_track_metadata(db_path, path=path)
    return False


def _normalize_artwork_for_rekordbox(
    *,
    mime: str,
    data: bytes,
) -> tuple[str, bytes]:
    ffmpeg_path = shutil.which("ffmpeg")
    if ffmpeg_path is None:
        return mime, data

    input_suffix = ".png" if mime == "image/png" else ".jpg"
    with TemporaryDirectory(prefix="crate-digger-artwork-normalize-") as temp_dir:
        input_path = Path(temp_dir) / f"input{input_suffix}"
        output_path = Path(temp_dir) / "cover.jpg"
        try:
            input_path.write_bytes(data)
            result = subprocess.run(
                [
                    ffmpeg_path,
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-i",
                    str(input_path),
                    "-frames:v",
                    "1",
                    "-vf",
                    (
                        "scale=1000:1000:force_original_aspect_ratio=decrease,"
                        "pad=1000:1000:(ow-iw)/2:(oh-ih)/2:color=white,"
                        "format=yuvj420p"
                    ),
                    "-pix_fmt",
                    "yuvj420p",
                    "-q:v",
                    "2",
                    str(output_path),
                ],
                capture_output=True,
                check=False,
                text=True,
                timeout=SPOTIFY_ARTWORK_DOWNLOAD_TIMEOUT_SECONDS,
            )
            if result.returncode != 0:
                logger.warning(
                    "Artwork JPEG normalization failed: %s",
                    result.stderr.strip()[:180],
                )
                return mime, data
            normalized = output_path.read_bytes()
        except (OSError, subprocess.TimeoutExpired):
            return mime, data

    if len(normalized) > MAX_ARTWORK_DOWNLOAD_BYTES:
        return mime, data
    if _infer_image_mime(normalized) != "image/jpeg":
        return mime, data
    return "image/jpeg", normalized


def _download_spotify_artwork(url: str) -> tuple[str, bytes] | None:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None

    bash_path = shutil.which("bash")
    script_path = Path(__file__).with_name("download_artwork.sh")
    if bash_path is None or shutil.which("curl") is None or not script_path.is_file():
        return None

    with TemporaryDirectory(prefix="crate-digger-artwork-") as temp_dir:
        output_path = Path(temp_dir) / "cover"
        try:
            response = subprocess.run(
                [
                    bash_path,
                    str(script_path),
                    url,
                    str(output_path),
                    str(SPOTIFY_ARTWORK_DOWNLOAD_TIMEOUT_SECONDS),
                    str(MAX_ARTWORK_DOWNLOAD_BYTES),
                ],
                capture_output=True,
                check=False,
                text=True,
            )
        except OSError:
            return None

        if response.returncode != 0:
            logger.warning(
                "Spotify artwork curl helper failed for %s: %s",
                url,
                response.stderr.strip()[:180],
            )
            return None

        try:
            data = output_path.read_bytes()
        except OSError:
            return None

    mime = response.stdout.strip().splitlines()[-1] if response.stdout.strip() else ""
    return _validated_downloaded_artwork(mime, data)


def _validated_downloaded_artwork(
    mime: str,
    data: bytes,
) -> tuple[str, bytes] | None:
    if len(data) > MAX_ARTWORK_DOWNLOAD_BYTES:
        return None
    inferred_mime = _infer_image_mime(data)
    if inferred_mime is None:
        return None
    if mime not in {"image/jpeg", "image/png"}:
        mime = inferred_mime
    return mime, data


def _infer_image_mime(data: bytes) -> str | None:
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    return None


def _safe_return_to(value: str | None) -> str:
    if not value:
        return "/"
    parsed = urlsplit(value)
    if (
        parsed.scheme
        or parsed.netloc
        or not value.startswith("/")
        or value.startswith("//")
    ):
        return "/"
    return value


def _with_art_refresh(return_to: str, *, path: str | None = None) -> str:
    parsed = urlsplit(return_to)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query["art_refresh"] = "1"
    if path:
        query["art_path"] = path
    return urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path or "/",
            urlencode(query),
            parsed.fragment,
        )
    )


def _spotify_search_query(track: LocalTrack) -> str:
    return f"{track.display_artist} - {track.display_title}"


async def _search_spotify_candidates_for_track(
    *,
    config_path: str,
    track: LocalTrack,
    offset: int,
    limit: int,
) -> tuple[list[SpotifyCandidate], str | None]:
    query = _spotify_search_query(track)
    try:
        candidates = await run_in_threadpool(
            _search_spotify_candidates_from_config_with_timeout,
            config_path,
            query,
            offset=offset,
            limit=limit,
            timeout_seconds=SPOTIFY_LINK_LOOKUP_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        return [], "Spotify lookup timed out. Try again in a moment."
    except Exception as exc:
        return [], f"Spotify lookup failed: {_short_error(exc)}"
    return candidates, None


def _search_spotify_candidates_from_config_with_timeout(
    config_path: str,
    query: str,
    *,
    offset: int,
    limit: int,
    timeout_seconds: float,
) -> list[SpotifyCandidate]:
    return _run_spotify_lookup_with_timeout(
        lambda: _search_spotify_candidates_from_config(
            config_path,
            query,
            offset=offset,
            limit=limit,
        ),
        timeout_seconds=timeout_seconds,
    )


def _search_spotify_candidates_from_config(
    config_path: str,
    query: str,
    *,
    offset: int,
    limit: int,
) -> list[SpotifyCandidate]:
    spotify_config = get_settings(config_path)["spotify"]
    sp = get_spotify_client(
        " ".join(spotify_config["scopes"]),
        allow_browser_auth=False,
    )
    return _search_spotify_candidates(
        sp,
        query,
        offset=offset,
        limit=limit,
    )


def _run_spotify_lookup_with_timeout(
    callback: Callable[[], list[SpotifyCandidate]],
    *,
    timeout_seconds: float,
) -> list[SpotifyCandidate]:
    return _run_with_timeout(
        callback,
        timeout_seconds=timeout_seconds,
        thread_name="spotify-link-lookup",
    )


def _run_with_timeout(
    callback: Callable[[], T],
    *,
    timeout_seconds: float,
    thread_name: str,
) -> T:
    results: Queue[tuple[str, T | BaseException]] = Queue(maxsize=1)

    def run() -> None:
        try:
            results.put(("result", callback()))
        except BaseException as exc:
            results.put(("error", exc))

    Thread(target=run, name=thread_name, daemon=True).start()

    try:
        kind, value = results.get(timeout=timeout_seconds)
    except Empty as exc:
        raise TimeoutError(f"{thread_name} exceeded {timeout_seconds:g}s") from exc

    if kind == "error":
        assert isinstance(value, BaseException)
        raise value
    return cast(T, value)


def _short_error(error: Exception) -> str:
    message = str(error).strip()
    if not message:
        message = error.__class__.__name__
    return message[:180]


def _search_spotify_candidates(
    client: Any,
    query: str,
    *,
    offset: int,
    limit: int,
) -> list[SpotifyCandidate]:
    response = client.search(q=query, type="track", limit=limit, offset=offset)
    items = response.get("tracks", {}).get("items", [])
    candidates: list[SpotifyCandidate] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        uri = item.get("uri")
        name = item.get("name")
        if not isinstance(uri, str) or not isinstance(name, str):
            continue

        artists = ", ".join(
            artist["name"]
            for artist in item.get("artists", [])
            if isinstance(artist, dict) and isinstance(artist.get("name"), str)
        )
        album = item.get("album") if isinstance(item.get("album"), dict) else {}
        raw_album_name = album.get("name")
        album_name = raw_album_name if isinstance(raw_album_name, str) else ""
        image_url = _spotify_album_image_url(album)
        external_urls = item.get("external_urls")
        external_url = None
        if isinstance(external_urls, dict) and isinstance(
            external_urls.get("spotify"), str
        ):
            external_url = external_urls["spotify"]

        candidates.append(
            SpotifyCandidate(
                uri=uri,
                name=name,
                artists=artists,
                album=album_name,
                image_url=image_url,
                external_url=external_url,
            )
        )

    return candidates


def _spotify_album_image_url(album: dict[str, Any]) -> str | None:
    images = album.get("images")
    if not isinstance(images, list):
        return None

    sized_images: list[tuple[int, str]] = []
    fallback_url: str | None = None
    for image in images:
        if not isinstance(image, dict) or not isinstance(image.get("url"), str):
            continue

        url = cast(str, image["url"])
        if fallback_url is None:
            fallback_url = url

        width = image.get("width")
        if isinstance(width, int):
            sized_images.append((width, url))

    if sized_images:
        large_enough = [(width, url) for width, url in sized_images if width >= 300]
        if large_enough:
            return min(large_enough, key=lambda item: item[0])[1]
        return max(sized_images, key=lambda item: item[0])[1]

    return fallback_url


def _build_collection_view(
    db_path: Path,
    *,
    q: str,
    audio_format: str,
    metadata: str,
    spotify: str,
    sort: str,
    direction: str,
    page: int,
    page_size: int,
) -> CollectionView:
    query = _normalize_query(
        q=q,
        audio_format=audio_format,
        metadata=metadata,
        spotify=spotify,
        sort=sort,
        direction=direction,
        page=page,
        page_size=page_size,
    )
    result = query_tracks(
        db_path,
        q=query.q,
        audio_format=query.audio_format,
        metadata=query.metadata,
        spotify=query.spotify,
        sort=query.sort,
        direction=query.direction,
        page=query.page,
        page_size=query.page_size,
    )
    query = CollectionQuery(
        q=query.q,
        audio_format=query.audio_format,
        metadata=query.metadata,
        spotify=query.spotify,
        sort=query.sort,
        direction=query.direction,
        page=result.page,
        page_size=query.page_size,
    )

    return CollectionView(
        query=query,
        tracks=result.tracks,
        filtered_count=result.filtered_count,
        total_count=result.total_count,
        total_pages=result.total_pages,
        formats=result.formats,
    )


def _normalize_query(
    *,
    q: str,
    audio_format: str,
    metadata: str,
    spotify: str,
    sort: str,
    direction: str,
    page: int,
    page_size: int,
) -> CollectionQuery:
    normalized_metadata: MetadataFilter = "all"
    if metadata in METADATA_FILTER_LABELS:
        normalized_metadata = cast(MetadataFilter, metadata)

    normalized_spotify: SpotifyFilter = "all"
    if spotify in SPOTIFY_FILTER_LABELS:
        normalized_spotify = cast(SpotifyFilter, spotify)

    normalized_sort: SortKey = "title"
    if sort in SORT_LABELS:
        normalized_sort = cast(SortKey, sort)

    normalized_direction: SortDirection = "asc"
    if direction == "desc":
        normalized_direction = "desc"

    return CollectionQuery(
        q=q.strip(),
        audio_format=audio_format.strip().upper(),
        metadata=normalized_metadata,
        spotify=normalized_spotify,
        sort=normalized_sort,
        direction=normalized_direction,
        page=max(1, page),
        page_size=DEFAULT_PAGE_SIZE,
    )


def _render_index(
    view: CollectionView,
    music_dirs: list[str],
    *,
    genre_pending: int = 0,
    auto_artwork_status: dict[str, object] | None = None,
    comment_cleanup_status: dict[str, object] | None = None,
) -> str:
    showing_start = (
        0
        if view.filtered_count == 0
        else ((view.query.page - 1) * view.query.page_size + 1)
    )
    showing_end = min(view.filtered_count, view.query.page * view.query.page_size)
    sort_keys: tuple[SortKey, ...] = (
        "title",
        "artist",
        "album",
        "genre",
        "release_date",
        "file_created_at",
        "format",
        "bitrate",
        "duration",
    )
    return render_template(
        "dashboard.html",
        view=view,
        music_dirs_count=len(music_dirs),
        genre_pending=genre_pending,
        showing_start=showing_start,
        showing_end=showing_end,
        controls=_render_controls(view),
        auto_artwork=_render_auto_artwork_status(auto_artwork_status),
        comment_cleanup=_render_comment_cleanup_status(comment_cleanup_status),
        pagination=_render_pagination(view),
        return_to=_url_for(view),
        sort_links={key: _sort_link(view, key) for key in sort_keys},
        rows="\n".join(_render_track_row(track, view) for track in view.tracks),
    )


def _render_auto_artwork_status(status: dict[str, object] | None) -> str:
    if not status:
        return '<div id="auto-artwork-status" class="auto-artwork" hidden></div>'

    running = bool(status.get("running"))
    total = _status_int(status.get("total"))
    processed = _status_int(status.get("processed"))
    linked = _status_int(status.get("linked"))
    artwork_updated = _status_int(status.get("artwork_updated"))
    no_results = _status_int(status.get("no_results"))
    failed = _status_int(status.get("failed"))
    current = status.get("current")
    hidden = "hidden" if not running and total == 0 else ""
    if running:
        current_html = (
            f" · Current: <strong>{escape(str(current))}</strong>" if current else ""
        )
        text = (
            "<strong>Spotify sweep running</strong>"
            f" · {processed}/{total} processed"
            f" · {linked} linked"
            f" · {artwork_updated} covers"
            f" · {no_results} no results"
            f" · {failed} failed"
            f"{current_html}"
        )
    else:
        text = (
            "<strong>Spotify sweep complete</strong>"
            f" · {processed}/{total} processed"
            f" · {linked} linked"
            f" · {artwork_updated} covers"
            f" · {no_results} no results"
            f" · {failed} failed"
        )

    return f'<div id="auto-artwork-status" class="auto-artwork" {hidden}>{text}</div>'


def _render_comment_cleanup_status(status: dict[str, object] | None) -> str:
    if not status:
        return '<div id="comment-cleanup-status" class="auto-artwork" hidden></div>'

    running = bool(status.get("running"))
    total = _status_int(status.get("total"))
    processed = _status_int(status.get("processed"))
    cleaned = _status_int(status.get("cleaned"))
    skipped = _status_int(status.get("skipped"))
    failed = _status_int(status.get("failed"))
    current = status.get("current")
    result = status.get("last_result")
    hidden = "hidden" if not running and total == 0 else ""
    result_html = (
        _render_comment_cleanup_result(status=cast(dict[str, object], result))
        if isinstance(result, dict)
        else ""
    )
    if running:
        current_html = (
            f" · Current: <strong>{escape(str(current))}</strong>" if current else ""
        )
        text = (
            "<strong>Comment cleanup sweep running</strong>"
            f" · {processed}/{total} processed"
            f" · {cleaned} cleaned"
            f" · {skipped} skipped"
            f" · {failed} failed"
            f"{current_html}"
            f"{result_html}"
        )
    else:
        text = (
            "<strong>Comment cleanup sweep complete</strong>"
            f" · {processed}/{total} processed"
            f" · {cleaned} cleaned"
            f" · {skipped} skipped"
            f" · {failed} failed"
            f"{result_html}"
        )

    return (
        f'<div id="comment-cleanup-status" class="auto-artwork" {hidden}>{text}</div>'
    )


def _render_comment_cleanup_result(*, status: dict[str, object]) -> str:
    label = escape(str(status.get("label") or ""))
    before = str(status.get("before_comment") or "empty")
    after = str(status.get("after_comment") or "empty")
    if not bool(status.get("cleaned")):
        return f' · <span class="status-result">{label}: unchanged</span>'
    if bool(status.get("clear_all")) or not status.get("after_comment"):
        return (
            f' · <span class="status-result">{label}: '
            f"removed comment (was {escape(before)})</span>"
        )
    return (
        f' · <span class="status-result">{label}: '
        f"kept <strong>{escape(after)}</strong> "
        f"(was {escape(before)})</span>"
    )


def _status_int(value: object) -> int:
    return value if isinstance(value, int) else 0


def _render_controls(view: CollectionView) -> str:
    return render_template(
        "partials/collection_controls.html",
        view=view,
        spotify_filters=SPOTIFY_FILTER_LABELS,
    )


def _render_pagination(view: CollectionView) -> str:
    return render_template(
        "partials/collection_pagination.html",
        view=view,
        previous_link=_page_link(view, view.query.page - 1, "Previous"),
        next_link=_page_link(view, view.query.page + 1, "Next"),
    )


def _render_spotify_link_idle(music_dirs: list[str]) -> str:
    return render_template(
        "link_page.html",
        title="Spotify Linker",
        summary=f"{len(music_dirs)} folders",
        state="idle",
    )


def _render_spotify_link_done(music_dirs: list[str]) -> str:
    return render_template(
        "link_page.html",
        title="Spotify Linker",
        summary=f"{len(music_dirs)} folders",
        state="done",
    )


def _render_spotify_link_page(
    *,
    track: LocalTrack,
    candidates: list[SpotifyCandidate],
    offset: int,
    music_dirs: list[str],
    return_to: str,
    lookup_error: str | None,
) -> str:
    content = _render_spotify_link_content(
        track=track,
        candidates=candidates,
        offset=offset,
        partial=False,
        return_to=return_to,
        lookup_error=lookup_error,
    )
    return render_template(
        "link_page.html",
        title="Spotify Linker",
        summary=f"{len(music_dirs)} folders",
        state="active",
        content=content,
    )


def _render_spotify_link_content(
    *,
    track: LocalTrack,
    candidates: list[SpotifyCandidate],
    offset: int,
    partial: bool,
    return_to: str,
    lookup_error: str | None,
) -> str:
    next_offset = offset + SPOTIFY_LINK_LIMIT
    previous_offset = max(0, offset - SPOTIFY_LINK_LIMIT)
    return render_template(
        "partials/link_content.html",
        track=track,
        partial=partial,
        return_to=return_to,
        cover=_render_cover(track),
        short_path=_short_path(track.path),
        search_query=_spotify_search_query(track),
        lookup_error=lookup_error,
        candidate_rows="\n".join(
            _render_spotify_candidate(track, candidate, return_to)
            for candidate in candidates
        ),
        previous_href=_spotify_link_href(
            track=track,
            offset=previous_offset,
            return_to=return_to,
            partial=False,
        ),
        next_href=_spotify_link_href(
            track=track,
            offset=next_offset,
            return_to=return_to,
            partial=False,
        ),
        previous_modal_url=_spotify_link_href(
            track=track,
            offset=previous_offset,
            return_to=return_to,
            partial=True,
        ),
        next_modal_url=_spotify_link_href(
            track=track,
            offset=next_offset,
            return_to=return_to,
            partial=True,
        ),
    )


def _render_spotify_candidate(
    track: LocalTrack,
    candidate: SpotifyCandidate,
    return_to: str,
) -> str:
    return render_template(
        "partials/link_candidate.html",
        track=track,
        candidate=candidate,
        return_to=return_to,
        cover=_render_candidate_cover(candidate),
    )


def _spotify_link_href(
    *,
    track: LocalTrack,
    offset: int,
    return_to: str,
    partial: bool,
) -> str:
    params: dict[str, object] = {
        "path": str(track.path),
        "offset": offset,
        "return_to": return_to,
    }
    if partial:
        params["partial"] = 1
    return f"/spotify-link?{urlencode(params)}"


def _render_candidate_cover(candidate: SpotifyCandidate) -> str:
    if candidate.image_url is None:
        return '<span class="candidate-cover"></span>'
    return f'<img class="candidate-cover" src="{escape(candidate.image_url)}" alt="">'


def _sort_link(view: CollectionView, sort: SortKey) -> str:
    direction = "asc"
    marker = ""
    if view.query.sort == sort:
        direction = "desc" if view.query.direction == "asc" else "asc"
        marker = " ↑" if view.query.direction == "asc" else " ↓"

    href = _url_for(view, sort=sort, direction=direction, page=1)
    return f'<a href="{escape(href)}">{escape(SORT_LABELS[sort])}{marker}</a>'


def _page_link(view: CollectionView, page: int, label: str) -> str:
    if page < 1 or page > view.total_pages:
        return f'<span class="button" aria-disabled="true">{escape(label)}</span>'
    return f'<a class="button" href="{escape(_url_for(view, page=page))}">{escape(label)}</a>'


def _url_for(view: CollectionView, **overrides: object) -> str:
    params = {
        "q": view.query.q,
        "format": view.query.audio_format,
        "metadata": view.query.metadata,
        "spotify": view.query.spotify,
        "sort": view.query.sort,
        "direction": view.query.direction,
        "page": view.query.page,
    }
    params.update(overrides)
    filtered_params = {
        key: value
        for key, value in params.items()
        if value not in ("", None) and not (key == "page" and value == 1)
    }
    return f"/?{urlencode(filtered_params)}" if filtered_params else "/"


def _render_track_row(track: LocalTrack, view: CollectionView) -> str:
    return_to = _url_for(view)
    return render_template(
        "partials/track_row.html",
        track=track,
        cover=_render_cover(track),
        short_path=_short_path(track.path),
        comment_action=_render_comment_track_action(track, return_to=return_to),
        spotify_action=_render_spotify_action(track, return_to=return_to),
        created_at=_format_date(track.file_created_at),
        bitrate=_format_bitrate(track.bitrate),
        duration=_format_duration(track.duration_seconds),
    )


def _render_comment_track_action(track: LocalTrack, *, return_to: str) -> str:
    return render_template(
        "partials/track_comment_actions.html",
        track=track,
        return_to=return_to,
    )


def _render_spotify_action(track: LocalTrack, *, return_to: str) -> str:
    return render_template(
        "partials/track_source_actions.html",
        track=track,
        return_to=return_to,
        external_url=(
            _spotify_external_url_from_uri(track.spotify_uri)
            if track.spotify_uri
            else None
        ),
        find_href=_spotify_link_href(
            track=track,
            offset=0,
            return_to=return_to,
            partial=False,
        ),
        modal_url=_spotify_link_href(
            track=track,
            offset=0,
            return_to=return_to,
            partial=True,
        ),
    )


def _spotify_external_url_from_uri(uri: str) -> str | None:
    prefix = "spotify:track:"
    if not uri.startswith(prefix):
        return None
    return f"https://open.spotify.com/track/{uri.removeprefix(prefix)}"


def _spotify_uri_from_input(value: str) -> str | None:
    value = value.strip()
    if not value:
        return None

    uri_prefix = "spotify:track:"
    if value.startswith(uri_prefix):
        track_id = value.removeprefix(uri_prefix).split("?", 1)[0].strip()
        return _spotify_track_uri(track_id)

    parts = urlsplit(value)
    if parts.scheme not in {"http", "https"}:
        return None
    if parts.netloc.lower() != "open.spotify.com":
        return None

    path_parts = [part for part in parts.path.split("/") if part]
    if len(path_parts) >= 2 and path_parts[0] == "track":
        return _spotify_track_uri(path_parts[1])
    if len(path_parts) >= 3 and path_parts[1] == "track":
        return _spotify_track_uri(path_parts[2])
    return None


def _soundcloud_url_from_input(value: str) -> str | None:
    value = value.strip()
    if not value:
        return None

    parts = urlsplit(value)
    if parts.scheme not in {"http", "https"}:
        return None
    hostname = parts.hostname.lower() if parts.hostname else ""
    if hostname != "soundcloud.com" and not hostname.endswith(".soundcloud.com"):
        return None
    if not parts.path.strip("/"):
        return None
    return urlunsplit(("https", hostname, parts.path, "", ""))


def _spotify_track_uri(track_id: str) -> str | None:
    if not track_id or len(track_id) > 64 or not track_id.isalnum():
        return None
    return f"spotify:track:{track_id}"


def _render_cover(track: LocalTrack) -> str:
    data_path = f'data-cover-path="{escape(str(track.path))}"'
    if track.artwork_mime is None:
        return f'<span class="cover cover-placeholder" {data_path}></span>'
    params = {"path": str(track.path)}
    if track.indexed_at:
        params["v"] = track.indexed_at
    src = f"/art?{urlencode(params)}"
    return f'<img class="cover" src="{escape(src)}" alt="" {data_path}>'


def _short_path(path: Path) -> str:
    parts = path.parts
    if len(parts) <= 3:
        return str(path)
    return str(Path("...", *parts[-3:]))


def _format_bitrate(bitrate: int | None) -> str:
    if not bitrate:
        return ""
    return f"{round(bitrate / 1000)} kbps"


def _format_date(value: str | None) -> str:
    if not value:
        return ""
    return value[:10]


def _format_duration(duration_seconds: float | None) -> str:
    if duration_seconds is None:
        return ""
    minutes, seconds = divmod(round(duration_seconds), 60)
    return f"{minutes}:{seconds:02d}"
