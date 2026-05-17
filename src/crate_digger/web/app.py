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

from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

from crate_digger.collection.index import (
    DEFAULT_COLLECTION_DB_PATH,
    get_track_artwork,
    get_track_for_spotify_linking,
    list_tracks_missing_spotify_artwork,
    list_tracks_pending_spotify_linking,
    query_tracks,
    refresh_collection_index,
    refresh_track_metadata,
    set_track_spotify_uri,
    skip_track_spotify_link,
)
from crate_digger.collection.models import LocalTrack
from crate_digger.collection.scanner import overwrite_embedded_artwork
from crate_digger.utils.config import get_settings
from crate_digger.utils.logging import get_logger
from crate_digger.utils.spotify import get_spotify_client

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
        _start_auto_artwork_refresh(
            config_path=config_path,
            db_path=db_path,
            state=app.state.auto_artwork_refresh_state,
        )
        yield

    app = FastAPI(title="Crate Digger Dashboard", lifespan=lifespan)

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
                auto_artwork_status=_auto_artwork_refresh_snapshot(
                    _get_auto_artwork_refresh_state(app)
                ),
            )
        )

    @app.get("/api/spotify-artwork-refresh")
    def spotify_artwork_refresh_status() -> dict[str, object]:
        return _auto_artwork_refresh_snapshot(_get_auto_artwork_refresh_state(app))

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
        set_track_spotify_uri(db_path, path=path, spotify_uri=spotify_uri)
        return_to = _safe_return_to(form.get("return_to"))
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
        if track is None:
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

    @app.post("/spotify-link/skip")
    async def skip_spotify_track(request: Request) -> RedirectResponse:
        form = _parse_urlencoded_form(await request.body())
        path = form["path"]
        skip_track_spotify_link(db_path, path=path)
        return RedirectResponse(_safe_return_to(form.get("return_to")), status_code=303)

    @app.post("/spotify-link/refresh-art")
    async def refresh_spotify_art(request: Request) -> RedirectResponse:
        form = _parse_urlencoded_form(await request.body())
        path = form["path"]
        return_to = _safe_return_to(form.get("return_to"))
        track = get_track_for_spotify_linking(db_path, path=path)
        if track is None or not track.spotify_uri:
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
        "spotify_link_skipped_at": track.spotify_link_skipped_at,
        "indexed_at": track.indexed_at,
    }


def _parse_urlencoded_form(body: bytes) -> dict[str, str]:
    parsed = parse_qs(body.decode("utf-8"), keep_blank_values=True)
    return {key: values[0] for key, values in parsed.items() if values}


