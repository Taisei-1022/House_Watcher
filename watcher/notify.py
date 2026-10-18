#!/usr/bin/env python3
"""通知層。いまは LINE broadcast だけ。

送信に失敗しても例外は投げない（ログに出して False を返す）。取得自体は
成功しているので、通知の失敗で他の watch まで止める必要はない。
戻り値を見て「送れたときだけ state を進める」ことで次回再送できる。
"""

import json
import os
import urllib.error
import urllib.request

BROADCAST = "https://api.line.me/v2/bot/message/broadcast"
LIMIT = 4900  # LINE のテキスト上限 5000 文字に対する安全枠


def line_push(message):
    """公式アカウントを友だち追加している全員に送る。

    broadcast なので宛先の userId は不要。受け取りたい人は友だち追加するだけ。
    通数は宛先1人につき1通としてカウントされる。
    """
    token = (os.environ.get("LINE_CHANNEL_ACCESS_TOKEN") or "").strip()
    if not token:
        print("LINE 未設定のため送信をスキップ:")
        print(message)
        return False

    body = json.dumps(
        {"messages": [{"type": "text", "text": message[:LIMIT]}]}
    ).encode("utf-8")
    req = urllib.request.Request(
        BROADCAST,
        data=body,
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            print("LINE 送信 OK:", res.status)
            return True
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")
        print(f"LINE 送信 失敗: {e.code} {detail}")
        if e.code == 401:
            print("→ チャネルアクセストークン（長期）が違います。")
        elif e.code == 403:
            print("→ このチャネルで broadcast が使えない設定です。")
        elif e.code == 429:
            print("→ 送信上限。無料プランは月200通なので有料化が必要です。")
        return False
    except Exception as e:  # noqa: BLE001
        print(f"LINE 送信 失敗: {type(e).__name__}: {e}")
        return False


def format_item(item, note=""):
    """アイテム1件を人間向けに。未知のフィールドも落とさず出す。"""
    label = item.get("label") or item.get("name") or item.get("id") or "(項目)"
    lines = [f"■ {label}"]
    if note:
        lines.append(f"　{note}")
    shown = {"id", "key", "label", "name", "url", "link"}
    detail = [f"{k}: {v}" for k, v in item.items()
              if k not in shown and v not in (None, "")]
    if detail:
        lines.append("　" + " / ".join(detail))
    url = item.get("url") or item.get("link")
    if url:
        lines.append(f"　{url}")
    return "\n".join(lines)
