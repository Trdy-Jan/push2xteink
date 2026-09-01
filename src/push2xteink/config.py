from __future__ import annotations

import os
from pathlib import Path

from pydantic import ValidationError
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap
from ruamel.yaml.error import CommentMark, YAMLError
from ruamel.yaml.tokens import CommentToken

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


def _errors_summary(exc: ValidationError) -> str:
    """`loc: msg` lines only.

    ``str(ValidationError)`` embeds ``input_value={...}`` — for this config that
    is the whole mapping, xteink password and AI api_keys included. The message
    ends up in ``docker logs`` via ``maybe_reload``'s WARNING and ``_serve``'s
    stderr, so it must never carry secrets.
    """
    parts = [
        f"{'.'.join(str(p) for p in e.get('loc', ()))}: {e.get('msg', 'invalid')}".lstrip(": ")
        for e in exc.errors(include_url=False)
    ]
    return "; ".join(parts) or "validation failed"


def _parse(raw) -> Config:
    try:
        return Config.model_validate(dict(raw))
    except ValidationError as exc:
        raise ConfigError(f"invalid config: {_errors_summary(exc)}") from exc


def load_config(path: Path) -> Config:
    return _parse(_load_raw(path))


def _last_key(m) -> object | None:
    keys = list(m.keys())
    return keys[-1] if keys else None


def _detach_trailing_comment(m, key) -> CommentToken | None:
    """Pop the *block* that trails ``key``'s line — blank lines and standalone
    ``# ...`` lines that visually belong AFTER the mapping, not to ``key``.

    ruamel glues those onto the preceding key's end-of-line comment token, so
    appending a new key (a defaulted field that was implicit in the file) would
    otherwise emit it *below* the comment, dragging e.g. a
    ``# standalone between two feeds`` note into the middle of the first feed.
    ``key``'s own eol comment (the first line) stays put.
    """
    if key is None or not hasattr(m, "ca"):
        return None
    slot = m.ca.items.get(key)
    if not slot or len(slot) < 3 or slot[2] is None:
        return None
    tok = slot[2]
    head, sep, tail = getattr(tok, "value", "").partition("\n")
    if not sep or not tail:
        return None
    tok.value = head + sep
    return CommentToken("\n" + tail, tok.start_mark)


def _attach_trailing_comment(m, key, tok: CommentToken | None) -> None:
    if tok is None or key is None or not hasattr(m, "ca"):
        return
    slot = m.ca.items.setdefault(key, [None, None, None, None])
    if slot[2] is None:
        slot[2] = tok
    else:
        slot[2].value = slot[2].value.rstrip("\n") + tok.value


def _merge_map_items(existing, nd: dict) -> None:
    """Fold ``nd`` into the CommentedMap ``existing`` in place, keeping the
    trailing comment block anchored to whatever ends up last."""
    trailing = _detach_trailing_comment(existing, _last_key(existing))
    for k in [k for k in existing.keys() if k not in nd]:
        del existing[k]
    for k, v in nd.items():
        # Skip unchanged scalars: reassigning would drop the original quoting /
        # flow style (and re-appending an already-present key is pointless).
        if k in existing and existing[k] == v:
            continue
        existing[k] = v
    _attach_trailing_comment(existing, _last_key(existing), trailing)


def _block_to_tokens(tok: CommentToken | None) -> list[CommentToken]:
    """A trailing-block token (indentation baked into the value) -> the
    list-of-CommentToken form ruamel wants for a *leading* comment (indentation
    carried by each token's mark)."""
    if tok is None:
        return []
    body = tok.value[1:] if tok.value.startswith("\n") else tok.value
    out = []
    for line in body.splitlines(keepends=True):
        stripped = line.lstrip(" ")
        out.append(CommentToken(stripped, CommentMark(len(line) - len(stripped))))
    return out


def _tokens_to_block(tokens: list[CommentToken]) -> CommentToken | None:
    """The inverse of :func:`_block_to_tokens`."""
    if not tokens:
        return None
    parts = []
    for t in tokens:
        col = getattr(getattr(t, "start_mark", None), "column", 0) or 0
        parts.append(t.value if t.value.startswith("\n") else " " * col + t.value)
    return CommentToken("\n" + "".join(parts), tokens[0].start_mark)


def _get_seq_lead(parent, key) -> list[CommentToken]:
    """The comment block sitting above the FIRST item of ``parent[key]``.

    ruamel keeps it on the parent mapping's slot for ``key`` (``ca.comment`` on
    the sequence itself is a mirror of the same list object; only the parent's
    copy is consulted by the dumper).
    """
    slot = parent.ca.items.get(key) if hasattr(parent, "ca") else None
    if slot and len(slot) > 3 and slot[3]:
        return list(slot[3])
    return []


