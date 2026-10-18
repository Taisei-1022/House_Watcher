#!/usr/bin/env python3
"""watches.json に書いた監視をまとめて実行する。

使い方:
  python run.py                     全部実行（GitHub Actions から呼ぶ）
  python run.py --only hotel-x      1つだけ
  python run.py --only x --dry-run  LINE に送らず state も書かない（レシピ開発用）
  python run.py --force             間隔判定を無視して必ず叩く
  python run.py --list              定義一覧

新しいサイトを足すときは --only + --dry-run で抽出が通るまで回し、
通ったら間隔を付けて本番に入れる。
"""

import argparse
import json
import sys

from watcher import core, notify, state
from watcher.util import JST, parse_duration

import datetime

CONFIG_PATH = "watches.json"


def load_config(path=CONFIG_PATH):
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    watches = cfg.get("watches") or []
    seen = set()
    for w in watches:
        wid = w.get("id")
        if not wid:
            raise ValueError(f"id の無い watch があります: {w.get('name')!r}")
        if wid in seen:
            raise ValueError(f"id が重複しています: {wid!r}")
        seen.add(wid)
    return cfg, watches


def digest_due(digest, now, hour):
    """その日の指定時刻を過ぎた最初の実行で送る。

    cron を1本増やすのではなく毎回これを判定する。GitHub の scheduler は
    遅延・スキップがあるため 19:00 ちょうどの cron に賭けると
    「その1回が飛んだら届かない」。この方式なら次の実行が拾う。
    """
    if now.hour < hour:
        return False
    return digest.get("last") != now.strftime("%Y-%m-%d")


def format_digest(digest, results, now):
    """異常がないことを伝えるための定期レポート。

    「しばらく通知が無い」状態を作らないのが目的なので、失敗している
    ときこそ送る必要がある。
    """
    since = digest.get("last_at") or "監視開始"
    pending = digest.get("pending") or []
    lines = [f"📋 デイリーレポート",
             f"{since} 〜 {now.strftime('%m/%d %H:%M')}",
             "",
             f"この間の通知 {len(pending)}件", ""]

    for r in results:
        mark = {"ok": "✅", "error": "⚠️", "misconfigured": "🛠",
                "stopped": "⏹", "disabled": "💤", "skipped": "⏭"}.get(
                    r["status"], "・")
        tail = ""
        if r["status"] == "ok":
            tail = f"{r['items']}件（成立 {r['active']}件）"
        elif r.get("error"):
            tail = r["error"][:60]
        lines.append(f"{mark} {r['name']}  {tail}".rstrip())

    if pending:
        lines += ["", "— この間の通知 —"] + pending[-10:]
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", action="append", help="この id だけ実行")
    ap.add_argument("--dry-run", action="store_true",
                    help="LINE に送らず state も保存しない")
    ap.add_argument("--force", action="store_true", help="間隔判定を無視する")
    ap.add_argument("--list", action="store_true", help="定義一覧を出す")
    ap.add_argument("--config", default=CONFIG_PATH)
    ap.add_argument("--state", default=state.DEFAULT_PATH)
    args = ap.parse_args(argv)

    cfg, watches = load_config(args.config)

    if args.list:
        for w in watches:
            flag = "" if w.get("enabled", True) else " (無効)"
            print(f"{w['id']:24} {w.get('interval','10m'):>5}  "
                  f"{w.get('source',{}).get('type','?'):14} "
                  f"{w.get('name','')}{flag}")
        return 0

    if args.dry_run:
        # レシピ開発中に友だち全員へ誤爆しないための安全装置。
        notify.line_push = lambda msg: (print("--- dry-run の通知 ---"),
                                        print(msg), True)[-1]

    data = state.load(args.state)
    now = datetime.datetime.now(JST)

    targets = [w for w in watches
               if not args.only or w.get("id") in args.only]
    if args.only and not targets:
        print(f"該当する id がありません: {args.only}", file=sys.stderr)
        return 2
    if args.only:
        # 明示的に id を指定したなら enabled:false でも動かす。
        # レシピを書いている最中は無効のまま試したいことがほとんど。
        targets = [{**w, "enabled": True} for w in targets]

    results = []
    for w in targets:
        print(f"[{w.get('id')}] {w.get('name','')}")
        st = state.for_watch(data, w["id"])
        if args.force:
            st.pop("last_checked", None)
        results.append(core.run_watch(w, st, now))

    # 通知した内容をレポート用に溜める（上限50件）
    pending = data["digest"].get("pending") or []
    for r in results:
        for item, note in r.get("fresh") or []:
            pending.append(notify.format_item(item, note))
    data["digest"]["pending"] = pending[-50:]

    # 有効な watch が1つも無いのにレポートを送っても中身が無い。
    # 既存の ur_watch.py 側のレポートと二重に飛ぶのも避ける。
    live = [r for r in results if r["status"] not in ("disabled", "stopped")]
    hour = int(cfg.get("digest_hour", 19))
    if live and not args.only and digest_due(data["digest"], now, hour):
        if notify.line_push(format_digest(data["digest"], results, now)):
            data["digest"]["last"] = now.strftime("%Y-%m-%d")
            data["digest"]["last_at"] = now.strftime("%m/%d %H:%M")
            data["digest"]["pending"] = []
        else:
            print("デイリーレポートの送信に失敗。次の実行で再送します。")

    if not args.dry_run:
        state.save(data, args.state)

    ran = [r for r in results if r["status"] in ("ok", "error")]
    broken = [r for r in results if r["status"] == "misconfigured"]
    failed = [r for r in results if r["status"] == "error"]

    print(f"\n{len(results)}件中 実行{len(ran)} / 失敗{len(failed)} / "
          f"設定ミス{len(broken)}")

    # 個別サイトの一時的な失敗で run を赤くしても意味が無い（LINE と
    # デイリーレポートで伝わる）。直す必要があるのは設定ミスと全滅のとき。
    if broken or (ran and len(failed) == len(ran)):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
