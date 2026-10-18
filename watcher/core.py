#!/usr/bin/env python3
"""1つの watch を実行する。

流れ: 間隔判定 → 取得 → 条件評価 → クールダウン → 通知 → 状態保存

「取得失敗」と「取れたが0件」を最後まで混ぜないのがこのファイルの要点。
混ぜると、障害が『空き無し』として静かに飲み込まれる。
"""

import datetime

from . import notify, sources
from .conditions import evaluate
from .http import FetchError
from .util import JST, parse_duration

# GitHub の scheduler は 10〜20分ぶれるので、間隔判定に少し余裕を持たせる。
# これが無いと "1h" 指定が実質 70〜80分間隔に伸びていく。
INTERVAL_GRACE = 120


def run_watch(watch, st, now=None):
    """戻り値は run.py がレポートにまとめるための辞書。"""
    now = now or datetime.datetime.now(JST)
    wid = watch.get("id")
    name = watch.get("name") or wid

    if not watch.get("enabled", True):
        return {"id": wid, "name": name, "status": "disabled"}
    if st.get("stopped"):
        return {"id": wid, "name": name, "status": "stopped"}

    interval = parse_duration(watch.get("interval", "10m"), 600)
    waited = _elapsed(st.get("last_checked"), now)
    if waited is not None and waited < interval - INTERVAL_GRACE:
        return {"id": wid, "name": name, "status": "skipped",
                "wait": int(interval - waited)}

    # 失敗しても「叩いた」ことは記録する。成功時だけにすると、エラーを
    # 返しているサイトに対して 1h 指定の watch が 10分ごとに再試行して
    # しまう——いちばん負荷をかけてはいけない相手に。
    st["last_checked"] = now.isoformat(timespec="seconds")

    source_cfg = watch.get("source") or {}
    try:
        fetcher = sources.get(source_cfg.get("type"))
        items = fetcher(source_cfg)
    except FetchError as e:
        # 取得できなかった。state は進めない（次回やり直す）。
        print(f"  取得失敗: {e}")
        result = {"id": wid, "name": name, "status": "error", "error": str(e)}
        if not st.get("error_notified"):
            if notify.line_push(f"⚠️ {name} の監視が失敗しました\n\n{e}"):
                st["error_notified"] = True
        return result
    except (ValueError, KeyError) as e:
        # 設定ミス。リトライしても直らないので黙らせず毎回出す。
        print(f"  設定エラー: {e}")
        return {"id": wid, "name": name, "status": "misconfigured",
                "error": str(e)}

    st.pop("error_notified", None)

    fresh, active, snapshot = evaluate(watch.get("condition"), items, st)
    first_run = "active" not in st

    if first_run:
        # 初回に「全部が新着」として流すと数十件の通知になるので、
        # 件数サマリだけ送って以降の差分に備える。
        notify.line_push(_start_message(watch, items, now))
        fresh = []
    else:
        fresh = _apply_cooldown(watch, st, fresh, now)
        if fresh:
            if notify.line_push(_hit_message(watch, fresh, len(items))):
                for key, _, _ in fresh:
                    st.setdefault("last_notified", {})[key] = \
                        now.isoformat(timespec="seconds")
                if watch.get("notify", {}).get("once"):
                    st["stopped"] = True
                    print("  once 指定のためこの watch を停止しました")
            else:
                # 送れなかったぶんは active に入れず、次回また fresh になる
                sent = {k for k, _, _ in fresh}
                active = [k for k in active if k not in sent]

    st["active"] = active
    st["seen"] = sorted({*snapshot})
    st["values"] = snapshot
    st["count"] = len(items)

    print(f"  {len(items)}件取得 / 成立 {len(active)}件 / 新規 {len(fresh)}件")
    return {"id": wid, "name": name, "status": "ok", "items": len(items),
            "active": len(active),
            "fresh": [(i, n) for _, i, n in fresh],
            "first_run": first_run}


def _elapsed(iso, now):
    if not iso:
        return None
    try:
        then = datetime.datetime.fromisoformat(iso)
    except ValueError:
        return None
    if then.tzinfo is None:
        then = then.replace(tzinfo=JST)
    return (now - then).total_seconds()


def _apply_cooldown(watch, st, fresh, now):
    """同じものを短時間に繰り返し通知しない。

    価格が数円単位で揺れるサイトだと、条件の境界を行き来して10分ごとに
    通知が飛ぶ。それで即アンインストールされるので既定で効かせる。
    """
    cooldown = parse_duration(watch.get("notify", {}).get("cooldown", "6h"),
                              6 * 3600)
    if cooldown <= 0:
        return fresh
    last = st.get("last_notified") or {}
    kept = []
    for key, item, note in fresh:
        waited = _elapsed(last.get(key), now)
        if waited is not None and waited < cooldown:
            print(f"  クールダウン中のため保留: {key} "
                  f"(あと{int(cooldown - waited)}秒)")
            continue
        kept.append((key, item, note))
    return kept


def _start_message(watch, items, now):
    name = watch.get("name") or watch.get("id")
    interval = watch.get("interval", "10m")
    lines = [f"👀 {name} の監視を開始しました",
             f"{interval}ごとにチェックします。", "",
             f"現在 {len(items)}件"]
    if items:
        lines += [""] + [notify.format_item(i) for i in items[:5]]
        if len(items) > 5:
            lines.append(f"…ほか {len(items) - 5}件")
    if watch.get("url"):
        lines += ["", watch["url"]]
    return "\n".join(lines)


def _hit_message(watch, fresh, total):
    name = watch.get("name") or watch.get("id")
    lines = [f"🔔 {name}（{len(fresh)}件）", ""]
    lines += [notify.format_item(item, note) for _, item, note in fresh]
    lines += ["", f"現在の全体 {total}件"]
    if watch.get("url"):
        lines.append(watch["url"])
    return "\n".join(lines)