def _get_auto_artwork_refresh_state(app: FastAPI) -> AutoArtworkRefreshState:
    state = getattr(app.state, "auto_artwork_refresh_state", None)
    if isinstance(state, AutoArtworkRefreshState):
        return state

    state = AutoArtworkRefreshState(finished_at=_now_iso())
    app.state.auto_artwork_refresh_state = state
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
) -> bool:
    if not image_url:
        return False

    artwork = _download_spotify_artwork(image_url)
    if artwork is None:
        return False

    mime, data = artwork
    if overwrite_embedded_artwork(Path(path), mime=mime, data=data):
        return refresh_track_metadata(db_path, path=path)
    return False


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
    auto_artwork_status: dict[str, object] | None = None,
) -> str:
    rows = "\n".join(_render_track_row(track, view) for track in view.tracks)
    empty = ""
    if not music_dirs:
        empty = '<p class="empty">No collection directories configured.</p>'
    elif not view.tracks:
        empty = '<p class="empty">No tracks match the current view.</p>'

    showing_start = (
        0
        if view.filtered_count == 0
        else ((view.query.page - 1) * view.query.page_size + 1)
    )
    showing_end = min(view.filtered_count, view.query.page * view.query.page_size)

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Crate Digger</title>
  <style>
    :root {{
      color-scheme: light;
      --ink: #191b22;
      --muted: #636978;
      --line: #d8dce5;
      --panel: #f6f7f9;
      --accent: #0f766e;
      --accent-2: #b45309;
      --bg: #ffffff;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--ink);
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      line-height: 1.45;
    }}
    header {{
      border-bottom: 1px solid var(--line);
      background: var(--panel);
    }}
    .wrap {{
      width: min(1180px, calc(100vw - 32px));
      margin: 0 auto;
    }}
    .topbar {{
      display: flex;
      align-items: end;
      justify-content: space-between;
      gap: 24px;
      padding: 16px 0 14px;
    }}
    h1 {{
      margin: 0;
      font-size: 28px;
      font-weight: 750;
      letter-spacing: 0;
    }}
    .summary {{
      display: flex;
      gap: 12px;
      flex-wrap: wrap;
      color: var(--muted);
      font-size: 14px;
    }}
    .metric {{
      border-left: 3px solid var(--accent);
      padding-left: 10px;
      min-width: 84px;
    }}
    .metric strong {{
      display: block;
      color: var(--ink);
      font-size: 18px;
      line-height: 1.1;
    }}
    main {{
      padding: 12px 0 28px;
    }}
    .controls {{
      display: grid;
      grid-template-columns: minmax(220px, 1fr) repeat(3, minmax(130px, auto)) auto;
      gap: 10px;
      align-items: end;
      margin-bottom: 10px;
    }}
    label {{
      display: grid;
      gap: 5px;
      color: var(--muted);
      font-size: 12px;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0;
    }}
    input, select, button, .button {{
      height: 36px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #fff;
      color: var(--ink);
      font: inherit;
      font-size: 14px;
    }}
    input, select {{
      width: 100%;
      padding: 0 10px;
    }}
    button, .button {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      padding: 0 12px;
      text-decoration: none;
      font-weight: 700;
      cursor: pointer;
    }}
    button {{
      background: var(--accent);
      color: #fff;
      border-color: var(--accent);
    }}
    .button {{
      color: var(--muted);
    }}
    .viewbar {{
      display: grid;
      grid-template-columns: 1fr auto auto;
      align-items: center;
      gap: 10px;
      margin-bottom: 8px;
      color: var(--muted);
      font-size: 13px;
    }}
    .viewbar form {{
      margin: 0;
    }}
    .viewbar button {{
      height: 30px;
      background: #fff;
      color: var(--muted);
      border-color: var(--line);
    }}
    .navlink {{
      height: 30px;
      color: var(--muted);
    }}
    .spotify-cell {{
      width: 150px;
    }}
    .spotify-actions {{
      display: flex;
      align-items: center;
      gap: 6px;
    }}
    .spotify-actions form {{
      margin: 0;
    }}
    .spotify-action {{
      height: 28px;
      padding: 0 10px;
      color: var(--accent);
      font-size: 13px;
    }}
    button.spotify-action {{
      background: var(--accent);
      color: #fff;
      border-color: var(--accent);
    }}
    .spotify-linked {{
      color: var(--muted);
      font-size: 13px;
      font-weight: 700;
      text-decoration: none;
    }}
    dialog {{
      width: min(860px, calc(100vw - 32px));
      max-height: min(760px, calc(100vh - 32px));
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 0;
      color: var(--ink);
      background: #fff;
    }}
    dialog::backdrop {{
      background: rgba(25, 27, 34, 0.35);
    }}
    .modal-header {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      padding: 10px 14px;
      border-bottom: 1px solid var(--line);
      background: var(--panel);
    }}
    .modal-close {{
      height: 30px;
      background: #fff;
      color: var(--muted);
      border-color: var(--line);
    }}
    .spotify-modal-body {{
      padding: 14px;
      overflow: auto;
      max-height: calc(100vh - 112px);
    }}
    .linkbar {{
      display: flex;
      gap: 8px;
      justify-content: flex-end;
      margin-bottom: 12px;
    }}
    .link-layout {{
      display: grid;
      grid-template-columns: 280px 1fr;
      gap: 18px;
      align-items: start;
    }}
    .local-track {{
      display: grid;
      grid-template-columns: 72px 1fr;
      gap: 12px;
      padding: 12px 0;
      border-top: 1px solid var(--line);
      border-bottom: 1px solid var(--line);
    }}
    .local-track .cover {{
      width: 72px;
      height: 72px;
    }}
    .candidate-list {{
      display: grid;
      gap: 8px;
    }}
    .query {{
      color: var(--muted);
      font-size: 12px;
    }}
    .candidate {{
      display: grid;
      grid-template-columns: 54px 1fr auto;
      gap: 12px;
      align-items: center;
      padding: 8px 0;
      border-bottom: 1px solid var(--line);
    }}
    .candidate-cover {{
      width: 44px;
      height: 44px;
      border: 1px solid var(--line);
      border-radius: 6px;
      object-fit: cover;
      background: var(--panel);
    }}
    .candidate p {{
      margin: 0;
      color: var(--muted);
      font-size: 13px;
    }}
    .candidate-actions {{
      display: flex;
      align-items: center;
      gap: 8px;
    }}
    .candidate-open {{
      color: var(--accent);
      font-size: 13px;
      font-weight: 700;
      text-decoration: none;
    }}
    .table {{
      width: 100%;
      border-collapse: collapse;
      table-layout: fixed;
      border-top: 1px solid var(--line);
    }}
    .cover-cell {{
      width: 52px;
    }}
    .cover {{
      display: block;
      width: 36px;
      height: 36px;
      border: 1px solid var(--line);
      border-radius: 6px;
      object-fit: cover;
      background: var(--panel);
    }}
    .cover-placeholder {{
      display: flex;
      align-items: center;
      justify-content: center;
      color: var(--muted);
      font-size: 15px;
      font-weight: 800;
    }}
    th, td {{
      padding: 6px 10px;
      border-bottom: 1px solid var(--line);
      text-align: left;
      vertical-align: middle;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }}
    th {{
      color: var(--muted);
      font-size: 12px;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0;
    }}
    th a {{
      color: inherit;
      text-decoration: none;
    }}
    th a:hover {{
      color: var(--ink);
    }}
    td {{
      font-size: 14px;
    }}
    .track-title {{
      font-weight: 650;
    }}
    .path {{
      color: var(--muted);
      font-size: 12px;
    }}
    .pill {{
      display: inline-flex;
      align-items: center;
      height: 24px;
      padding: 0 8px;
      border: 1px solid var(--line);
      border-radius: 6px;
      color: var(--accent-2);
      background: #fffaf2;
      font-size: 12px;
      font-weight: 700;
    }}
    .empty {{
      margin: 38px 0;
      color: var(--muted);
      font-size: 15px;
    }}
    .auto-artwork {{
      margin: 14px 0;
      padding: 10px 12px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: var(--panel);
      color: var(--muted);
      font-size: 13px;
    }}
    .auto-artwork strong {{
      color: var(--ink);
    }}
    .pagination {{
      display: flex;
      align-items: center;
      justify-content: flex-end;
      gap: 8px;
      margin: 0;
      color: var(--muted);
      font-size: 13px;
    }}
    .pagination .button {{
      min-width: 82px;
    }}
    @media (max-width: 760px) {{
      .topbar {{
        display: block;
      }}
      .summary {{
        margin-top: 14px;
      }}
      .controls {{
        grid-template-columns: 1fr 1fr;
      }}
      .controls label:first-child {{
        grid-column: 1 / -1;
      }}
      .viewbar {{
        grid-template-columns: 1fr;
      }}
      .pagination {{
        align-items: flex-start;
      }}
      .link-layout, .candidate {{
        grid-template-columns: 1fr;
      }}
      th:nth-child(4), td:nth-child(4),
      th:nth-child(5), td:nth-child(5),
      th:nth-child(7), td:nth-child(7),
      th:nth-child(9), td:nth-child(9) {{
        display: none;
      }}
      th, td {{
        padding-inline: 6px;
      }}
    }}
  </style>
