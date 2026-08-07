import json
import mimetypes
from dataclasses import asdict
from html import escape
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode

from fastapi import APIRouter, Query, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
)

from crate_digger.collection.models import LocalTrack, TrackTag
from crate_digger.collection.profiles import (
    PROFILE_ROLES,
    REVIEW_MODES,
    TAG_CATEGORIES,
    get_profile,
    get_profile_review_candidates,
    get_track,
    list_source_metadata,
    list_tags,
    replace_manual_tags,
    upsert_manual_profile,
)


COMMON_TAGS = (
    ("palette", "tech", "Tech", "T"),
    ("palette", "house", "House", "H"),
    ("groove", "groovy", "Groovy", "G"),
    ("groove", "rolling", "Rolling", "R"),
    ("palette", "deep", "Deep", "D"),
    ("palette", "minimal", "Minimal", "M"),
)


def create_profiles_router(db_path: Path) -> APIRouter:
    router = APIRouter()

    @router.get("/profiles", response_class=HTMLResponse)
    def profiles_page(
        mode: str = "missing-energy",
        path: str | None = None,
    ) -> HTMLResponse:
        normalized_mode = _mode(mode)
        view = _profile_view(db_path, normalized_mode, path)
        return HTMLResponse(_render_profiles(view, normalized_mode))

    @router.post("/profiles/update")
    async def update_profile_page(request: Request) -> Response:
        form = _parse_form(await request.body())
        mode = _mode(form.get("mode", "missing-energy"))
        track_path = form.get("path", "")
        candidates = get_profile_review_candidates(db_path, mode=mode)
        next_path = _neighbor_path(candidates, track_path, 1)
        try:
            values = _profile_values(form)
            _save_values(db_path, track_path, values)
        except (KeyError, ValueError) as error:
            view = _profile_view(db_path, mode, track_path)
            return HTMLResponse(
                _render_profiles(view, mode, error=str(error)), status_code=400
            )
        query: dict[str, str] = {"mode": mode}
        if next_path is not None:
            query["path"] = next_path
        return RedirectResponse(f"/profiles?{urlencode(query)}", status_code=303)

    @router.get("/api/profiles")
    def profiles_api(mode: str = "missing-energy") -> dict[str, object]:
        normalized_mode = _mode(mode)
        candidates = get_profile_review_candidates(db_path, mode=normalized_mode)
        return {
            "mode": normalized_mode,
            "count": len(candidates),
            "tracks": [_track_json(track) for track in candidates],
        }

    @router.get("/api/profiles/detail")
    def profile_detail_api(
        path: str = Query(...), mode: str = "missing-energy"
    ) -> JSONResponse:
        normalized_mode = _mode(mode)
        view = _profile_view(db_path, normalized_mode, path)
        if view["track"] is None:
            return JSONResponse({"detail": "Indexed track not found"}, status_code=404)
        return JSONResponse(_view_json(view, normalized_mode))

    @router.post("/api/profiles/update")
    async def update_profile_api(request: Request) -> JSONResponse:
        try:
            payload = await request.json()
        except json.JSONDecodeError:
            return JSONResponse({"detail": "Invalid JSON body"}, status_code=400)
        if not isinstance(payload, dict):
            return JSONResponse(
                {"detail": "JSON body must be an object"}, status_code=400
            )
        try:
            track_path = _required_string(payload, "path")
            mode = _mode(str(payload.get("mode", "missing-energy")))
            values = _profile_values(payload)
            candidates = get_profile_review_candidates(db_path, mode=mode)
            next_path = _neighbor_path(candidates, track_path, 1)
            _save_values(db_path, track_path, values)
        except KeyError as error:
            return JSONResponse({"detail": str(error)}, status_code=404)
        except (TypeError, ValueError) as error:
            return JSONResponse({"detail": str(error)}, status_code=400)
        view = _profile_view(db_path, mode, track_path)
        response = _view_json(view, mode)
        response["next_path"] = next_path
        return JSONResponse(response)

    @router.get("/profiles/audio")
    def profile_audio(path: str = Query(...)) -> Response:
        track = get_track(db_path, track_path=path)
        if track is None:
            return JSONResponse({"detail": "Indexed track not found"}, status_code=404)
        audio_path = Path(path)
        if not audio_path.is_file():
            return JSONResponse({"detail": "Audio file not found"}, status_code=404)
        media_type = (
            mimetypes.guess_type(audio_path.name)[0] or "application/octet-stream"
        )
        return FileResponse(audio_path, media_type=media_type, filename=audio_path.name)

    return router


