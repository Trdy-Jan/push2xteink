from __future__ import annotations

from pathlib import Path

from pydantic import ValidationError
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap
from ruamel.yaml.error import YAMLError

from .models import Config


class ConfigError(Exception):
    """配置文件缺失、语法错误或校验失败。"""


_yaml = YAML()
_yaml.preserve_quotes = True
_yaml.indent(mapping=2, sequence=4, offset=2)


def _load_raw(path: Path):
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ConfigError(f"config file not found: {path}") from exc
    except OSError as exc:
        raise ConfigError(f"cannot read config file {path}: {exc}") from exc
    try:
        data = _yaml.load(text)
    except YAMLError as exc:
        raise ConfigError(f"failed to parse YAML {path}: {exc}") from exc
    if data is None:
        raise ConfigError(f"config file is empty: {path}")
    return data


def _parse(raw) -> Config:
    try:
        return Config.model_validate(dict(raw))
    except ValidationError as exc:
        raise ConfigError(f"invalid config:\n{exc}") from exc


def load_config(path: Path) -> Config:
    return _parse(_load_raw(path))


def _merge_id_list(raw_seq: list, new_items: list) -> None:
    """In-place: reconcile an id-keyed list against ``new_items`` order.

    Existing items are matched by ``id`` and mutated in place (keeping their
    inline comments); vanished ids drop out, new ids are appended as plain dicts.
    """
    by_id = {
        item["id"]: item
        for item in raw_seq
        if isinstance(item, dict) and "id" in item
    }
    result = []
    for nd in new_items:
        existing = by_id.get(nd.get("id")) if isinstance(nd, dict) else None
        if existing is not None:
            for k in [k for k in existing.keys() if k not in nd]:
                del existing[k]
            for k, v in nd.items():
                existing[k] = v
            result.append(existing)
        else:
            result.append(nd)
    raw_seq[:] = result


def _merge_into(raw: CommentedMap, payload: dict) -> None:
    """Recursively fold ``payload`` scalars into ``raw``, preserving comments."""
    for key, value in payload.items():
        cur = raw.get(key)
        if key in ("feeds", "tasks") and isinstance(cur, list):
            _merge_id_list(cur, value)
        elif isinstance(value, dict) and isinstance(cur, dict):
            _merge_into(cur, value)
            for k in [k for k in cur.keys() if k not in value]:
                del cur[k]
        else:
            raw[key] = value
    for key in [k for k in raw.keys() if k not in payload]:
        del raw[key]


def write_config(path: Path, config: Config) -> None:
    path = Path(path)
    raw: CommentedMap = CommentedMap()
    if path.exists():
        try:
            loaded = _load_raw(path)
            if isinstance(loaded, CommentedMap):
                raw = loaded
        except ConfigError:
            raw = CommentedMap()  # unparseable/empty existing file: overwrite from scratch

    payload = config.model_dump(mode="json", exclude_none=True)
    if payload.get("proxy") in ({}, {"url": None}):
        payload.pop("proxy", None)

    _merge_into(raw, payload)

    tmp = path.with_name(path.name + ".tmp")
    try:
        with tmp.open("w", encoding="utf-8") as fh:
            _yaml.dump(raw, fh)
        tmp.replace(path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
