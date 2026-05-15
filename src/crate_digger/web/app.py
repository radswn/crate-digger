from contextlib import asynccontextmanager
from dataclasses import dataclass
from html import escape
from pathlib import Path
from typing import Literal, cast
from urllib.parse import urlencode

from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, RedirectResponse

from crate_digger.collection.index import (
    DEFAULT_COLLECTION_DB_PATH,
    query_tracks,
    refresh_collection_index,
)
from crate_digger.collection.models import LocalTrack
from crate_digger.utils.config import get_settings

SortKey = Literal["title", "artist", "album", "format", "bitrate", "duration", "path"]
SortDirection = Literal["asc", "desc"]
MetadataFilter = Literal["all", "missing", "complete"]

DEFAULT_PAGE_SIZE = 50
PAGE_SIZE_OPTIONS = (25, 50, 100, 200)
SORT_LABELS: dict[SortKey, str] = {
    "title": "Title",
    "artist": "Artist",
    "album": "Album",
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


@dataclass(frozen=True)
class CollectionQuery:
    q: str
    audio_format: str
    metadata: MetadataFilter
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
        sort: str = "title",
        direction: str = "asc",
        page: int = 1,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> dict[str, object]:
        view = _build_collection_view(
            db_path,
            q=q,
            audio_format=audio_format,
            metadata=metadata,
            sort=sort,
            direction=direction,
            page=page,
            page_size=page_size,
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
        sort: str = "title",
        direction: str = "asc",
        page: int = 1,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> HTMLResponse:
        collection = get_settings(config_path)["collection"]
        view = _build_collection_view(
            db_path,
            q=q,
            audio_format=audio_format,
            metadata=metadata,
            sort=sort,
            direction=direction,
            page=page,
            page_size=page_size,
        )
        return HTMLResponse(_render_index(view, collection["music_dirs"]))

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
        "duration_seconds": track.duration_seconds,
        "bitrate": track.bitrate,
        "audio_format": track.audio_format,
    }


def _build_collection_view(
    db_path: Path,
    *,
    q: str,
    audio_format: str,
    metadata: str,
    sort: str,
    direction: str,
    page: int,
    page_size: int,
) -> CollectionView:
    query = _normalize_query(
        q=q,
        audio_format=audio_format,
        metadata=metadata,
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
        sort=query.sort,
        direction=query.direction,
        page=query.page,
        page_size=query.page_size,
    )
    query = CollectionQuery(
        q=query.q,
        audio_format=query.audio_format,
        metadata=query.metadata,
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
    sort: str,
    direction: str,
    page: int,
    page_size: int,
) -> CollectionQuery:
    normalized_metadata: MetadataFilter = "all"
    if metadata in METADATA_FILTER_LABELS:
        normalized_metadata = cast(MetadataFilter, metadata)

    normalized_sort: SortKey = "title"
    if sort in SORT_LABELS:
        normalized_sort = cast(SortKey, sort)

    normalized_direction: SortDirection = "asc"
    if direction == "desc":
        normalized_direction = "desc"

    normalized_page_size = (
        page_size if page_size in PAGE_SIZE_OPTIONS else DEFAULT_PAGE_SIZE
    )

    return CollectionQuery(
        q=q.strip(),
        audio_format=audio_format.strip().upper(),
        metadata=normalized_metadata,
        sort=normalized_sort,
        direction=normalized_direction,
        page=max(1, page),
        page_size=normalized_page_size,
    )


def _render_index(view: CollectionView, music_dirs: list[str]) -> str:
    rows = "\n".join(_render_track_row(track) for track in view.tracks)
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
      padding: 28px 0 22px;
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
      padding: 24px 0 48px;
    }}
    .controls {{
      display: grid;
      grid-template-columns: minmax(220px, 1fr) repeat(3, minmax(130px, auto)) auto;
      gap: 10px;
      align-items: end;
      margin-bottom: 18px;
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
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      margin-bottom: 10px;
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
    .table {{
      width: 100%;
      border-collapse: collapse;
      table-layout: fixed;
      border-top: 1px solid var(--line);
    }}
    th, td {{
      padding: 11px 10px;
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
    .pagination {{
      display: flex;
      align-items: center;
      justify-content: flex-end;
      gap: 8px;
      margin-top: 16px;
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
      .viewbar, .pagination {{
        align-items: flex-start;
        flex-direction: column;
      }}
      th:nth-child(3), td:nth-child(3),
      th:nth-child(5), td:nth-child(5) {{
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
        <div class="metric"><strong>{_total_duration(view.tracks)}</strong>page runtime</div>
      </div>
    </div>
  </header>
  <main class="wrap">
    {_render_controls(view)}
    <div class="viewbar">
      <span>Showing {showing_start}-{showing_end} of {view.filtered_count} matching tracks</span>
      <form method="post" action="/reindex">
        <button type="submit">Refresh index</button>
      </form>
    </div>
    {empty}
    <table class="table" {"hidden" if not view.tracks else ""}>
      <thead>
        <tr>
          <th style="width: 31%">{_sort_link(view, "title")}</th>
          <th style="width: 18%">{_sort_link(view, "artist")}</th>
          <th style="width: 20%">{_sort_link(view, "album")}</th>
          <th style="width: 9%">{_sort_link(view, "format")}</th>
          <th style="width: 10%">{_sort_link(view, "bitrate")}</th>
          <th style="width: 12%">{_sort_link(view, "duration")}</th>
        </tr>
      </thead>
      <tbody>
        {rows}
      </tbody>
    </table>
    {_render_pagination(view)}
  </main>
</body>
</html>"""


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
    page_size_options = "\n".join(
        f'<option value="{page_size}" {_selected(view.query.page_size, page_size)}>'
        f"{page_size} / page</option>"
        for page_size in PAGE_SIZE_OPTIONS
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
    Rows
    <select name="page_size">
      {page_size_options}
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
        "sort": view.query.sort,
        "direction": view.query.direction,
        "page": view.query.page,
        "page_size": view.query.page_size,
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


def _render_track_row(track: LocalTrack) -> str:
    return f"""<tr>
  <td>
    <div class="track-title">{escape(track.display_title)}</div>
    <div class="path">{escape(_short_path(track.path))}</div>
  </td>
  <td>{escape(track.display_artist)}</td>
  <td>{escape(track.album or "")}</td>
  <td><span class="pill">{escape(track.audio_format or "?")}</span></td>
  <td>{escape(_format_bitrate(track.bitrate))}</td>
  <td>{escape(_format_duration(track.duration_seconds))}</td>
</tr>"""


def _short_path(path: Path) -> str:
    parts = path.parts
    if len(parts) <= 3:
        return str(path)
    return str(Path("...", *parts[-3:]))


def _format_bitrate(bitrate: int | None) -> str:
    if not bitrate:
        return ""
    return f"{round(bitrate / 1000)} kbps"


def _format_duration(duration_seconds: float | None) -> str:
    if duration_seconds is None:
        return ""
    minutes, seconds = divmod(round(duration_seconds), 60)
    return f"{minutes}:{seconds:02d}"


def _total_duration(tracks: list[LocalTrack]) -> str:
    seconds = int(sum(track.duration_seconds or 0 for track in tracks))
    if seconds <= 0:
        return "0m"
    hours, remainder = divmod(seconds, 3600)
    minutes = remainder // 60
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"
