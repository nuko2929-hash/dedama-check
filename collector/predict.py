"""晒し屋X予想の収集器。
  1. twitterapi.io で各晒し屋アカウントの投稿を取得（公式APIは使わない・ページ送り対応）
  2. Claude API で投稿を「対象日/店舗/機種/末尾」に構造化（登録店舗だけ）
  3. data/predictions.json に保存（既存とマージ＝過去ぶんは消えない）

鍵: TWITTERAPI_KEY / ANTHROPIC_API_KEY
    → 環境変数（GitHub Actions）または ~/.dedama-keys.env（ローカル）

収集する日数: 環境変数 PREDICT_DAYS（既定4）。バックフィル時は 30 等を指定。
"""
import os, sys, json, re, time, pathlib, urllib.request, urllib.parse
from datetime import date, datetime, timedelta

ROOT = pathlib.Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
COLL = ROOT / "collector"
DAYS = int(os.environ.get("PREDICT_DAYS", "4"))
MAX_PAGES = 25            # 1アカウントあたり最大ページ数(暴走防止)
CHUNK = 22               # Claudeに一度に渡す投稿数
# 抽出モデル。既定 Haiku(安い)。質が落ちたら env PREDICT_MODEL=claude-sonnet-4-6 で即戻せる。
CLAUDE_MODEL = os.environ.get("PREDICT_MODEL", "claude-haiku-4-5-20251001")


def load_keys():
    k = {"TWITTERAPI_KEY": os.environ.get("TWITTERAPI_KEY", ""),
         "ANTHROPIC_API_KEY": os.environ.get("ANTHROPIC_API_KEY", "")}
    envf = pathlib.Path.home() / ".dedama-keys.env"
    if envf.exists():
        for line in envf.read_text().splitlines():
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                a, b = line.split("=", 1)
                if not k.get(a.strip()):
                    k[a.strip()] = b.strip()
    return k


def tracked_stores():
    s = json.loads((DATA / "stores.json").read_text(encoding="utf-8"))
    return [{"name": x["name"], "aliases": x.get("aliases", [])} for x in s]


SEEN_PATH = DATA / "_seen_tweets.json"
SEEN_CAP = 9000  # 保持するツイートID数の上限(ファイル肥大防止)


def load_seen():
    """Claudeに投げ済みのツイートID集合。初回は既存予想のIDから種を撒く。"""
    if SEEN_PATH.exists():
        try:
            return set(json.loads(SEEN_PATH.read_text(encoding="utf-8")))
        except Exception:
            pass
    seed = set()
    p = DATA / "predictions.json"
    if p.exists():
        try:
            for pr in json.loads(p.read_text(encoding="utf-8")).get("predictions", []):
                # id 形式: "{handle}-{tweetid}-{store}" → tweetid を抽出
                parts = (pr.get("id") or "").split("-")
                if len(parts) >= 3:
                    seed.add(parts[1])
        except Exception:
            pass
    return seed


def save_seen(seen):
    ids = list(seen)[-SEEN_CAP:]
    SEEN_PATH.write_text(json.dumps(ids, ensure_ascii=False), encoding="utf-8")


