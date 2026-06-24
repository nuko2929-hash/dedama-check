"""晒し屋X予想の収集器。
  1. twitterapi.io で各晒し屋アカウントの最新投稿を取得（公式APIは使わない）
  2. Claude API で投稿を「対象日/店舗/機種/末尾」に構造化（登録店舗だけ）
  3. data/predictions.json に保存（画面がそのまま読む）

鍵:
  TWITTERAPI_KEY / ANTHROPIC_API_KEY
  → 環境変数（GitHub Actions）または ~/.dedama-keys.env（ローカル）から読む
"""
import os, json, re, time, pathlib, urllib.request, urllib.parse
from datetime import date, datetime, timedelta

ROOT = pathlib.Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
COLL = ROOT / "collector"
RECENT_DAYS = 4        # 何日前までの投稿を見るか
TWEETS_PER_ACCT = 12   # 各アカウントの直近何件を見るか
CLAUDE_MODEL = "claude-sonnet-4-6"


def load_keys():
    k = {"TWITTERAPI_KEY": os.environ.get("TWITTERAPI_KEY", ""),
         "ANTHROPIC_API_KEY": os.environ.get("ANTHROPIC_API_KEY", "")}
    envf = pathlib.Path.home() / ".dedama-keys.env"
    if envf.exists():
        for line in envf.read_text().splitlines():
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                a, b = line.split("=", 1)
                a = a.strip()
                if not k.get(a):
                    k[a] = b.strip()
    return k


def tracked_stores():
    """登録店舗の (正規名, [別名...]) リスト。Claudeの照合ヒントに使う。"""
    s = json.loads((DATA / "stores.json").read_text(encoding="utf-8"))
    return [{"name": x["name"], "aliases": x.get("aliases", [])} for x in s]


# ── twitterapi.io ──
def fetch_tweets(handle, key):
    url = "https://api.twitterapi.io/twitter/user/last_tweets?userName=" + urllib.parse.quote(handle)
    req = urllib.request.Request(url, headers={"X-API-Key": key})
    with urllib.request.urlopen(req, timeout=30) as r:
        d = json.loads(r.read().decode("utf-8", "replace"))
    return (d.get("data") or {}).get("tweets", []) or []


