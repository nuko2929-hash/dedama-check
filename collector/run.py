"""収集ループ: 全稼働店舗の直近レポートを集めて data/reports.json に保存。

GitHub Actions から定期実行する想定。手元でも `python3 collector/run.py` で動く。
"""
import json, os, time, pathlib, sys, re
from datetime import date, datetime, timedelta
import minrepo

ROOT = pathlib.Path(__file__).resolve().parents[1]
DATA = ROOT / "data"

# reports.json の保持期間(日)。これより古い出玉は書き出し時に捨ててファイル肥大を止める。
# 0 なら全保持。env REPORTS_RETAIN_DAYS で上書き可(軽くしたいなら 45 等)。
RETAIN_DAYS = int(os.environ.get("REPORTS_RETAIN_DAYS", "60"))

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

    # store+iso_date をキーに既存をマージ土台に。店ごとに逐次セーブ(チェックポイント)するので
    # 途中でブロック/中断されても進捗が残り、再実行で続きから埋まる。
    def key(r):
        return (r.get("store"), r.get("iso_date") or r.get("date"))
    merged = {key(r): r for r in existing}

    def checkpoint():
        # 保持期間より古いレポートは捨てる(ファイル肥大→GitHub Pages 503 対策)。
        # 古い出玉は「過去実績」表示の価値が薄く、予想の答え合わせ窓も十分カバーできる。
        kept = list(merged.values())
        if RETAIN_DAYS > 0:
            cutoff = (date.today() - timedelta(days=RETAIN_DAYS)).isoformat()
            kept = [r for r in kept if not r.get("iso_date") or r["iso_date"] >= cutoff]
        final = sorted(kept, key=lambda r: r.get("iso_date") or "", reverse=True)
        out = {"generated_at": datetime.now().isoformat(timespec="minutes"),
               "count": len(final), "reports": final}
        # アトミック書き込み: 一旦tmpに書いてから os.replace で差し替える。
        # こうしないと、収集中にアプリ(ローカル)が読みにいった瞬間に
        # 「書き途中=途中までしか無いJSON」を掴んで他店が一瞬消えて見える。
        # indent無し(コンパクト)で書く: 整形の空白だけで従来ファイルの約4割を食っていた。
        tmp = out_path.parent / (out_path.name + ".tmp")
        tmp.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        os.replace(tmp, out_path)   # 同一FS上ならアトミック(読み手は旧か新の完全版しか見ない)
        return len(final)

    all_reports = []
    attempted = 0   # フェッチを試みたレポート数(=新しい日付ぶん)
    failures = 0    # うち例外で取れなかった数
    for store in active:
        if SINCE:
            # 指定日まで遡る: タグページを深掘りし(検索フォールバック)、SINCE以降だけ残す
            reps = minrepo.list_any(store, max_pages=4, since_iso=SINCE)
            reps = [r for r in reps if (to_iso(r["date"]) or "") >= SINCE]
            # バックフィルでは既に取得済みの日はフェッチしない(再開を軽く・ブロック回避)
            before = len(reps)
            reps = [r for r in reps if (store["name"], to_iso(r["date"])) not in done]
            print(f"▼ {store['name']}: {SINCE}以降 {before}件 (新規{len(reps)}件/取得済{before-len(reps)})")
        else:
            reps = minrepo.list_any(store)[:RECENT_PER_STORE]
            # 既に持っている日はフェッチしない(毎日のリクエストを最小化＝ブロック回避)
            reps = [r for r in reps if (store["name"], to_iso(r["date"])) not in done]
        store_new = 0
        for r in reps:
            attempted += 1
            try:
                data = minrepo.collect_report(r["url"])
            except Exception as e:
                failures += 1
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
            merged[key(data)] = data          # 即マージ(チェックポイント対象に)
            all_reports.append(data)
            store_new += 1
            print(f"  ✓ {store['name']} {r['date']} 機種{len(data['kishu'])} 末尾{len(data['matsubi'])} 台{data['summary'].get('total_units')}")
            minrepo._sleep()
        # 店ごとにディスクへ保存(中断耐性)。新規が出た店だけ書く。
        if store_new:
            total = checkpoint()
            print(f"  💾 {store['name']} 完了: +{store_new}件 → 計{total}レポート保存")
        if SINCE and reps:
            time.sleep(18)  # 店ごとに小休止(バースト検知を避ける)

    # 取得を試みた全件が例外で落ちた＝min-repo にブロック/障害された疑い。
    # 「新しい日付が無くて0件(=正常)」とは別物なので、ここで CI を赤くして気づけるようにする。
    if attempted and failures == attempted:
        print(f"::error::min-repo 取得が全{attempted}件で失敗（ブロック/障害の疑い）。出玉は更新されません。")
        sys.exit(1)
    if failures:
        print(f"::warning::min-repo 取得に一部失敗: {failures}/{attempted}件")

    # 今回0件(=全部取得済み or ブロック)なら既存を絶対に消さない(checkpointも走っていない)
    if not all_reports:
        print(f"\n⚠ 今回の新規収集は0件。既存{len(existing)}件を保持し、上書きしません。")
        return

    print(f"\n保存完了: 今回{len(all_reports)}件 + 既存マージ → 計{len(merged)}レポート → data/reports.json")


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT / "collector"))
    main()
