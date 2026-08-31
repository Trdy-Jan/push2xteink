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


def write_config(path: Path, config: Config) -> None:
    path = Path(path)
    raw = CommentedMap()
    if path.exists():
        try:
            raw = _load_raw(path)
        except ConfigError:
            raw = CommentedMap()  # unparseable/empty existing file: overwrite from scratch

    payload = config.model_dump(mode="json", exclude_none=True)
    if payload.get("proxy") in ({}, {"url": None}):
        payload.pop("proxy", None)

    for key, value in payload.items():
        raw[key] = value
    for key in [k for k in raw.keys() if k not in payload]:
        del raw[key]

    tmp = path.with_name(path.name + ".tmp")
    try:
        with tmp.open("w", encoding="utf-8") as fh:
            _yaml.dump(raw, fh)
        tmp.replace(path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
