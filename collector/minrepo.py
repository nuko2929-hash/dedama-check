"""min-repo 収集器。

店舗マスタ(data/stores.json)の各店について:
  1. サイト内検索で直近の日別レポート(日付つき)を一覧取得
  2. 各レポートページを 総合集計＋機種別 に構造化
  3. 末尾別は ?kishu=0..9 のフィルタURLの総合集計を読んで構成

礼儀正しく: User-Agent明示・リクエスト間に待機。
"""
import json, re, time, html as htmllib, urllib.parse, urllib.request, pathlib
from bs4 import BeautifulSoup

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/148 Safari/537.36"
ROOT = pathlib.Path(__file__).resolve().parents[1]
SLEEP = 1.2


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=25) as r:
        return r.read().decode("utf-8", "replace")


def num(s):
    s = (s or "").replace(",", "").replace("枚", "").replace("%", "").strip()
    m = re.search(r"-?\d+\.?\d*", s)
    return float(m.group()) if m else None


# --- レポート一覧(検索ページ) -------------------------------------------------

def list_reports(store):
    """店舗の直近レポート [{date, title, url}] を新しい順で返す。"""
    names = [store["name"]] + store.get("aliases", [])
    seen, out = set(), []
    for q in names:
        try:
            raw = fetch("https://min-repo.com/?s=" + urllib.parse.quote(q))
        except Exception:
            continue
        for url, title in re.findall(
            r'<div class="ichiran_title"><a href="(https://min-repo\.com/\d+/)">([^<]+)</a>', raw
        ):
            title = htmllib.unescape(title)
            dm = re.search(r"(\d{1,2}/\d{1,2})\([月火水木金土日]\)", title)
            # 店名の語幹一致(エイリアス含む・大小無視)で絞る
            tl = title.lower()
            if not any(a.replace("店", "").replace(" ", "")[:4].lower() in tl.replace(" ", "")
                       for a in names):
                continue
            if url in seen:
                continue
            seen.add(url)
            out.append({"date": dm.group(1) if dm else None, "title": title, "url": url})
        time.sleep(SLEEP)
    return out


# --- レポート本体パース -------------------------------------------------------

def _rows(table):
    out = []
    for tr in table.find_all("tr"):
        out.append([td.get_text(" ", strip=True) for td in tr.find_all(["td", "th"])])
    return out


def _winratio(s):
    """'259/600' → (259, 600)。"""
    m = re.match(r"(\d+)\s*/\s*(\d+)", s or "")
    return (int(m.group(1)), int(m.group(2))) if m else (None, None)


def _find_tables(raw):
    """ページ内テーブルを役割ごとに分類して返す。"""
    soup = BeautifulSoup(raw, "html.parser")
    summary = kishu = matsubi = None
    for t in soup.find_all("table"):
        rows = _rows(t)
        if not rows:
            continue
        head = rows[0]
        flat = [c for r in rows for c in r]
        if "総差枚" in flat and summary is None:
            summary = rows
        elif head[:1] == ["機種"] and "平均差枚" in head and kishu is None:
            kishu = t
        elif head[:1] == ["末尾"] and matsubi is None:
            matsubi = rows
    return summary, kishu, matsubi


def parse_summary(rows):
    """総合集計テーブル(縦持ち)を辞書に。対象台数は勝率の分母から。"""
    d = {}
    for r in rows or []:
        if len(r) < 2:
            continue
        key, val = r[0], r[1]
        if key == "総差枚":
            d["total_diff"] = num(val)
        elif key == "平均差枚":
            d["avg_diff"] = num(val)
        elif key == "平均G数":
            d["avg_games"] = num(val)
        elif key == "勝率":
            plus, total = _winratio(val)
            d["win_plus"], d["total_units"] = plus, total
            d["win_pct"] = round(plus / total * 100, 1) if plus is not None and total else None
    return d


def parse_kishu(table):
    rows = []
    if table is None:
        return rows
    for cells in _rows(table):
        if len(cells) < 5 or cells[0] in ("機種", "") or "平均差枚" in cells[0]:
            continue
        plus, total = _winratio(cells[3])
        rows.append({
            "name": cells[0],
            "avg_diff": num(cells[1]),
            "avg_games": num(cells[2]),
            "win_plus": plus,
            "win_total": total,
            "rate": num(cells[4]),
        })
    return rows


def parse_matsubi(rows):
    out = []
    for cells in rows or []:
        if len(cells) < 5 or cells[0] == "末尾":
            continue
        label = cells[0]
        plus, total = _winratio(cells[3])
        out.append({
            "matsubi": label,                 # "0".."9" または "ゾロ目 (下二桁)"
            "avg_diff": num(cells[1]),         # データ無しは None("-")
            "avg_games": num(cells[2]),
            "win_plus": plus,
            "win_total": total,
            "rate": num(cells[4]),
        })
    return out


def _summary_from_kishu(kishu):
    """総合集計テーブルが取れない店向けに、機種別データの合計から算出。"""
    units = sum(k["win_total"] for k in kishu if k["win_total"])
    plus = sum(k["win_plus"] for k in kishu if k["win_plus"] is not None)
    total = sum((k["avg_diff"] or 0) * (k["win_total"] or 0)
                for k in kishu if k["avg_diff"] is not None)
    return {
        "total_units": units or None,
        "win_plus": plus if units else None,
        "win_pct": round(plus / units * 100, 1) if units else None,
        "total_diff": round(total) if units else None,
        "avg_diff": round(total / units) if units else None,
        "estimated": True,  # 機種別からの推定値である印
    }


def collect_report(report_url):
    """1フェッチで 総合集計＋機種別＋末尾別 を取得。"""
    raw = fetch(report_url)
    summary_rows, kishu_table, matsubi_rows = _find_tables(raw)
    kishu = parse_kishu(kishu_table)
    summary = parse_summary(summary_rows)
    # 総合集計が欠けてたら機種別合計で補完
    if not summary.get("total_units") and kishu:
        summary = _summary_from_kishu(kishu)
    return {
        "url": report_url,
        "summary": summary,
        "kishu": kishu,
        "matsubi": parse_matsubi(matsubi_rows),
    }


if __name__ == "__main__":
    import sys
    stores = json.loads((ROOT / "data" / "stores.json").read_text(encoding="utf-8"))
    target = [s for s in stores if not s.get("skip")]
    if len(sys.argv) > 1:  # 単体テスト: 店名指定
        target = [s for s in target if sys.argv[1] in s["name"]]
    for store in target:
        reps = list_reports(store)
        print(f"[{store['name']}] 直近レポート {len(reps)}件", reps[0] if reps else "")
