#!/usr/bin/env python3
"""汎用: HTML を名前付きキャプチャで切り出してアイテム列にする。

JSON API が見つからないサイト向けの受け皿。壊れやすいので sanity が必須。
sanity を省略すると、ページ構成が変わって0件になったときに
「空き無し」と誤認して黙り込む——変化監視で最悪の壊れ方なので禁止する。
"""

from .. import http
from ..extract import regex_items
from ..util import text


def fetch(cfg):
    url = cfg.get("url")
    if not url:
        raise ValueError("html_regex には url が必要です")
    sanity = cfg.get("sanity")
    if not sanity:
        raise ValueError(
            "html_regex には sanity（ページに必ず含まれる文字列）が必要です。"
            "これが無いと構造変更を『変化なし』と誤認します。"
        )

    body = http.fetch(
        url,
        headers=cfg.get("headers"),
        sanity=sanity,
        cookies=bool(cfg.get("cookies")),
        check_robots=cfg.get("check_robots", True),
        allow_status=tuple(cfg.get("allow_status") or ()),
    )

    items = regex_items(body, cfg["pattern"], cfg.get("flags", "s"))
    unescape = cfg.get("unescape", True)
    for item in items:
        if unescape:
            for k, v in list(item.items()):
                item[k] = text(v)
        item.setdefault("url", url)
    return items