</head>
<body>
  <header>
    <div class="wrap topbar">
      <h1>Crate Digger</h1>
      <div class="summary">
        <div class="metric"><strong>{view.total_count}</strong>tracks</div>
        <div class="metric"><strong>{len(music_dirs)}</strong>folders</div>
      </div>
    </div>
  </header>
  <main class="wrap">
    {_render_controls(view)}
    {_render_auto_artwork_status(auto_artwork_status)}
    <div class="viewbar">
      <span>Showing {showing_start}-{showing_end} of {view.filtered_count} matching tracks</span>
      {_render_pagination(view)}
      <form method="post" action="/reindex">
        <button type="submit">Refresh index</button>
      </form>
    </div>
    {empty}
    <table class="table" {"hidden" if not view.tracks else ""}>
      <thead>
        <tr>
          <th class="cover-cell"></th>
          <th style="width: 20%">{_sort_link(view, "title")}</th>
          <th style="width: 12%">{_sort_link(view, "artist")}</th>
          <th style="width: 13%">{_sort_link(view, "album")}</th>
          <th style="width: 8%">{_sort_link(view, "genre")}</th>
          <th style="width: 8%">{_sort_link(view, "release_date")}</th>
          <th style="width: 8%">{_sort_link(view, "file_created_at")}</th>
          <th style="width: 7%">{_sort_link(view, "format")}</th>
          <th style="width: 8%">{_sort_link(view, "bitrate")}</th>
          <th style="width: 7%">{_sort_link(view, "duration")}</th>
          <th class="spotify-cell">Spotify</th>
        </tr>
      </thead>
      <tbody>
        {rows}
      </tbody>
    </table>
    <dialog id="spotify-dialog">
      <div class="modal-header">
        <strong>Spotify</strong>
        <button class="modal-close" type="button" data-spotify-close>Close</button>
      </div>
      <div class="spotify-modal-body" id="spotify-dialog-content"></div>
    </dialog>
    <script>
      const spotifyDialog = document.getElementById("spotify-dialog");
      const spotifyDialogContent = document.getElementById("spotify-dialog-content");

      async function loadSpotifyModal(url) {{
        spotifyDialogContent.innerHTML = '<p class="empty">Loading...</p>';
        spotifyDialog.showModal();
        const response = await fetch(url, {{ headers: {{ "X-Requested-With": "fetch" }} }});
        spotifyDialogContent.innerHTML = await response.text();
      }}

      function refreshCoverImages() {{
        const artPath = pageParams.get("art_path");
        document.querySelectorAll(".cover[data-cover-path]").forEach((cover) => {{
          if (artPath && cover.dataset.coverPath !== artPath) return;

          const url = new URL("/art", window.location.origin);
          url.searchParams.set("path", cover.dataset.coverPath);
          url.searchParams.set("refresh", Date.now().toString());
          if (cover.tagName === "IMG") {{
            cover.src = url.toString();
            return;
          }}

          const image = new Image();
          image.className = "cover";
          image.alt = "";
          image.dataset.coverPath = cover.dataset.coverPath;
          image.onload = () => {{
            cover.replaceWith(image);
          }};
          image.src = url.toString();
        }});
      }}

      const pageParams = new URLSearchParams(window.location.search);
      if (pageParams.get("art_refresh") === "1") {{
        pageParams.delete("art_refresh");
        pageParams.delete("art_path");
        const cleanQuery = pageParams.toString();
        const cleanUrl = `${{window.location.pathname}}${{cleanQuery ? `?${{cleanQuery}}` : ""}}`;
        window.history.replaceState(null, "", cleanUrl);
        [0, 1000, 3000, 7000, 15000].forEach((delay) => {{
          window.setTimeout(refreshCoverImages, delay);
        }});
      }}

      function renderAutoArtworkStatus(status) {{
        const node = document.getElementById("auto-artwork-status");
        if (!node) return;
        if (!status.running && Number(status.total || 0) === 0) {{
          node.hidden = true;
          return;
        }}

        node.hidden = false;
        const total = Number(status.total || 0);
        const processed = Number(status.processed || 0);
        const linked = Number(status.linked || 0);
        const artworkUpdated = Number(status.artwork_updated || 0);
        const noResults = Number(status.no_results || 0);
        const failed = Number(status.failed || 0);
        const escapeHtml = (value) => value
          .replaceAll("&", "&amp;")
          .replaceAll("<", "&lt;")
          .replaceAll(">", "&gt;")
          .replaceAll('"', "&quot;")
          .replaceAll("'", "&#x27;");
        const current = status.current ? ` · Current: <strong>${{escapeHtml(status.current)}}</strong>` : "";
        if (status.running) {{
          node.innerHTML = `<strong>Spotify sweep running</strong> · ${{processed}}/${{total}} processed · ${{linked}} linked · ${{artworkUpdated}} covers · ${{noResults}} no results · ${{failed}} failed${{current}}`;
          return;
        }}
        node.innerHTML = `<strong>Spotify sweep complete</strong> · ${{processed}}/${{total}} processed · ${{linked}} linked · ${{artworkUpdated}} covers · ${{noResults}} no results · ${{failed}} failed`;
      }}

      async function pollAutoArtworkStatus() {{
        const node = document.getElementById("auto-artwork-status");
        if (!node || node.hidden) return;
        const response = await fetch("/api/spotify-artwork-refresh");
        const status = await response.json();
        renderAutoArtworkStatus(status);
        if (status.running) {{
          window.setTimeout(pollAutoArtworkStatus, 2500);
        }} else if (Number(status.artwork_updated || 0) > 0) {{
          window.setTimeout(refreshCoverImages, 250);
        }}
      }}

      pollAutoArtworkStatus();

      document.addEventListener("click", (event) => {{
        const opener = event.target.closest("[data-spotify-modal-url]");
        if (opener) {{
          event.preventDefault();
          loadSpotifyModal(opener.dataset.spotifyModalUrl);
        }}

        if (event.target.closest("[data-spotify-close]")) {{
          spotifyDialog.close();
        }}
      }});
    </script>
  </main>
