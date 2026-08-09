#!/usr/bin/env bash
# CLAUDE GAGE DESKTOP ランチャー(xcb強制=位置記憶が効く)
cd "$(dirname "$(readlink -f "$0")")" || exit 1
export QT_QPA_PLATFORM="${QT_QPA_PLATFORM:-xcb}"
systemctl --user start claude-gage.service 2>/dev/null
exec python3 gage_desktop.py "$@"
