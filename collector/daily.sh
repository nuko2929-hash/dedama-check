#!/bin/bash
# のりレポ 日次収集（ローカル・ゆっくり）。launchd から毎日呼ばれる。
# - run.py は SLEEP 3.5秒＋ジッター、取得済みスキップ、0件なら上書きしない設計
# - reports.json が変わったときだけ commit & push（本番に反映）
set -u
REPO="$HOME/dedama-check"
LOG="/tmp/dedama_daily.log"
cd "$REPO" || { echo "$(date '+%F %T') repo not found" >> "$LOG"; exit 1; }

echo "===== $(date '+%F %T') 日次収集 開始 =====" >> "$LOG"
/usr/bin/python3 -u collector/run.py 3 >> "$LOG" 2>&1

# reports.json に差分があるときだけ反映
if ! git diff --quiet -- data/reports.json 2>/dev/null; then
  git add data/reports.json
  git -c commit.gpgsign=false commit -q -m "auto: 日次収集で reports.json 更新 ($(date '+%F'))" >> "$LOG" 2>&1
  git pull --rebase origin main -q  >> "$LOG" 2>&1
  git push  -q origin main          >> "$LOG" 2>&1 && echo "$(date '+%F %T') push 完了" >> "$LOG"
else
  echo "$(date '+%F %T') 新規データなし（差分なし）" >> "$LOG"
fi
echo "----- $(date '+%F %T') 日次収集 終了 -----" >> "$LOG"
