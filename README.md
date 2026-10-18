# House Watcher

流動的なものを定期的にチェックして、条件に当てはまったら LINE に通知する。
GitHub Actions で10分ごとに動く。依存ライブラリなし（標準ライブラリのみ）。

```
run.py              汎用エンジンのランナー（watches.json を読む）
watches.json        監視定義。ここを書けば新しい対象が増える
watcher/            汎用エンジン本体
ur_watch.py         UR ヌーヴェル赤羽台（専用・稼働中）
tests/              python -m unittest discover -s tests
```

専用の ur_watch.py と汎用エンジンは独立に動く。専用側は実績があるので、
汎用エンジンが同等に動くことを確認できるまで置き換えない。

栗駒山いわかがみ平の駐車場監視 (webket_watch.py) は対象日の終了に伴い
f30d767 で削除した。実装と調査結果は b003be8 の履歴にある。

## 新しい監視を足す

```bash
python run.py --only <id> --dry-run --force
```

`--dry-run` は LINE に送らず state も書かないので、抽出が通るまで何度でも回せる。
通ったら `watches.json` の `enabled` を `true` にする。

```bash
python run.py --list    # 定義一覧
python run.py           # 全部実行（Actions がこれを呼ぶ）
```

### 取得方法の選び方

**1. `json_api` — 裏の JSON API を直叩き（最優先）**

ブラウザの DevTools で Network タブを開き、XHR を探す。SPA 化しているサイトは
だいたい裏に API がある。HTML を読むより壊れにくく、速く、サイトへの負荷も軽い。
UR がこれで取れたので10分監視が成立している。

```json
{
  "type": "json_api",
  "url": "https://example.test/api/rooms",
  "data": { "danchi": "694" },
  "items": "data.rooms[*]",
  "fields": { "id": "roomId", "label": "roomName", "price": "charge" },
  "sanity_path": "status",
  "paginate": { "param": "page", "total_path": "data.total" },
  "link_prefix": "https://example.test"
}
```

| 項目 | 意味 |
|---|---|
| `data` | あると POST、無いと GET |
| `items` | 繰り返しの単位へのパス。`[*]` で展開、`[0]` `[-1]` も使える |
| `fields` | アイテム内の相対パス → フィールド名 |
| `sanity_path` | 応答に必ずあるパス。無ければ失敗扱い（後述） |
| `paginate` | `total_path` の件数まで `param` を進める。全件揃わなければ失敗 |
| `null_is_empty` | 本文が `null` のとき0件とみなす（既定 true、UR 用） |
| `empty_markers` | `{"error": "..."}` がこの文字列なら0件（楽天 `not_found` 用） |
| `link_prefix` | `/path` で返る相対リンクを絶対化 |
| `allow_status` | 本文を正常として受け取る 4xx |

**2. `html_regex` — HTML を名前付きキャプチャで切る**

API が見つからないときの受け皿。`(?P<price>…)` の名前がそのままフィールドになる。
`sanity` は**必須**（省略すると起動時に落ちる）。

```json
{
  "type": "html_regex",
  "url": "https://example.test/item/123",
  "sanity": "商品名",
  "flags": "s",
  "pattern": "data-sku=\"(?P<id>[^\"]+)\".*?<span>(?P<status>[^<]*)</span>",
  "cookies": true
}
```

`cookies: true` で Cookie を保持してリダイレクトを追う（Webket の順番待ち行列対策）。

**3. 専用アダプタ — 公式 API があるとき**

`watcher/sources/rakuten_travel.py` が例。公式 API は規約・負荷・安定性の
どれでもスクレイピングより有利なので、あるなら必ずこちらを使う。

### 条件

```json
{ "type": "appeared" }
{ "type": "threshold", "field": "price", "op": "<=", "value": 20000 }
{ "type": "contains", "field": "status", "value": "在庫あり" }
{ "type": "not_contains", "field": "status", "value": "SOLD OUT" }
{ "type": "changed", "field": "price" }
{ "type": "gone" }
{ "type": "all", "conditions": [ … ] }
{ "type": "any", "conditions": [ … ] }
{ "type": "not", "condition": { … } }
```

`op` は `<= < >= > == !=`。値は `"20,000円"` のような文字列でも数値を拾う。

## 通知のルール（ここを誤解すると誤爆する）

**「成立している集合」を保存し、新しく成立したものだけ送る。**
UR の「新しい部屋」も、駐車場の「買える時間帯」も、「2万円を下回った」も、
すべてこの集合差分ひとつで表している。

