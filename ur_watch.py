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
DANCHI_NAME = os.environ.get("UR_NAME", "ヌーヴェル赤羽台")
DANCHI_URL = os.environ.get(
    "UR_URL", "https://www.ur-net.go.jp/chintai/kanto/tokyo/20_6940.html"
)
SHISYA = os.environ.get("UR_SHISYA", "20")

# 20_6940 の分解の仕方が確定していないので候補を順に試す。
# UR_DANCHI / UR_SHIKIBETSU を指定した場合はそれだけを使う。
if os.environ.get("UR_DANCHI"):
    DANCHI_CANDIDATES = [
        (os.environ["UR_DANCHI"], os.environ.get("UR_SHIKIBETSU", "0"))
    ]
else:
    DANCHI_CANDIDATES = [
        ("694", "0"),
        ("6940", ""),
        ("6940", "0"),
        ("0694", "0"),
    ]

MAX_RENT = os.environ.get("UR_MAX_RENT", "")
MADORI_FILTER = os.environ.get("UR_MADORI", "")
STATE_PATH = os.environ.get("UR_STATE", "state.json")

ENDPOINT = (
    "https://chintai.r6.ur-net.go.jp"
    "/chintai/api/bukken/detail/detail_bukken_room/"
)

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


def param_variants(danchi, shikibetsu):
    """試すパラメータの組み合わせ。上から順に試す。"""
    base = {
        "shisya": SHISYA,
        "danchi": danchi,
        "shikibetsu": shikibetsu,
        "orderByField": "0",
        "orderBySort": "0",
        "pageIndex": "0",
        "sp": "",
    }
    yield "A:mode=init", {**base, "mode": "init"}
    yield "B:base", dict(base)
    yield "C:full", {
        **base,
        "mode": "init",
        "rent_low": "",
        "rent_high": "",
        "floorspace_low": "",
        "floorspace_high": "",
        "newBukkenRoom": "",
    }
    yield "D:legacy", {
        **base,
        "rent_low": "",
        "rent_high": "",
        "floorspace_low": "",
        "floorspace_high": "",
        "newBukkenRoom": "",
    }


def post(params):
    req = urllib.request.Request(
        ENDPOINT,
        data=urllib.parse.urlencode(params).encode("utf-8"),
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": UA,
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": DANCHI_URL,
            "Origin": "https://www.ur-net.go.jp",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as res:
        return res.read().decode("utf-8", errors="replace")


def extract_list(data):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("roomList", "list", "data", "items", "room"):
            if isinstance(data.get(key), list):
                return data[key]
    return None


def fetch_rooms():
    """当たりのパラメータが見つかるまで総当たりし、結果をログに出す。"""
    log = []
    for danchi, shikibetsu in DANCHI_CANDIDATES:
        for label, params in param_variants(danchi, shikibetsu):
            tag = f"danchi={danchi} shikibetsu={shikibetsu!r} {label}"
            try:
                body = post(params)
            except Exception as e:  # noqa: BLE001
                log.append(f"{tag} -> {type(e).__name__}: {e}")
                continue

            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                log.append(f"{tag} -> not JSON: {body[:150]!r}")
                continue

            rooms = extract_list(data)
            if rooms is not None:
                print(f"✅ HIT: {tag} -> {len(rooms)}件")
                if rooms:
                    print("先頭の部屋のキー:", sorted(rooms[0].keys()))
                    print("先頭の部屋:", json.dumps(rooms[0], ensure_ascii=False)[:600])
                print(f"次回から UR_DANCHI={danchi} UR_SHIKIBETSU={shikibetsu} "
                      f"を env に固定すると速くなります")
                return rooms

            log.append(f"{tag} -> {body[:150]!r}")

    raise RuntimeError("UR API から空室一覧を取得できませんでした:\n" + "\n".join(log))


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
    """送信に失敗しても例外を投げない（ログに出すだけ）。"""
    token = (os.environ.get("LINE_CHANNEL_ACCESS_TOKEN") or "").strip()
    to = (os.environ.get("LINE_TO") or "").strip()

    if not token or not to:
        print("LINE の認証情報が未設定のため送信をスキップ:\n" + text)
        return False

    print(f"LINE: token {len(token)}文字 / 宛先 {to[:5]}… に送信します")

    body = json.dumps(
        {"to": to, "messages": [{"type": "text", "text": text[:4900]}]}
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
            return True
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")
        print(f"LINE 送信 失敗: {e.code} {detail}")
        if e.code == 401:
            print("→ チャネルアクセストークン（長期）が違います。"
                  "チャネルシークレットと取り違えていないか確認してください。")
        elif e.code == 400 and "Invalid to" in detail:
            print("→ LINE_TO のユーザーIDが不正です。U で始まる33文字か確認してください。")
        elif e.code == 403:
            print("→ この公式アカウントを自分のLINEで友だち追加していない可能性があります。")
        return False
    except Exception as e:  # noqa: BLE001
        print(f"LINE 送信 失敗: {type(e).__name__}: {e}")
        return False


# ---- main ---------------------------------------------------------------
def main():
    try:
        raw = fetch_rooms()
    except Exception as e:  # noqa: BLE001
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

    if first_run:
        msg = f"🏠 {DANCHI_NAME} の空室監視を開始しました\n毎時10分にチェックします。\n\n"
        msg += (
            f"現在の空室 {len(rooms)}件\n\n" + "\n\n".join(format_room(r) for r in rooms)
            if rooms
            else "現在の空室 0件"
        )
        line_push(msg)
    else:
        new_rooms = [r for r in rooms if r["id"] not in known]
        if new_rooms:
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
