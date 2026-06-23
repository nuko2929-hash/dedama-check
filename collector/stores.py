"""対象店舗マスタ。min-repo のタグページを解決して保存する。

店舗は追加・削除しやすいよう、ここの STORES リストを編集するだけでよい。
別名(エイリアス)は晒し屋投稿の表記ゆれ吸収にも使う(Phase 2)。
"""
import json, re, time, urllib.parse, urllib.request, pathlib

# エリア, 正式名, 別名リスト(表記ゆれ用)
STORES = [
    ("立川", "楽園立川店", ["楽園立川", "楽園 立川"]),
    ("立川", "ハイパージアス立川", ["ハイパージアス立川", "ジアス立川"]),
    ("立川", "プレゴ立川店", ["プレゴ立川", "プレゴ 立川"]),
    ("立川", "スーパーDステーション立川", ["Dステ立川", "スーパーDステーション立川", "Dステーション立川"]),
    ("相模大野", "ピーアーク相模大野", ["ピーアーク相模大野", "Pアーク相模大野"]),
    ("相模大野", "ザシティ相模大野", ["ザシティ相模大野", "ベルシティ相模大野", "THE CITY相模大野"]),
    ("相模大野", "相模大野UNO", ["相模大野UNO", "UNO相模大野"]),
    ("戸塚", "キコーナ戸塚", ["キコーナ戸塚", "キコーナ 戸塚"]),
    ("戸塚", "エランドール泉", ["エランドール泉", "エランドール 泉"]),
    ("戸塚", "ブラジャン戸塚", ["ブラジャン戸塚", "ブラジャン 戸塚"]),
    ("戸塚", "スクランブル田谷", ["スクランブル田谷", "スクランブル 田谷"]),
    ("戸塚", "ガーデン戸塚", ["ガーデン戸塚", "新ガーデン戸塚", "新！ガーデン戸塚"]),
    ("湘南台", "アビバ湘南台", ["アビバ湘南台", "アビバ 湘南台"]),
    ("湘南台", "クリエ湘南台", ["クリエ湘南台", "クリエ 湘南台"]),
    ("湘南台", "パラッツォ湘南台", ["パラッツォ湘南台", "パラッツォ 湘南台"]),
    ("湘南台", "マルハン綾瀬", ["マルハン綾瀬", "マルハン 綾瀬", "綾瀬マルハン"]),
    ("湘南台", "グランドホール長後", ["グランドホール長後", "グランドホール 長後"]),
]

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/148 Safari/537.36"


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read().decode("utf-8", "replace")


def resolve_tag(query):
    """min-repo サイト内検索で、その店のタグページURLと最新レポートIDを探す。"""
    url = "https://min-repo.com/?s=" + urllib.parse.quote(query)
    try:
        html_ = fetch(url)
    except Exception as e:
        return {"tag": None, "sample_report": None, "error": str(e)}
    tags = re.findall(r'https://min-repo\.com/tag/[^"\']+/', html_)
    reports = re.findall(r'https://min-repo\.com/(\d+)/', html_)
    return {
        "tag": tags[0] if tags else None,
        "sample_report": reports[0] if reports else None,
    }


def build_master():
    out = []
    for area, name, aliases in STORES:
        info = resolve_tag(name)
        out.append({
            "area": area,
            "name": name,
            "aliases": aliases,
            "tag_url": info.get("tag"),
            "sample_report": info.get("sample_report"),
            "resolved": bool(info.get("tag")),
        })
        time.sleep(1.2)  # 礼儀正しいアクセス間隔
    return out


if __name__ == "__main__":
    master = build_master()
    path = pathlib.Path(__file__).resolve().parents[1] / "data" / "stores.json"
    path.write_text(json.dumps(master, ensure_ascii=False, indent=2), encoding="utf-8")
    ok = sum(1 for s in master if s["resolved"])
    print(f"解決 {ok}/{len(master)} 店舗 → {path}")
    for s in master:
        mark = "✓" if s["resolved"] else "✗"
        print(f"  {mark} [{s['area']}] {s['name']}  {s['tag_url'] or '(未解決)'}")
