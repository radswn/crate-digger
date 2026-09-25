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

from crate_digger.collection.dj_curation import (
    CHARACTER_TAGS,
    VOCAL_PRESENCE,
    curation_history,
    get_curation,
    save_curation,
)
from crate_digger.collection.genre_review import TAXONOMY
from crate_digger.collection.intake import (
    CheckKey,
    intake_history,
    list_intake_checks,
    review_intake_check,
)
from crate_digger.collection.tag_publish import apply_tags, preview_tags
from crate_digger.collection.traktor_organization import CATEGORIES
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
    feedback_history,
    get_session,
    get_session_item,
    latest_open_session,
    list_session_items,
    list_sessions,
    record_feedback,
    record_session_review,
)
from crate_digger.discover.taste import rebuild_taste_index
from crate_digger.discover.wishlist import (
    confirm_local_match,
    import_playlist,
    list_wishlist,
    playlist_import_preview,
    preview_local_match,
    search_local_matches,
    set_progress,
    set_wanted,
    wishlist_events,
    wishlist_statuses,
)
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
            wants: dict[int, bool] = {}
            for key, values in form.items():
                if key.startswith("want_"):
                    item_id = _int_value(key.removeprefix("want_"))
                    if values not in (["0"], ["1"], ["0", "1"]):
                        raise ValueError("Invalid Want choice")
                    wants[item_id] = values[-1] == "1"
                    continue
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
                wants=wants,
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

    @router.get("/wishlist", response_class=HTMLResponse)
    def wishlist_page(
        notice: str | None = None, show_removed: bool = False
    ) -> HTMLResponse:
        rows = list_wishlist(db_path, include_removed=show_removed)
        for row in rows:
            row["events"] = wishlist_events(db_path, str(row["spotify_track_id"]))
        return HTMLResponse(
            render_template(
                "wishlist.html",
                title="Wishlist",
                javascript=False,
                tracks=rows,
                notice=notice,
                show_removed=show_removed,
            )
        )

    @router.get("/curate", response_class=HTMLResponse)
    def curation_page(path: str, notice: str | None = None) -> HTMLResponse:
        try:
            track = get_curation(db_path, path)
        except KeyError:
            return HTMLResponse("Indexed track not found", status_code=404)
        return HTMLResponse(
            render_template(
                "dj_curation.html",
                title="Curation",
                javascript=False,
                track=track,
                notice=notice,
                history=curation_history(db_path, path),
                intake_checks=list_intake_checks(db_path, path),
                intake_history=intake_history(db_path, path),
                taxonomy=TAXONOMY,
                character_tags=CHARACTER_TAGS,
                vocal_options=VOCAL_PRESENCE,
                categories=CATEGORIES,
            )
        )

    @router.post("/curate/check")
    async def intake_check_page(request: Request) -> RedirectResponse:
        form = _parse_form(await request.body())
        path = form.get("path", "")
        try:
            changed = review_intake_check(
                db_path,
                path,
                cast(CheckKey, form["check_key"]),
                form["decision"],
                fingerprint=form["fingerprint"],
                confirmed=form.get("confirmed") == "yes",
                note=form.get("note", ""),
            )
            notice = "Intake check saved" if changed else "Intake check already saved"
        except (KeyError, ValueError, sqlite3.Error) as error:
            notice = f"Intake check was not saved: {error}"
        return RedirectResponse(
            f"/curate?{urlencode({'path': path, 'notice': notice})}", status_code=303
        )

    @router.get("/curate/tags", response_class=HTMLResponse)
    def tag_preview_page(path: str, notice: str | None = None) -> HTMLResponse:
        try:
            preview = preview_tags(db_path, path)
        except (KeyError, ValueError, OSError) as error:
            return HTMLResponse(
                f"Tag preview unavailable: {escape(str(error))}", status_code=400
            )
        return HTMLResponse(
            render_template(
                "dj_tag_preview.html",
                title="File tag preview",
                javascript=False,
                preview=preview,
                notice=notice,
            )
        )

    @router.post("/curate/tags")
    async def tag_publish_page(request: Request) -> RedirectResponse:
        form = _parse_form(await request.body())
        path = form.get("path", "")
        try:
            if form.get("confirmed") != "yes":
                raise ValueError("Confirm the file tag changes")
            result = apply_tags(db_path, path, form.get("fingerprint", ""))
            notice = (
                f"File tags published. Exact backup: {result['backup']}"
                if result["changed"]
                else "File tags already match the reviewed values"
            )
        except (KeyError, ValueError, OSError, sqlite3.Error) as error:
            notice = f"File tags were not published: {error}"
        return RedirectResponse(
            f"/curate/tags?{urlencode({'path': path, 'notice': notice})}",
            status_code=303,
        )

    @router.post("/curate")
    async def curation_save_page(request: Request) -> RedirectResponse:
        form = parse_qs((await request.body()).decode("utf-8"), keep_blank_values=True)
        path = form.get("path", [""])[-1]
        try:
            if not path:
                raise ValueError("Choose an indexed track")
            changed = save_curation(
                db_path,
                path,
                genre=_optional_string(form.get("genre", [""])[-1]),
                energy=_optional_int(form.get("energy", [""])[-1]),
                tone=_optional_int(form.get("tone", [""])[-1]),
                character=form.get("character", []),
                vocal_presence=_optional_string(form.get("vocal_presence", [""])[-1]),
                collection_category=_optional_string(
                    form.get("collection_category", [""])[-1]
                ),
            )
            notice = (
                "Reviewed DJ metadata saved" if changed else "No DJ metadata changes"
            )
        except (KeyError, ValueError, sqlite3.Error) as error:
            notice = f"DJ metadata was not saved: {error}"
        return RedirectResponse(
            f"/curate?{urlencode({'path': path, 'notice': notice})}", status_code=303
        )

    @router.get("/wishlist/{spotify_track_id}/match", response_class=HTMLResponse)
    def wishlist_match_page(
        spotify_track_id: str,
        q: str = "",
        path: str | None = None,
        notice: str | None = None,
    ) -> HTMLResponse:
        try:
            track = next(
                row
                for row in list_wishlist(db_path)
                if row["spotify_track_id"] == spotify_track_id
            )
            matches = search_local_matches(db_path, spotify_track_id, q)
            selected = (
                preview_local_match(db_path, spotify_track_id, path) if path else None
            )
            return HTMLResponse(
                render_template(
                    "wishlist_match.html",
                    title="Match local file",
                    javascript=False,
                    track=track,
                    matches=matches,
                    selected=selected,
                    q=q,
                    notice=notice,
                )
            )
        except StopIteration:
            return HTMLResponse("Wishlist track not found", status_code=404)
        except (KeyError, ValueError) as error:
            return HTMLResponse(
                render_template(
                    "wishlist_match.html",
                    title="Match local file",
                    javascript=False,
                    track=None,
                    matches=[],
                    selected=None,
                    q=q,
                    notice=str(error),
                ),
                status_code=409,
            )

    @router.post("/wishlist/{spotify_track_id}/match")
    async def wishlist_match_confirm_page(
        request: Request, spotify_track_id: str
    ) -> RedirectResponse:
        form = _parse_form(await request.body())
        try:
            changed = confirm_local_match(
                db_path,
                spotify_track_id,
                form["path"],
                form["fingerprint"],
                verified=form.get("verified") == "yes",
            )
            notice = (
                "Local recording linked; review its curation before export"
                if changed
                else "Local recording was already linked"
            )
        except (KeyError, ValueError, sqlite3.Error) as error:
            notice = f"Local match was not saved: {error}"
            return RedirectResponse(
                f"/wishlist/{spotify_track_id}/match?{urlencode({'q': form.get('q', ''), 'notice': notice})}",
                status_code=303,
            )
        return RedirectResponse(
            f"/wishlist?{urlencode({'notice': notice})}", status_code=303
        )

    @router.post("/wishlist/update")
    async def wishlist_update_page(request: Request) -> RedirectResponse:
        form = _parse_form(await request.body())
        try:
            track_id = form["spotify_track_id"]
            action = form["action"]
            if action == "remove":
                changed = set_wanted(db_path, track_id, False)
            elif action == "restore":
                changed = set_wanted(db_path, track_id, True)
            elif action in {"searching", "unavailable", "wanted"}:
                changed = set_progress(
                    db_path, track_id, cast(Any, action), note=form.get("note") or None
                )
            else:
                raise ValueError("Unknown wishlist action")
            notice = "Wishlist updated" if changed else "No change needed"
        except (KeyError, ValueError, sqlite3.Error) as error:
            notice = f"Wishlist was not updated: {error}"
        return RedirectResponse(
            f"/wishlist?{urlencode({'notice': notice})}", status_code=303
        )

    @router.post("/wishlist/import-preview", response_class=HTMLResponse)
    def wishlist_import_preview_page() -> HTMLResponse:
        try:
            playlist_uri = get_settings(config_path)["spotify"]["to_download_playlist"]
            preview = playlist_import_preview(spotify_adapter().client, playlist_uri)
            statuses = wishlist_statuses(
                db_path, [track.spotify_track_id for track in preview["tracks"]]
            )
            preview["new_count"] = sum(
                track.spotify_track_id not in statuses for track in preview["tracks"]
            )
            return HTMLResponse(
                render_template(
                    "wishlist_import.html",
                    title="Import preview",
                    javascript=False,
                    preview=preview,
                    playlist_uri=playlist_uri,
                )
            )
        except (
            KeyError,
            ValueError,
            RuntimeError,
            RequestException,
            SpotifyException,
        ) as error:
            return HTMLResponse(
                _shell(
                    "Import preview",
                    f"<main id='main'><p>{escape(str(error))}</p><a href='/wishlist'>Back to wishlist</a></main>",
                ),
                status_code=409,
            )

    @router.post("/wishlist/import")
    async def wishlist_import_page(request: Request) -> RedirectResponse:
        form = _parse_form(await request.body())
        try:
            settings = get_settings(config_path)
            if form["playlist_uri"] != settings["spotify"]["to_download_playlist"]:
                raise ValueError(
                    "Configured playlist changed since preview; preview it again"
                )
            result = import_playlist(
                spotify_adapter().client,
                db_path,
                settings["spotify"]["to_download_playlist"],
                fingerprint=form["fingerprint"],
                label_aliases=settings["discovery"]["label_aliases"],
            )
            notice = f"Imported {result['added']} wanted tracks; {result['already_present']} already present, {result['skipped']} skipped."
        except (
            KeyError,
            ValueError,
            RuntimeError,
            sqlite3.Error,
            RequestException,
            SpotifyException,
        ) as error:
            notice = f"Playlist import failed: {error}"
        return RedirectResponse(
            f"/wishlist?{urlencode({'notice': notice})}", status_code=303
        )

    @router.get("/api/wishlist")
    def wishlist_api(include_removed: bool = False) -> dict[str, object]:
        return {"tracks": list_wishlist(db_path, include_removed=include_removed)}

    @router.post("/api/wishlist/{spotify_track_id}")
    async def wishlist_update_api(
        request: Request, spotify_track_id: str
    ) -> JSONResponse:
        try:
            payload = await _json_object(request)
            if not isinstance(payload.get("wanted"), bool):
                raise ValueError("wanted must be a boolean")
            changed = set_wanted(db_path, spotify_track_id, payload["wanted"])
            return JSONResponse(
                {
                    "changed": changed,
                    "status": wishlist_statuses(db_path, [spotify_track_id]).get(
                        spotify_track_id
                    ),
                }
            )
        except KeyError as error:
            return JSONResponse({"detail": str(error)}, status_code=404)
        except ValueError as error:
            return JSONResponse({"detail": str(error)}, status_code=400)

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
                    f"<li>{row['position']}. {escape(row['title'])} · {escape(row['track_uri'])}</li>"
                    for row in result["items"]
                )
                unavailable = "".join(
                    f"<li>{row['position']}. {escape(row['title'])} · {escape(row['reason'])}</li>"
                    for row in result["unavailable"]
                )
                next_action = "resume" if action == "preview-resume" else "start"
                body = (
                    f'<p><a href="/discover?session_id={session_id}">Back to session</a></p>'
                    f'<p>Playlist: <a href="{escape(result["playlist_url"], quote=True)}">{escape(result["playlist_uri"])}</a></p>'
                    f"<p>Resume after position {result['resume_after_position']}. No Spotify tracks were changed.</p>"
                    f"<h2>Publishable ({len(result['items'])})</h2><ol>{entries}</ol>"
                    f"<h2>Unavailable ({len(result['unavailable'])})</h2><ul>{unavailable}</ul>"
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

    @router.get("/api/discover/sessions/{session_id}/items/{item_id}/history")
    def feedback_history_api(session_id: int, item_id: int) -> JSONResponse:
        try:
            return JSONResponse(
                {"events": feedback_history(db_path, session_id, item_id)}
            )
        except KeyError as error:
            return JSONResponse({"detail": str(error)}, status_code=404)

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
    hearing: dict[int, str] = {}
    for entry in runs:
        for listened_item in entry["items"]:
            item_id = int(listened_item["item_id"])
            progress = str(listened_item["progress"])
            if progress == "confirmed heard" or (
                progress == "observed heard"
                and hearing.get(item_id) != "confirmed heard"
            ):
                hearing[item_id] = progress
    return render_template(
        "discover_session.html",
        title="Discovery",
        javascript=bool(items),
        session=session,
        items=items,
        run=run,
        pending=pending,
        decided=len(items) - pending,
        hearing=hearing,
        wishlist_statuses=wishlist_statuses(
            db_path, [item.track.spotify_track_id for item in items]
        ),
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


def _optional_int(value: str) -> int | None:
    return _int_value(value) if value.strip() else None
