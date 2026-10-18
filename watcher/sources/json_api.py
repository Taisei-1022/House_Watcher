#!/usr/bin/env python3
"""汎用: JSON を返す API を叩いてアイテム列にする。

裏の JSON API を直叩きできるなら常にこれを選ぶ。HTML スクレイピングより
壊れにくく、速く、サイトへの負荷も軽い。UR で効いたのと同じ理屈。
"""

import json

from .. import http
from ..extract import json_path, json_pick
from ..util import text


def fetch(cfg):
    url = cfg.get("url")
    if not url:
        raise ValueError("json_api には url が必要です")
    fields = cfg.get("fields") or {}
    if not fields:
        raise ValueError("json_api には fields が必要です")

    pager = cfg.get("paginate") or {}
    page = int(pager.get("start", 0))
    max_pages = int(pager.get("max_pages", 20))  # 暴走よけ

    items, expected, pages_done = [], None, 0
    while True:
        data = _request(cfg, page if pager else None)
        if data is _EMPTY:
            break

        _sanity(cfg, data)
        raw = (json_path(data, cfg["items"]) if cfg.get("items") else [data])
        items.extend(_shape(r, fields, cfg) for r in raw)
        pages_done += 1

        if not pager or not raw:
            break
        if expected is None and pager.get("total_path"):
            expected = _as_int(json_pick(data, pager["total_path"]))
        if expected is None or len(items) >= expected or pages_done >= max_pages:
            break
        page += 1

    if expected is not None and len(items) < expected:
        # 全部取れていないのに「これが全部」として差分を取ると、
        # 取りこぼした分が次回「新着」として遅れて出る or 消失扱いになる。
        raise http.SanityError(
            f"{expected}件のうち{len(items)}件しか取得できていません "
            f"({pages_done}ページ取得)")
    return items


_EMPTY = object()


def _request(cfg, page):
    data = dict(cfg.get("data") or {}) if cfg.get("data") else None
    url = cfg["url"]
    if page is not None:
        param = (cfg.get("paginate") or {}).get("param", "pageIndex")
        if data is not None:
            data[param] = str(page)
        else:
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}{param}={page}"

    body = http.fetch(
        url,
        data=data,
        headers=cfg.get("headers"),
        cookies=bool(cfg.get("cookies")),
        check_robots=cfg.get("check_robots", True),
        allow_status=tuple(cfg.get("allow_status") or ()),
    )

    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as e:
        raise http.SanityError(f"JSON を返しませんでした: {body[:200]!r}") from e

    # 空を表す形がサイトごとに違う。UR は本文そのものが null、
    # 楽天は 404 + {"error":"not_found"}。どちらも「失敗」ではない。
    if parsed is None:
        if cfg.get("null_is_empty", True):
            return _EMPTY
        raise http.SanityError("応答が null です")
    for marker in (cfg.get("empty_markers") or []):
        if isinstance(parsed, dict) and marker in str(parsed.get("error", "")):
            return _EMPTY
    return parsed


def _sanity(cfg, data):
    """取得できたことの確認。

    これが無いと、構造変更で抽出が0件になったのを「空き無し」と誤認して
    黙り込む。items が空になり得るソースでは sanity_path を強く推奨する。
    """
    path = cfg.get("sanity_path")
    if path and not json_path(data, path):
        raise http.SanityError(f"応答に sanity_path '{path}' がありません")


def _shape(raw, fields, cfg):
    # UR の floorspace は "42&#13217;" のように HTML エンティティで来る。
    # 戻さないと通知にそのまま出るので、既定で unescape する。
    unescape = cfg.get("unescape", True)
    item = {}
    for name, path in fields.items():
        value = json_pick(raw, path) if isinstance(path, str) else path
        item[name] = text(value) if unescape and isinstance(value, str) else value
    prefix = cfg.get("link_prefix")
    for key in ("url", "link"):
        v = item.get(key)
        if prefix and isinstance(v, str) and v.startswith("/"):
            item[key] = prefix + v
    return item


def _as_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None
