#!/usr/bin/env python3
"""共通の小道具。環境変数・時刻・期間文字列。"""

import datetime
import html as html_mod
import os
import re

JST = datetime.timezone(datetime.timedelta(hours=9))


def env(name, default=""):
    """未設定でも空文字でも既定値に戻す。

    GitHub Actions は workflow_dispatch の未入力を空文字で渡してくるので、
    os.environ.get(name, default) では既定値にフォールバックしない。
    """
    return (os.environ.get(name) or "").strip() or default


def jst_now():
    return datetime.datetime.now(JST)


def jst_today():
    return jst_now().strftime("%Y-%m-%d")


# 単位なしは秒として扱う。"0"（抑制なし）を書けないと実用に耐えない。
_DURATION_RE = re.compile(r"^\s*(\d+)\s*([smhd]?)\s*$", re.I)
_UNIT_SECONDS = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_duration(value, default=0):
    """'10m' / '6h' / '1d' を秒に。単位なし・数値はそのまま秒。

    watches.json を人間が手で書くので、秒数より単位付きのほうが読みやすい。
    """
    if value in (None, ""):
        return default
    if isinstance(value, (int, float)):
        return int(value)
    m = _DURATION_RE.match(str(value))
    if not m:
        raise ValueError(
            f"期間の書式が不正です: {value!r} (例: 10m, 6h, 1d, 0)")
    return int(m.group(1)) * _UNIT_SECONDS[m.group(2).lower()]


def text(value):
    """'47&#13217;' のような HTML エンティティを戻す。None は空文字に。"""
    if value in (None, ""):
        return ""
    return html_mod.unescape(str(value)).strip()


def to_number(value):
    """'20,000円' -> 20000.0 / '1.5万' は非対応。取れなければ None。

    価格の閾値判定に使う。カンマ・通貨記号・単位を落として数値だけ拾う。
    小数点は最初の1個だけ有効にする（'1.2.3' のような壊れた入力を弾く）。
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value)
    kept, seen_dot = [], False
    for ch in s:
        if ch.isdigit():
            kept.append(ch)
        elif ch == "." and not seen_dot and kept:
            kept.append(ch)
            seen_dot = True
    if not kept:
        return None
    try:
        return float("".join(kept).rstrip("."))
    except ValueError:
        return None