- 同じキーが成立し続けている間は**1回だけ**通知する
- 条件が崩れて再成立したら、また通知する（値下がり→戻り→再値下がりを取りこぼさない）
- ただし `notify.cooldown`（既定6時間）以内の再通知は**抑制され、破棄される**。
  明けてから配信し直すことはしない — その時点で条件が成立し続けているだけで、
  新しい情報が無いため
- 値の推移そのものを追いたいなら `threshold` ではなく `changed` を使う
- `notify.once: true` で、一度通知したらその watch を止める（チケットが取れたら不要）
- 初回実行は件数サマリだけ送る。全件を「新着」として流さない

## 「取れたが空」と「取得に失敗した」を混ぜない

変化監視で最悪の壊れ方は、**障害を「空きなし」と誤認して黙り込むこと**。
通知が来ないので正常に見えるが、実は何も見ていない。

エンジンはこれを分けるために:

- `sanity` / `sanity_path` を満たさない応答は `SanityError`（失敗扱い）
- ページングで全件揃わなければ失敗扱い（取りこぼしを「消失」にしない）
- 閾値条件は、値が取れないアイテムを**成立させない**（抽出が壊れた瞬間の全件通知を防ぐ）
- 失敗時は成立集合を更新しない（復旧時に全件が「新着」として飛ばない）
- 失敗は1回だけ `⚠️` で通知し、復旧したらまた通知できる状態に戻す
- デイリーレポート（既定19時）で全 watch の状態を送る。
  「しばらく通知がない」状態を作らないのが目的なので、失敗しているときこそ送る

新しいレシピを書くときは `sanity` を必ず入れること。`html_regex` では必須にしてある。

## サイトへの行儀は可用性の要件

IP ごと弾かれたら全 watch が同時に死ぬので、マナーの話ではない。

- `robots.txt` を見る（引けないときは許可扱い。一時障害で監視を止めないため）
- 同一ホストへは最短2秒間隔（`watcher/http.py` の `HOST_INTERVAL`）
- 429 / 503 は `Retry-After` に従ってバックオフ
- 取得失敗時も「叩いた」ことを記録する。1時間間隔の watch が失敗したとき
  10分ごとに再試行してしまうと、弱っている相手を最も強く叩くことになる
- bot 対策の回避はしない

規約で自動アクセスを禁じているサイトは対象にしない。個人利用と有料サービスでは
法的な立ち位置が変わるので、他人向けに公開するなら対象サイトは許可リスト方式にして
規約を1件ずつ確認する。

## セットアップ

GitHub の Secrets に:

| 名前 | 用途 |
|---|---|
| `LINE_CHANNEL_ACCESS_TOKEN` | LINE Messaging API のチャネルアクセストークン（長期） |
| `RAKUTEN_APP_ID` | [楽天ウェブサービス](https://webservice.rakuten.co.jp/)のアプリ ID（無料・ホテル監視に使う） |

LINE は broadcast なので宛先指定が不要。受け取りたい人は公式アカウントを友だち追加するだけ。
ただし**無料プランは月200通**で、宛先1人につき1通カウントされる。

10分ごとの実行は外部 cron（cron-job.org）から `workflow_dispatch` を叩いている。
GitHub の `schedule` だけだと数時間おきにしか発火しないため。
**`.github/workflows/ur-watch.yml` のファイル名は変えない**（外部 cron がこの名前を指している）。

## 既知の制約

- JS 実行が必要なページは取れない。ヘッドレスブラウザが必要
- `html_regex` はパーサではないので、HTML 構造の変更に弱い（`sanity` で検知はできる）
- `ur_watch.py` と `watcher/` で LINE 送信と状態保存が重複している。
  解消するなら、ur_watch.py を `watches.json` の `ur-akabanedai-generic` に
  置き換えて削除するのが筋（結合を増やすのではなく、片方を無くす）
- **順番待ち行列のあるサイトは、需要が高いときだけ監視が死ぬ**。
  削除済みの webket.jp 監視で実際に起きた: 10/09 15:00 以降ずっと失敗していて、
  席が売れている最中こそ取得できなかった。`http.py` の
  RETRIES=3 / 3-8秒待ちでは行列を抜けられない。チケット系を対象にするなら
  行列を待つ処理が必須（これが無いと一番必要な瞬間に機能しない）
- 同じ URL を複数の watch が見ても取得はまとめられない（自分用なので実害なし）
