#!/usr/bin/env python3
"""CLAUDE GAGE DESKTOP — デスクトップの上をClawdがとことこ歩く透明ウィジェット。

データ源はCLAUDE GAGE本体(:8902)の/api/status。貝殻は出さない(起きてる子だけ放牧)。
pixel-neon-widget作法のうち、枠・タイトルバーは意図的に無し(透明放牧が目的)。
奥行きトグル/位置永続/xcb起動は作法準拠。操作は右クリックメニュー。
"""
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

from PySide6.QtCore import Qt, QTimer, QThread, Signal, QPoint
from PySide6.QtGui import QPainter, QPixmap, QColor, QFont, QTransform, QCursor, QIcon
from PySide6.QtWidgets import QApplication, QWidget, QMenu, QMessageBox, QInputDialog, QLineEdit

API = "http://localhost:8902/api/status?shells="
SHELL_STATES = ("sleep", "buried", "clam")
STATE_DIR = os.path.expanduser("~/.config/claude-gage-desktop")
STATE = os.path.join(STATE_DIR, "state.json")

# ── 公式Clawdスプライト(claudeバイナリのCLAWD_FRAMES/CLAWD_PALそのまま) ──
PAL = {"O": "#d97757", "D": "#2a1f1b", "E": "#e98fa2", "F": "#7d848a",
       "P": "#e8b93c", "W": "#d9a066", "G": "#4a4a4a"}
WRITE_A = [
    "................", "................", "................", "..OOOOOOOO......",
    "..OOOOOOOO......", "..OODOODOO......", "..OODOODOO......", "..OOOOOOOOOEFPWG",
    "..OOOOOOOO......", "..OOOOOOOO......", "...OO..OO.......", "...OO..OO.......",
    "................", "................"]
WRITE_B = [
    "................", "..............WG", ".............P..", "..OOOOOOOO..P...",
    "..OOOOOOOO.F....", "..OODOODOOE.....", "..OODOODOOO.....", "..OOOOOOOO......",
    "..OOOOOOOO......", "..OOOOOOOO......", "...OO..OO.......", "...OO..OO.......",
    "................", "................"]
IDLE_A = [r.replace("E", ".").replace("F", ".").replace("P", ".")
           .replace("W", ".").replace("G", ".") for r in WRITE_A]
BLINK = [r.replace("D", "O") if i in (5, 6) else r for i, r in enumerate(IDLE_A)]
# キョロキョロ(目玉だけ左右にずらす)
LOOK_L = [r.replace("OODOODOO", "ODOODOOO") if i in (5, 6) else r
          for i, r in enumerate(IDLE_A)]
LOOK_R = [r.replace("OODOODOO", "OOODOODO") if i in (5, 6) else r
          for i, r in enumerate(IDLE_A)]
# 冬眠3態(Web版と同じドット)
PAL.update({"S": "#8a5a3b", "w": "#f0e0c0", "s": "#c9a06a"})
SLEEP = list(IDLE_A)
SLEEP[5] = SLEEP[5].replace("D", "O")
SLEEP[6] = "..ODDOODDO......"
BURIED = [
    "................", "................", "................", "................",
    "..OOOOOOOO......", "..OOOOOOOO......", "..ODDOODDO......", "..OOOOOOOO......",
    "ssssssssssssssss", "sswsssswsssswsss", "ssssssssssssssss", "................",
    "................", "................"]
CLAM = [
    "................", "................", "................", "....wwwwwww.....",
    "...wsssssssw....", "..wsswsswsssw...", "..wsswsswsswsw..", "..ssssssssssss..",
    "..SSSSSSSSSSSS..", "...SSSSSSSSSS...", "....SSSSSSSS....", "................",
    "................", "................"]
SW, SH = 16, 14
SCALE, SUBSCALE = 4, 2

TOOL_ICON = {"Bash": "⚙", "Read": "📖", "Edit": "✏️", "Write": "📝", "WebFetch": "🌐",
             "WebSearch": "🌐", "Agent": "🐣", "Task": "🐣", "Grep": "🔍", "Glob": "🔍"}


