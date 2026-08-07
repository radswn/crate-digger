import json
from dataclasses import asdict
from html import escape
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qs, urlencode

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from crate_digger.discover.labels import normalize_label_name
from crate_digger.discover.models import Decision, SessionMode
from crate_digger.discover.repository import (
    discovery_counts,
    find_affinity,
    get_affinities,
)
from crate_digger.discover.sessions import (
    build_session,
    expand_release,
    explore_label,
    get_session,
    get_session_item,
    item_media,
    latest_open_session,
    list_session_items,
    list_sessions,
    record_feedback,
)
from crate_digger.discover.taste import rebuild_taste_index
from crate_digger.utils.config import get_settings


def create_discover_router(db_path: Path, config_path: str) -> APIRouter:
    router = APIRouter()

    @router.get("/discover", response_class=HTMLResponse)
    def discover_page(
        session_id: int | None = None,
        item_id: int | None = None,
        notice: str | None = None,
    ) -> HTMLResponse:
        session = (
            get_session(db_path, session_id)
            if session_id is not None
            else latest_open_session(db_path)
        )
        if session is None:
            return HTMLResponse(_render_empty_discover())
        items = list_session_items(db_path, session.session_id)
        item = next((entry for entry in items if entry.item_id == item_id), None)
        if item is None:
            item = next((entry for entry in items if entry.decision is None), None)
        if item is None and items:
            item = items[0]
        return HTMLResponse(
            _render_discover(db_path, session, items, item, notice=notice)
        )

    @router.post("/discover/build")
    async def build_page(request: Request) -> RedirectResponse:
        form = _parse_form(await request.body())
        settings = get_settings(config_path)
        mode = _mode(form.get("mode", "balanced"))
        size = _int_value(form.get("size"), default=30)
        result = build_session(
            db_path,
            mode=mode,
            size=size,
            freshness_days=settings["discovery"]["freshness_days"],
        )
        return RedirectResponse(
            f"/discover?{urlencode({'session_id': result.session.session_id})}",
            status_code=303,
        )

    @router.post("/discover/feedback")
    async def feedback_page(request: Request) -> RedirectResponse:
        form = _parse_form(await request.body())
        session_id = _int_value(form.get("session_id"))
        item_id = _int_value(form.get("item_id"))
        decision = _decision(form.get("decision", ""))
        record_feedback(
            db_path, session_id=session_id, item_id=item_id, decision=decision
        )
        items = list_session_items(db_path, session_id)
        next_item = next((item for item in items if item.decision is None), None)
        query: dict[str, object] = {
            "session_id": session_id,
            "notice": decision.title(),
        }
        if next_item is not None:
            query["item_id"] = next_item.item_id
        return RedirectResponse(f"/discover?{urlencode(query)}", status_code=303)

    @router.post("/discover/expand-release")
    async def expand_page(request: Request) -> RedirectResponse:
        form = _parse_form(await request.body())
        session_id = _int_value(form.get("session_id"))
        item_id = _int_value(form.get("item_id"))
        count = len(expand_release(db_path, session_id=session_id, item_id=item_id))
        return RedirectResponse(
            f"/discover?{urlencode({'session_id': session_id, 'item_id': item_id, 'notice': f'Expanded release: {count} tracks'})}",
            status_code=303,
        )

    @router.post("/discover/explore-label")
    async def explore_page(request: Request) -> RedirectResponse:
        form = _parse_form(await request.body())
        session_id = _int_value(form.get("session_id"))
        item_id = _int_value(form.get("item_id"))
        count = len(explore_label(db_path, session_id=session_id, item_id=item_id))
        return RedirectResponse(
            f"/discover?{urlencode({'session_id': session_id, 'item_id': item_id, 'notice': f'Label sampler: {count} releases'})}",
            status_code=303,
        )

    @router.post("/api/discover/rebuild-taste")
    def rebuild_taste_api() -> dict[str, object]:
        return asdict(rebuild_taste_index(db_path))

    @router.get("/api/discover/taste-stats")
    def taste_stats_api(
        label: str | None = None, artist: str | None = None
    ) -> JSONResponse:
        if label and artist:
            return JSONResponse(
                {"detail": "Choose either label or artist"}, status_code=400
            )
        if label:
            settings = get_settings(config_path)
            canonical_label = normalize_label_name(
                label, settings["discovery"]["label_aliases"]
            ).display_name
            affinity = find_affinity(db_path, entity_type="label", name=canonical_label)
            return JSONResponse(asdict(affinity) if affinity else None)
        if artist:
            affinity = find_affinity(db_path, entity_type="artist", name=artist)
            return JSONResponse(asdict(affinity) if affinity else None)
        return JSONResponse(
            {
                entity_type: [
                    asdict(item) for item in get_affinities(db_path, entity_type)
                ]
                for entity_type in ("artist", "label", "tag", "source")
            }
        )

    @router.get("/api/discover/sessions")
    def sessions_api() -> list[dict[str, object]]:
        return [asdict(session) for session in list_sessions(db_path)]

    @router.get("/api/discover/sessions/{session_id}")
    def session_api(session_id: int) -> JSONResponse:
        session = get_session(db_path, session_id)
        if session is None:
            return JSONResponse({"detail": "Session not found"}, status_code=404)
        return JSONResponse(
            {
                "session": asdict(session),
                "items": [
                    _item_json(item) for item in list_session_items(db_path, session_id)
                ],
            }
        )

    @router.post("/api/discover/sessions")
    async def create_session_api(request: Request) -> JSONResponse:
        try:
            payload = await _json_object(request)
            settings = get_settings(config_path)
            requested_label = _optional_string(payload.get("label"))
            label_filter = (
                normalize_label_name(
                    requested_label, settings["discovery"]["label_aliases"]
                ).display_name
                if requested_label
                else None
            )
            result = build_session(
                db_path,
                mode=_mode(str(payload.get("mode", "balanced"))),
                size=_int_value(payload.get("size"), default=30),
                seed=_int_value(payload.get("seed"), default=0),
                freshness_days=settings["discovery"]["freshness_days"],
                label_filter=label_filter,
                artist_filter=_optional_string(payload.get("artist")),
            )
        except ValueError as error:
            return JSONResponse({"detail": str(error)}, status_code=400)
        return JSONResponse(
            {
                "session": asdict(result.session),
                "items": [_item_json(item) for item in result.items],
                "bucket_counts": result.bucket_counts,
            },
            status_code=201,
        )

    @router.get("/api/discover/sessions/{session_id}/items/{item_id}/explain")
    def explain_api(session_id: int, item_id: int) -> JSONResponse:
        item = get_session_item(db_path, session_id, item_id)
        if item is None:
            return JSONResponse({"detail": "Session item not found"}, status_code=404)
        return JSONResponse(
            {
                "score": item.score_at_selection,
                "affinity": item.affinity_at_selection,
                "reasons": list(item.reasons_at_selection),
            }
        )

    @router.post("/api/discover/sessions/{session_id}/items/{item_id}/feedback")
    async def feedback_api(
        request: Request, session_id: int, item_id: int
    ) -> JSONResponse:
        try:
            payload = await _json_object(request)
            decision = _decision(str(payload.get("decision", "")))
            item = record_feedback(
                db_path,
                session_id=session_id,
                item_id=item_id,
                decision=decision,
            )
        except KeyError as error:
            return JSONResponse({"detail": str(error)}, status_code=404)
        except ValueError as error:
            return JSONResponse({"detail": str(error)}, status_code=400)
        return JSONResponse(_item_json(item))

    @router.post("/api/discover/sessions/{session_id}/items/{item_id}/expand-release")
    def expand_api(session_id: int, item_id: int) -> JSONResponse:
        try:
            candidate_ids = expand_release(
                db_path, session_id=session_id, item_id=item_id
            )
        except KeyError as error:
            return JSONResponse({"detail": str(error)}, status_code=404)
        except ValueError as error:
            return JSONResponse({"detail": str(error)}, status_code=400)
        return JSONResponse({"candidate_ids": candidate_ids})

    @router.post("/api/discover/sessions/{session_id}/items/{item_id}/explore-label")
    def explore_api(session_id: int, item_id: int) -> JSONResponse:
        try:
            candidate_ids = explore_label(
                db_path, session_id=session_id, item_id=item_id
            )
        except KeyError as error:
            return JSONResponse({"detail": str(error)}, status_code=404)
        except ValueError as error:
            return JSONResponse({"detail": str(error)}, status_code=400)
        return JSONResponse({"candidate_ids": candidate_ids})

    @router.get("/api/discover/stats")
    def discovery_stats_api() -> dict[str, object]:
        return discovery_counts(db_path)

    return router


