#!/usr/bin/env python3
"""
UR ヌーヴェル赤羽台 (20_6940) の空室を監視して、
新しく出た部屋だけ LINE (Messaging API) に push する。

標準ライブラリのみ。GitHub Actions から1時間ごとに実行する想定。
"""

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

# ---- 監視対象 ----------------------------------------------------------
# ヌーヴェル赤羽台: https://www.ur-net.go.jp/chintai/kanto/tokyo/20_6940.html
# URL の "20_6940" が shisya=20 / danchi=694 / shikibetsu=0 に対応する。
SHISYA = os.environ.get("UR_SHISYA", "20")
DANCHI = os.environ.get("UR_DANCHI", "694")
SHIKIBETSU = os.environ.get("UR_SHIKIBETSU", "0")
DANCHI_NAME = os.environ.get("UR_NAME", "ヌーヴェル赤羽台")
DANCHI_URL = os.environ.get(
    "UR_URL", "https://www.ur-net.go.jp/chintai/kanto/tokyo/20_6940.html"
)

# 任意のフィルタ（空なら絞り込みなし）
MAX_RENT = os.environ.get("UR_MAX_RENT", "")          # 例: "150000"
MADORI_FILTER = os.environ.get("UR_MADORI", "")        # 例: "2DK,2LDK,3LDK"

STATE_PATH = os.environ.get("UR_STATE", "state.json")

# UR のサイトが内部で叩いている非公開エンドポイント。
# ホスト名が変わることがあるので順に試す。
ENDPOINTS = [
    "https://chintai.r6.ur-net.go.jp/chintai/api/bukken/detail/detail_bukken_room/",
    "https://chintai.sumai.ur-net.go.jp/chintai/api/bukken/detail/detail_bukken_room/",
]

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


# ---- UR から空室一覧を取る ---------------------------------------------
def fetch_rooms():
    payload = urllib.parse.urlencode(
        {
            "rent_low": "",
            "rent_high": "",
            "floorspace_low": "",
            "floorspace_high": "",
            "shisya": SHISYA,
            "danchi": DANCHI,
            "shikibetsu": SHIKIBETSU,
            "newBukkenRoom": "",
            "orderByField": "0",
            "orderBySort": "0",
            "pageIndex": "0",
            "sp": "",
        }
    ).encode("utf-8")

    errors = []
    for url in ENDPOINTS:
        req = urllib.request.Request(
            url,
            data=payload,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": UA,
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "X-Requested-With": "XMLHttpRequest",
                "Referer": DANCHI_URL,
                "Origin": "https://www.ur-net.go.jp",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as res:
                body = res.read().decode("utf-8", errors="replace")
            data = json.loads(body)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{url} -> {type(e).__name__}: {e}")
            continue

        # 空室ゼロだと {"err":"...", ...} や [] が返る
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for key in ("roomList", "list", "data", "items"):
                if isinstance(data.get(key), list):
                    return data[key]
            return []
        errors.append(f"{url} -> unexpected JSON: {body[:200]}")

    raise RuntimeError("UR API から取得できませんでした:\n" + "\n".join(errors))


# ---- 部屋情報の整形 -----------------------------------------------------
def pick(room, *keys, default=""):
    for k in keys:
        v = room.get(k)
        if v not in (None, "", []):
            return str(v)
    return default


def normalize(room):
    room_id = pick(room, "id", "roomId", "bukkenNo", "roomNo")
    link = pick(room, "roomDetailLink", "roomLink", "detailLink")
    if link and link.startswith("/"):
        link = "https://www.ur-net.go.jp" + link

    name = pick(room, "name", "roomName", "title")
    rent = pick(room, "rent", "rentNormal", "chinryo")
    common = pick(room, "commonfee", "commonFee", "kyoekihi")
    madori = pick(room, "madori", "type", "floorPlan")
    space = pick(room, "floorspace", "menseki", "floorSpace")
    floor = pick(room, "floor", "kaisu")

    if not room_id:
        room_id = "|".join([name, madori, space, floor, rent])

    return {
        "id": room_id,
        "name": name,
        "rent": rent,
        "common": common,
        "madori": madori,
        "space": space,
        "floor": floor,
        "link": link or DANCHI_URL,
    }


def rent_to_int(rent_str):
    digits = "".join(ch for ch in rent_str if ch.isdigit())
    return int(digits) if digits else None


def passes_filter(r):
    if MAX_RENT:
        v = rent_to_int(r["rent"])
        if v is not None and v > int(MAX_RENT):
            return False
    if MADORI_FILTER:
        wanted = [m.strip() for m in MADORI_FILTER.split(",") if m.strip()]
        if wanted and not any(w in r["madori"] for w in wanted):
            return False
    return True


def format_room(r):
    parts = [p for p in [r["name"], r["madori"], r["space"], r["floor"]] if p]
    head = " / ".join(parts) or "(部屋情報)"
    rent = r["rent"]
    if r["common"]:
        rent = f"{rent}（共益費 {r['common']}）"
    return f"■ {head}\n　{rent}\n　{r['link']}"


# ---- 状態の保存 ---------------------------------------------------------
def load_state():
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ---- LINE 送信 ----------------------------------------------------------
def line_push(text):
    token = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
    to = os.environ.get("LINE_TO")
    if not token or not to:
        print("LINE の認証情報が無いので送信をスキップします:\n" + text)
        return

    text = text[:4900]
    body = json.dumps(
        {"to": to, "messages": [{"type": "text", "text": text}]}
    ).encode("utf-8")
    req = urllib.request.Request(
        "https://api.line.me/v2/bot/message/push",
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            print("LINE 送信 OK:", res.status)
    except urllib.error.HTTPError as e:
        print("LINE 送信 失敗:", e.code, e.read().decode("utf-8", "replace"))
        raise


# ---- main ---------------------------------------------------------------
def main():
    try:
        raw = fetch_rooms()
    except Exception as e:  # noqa: BLE001
        # 取得に失敗した時は状態を壊さず、気づけるように1度だけ通知する
        state = load_state()
        if not state.get("error_notified"):
            line_push(f"⚠️ UR空室チェックが失敗しました\n{DANCHI_NAME}\n\n{e}")
            state["error_notified"] = True
            save_state(state)
        print(e, file=sys.stderr)
        sys.exit(1)

    rooms = [normalize(r) for r in raw]
    rooms = [r for r in rooms if passes_filter(r)]
    current_ids = {r["id"] for r in rooms}

    state = load_state()
    state.pop("error_notified", None)
    known = set(state.get("ids", []))
    first_run = "ids" not in state

    new_rooms = [r for r in rooms if r["id"] not in known]

    if first_run:
        msg = f"🏠 {DANCHI_NAME} の空室監視を開始しました\n毎時10分にチェックします。\n\n"
        msg += (
            f"現在の空室 {len(rooms)}件\n\n" + "\n\n".join(format_room(r) for r in rooms)
            if rooms
            else "現在の空室 0件"
        )
        line_push(msg)
    elif new_rooms:
        msg = f"🔔 {DANCHI_NAME} に空きが出ました（{len(new_rooms)}件）\n\n"
        msg += "\n\n".join(format_room(r) for r in new_rooms)
        msg += f"\n\n一覧: {DANCHI_URL}"
        line_push(msg)
        print(f"新着 {len(new_rooms)}件を通知")
    else:
        print(f"新着なし（現在の空室 {len(rooms)}件）")

    state["ids"] = sorted(current_ids)
    state["count"] = len(rooms)
    save_state(state)


if __name__ == "__main__":
    main()
