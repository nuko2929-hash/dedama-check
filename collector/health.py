"""データ健全性チェック: 出玉(reports.json)が店ごとにちゃんと取れているか点検する。

  python3 collector/health.py            … 一覧を表示(情報のみ・原則exit 0)
  python3 collector/health.py --strict   … 問題があれば非ゼロ終了(CI向け)

見るもの:
  - 各アクティブ店の レポート件数 / 最古〜最新 / 最終更新からの日数 / 直近30日の歯抜け
  - フラグ: MISSING(0件) / THIN(履歴が薄い) / STALE(最新が古い=日次が止まってる疑い)
  - 未解決タグ(tag_url=None)も注意表示

stdlib のみ。bs4 不要なのでどこでも動く。
"""
import json, sys, pathlib
from datetime import date

ROOT = pathlib.Path(__file__).resolve().parents[1]
DATA = ROOT / "data"

THIN = 7        # この件数未満なら「履歴が薄い」
STALE_DAYS = 4  # 最新がこれより古ければ「日次が止まってる疑い」(日次3回/日なので通常≤3日)
WINDOW = 30     # 歯抜けを数える直近日数


def load(name):
    p = DATA / name
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def main():
    strict = "--strict" in sys.argv[1:]
    today = date.today()

    stores = load("stores.json")
    rep = load("reports.json")
    if not stores or not rep or not rep.get("reports"):
        print("✗ reports.json または stores.json が読めない/空。収集が一度も成功していない可能性。")
        return 1 if strict else 0

    reports = rep["reports"]
    active = [s for s in stores if not s.get("skip")]

    # 店ごとに iso_date を集約
    by_store = {}
    for r in reports:
        by_store.setdefault(r.get("store"), []).append(r.get("iso_date"))

    print(f"のりレポ データ健全性  (今日 {today.isoformat()} / 全{len(reports)}レポート / "
          f"生成 {rep.get('generated_at')})")
    print(f"{'件数':>4} {'最古':>10} {'最新':>10} {'経過':>4} {'歯抜':>4}  店舗")
    print("-" * 72)

    problems, warnings = [], []
    overall_newest = None

    for s in active:
        name = s["name"]
        ds = sorted(d for d in by_store.get(name, []) if d)
        flags = []
        if not s.get("tag_url"):
            flags.append("NO-TAG")  # 検索頼み(取りこぼしやすい)

        if not ds:
            print(f"{0:>4} {'-':>10} {'-':>10} {'-':>4} {'-':>4}  {name}  ⛔MISSING {' '.join(flags)}")
            problems.append(f"{name}: 0件(未収集)")
            continue

        oldest, newest = ds[0], ds[-1]
        nd = date.fromisoformat(newest)
        age = (today - nd).days
        overall_newest = max(overall_newest or newest, newest)

        # 直近WINDOW日の歯抜け(最新日を基準に過去WINDOW日のうち欠けている日数)
        have = set(ds)
        from datetime import timedelta
        miss = sum(1 for i in range(WINDOW)
                   if (nd - timedelta(days=i)).isoformat() not in have)

        if len(ds) < THIN:
            flags.append("THIN")
            warnings.append(f"{name}: 履歴{len(ds)}件(薄い)")
        if age > STALE_DAYS:
            flags.append("STALE")
            warnings.append(f"{name}: 最新{newest}({age}日前・日次停止疑い)")

        mark = "  " + " ".join(flags) if flags else ""
        print(f"{len(ds):>4} {oldest:>10} {newest:>10} {age:>3}d {miss:>3}/{WINDOW}  {name}{mark}")

    print("-" * 72)
    # 全体の鮮度(=日次収集が生きているか)
    global_age = (today - date.fromisoformat(overall_newest)).days if overall_newest else 999
    print(f"最も新しいレポート: {overall_newest} ({global_age}日前)")

    if warnings:
        print("\n⚠ 注意(" + str(len(warnings)) + "):")
        for w in warnings:
            print("   - " + w)
    if problems:
        print("\n⛔ 問題(" + str(len(problems)) + "):")
        for p in problems:
            print("   - " + p)
    if not warnings and not problems:
        print("\n✅ 全アクティブ店が新鮮＆十分な履歴あり。")

    # 非ゼロ終了は「収集が壊れている」級だけ: 全体が古い or 全店MISSING。
    # 個別のTHIN/STALEは警告どまり(デプロイ直後やバックフィル前で正常に起こりうるため)。
    broken = global_age > STALE_DAYS or len(problems) == len(active)
    if strict and broken:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