def _render_empty_discover() -> str:
    body = """
    <section class="empty"><h2>No discovery session yet</h2>
      <p>Index the existing catalogue and rebuild taste, then create a finite session.</p>
      <form method="post" action="/discover/build">
        <select name="mode"><option>balanced</option><option>fresh</option><option>deep-dig</option><option>frontier</option></select>
        <input name="size" type="number" min="1" max="100" value="30">
        <button type="submit">Build session</button>
      </form>
    </section>
    """
    return _shell("Taste-Aware Discovery Sessions", body, "")


def _render_discover(
    db_path: Path,
    session: Any,
    items: list[Any],
    item: Any,
    *,
    notice: str | None,
) -> str:
    if item is None:
        return _shell("Discovery", "<p>This session has no items.</p>", "")
    media = item_media(db_path, item.item_id)
    taste_summary = {
        entity_type: [
            affinity
            for affinity in get_affinities(db_path, entity_type)
            if affinity.sample_size > 0
        ][:5]
        for entity_type in ("artist", "label")
    }
    return _render_with_media(
        session,
        items,
        item,
        sum(entry.decision is not None for entry in items),
        notice,
        media,
        taste_summary,
    )


def _render_with_media(
    session: Any,
    items: list[Any],
    item: Any,
    decided: int,
    notice: str | None,
    media: dict[str, str | None] | None,
    taste_summary: dict[str, list[Any]],
) -> str:
    index = items.index(item)
    previous_item = items[index - 1] if index > 0 else None
    next_item = items[index + 1] if index + 1 < len(items) else None
    notice_html = f'<p class="notice">{escape(notice)}</p>' if notice else ""
    reasons = "".join(
        f"<li>{escape(reason)}</li>" for reason in item.reasons_at_selection
    )
    audio = ""
    spotify_link = ""
    if media:
        if media.get("local_track_path"):
            audio = f'<audio id="preview" controls preload="metadata" src="/profiles/audio?{urlencode({"path": media["local_track_path"]})}"></audio>'
        elif media.get("preview_url"):
            audio = f'<audio id="preview" controls preload="metadata" src="{escape(media["preview_url"], quote=True)}"></audio>'
        if media.get("external_url"):
            spotify_link = f'<a target="_blank" rel="noreferrer" href="{escape(media["external_url"], quote=True)}">Open in Spotify</a>'
    controls = "".join(
        f'<button name="decision" value="{decision}" class="{decision}">{label} <kbd>{key}</kbd></button>'
        for decision, label, key in (
            ("keep", "Keep", "K"),
            ("maybe", "Maybe", "M"),
            ("pass", "Pass", "P"),
            ("skip", "Skip", "S"),
        )
    )
    navigation = "".join(
        _item_link(session.session_id, neighbor, label, link_id)
        for neighbor, label, link_id in (
            (previous_item, "← Previous", "previous"),
            (next_item, "Next →", "next"),
        )
    )
    affinity_html = "".join(
        f'<section class="affinity"><h4>Top {escape(entity_type)} affinities</h4><ul>'
        + "".join(
            f"<li>{escape(affinity.entity_name)} — {affinity.smoothed_affinity:.0%} <small>({affinity.sample_size} reviewed)</small></li>"
            for affinity in affinities
        )
        + "</ul></section>"
        for entity_type, affinities in taste_summary.items()
        if affinities
    )
    body = f"""
    {notice_html}
    <section class="session-head"><div><strong>{escape(session.mode)}</strong> session #{session.session_id}</div>
      <div>{decided}/{len(items)} decided · item {item.position}/{len(items)}</div></section>
    <article class="card">
      <p class="eyebrow">{escape(item.bucket)} · score {item.score_at_selection:.1f}</p>
      <h2>{escape(item.track.title)}</h2><h3>{escape(item.track.artist_name)}</h3>
      <p>{escape(item.track.release_title or "Unknown release")} · {escape(item.track.label_name or "Unknown label")} · {escape(item.track.release_date or "Unknown date")}</p>
      {audio}<p>{spotify_link}</p>
      <h4>Why selected</h4><ul>{reasons}</ul>
      <form id="feedback" method="post" action="/discover/feedback">
        <input type="hidden" name="session_id" value="{session.session_id}"><input type="hidden" name="item_id" value="{item.item_id}">
        <div class="feedback">{controls}</div>
      </form>
      <div class="expansions">
        <form method="post" action="/discover/expand-release"><input type="hidden" name="session_id" value="{session.session_id}"><input type="hidden" name="item_id" value="{item.item_id}"><button>Expand release <kbd>E</kbd></button></form>
        <form method="post" action="/discover/explore-label"><input type="hidden" name="session_id" value="{session.session_id}"><input type="hidden" name="item_id" value="{item.item_id}"><button>Explore label <kbd>L</kbd></button></form>
      </div>
    </article><nav class="navigation">{navigation}</nav><aside class="taste-summary">{affinity_html}</aside>
    """
    return _shell("Taste-Aware Discovery Sessions", body, _javascript())