def _profile_view(
    db_path: Path, mode: str, requested_path: str | None
) -> dict[str, Any]:
    candidates = get_profile_review_candidates(db_path, mode=mode)
    track: LocalTrack | None = None
    if requested_path is not None:
        track = get_track(db_path, track_path=requested_path)
    if track is None and requested_path is None and candidates:
        track = candidates[0]

    track_path = str(track.path) if track is not None else None
    return {
        "track": track,
        "profile": get_profile(db_path, track_path=track_path) if track_path else None,
        "tags": list_tags(db_path, track_path=track_path) if track_path else [],
        "source_metadata": (
            list_source_metadata(db_path, track_path=track_path) if track_path else []
        ),
        "candidate_count": len(candidates),
        "previous_path": _neighbor_path(candidates, track_path, -1),
        "next_path": _neighbor_path(candidates, track_path, 1),
    }


def _save_values(db_path: Path, track_path: str, values: dict[str, Any]) -> None:
    if get_track(db_path, track_path=track_path) is None:
        raise KeyError(f"Indexed track not found: {track_path}")
    tags = values.pop("manual_tags")
    upsert_manual_profile(db_path, track_path=track_path, **values)
    replace_manual_tags(db_path, track_path=track_path, tags=tags)


def _profile_values(data: dict[str, Any]) -> dict[str, Any]:
    energy = _optional_scale(data.get("energy"), "energy")
    personal_rating = _optional_scale(data.get("personal_rating"), "personal_rating")
    raw_role = data.get("set_role")
    set_role = str(raw_role).strip() if raw_role is not None else ""
    if set_role and set_role not in PROFILE_ROLES:
        raise ValueError(f"Invalid set role: {set_role}")
    raw_notes = data.get("notes")
    notes = str(raw_notes).strip() if raw_notes is not None else ""
    manual_tags = _manual_tags(data)
    return {
        "energy": energy,
        "personal_rating": personal_rating,
        "set_role": set_role or None,
        "notes": notes or None,
        "manual_tags": manual_tags,
    }


def _manual_tags(data: dict[str, Any]) -> list[tuple[str, str]]:
    tags: set[tuple[str, str]] = set()
    raw_tags = data.get("manual_tags", "")
    if isinstance(raw_tags, list):
        items = raw_tags
    else:
        items = str(raw_tags).replace(",", "\n").splitlines()
    for raw in items:
        text = str(raw).strip()
        if not text:
            continue
        if ":" in text:
            category, value = text.split(":", maxsplit=1)
        else:
            category, value = "legacy", text
        category = category.strip().casefold()
        value = " ".join(value.split()).casefold()
        if category not in TAG_CATEGORIES:
            raise ValueError(f"Invalid tag category: {category}")
        if not value:
            raise ValueError("Manual tag value cannot be empty")
        tags.add((category, value))
    for category, value, _label, _key in COMMON_TAGS:
        field = f"tag_{category}_{value}"
        enabled = data.get(field)
        if enabled in (True, 1, "1", "on", "true", value):
            tags.add((category, value))
    return sorted(tags)


def _optional_scale(value: object, name: str) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValueError(f"{name} must be unset or an integer from 1 to 5")
    try:
        parsed = int(str(value))
    except (TypeError, ValueError) as error:
        raise ValueError(f"{name} must be unset or an integer from 1 to 5") from error
    if not 1 <= parsed <= 5:
        raise ValueError(f"{name} must be unset or an integer from 1 to 5")
    return parsed


def _mode(value: str) -> str:
    return value if value in REVIEW_MODES else "missing-energy"


def _neighbor_path(
    candidates: list[LocalTrack], current_path: str | None, offset: int
) -> str | None:
    if not candidates or current_path is None:
        return None
    paths = [str(track.path) for track in candidates]
    try:
        index = paths.index(current_path)
    except ValueError:
        return None
    target = index + offset
    return paths[target] if 0 <= target < len(paths) else None


def _parse_form(body: bytes) -> dict[str, Any]:
    parsed = parse_qs(body.decode("utf-8"), keep_blank_values=True)
    return {key: values[-1] for key, values in parsed.items() if values}


