#!/usr/bin/env python3
"""条件層。

設計の核:「成立している集合」を state に持ち、新しく成立したものだけ通知する。
UR の「新しい部屋 id」も、webket の「買える時間帯」も、
「価格が2万円を下回った」も、すべてこの集合差分ひとつで表せる。

条件が崩れたら集合から外れるので、もう一度成立したときに再通知できる。
「価格が下がった→戻った→また下がった」を取りこぼさないために必要。
"""

from .util import to_number

_NUM_OPS = {
    "<=": lambda a, b: a <= b,
    "<": lambda a, b: a < b,
    ">=": lambda a, b: a >= b,
    ">": lambda a, b: a > b,
    "==": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
}


class ConditionError(ValueError):
    """条件の書き方が間違っている。設定ミスなので即座に落とす。"""


def _field(item, name):
    if name is None:
        raise ConditionError("field を指定してください")
    return item.get(name)


def _predicate(cond, item, prev_values):
    """1件のアイテムが条件を満たすか。(bool, 説明文) を返す。"""
    kind = (cond.get("type") or "appeared").lower()

    if kind in ("appeared", "always", "exists"):
        return True, ""

    if kind == "all":
        subs = cond.get("conditions") or []
        if not subs:
            raise ConditionError("all には conditions が必要です")
        notes = []
        for sub in subs:
            ok, note = _predicate(sub, item, prev_values)
            if not ok:
                return False, ""
            if note:
                notes.append(note)
        return True, " / ".join(notes)

    if kind == "any":
        subs = cond.get("conditions") or []
        if not subs:
            raise ConditionError("any には conditions が必要です")
        for sub in subs:
            ok, note = _predicate(sub, item, prev_values)
            if ok:
                return True, note
        return False, ""

    if kind == "not":
        sub = cond.get("condition")
        if not sub:
            raise ConditionError("not には condition が必要です")
        ok, _ = _predicate(sub, item, prev_values)
        return (not ok), ""

    if kind == "threshold":
        op = cond.get("op", "<=")
        if op not in _NUM_OPS:
            raise ConditionError(
                f"op が不正です: {op!r} (使えるのは {', '.join(_NUM_OPS)})")
        name = cond.get("field")
        want = to_number(cond.get("value"))
        if want is None:
            raise ConditionError(f"value を数値にできません: {cond.get('value')!r}")
        got = to_number(_field(item, name))
        if got is None:
            # 値が取れないものは成立させない。取れないことを「条件成立」に
            # すると、抽出が壊れた瞬間に全件通知が飛ぶ。
            return False, ""
        if not _NUM_OPS[op](got, want):
            return False, ""
        return True, f"{name} {_fmt(got)} {op} {_fmt(want)}"

    if kind in ("contains", "not_contains"):
        name = cond.get("field")
        needle = str(cond.get("value", ""))
        if not needle:
            raise ConditionError("contains には value が必要です")
        hay = str(_field(item, name) or "")
        hit = needle in hay
        if kind == "contains":
            return (hit, f"{name} に「{needle}」") if hit else (False, "")
        return ((not hit), f"{name} に「{needle}」なし") if not hit else (False, "")

    if kind == "changed":
        name = cond.get("field")
        now = _field(item, name)
        before = (prev_values or {}).get(name)
        if before is None or str(before) == str(now):
            return False, ""
        return True, f"{name} {before} → {now}"

    raise ConditionError(f"未知の条件 type: {kind!r}")


def _fmt(v):
    return str(int(v)) if float(v).is_integer() else str(v)


def evaluate(cond, items, prev):
    """条件を評価して通知すべきものを決める。

    prev: 前回の {"active": [...], "seen": [...], "values": {id: {...}}}
    返り値: (fresh, active, snapshot)
      fresh    通知すべき [(key, item, 説明文)]
      active   いま成立しているキー一覧（次回の prev["active"] になる）
      snapshot {id: {field: value}}（changed の比較用）
    """
    cond = cond or {"type": "appeared"}
    kind = (cond.get("type") or "appeared").lower()
    prev_active = set(prev.get("active") or [])
    prev_values = prev.get("values") or {}

    if kind == "gone":
        # 消失検知だけは「今あるもの」ではなく「前にあって今ないもの」。
        current = {_key(i) for i in items}
        vanished = sorted(set(prev.get("seen") or []) - current)
        fresh = [(k, prev_values.get(k, {"label": k}), "消えました")
                 for k in vanished if k not in prev_active]
        return fresh, vanished, _snapshot(items)

    active, fresh = [], []
    for item in items:
        key = _key(item)
        ok, note = _predicate(cond, item, prev_values.get(key))
        if not ok:
            continue
        active.append(key)
        if key not in prev_active:
            fresh.append((key, item, note))
    return fresh, active, _snapshot(items)


def _key(item):
    """アイテムの同一性。id が無ければ中身から作る。

    id を決められないソースだと、毎回キーが変わって通知が止まらなくなる。
    label や主要フィールドの連結で代用し、それも無ければ全フィールド。
    """
    for name in ("id", "key"):
        v = item.get(name)
        if v not in (None, ""):
            return str(v)
    for name in ("label", "name", "title", "url", "link"):
        v = item.get(name)
        if v not in (None, ""):
            return str(v)
    return "|".join(f"{k}={item[k]}" for k in sorted(item))


def _snapshot(items):
    return {_key(i): dict(i) for i in items}