</body>
</html>"""


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


def _status_int(value: object) -> int:
    return value if isinstance(value, int) else 0


def _render_controls(view: CollectionView) -> str:
    format_options = ['<option value="">All formats</option>']
    format_options.extend(
        f'<option value="{escape(audio_format)}" {_selected(view.query.audio_format, audio_format)}>'
        f"{escape(audio_format)}</option>"
        for audio_format in view.formats
    )
    metadata_options = "\n".join(
        f'<option value="{escape(key)}" {_selected(view.query.metadata, key)}>'
        f"{escape(label)}</option>"
        for key, label in METADATA_FILTER_LABELS.items()
    )
    spotify_options = "\n".join(
        f'<option value="{escape(key)}" {_selected(view.query.spotify, key)}>'
        f"{escape(label)}</option>"
        for key, label in SPOTIFY_FILTER_LABELS.items()
    )
    return f"""<form class="controls" method="get">
  <label>
    Search
    <input type="search" name="q" value="{escape(view.query.q)}" placeholder="Title, artist, album, path">
  </label>
  <label>
    Format
    <select name="format">
      {"".join(format_options)}
    </select>
  </label>
  <label>
    Tags
    <select name="metadata">
      {metadata_options}
    </select>
  </label>
  <label>
    Spotify
    <select name="spotify">
      {spotify_options}
    </select>
  </label>
  <input type="hidden" name="sort" value="{escape(view.query.sort)}">
  <input type="hidden" name="direction" value="{escape(view.query.direction)}">
  <button type="submit">Apply</button>
  <a class="button" href="/">Reset</a>