def parse_tweet_time(t):
    """tweetのcreatedAtをISO日付に。失敗時None。"""
    s = t.get("createdAt") or ""
    for fmt in ("%a %b %d %H:%M:%S %z %Y", "%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except Exception:
            pass
    return None


# ── Claude 構造化 ──
def claude_extract(account, tweets, stores, anthropic_key, today):
    """1アカウントの複数投稿をまとめて構造化し、予想レコード配列を返す。"""
    store_hint = "\n".join(f"- {s['name']}" + (f"（別名: {', '.join(s['aliases'])}）" if s["aliases"] else "")
                           for s in stores)
    posts = []
    for i, t in enumerate(tweets):
        posts.append(f"[投稿{i}] 投稿日={t.get('_posted','?')} url={t.get('url','')}\n{t.get('text','')}")
    posts_text = "\n\n".join(posts)

    prompt = f"""あなたはパチスロ「晒し屋」のXポストを構造化するアシスタントです。
今日は {today} です。

# 登録店舗（この店に該当する投稿だけ抽出。表記ゆれOK。下記以外の店は完全に無視）
{store_hint}

# 発信者
{account}

# 投稿群
{posts_text}

# 指示
各投稿から、登録店舗に該当する「予想」を抜き出してJSON配列で出力してください。
- お品書き／狙い目／推奨店リストなど、その日に強いと示唆している店を予想とみなす
- 1投稿に複数店あれば、店ごとに1レコード
- target_date: その投稿が対象にしている日(YYYY-MM-DD)。「明日25日」等は投稿日から推定。日付が無ければ投稿日を使う
- machines: 機種名が挙がっていれば配列で(無ければ空配列)
- matsubi: 末尾の数字が挙がっていれば配列で(無ければ空配列)
- note: 投稿の要点を25字程度で
- post_index: その投稿の番号(整数)
- store: 必ず登録店舗の「正規名」(上のリストの名称)に正規化

登録店舗が1つも該当しない場合は空配列 [] を返す。
**JSON配列のみ**を出力（前後の説明文・コードフェンス不要）。"""

    body = json.dumps({
        "model": CLAUDE_MODEL,
        "max_tokens": 2000,
        "messages": [{"role": "user", "content": prompt}],
    }).encode("utf-8")
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=body, headers={
        "x-api-key": anthropic_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    })
    with urllib.request.urlopen(req, timeout=60) as r:
        resp = json.loads(r.read().decode("utf-8", "replace"))
    text = "".join(c.get("text", "") for c in resp.get("content", []) if c.get("type") == "text").strip()
    text = re.sub(r"^```(json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    m = re.search(r"\[.*\]", text, re.DOTALL)
    if not m:
        return []
    try:
        return json.loads(m.group(0))
    except Exception:
        return []


def main():
    keys = load_keys()
    if not keys.get("TWITTERAPI_KEY") or not keys.get("ANTHROPIC_API_KEY"):
        print("⚠ 鍵が無い(TWITTERAPI_KEY / ANTHROPIC_API_KEY)。中断。")
        return
    sources = json.loads((COLL / "sources.json").read_text(encoding="utf-8"))["sources"]
    stores = tracked_stores()
    today = date.today().isoformat()
    floor = (date.today() - timedelta(days=RECENT_DAYS)).isoformat()

    out = []
    for src in sources:
        if not src.get("active", True):
            continue
        name, handle = src["name"], src["handle"]
        try:
            raw = fetch_tweets(handle, keys["TWITTERAPI_KEY"])[:TWEETS_PER_ACCT]
        except Exception as e:
            print(f"  ! {name} 取得失敗: {e}")
            continue
        # 直近ぶんだけ＋投稿日付与
        recent = []
        for t in raw:
            pt = parse_tweet_time(t) or today
            if pt >= floor:
                t["_posted"] = pt
                recent.append(t)
        if not recent:
            print(f"▼ {name}: 直近投稿なし")
            continue
        try:
            recs = claude_extract(name, recent, stores, keys["ANTHROPIC_API_KEY"], today)
        except Exception as e:
            print(f"  ! {name} 構造化失敗: {e}")
            continue
        n = 0
        for r in recs:
            pi = r.get("post_index")
            tw = recent[pi] if isinstance(pi, int) and 0 <= pi < len(recent) else None
            store = (r.get("store") or "").strip()
            if not store:
                continue
            out.append({
                "id": f"{handle}-{(tw or {}).get('id', len(out))}-{store}",
                "source": name, "handle": handle,
                "posted_at": (tw or {}).get("_posted", today),
                "target_date": r.get("target_date") or (tw or {}).get("_posted", today),
                "store": store,
                "area": None,  # 後で店舗マスタから補完
                "machines": r.get("machines") or [],
                "matsubi": r.get("matsubi") or [],
                "note": r.get("note") or "",
                "url": (tw or {}).get("url", f"https://x.com/{handle}"),
            })
            n += 1
        print(f"▼ {name}: 投稿{len(recent)}件 → 予想{n}件")
        time.sleep(1)

    # エリアを店舗マスタから補完
    smap = {s["name"]: s.get("area") for s in json.loads((DATA / "stores.json").read_text(encoding="utf-8"))}
    for p in out:
        p["area"] = smap.get(p["store"])

    out.sort(key=lambda p: p.get("target_date") or "", reverse=True)
    res = {"generated_at": datetime.now().isoformat(timespec="minutes"),
           "count": len(out), "predictions": out}
    (DATA / "predictions.json").write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n保存: {len(out)}予想 → data/predictions.json")


if __name__ == "__main__":
    main()
