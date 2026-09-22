#!/usr/bin/env python3
"""
UR ヌーヴェル赤羽台 (20_6940) の空室を監視して、
新しく出た部屋だけ LINE (Messaging API) に push する。

標準ライブラリのみ。GitHub Actions から10分ごとに実行する想定。

API 仕様（www.ur-net.go.jp の common/js/api_bukken_detail.js を実測して確定）:
  POST https://chintai.r6.ur-net.go.jp/chintai/api/bukken/detail/detail_bukken_room/
  - 物件コード "20_6940" は shisya=20 / danchi=694 / shikibetu=0 に分解する。
  - パラメータ名は shikibetsu ではなく **shikibetu**（s が入らない）。
    綴りを誤ると空室があっても常に null が返るので注意。
  - 空室 0 件のときは JSON の null が返る。サイト側も data === null を
    「ご案内できるお部屋がございません」の分岐に使っている。
  - 1 リクエストで返るのは rowMax(=5) 件まで。全件取るには allCount を見て
    pageIndex を進める。
"""

import datetime
import html
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# ---- 監視対象 ----------------------------------------------------------
def env(name, default=""):
    """未設定でも空文字でも既定値に戻す。

    GitHub Actions は workflow_dispatch の未入力を空文字で渡してくるので、
    os.environ.get(name, default) では既定値にフォールバックしない。
    """
    return (os.environ.get(name) or "").strip() or default


DANCHI_NAME = env("UR_NAME", "ヌーヴェル赤羽台")
DANCHI_URL = env(
    "UR_URL", "https://www.ur-net.go.jp/chintai/kanto/tokyo/20_6940.html"
)
SHISYA = env("UR_SHISYA", "20")
DANCHI = env("UR_DANCHI", "694")
# 旧名 UR_SHIKIBETSU も一応受ける
SHIKIBETU = env("UR_SHIKIBETU", env("UR_SHIKIBETSU", "0"))

MAX_RENT = env("UR_MAX_RENT")
MADORI_FILTER = env("UR_MADORI")
STATE_PATH = env("UR_STATE", "state.json")

# デイリーレポートを送る時刻（JST の時）。この時刻を過ぎた最初の実行で送る。
try:
    DIGEST_HOUR = int(env("UR_DIGEST_HOUR", "19"))
except ValueError:
    DIGEST_HOUR = 19

ENDPOINT = (
    "https://chintai.r6.ur-net.go.jp"
    "/chintai/api/bukken/detail/detail_bukken_room/"
)
SITE = "https://www.ur-net.go.jp"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

MAX_PAGES = 20  # 暴走よけ
RETRIES = 3
RETRY_WAIT = (3, 8)  # 秒。RETRIES - 1 個必要


def build_params(page_index):
    """ブラウザが実際に送っている form データと同じ名前・並びで作る。"""
    return [
        ("rent_low", ""),
        ("rent_high", ""),
        ("floorspace_low", ""),
        ("floorspace_high", ""),
        ("shisya", SHISYA),
        ("danchi", DANCHI),
        ("shikibetu", SHIKIBETU),
        ("newBukkenRoom", ""),
        ("orderByField", "0"),
        ("orderBySort", "0"),
        ("pageIndex", str(page_index)),
        ("sp", ""),
    ]


def post(params):
    """一過性の 5xx・通信エラーはリトライする。

    10分間隔だと月4,000回以上叩くことになり、UR 側が稀に返す 500 を
    そのまま失敗にすると ⚠️ が誤発報して run も赤くなる。
    4xx はこちらの組み立てミスなので即座に諦める。
    """
    req = urllib.request.Request(
        ENDPOINT,
        data=urllib.parse.urlencode(params).encode("utf-8"),
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": UA,
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": DANCHI_URL,
            "Origin": SITE,
        },
    )

    last_error = None
    for attempt in range(RETRIES):
        try:
            with urllib.request.urlopen(req, timeout=30) as res:
                return res.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            if e.code < 500:
                raise
            last_error = e
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last_error = e

        if attempt < RETRIES - 1:
            wait = RETRY_WAIT[attempt]
            print(
                f"UR API 一時エラー ({last_error}) — {wait}秒後に再試行 "
                f"({attempt + 2}/{RETRIES})"
            )
            time.sleep(wait)

    raise last_error


