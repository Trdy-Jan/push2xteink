from __future__ import annotations

from fastapi import APIRouter, Body, HTTPException, Request, Response

from ..models import Feed
from ._common import apply_config_change, current_config

router = APIRouter()


@router.get("/api/feeds")
def list_feeds(request: Request) -> list[dict]:
    return [f.model_dump(mode="json") for f in current_config(request).feeds]


@router.post("/api/feeds", status_code=201)
def create_feed(request: Request, feed: Feed) -> dict:
    if feed.id in {f.id for f in current_config(request).feeds}:
        raise HTTPException(status_code=409, detail=f"feed {feed.id!r} already exists")

    def mutate(raw: dict) -> None:
        raw["feeds"].append(feed.model_dump(mode="json"))

    cfg = apply_config_change(request, mutate)
    return next(f for f in cfg.feeds if f.id == feed.id).model_dump(mode="json")


@router.put("/api/feeds/{feed_id}")
def update_feed(
    request: Request, feed_id: str, body: dict = Body(...)
) -> dict:
    if feed_id not in {f.id for f in current_config(request).feeds}:
        raise HTTPException(status_code=404, detail=f"feed {feed_id!r} not found")

    def mutate(raw: dict) -> None:
        for f in raw["feeds"]:
            if f["id"] == feed_id:
                f.update({k: v for k, v in body.items() if k != "id"})

    cfg = apply_config_change(request, mutate)
    return next(f for f in cfg.feeds if f.id == feed_id).model_dump(mode="json")


@router.delete("/api/feeds/{feed_id}", status_code=204)
def delete_feed(request: Request, feed_id: str) -> Response:
    cfg = current_config(request)
    if feed_id not in {f.id for f in cfg.feeds}:
        raise HTTPException(status_code=404, detail=f"feed {feed_id!r} not found")
    refs = [t.id for t in cfg.tasks if feed_id in t.feeds]
    if refs:
        raise HTTPException(
            status_code=409, detail=f"feed {feed_id!r} used by tasks {refs}"
        )

    def mutate(raw: dict) -> None:
        raw["feeds"] = [f for f in raw["feeds"] if f["id"] != feed_id]

    apply_config_change(request, mutate)
    return Response(status_code=204)
