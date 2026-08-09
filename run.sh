#!/usr/bin/env bash
# CLAUDE GAGE ランチャー: サーバ死活確認→Braveアプリ窓
# ★--class はBrave既存プロセスがあると効かない→専用プロファイルで別プロセス化(worldmonitorと同じ作法)
# ★別プロファイルは常用BraveのWebGL回避フラグを継承しない→明示指定(canvas 2Dでも無害)
URL="http://localhost:8902/"

if ! curl -sf -m 2 "$URL" >/dev/null; then
  systemctl --user start claude-gage.service
  for i in $(seq 1 20); do curl -sf -m 2 "$URL" >/dev/null && break; sleep 0.5; done
fi

exec brave-browser \
  --app="$URL" \
  --class=claude-gage \
  --user-data-dir="$HOME/Claude/claude-gage/brave-profile" \
  --ozone-platform=x11 --enable-unsafe-swiftshader