def bake(grid, scale):
    pm = QPixmap(SW * scale, SH * scale)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    for y, row in enumerate(grid):
        for x, ch in enumerate(row):
            if ch in PAL:
                p.fillRect(x * scale, y * scale, scale, scale, QColor(PAL[ch]))
    p.end()
    return pm


# メニュー用ドット絵アイコン(絵文字はxcbのQMenuでもQPainterでも描けない=自前ドットが確実)
ICON_PAL = {"W": "#e8e8e8", "D": "#3a3a4a", "B": "#4aa3ff", "G": "#2e7d4f"}
ICON_SKULL = [
    "..WWWWWW..", ".WWWWWWWW.", "WWWWWWWWWW", "WWDDWWDDWW", "WWDDWWDDWW",
    "WWWWWWWWWW", ".WWWDDWWW.", ".WWWWWWWW.", "..W.WW.W..", "..W.WW.W.."]
ICON_GLOBE = [
    "..BBBBBB..", ".BBGGBBBB.", "BBGGGGBBBB", "BGGGGBBGGB", "BGGGBBBGGB",
    "BBGGBBGGGB", "BBBBBGGGGB", "BBBGGGGBBB", ".BBBBGGBB.", "..BBBBBB.."]
ICON_PEN = [
    "......WWW.", ".....WWWWW", "....WWWWW.", "...WWWWW..", "..WWWWW...",
    ".WWWWW....", "WWWWW.....", "WDWW......", "WDD.......", "WW........"]
ICON_TERM = [
    "WWWWWWWWWW", "WDDDDDDDDW", "WDGGDDDDDW", "WDDGGDDDDW", "WDGGDDDDDW",
    "WDDDDGGGDW", "WDDDDDDDDW", "WWWWWWWWWW", "....WW....", "..WWWWWW.."]
ICON_ZIP = [
    "....WW....", "...WWWW...", "..WWWWWW..", "....WW....", "..........",
    "BBBBBBBBBB", "..........", "....WW....", "..WWWWWW..", "...WWWW..."]
_ICON_CACHE = {}