def _get(url, key):
    req = urllib.request.Request(url, headers={"X-API-Key": key})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def parse_tweet_time(t):
    s = t.get("createdAt") or ""
    for fmt in ("%a %b %d %H:%M:%S %z %Y", "%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except Exception:
            pass
    return None


def fetch_tweets(handle, key, floor_iso, seen):
    """floor_iso 以降の「未処理」投稿だけを、ページ送りしながら集める。
    last_tweets は新着順なので、既読(seen)/枠外しか無いページに到達した時点で
    以降は全部古い＝そこでページ送りを打ち切る。これで毎回同じツイートを
    再取得して twitterapi.io 代を無駄に払うのを防ぐ(ピン留め古ツイートが
    先頭に来ても、ページ単位で新規有無を見るので取りこぼさない)。
    返り値 (tweets, ok)。ok=False は1ページ目すら取得できなかった＝
    twitterapi.io のキー切れ/レート制限/障害の疑い（取得成功で0件とは区別する）。"""
    base = "https://api.twitterapi.io/twitter/user/last_tweets?userName=" + urllib.parse.quote(handle)
    out, cursor, ok = [], None, False
    for _ in range(MAX_PAGES):
        url = base + (f"&cursor={urllib.parse.quote(cursor)}" if cursor else "")
        try:
            resp = _get(url, key)
        except Exception as e:
            print(f"    (fetch err {handle}: {e})")
            break
        ok = True
        data = resp.get("data") or {}
        page = data.get("tweets") or (resp.get("tweets") if isinstance(resp.get("tweets"), list) else []) or []
        if not page:
            break
        oldest = "9999"
        page_had_new = False
        for t in page:
            pt = parse_tweet_time(t) or date.today().isoformat()
            t["_posted"] = pt
            oldest = min(oldest, pt)
            if pt < floor_iso:
                continue                       # 枠外(古すぎ)
            if str(t.get("id")) in seen:
                continue                       # 既読(処理済) — 再フェッチしない
            out.append(t)
            page_had_new = True
        cursor = resp.get("next_cursor") or data.get("next_cursor")
        has = resp.get("has_next_page", data.get("has_next_page", bool(cursor)))
        # 新規ゼロのページに到達＝以降は全部既読/枠外 → 打ち切り(深いページを叩かない)
        if not page_had_new or oldest < floor_iso or not has or not cursor:
            break
        time.sleep(0.6)
    return out, ok


def claude_extract(account, tweets, stores, anthropic_key, today):
    store_hint = "\n".join(f"- {s['name']}" + (f"（別名: {', '.join(s['aliases'])}）" if s["aliases"] else "")
                           for s in stores)
    posts = "\n\n".join(f"[投稿{i}] 投稿日={t.get('_posted','?')}\n{t.get('text','')}" for i, t in enumerate(tweets))
    prompt = f"""あなたはパチスロ「晒し屋」のXポストを構造化するアシスタントです。今日は {today}。

# 登録店舗（この店に該当する投稿だけ抽出。表記ゆれOK。下記以外の店は完全に無視）
{store_hint}

# 発信者
{account}

# 投稿群
{posts}

# 指示
各投稿から、登録店舗に該当する「予想」をJSON配列で出力。
- お品書き/狙い目/推奨店リスト等、その日強いと示唆している店を予想とみなす
- 1投稿に複数店あれば店ごと1レコード
- target_date: 投稿が対象にする日(YYYY-MM-DD)。「明日X日」等は投稿日から推定。無ければ投稿日
- machines: 機種名(無ければ空配列) / matsubi: 末尾数字(無ければ空配列)
- note: 要点25字程度 / post_index: 投稿番号(整数) / store: 登録店舗の正規名に正規化
登録店舗が無ければ []。**JSON配列のみ**出力。"""
    body = json.dumps({"model": CLAUDE_MODEL, "max_tokens": 3000,
                       "messages": [{"role": "user", "content": prompt}]}).encode("utf-8")
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=body, headers={
        "x-api-key": anthropic_key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=90) as r:
        resp = json.loads(r.read().decode("utf-8", "replace"))
    text = "".join(c.get("text", "") for c in resp.get("content", []) if c.get("type") == "text").strip()
    text = re.sub(r"^```(json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    m = re.search(r"\[.*\]", text, re.DOTALL)
    try:
        return json.loads(m.group(0)) if m else []
    except Exception:
        return []


def main():
    keys = load_keys()
    if not keys.get("TWITTERAPI_KEY") or not keys.get("ANTHROPIC_API_KEY"):
        print("⚠ 鍵が無い。中断。")
        # CI上で鍵が空＝Secrets未設定/失効。サイレントに緑で終わらせず落とす。
        if os.environ.get("GITHUB_ACTIONS") == "true":
            print("::error::TWITTERAPI_KEY / ANTHROPIC_API_KEY が空。GitHub Secrets 未設定/失効の疑い。")
            sys.exit(1)
        return
    sources = json.loads((COLL / "sources.json").read_text(encoding="utf-8"))["sources"]
    active_sources = [s for s in sources if s.get("active", True)]
    stores = tracked_stores()
    smap = {s["name"]: s.get("area") for s in json.loads((DATA / "stores.json").read_text(encoding="utf-8"))}
    today = date.today().isoformat()
    floor = (date.today() - timedelta(days=DAYS)).isoformat()
    print(f"収集範囲: {floor} 〜 {today}（{DAYS}日）")

    seen = load_seen()
    new = []
    fetch_failed = []
    claude_calls = claude_errs = 0
    for src in active_sources:
        name, handle = src["name"], src["handle"]
        # fetch_tweets が seen を見て既読まで来たらページ送りを打ち切る＝
        # 戻り値 tw は「未処理の新規」だけ。処理したIDを seen に積む。
        tw, ok = fetch_tweets(handle, keys["TWITTERAPI_KEY"], floor, seen)
        if not ok:
            fetch_failed.append(name)
        for t in tw:
            seen.add(str(t.get("id")))
        if not tw:
            print(f"▼ {name}: 新規なし(既読まで取得して打ち切り)")
            continue
        recs_all = []
        for i in range(0, len(tw), CHUNK):
            chunk = tw[i:i + CHUNK]
            claude_calls += 1
            try:
                recs = claude_extract(name, chunk, stores, keys["ANTHROPIC_API_KEY"], today)
            except Exception as e:
                claude_errs += 1
                print(f"    (claude err {name}: {e})")
                recs = []
            for r in recs:
                pi = r.get("post_index")
                t = chunk[pi] if isinstance(pi, int) and 0 <= pi < len(chunk) else None
                store = (r.get("store") or "").strip()
                if not store or not t:
                    continue
                recs_all.append({
                    "id": f"{handle}-{t.get('id','?')}-{store}",
                    "source": name, "handle": handle,
                    "posted_at": t.get("_posted", today),
                    "target_date": r.get("target_date") or t.get("_posted", today),
                    "store": store, "area": smap.get(store),
                    "machines": r.get("machines") or [], "matsubi": r.get("matsubi") or [],
                    "note": r.get("note") or "", "url": t.get("url", f"https://x.com/{handle}"),
                })
            time.sleep(0.8)
        print(f"▼ {name}: 新規{len(tw)}件 → 予想{len(recs_all)}件")
        new.extend(recs_all)
    save_seen(seen)

    # X取得の健全性: 稼働アカが全滅＝twitterapi.io のキー切れ/レート制限/障害。
    # 「取得成功で新規0件(=ネタ無し)」とは別物なので、ここで CI を赤くして気づけるようにする。
    if active_sources and len(fetch_failed) == len(active_sources):
        print(f"::error::X取得が全{len(active_sources)}アカで失敗（twitterapi.io キー切れ/レート制限/障害の疑い）。予想は更新されません。")
        sys.exit(1)
    if fetch_failed:
        print(f"::warning::X取得に一部失敗: {', '.join(fetch_failed)}（残アカは継続）")

    # Claude抽出の健全性: 投げた全コールが例外＝モデルID不正/キー失効/障害。
    # 握り潰すと「予想が静かにゼロ」になるので、全滅なら CI を赤くする。
    if claude_calls and claude_errs == claude_calls:
        print(f"::error::Claude抽出が全{claude_calls}コールで失敗（モデルID不正/キー失効/障害の疑い）。予想は更新されません。")
        sys.exit(1)
    if claude_errs:
        print(f"::warning::Claude抽出に一部失敗: {claude_errs}/{claude_calls}コール")

    # 既存とマージ（id単位・過去ぶんは消えない）
    out_path = DATA / "predictions.json"
    existing = []
    if out_path.exists():
        try:
            existing = json.loads(out_path.read_text(encoding="utf-8")).get("predictions", [])
        except Exception:
            existing = []
    merged = {p.get("id"): p for p in existing}
    for p in new:
        merged[p["id"]] = p
    final = sorted(merged.values(), key=lambda p: p.get("target_date") or "", reverse=True)
    if not new and existing:
        print(f"\n⚠ 今回0件。既存{len(existing)}件を保持。")
        return
    out_path.write_text(json.dumps({"generated_at": datetime.now().isoformat(timespec="minutes"),
                                    "count": len(final), "predictions": final},
                                   ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n保存: 今回{len(new)}件 + 既存マージ → 計{len(final)}予想")


if __name__ == "__main__":
    main()
