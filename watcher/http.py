#!/usr/bin/env python3
"""取得層。リトライ・ホスト単位の間隔制限・robots.txt・sanity 判定。

ここが雑だと IP ごと弾かれて全 watch が同時に死ぬので、
行儀の良さは「マナー」ではなく可用性の要件として実装する。
"""

import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from http.cookiejar import CookieJar

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

RETRIES = 3
RETRY_WAIT = (3, 8)  # 秒。RETRIES - 1 個必要
TIMEOUT = 30

# 同一ホストへの最短間隔。1 run の中で複数 watch が同じサイトを
# 見ていても、これだけは空ける。
HOST_INTERVAL = 2.0

_last_hit = {}
_robots = {}


class FetchError(RuntimeError):
    """取得に失敗した。「取れたが空」とは明確に区別する。"""


class SanityError(FetchError):
    """取得はできたが中身が想定と違う。

    これを「空き無し」と解釈してはいけない。待合室・メンテ画面・
    ページ構成変更を空振りと誤認すると、監視は黙ったまま死ぬ。
    """


def _host(url):
    return urllib.parse.urlsplit(url).netloc


def _throttle(url):
    host = _host(url)
    wait = HOST_INTERVAL - (time.monotonic() - _last_hit.get(host, 0.0))
    if wait > 0:
        time.sleep(wait)
    _last_hit[host] = time.monotonic()


def robots_allows(url, user_agent=UA):
    """robots.txt で拒否されていないか。取得できなければ許可扱い。

    robots.txt が引けないことを理由に監視を止めると、一時的な障害で
    全 watch が無効になってしまう。拒否が明示されている場合だけ止める。
    """
    host = _host(url)
    if host not in _robots:
        rp = urllib.robotparser.RobotFileParser()
        base = urllib.parse.urlsplit(url)
        rp.set_url(f"{base.scheme}://{host}/robots.txt")
        try:
            rp.read()
        except Exception:  # noqa: BLE001
            rp = None
        _robots[host] = rp
    rp = _robots[host]
    if rp is None:
        return True
    try:
        return rp.can_fetch(user_agent, url)
    except Exception:  # noqa: BLE001
        return True


def fetch(url, data=None, headers=None, sanity=None, cookies=False,
          check_robots=True, timeout=TIMEOUT, allow_status=()):
    """本文を文字列で返す。data を渡すと POST。

    sanity: 本文に必ず含まれるべき文字列。無ければ SanityError。
    cookies: True なら Cookie を保持してリダイレクトを追う（待合室対策）。
    allow_status: 本文を正常として受け取る 4xx。
        楽天トラベル API は空室ゼロのとき 404 + not_found を返す。これを
        失敗として扱うと「空きが無い」たびに ⚠️ が飛ぶので、呼び側が
        「取れたが空」と解釈できるよう本文をそのまま返す必要がある。
    """
    if check_robots and not robots_allows(url):
        raise FetchError(f"robots.txt が許可していません: {url}")

    body = None
    if data is not None:
        body = (urllib.parse.urlencode(data).encode("utf-8")
                if not isinstance(data, (bytes, str)) else
                (data.encode("utf-8") if isinstance(data, str) else data))

    base_headers = {
        "User-Agent": UA,
        "Accept-Language": "ja,en;q=0.9",
    }
    base_headers.update(headers or {})

    opener = (urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(CookieJar()))
        if cookies else urllib.request.build_opener())

    last_error = None
    for attempt in range(RETRIES):
        _throttle(url)
        req = urllib.request.Request(url, data=body, headers=base_headers)
        try:
            with opener.open(req, timeout=timeout) as res:
                payload = res.read().decode("utf-8", errors="replace")
            if sanity and sanity not in payload:
                raise SanityError(
                    f"ページの内容が想定外です ({len(payload)}文字, "
                    f"'{sanity}' が見つかりません)。待合室で止められたか、"
                    "ページ構成が変わった可能性があります。"
                )
            return payload
        except urllib.error.HTTPError as e:
            # 429/503 はこちらが急ぎすぎ。Retry-After があれば従う。
            if e.code in (429, 503):
                last_error = e
                hinted = _retry_after(e)
                if attempt < RETRIES - 1:
                    print(f"  {e.code} のため {hinted}秒待機して再試行")
                    time.sleep(hinted)
                    continue
            elif e.code in allow_status:
                return e.read().decode("utf-8", errors="replace")
            elif e.code < 500:
                # 4xx はこちらの組み立てミス。粘っても直らない。
                detail = e.read().decode("utf-8", errors="replace")[:200]
                raise FetchError(
                    f"HTTP {e.code} {e.reason}: {url} {detail}") from e
            else:
                last_error = e
        except SanityError as e:
            # 待合室は時間を置けば抜けられることがあるので再試行に含める。
            last_error = e
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last_error = e

        if attempt < RETRIES - 1:
            wait = RETRY_WAIT[attempt]
            print(f"  一時エラー ({last_error}) — {wait}秒後に再試行 "
                  f"({attempt + 2}/{RETRIES})")
            time.sleep(wait)

    if isinstance(last_error, SanityError):
        raise last_error
    raise FetchError(f"{type(last_error).__name__}: {last_error}")


def _retry_after(err, cap=60):
    try:
        return min(int(err.headers.get("Retry-After", "")), cap)
    except (TypeError, ValueError):
        return 15
