#!/usr/bin/env python3
"""
Webket の栗駒山いわかがみ平 臨時駐車場予約を監視して、
買える時間帯が出たら LINE に通知する。

標準ライブラリのみ。ur_watch.py と同じ run から10分ごとに呼ばれる想定。

判定の考え方:
  STEP1 駐車場予約 (igc=0001) と STEP2 環境保護協力金 (igc=0002) は別商品で、
  どちらか片方だけ空いていても現地には行けない。
  そのため「同じ日・同じ時間帯で、両方とも完売でない」ときだけ通知する。

ページ構造（実測）:
  - 販売中の日は <div class="safe" id="YYYYMMDD"> を持ち、その中に
    <input name="YYYYMMDDt" value="開始,終了,状態,枠ID"> が時間帯の数だけ並ぶ。
    状態は 00=完売 / 01=残りわずか / 02=空席あり。
  - 完売・販売対象外の日は <div class="out"> になり、id も hidden input も無い。
    つまり「safe が無い = その日は買えない」と判定できる。
  - カレンダーの日表示（○△×）は当てにならない。△ でも中身が
    「4枠完売＋1枠残りわずか」ということが実際にある。必ず枠単位で見る。
  - アクセスすると waiting.webket.jp（順番待ち行列）を経由するため、
    Cookie を保持してリダイレクトを追う必要がある。
"""

import datetime
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar

from ur_watch import JST, line_push

# ---- 監視対象 ----------------------------------------------------------
def env(name, default=""):
    return (os.environ.get(name) or "").strip() or default


BASE = "https://webket.jp/pc/ticket/itemdetail"
FC = env("WK_FC", "52850")
AC = env("WK_AC", "9000")

# (igc, 画面上の名前)
PRODUCTS = [
    (env("WK_IGC_PARK", "0001"), "駐車場"),
    (env("WK_IGC_FEE", "0002"), "協力金"),
]

# 監視したい日。YYYYMMDD をカンマ区切りで。
TARGET_DATES = [
    d.strip() for d in env("WK_DATES", "20261010,20261011,20261012").split(",")
    if d.strip()
]

# 「買える」とみなす状態コード。00=完売 は除く。
OPEN_CODES = set(
    c.strip() for c in env("WK_OPEN_CODES", "01,02").split(",") if c.strip()
)

STATE_PATH = env("WK_STATE", "webket_state.json")
NAME = env("WK_NAME", "栗駒山いわかがみ平 駐車場")

LABEL = {"00": "完売", "01": "残りわずか", "02": "空席あり"}

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

RETRIES = 3
RETRY_WAIT = (3, 8)

# ページがちゃんと取れたかの確認に使う文字列。
# これが無いのに「全部完売」と判断すると、障害を空き無しと誤認してしまう。
SANITY = "購入条件選択"


def item_url(igc):
    return f"{BASE}?{urllib.parse.urlencode({'fc': FC, 'ac': AC, 'igc': igc})}"


def fetch(igc):
    """1商品のページを取る。Cookie を保持して待合室のリダイレクトを追う。"""
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(CookieJar())
    )
    req = urllib.request.Request(
        item_url(igc),
        headers={
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ja,en;q=0.9",
        },
    )

    last_error = None
    for attempt in range(RETRIES):
        try:
            with opener.open(req, timeout=30) as res:
                html = res.read().decode("utf-8", errors="replace")
            if SANITY not in html:
                raise RuntimeError(
                    f"ページの内容が想定外です (igc={igc}, {len(html)}文字)。"
                    "待合室で止められたか、ページ構成が変わった可能性があります。"
                )
            return html
        except urllib.error.HTTPError as e:
            if e.code < 500:
                raise
            last_error = e
        except (urllib.error.URLError, TimeoutError, ConnectionError,
                RuntimeError) as e:
            last_error = e

        if attempt < RETRIES - 1:
            wait = RETRY_WAIT[attempt]
            print(f"Webket 一時エラー ({last_error}) — {wait}秒後に再試行 "
                  f"({attempt + 2}/{RETRIES})")
            time.sleep(wait)

    raise last_error


DAY_RE = re.compile(r'<div class="safe" id="(\d{8})">(.*?)</div>', re.S)
SLOT_RE = re.compile(r'name="\d+t" value="([^"]+)"')
DOW_RE = re.compile(r'id="\d+d" value="\(([^)]+)\)"')


def parse(html):
    """{YYYYMMDD: {"dow": "日", "slots": {"05:45-07:00": "02", ...}}}"""
    days = {}
    for m in DAY_RE.finditer(html):
        date, inner = m.group(1), m.group(2)
        slots = {}
        for raw in SLOT_RE.findall(inner):
            p = raw.split(",")
            if len(p) >= 3:
                slots[f"{p[0]}-{p[1]}"] = p[2]
        dow = DOW_RE.search(inner)
        days[date] = {"dow": dow.group(1) if dow else "", "slots": slots}
    return days


