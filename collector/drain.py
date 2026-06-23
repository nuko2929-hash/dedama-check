"""バックフィル・ドレイン: min-repoのIPブロックをかわしながら、
指定日(SINCE)以降の全店レポートを少しずつ確実に集める。

- 店ごとに「ブロック解除を待つ → 収集 → 即 reports.json に保存」
- 既に取得済みの日はスキップ(再開が軽い)
- 1レポートごとに保存するので、途中で止まっても進捗は失われない

使い方: python3 -u collector/drain.py [SINCE]
"""
import json, time, pathlib, sys
from datetime import datetime
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import minrepo, run

ROOT = pathlib.Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
OUT = DATA / "reports.json"
SINCE = sys.argv[1] if len(sys.argv) > 1 else "2026-05-21"


def load():
    try:
        return json.loads(OUT.read_text(encoding="utf-8")).get("reports", [])
    except Exception:
        return []


def save(reports):
    reports = sorted(reports, key=lambda r: r.get("iso_date") or "", reverse=True)
    OUT.write_text(json.dumps(
        {"generated_at": datetime.now().isoformat(timespec="minutes"),
         "count": len(reports), "reports": reports},
        ensure_ascii=False, indent=2), encoding="utf-8")


def alive():
    try:
        return len(minrepo.fetch("https://min-repo.com/?s=%E7%AB%8B%E5%B7%9D", _retries=1)) > 1000
    except Exception:
        return False


def wait_alive(max_wait=1800):
    waited = 0
    while waited < max_wait:
        if alive():
            return True
        print(f"    …ブロック中、60秒待機(累計{waited}s)", flush=True)
        time.sleep(60)
        waited += 60
    return False


def main():
    stores = json.loads((DATA / "stores.json").read_text(encoding="utf-8"))
    active = [s for s in stores if not s.get("skip")]
    for store in active:
        reports = load()
        done = {(r.get("store"), r.get("iso_date")) for r in reports}
        if not wait_alive():
            print("⚠ ブロックが30分続いたため中断。後でまた drain を流せば続きから。", flush=True)
            return
        try:
            reps = minrepo.list_reports(store, max_pages=3)
        except Exception as e:
            print(f"  ! {store['name']} 一覧失敗: {e}", flush=True)
            continue
        reps = [r for r in reps
                if (run.to_iso(r["date"]) or "") >= SINCE
                and (store["name"], run.to_iso(r["date"])) not in done]
        if not reps:
            print(f"▼ {store['name']}: 新規なし(取得済 or データ無し)", flush=True)
            continue
        print(f"▼ {store['name']}: 新規{len(reps)}件 収集開始", flush=True)
        got = 0
        for r in reps:
            try:
                data = minrepo.collect_report(r["url"])
            except Exception as e:
                print(f"  ! {store['name']} {r['date']} 失敗: {e} → この店は次回再開", flush=True)
                break
            if not data["kishu"] and not data["summary"].get("total_units"):
                print(f"  ! {store['name']} {r['date']} 空応答=再ブロック → 中断して待機", flush=True)
                break  # 再ブロック: この店の残りは次パスで
            from datetime import date as _date
            iso = run.to_iso(r["date"])
            data.update({
                "store": store["name"], "area": store["area"], "date": r["date"],
                "iso_date": iso,
                "stale": (iso and (_date.today() - _date.fromisoformat(iso)).days > 30) or False,
                "title": r["title"],
                "consider": run.consider(data),
                "kishu_class": run.classify_kishu(data["kishu"]),
            })
            reports.append(data)
            save(reports)  # 1件ごとに保存=進捗が消えない
            got += 1
            print(f"  ✓ {store['name']} {r['date']} (計{len(reports)})", flush=True)
            minrepo._sleep()
        print(f"  → {store['name']} 完了 {got}件", flush=True)
        time.sleep(18)  # 店間の小休止
    print(f"\n🎉 ドレイン完了。reports.json = {len(load())}件", flush=True)


if __name__ == "__main__":
    main()
