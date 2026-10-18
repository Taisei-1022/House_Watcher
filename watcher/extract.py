#!/usr/bin/env python3
"""抽出層。JSON からはパス式で、HTML からは名前付きキャプチャで取る。

依存は標準ライブラリだけに保つ（既存2本と同じ方針）。そのため HTML は
パーサではなく正規表現で扱う。壊れやすいが、レシピ側で sanity を必須に
しているので「黙って空振りする」ことは防げる。
"""

import re

_TOKEN_RE = re.compile(r"([^.\[\]]+)|\[(\*|-?\d+)\]")


def _tokens(path):
    """'a.b[*].c[0]' -> [('key','a'),('key','b'),('idx','*'),('key','c'),('idx','0')]"""
    out, pos = [], 0
    for m in _TOKEN_RE.finditer(path):
        if m.start() != pos and path[pos:m.start()] != ".":
            raise ValueError(f"パス式が不正です: {path!r}")
        pos = m.end()
        if m.group(1) is not None:
            out.append(("key", m.group(1)))
        else:
            out.append(("idx", m.group(2)))
    if pos != len(path):
        raise ValueError(f"パス式が不正です: {path!r}")
    return out


def json_path(data, path):
    """パスに一致する値を全部リストで返す。[*] があれば展開する。

    途中が無い・型が違う場合はその枝を黙って捨てる。API が項目を
    省略してくることは普通にあるので、例外にすると使い物にならない。
    """
    if path in ("", None, "$"):
        return [data]
    current = [data]
    for kind, token in _tokens(path):
        nxt = []
        for node in current:
            if kind == "key":
                if isinstance(node, dict) and token in node:
                    nxt.append(node[token])
            elif token == "*":
                if isinstance(node, list):
                    nxt.extend(node)
                elif isinstance(node, dict):
                    nxt.extend(node.values())
            else:
                i = int(token)
                if isinstance(node, list) and -len(node) <= i < len(node):
                    nxt.append(node[i])
        current = nxt
        if not current:
            return []
    return current


def json_pick(data, path):
    """最初の一致を返す。無ければ None。"""
    found = json_path(data, path)
    return found[0] if found else None


_FLAGS = {"s": re.S, "i": re.I, "m": re.M, "x": re.X}


def compile_pattern(pattern, flags=""):
    bits = 0
    for ch in flags or "":
        if ch not in _FLAGS:
            raise ValueError(f"未知の正規表現フラグ: {ch!r} (使えるのは s/i/m/x)")
        bits |= _FLAGS[ch]
    return re.compile(pattern, bits)


def regex_items(body, pattern, flags=""):
    """名前付きキャプチャをそのままフィールドにした dict のリストを返す。

    (?P<id>…) (?P<price>…) のように書けば、条件式から field 名で参照できる。
    """
    rx = compile_pattern(pattern, flags)
    if not rx.groupindex:
        raise ValueError(
            "pattern に名前付きキャプチャがありません。"
            "(?P<price>…) のようにフィールド名を付けてください。"
        )
    return [m.groupdict() for m in rx.finditer(body)]
