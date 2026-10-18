#!/usr/bin/env python3
"""状態の保存。watch ごとに名前空間を分けて1ファイルに集約する。

GitHub Actions から git commit で永続化するので、ファイルは少ないほうがよい。
watch を増やすたびにファイルが増えると commit が衝突しやすくなる。
"""

import json

DEFAULT_PATH = "watch_state.json"


def load(path=DEFAULT_PATH):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        data = {}
    data.setdefault("watches", {})
    data.setdefault("digest", {})
    return data


def save(data, path=DEFAULT_PATH):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)


def for_watch(data, watch_id):
    return data["watches"].setdefault(watch_id, {})
