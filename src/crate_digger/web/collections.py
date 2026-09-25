"""Dashboard forms for saved DJ collection rules and their offline previews."""

import sqlite3
from pathlib import Path
from urllib.parse import parse_qs, urlencode

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from crate_digger.collection.dj_curation import CHARACTER_TAGS
from crate_digger.collection.genre_review import TAXONOMY
from crate_digger.collection.saved_collections import (
    collection_history,
    get_saved_collection,
    list_saved_collections,
    preview_collection,
    save_collection,
)
from crate_digger.collection.traktor_organization import CATEGORIES
from crate_digger.web.templating import render_template


def _optional_int(value: str) -> int | None:
    if not value.strip():
        return None
    try:
        return int(value)
    except ValueError as error:
        raise ValueError("Choose a numeric rule bound") from error


def create_saved_collections_router(db_path: Path) -> APIRouter:
    router = APIRouter()

    @router.get("/collections", response_class=HTMLResponse)
    def collections_page(
        edit: int | None = None, notice: str | None = None
    ) -> HTMLResponse:
        try:
            selected = get_saved_collection(db_path, edit) if edit is not None else None
        except KeyError:
            return HTMLResponse("Saved collection not found", status_code=404)
        rows = list_saved_collections(db_path)
        return HTMLResponse(
            render_template(
                "saved_collections.html",
                title="Collections",
                javascript=False,
                collections=rows,
                selected=selected,
                notice=notice,
                taxonomy=TAXONOMY,
                categories=CATEGORIES,
                character_tags=CHARACTER_TAGS,
            )
        )

    @router.post("/collections/save")
    async def save_page(request: Request) -> RedirectResponse:
        form = parse_qs((await request.body()).decode("utf-8"), keep_blank_values=True)
        try:

            def get(key: str) -> str:
                return form.get(key, [""])[-1]

            rule = {
                "min_energy": _optional_int(get("min_energy")),
                "max_energy": _optional_int(get("max_energy")),
                "min_tone": _optional_int(get("min_tone")),
                "max_tone": _optional_int(get("max_tone")),
                "genre": get("genre") or None,
                "collection_category": get("collection_category") or None,
                "character_tags": form.get("character_tags", []),
            }
            collection_id = save_collection(
                db_path,
                name=get("name"),
                description=get("description"),
                rule=rule,
                collection_id=_optional_int(get("collection_id")),
            )
            return RedirectResponse(f"/collections/{collection_id}", status_code=303)
        except (KeyError, ValueError, sqlite3.Error) as error:
            return RedirectResponse(
                f"/collections?{urlencode({'notice': f'Collection was not saved: {error}'})}",
                status_code=303,
            )

    @router.get("/collections/{collection_id}", response_class=HTMLResponse)
    def collection_preview_page(collection_id: int) -> HTMLResponse:
        try:
            collection = get_saved_collection(db_path, collection_id)
            tracks = preview_collection(db_path, collection_id)
            history = collection_history(db_path, collection_id)
        except KeyError:
            return HTMLResponse("Saved collection not found", status_code=404)
        return HTMLResponse(
            render_template(
                "saved_collection_preview.html",
                title="Collection preview",
                javascript=False,
                collection=collection,
                tracks=tracks,
                history=history,
            )
        )

    return router