def is_open(code):
    return code in OPEN_CODES


def collect():
    """対象日×時間帯ごとに、両商品が買えるかを突き合わせる。

    返り値: {"YYYYMMDD|HH:MM-HH:MM": {"dow":…, "駐車場":"02", "協力金":"01"}}
    値が入るのは「両方とも完売でない」組み合わせだけ。
    """
    pages = {}
    for igc, label in PRODUCTS:
        pages[label] = parse(fetch(igc))

    available = {}
    for date in TARGET_DATES:
        per = {label: pages[label].get(date) for _, label in PRODUCTS}
        if any(v is None for v in per.values()):
            continue  # どちらかが丸ごと完売なら、その日は成立しない

        times = set()
        for v in per.values():
            times |= set(v["slots"])

        for t in sorted(times):
            codes = {label: per[label]["slots"].get(t, "00") for _, label in PRODUCTS}
            if all(is_open(c) for c in codes.values()):
                dow = next((v["dow"] for v in per.values() if v["dow"]), "")
                available[f"{date}|{t}"] = {"dow": dow, **codes}
    return available, pages


# ---- 整形 ---------------------------------------------------------------
def fmt_date(date, dow):
    return f"{int(date[4:6])}/{int(date[6:])}" + (f"({dow})" if dow else "")


def fmt_slot(key, info):
    date, t = key.split("|")
    detail = " / ".join(
        f"{label} {LABEL.get(info.get(label), '?')}" for _, label in PRODUCTS
    )
    return f"■ {fmt_date(date, info.get('dow', ''))} {t}\n　{detail}"


def current_summary(pages):
    """今どうなっているかを人間向けに1行ずつ。"""
    lines = []
    for date in TARGET_DATES:
        bits = []
        for _, label in PRODUCTS:
            day = pages[label].get(date)
            if not day:
                bits.append(f"{label}=完売")
            else:
                n = sum(1 for c in day["slots"].values() if is_open(c))
                bits.append(f"{label}={n}/{len(day['slots'])}枠")
        dow = next((pages[l].get(date, {}).get("dow", "")
                    for _, l in PRODUCTS if pages[l].get(date)), "")
        lines.append(f"　{fmt_date(date, dow)}  " + "  ".join(bits))
    return lines


# ---- 状態 ---------------------------------------------------------------
def load_state():
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ---- main ---------------------------------------------------------------
def main():
    state = load_state()
    now = datetime.datetime.now(JST)

    try:
        available, pages = collect()
    except Exception as e:  # noqa: BLE001
        print(f"Webket 取得失敗: {e}", file=sys.stderr)
        if not state.get("error_notified"):
            line_push(f"⚠️ {NAME} の空き監視が失敗しました\n\n{e}")
            state["error_notified"] = True
            save_state(state)
        sys.exit(1)

    state.pop("error_notified", None)
    known = set(state.get("open", []))
    current = set(available)
    first_run = "open" not in state

    if first_run:
        msg = [f"🅿️ {NAME} の空き監視を開始しました",
               "10分ごとに STEP1(駐車場) と STEP2(協力金) を突き合わせます。", ""]
        msg += ["現在の状況:"] + current_summary(pages) + [""]
        if available:
            msg.append(f"今すぐ買える組み合わせ {len(available)}件")
            msg.append("")
            msg += [fmt_slot(k, available[k]) for k in sorted(available)]
        else:
            msg.append("今すぐ買える組み合わせはありません。")
        msg += ["", item_url(PRODUCTS[0][0])]
        line_push("\n".join(msg))
    else:
        fresh = sorted(current - known)
        if fresh:
            msg = [f"🅿️ {NAME} に空きが出ました（{len(fresh)}件）",
                   "駐車場と協力金の両方が取れる時間帯です。", ""]
            msg += [fmt_slot(k, available[k]) for k in fresh]
            msg += ["", "STEP1 駐車場:", item_url(PRODUCTS[0][0]),
                    "STEP2 協力金:", item_url(PRODUCTS[1][0])]
            line_push("\n".join(msg))
            print(f"新着 {len(fresh)}件を通知: {fresh}")
        else:
            print(f"変化なし（買える組み合わせ {len(current)}件）")

    state["open"] = sorted(current)
    state["detail"] = {k: available[k] for k in sorted(available)}
    state["checked"] = now.strftime("%Y-%m-%d %H:%M")
    save_state(state)


if __name__ == "__main__":
    main()