</form>"""


def _render_pagination(view: CollectionView) -> str:
    previous_link = _page_link(view, view.query.page - 1, "Previous")
    next_link = _page_link(view, view.query.page + 1, "Next")
    return f"""<nav class="pagination" aria-label="Pagination">
  {previous_link}
  <span>Page {view.query.page} of {view.total_pages}</span>
  {next_link}
</nav>"""


def _render_spotify_link_idle(music_dirs: list[str]) -> str:
    return _render_page_shell(
        title="Spotify Linker",
        summary=f"{len(music_dirs)} folders",
        body="""
  <main class="wrap">
    <div class="link-layout">
      <p class="empty">Choose a track from the collection list to search Spotify.</p>
      <a class="button" href="/">Back to collection</a>
    </div>
  </main>
""",
    )


def _render_spotify_link_done(music_dirs: list[str]) -> str:
    return _render_page_shell(
        title="Spotify Linker",
        summary=f"{len(music_dirs)} folders",
        body="""
  <main class="wrap">
    <div class="link-layout">
      <p class="empty">That local track is no longer in the collection index.</p>
      <a class="button" href="/">Back to collection</a>
    </div>
  </main>
""",
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
    return _render_page_shell(
        title="Spotify Linker",
        summary=f"{len(music_dirs)} folders",
        body=f"""
  <main class="wrap">
    {content}
  </main>