def _shell(title: str, body: str, javascript: str) -> str:
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{escape(title)} · Crate Digger</title><style>{_css()}</style></head>
    <body><header><a href="/">Crate Digger</a><h1>{escape(title)}</h1><a href="/profiles">Track Profiles</a></header><main>{body}</main><footer>K Keep · M Maybe · P Pass · S Skip · E Expand · L Explore · Space play/pause</footer><script>{javascript}</script></body></html>"""


def _item_link(session_id: int, item: Any, label: str, link_id: str) -> str:
    if item is None:
        return f'<span class="disabled" id="{link_id}">{label}</span>'
    href = f"/discover?{urlencode({'session_id': session_id, 'item_id': item.item_id})}"
    return f'<a id="{link_id}" href="{href}">{label}</a>'


def _javascript() -> str:
    return """
    document.addEventListener('keydown', event => {
      const target = event.target;
      if (target && target.closest('input, textarea, select, audio, button')) return;
      const key = event.key.toLowerCase();
      const decisions = {k:'keep',m:'maybe',p:'pass',s:'skip'};
      if (decisions[key]) { document.querySelector(`button[value="${decisions[key]}"]`)?.click(); event.preventDefault(); }
      else if (key === 'e') { document.querySelector('form[action="/discover/expand-release"] button')?.click(); event.preventDefault(); }
      else if (key === 'l') { document.querySelector('form[action="/discover/explore-label"] button')?.click(); event.preventDefault(); }
      else if (event.code === 'Space') { const audio=document.getElementById('preview'); if(audio){audio.paused?audio.play():audio.pause();event.preventDefault();} }
    });
    """


def _css() -> str:
    return """
    :root{--ink:#17201f;--muted:#68716f;--line:#d6dcda;--paper:#f7f6f0;--accent:#0f766e}*{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--ink);font:15px/1.5 system-ui,sans-serif}header,main,footer{width:min(900px,calc(100% - 32px));margin:auto}header{display:flex;align-items:center;gap:18px;padding:22px 0;border-bottom:1px solid var(--line)}header h1{flex:1;margin:0;font-size:25px}a{color:var(--accent)}main{padding:24px 0 72px}.session-head,.navigation,.feedback,.expansions{display:flex;justify-content:space-between;gap:10px;flex-wrap:wrap}.card,.empty,.affinity{background:white;border:1px solid var(--line);border-radius:14px;padding:24px;margin-top:16px}.card h2{font-size:32px;margin:3px 0}.card h3{color:var(--muted);margin:0}.eyebrow{color:var(--accent);font-weight:700;text-transform:uppercase;font-size:12px}.feedback{justify-content:flex-start}.feedback button,.expansions button,.empty button,.empty input,.empty select{padding:10px 15px;border:1px solid var(--line);border-radius:8px;background:white;font:inherit}.feedback .keep{background:#d1fae5}.feedback .maybe{background:#fef3c7}.feedback .pass{background:#fee2e2}.feedback .skip{background:#e5e7eb}.expansions{justify-content:flex-start;margin-top:16px}.navigation{margin-top:16px}.taste-summary{display:grid;grid-template-columns:1fr 1fr;gap:12px}.affinity h4{margin-top:0}.notice{padding:10px;background:#d1fae5;border-radius:8px}audio{width:100%;margin-top:12px}.disabled{opacity:.4}footer{position:fixed;bottom:0;left:0;right:0;width:100%;padding:10px;text-align:center;background:#17201f;color:white;font-size:12px}kbd{opacity:.65}@media(max-width:650px){.taste-summary{grid-template-columns:1fr}}
    """


def _item_json(item: Any) -> dict[str, object]:
    data = asdict(item)
    media = data.pop("track")
    data["track"] = media
    return data


async def _json_object(request: Request) -> dict[str, Any]:
    try:
        payload = await request.json()
    except json.JSONDecodeError as error:
        raise ValueError("Invalid JSON body") from error
    if not isinstance(payload, dict):
        raise ValueError("JSON body must be an object")
    return cast(dict[str, Any], payload)


def _parse_form(body: bytes) -> dict[str, str]:
    parsed = parse_qs(body.decode("utf-8"), keep_blank_values=True)
    return {key: values[-1] for key, values in parsed.items() if values}


def _mode(value: str) -> SessionMode:
    if value not in {"balanced", "fresh", "deep-dig", "frontier"}:
        raise ValueError(f"Invalid discovery mode: {value}")
    return cast(SessionMode, value)


def _decision(value: str) -> Decision:
    if value not in {"keep", "maybe", "pass", "skip"}:
        raise ValueError(f"Invalid feedback decision: {value}")
    return cast(Decision, value)


def _int_value(value: object, *, default: int | None = None) -> int:
    if value is None and default is not None:
        return default
    if isinstance(value, bool):
        raise ValueError("Expected an integer")
    try:
        return int(str(value))
    except (TypeError, ValueError) as error:
        raise ValueError("Expected an integer") from error


def _optional_string(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None