def pix_icon(key, grid, px=20):
    if key not in _ICON_CACHE:
        n = len(grid)
        cell = max(1, px // n)
        pm = QPixmap(cell * n, cell * n)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        for y, row in enumerate(grid):
            for x, ch in enumerate(row):
                if ch in ICON_PAL:
                    p.fillRect(x * cell, y * cell, cell, cell, QColor(ICON_PAL[ch]))
        p.end()
        _ICON_CACHE[key] = QIcon(pm)
    return _ICON_CACHE[key]


def load_state():
    try:
        with open(STATE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(st):
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(STATE, "w") as f:
        json.dump(st, f)


class Poller(QThread):
    data = Signal(list)

    def __init__(self, widget):
        super().__init__(widget)
        self.widget = widget

    def run(self):
        while not self.isInterruptionRequested():
            want_shells = getattr(self.widget, "show_shells", False)
            try:
                with urllib.request.urlopen(API + ("10" if want_shells else "0"),
                                            timeout=5) as r:
                    d = json.load(r)
                keep = ("working", "idle") + (SHELL_STATES if want_shells else ())
                self.data.emit([s for s in d["sessions"] if s["state"] in keep])
            except Exception:
                self.data.emit(None)  # 接続断=前回の子を維持
            self.msleep(3000)


class Gage(QWidget):
    def __init__(self):
        super().__init__()
        self.state = load_state()
        self._on_top = bool(self.state.get("on_top", False))
        depth = Qt.WindowStaysOnTopHint if self._on_top else Qt.WindowStaysOnBottomHint
        self.setWindowFlags(Qt.FramelessWindowHint | depth)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setWindowTitle("CLAUDE GAGE DESKTOP")

        self.show_shells = bool(self.state.get("show_shells", False))
        self.spr = {
            "writeA": bake(WRITE_A, SCALE), "writeB": bake(WRITE_B, SCALE),
            "idle": bake(IDLE_A, SCALE), "blink": bake(BLINK, SCALE),
            "lookL": bake(LOOK_L, SCALE), "lookR": bake(LOOK_R, SCALE),
            "idle_s": bake(IDLE_A, SUBSCALE),
            "writeA_s": bake(WRITE_A, SUBSCALE), "writeB_s": bake(WRITE_B, SUBSCALE),
            "sleep": bake(SLEEP, SUBSCALE + 1), "buried": bake(BURIED, SUBSCALE + 1),
            "clam": bake(CLAM, SUBSCALE + 1),
        }
        self.mirror = {k: v.transformed(QTransform().scale(-1, 1))
                       for k, v in self.spr.items()}

        scr = QApplication.primaryScreen().availableGeometry()
        w = self.state.get("size", [scr.width() - 120, 150])
        self.resize(*w)
        pos = self.state.get("pos")
        if pos:
            self.move(*pos)
        else:
            self.move(scr.x() + 60, scr.y() + scr.height() - self.height())

        self.critters = {}
        self.t = 0.0
        self.edit_mode = False
        self.font_label = QFont("JetBrainsMono Nerd Font", 8)
        self.font_bubble = QFont("JetBrainsMono Nerd Font", 9)

        self.poller = Poller(self)
        self.poller.data.connect(self.reconcile)
        self.poller.start()
        self.anim = QTimer(self)
        self.anim.timeout.connect(self.step)
        self.anim.start(33)
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.timeout.connect(self._save_geo)

    # ── データ同期 ──
    def reconcile(self, sessions):
        if sessions is None:
            return
        import random
        seen = set()
        for s in sessions:
            seen.add(s["id"])
            c = self.critters.get(s["id"])
            if not c:
                c = {"x": 40 + random.random() * max(40, self.width() - 160),
                     "tx": 0, "dir": 1, "phase": random.random() * 100,
                     "sess": s, "bubble": "", "bubble_until": 0, "subs": {}}
                c["tx"] = c["x"]
                self.critters[s["id"]] = c
            else:
                if s.get("last_text") and c["sess"].get("last_text") != s["last_text"]:
                    c["bubble"] = s["last_text"]
                    c["bubble_until"] = self.t + 10
            # compact直後: 一度だけスッキリ祝い(キラキラ+吹き出し)
            if (s.get("compact_age") is not None and s["compact_age"] < 300
                    and not c.get("compacted")):
                c["compacted"] = True
                c["bubble"] = f'✨スッキリ!{s.get("compact_saved_k", 0)}k軽くなった'
                c["bubble_until"] = self.t + 8
                c["sparkle_until"] = self.t + 20
            elif s.get("compact_age") is None:
                c["compacted"] = False
            c["sess"] = s
        for sid in list(self.critters):
            if sid not in seen:
                del self.critters[sid]
        # セッション間会話(SendMessage)のペアを双方向リンク化(受信側にはtalk_toが無い)
        for c in self.critters.values():
            c["mate"] = None
        for s in sessions:
            tid = s.get("talk_to")
            if not tid or s["state"] in SHELL_STATES:
                continue
            a, b = self.critters.get(s["id"]), self.critters.get(tid)
            if a and b and b["sess"]["state"] not in SHELL_STATES:
                a["mate"], b["mate"] = tid, s["id"]

    # ── アニメ ──
    def step(self):
        import random
        self.t += 0.033
        # 縄張り制: 画面幅を頭数で等分し、各自が自分のスロット内をうろつく
        # (押し合い方式は相互に目標を書き換え合ってプルプル震えるのでやめた)
        walkers = [c for c in self.critters.values()
                   if c["sess"]["state"] not in SHELL_STATES]
        if walkers:
            span = max(1.0, self.width() - SW * SCALE - 40)
            n = len(walkers)
            for idx, c in enumerate(sorted(walkers, key=lambda c: c["x"])):
                c["slot"] = 20 + span * (idx + 0.5) / n
                c["slot_half"] = max(30.0, span / n / 2 - 10)
        for c in walkers:
            s = c["sess"]
            c["phase"] += 0.016
            mate = self.critters.get(c.get("mate") or "")
            if s["state"] == "working":
                c["tx"] = c["x"]  # 執筆中は机から動かない(が、子の更新は下で続ける)
            elif mate:
                # 会話中: 相手のそばへ寄って向かい合う(縄張りより優先)
                D = SW * SCALE + 6
                if abs(c["x"] - mate["x"]) > D + 8:
                    c["tx"] = mate["x"] + (-D if c["x"] < mate["x"] else D)
                    c["x"] += (1 if c["tx"] > c["x"] else -1) * 0.35
                    c["dir"] = 1 if c["tx"] > c["x"] else -1
                else:
                    c["tx"] = c["x"]
                    c["dir"] = 1 if mate["x"] >= c["x"] else -1
            else:
                if abs(c["x"] - c["slot"]) > c["slot_half"]:
                    # 縄張りの外に居る=中へ帰る(一度だけ設定→着くまで歩く。毎フレーム再設定しない)
                    if abs(c["tx"] - c["slot"]) > c["slot_half"]:
                        c["tx"] = c["slot"] + (random.random() - 0.5) * 40
                elif abs(c["x"] - c["tx"]) < 2:
                    if random.random() < 0.004:
                        c["tx"] = c["slot"] + (random.random() - 0.5) * 1.6 * c["slot_half"]
                if abs(c["x"] - c["tx"]) >= 2:
                    c["x"] += (1 if c["tx"] > c["x"] else -1) * 0.35
                    c["dir"] = 1 if c["tx"] > c["x"] else -1
            # 子エージェント(旧実装はworking親でcontinueしてしまい一切表示されないバグだった)
            for k, sub in enumerate(s.get("subagents", [])):
                ch = c["subs"].setdefault(sub["id"], {"x": c["x"] - (k + 1) * 30 * c["dir"], "dir": c["dir"]})
                target = c["x"] - (k + 1) * 26 * c["dir"] - 6 * c["dir"]
                dx = target - ch["x"]
                ch["x"] += dx * 0.05
                if abs(dx) > 1.5:
                    ch["dir"] = 1 if dx > 0 else -1
            live = {x["id"] for x in s.get("subagents", [])}
            for sid in list(c["subs"]):
                if sid not in live:
                    del c["subs"][sid]
        self.update()

    # ── 描画 ──
    def paintEvent(self, ev):
        import math
        p = QPainter(self)
        p.setRenderHint(QPainter.SmoothPixmapTransform, False)
        # 貝殻ゾーン(表示中のみ底1段)。歩く床はその分せり上がる
        shell_sc = SUBSCALE + 1
        shells = [c for c in self.critters.values() if c["sess"]["state"] in SHELL_STATES]
        shell_zone = (SH * shell_sc + 4) if shells else 0
        floor = self.height() - SH * SCALE - 34 - shell_zone
        shell_row_w = len(shells) * (SW * shell_sc + 10) - 10 if shells else 0
        shell_x0 = max(8, (self.width() - shell_row_w) // 2)  # センター寄せ
        for k, c in enumerate(sorted(shells, key=lambda c: c["sess"]["age"])):
            sx = shell_x0 + k * (SW * shell_sc + 10)
            sy = self.height() - SH * shell_sc - 2
            p.drawPixmap(sx, sy, self.spr[c["sess"]["state"]])
            c["hit"] = (sx, sy, SW * shell_sc, SH * shell_sc)
            if c["sess"]["state"] == "sleep":
                p.setFont(self.font_label)
                p.setPen(QColor("#9a9ab8"))
                p.drawText(sx + SW * shell_sc - 4, sy - 2, "z" * (int(self.t) % 3 + 1))
            if c["bubble_until"] > self.t and c["bubble"]:
                self._draw_bubble(p, c, sy)
        # 働いてる子(オレンジラベル)を後に描く=手前に来る。灰色は奥
        ordered = sorted((c for c in self.critters.values() if c not in shells),
                         key=lambda c: c["sess"]["state"] == "working")
        for i, c in enumerate(ordered):
            s = c["sess"]
            working = s["state"] == "working"
            bob = 0 if working else round(math.sin(c["phase"] * 3))
            y = floor + bob
            if working:
                wc = int(self.t * 2.5 + c["phase"] * 10) % 22  # 約8.8秒周期・各自ずらし
                if wc == 20:
                    key = "idle"       # 顔を上げて一息
                elif wc == 21:
                    key = "blink"
                else:
                    key = "writeA" if int(self.t * 3.5) % 2 else "writeB"
            else:
                gc = int(self.t * 1.1 + c["phase"] * 10) % 26  # キョロキョロはたまにだけ(約23秒周期)
                if gc == 10:
                    key = "lookL"
                elif gc == 18:
                    key = "lookR"
                else:
                    key = "blink" if int(self.t * 8) % 30 == 0 else "idle"
            pm = self.spr[key] if c["dir"] >= 0 else self.mirror[key]
            p.drawPixmap(int(c["x"]), y, pm)
            c["hit"] = (c["x"] - 6, y - 14, SW * SCALE + 12, SH * SCALE + 30)
            # サブエージェント
            for k, sub in enumerate(s.get("subagents", [])):
                ch = c["subs"].get(sub["id"])
                if not ch:
                    continue
                walking = abs(ch["x"] - (c["x"] - (k + 1) * 26 * c["dir"])) > 8
                skey = ("writeA_s" if int(self.t * 3.5 + k) % 2 else "writeB_s") \
                    if sub["state"] == "working" and not walking else "idle_s"
                spm = self.spr[skey] if ch["dir"] >= 0 else self.mirror[skey]
                hop = abs(round(math.sin(self.t * 11 + k * 2) * 2)) if walking else 0
                p.drawPixmap(int(ch["x"]), y + SH * (SCALE - SUBSCALE) - hop, spm)
            # ラベル
            p.setFont(self.font_label)
            p.setPen(QColor("#d97757" if working else "#9a9ab8"))
            # ラベル: 幅に収まるよう「…」省略(枠クリップで頭が欠けるのを防ぐ)+2段互い違い
            fm = p.fontMetrics()
            box_w = SW * SCALE + 100
            label = fm.elidedText(s.get("title") or "", Qt.ElideRight, box_w)
            p.drawText(int(c["x"] + SW * SCALE / 2 - box_w / 2),
                       y + SH * SCALE + 2 + (14 if i % 2 else 0),
                       box_w, 14, Qt.AlignHCenter, label)
            # ツール実況
            if working and s.get("last_tool") and c["bubble_until"] <= self.t:
                p.setPen(QColor("#9a9ab8"))
                p.drawText(int(c["x"] - 30), y - 16, SW * SCALE + 60, 14, Qt.AlignHCenter,
                           f'{TOOL_ICON.get(s["last_tool"], "🔧")} {s["last_tool"]}')
            # compact直後のキラキラ(粒がふわふわ回る)
            if c.get("sparkle_until", 0) > self.t:
                for k in range(5):
                    a = self.t * 2.5 + k * 1.26
                    px = c["x"] + SW * SCALE / 2 + math.cos(a) * (18 + 7 * math.sin(self.t * 1.7 + k * 2))
                    py = y + SH * SCALE / 2 - 6 + math.sin(a * 1.3) * 16
                    p.fillRect(int(px), int(py), 3 if int(self.t * 4 + k) % 2 else 2,
                               3 if int(self.t * 4 + k) % 2 else 2,
                               QColor(("#ffe066", "#00f0ff", "#ffffff")[k % 3]))
            # セッション間会話中: そばまで来てたらペアの間にドット吹き出し(絵文字はQtで描けない)
            mate = self.critters.get(c.get("mate") or "")
            if mate and s["id"] < c["mate"] and abs(c["x"] - mate["x"]) < SW * SCALE + 40:
                mx = int((c["x"] + mate["x"] + SW * SCALE) / 2)
                my = y - 14 - int(math.sin(self.t * 2.5) * 2)
                p.fillRect(mx - 10, my, 20, 12, QColor("#ffffff"))
                p.fillRect(mx - 2, my + 12, 4, 3, QColor("#ffffff"))
                on = int(self.t * 2) % 3  # 「…」が順に灯る
                for k in range(3):
                    p.fillRect(mx - 6 + k * 5, my + 5, 3, 3,
                               QColor("#3a3a4a" if k <= on else "#c8c8d8"))
            # 吹き出し
            if c["bubble_until"] > self.t and c["bubble"]:
                self._draw_bubble(p, c, y)
        # 編集モード: 帯の輪郭+右下グリップ+サイズを可視化(CROSS HOTBAR方式)
        if self.edit_mode:
            from PySide6.QtGui import QPen
            pen = QPen(QColor("#00e5ff"), 2, Qt.DashLine)
            p.setPen(pen)
            p.drawRect(1, 1, self.width() - 2, self.height() - 2)
            p.setPen(QPen(QColor("#00e5ff"), 1))
            w, h = self.width(), self.height()
            for off in (5, 10, 15):
                p.drawLine(w - off, h - 3, w - 3, h - off)
            p.setFont(self.font_label)
            p.drawText(8, 16, f"📐 {w}x{h}  右下ドラッグ=サイズ / 中ドラッグ=移動 / 右クリックで終了")
        p.end()

    def _draw_bubble(self, p, c, y):
        text = " ".join(c["bubble"].split())
        lines = [text[i:i + 16] for i in range(0, min(len(text), 48), 16)]
        if len(text) > 48:
            lines[-1] = lines[-1][:15] + "…"
        p.setFont(self.font_bubble)
        fm = p.fontMetrics()
        bw = max(fm.horizontalAdvance(l) for l in lines) + 14
        bh = len(lines) * (fm.height() + 1) + 8
        bx = int(min(max(2, c["x"] + SW * SCALE / 2 - bw / 2), self.width() - bw - 2))
        by = int(y - bh - 12)
        if by >= 2:
            # 頭上に置ける
            p.fillRect(bx, by, bw, bh, QColor("#ffffff"))
            p.fillRect(int(c["x"] + SW * SCALE / 2 - 3), by + bh, 6, 4, QColor("#ffffff"))
        else:
            # 頭上が詰まってる(貝殻段で床がせり上がった等)→横に出す
            spr_w = SW * SCALE
            if c["x"] + spr_w + 10 + bw <= self.width() - 2:
                bx = int(c["x"] + spr_w + 10)
            else:
                bx = int(max(2, c["x"] - bw - 10))
            by = int(max(2, min(y + SH * SCALE / 2 - bh / 2,
                                self.height() - bh - 2)))
            p.fillRect(bx, by, bw, bh, QColor("#ffffff"))
            ty = int(y + SH * SCALE / 2 - 2)
            if bx > c["x"]:
                p.fillRect(bx - 4, ty, 4, 4, QColor("#ffffff"))
            else:
                p.fillRect(bx + bw, ty, 4, 4, QColor("#ffffff"))
        p.setPen(QColor("#0d0d1a"))
        for i, l in enumerate(lines):
            p.drawText(bx + 7, by + fm.ascent() + 4 + i * (fm.height() + 1), l)

    # ── 操作 ──
    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            pos = e.position()
            if self.edit_mode:
                wh = self.windowHandle()
                near_r = pos.x() > self.width() - 24
                near_b = pos.y() > self.height() - 24
                if wh is not None:
                    if near_r and near_b:
                        wh.startSystemResize(Qt.BottomEdge | Qt.RightEdge)
                    elif near_r:
                        wh.startSystemResize(Qt.RightEdge)
                    elif near_b:
                        wh.startSystemResize(Qt.BottomEdge)
                    else:
                        wh.startSystemMove()
                return
            for c in self.critters.values():
                hit = c.get("hit")
                if not hit:
                    continue
                hx, hy, hw, hh = hit
                if hx <= pos.x() <= hx + hw and hy <= pos.y() <= hy + hh:
                    s = c["sess"]
                    age = s["age"]
                    ago = (f"{age}秒前" if age < 90 else f"{age//60}分前" if age < 5400
                           else f"{age//3600}時間前" if age < 129600 else f"{age//86400}日前")
                    c["bubble"] = f'{s.get("title", "")} · {ago}'
                    c["bubble_until"] = self.t + 4
                    return
            wh = self.windowHandle()
            if wh is not None and wh.startSystemMove():
                return

    def contextMenuEvent(self, e):
        m = QMenu(self)
        target = self._critter_at(e.pos())
        if target is not None:
            s = target["sess"]
            title = (s.get("title") or s["id"][:8])[:24]
            # 各Claudeが名乗る台帳名(muu-xx)。話しかけた時の照合用に常に見せる
            agent = s.get("proc_name") or ""
            hdr = m.addAction(f"{title}  =  {agent}" if agent else title)
            hdr.setEnabled(False)
            m.addSeparator()
            term_label = ("ターミナルで開く" if s["state"] not in SHELL_STATES
                          else "復活してターミナルで開く")
            m.addAction(pix_icon("term", ICON_TERM), f"「{title}」を{term_label}",
                        lambda sess=s: self._open_terminal(sess))
            m.addAction(pix_icon("pen", ICON_PEN), f"「{title}」に名前を付ける",
                        lambda sess=s: self._rename_session(sess))
            if s.get("can_say"):  # /compact注入はエサやり口(tmux)がある子だけ
                m.addAction(pix_icon("zip", ICON_ZIP), f"「{title}」をcompactする",
                            lambda sess=s: self._compact_session(sess))
            if s["state"] not in SHELL_STATES:  # 貝殻はプロセスが無いので終了は生きてる子だけ
                m.addAction(pix_icon("skull", ICON_SKULL), f"「{title}」を終了する",
                            lambda sid=s["id"], t=title: self._close_session(sid, t))
            m.addSeparator()
        m.addAction("▲ 最前面にする" if not self._on_top else "▼ 最背面に戻す",
                    self._toggle_depth)
        shell_act = m.addAction("貝殻(冬眠中)も出す", self._toggle_shells)
        shell_act.setCheckable(True)
        shell_act.setChecked(self.show_shells)
        edit_act = m.addAction("帯の編集モード(サイズ/位置)", self._toggle_edit)
        edit_act.setCheckable(True)
        edit_act.setChecked(self.edit_mode)
        m.addAction(pix_icon("globe", ICON_GLOBE), "Web版の水槽を開く",
                    lambda: os.system("xdg-open http://localhost:8902/ >/dev/null 2>&1 &"))
        m.addSeparator()
        m.addAction("✕ 放牧をやめる", self.close)
        m.exec(QCursor.pos())

    def _critter_at(self, pos):
        for c in self.critters.values():
            hit = c.get("hit")
            if not hit:
                continue
            hx, hy, hw, hh = hit
            if hx <= pos.x() <= hx + hw and hy <= pos.y() <= hy + hh:
                return c
        return None

    def _open_terminal(self, sess):
        try:
            req = urllib.request.Request(
                "http://localhost:8902/api/open_terminal",
                data=json.dumps({"id": sess["id"]}).encode(),
                headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=25) as r:
                d = json.load(r)
            c = self.critters.get(sess["id"])
            if c is not None:
                c["bubble"] = {"attach": "タブに出た!", "focus": "ここだよ!",
                               "resume": "おはよう…!"}.get(d.get("via"), "開いた!")
                c["bubble_until"] = self.t + 4
        except urllib.error.HTTPError as ex:
            msg = ex.read().decode("utf-8", "replace")
            try:
                msg = json.loads(msg).get("error", msg)
            except ValueError:
                pass
            QMessageBox.warning(self, "ターミナルで開く", f"開けなかった:\n{msg}")
        except Exception as ex:
            QMessageBox.warning(self, "ターミナルで開く", f"サーバーに届かなかった:\n{ex}")

    def _rename_session(self, sess):
        # QInputDialogだとpip版PySide6にfcitxプラグインが無く日本語が打てない
        # (システムプラグインはQt_6.11_PRIVATE_APIのABI不一致で移植も不可)。
        # → システムQtのkdialogに外注すればIMEが確実に効く
        cur = sess.get("nick", "")
        auto = sess.get("auto_title") or sess["id"][:8]
        try:
            r = subprocess.run(
                ["kdialog", "--title", "CLAUDE GAGE — 名前を付ける",
                 "--inputbox", f"この子の名前(空欄で自動タイトルに戻す)\n自動タイトル: {auto}", cur],
                capture_output=True, text=True, timeout=300)
        except FileNotFoundError:
            name, ok = QInputDialog.getText(self, "名前を付ける",
                                            "この子の名前(日本語入力は不可かも):",
                                            QLineEdit.Normal, cur)
            if not ok:
                return
            r = None
        except subprocess.TimeoutExpired:
            return
        if r is not None:
            if r.returncode != 0:  # キャンセル
                return
            name = r.stdout.rstrip("\n")
        name = name.strip()[:40]
        try:
            req = urllib.request.Request(
                "http://localhost:8902/api/rename",
                data=json.dumps({"id": sess["id"], "name": name}).encode(),
                headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=5) as r:
                json.load(r)
            sess["title"] = name or sess.get("auto_title") or sess["id"][:8]
            sess["nick"] = name
            c = self.critters.get(sess["id"])
            if c is not None:
                c["bubble"] = f"{name}になった!" if name else "名前が消えた…"
                c["bubble_until"] = self.t + 4
            self.update()
        except Exception as ex:
            QMessageBox.warning(self, "名前を付ける", f"サーバーに届かなかった:\n{ex}")

    def _compact_session(self, sess):
        title = (sess.get("title") or sess["id"][:8])[:24]
        ctx = sess.get("ctx_tokens", 0)
        ans = QMessageBox.question(
            self, "compactする",
            f"「{title}」に /compact を流し込む?\n"
            f"(いまのコンテキストは約{ctx//1000}kトークン。数分かかることがある)",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if ans != QMessageBox.Yes:
            return
        try:
            req = urllib.request.Request(
                "http://localhost:8902/api/say",
                data=json.dumps({"id": sess["id"], "text": "/compact"}).encode(),
                headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=15) as r:
                json.load(r)
            c = self.critters.get(sess["id"])
            if c is not None:
                c["bubble"] = "ぎゅ〜っ…🗜(数分かかるよ)"
                c["bubble_until"] = self.t + 6
        except urllib.error.HTTPError as ex:
            msg = ex.read().decode("utf-8", "replace")
            try:
                msg = json.loads(msg).get("error", msg)
            except ValueError:
                pass
            QMessageBox.warning(self, "compactする", f"流し込めなかった:\n{msg}")
        except Exception as ex:
            QMessageBox.warning(self, "compactする", f"サーバーに届かなかった:\n{ex}")

    def _close_session(self, sid, title):
        ans = QMessageBox.question(
            self, "セッション終了",
            f"「{title}」を終了する?\n\n記録は残るので、あとで貝殻から復活できる。",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if ans != QMessageBox.Yes:
            return
        try:
            req = urllib.request.Request(
                "http://localhost:8902/api/close",
                data=json.dumps({"id": sid}).encode(),
                headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=5) as r:
                json.load(r)
            c = self.critters.get(sid)
            if c is not None:
                c["bubble"] = "おやすみ…💤"
                c["bubble_until"] = self.t + 4
        except urllib.error.HTTPError as ex:
            msg = ex.read().decode("utf-8", "replace")
            try:
                msg = json.loads(msg).get("error", msg)
            except ValueError:
                pass
            QMessageBox.warning(self, "セッション終了", f"終了できなかった:\n{msg}")
        except Exception as ex:
            QMessageBox.warning(self, "セッション終了", f"サーバーに届かなかった:\n{ex}")

    def _toggle_edit(self):
        self.edit_mode = not self.edit_mode
        self.setCursor(Qt.SizeAllCursor if self.edit_mode else Qt.ArrowCursor)

    def _toggle_shells(self):
        self.show_shells = not self.show_shells
        if not self.show_shells:
            for sid in [k for k, c in self.critters.items()
                        if c["sess"]["state"] in SHELL_STATES]:
                del self.critters[sid]
        self.state["show_shells"] = self.show_shells
        save_state(self.state)

    def _toggle_depth(self):
        self._on_top = not self._on_top
        geo = self.geometry()
        self.setWindowFlag(Qt.WindowStaysOnTopHint, self._on_top)
        self.setWindowFlag(Qt.WindowStaysOnBottomHint, not self._on_top)
        self.setGeometry(geo)
        self.show()
        self.state["on_top"] = self._on_top
        save_state(self.state)

    def moveEvent(self, e):
        self._save_timer.start(500)

    def resizeEvent(self, e):
        self._save_timer.start(500)

    def _save_geo(self):
        self.state["pos"] = [self.x(), self.y()]
        self.state["size"] = [self.width(), self.height()]
        save_state(self.state)

    def closeEvent(self, e):
        self._save_geo()
        self.poller.requestInterruption()
        self.poller.wait(4000)
        e.accept()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setApplicationName("claude-gage-desktop")
    app.setDesktopFileName("claude-gage-desktop")
    g = Gage()
    g.show()
    sys.exit(app.exec())