""",
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
    candidate_rows = "\n".join(
        _render_spotify_candidate(track, candidate, return_to)
        for candidate in candidates
    )
    if not candidate_rows:
        candidate_rows = '<p class="empty">No Spotify results for this query.</p>'
    if lookup_error:
        candidate_rows = f'<p class="empty">{escape(lookup_error)}</p>'

    next_offset = offset + SPOTIFY_LINK_LIMIT
    previous_offset = max(0, offset - SPOTIFY_LINK_LIMIT)
    previous_href = _spotify_link_href(
        track=track,
        offset=previous_offset,
        return_to=return_to,
        partial=False,
    )
    next_href = _spotify_link_href(
        track=track,
        offset=next_offset,
        return_to=return_to,
        partial=False,
    )
    previous_modal_url = _spotify_link_href(
        track=track,
        offset=previous_offset,
        return_to=return_to,
        partial=True,
    )
    next_modal_url = _spotify_link_href(
        track=track,
        offset=next_offset,
        return_to=return_to,
        partial=True,
    )
    collection_link = (
        f'<a class="button" href="{escape(return_to)}">Collection</a>'
        if not partial
        else ""
    )

    return f"""
    <div class="linkbar">
      {collection_link}
      <form method="post" action="/spotify-link/skip">
        <input type="hidden" name="path" value="{escape(str(track.path))}">
        <input type="hidden" name="return_to" value="{escape(return_to)}">
        <button type="submit">Skip</button>
      </form>
      <a class="button" href="{escape(previous_href)}" data-spotify-modal-url="{escape(previous_modal_url)}">Previous results</a>
      <a class="button" href="{escape(next_href)}" data-spotify-modal-url="{escape(next_modal_url)}">More results</a>
    </div>
    <section class="link-layout">
      <div class="local-track">
        {_render_cover(track)}
        <div>
          <h2>{escape(track.display_title)}</h2>
          <p>{escape(track.display_artist)}</p>
          <p>{escape(track.album or "Unknown album")}</p>
          <p class="path">{escape(_short_path(track.path))}</p>
        </div>
      </div>
      <div class="candidate-list">
        <p class="query">Spotify search: {escape(_spotify_search_query(track))}</p>
        {candidate_rows}
      </div>
    </section>
"""


def _render_spotify_candidate(
    track: LocalTrack,
    candidate: SpotifyCandidate,
    return_to: str,
) -> str:
    external_link = ""
    if candidate.external_url:
        external_link = (
            f'<a class="candidate-open" href="{escape(candidate.external_url)}" '
            'target="_blank" rel="noreferrer">Open</a>'
        )
    cover = _render_candidate_cover(candidate)
    image_input = ""
    if candidate.image_url:
        image_input = (
            f'<input type="hidden" name="image_url" '
            f'value="{escape(candidate.image_url)}">'
        )
    return f"""<div class="candidate">
  {cover}
  <div>
    <strong>{escape(candidate.name)}</strong>
    <p>{escape(candidate.artists)}</p>
    <p>{escape(candidate.album)}</p>
    <p class="path">{escape(candidate.uri)}</p>
  </div>
  <div class="candidate-actions">
    {external_link}
    <form method="post" action="/spotify-link/link">
      <input type="hidden" name="path" value="{escape(str(track.path))}">
      <input type="hidden" name="spotify_uri" value="{escape(candidate.uri)}">
      {image_input}
      <input type="hidden" name="return_to" value="{escape(return_to)}">
      <button type="submit">Link</button>
    </form>
  </div>