def _set_seq_lead(parent, key, tokens: list[CommentToken]) -> None:
    if not hasattr(parent, "ca"):
        return
    slot = parent.ca.items.get(key)
    if not tokens:
        if slot and len(slot) > 3:
            # An empty list makes the dumper emit a mangled `feeds:   -`; None is
            # the only way to say "no leading comment".
            slot[3] = None
        return
    if slot and len(slot) > 3 and slot[3] is not None:
        slot[3][:] = tokens  # same list object the sequence's ca.comment mirrors
    else:
        parent.ca.items.setdefault(key, [None, None, None, None])[3] = tokens


def _merge_id_list(parent, key, raw_seq: list, new_items: list) -> None:
    """In-place: reconcile an id-keyed list against ``new_items`` order.

    Existing items are matched by ``id`` and mutated in place; vanished ids drop
    out, new ids are appended as plain dicts.

    Comments follow their item. ruamel stores the block between item *i* and
    item *i+1* as a trailing comment on item *i*'s last key, and the block above
    item 0 on the parent's slot for ``key`` — so a naive rebuild leaves every
    comment pinned to its old POSITION while the items move. Here each block is
    lifted to "the pre-comment of the item it precedes", carried through the
    reorder by item id, and written back at the new position. The block after
    the LAST item belongs to the list (it is the gap before the next section),
    so it stays at the end.
    """
    old_items = [it for it in raw_seq]
    pre: dict[object, list[CommentToken]] = {}
    if old_items:
        pre[_item_key(old_items[0])] = _get_seq_lead(parent, key)
    list_tail: CommentToken | None = None
    for i, item in enumerate(old_items):
        block = _detach_trailing_comment(item, _last_key(item)) if hasattr(item, "ca") else None
        if i + 1 < len(old_items):
            pre[_item_key(old_items[i + 1])] = _block_to_tokens(block)
        else:
            list_tail = block

    by_id = {
        item["id"]: item
        for item in old_items
        if isinstance(item, dict) and "id" in item
    }
    old_index = {id(item): i for i, item in enumerate(old_items)}
    old_ca = dict(raw_seq.ca.items) if hasattr(raw_seq, "ca") else {}

    result = []
    for nd in new_items:
        existing = by_id.get(nd.get("id")) if isinstance(nd, dict) else None
        if existing is not None:
            _merge_map_items(existing, nd)
            result.append(existing)
        else:
            result.append(nd)

    # Re-key any sequence-level comments by the item's NEW position too.
    new_ca = {}
    for new_i, item in enumerate(result):
        old_i = old_index.get(id(item))
        if old_i is not None and old_i in old_ca:
            new_ca[new_i] = old_ca[old_i]
    raw_seq[:] = result
    if hasattr(raw_seq, "ca"):
        raw_seq.ca.items.clear()
        raw_seq.ca.items.update(new_ca)

    if not result:
        _set_seq_lead(parent, key, [])
        return
    _set_seq_lead(parent, key, pre.get(_item_key(result[0]), []))
    for i in range(1, len(result)):
        _attach_trailing_comment(
            result[i - 1],
            _last_key(result[i - 1]),
            _tokens_to_block(pre.get(_item_key(result[i]), [])),
        )
    _attach_trailing_comment(result[-1], _last_key(result[-1]), list_tail)


def _item_key(item) -> object:
    return item.get("id") if isinstance(item, dict) else id(item)


def _merge_into(raw: CommentedMap, payload: dict) -> None:
    """Recursively fold ``payload`` scalars into ``raw``, preserving comments."""
    trailing = _detach_trailing_comment(raw, _last_key(raw))
    for key, value in payload.items():
        cur = raw.get(key)
        if key in ("feeds", "tasks") and isinstance(cur, list):
            _merge_id_list(raw, key, cur, value)
        elif isinstance(value, dict) and isinstance(cur, dict):
            _merge_into(cur, value)
            for k in [k for k in cur.keys() if k not in value]:
                del cur[k]
        elif key in raw and raw[key] == value:
            continue  # unchanged: keep the file's own quoting / flow style
        else:
            raw[key] = value
    for key in [k for k in raw.keys() if k not in payload]:
        del raw[key]
    _attach_trailing_comment(raw, _last_key(raw), trailing)


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
    # exclude_none drops proxy.url when unset, leaving an empty mapping. Keep the
    # `proxy:` key with an explicit null instead of deleting the block, so any
    # comments the user parked there survive.
    if payload.get("proxy") == {}:
        payload["proxy"] = {"url": None}

    _merge_into(raw, payload)

    tmp = path.with_name(path.name + ".tmp")
    try:
        with tmp.open("w", encoding="utf-8") as fh:
            _yaml.dump(raw, fh)
        # replace() gives the new inode the process umask, silently undoing a
        # `chmod 600` on a file holding a plaintext xteink password and AI keys —
        # and P5's web UI rewrites it on every click.
        try:
            if path.exists():
                os.chmod(tmp, os.stat(path).st_mode & 0o7777)
            else:
                os.chmod(tmp, 0o600)
        except OSError:  # e.g. an exotic filesystem; not worth failing the write
            pass
        tmp.replace(path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
