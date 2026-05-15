from html import escape
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from crate_digger.collection.models import LocalTrack
from crate_digger.collection.scanner import scan_collection
from crate_digger.utils.config import get_settings


def create_app(config_path: str = "config.toml") -> FastAPI:
    app = FastAPI(title="Crate Digger Dashboard")

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/tracks")
    def tracks() -> list[dict[str, object]]:
        collection = get_settings(config_path)["collection"]
        return [
            _track_to_json(track) for track in scan_collection(collection["music_dirs"])
        ]

    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        collection = get_settings(config_path)["collection"]
        tracks = scan_collection(collection["music_dirs"])
        return HTMLResponse(_render_index(tracks, collection["music_dirs"]))

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


def _render_index(tracks: list[LocalTrack], music_dirs: list[str]) -> str:
    rows = "\n".join(_render_track_row(track) for track in tracks)
    empty = ""
    if not music_dirs:
        empty = '<p class="empty">No collection directories configured.</p>'
    elif not tracks:
        empty = '<p class="empty">No supported audio files found.</p>'

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
    @media (max-width: 760px) {{
      .topbar {{
        display: block;
      }}
      .summary {{
        margin-top: 14px;
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
        <div class="metric"><strong>{len(tracks)}</strong>tracks</div>
        <div class="metric"><strong>{len(music_dirs)}</strong>folders</div>
        <div class="metric"><strong>{_total_duration(tracks)}</strong>runtime</div>
      </div>
    </div>
  </header>
  <main class="wrap">
    {empty}
    <table class="table" {"hidden" if not tracks else ""}>
      <thead>
        <tr>
          <th style="width: 34%">Title</th>
          <th style="width: 22%">Artist</th>
          <th style="width: 22%">Album</th>
          <th style="width: 10%">Format</th>
          <th style="width: 12%">Bitrate</th>
        </tr>
      </thead>
      <tbody>
        {rows}
      </tbody>
    </table>
  </main>
</body>
</html>"""


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


def _total_duration(tracks: list[LocalTrack]) -> str:
    seconds = int(sum(track.duration_seconds or 0 for track in tracks))
    if seconds <= 0:
        return "0m"
    hours, remainder = divmod(seconds, 3600)
    minutes = remainder // 60
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"