</div>"""


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


def _dashboard_css() -> str:
    return """
    :root {
      color-scheme: light;
      --ink: #191b22;
      --muted: #636978;
      --line: #d8dce5;
      --panel: #f6f7f9;
      --accent: #0f766e;
      --accent-2: #b45309;
      --bg: #ffffff;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--ink);
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      line-height: 1.45;
    }
    header {
      border-bottom: 1px solid var(--line);
      background: var(--panel);
    }
    .wrap {
      width: min(1180px, calc(100vw - 32px));
      margin: 0 auto;
    }
    .topbar {
      display: flex;
      align-items: end;
      justify-content: space-between;
      gap: 24px;
      padding: 16px 0 14px;
    }
    h1, h2, p {
      margin: 0;
    }
    h1 {
      font-size: 28px;
      font-weight: 750;
      letter-spacing: 0;
    }
    h2 {
      font-size: 20px;
      letter-spacing: 0;
    }
    main {
      padding: 14px 0 32px;
    }
    .summary {
      display: flex;
      gap: 12px;
      color: var(--muted);
      font-size: 14px;
    }
    .metric {
      border-left: 3px solid var(--accent);
      padding-left: 10px;
      min-width: 84px;
    }
    .button, button {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      height: 34px;
      padding: 0 12px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #fff;
      color: var(--ink);
      font: inherit;
      font-size: 14px;
      font-weight: 700;
      text-decoration: none;
      cursor: pointer;
    }
    button {
      background: var(--accent);
      color: #fff;
      border-color: var(--accent);
    }
    .linkbar {
      display: flex;
      gap: 8px;
      justify-content: flex-end;
      margin-bottom: 12px;
    }
    .link-layout {
      display: grid;
      grid-template-columns: 320px 1fr;
      gap: 20px;
      align-items: start;
    }
    .local-track {
      display: grid;
      grid-template-columns: 84px 1fr;
      gap: 14px;
      padding: 14px 0;
      border-top: 1px solid var(--line);
      border-bottom: 1px solid var(--line);
    }
    .local-track .cover {
      width: 84px;
      height: 84px;
    }
    .cover {
      display: block;
      width: 42px;
      height: 42px;
      border: 1px solid var(--line);
      border-radius: 6px;
      object-fit: cover;
      background: var(--panel);
    }
    .cover-placeholder {
      display: flex;
      align-items: center;
      justify-content: center;
    }
    .path, .query {
      color: var(--muted);
      font-size: 12px;
    }
    .candidate-list {
      display: grid;
      gap: 8px;
    }
    .candidate {
      display: grid;
      grid-template-columns: 54px 1fr auto;
      gap: 12px;
      align-items: center;
      padding: 10px 0;
      border-bottom: 1px solid var(--line);
    }
    .candidate-cover {
      width: 44px;
      height: 44px;
      border: 1px solid var(--line);
      border-radius: 6px;
      object-fit: cover;
      background: var(--panel);
    }
    .candidate p {
      color: var(--muted);
      font-size: 13px;
    }
    .candidate-actions {
      display: flex;
      align-items: center;
      gap: 8px;
    }
    .candidate-open {
      color: var(--accent);
      font-size: 13px;
      font-weight: 700;
      text-decoration: none;
    }
    .empty {
      margin: 28px 0;
      color: var(--muted);
      font-size: 15px;
    }
    @media (max-width: 760px) {
      .topbar, .linkbar {
        display: block;
      }
      .link-layout {
        grid-template-columns: 1fr;
      }
      .linkbar > * {
        margin-bottom: 8px;
      }
    }
