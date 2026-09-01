from __future__ import annotations

from fastapi import APIRouter, Body, Request

from ..http import make_client
from ..models import AIConfig, Config
from ..summarize import Summarizer
from ..xteink import XteinkClient
from ._common import MASK, apply_config_change, current_config, get_db

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


# --- test-connection probes: these handlers must never raise (never 500) ---

_PROBE_TEXT = "测试连通性。"


def _probe_provider(ai_cfg: AIConfig, proxy_url: str | None) -> dict:
    summ: Summarizer | None = None
    try:
        summ = Summarizer(ai_cfg, proxy_url=proxy_url)
        summ.summarize(_PROBE_TEXT)
        return {"ok": True, "error": None}
    except Exception as exc:  # noqa: BLE001 - probe must not 500
        return {"ok": False, "error": str(exc)}
    finally:
        if summ is not None:
            summ.close()


@router.post("/api/test/ai")
def test_ai(request: Request) -> dict:
    cfg = current_config(request)
    if cfg.ai is None:
        return {
            "primary": {"ok": False, "error": "ai not configured"},
            "fallback": None,
        }
    proxy_url = cfg.proxy.url
    base = {"fallback": None, "max_retries": 0}
    primary_cfg = cfg.ai.model_copy(update=base)
    result: dict = {
        "primary": _probe_provider(primary_cfg, proxy_url),
        "fallback": None,
    }
    if cfg.ai.fallback is not None:
        fb_cfg = cfg.ai.model_copy(update={**base, "primary": cfg.ai.fallback})
        result["fallback"] = _probe_provider(fb_cfg, proxy_url)
    return result


@router.post("/api/test/xteink")
def test_xteink(request: Request) -> dict:
    cfg = current_config(request)
    client: XteinkClient | None = None
    try:
        client = XteinkClient(cfg.xteink, get_db(request))
        client._access_token(force_refresh=True)
        return {"ok": True, "error": None}
    except Exception as exc:  # noqa: BLE001 - probe must not 500
        return {"ok": False, "error": str(exc)}
    finally:
        if client is not None:
            client.close()


@router.post("/api/test/proxy")
def test_proxy(request: Request) -> dict:
    url = current_config(request).proxy.url
    if not url:
        return {"ok": False, "error": "no proxy configured"}
    client = None
    try:
        client = make_client(proxy=url, timeout=10)
        resp = client.head("https://www.example.com")
        ok = 200 <= resp.status_code < 400
        return {"ok": ok, "error": None if ok else f"HTTP {resp.status_code}"}
    except Exception as exc:  # noqa: BLE001 - probe must not 500
        return {"ok": False, "error": str(exc)}
    finally:
        if client is not None:
            client.close()
