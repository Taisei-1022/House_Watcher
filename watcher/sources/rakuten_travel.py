#!/usr/bin/env python3
"""楽天トラベル空室検索API（公式・無料）で宿の空室と料金を取る。

スクレイピングではなく公式 API なので、規約・負荷・安定性のどれでも
HTML を読むより大幅に有利。ホテル価格監視はこれを第一候補にする。

  https://webservice.rakuten.co.jp/documentation/vacant-hotel-search

要: 楽天ウェブサービスのアプリ ID（無料登録）を RAKUTEN_APP_ID に入れる。

注意（これを踏むと毎回誤報する）:
  空室が1件も無いとき、この API は 200 + 空配列ではなく
  **HTTP 404 + {"error":"not_found"}** を返す。取得失敗と区別しないと
  「空きが無い」たびに ⚠️ が飛ぶ。allow_status=(404,) で本文を受け取り、
  not_found を 0件として扱う。

レスポンス構造（実装側で防御的に歩く）:
  hotels[*].hotel[0].hotelBasicInfo        宿の基本情報
  hotels[*].hotel[n].roomInfo[*]           プラン/部屋。roomBasicInfo と
                                           dailyCharge が別要素で並ぶ。
  並び順に依存せず「そのキーを持つ要素」を探して突き合わせる。
"""

import json

from .. import http
from ..util import env

ENDPOINT = (
    "https://openapi.rakuten.co.jp/engine/api/Travel/"
    "VacantHotelSearch/20170426"
)


def fetch(cfg):
    app_id = env("RAKUTEN_APP_ID", cfg.get("application_id", ""))
    if not app_id:
        raise ValueError(
            "RAKUTEN_APP_ID が未設定です。"
            "https://webservice.rakuten.co.jp/ で無料のアプリ ID を取得して"
            "GitHub の Secrets に入れてください。"
        )
    hotel_no = cfg.get("hotel_no")
    if not hotel_no:
        raise ValueError("rakuten_travel には hotel_no が必要です")
    for key in ("checkin", "checkout"):
        if not cfg.get(key):
            raise ValueError(f"rakuten_travel には {key} が必要です (YYYY-MM-DD)")

    params = {
        "applicationId": app_id,
        "format": "json",
        "hotelNo": str(hotel_no),
        "checkinDate": cfg["checkin"],
        "checkoutDate": cfg["checkout"],
        "adultNum": str(cfg.get("adults", 2)),
        # 1 = 宿泊プランごと。価格監視はプラン単位で見たいので既定にする。
        "searchPattern": str(cfg.get("search_pattern", 1)),
        "responseType": cfg.get("response_type", "middle"),
    }
    if cfg.get("rooms"):
        params["roomNum"] = str(cfg["rooms"])

    url = ENDPOINT + "?" + _qs(params)
    body = http.fetch(url, check_robots=False, allow_status=(404,))

    try:
        data = json.loads(body)
    except json.JSONDecodeError as e:
        raise http.SanityError(f"JSON を返しませんでした: {body[:200]!r}") from e

    if isinstance(data, dict) and data.get("error"):
        # not_found = その日程に空室が無い。これは正常な「0件」。
        if data["error"] == "not_found":
            return []
        raise http.FetchError(
            f"楽天API エラー: {data['error']} "
            f"{data.get('error_description', '')}")

    hotels = data.get("hotels") if isinstance(data, dict) else None
    if not isinstance(hotels, list):
        raise http.SanityError(f"応答に hotels がありません: {body[:200]!r}")

    items = []
    for entry in hotels:
        parts = entry.get("hotel") if isinstance(entry, dict) else None
        if not isinstance(parts, list):
            continue
        basic = _find(parts, "hotelBasicInfo") or {}
        hotel_name = basic.get("hotelName") or str(hotel_no)
        hotel_url = basic.get("hotelInformationUrl") or ""

        for group in parts:
            rooms = group.get("roomInfo") if isinstance(group, dict) else None
            if not isinstance(rooms, list):
                continue
            room = _find(rooms, "roomBasicInfo") or {}
            charge = _find(rooms, "dailyCharge") or {}
            price = charge.get("total") or charge.get("rakutenCharge")
            if price in (None, "", 0):
                continue

            plan = room.get("planName") or ""
            room_name = room.get("roomName") or ""
            label = " / ".join(p for p in (hotel_name, room_name, plan) if p)
            items.append({
                "id": str(room.get("planId")
                          or f"{room.get('roomClass', '')}|{plan}|{room_name}"),
                "label": label,
                "price": price,
                "hotel": hotel_name,
                "plan": plan,
                "room": room_name,
                "stay_date": charge.get("stayDate", ""),
                "url": room.get("reserveUrl") or hotel_url,
            })
    return items


def _find(parts, key):
    """リストの中から、そのキーを持つ要素の中身を返す。並び順に依存しない。"""
    for p in parts:
        if isinstance(p, dict) and isinstance(p.get(key), dict):
            return p[key]
    return None


def _qs(params):
    import urllib.parse
    return urllib.parse.urlencode(params)