"""


def _render_page_shell(*, title: str, summary: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(title)}</title>
  <style>
{_dashboard_css()}
  </style>
</head>
<body>
  <header>
    <div class="wrap topbar">
      <h1>{escape(title)}</h1>
      <div class="summary">
        <div class="metric"><strong>{escape(summary)}</strong></div>
      </div>
    </div>
  </header>
{body}
</body>
</html>"""


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


def _selected(current: object, value: object) -> str:
    return "selected" if current == value else ""


def _render_track_row(track: LocalTrack, view: CollectionView) -> str:
    spotify_action = _render_spotify_action(track, return_to=_url_for(view))
    comment_attr = f' title="{escape(track.comment)}"' if track.comment else ""
    return f"""<tr{comment_attr}>
  <td class="cover-cell">{_render_cover(track)}</td>
  <td>
    <div class="track-title">{escape(track.display_title)}</div>
    <div class="path">{escape(_short_path(track.path))}</div>
  </td>
  <td>{escape(track.display_artist)}</td>
  <td>{escape(track.album or "")}</td>
  <td>{escape(track.genre or "")}</td>
  <td>{escape(track.release_date or "")}</td>
  <td>{escape(_format_date(track.file_created_at))}</td>
  <td><span class="pill">{escape(track.audio_format or "?")}</span></td>
  <td>{escape(_format_bitrate(track.bitrate))}</td>
  <td>{escape(_format_duration(track.duration_seconds))}</td>
  <td class="spotify-cell">{spotify_action}</td>
</tr>"""


def _render_spotify_action(track: LocalTrack, *, return_to: str) -> str:
    if track.spotify_uri:
        external_url = _spotify_external_url_from_uri(track.spotify_uri)
        if external_url:
            linked = (
                f'<a class="spotify-linked" href="{escape(external_url)}" '
                'target="_blank" rel="noreferrer">Linked</a>'
            )
        else:
            linked = '<span class="spotify-linked">Linked</span>'
        art_action = _refresh_spotify_art_action(track, return_to=return_to)
        return f"""<div class="spotify-actions">
  {linked}
  {art_action}
  {_manual_spotify_link(track, return_to=return_to)}
</div>"""

    if track.spotify_link_skipped_at:
        return '<span class="spotify-linked">Skipped</span>'

    return f"""<div class="spotify-actions">
  <form method="post" action="/spotify-link/quick-link">
    <input type="hidden" name="path" value="{escape(str(track.path))}">
    <input type="hidden" name="return_to" value="{escape(return_to)}">
    <button class="spotify-action" type="submit">Link</button>
  </form>
  {_manual_spotify_link(track, return_to=return_to)}
</div>"""


def _manual_spotify_link(track: LocalTrack, *, return_to: str) -> str:
    href = _spotify_link_href(
        track=track,
        offset=0,
        return_to=return_to,
        partial=False,
    )
    modal_url = _spotify_link_href(
        track=track,
        offset=0,
        return_to=return_to,
        partial=True,
    )
    return (
        f'<a class="button spotify-action" href="{escape(href)}" '
        f'data-spotify-modal-url="{escape(modal_url)}">Find</a>'
    )


def _refresh_spotify_art_action(track: LocalTrack, *, return_to: str) -> str:
    if track.artwork_mime is not None:
        return ""
    return f"""<form method="post" action="/spotify-link/refresh-art">
    <input type="hidden" name="path" value="{escape(str(track.path))}">
    <input type="hidden" name="return_to" value="{escape(return_to)}">
    <button class="spotify-action" type="submit">Art</button>
  </form>"""


def _spotify_external_url_from_uri(uri: str) -> str | None:
    prefix = "spotify:track:"
    if not uri.startswith(prefix):
        return None
    return f"https://open.spotify.com/track/{uri.removeprefix(prefix)}"


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
