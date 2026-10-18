#!/usr/bin/env python3
"""ソースのレジストリ。

watches.json の source.type でここから実装を引く。新しいサイトに対応する
ときは、このディレクトリにモジュールを足して REGISTRY に登録するだけでよい。

汎用の json_api / html_regex でまず試し、構造が複雑すぎて表現できない
ものだけ専用アダプタにする（UR も webket もこの「専用」の側）。
"""

from . import html_regex, json_api, rakuten_travel

REGISTRY = {
    "json_api": json_api.fetch,
    "html_regex": html_regex.fetch,
    "rakuten_travel": rakuten_travel.fetch,
}


def get(type_name):
    try:
        return REGISTRY[type_name]
    except KeyError:
        raise ValueError(
            f"未知の source.type: {type_name!r} "
            f"(使えるのは {', '.join(sorted(REGISTRY))})"
        ) from None
