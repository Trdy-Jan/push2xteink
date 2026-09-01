from __future__ import annotations

from fastapi import APIRouter, Body, Request

from ..models import Config
from ._common import MASK, apply_config_change, current_config

router = APIRouter()

_SECTIONS = ("xteink", "proxy", "ai", "fetch")


def _mask_ai(ai: dict | None) -> dict | None:
    if ai is None:
        return None
    masked = dict(ai)
    if isinstance(masked.get("primary"), dict):
        masked["primary"] = {**masked["primary"], "api_key": MASK}
    if isinstance(masked.get("fallback"), dict):
        masked["fallback"] = {**masked["fallback"], "api_key": MASK}
    return masked


def _settings_view(cfg: Config) -> dict:
    d = cfg.model_dump(mode="json")
    return {
        "xteink": {**d["xteink"], "password": MASK},
        "proxy": d["proxy"],
        "fetch": d["fetch"],
        "ai": _mask_ai(d.get("ai")),
    }


def _deep_update_keep_masked(dst: dict, src: dict) -> None:
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            _deep_update_keep_masked(dst[k], v)
        elif v == MASK:
            continue  # keep existing value
        else:
            dst[k] = v


@router.get("/api/settings")
def get_settings(request: Request) -> dict:
    return _settings_view(current_config(request))


@router.put("/api/settings")
def put_settings(request: Request, body: dict = Body(...)) -> dict:
    def mutate(raw: dict) -> None:
        for section in _SECTIONS:
            incoming = body.get(section)
            if not isinstance(incoming, dict):
                continue
            if not isinstance(raw.get(section), dict):
                raw[section] = {}
            _deep_update_keep_masked(raw[section], incoming)

    cfg = apply_config_change(request, mutate)
    return _settings_view(cfg)