def _required_string(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} must be a non-empty string")
    return value


def _view_json(view: dict[str, Any], mode: str) -> dict[str, Any]:
    track = view["track"]
    return {
        "mode": mode,
        "track": _track_json(track) if track is not None else None,
        "profile": asdict(view["profile"]) if view["profile"] is not None else None,
        "tags": [asdict(tag) for tag in view["tags"]],
        "source_metadata": [asdict(item) for item in view["source_metadata"]],
        "candidate_count": view["candidate_count"],
        "previous_path": view["previous_path"],
        "next_path": view["next_path"],
    }


def _track_json(track: LocalTrack) -> dict[str, object]:
    return {
        "path": str(track.path),
        "title": track.title,
        "artist": track.artist,
        "album": track.album,
        "duration_seconds": track.duration_seconds,
        "audio_format": track.audio_format,
        "spotify_uri": track.spotify_uri,
        "has_artwork": track.artwork_mime is not None,
    }


def _render_profiles(view: dict[str, Any], mode: str, error: str | None = None) -> str:
    track: LocalTrack | None = view["track"]
    mode_links = "".join(
        f'<a class="mode {"active" if item == mode else ""}" '
        f'href="/profiles?{urlencode({"mode": item})}">{escape(label)}</a>'
        for item, label in (
            ("missing-energy", "Missing energy"),
            ("all", "All"),
            ("imported", "Imported"),
            ("conflicts", "Conflicts"),
        )
    )
    if track is None:
        content = """
        <section class="empty">
          <h2>No tracks in this review queue</h2>
          <p>Choose another mode or import/index more tracks.</p>
        </section>
        """
    else:
        content = _render_track_form(view, mode, error)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Track Profiles · Crate Digger</title>
  <style>{_css()}</style>
</head>
<body>
  <header><div><a href="/">Crate Digger</a><h1>Track Profiles</h1></div>
    <span>{view["candidate_count"]} tracks in queue</span></header>
  <nav>{mode_links}</nav>
  <main>{content}</main>
  <footer>Shortcuts: 1–5 energy · G Groovy · R Rolling · T Tech · H House · D Deep · M Minimal · S/Enter save · ←/→ navigate</footer>
  <script>{_javascript()}</script>
