"""収集ループ: 全稼働店舗の直近レポートを集めて data/reports.json に保存。

GitHub Actions から定期実行する想定。手元でも `python3 collector/run.py` で動く。
"""
import json, time, pathlib, sys, re
from datetime import date, datetime
import minrepo

ROOT = pathlib.Path(__file__).resolve().parents[1]
DATA = ROOT / "data"

# 引数解釈:
#   python3 collector/run.py            … 各店 直近3レポート(日次運用)
#   python3 collector/run.py 5          … 各店 直近5レポート
#   python3 collector/run.py --since 2026-05-21  … その日まで遡って全部(バックフィル)
SINCE = None
RECENT_PER_STORE = 3
_args = sys.argv[1:]
if _args and _args[0] == "--since" and len(_args) > 1:
    SINCE = _args[1]
elif _args and _args[0].isdigit():
    RECENT_PER_STORE = int(_args[0])


def to_iso(md):
    """'6/22' を、今日以前で最も近い実日付(年つきISO)に変換。
    9/29 や 11/16 のような未来日付は前年扱いになり、新旧の並びが正しくなる。"""
    m = re.match(r"(\d{1,2})/(\d{1,2})", md or "")
    if not m:
        return None
    mo, da = int(m.group(1)), int(m.group(2))
    today = date.today()
    y = today.year
    try:
        d = date(y, mo, da)
    except ValueError:
        return None
    if d > today:
        d = date(y - 1, mo, da)
    return d.isoformat()


def consider(report):
    """機種別・末尾別の数字から、人が読める考察(箇条書き)を機械生成。

    複雑なAIは使わず、極端値・全勝・好調末尾を拾う程度のシンプルロジック。
    """
    s = report["summary"]
    kishu = report["kishu"]
    matsubi = report["matsubi"]
    units = s.get("total_units")
    notes = []

    if not units or units < 10:
        notes.append("対象台数が少なめ。データ不足ぎみなので参考程度に。")

    wp = s.get("win_pct")
    avg = s.get("avg_diff")
    if avg is not None and wp is not None:
        if avg > 0 and wp >= 50:
            notes.append(f"全体的に高設定が多めで、平均+{avg:.0f}枚・勝率{wp:.0f}%と良い傾向。")
        elif avg <= 0 and wp < 40:
            notes.append(f"全体は重め(平均{avg:+.0f}枚・勝率{wp:.0f}%)。狙いを絞った方が良さそう。")

    # 全勝機種(2台以上で全台プラス)
    zensho = [k for k in kishu if k["win_total"] and k["win_total"] >= 2
              and k["win_plus"] == k["win_total"]]
    zensho.sort(key=lambda k: (k["win_total"], k["avg_diff"] or 0), reverse=True)
    for k in zensho[:2]:
        notes.append(f"{k['name']}は{k['win_total']}台全勝(平均+{k['avg_diff']:.0f}枚)で鉄板感。")

    # 平均差枚トップ機種
    top = max((k for k in kishu if k["avg_diff"] is not None), key=lambda k: k["avg_diff"], default=None)
    if top and top["avg_diff"] > 1500 and top not in zensho:
        notes.append(f"{top['name']}が平均+{top['avg_diff']:.0f}枚と特に好調。")

    # 好調な末尾(出率トップ、ゾロ目除く・データありのみ)
    md = [m for m in matsubi if m["rate"] is not None and "ゾロ" not in m["matsubi"]]
    if md:
        best = max(md, key=lambda m: m["rate"])
        if best["rate"] >= 103:
            notes.append(f"末尾{best['matsubi']}が出率{best['rate']}%/勝率{best['win_plus']}/{best['win_total']}と動いてる。")
    zoro = next((m for m in matsubi if "ゾロ" in m["matsubi"] and m["rate"]), None)
    if zoro and zoro["rate"] >= 105:
        notes.append(f"ゾロ目(下二桁)が出率{zoro['rate']}%と強い。")

    if not notes:
        notes.append("目立った偏りは無し。フラットな結果。")
    return notes


def classify_kishu(kishu):
    """機種を 優秀機種候補 / 勝率高機種 / 気になる台 に仕分け(画面用)。"""
    excellent, highwin, others = [], [], []
    for k in kishu:
        wt, wp, ad = k["win_total"], k["win_plus"], k["avg_diff"]
        if ad is not None and ad >= 2000:
            excellent.append(k)
        elif wt and wt >= 3 and wp is not None and wp / wt >= 0.6:
            highwin.append(k)
        elif ad is not None and ad > 0:
            others.append(k)
    return {"excellent": excellent, "highwin": highwin, "others": others}


def main():
    stores = json.loads((DATA / "stores.json").read_text(encoding="utf-8"))
    active = [s for s in stores if not s.get("skip")]

    # 既存データ(再開時のスキップ判定＆マージ用)
    out_path = DATA / "reports.json"
    existing = []
    if out_path.exists():
        try:
            existing = json.loads(out_path.read_text(encoding="utf-8")).get("reports", [])
        except Exception:
            existing = []
    done = {(r.get("store"), r.get("iso_date")) for r in existing}

    all_reports = []
    for store in active:
        if SINCE:
            # 指定日まで遡る: ページ送りで深掘りし、SINCE以降だけ残す
            reps = minrepo.list_reports(store, max_pages=3)
            reps = [r for r in reps if (to_iso(r["date"]) or "") >= SINCE]
            # バックフィルでは既に取得済みの日はフェッチしない(再開を軽く・ブロック回避)
            before = len(reps)
            reps = [r for r in reps if (store["name"], to_iso(r["date"])) not in done]
            print(f"▼ {store['name']}: {SINCE}以降 {before}件 (新規{len(reps)}件/取得済{before-len(reps)})")
        else:
            reps = minrepo.list_reports(store)[:RECENT_PER_STORE]
        for r in reps:
            try:
                data = minrepo.collect_report(r["url"])
            except Exception as e:
                print(f"  ! {store['name']} {r['date']} 失敗: {e}")
                continue
            iso = to_iso(r["date"])
            stale = False
            if iso:
                stale = (date.today() - date.fromisoformat(iso)).days > 30
            data.update({
                "store": store["name"],
                "area": store["area"],
                "date": r["date"],
                "iso_date": iso,
                "stale": stale,
                "title": r["title"],
                "consider": consider(data),
                "kishu_class": classify_kishu(data["kishu"]),
            })
            all_reports.append(data)
            print(f"  ✓ {store['name']} {r['date']} 機種{len(data['kishu'])} 末尾{len(data['matsubi'])} 台{data['summary'].get('total_units')}")
            minrepo._sleep()
        if SINCE and reps:
            time.sleep(18)  # 店ごとに小休止(バースト検知を避ける)

    # 今回0件(=全部取得済み or ブロック)なら既存を絶対に消さない
    if not all_reports:
        print(f"\n⚠ 今回の新規収集は0件。既存{len(existing)}件を保持し、上書きしません。")
        return

    # store+iso_date をキーにマージ（新しい収集で既存を更新、既存のみの履歴は残す）
    def key(r):
        return (r.get("store"), r.get("iso_date") or r.get("date"))
    merged = {key(r): r for r in existing}
    for r in all_reports:
        merged[key(r)] = r
    final = sorted(merged.values(), key=lambda r: r.get("iso_date") or "", reverse=True)

    out = {"generated_at": datetime.now().isoformat(timespec="minutes"),
           "count": len(final), "reports": final}
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n保存: 今回{len(all_reports)}件 + 既存マージ → 計{len(final)}レポート → data/reports.json")


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT / "collector"))
    main()