def fetch_page(page_index):
    """1ページ分を取る。空室なしなら [] を返す。取得自体に失敗したら例外。"""
    body = post(build_params(page_index))
    try:
        data = json.loads(body)
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"UR API が JSON を返しませんでした (pageIndex={page_index}): "
            f"{body[:200]!r}"
        ) from e

    # null = 空室 0 件。エラーではない。
    if data is None:
        return []
    if not isinstance(data, list):
        raise RuntimeError(
            f"UR API の応答が想定外です (pageIndex={page_index}): {body[:200]!r}"
        )
    return data


def fetch_rooms():
    """allCount を見ながら全ページ取得する。"""
    bukken = f"{SHISYA}_{DANCHI}{SHIKIBETU}"
    rooms = fetch_page(0)
    if not rooms:
        print(f"UR API: {bukken} -> 空室 0件 (null)")
        return []

    try:
        all_count = int(rooms[0].get("allCount", len(rooms)))
        row_max = int(rooms[0].get("rowMax", len(rooms))) or len(rooms)
    except (TypeError, ValueError):
        all_count, row_max = len(rooms), len(rooms)

    pages = min(-(-all_count // row_max), MAX_PAGES)
    for page in range(1, pages):
        more = fetch_page(page)
        if not more:
            break
        rooms.extend(more)

    print(f"UR API: {bukken} -> allCount={all_count} 取得={len(rooms)}件")
    if len(rooms) < all_count:
        print(f"⚠️ {all_count}件のうち{len(rooms)}件しか取得できていません")
    return rooms


# ---- 部屋情報の整形 -----------------------------------------------------
def text(value):
    """'47&#13217;' のような HTML エンティティを戻す。None は空文字に。"""
    if value in (None, ""):
        return ""
    return html.unescape(str(value)).strip()


def normalize(room):
    link = text(room.get("roomDetailLink"))
    if link.startswith("/"):
        link = SITE + link

    # madori は間取り図の画像 URL なので使わない。間取りの文字列は type。
    return {
        "id": text(room.get("id")),
        "name": text(room.get("name")),
        "rent": text(room.get("rent")),
        "common": text(room.get("commonfee")),
        "madori": text(room.get("type")),
        "space": text(room.get("floorspace")),
        "floor": text(room.get("floor")),
        "system": [
            text(s.get("制度名"))
            for s in (room.get("system") or [])
            if isinstance(s, dict) and s.get("制度名")
        ],
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
    lines = [f"■ {head}", f"　{rent}"]
    if r["system"]:
        lines.append("　" + "・".join(r["system"]))
    lines.append(f"　{r['link']}")
    return "\n".join(lines)


# ---- 状態の保存 ---------------------------------------------------------
JST = datetime.timezone(datetime.timedelta(hours=9))


def jst_now():
    return datetime.datetime.now(JST)


def jst_today():
    """JST の日付 (YYYY-MM-DD)。

    state.json にこれを入れておくと、空室 0 件が続いても日付が変わった日だけ
    ファイルの中身が変わる = Actions が 1日1回だけ commit する。
    「いつまで動いていたか」の記録になり、リポジトリが無活動になるのも防ぐ。
    """
    return jst_now().strftime("%Y-%m-%d")


def digest_due(state, now):
    """デイリーレポートを送るべきか。

    cron を1本増やすのではなく、10分ごとの実行が毎回これを判定する。
    GitHub のスケジューラは遅延・スキップがあるため、19:00 ちょうどの
    cron に賭けると「その1回が飛んだら届かない」ことになる。
    この方式なら何回飛んでも次の実行が拾うので、必ず届く。
    送信に失敗した場合も last_digest を更新しないので、次の実行で再送する。
    """
    if now.hour < DIGEST_HOUR:
        return False
    return state.get("last_digest") != now.strftime("%Y-%m-%d")


def actions_history_url():
    """この workflow の実行履歴ページ。Actions 上でしか組み立てられない。"""
    server = env("GITHUB_SERVER_URL", "https://github.com")
    repo = env("GITHUB_REPOSITORY")
    if not repo:
        return None
    ref = env("GITHUB_WORKFLOW_REF")  # owner/repo/.github/workflows/x.yml@refs/heads/main
    workflow = ref.split("@")[0].rsplit("/", 1)[-1] if ref else "ur-watch.yml"
    return f"{server}/{repo}/actions/workflows/{workflow}"


def format_digest(state, rooms, error, now):
    """「異常がないこと」を伝えるための定期レポート。"""
    since = state.get("last_digest_at") or "監視開始"
    new_rooms = state.get("since_digest", [])

    lines = [
        f"📋 {DANCHI_NAME} デイリーレポート",
        f"{since} 〜 {now.strftime('%m/%d %H:%M')}",
        "",
    ]

    if error:
        lines += [
            "⚠️ 現在 UR のデータを取得できていません",
            f"　{error}",
            "",
            f"この間の新着 {len(new_rooms)}件",
        ]
    else:
        lines += [
            f"この間の新着 {len(new_rooms)}件",
            f"現在の空室 {len(rooms)}件",
        ]
        if not new_rooms:
            lines += ["", "監視は正常に動いています。"]

    if new_rooms:
        lines += [""] + ["\n\n".join(format_room(r) for r in new_rooms)]

    lines += ["", DANCHI_URL]
    history = actions_history_url()
    if history:
        lines.append(f"チェック履歴: {history}")
    return "\n".join(lines)


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
LINE_BROADCAST = "https://api.line.me/v2/bot/message/broadcast"


def line_push(message):
    """公式アカウントを友だち追加している全員に送る。

    broadcast を使うので宛先の userId は不要。受け取りたい人は
    公式アカウントを友だち追加するだけでよい。
    通数は宛先1人につき1通としてカウントされる（2人なら2通）。

    送信に失敗しても例外は投げない（ログに出すだけ）。空室の取得自体は
    成功しているので、通知の失敗で run 全体を落とす必要はない。
    """
    token = (os.environ.get("LINE_CHANNEL_ACCESS_TOKEN") or "").strip()

    if not token:
        print("LINE の認証情報が未設定のため送信をスキップ:")
        print(message)
        return False

    print(f"LINE: token {len(token)}文字 / 友だち全員に broadcast します")

    body = json.dumps(
        {"messages": [{"type": "text", "text": message[:4900]}]}
    ).encode("utf-8")
    req = urllib.request.Request(
        LINE_BROADCAST,
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
        elif e.code == 403:
            print("→ このチャネルで broadcast が使えない設定になっています。"
                  "Messaging API チャネルか確認してください。")
        elif e.code == 429:
            print("→ 送信上限に達しました。無料プランの月間通数を "
                  "LINE Official Account Manager で確認してください。")
        return False
    except Exception as e:  # noqa: BLE001
        print(f"LINE 送信 失敗: {type(e).__name__}: {e}")
        return False


# ---- main ---------------------------------------------------------------
def main():
    state = load_state()
    now = jst_now()

    error = None
    rooms = []
    try:
        rooms = [normalize(r) for r in fetch_rooms()]
        rooms = [r for r in rooms if passes_filter(r)]
    except Exception as e:  # noqa: BLE001
        error = e
        print(e, file=sys.stderr)

    if error is None:
        state.pop("error_notified", None)
        known = set(state.get("ids", []))
        first_run = "ids" not in state

        if first_run:
            msg = (f"🏠 {DANCHI_NAME} の空室監視を開始しました\n"
                   f"10分ごとにチェックし、毎日{DIGEST_HOUR}時に"
                   f"レポートを送ります。\n\n")
            msg += (
                f"現在の空室 {len(rooms)}件\n\n"
                + "\n\n".join(format_room(r) for r in rooms)
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
                # デイリーレポートに載せるため溜めておく（上限50件）
                state["since_digest"] = (
                    state.get("since_digest", []) + new_rooms
                )[-50:]
            else:
                print(f"新着なし（現在の空室 {len(rooms)}件）")

        state["ids"] = sorted(r["id"] for r in rooms)
        state["count"] = len(rooms)
        state["checked"] = jst_today()
    elif not state.get("error_notified"):
        line_push(f"⚠️ UR空室チェックが失敗しました\n{DANCHI_NAME}\n\n{error}")
        state["error_notified"] = True

    # デイリーレポートは取得に失敗していても送る。
    # 「しばらく通知が無い」状態を作らないことが目的なので、
    # むしろ失敗しているときこそ知らせる必要がある。
    if digest_due(state, now):
        if line_push(format_digest(state, rooms, error, now)):
            state["last_digest"] = now.strftime("%Y-%m-%d")
            state["last_digest_at"] = now.strftime("%m/%d %H:%M")
            state["since_digest"] = []
        else:
            print("デイリーレポートの送信に失敗。次の実行で再送します。")

    save_state(state)
    if error:
        sys.exit(1)


if __name__ == "__main__":
    main()