</body>
</html>"""


def _render_track_form(view: dict[str, Any], mode: str, error: str | None) -> str:
    track: LocalTrack = view["track"]
    profile = view["profile"]
    tags: list[TrackTag] = view["tags"]
    source_metadata = view["source_metadata"]
    manual_set = {(tag.category, tag.value) for tag in tags if tag.source == "manual"}
    common_set = {(category, value) for category, value, _label, _key in COMMON_TAGS}
    other_manual = sorted(manual_set - common_set)
    imported = [tag for tag in tags if tag.source != "manual"]
    artwork = (
        f'<img class="cover" src="/art?{urlencode({"path": str(track.path)})}" alt="">'
        if track.artwork_mime
        else '<div class="cover placeholder">No artwork</div>'
    )
    error_html = f'<p class="error">{escape(error)}</p>' if error else ""
    sources = "".join(_render_source(item) for item in source_metadata)
    imported_tags = _render_tags(imported, "No imported tags")
    common_buttons = "".join(
        _render_common_tag(category, value, label, key, manual_set)
        for category, value, label, key in COMMON_TAGS
    )
    role_options = '<option value="">Unset</option>' + "".join(
        f'<option value="{role}" {"selected" if profile and profile.set_role == role else ""}>{role.title()}</option>'
        for role in sorted(PROFILE_ROLES)
    )
    previous_link = _navigation_link(view["previous_path"], mode, "← Previous", "prev")
    next_link = _navigation_link(view["next_path"], mode, "Next →", "next")
    spotify = (
        '<span class="ok">Linked</span>'
        if track.spotify_uri
        else "<span>Not linked</span>"
    )
    return f"""
    {error_html}
    <section class="track-grid">
      <div>{artwork}<audio controls preload="metadata" src="/profiles/audio?{urlencode({"path": str(track.path)})}"></audio></div>
      <div class="track-meta">
        <p class="eyebrow">{escape(track.audio_format or "Unknown format")} · {_duration(track.duration_seconds)}</p>
        <h2>{escape(track.display_title)}</h2>
        <h3>{escape(track.display_artist)}</h3>
        <p>{escape(track.album or "Unknown album")} · Spotify: {spotify}</p>
        <p class="path">{escape(str(track.path))}</p>
        <div class="sources">{sources or "<p>No imported source metadata.</p>"}</div>
      </div>
    </section>
    <form id="profile-form" method="post" action="/profiles/update">
      <input type="hidden" name="path" value="{escape(str(track.path), quote=True)}">
      <input type="hidden" name="mode" value="{escape(mode, quote=True)}">
      <section class="panel">
        <h3>Energy</h3><div class="scale">{_scale("energy", profile.energy if profile else None, False)}</div>
        <h3>Personal rating</h3><div class="scale">{_scale("personal_rating", profile.personal_rating if profile else None, True)}</div>
        <label>Set role<select name="set_role">{role_options}</select></label>
        <label>Notes<textarea name="notes" rows="4">{escape(profile.notes or "" if profile else "")}</textarea></label>
      </section>
      <section class="panel"><h3>Manual tags</h3><div class="tag-toggles">{common_buttons}</div>
        <label>Additional tags <small>One per line as category:value; unprefixed values use legacy.</small>
          <textarea name="manual_tags" rows="4">{escape(chr(10).join(f"{category}:{value}" for category, value in other_manual))}</textarea>
        </label>
      </section>
      <section class="panel"><h3>Imported tags</h3>{imported_tags}</section>
      <div class="actions">{previous_link}<button class="save" type="submit">Save &amp; next</button>{next_link}</div>
    </form>
    """


def _render_source(item: Any) -> str:
    rating = item.legacy_rating if item.legacy_rating is not None else "unrated"
    details = [f"Legacy rating: {rating}"]
    if item.genre:
        details.append(f"Genre: {item.genre}")
    comments = " · ".join(value for value in (item.comment, item.comment2) if value)
    if comments:
        details.append(f"Comments: {comments}")
    return f"<article><strong>{escape(item.source.title())}</strong><p>{escape(' · '.join(details))}</p></article>"


def _render_tags(tags: list[TrackTag], empty: str) -> str:
    if not tags:
        return f'<p class="muted">{escape(empty)}</p>'
    categories: dict[str, list[TrackTag]] = {}
    for tag in tags:
        categories.setdefault(tag.category, []).append(tag)
    return "".join(
        f'<div class="tag-group"><strong>{escape(category.title())}</strong>'
        + "".join(
            f'<span class="tag imported">{escape(tag.value)} <small>{escape(tag.source)}</small></span>'
            for tag in group
        )
        + "</div>"
        for category, group in sorted(categories.items())
    )


def _render_common_tag(
    category: str,
    value: str,
    label: str,
    key: str,
    manual_set: set[tuple[str, str]],
) -> str:
    field = f"tag_{category}_{value}"
    checked = "checked" if (category, value) in manual_set else ""
    return f'<label class="tag-toggle"><input type="checkbox" id="{field}" name="{field}" {checked}><span>{label} <kbd>{key}</kbd></span></label>'


def _scale(name: str, selected: int | None, unset: bool) -> str:
    choices = []
    if unset:
        choices.append(
            f'<label><input type="radio" name="{name}" value="" {"checked" if selected is None else ""}><span>Unset</span></label>'
        )
    for value in range(1, 6):
        choices.append(
            f'<label><input type="radio" name="{name}" value="{value}" {"checked" if selected == value else ""}><span>{value}</span></label>'
        )
    return "".join(choices)


def _navigation_link(path: str | None, mode: str, label: str, link_id: str) -> str:
    if path is None:
        return f'<span class="nav disabled" id="{link_id}">{label}</span>'
    href = f"/profiles?{urlencode({'mode': mode, 'path': path})}"
    return f'<a class="nav" id="{link_id}" href="{href}">{label}</a>'


def _duration(seconds: float | None) -> str:
    if seconds is None:
        return "Unknown duration"
    minutes, remainder = divmod(round(seconds), 60)
    return f"{minutes}:{remainder:02d}"


def _javascript() -> str:
    key_to_tag = {
        key.casefold(): f"tag_{category}_{value}"
        for category, value, _label, key in COMMON_TAGS
    }
    return f"""
    const keyToTag = {json.dumps(key_to_tag)};
    document.addEventListener('keydown', (event) => {{
      const target = event.target;
      if (target && target.closest('input, textarea, select, audio, button')) return;
      const key = event.key.toLowerCase();
      if (/^[1-5]$/.test(key)) {{
        const input = document.querySelector(`input[name="energy"][value="${{key}}"]`);
        if (input) {{ input.checked = true; event.preventDefault(); }}
      }} else if (keyToTag[key]) {{
        const input = document.getElementById(keyToTag[key]);
        if (input) {{ input.checked = !input.checked; event.preventDefault(); }}
      }} else if (key === 's' || event.key === 'Enter') {{
        document.getElementById('profile-form')?.requestSubmit(); event.preventDefault();
      }} else if (event.key === 'ArrowLeft') {{
        const link = document.getElementById('prev'); if (link?.href) location.href = link.href;
      }} else if (event.key === 'ArrowRight') {{
        const link = document.getElementById('next'); if (link?.href) location.href = link.href;
      }}
    }});
    """


def _css() -> str:
    return """
    :root { --ink:#18201f; --muted:#68706e; --line:#d6dcda; --paper:#fbfaf5; --accent:#0f766e; --warm:#eab308; }
    * { box-sizing:border-box } body { margin:0; color:var(--ink); background:var(--paper); font:15px/1.5 system-ui,sans-serif }
    header,nav,main,footer { width:min(1050px,calc(100% - 32px)); margin:auto } header { display:flex; justify-content:space-between; align-items:end; padding:24px 0 12px }
    header a { color:var(--accent); text-decoration:none } h1 { margin:2px 0 0 } nav { display:flex; gap:8px; padding:8px 0 20px; border-bottom:1px solid var(--line) }
    .mode,.nav { color:var(--ink); padding:8px 12px; border:1px solid var(--line); border-radius:999px; text-decoration:none; background:white }.mode.active { color:white;background:var(--accent);border-color:var(--accent) }
    main { padding:28px 0 70px }.track-grid { display:grid; grid-template-columns:250px 1fr; gap:28px }.cover { width:250px;height:250px;object-fit:cover;border-radius:12px;background:#e5e7e5 }.placeholder { display:grid;place-items:center;color:var(--muted) }
    audio { width:250px;margin-top:12px }.track-meta h2 { font-size:32px;margin:4px 0 }.track-meta h3 { font-size:20px;margin:0;color:var(--muted) }.eyebrow,.path,.muted,small { color:var(--muted) }.path { font:12px ui-monospace,monospace;word-break:break-all }
    .sources { display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:10px}.sources article,.panel { background:white;border:1px solid var(--line);border-radius:12px;padding:16px }.sources p { margin:5px 0 0 }
    form { display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-top:20px }.panel:last-of-type,.actions { grid-column:1/-1 }.panel h3 { margin:0 0 10px }.panel label { display:grid;gap:5px;margin:12px 0 }
    select,textarea { width:100%;padding:10px;border:1px solid var(--line);border-radius:8px;background:white;font:inherit }.scale,.tag-toggles { display:flex;gap:8px;flex-wrap:wrap }.scale label,.tag-toggle { margin:0!important;display:block!important }.scale input,.tag-toggle input { position:absolute;opacity:0 }
    .scale span,.tag-toggle span { display:block;padding:9px 14px;border:1px solid var(--line);border-radius:8px;cursor:pointer }.scale input:checked+span,.tag-toggle input:checked+span { color:white;background:var(--accent);border-color:var(--accent) }
    .tag-group { display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin:8px 0}.tag-group>strong { min-width:90px }.tag { padding:5px 9px;background:#edf2f0;border-radius:999px }.tag small { margin-left:3px }.actions { display:flex;justify-content:space-between;align-items:center }.save { padding:12px 24px;color:white;background:var(--accent);border:0;border-radius:9px;font-weight:700;cursor:pointer }.disabled { opacity:.4 }.error { padding:12px;background:#fee2e2;color:#991b1b;border-radius:8px }.empty { text-align:center;padding:80px 0 }.ok { color:var(--accent);font-weight:700 } footer { position:fixed;bottom:0;left:0;right:0;width:100%;padding:10px 16px;text-align:center;background:#17211f;color:white;font-size:12px } kbd { opacity:.7 }
    @media(max-width:700px) { .track-grid { grid-template-columns:1fr }.cover,audio { width:100% }.cover { height:auto;aspect-ratio:1 } form { grid-template-columns:1fr }.panel { grid-column:1!important } footer { position:static } }
    """
