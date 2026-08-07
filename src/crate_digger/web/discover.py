import json
import sqlite3
from dataclasses import asdict
from html import escape
from pathlib import Path
from typing import Any, Literal, cast
from urllib.parse import parse_qs, urlencode

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from requests.exceptions import RequestException
from spotipy.exceptions import SpotifyException

from crate_digger.discover.labels import normalize_label_name
from crate_digger.discover.listening import (
    SpotifyPlaylistAdapter,
    accept_playlist_snapshot,
    confirm_progress,
    end as end_listening_run,
    list_runs,
    preview as preview_listening_run,
    reconcile as reconcile_listening_run,
    start as start_listening_run,
)
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
    latest_open_session,
    list_session_items,
    list_sessions,
    record_feedback,
    record_session_review,
)
from crate_digger.discover.taste import rebuild_taste_index
from crate_digger.utils.config import get_settings
from crate_digger.utils.spotify import get_spotify_client


from crate_digger.web.templating import render_template


def create_discover_router(db_path: Path, config_path: str) -> APIRouter:
    router = APIRouter()

    def spotify_adapter() -> SpotifyPlaylistAdapter:
        settings = get_settings(config_path)
        return SpotifyPlaylistAdapter(
            get_spotify_client(" ".join(settings["spotify"]["scopes"]))
        )

    def listening_action(
        action: str,
        session_id: int,
        run_id: int | None = None,
        item_id: int | None = None,
    ) -> dict[str, Any]:
        if action == "confirm":
            if run_id is None:
                raise ValueError("Choose a listening run")
            return confirm_progress(db_path, run_id, item_id)
        settings = get_settings(config_path)
        spotify = spotify_adapter()
        if action == "accept-playlist":
            return accept_playlist_snapshot(db_path, spotify, settings)
        if action in ("preview", "preview-resume"):
            return preview_listening_run(
                db_path,
                spotify,
                settings,
                session_id,
                resume=action == "preview-resume",
            )
        if action in ("start", "resume"):
            return start_listening_run(
                db_path, spotify, settings, session_id, resume=action == "resume"
            )
        if run_id is None:
            raise ValueError("Choose a listening run")
        if action == "end":
            return end_listening_run(db_path, spotify, settings, run_id)
        if action == "reconcile":
            return reconcile_listening_run(db_path, spotify, run_id)
        raise ValueError("Unknown listening action")

    @router.get("/discover", response_class=HTMLResponse)
    def discover_page(
        session_id: int | None = None,
        run_id: int | None = None,
        notice: str | None = None,
    ) -> HTMLResponse:
        session = (
            get_session(db_path, session_id)
            if session_id is not None
            else (
                latest_open_session(db_path) or next(iter(list_sessions(db_path)), None)
            )
        )
        if session is None:
            return HTMLResponse(_render_empty_discover())
        items = list_session_items(db_path, session.session_id)
        return HTMLResponse(
            _render_discover(db_path, session, items, notice=notice, run_id=run_id)
        )

    @router.post("/discover/finish")
    async def finish_page(request: Request) -> RedirectResponse:
        form = parse_qs((await request.body()).decode("utf-8"), keep_blank_values=True)
        session_id: int | None = None
        try:
            session_id = _single_int(form, "session_id")
            unmarked = _single_value(form, "unmarked")
            if unmarked not in {"later", "skip"}:
                raise ValueError("Choose what to do with unmarked songs")
            choices: dict[int, Literal["keep", "skip"]] = {}
            for key, values in form.items():
                if not key.startswith("decision_"):
                    continue
                item_id = _int_value(key.removeprefix("decision_"))
                if len(values) != 1 or values[0] not in {"keep", "skip"}:
                    raise ValueError("Select only Keep or Skip for each song")
                choices[item_id] = cast(Literal["keep", "skip"], values[0])
            summary = record_session_review(
                db_path,
                session_id=session_id,
                choices=choices,
                unmarked=cast(Literal["later", "skip"], unmarked),
            )
            notice = (
                f"Saved {summary['keep']} Keep and {summary['skip']} Skip; "
                f"{summary['later']} left for later."
            )
            active = next(
                (
                    run
                    for run in list_runs(db_path, session_id)
                    if run["state"] != "ended"
                ),
                None,
            )
            if active is not None:
                try:
                    result = listening_action("end", session_id, active["id"])
                    notice += f" Playlist cleared; run {result['id']} ended."
                except (
                    KeyError,
                    ValueError,
                    RuntimeError,
                    OSError,
                    sqlite3.Error,
                    RequestException,
                    SpotifyException,
                ) as error:
                    notice += (
                        f" Reviews are saved; playlist cleanup needs a retry: {error}"
                    )
        except (KeyError, ValueError, sqlite3.Error) as error:
            notice = f"Review was not saved: {error}"
        query: dict[str, object] = {"notice": notice}
        if session_id is not None:
            query["session_id"] = session_id
        return RedirectResponse(f"/discover?{urlencode(query)}", status_code=303)

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

    @router.post("/discover/listening")
    async def listening_page(request: Request) -> Response:
        form = _parse_form(await request.body())
        session_id = _int_value(form.get("session_id"))
        action = form.get("action", "")
        try:
            result = listening_action(
                action,
                session_id,
                _int_value(form["run_id"]) if form.get("run_id") else None,
                _int_value(form["item_id"]) if form.get("item_id") else None,
            )
            if action in ("preview", "preview-resume"):
                entries = "".join(
                    f'<li>{row["position"]}. {escape(row["title"])} · {escape(row["track_uri"])}</li>'
                    for row in result["items"]
                )
                unavailable = "".join(
                    f'<li>{row["position"]}. {escape(row["title"])} · {escape(row["reason"])}</li>'
                    for row in result["unavailable"]
                )
                next_action = "resume" if action == "preview-resume" else "start"
                body = (
                    f'<p><a href="/discover?session_id={session_id}">Back to session</a></p>'
                    f'<p>Playlist: <a href="{escape(result["playlist_url"], quote=True)}">{escape(result["playlist_uri"])}</a></p>'
                    f'<p>Resume after position {result["resume_after_position"]}. No Spotify tracks were changed.</p>'
                    f'<h2>Publishable ({len(result["items"])})</h2><ol>{entries}</ol>'
                    f'<h2>Unavailable ({len(result["unavailable"])})</h2><ul>{unavailable}</ul>'
                    f'<form method="post" action="/discover/listening"><input type="hidden" name="session_id" value="{session_id}">'
                    f'<button name="action" value="{next_action}">{next_action.title()} run</button></form>'
                )
                return HTMLResponse(_shell("Spotify listening preview", body))
            notice = (
                f"Playlist snapshot accepted: {result['state']}"
                if action == "accept-playlist"
                else f"Run {result['id']}: {result['state']}"
            )
        except (
            KeyError,
            ValueError,
            RuntimeError,
            OSError,
            sqlite3.Error,
            RequestException,
            SpotifyException,
        ) as error:
            notice = f"Spotify listening action failed: {error}"
        return RedirectResponse(
            f"/discover?{urlencode({'session_id': session_id, 'notice': notice})}",
            status_code=303,
        )

    @router.get("/api/discover/sessions/{session_id}/listening")
    def listening_runs_api(session_id: int) -> JSONResponse:
        if get_session(db_path, session_id) is None:
            return JSONResponse({"detail": "Session not found"}, status_code=404)
        return JSONResponse({"runs": list_runs(db_path, session_id)})

    @router.post("/api/discover/sessions/{session_id}/listening/{action}")
    async def listening_action_api(
        request: Request, session_id: int, action: str
    ) -> JSONResponse:
        try:
            payload = await _json_object(request)
            result = listening_action(
                action,
                session_id,
                _int_value(payload["run_id"])
                if payload.get("run_id") is not None
                else None,
                _int_value(payload["item_id"])
                if payload.get("item_id") is not None
                else None,
            )
            return JSONResponse(result)
        except (
            KeyError,
            ValueError,
            RuntimeError,
            OSError,
            sqlite3.Error,
            RequestException,
            SpotifyException,
        ) as error:
            return JSONResponse({"detail": str(error)}, status_code=409)

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
    return render_template("discover_empty.html", title="Discovery", javascript=False)


def _render_discover(
    db_path: Path,
    session: Any,
    items: list[Any],
    *,
    notice: str | None,
    run_id: int | None = None,
) -> str:
    runs = list_runs(db_path, session.session_id)
    run = (
        next((entry for entry in runs if entry["id"] == run_id), None)
        if run_id
        else None
    )
    if run is None:
        run = runs[0] if runs else None
    pending = sum(item.decision is None for item in items)
    return render_template(
        "discover_session.html",
        title="Discovery",
        javascript=bool(items),
        session=session,
        items=items,
        run=run,
        pending=pending,
        decided=len(items) - pending,
        notice=notice,
    )


def _shell(title: str, body: str, javascript: bool = False) -> str:
    return render_template(
        "discover.html", title=title, body=body, javascript=javascript
    )


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


def _single_value(form: dict[str, list[str]], key: str) -> str:
    values = form.get(key, [])
    if len(values) != 1:
        raise ValueError(f"Expected one {key} value")
    return values[0]


def _single_int(form: dict[str, list[str]], key: str) -> int:
    return _int_value(_single_value(form, key))


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
