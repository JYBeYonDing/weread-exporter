#!/usr/bin/env bash
# 顺序导出剩余书。严格一本一本，绝不并发：本脚本是唯一的下载入口，
# 且用锁文件 + 计划任务的 MultipleInstances=IgnoreNew 双层保证同一时刻只有一个实例。
#
# 每本书的完成判定不再只看「章节数齐 + 有 PDF」，而是调用 check_book.py 做完整性
# 门槛：每章文件必须存在、非占位符，且正文达到声明字数的门槛（真实缺章会被判失败，
# 微信读书声明字数偏高造成的 94~97% 只告警不判失败）。判定不通过就用同一命令续跑，
# 直到通过或次数用尽；用尽的书记入 logs/NEEDS_ATTENTION.txt。
set -u
REPO="/c/Users/zglzs/workplace/opensource/weread-exporter"
LOG_DIR="$REPO/logs"
STATUS="$LOG_DIR/STATUS.tsv"
LOCK="$LOG_DIR/export_remaining.lock"
ATTENTION="$LOG_DIR/NEEDS_ATTENTION.txt"
cd "$REPO" || exit 1
mkdir -p "$LOG_DIR"

# 单实例锁：已在跑就直接退出，绝不并发
if [ -e "$LOCK" ]; then
  pid=$(cat "$LOCK" 2>/dev/null)
  if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
    echo "another export run is active (pid $pid); exit"
    exit 0
  fi
fi
echo $$ > "$LOCK"
trap 'rm -f "$LOCK"' EXIT

# 全部完成标记：再次触发时直接退出，避免重复点火还要空开浏览器
if [ -f "$LOG_DIR/all_done.flag" ]; then
  echo "all books already exported; nothing to do"
  exit 0
fi

# 序号|encodeId|期望章节数|书名
BOOKS=(
"7|9bb321505b16bc9bbb94ee7|9|销售读心术"
"8|5fa32fb0813ab7604g010cde|15|赢单九问：策略销售实战秘籍"
"9|ff0320007159cb09ff012b4|58|销售就是提问题"
"10|7b932060813aba162g018f20|21|差异化销售"
"12|7d032810813ab7817g012000|45|大客户销售：谋攻之道（全新版）"
"13|9c0327605c85e39c067a0a0|22|当客户说“不”"
"14|c3d3244071e5d40ac3df945|60|销售圣经（套装共2册）"
"15|a4b32f20813abb0dbg012751|28|情绪价值型销售高手"
"16|9f532d50813abc119g017d94|59|销售洗脑（全能销售系列）"
"17|9c832d005c85699c860113f|10|失传的营销秘诀"
"18|6da323305d18b86da0a7795|11|一本书读懂客户心理"
"19|d1f321607210fc63d1f7277|59|销量公式"
"20|e8432e60813ab8b4cg013133|24|大客户销售心法"
"21|a0832ec0813abbe0dg019bd6|48|如何让用户成为你的死忠粉：雷军小米营销法"
"22|6ef32fb071c307306ef1a59|44|营销心理战：掌握36种购买心理"
"23|41b329805df7df41ba680f7|60|销售就是要玩转情商"
"24|3b632790813ab8f85g0145b7|38|华为营销法"
"25|bf032c7072103ce5bf0568a|109|哈佛经典谈判术：你一开口就赢麻了"
)

say() { echo "$@" | tee -a "$STATUS"; }
say "=== RUN START $(date '+%F %T') | ${#BOOKS[@]} books | pid $$ ==="

for entry in "${BOOKS[@]}"; do
  IFS='|' read -r idx bid want title <<< "$entry"
  log="$LOG_DIR/book${idx}.log"
  marker="$LOG_DIR/.marker_b${idx}"
  say "=== BOOK ${idx} START $(date '+%F %T') | ${title} | ${bid} | expect ${want} ch ==="

  attempt=0; ok=0; why=""
  while [ $attempt -lt 3 ]; do
    attempt=$((attempt+1))
    echo "--- attempt ${attempt} $(date '+%F %T') ---" >> "$log"
    touch "$marker"
    # A PDF built from a previous (possibly truncated) attempt would be kept and
    # the exporter skips an existing PDF — so drop it and let it be rebuilt from
    # the chapters we now have.
    find output -maxdepth 1 -name "${title}*.pdf" -delete 2>/dev/null
    PYTHONIOENCODING=utf-8 python -u -m weread_exporter -b "$bid" -o md -o pdf >> "$log" 2>&1
    rc=$?
    got=$(ls cache/"$bid"/chapters/*.md 2>/dev/null | wc -l)
    newpdf=$(find output -maxdepth 1 -name '*.pdf' -newer "$marker" 2>/dev/null | head -1)
    rm -f "$marker"

    if [ -n "$newpdf" ] && [ "$got" -ge "$want" ]; then
      # 完整性门槛：不只数文件，逐章核对正文是否到位
      if PYTHONIOENCODING=utf-8 python check_book.py "$bid" >> "$log" 2>&1; then
        ok=1; break
      fi
      why="completeness gate failed"
      say "!!! BOOK ${idx} attempt ${attempt}: 完整性门槛未通过（详见 $(basename "$log")）"
    else
      why="incomplete output (rc=$rc)"
      say "!!! BOOK ${idx} attempt ${attempt} (rc=$rc): ${got}/${want} ch, new pdf=${newpdf:-none}"
    fi
    sleep 10
  done

  if [ $ok -eq 1 ]; then
    say "=== BOOK ${idx} DONE $(date '+%F %T') | ${got}/${want} ch | $(basename "$newpdf") | attempt ${attempt} ==="
  else
    say "### BOOK ${idx} UNRESOLVED after ${attempt} attempts | ${why} | $(ls cache/"$bid"/chapters/*.md 2>/dev/null | wc -l)/${want} ch"
    {
      echo "=== BOOK ${idx} | ${title} | ${bid} | $(date '+%F %T')"
      echo "    原因: ${why}"
      PYTHONIOENCODING=utf-8 python check_book.py "$bid" 2>&1 | sed 's/^/    /'
      echo
    } >> "$ATTENTION"
  fi
done

say "=== RUN END $(date '+%F %T') ==="

# 没有 UNRESOLVED 才落下完成标记
if ! grep -q "^### BOOK" "$STATUS"; then
  : > "$LOG_DIR/all_done.flag"
  say "=== ALL BOOKS DONE ==="
fi
