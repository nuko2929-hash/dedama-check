# 出玉ダイジェスト(晒し屋答え合わせツール)

X(晒し屋)の予想と min-repo.com の出玉結果を突き合わせる答え合わせツール。

## 構成
- `collector/` — min-repo 収集器(Python)
  - `stores.py` … 対象店舗マスタ(`data/stores.json` を生成)
  - `minrepo.py` … 店舗ページ→総合集計＋機種別＋末尾別 のパーサ
  - `run.py` … 全店舗を回して `data/reports.json` を生成＋考察文を付与
- `data/` — 収集結果(stores.json / reports.json)
- `index.html` — 画面(ダイジェスト一覧＋詳細レポート)。`data/reports.json` を読む
- `.github/workflows/collect.yml` — 毎日自動収集(GitHub Actions)

## 手元で動かす
```bash
pip install beautifulsoup4
python collector/run.py 3      # 各店の直近3レポートを収集
python3 -m http.server 4174    # http://localhost:4174 で画面確認
```

## 公開(GitHub Pages)
1. このフォルダを GitHub リポジトリに push
2. Settings → Pages → Source を main / root に
3. Actions が毎日 07:00 JST に収集して reports.json を更新 → 画面に自動反映

## Phase 状況
- ✅ Phase 1: min-repo 収集＋機種別/末尾別/総合集計＋考察＋画面
- ⬜ Phase 2: X 予想(サードパーティAPI)収集＋ダイジェスト統合
- ⬜ Phase 3: 発信者精度スコア／明日の店舗推薦／型(LINE手動入力)

## メモ
- min-repo は台番の生データ不要(機種別・末尾別とも集計済みを公開)
- 総合集計が取れない店は機種別合計から推定(画面に「※推定」表示)
- マルハン綾瀬=「マルハン綾瀬上土棚店」名。スキップ店: エランドール泉/ブラジャン戸塚/ガーデン戸塚(min-repo未掲載)
