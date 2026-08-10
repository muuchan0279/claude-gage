#!/usr/bin/env python3
"""CLAUDE GAGE — いま何匹のClaudeが働いてるかを飼育ケース風に見せるWebアプリ (:8902)

データ源は ~/.claude/projects/**/<sessionId>.jsonl のmtimeと中身だけ。
- working: 120秒以内に書き込みがあった = 生成中(鉛筆カリカリ)
- idle:    30分以内 = 生きてるが待機(うろうろ)
- サブエージェントは <sessionId>/subagents/agent-*.jsonl から検出して子として付ける
プロセスは見ない(mtimeが一次情報)。全走査はトップレベル1階層のみ=symlink爆発と無縁。
"""
import json
import os
import subprocess
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "1.20.0"  # 更新のたびに上げる(機能追加=minor / 修正=patch)

ROOT = os.path.expanduser("~/.claude/projects")
SESS_REG = os.path.expanduser("~/.claude/sessions")
HERE = os.path.dirname(os.path.abspath(__file__))
PORT = 8902
WORKING_S = 120
IDLE_S = 1800
SLEEP_S = 2 * 3600      # ここまで=寝てるClawd
BURIED_S = 24 * 3600    # ここまで=砂に半埋まり
SHELL_S = 7 * 86400     # ここまで=二枚貝。以降は表示しない
MAX_SHELLS = 10
TAIL_BYTES = 256 * 1024

# path -> (mtime, info) 変わってないファイルは読み直さない
_cache = {}


def tail_lines(path):
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            if size > TAIL_BYTES:
                f.seek(size - TAIL_BYTES)
                f.readline()  # 途中行を捨てる
            return f.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return []


def session_info(path):
    """jsonl末尾からタイトルと最後の発言を拾う(mtimeキャッシュ付き)"""
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None
    hit = _cache.get(path)
    if hit and hit[0] == mtime:
        return hit[1]

    title = ""
    last_text = ""
    last_text_full = ""
    last_tool = ""
    cwd = ""
    ctx_tokens = 0
    compact_ts = 0.0
    compact_saved = 0
    talk_to = ""
    talk_ts = 0.0
    lines = tail_lines(path)
    # セッション間会話(SendMessage)の相手を先に拾う。メインループはtitle等が
    # 揃った時点でbreakするので、別パスにしないと直近の送信を取りこぼす
    for line in reversed(lines):
        if '"SendMessage"' not in line or '"tool_use"' not in line:
            continue
        try:
            d = json.loads(line)
            for block in d.get("message", {}).get("content", []):
                if (isinstance(block, dict) and block.get("type") == "tool_use"
                        and block.get("name") == "SendMessage"):
                    talk_to = str(block.get("input", {}).get("to", ""))
                    ts = d.get("timestamp", "")
                    talk_ts = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
                    break
        except (ValueError, TypeError):
            continue
        if talk_to:
            break
    for line in reversed(lines):
        if title and last_text and cwd and ctx_tokens:
            break
        if not compact_ts and '"compact_boundary"' in line:
            try:
                d = json.loads(line)
                meta = d.get("compactMetadata", {})
                ts = d.get("timestamp", "")
                compact_ts = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
                compact_saved = max(0, meta.get("preTokens", 0) - meta.get("postTokens", 0))
            except (ValueError, TypeError):
                pass
            continue
        if not ctx_tokens and '"usage"' in line and '"assistant"' in line:
            try:
                u = json.loads(line).get("message", {}).get("usage", {})
                ctx_tokens = (u.get("input_tokens", 0) + u.get("cache_read_input_tokens", 0)
                              + u.get("cache_creation_input_tokens", 0) + u.get("output_tokens", 0))
            except ValueError:
                pass
        if not cwd and '"cwd"' in line:
            try:
                cwd = json.loads(line).get("cwd", "") or cwd
            except ValueError:
                pass
        if not title and '"ai-title"' in line:
            try:
                title = json.loads(line).get("aiTitle", "")
            except ValueError:
                pass
            continue
        if not last_text and '"assistant"' in line:
            try:
                content = json.loads(line).get("message", {}).get("content")
            except ValueError:
                continue
            if isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "text" and block.get("text", "").strip():
                        last_text = block["text"].strip()[:160]   # 吹き出し用(短)
                        last_text_full = block["text"].strip()[:4000]  # パネル用(全文・スクロールで読む)
                        break
                    if block.get("type") == "tool_use" and not last_tool:
                        last_tool = block.get("name", "")
            elif isinstance(content, str) and content.strip():
                last_text = content.strip()[:160]
                last_text_full = content.strip()[:4000]
    info = {"title": title, "last_text": last_text, "last_text_full": last_text_full,
            "last_tool": last_tool, "cwd": cwd,
            "ctx_tokens": ctx_tokens, "compact_ts": compact_ts, "compact_saved": compact_saved,
            "talk_to": talk_to, "talk_ts": talk_ts}
    _cache[path] = (mtime, info)
    return info


def inject_text(proc, text):
    """生きてるセッションのプロンプトへテキスト+Enterを注入。返り値=(HTTP相当コード, エラー) / 成功=(200, None)"""
    tmux = proc.get("tmux", "")
    if not tmux:
        # kittyタブ直走りの子: kitten @ send-textで注入(tmuxが無くてもエサやり可)
        sock, win = kitty_find_window(proc["pid"])
        if not sock:
            return 409, "この子はtmuxにもkittyにも居ない(注入口が無い)"
        try:
            r1 = subprocess.run(["kitten", "@", "--to", f"unix:{sock}", "send-text",
                                 "--match", f"id:{win}", "--", text],
                                capture_output=True, text=True, timeout=10)
            if r1.returncode != 0:
                return 500, f"send-text失敗: {r1.stderr.strip()[:120]}"
            time.sleep(0.15)  # TUIがペーストを受けてからEnter
            subprocess.run(["kitten", "@", "--to", f"unix:{sock}", "send-text",
                            "--match", f"id:{win}", "--", "\r"],
                           capture_output=True, text=True, timeout=10)
            return 200, None
        except subprocess.TimeoutExpired:
            return 500, "kittyがタイムアウト"
    pane = tmux.split(".")[-1]  # 'claude-3:@3.%3' -> '%3'
    try:
        r1 = subprocess.run(["tmux", "send-keys", "-t", pane, "-l", "--", text],
                            capture_output=True, text=True, timeout=10)
        if r1.returncode != 0:
            return 500, f"send-keys失敗: {r1.stderr.strip()[:120]}"
        time.sleep(0.15)  # TUIがペーストを受けてからEnter
        subprocess.run(["tmux", "send-keys", "-t", pane, "Enter"],
                       capture_output=True, text=True, timeout=10)
        return 200, None
    except subprocess.TimeoutExpired:
        return 500, "tmuxがタイムアウト"


# ---- 自動compact見回り ----
# 2時間さわってない(jsonl無更新)+コンテキストが太ってる生存セッションに/compactを流す。
# compactが走るとjsonlが更新されて経過時間が0に戻る=自然に再発火しない。
# 注入失敗(注入口なし等)へのリトライは1時間に1回まで
AUTO_COMPACT_S = 2 * 3600
AUTO_COMPACT_MIN_TOK = 50_000
AUTO_COMPACT_RETRY_S = 3600
AUTO_COMPACT_TICK_S = 300
_auto_compact_last = {}   # sid -> 最後に注入を試みたtime.time()
auto_compact_log = []     # 直近の実績(APIで見せる用)


def auto_compact_patrol():
    while True:
        time.sleep(AUTO_COMPACT_TICK_S)
        try:
            now = time.time()
            procs = proc_map()
            for sid, proc in procs.items():
                if proc.get("status") == "busy":
                    continue
                if now - _auto_compact_last.get(sid, 0) < AUTO_COMPACT_RETRY_S:
                    continue
                path = None
                for proj in os.listdir(ROOT):
                    cand = os.path.join(ROOT, proj, sid + ".jsonl")
                    if os.path.isfile(cand):
                        path = cand
                        break
                if not path:
                    continue
                try:
                    age = now - os.path.getmtime(path)
                except OSError:
                    continue
                if age < AUTO_COMPACT_S:
                    continue
                info = session_info(path) or {}
                if info.get("ctx_tokens", 0) < AUTO_COMPACT_MIN_TOK:
                    continue
                _auto_compact_last[sid] = now
                code, err = inject_text(proc, "/compact")
                label = proc.get("name") or sid[:8]
                auto_compact_log.append(
                    {"ts": now, "sid": sid, "name": label,
                     "ok": err is None, "err": err or "",
                     "ctx_k": round(info.get("ctx_tokens", 0) / 1000)})
                del auto_compact_log[:-20]
                print(f"[auto-compact] {label} ctx={info.get('ctx_tokens',0)//1000}k "
                      f"age={int(age//60)}min -> {'ok' if err is None else err}", flush=True)
        except Exception as e:  # 見回りは何があっても死なない
            print(f"[auto-compact] patrol error: {e}", flush=True)


def desktop_pid():
    """gage_desktop.pyのPIDを/proc走査で探す(pgrep -fは自爆しうるのでこの母艦の一般則に従う)"""
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                cmd = f.read().decode("utf-8", "replace")
        except OSError:
            continue
        # 実行スクリプトとして起動してるものだけ(テスト/エディタ等の誤マッチ防止)。
        # run-desktop.shはcdして相対パス起動するので、argv[1]をcwd基準で実体解決して照合する
        argv = cmd.split("\0")
        if not (argv[0].endswith("python3") and len(argv) > 1):
            continue
        try:
            cwd = os.readlink(f"/proc/{pid}/cwd")
        except OSError:
            continue
        script = os.path.normpath(os.path.join(cwd, argv[1]))
        if script == os.path.join(HERE, "gage_desktop.py"):
            return int(pid)
    return None


NAMES_FILE = os.path.join(HERE, "names.json")


def load_names():
    """むーちゃんが付けた別名 {sessionId: 名前}。表示名を上書きする"""
    try:
        with open(NAMES_FILE) as f:
            d = json.load(f)
            return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save_names(names):
    with open(NAMES_FILE, "w") as f:
        json.dump(names, f, ensure_ascii=False, indent=1)


def kitty_find_window(pid):
    """そのpidが前面で動いてるkitty窓を探す。返り値=(ソケット, window_id) or (None, None)"""
    try:
        socks = [f"/tmp/{f}" for f in os.listdir("/tmp") if f.startswith("mykitty-")]
    except OSError:
        return None, None
    for sock in socks:
        try:
            r = subprocess.run(["kitten", "@", "--to", f"unix:{sock}", "ls"],
                               capture_output=True, text=True, timeout=3)
            if r.returncode != 0:  # 死んだkittyの残骸ソケットはここで弾かれる
                continue
            for osw in json.loads(r.stdout):
                for tab in osw.get("tabs", []):
                    for w in tab.get("windows", []):
                        if any(fp.get("pid") == pid
                               for fp in w.get("foreground_processes", [])):
                            return sock, w["id"]
        except (OSError, ValueError, subprocess.TimeoutExpired):
            continue
    return None, None


def kitty_rename_tab(pid, name):
    """tmux外でkittyタブ直走りの子: kitten @ set-tab-titleでタブ名上書き(空文字=自動に戻る・実測済)"""
    sock, win = kitty_find_window(pid)
    if not sock:
        return False
    r = subprocess.run(["kitten", "@", "--to", f"unix:{sock}", "set-tab-title",
                        "--match", f"id:{win}", name],
                       capture_output=True, timeout=3)
    return r.returncode == 0


def kitty_untitle_tmux_tab(tmux_session):
    """tmux組の改名時の落とし穴対策: アタッチ先kittyタブに手動タイトルが焼き付いてると
    端末タイトル(=tmuxの窓名)を無視し続けるので、自動題(空文字)に戻す"""
    try:
        socks = [f"/tmp/{f}" for f in os.listdir("/tmp") if f.startswith("mykitty-")]
    except OSError:
        return False
    ok = False
    for sock in socks:
        try:
            r = subprocess.run(["kitten", "@", "--to", f"unix:{sock}", "ls"],
                               capture_output=True, text=True, timeout=3)
            if r.returncode != 0:
                continue
            for osw in json.loads(r.stdout):
                for tab in osw.get("tabs", []):
                    cmds = " ".join(" ".join(fp.get("cmdline") or [])
                                    for w in tab.get("windows", [])
                                    for fp in w.get("foreground_processes", []))
                    if "tmux" in cmds and tmux_session in cmds.split():
                        subprocess.run(["kitten", "@", "--to", f"unix:{sock}",
                                        "set-tab-title", "--match", f"id:{tab['id']}", ""],
                                       capture_output=True, timeout=3)
                        ok = True
        except (OSError, ValueError, subprocess.TimeoutExpired):
            continue
    return ok


def kitty_live_sock():
    """生きてるkittyのリモコンソケットを1本返す(残骸はls失敗で弾く)"""
    try:
        for f in sorted(os.listdir("/tmp")):
            if f.startswith("mykitty-"):
                sock = f"/tmp/{f}"
                r = subprocess.run(["kitten", "@", "--to", f"unix:{sock}", "ls"],
                                   capture_output=True, timeout=3)
                if r.returncode == 0:
                    return sock
    except OSError:
        pass
    return None


def kitty_open_tab(argv):
    """kittyの新タブでコマンドを開く。kittyが居なければ新窓を立てる"""
    sock = kitty_live_sock()
    if sock:
        r = subprocess.run(["kitten", "@", "--to", f"unix:{sock}", "launch",
                            "--type=tab"] + argv, capture_output=True, timeout=5)
        if r.returncode == 0:
            return True
    try:
        subprocess.Popen(["kitty", "--detach"] + argv,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except OSError:
        return False


def start_resume(sid):
    """冬眠セッションをtmux上でclaude --resumeする。返り値=(tmux名, エラーメッセージ)"""
    if not sid or "/" in sid or ".." in sid:
        return None, "不正なID"
    if sid in proc_map():
        return None, "もう起きてる(プロセス生存中)"
    path = None
    for proj in os.listdir(ROOT):
        cand = os.path.join(ROOT, proj, sid + ".jsonl")
        if os.path.isfile(cand):
            path = cand
            break
    if not path:
        return None, "セッション記録が見つからない"
    info = session_info(path) or {}
    cwd = info.get("cwd") or os.path.expanduser("~")
    if not os.path.isdir(cwd):
        cwd = os.path.expanduser("~")
    claude_bin = os.path.expanduser("~/.local/bin/claude")
    tmux_name = f"gage-{sid[:8]}"
    r = subprocess.run(
        ["tmux", "new-session", "-d", "-s", tmux_name, "-c", cwd,
         f"{claude_bin} --resume {sid}"],
        capture_output=True, text=True, timeout=15)
    if r.returncode != 0:
        return None, f"tmux起動失敗: {r.stderr.strip()[:120]}"
    return tmux_name, None


def shell_state(age):
    if age <= SLEEP_S:
        return "sleep"
    if age <= BURIED_S:
        return "buried"
    return "clam"


def proc_map():
    """~/.claude/sessions/<pid>.json (公式台帳) から sessionId -> 生きてるpid/名前 を引く"""
    out = {}
    if not os.path.isdir(SESS_REG):
        return out
    for name in os.listdir(SESS_REG):
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(SESS_REG, name)) as f:
                meta = json.load(f)
            pid = int(meta.get("pid", name[:-5]))
        except (OSError, ValueError):
            continue
        if not os.path.isdir(f"/proc/{pid}"):
            continue
        sid = meta.get("sessionId")
        if sid:
            # 制御端末なし(tty_nr=0)=iOS/リモート生まれのヘッドレス組。看取り対象の目印
            try:
                with open(f"/proc/{pid}/stat") as f:
                    tty_nr = int(f.read().rsplit(")", 1)[1].split()[4])
            except (OSError, ValueError, IndexError):
                tty_nr = -1
            out[sid] = {"pid": pid, "name": meta.get("name", ""), "tmux": meta.get("tmux", ""),
                        "status": meta.get("status", ""), "ios": tty_nr == 0}
    return out


def scan(max_shells=MAX_SHELLS):
    now = time.time()
    names = load_names()
    sessions = []
    shells = []
    if not os.path.isdir(ROOT):
        return sessions
    procs = proc_map()
    # SendMessageの宛先解決用: 台帳名(muu-xx)とpid(uds:...<pid>.sock)の逆引き
    talk_by_name = {}
    talk_by_pid = {}
    for psid, p in procs.items():
        if p.get("name"):
            talk_by_name[p["name"]] = psid
        talk_by_pid[str(p["pid"])] = psid

    def resolve_talk(to, self_sid):
        to = to.split(" [")[0].strip()          # "muu-01 [b4f397]" → "muu-01"
        if to.endswith(".sock"):
            tsid = talk_by_pid.get(os.path.basename(to)[:-5], "")
        else:
            tsid = talk_by_name.get(to, "")
        return tsid if tsid and tsid != self_sid else ""

    for proj in os.listdir(ROOT):
        pdir = os.path.join(ROOT, proj)
        if not os.path.isdir(pdir):
            continue
        for name in os.listdir(pdir):
            if not name.endswith(".jsonl"):
                continue
            path = os.path.join(pdir, name)
            try:
                age = now - os.path.getmtime(path)
            except OSError:
                continue
            if age > SHELL_S:
                continue
            sid = name[:-6]
            # プロセスが死んでたら記録が新しくても即冬眠(/exit直後のゾンビ散歩を防ぐ)。
            # 生きてれば30分無活動でも待機のまま
            if sid not in procs:
                shells.append((age, sid, proj, path))
                continue
            subs = []
            subdir = os.path.join(pdir, sid, "subagents")
            if os.path.isdir(subdir):
                for sub in os.listdir(subdir):
                    if not sub.endswith(".jsonl"):
                        continue
                    try:
                        sage = now - os.path.getmtime(os.path.join(subdir, sub))
                    except OSError:
                        continue
                    if sage <= IDLE_S:
                        subs.append({
                            "id": sub[:-6],
                            "age": round(sage),
                            "state": "working" if sage <= WORKING_S else "idle",
                        })
            info = session_info(path) or {}
            proc = procs.get(sid)
            sessions.append({
                "alive": bool(proc),
                "pid": proc["pid"] if proc else None,
                "proc_name": proc["name"] if proc else "",
                "proc_status": proc["status"] if proc else "",
                # tmux組はsend-keys、kitty直組はkitten @ send-textで注入できる=生きてれば口はある
                "can_say": bool(proc),
                "ctx_tokens": info.get("ctx_tokens", 0),
                "id": sid,
                "proj": proj.strip("-").replace("-", "/"),
                "age": round(age),
                "state": "working" if age <= WORKING_S else "idle",
                # iOS/リモート生まれ(端末なし)は頭に[iOS]を書いとく=看取り候補が一目でわかる
                "title": (("[iOS] " if proc and proc.get("ios") else "")
                          + (names.get(sid) or info.get("title") or sid[:8])),
                "ios": bool(proc and proc.get("ios")),
                "nick": names.get(sid, ""),
                "auto_title": info.get("title", ""),
                # compact直後(15分以内)だけ演出用に渡す
                "compact_age": (round(now - info["compact_ts"])
                                if info.get("compact_ts") and now - info["compact_ts"] < 900
                                else None),
                "compact_saved_k": round(info.get("compact_saved", 0) / 1000),
                # 直近10分以内にSendMessageした相手(会話中ならお互い寄り添うアニメ用)
                "talk_to": (resolve_talk(info["talk_to"], sid)
                            if info.get("talk_ts") and now - info["talk_ts"] < 600
                            else ""),
                "last_text": info.get("last_text", ""),
            "last_text_full": info.get("last_text_full", ""),
                "last_text_full": info.get("last_text_full", ""),
                "last_tool": info.get("last_tool", ""),
                "subagents": sorted(subs, key=lambda s: s["age"]),
            })
    sessions.sort(key=lambda s: s["age"])
    # 貝殻=新しい順にmax_shells個だけ底に沈める
    for age, sid, proj, path in sorted(shells)[:max_shells]:
        info = session_info(path) or {}
        sessions.append({
            "alive": False,
            "pid": None,
            "proc_name": "",
            "id": sid,
            "proj": proj.strip("-").replace("-", "/"),
            "age": round(age),
            "state": shell_state(age),
            "title": names.get(sid) or info.get("title") or sid[:8],
            "nick": names.get(sid, ""),
            "auto_title": info.get("title", ""),
            "last_text": info.get("last_text", ""),
            "last_text_full": info.get("last_text_full", ""),
            "last_tool": "",
            "ctx_tokens": info.get("ctx_tokens", 0),
            "subagents": [],
        })
    return sessions


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/api/status"):
            max_shells = MAX_SHELLS
            if "shells=" in self.path:
                try:
                    max_shells = max(0, min(50, int(self.path.split("shells=")[1].split("&")[0])))
                except ValueError:
                    pass
            sessions = scan(max_shells)
            body = json.dumps({
                "version": VERSION,
                "desktop": desktop_pid() is not None,
                "time": time.time(),
                "working": sum(1 for s in sessions if s["state"] == "working"),
                "idle": sum(1 for s in sessions if s["state"] == "idle"),
                "sessions": sessions,
                "auto_compact": auto_compact_log[-5:],
            }, ensure_ascii=False).encode()
            self._send(200, "application/json; charset=utf-8", body)
        elif self.path in ("/", "/index.html"):
            try:
                with open(os.path.join(HERE, "index.html"), "rb") as f:
                    self._send(200, "text/html; charset=utf-8", f.read())
            except OSError:
                self._send(404, "text/plain", b"index.html not found")
        else:
            self._send(404, "text/plain", b"not found")

    def do_POST(self):
        if self.path == "/api/close":
            try:
                length = int(self.headers.get("Content-Length", 0))
                sid = json.loads(self.rfile.read(length)).get("id", "")
            except (ValueError, TypeError):
                self._send(400, "application/json", b'{"error":"bad request"}')
                return
            proc = proc_map().get(sid)
            if not proc:
                self._send(404, "application/json; charset=utf-8",
                           json.dumps({"error": "生きてるプロセスが見つからない(もう終了してるかも)"}, ensure_ascii=False).encode())
                return
            try:
                os.kill(proc["pid"], 15)  # SIGTERM=お行儀のいい終了
                body = json.dumps({"ok": True, "pid": proc["pid"]}).encode()
                self._send(200, "application/json", body)
            except OSError as e:
                self._send(500, "application/json; charset=utf-8",
                           json.dumps({"error": str(e)}, ensure_ascii=False).encode())
        elif self.path == "/api/rename":
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length))
                sid = body.get("id", "")
                name = str(body.get("name", "")).strip()[:40]
            except (ValueError, TypeError):
                self._send(400, "application/json", b'{"error":"bad request"}')
                return
            if not sid:
                self._send(400, "application/json", b'{"error":"id required"}')
                return
            names = load_names()
            if name:
                names[sid] = name
            else:
                names.pop(sid, None)  # 空=自動タイトルに戻す
            save_names(names)
            # ベストエフォート: tmuxで走ってる子は窓名にも反映(kittyのタブは設定次第)
            tmux_ok = False
            proc = proc_map().get(sid)
            win = (proc.get("tmux") or "").rsplit(".", 1)[0] if proc else ""
            if win:
                try:
                    if name:
                        r = subprocess.run(["tmux", "rename-window", "-t", win, name],
                                           capture_output=True, timeout=3)
                        subprocess.run(["tmux", "set-option", "-w", "-t", win,
                                        "automatic-rename", "off"],
                                       capture_output=True, timeout=3)
                    else:
                        r = subprocess.run(["tmux", "set-option", "-w", "-t", win,
                                            "automatic-rename", "on"],
                                           capture_output=True, timeout=3)
                    tmux_ok = r.returncode == 0
                except (OSError, subprocess.TimeoutExpired):
                    pass
                if tmux_ok:  # タブに手動題が焼き付いてると窓名が流れないので剥がす
                    kitty_untitle_tmux_tab(win.split(":")[0])
            kitty_ok = False
            if proc and not win:  # tmux外の子はkittyタブを直接書く
                kitty_ok = kitty_rename_tab(proc["pid"], name)
            self._send(200, "application/json; charset=utf-8",
                       json.dumps({"ok": True, "name": name,
                                   "tmux": tmux_ok, "kitty": kitty_ok},
                                  ensure_ascii=False).encode())
        elif self.path == "/api/say":
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length))
                sid, text = body.get("id", ""), str(body.get("text", "")).strip()
            except (ValueError, TypeError):
                self._send(400, "application/json", b'{"error":"bad request"}')
                return
            self._say(sid, text)
        elif self.path == "/api/desktop_start":
            if desktop_pid() is not None:
                self._send(200, "application/json", b'{"ok": true, "note": "already"}')
                return
            env = dict(os.environ)
            env.setdefault("DISPLAY", ":0")
            env["QT_QPA_PLATFORM"] = "xcb"
            try:
                subprocess.Popen(["/usr/bin/python3", os.path.join(HERE, "gage_desktop.py")],
                                 env=env, start_new_session=True,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                self._send(200, "application/json", b'{"ok": true}')
            except OSError as e:
                self._send(500, "application/json; charset=utf-8",
                           json.dumps({"error": str(e)}, ensure_ascii=False).encode())
        elif self.path == "/api/desktop_stop":
            pid = desktop_pid()
            if pid is None:
                self._send(200, "application/json", b'{"ok": true, "note": "not running"}')
                return
            try:
                os.kill(pid, 15)
                self._send(200, "application/json", b'{"ok": true}')
            except OSError as e:
                self._send(500, "application/json; charset=utf-8",
                           json.dumps({"error": str(e)}, ensure_ascii=False).encode())
        elif self.path == "/api/resume":
            try:
                length = int(self.headers.get("Content-Length", 0))
                sid = json.loads(self.rfile.read(length)).get("id", "")
            except (ValueError, TypeError):
                self._send(400, "application/json", b'{"error":"bad request"}')
                return
            self._resume(sid)
        elif self.path == "/api/open_terminal":
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length))
                sid = body.get("id", "")
                no_kitty = bool(body.get("no_kitty"))
            except (ValueError, TypeError):
                self._send(400, "application/json", b'{"error":"bad request"}')
                return
            self._open_terminal(sid, no_kitty)
        else:
            self._send(404, "text/plain", b"not found")

    def _say(self, sid, text):
        """走行中セッションのtmuxペインへ指示テキストを注入(Kitty Ctrl Panelと同じ作法)"""
        def fail(code, msg):
            self._send(code, "application/json; charset=utf-8",
                       json.dumps({"error": msg}, ensure_ascii=False).encode())
        if not text:
            return fail(400, "テキストが空")
        if len(text) > 4000:
            return fail(400, "長すぎ(4000文字まで)")
        proc = proc_map().get(sid)
        if not proc:
            return fail(404, "生きてるプロセスが見つからない")
        code, err = inject_text(proc, text)
        if err:
            return fail(code, err)
        self._send(200, "application/json", b'{"ok": true}')

    def _resume(self, sid):
        def fail(code, msg):
            self._send(code, "application/json; charset=utf-8",
                       json.dumps({"error": msg}, ensure_ascii=False).encode())
        tmux_name, err = start_resume(sid)
        if err:
            code = {"不正なID": 400, "もう起きてる(プロセス生存中)": 409,
                    "セッション記録が見つからない": 404}.get(err, 500)
            return fail(code, err)
        self._send(200, "application/json; charset=utf-8",
                   json.dumps({"ok": True, "tmux": tmux_name}, ensure_ascii=False).encode())

    def _open_terminal(self, sid, no_kitty=False):
        """その子をターミナルに出す: tmux組=新kittyタブでattach / kitty直組=タブへフォーカス /
        冬眠中=resumeしてからattach(ワンクリック蘇生)。
        no_kitty=スマホ用: 母艦のkittyは触らずtmux名だけ返す(POCKET DECK WEBで開く前提)"""
        def fail(code, msg):
            self._send(code, "application/json; charset=utf-8",
                       json.dumps({"error": msg}, ensure_ascii=False).encode())
        if not sid or "/" in sid or ".." in sid:
            return fail(400, "不正なID")
        proc = proc_map().get(sid)
        if no_kitty:
            if proc and proc.get("tmux"):
                body = {"ok": True, "via": "tmux", "tmux": proc["tmux"].split(":")[0]}
            elif proc:
                return fail(409, "この子はtmuxの外(母艦のkittyタブ直)で走ってるので、"
                                 "スマホからは入れない(母艦で開いて)")
            else:
                tmux_name, err = start_resume(sid)
                if err:
                    return fail(500, err)
                body = {"ok": True, "via": "resume", "tmux": tmux_name}
            self._send(200, "application/json; charset=utf-8",
                       json.dumps(body, ensure_ascii=False).encode())
            return
        if proc and proc.get("tmux"):
            sess = proc["tmux"].split(":")[0]
            if not kitty_open_tab(["tmux", "attach", "-t", sess]):
                return fail(500, "kittyを開けなかった")
            body = {"ok": True, "via": "attach", "tmux": sess}
        elif proc:
            sock = kitty_live_sock()
            tab_id = None
            if sock:
                try:
                    r = subprocess.run(["kitten", "@", "--to", f"unix:{sock}", "ls"],
                                       capture_output=True, text=True, timeout=3)
                    for osw in json.loads(r.stdout or "[]"):
                        for tab in osw.get("tabs", []):
                            for w in tab.get("windows", []):
                                if any(fp.get("pid") == proc["pid"]
                                       for fp in w.get("foreground_processes", [])):
                                    tab_id = tab["id"]
                except (OSError, ValueError, subprocess.TimeoutExpired):
                    pass
            if tab_id is None:
                return fail(404, "この子が住んでるkittyタブが見つからない")
            subprocess.run(["kitten", "@", "--to", f"unix:{sock}", "focus-tab",
                            "--match", f"id:{tab_id}"], capture_output=True, timeout=3)
            body = {"ok": True, "via": "focus"}
        else:
            tmux_name, err = start_resume(sid)
            if err:
                return fail(500, err)
            if not kitty_open_tab(["tmux", "attach", "-t", tmux_name]):
                return fail(500, f"復活はした({tmux_name})けどkittyを開けなかった")
            body = {"ok": True, "via": "resume", "tmux": tmux_name}
        self._send(200, "application/json; charset=utf-8",
                   json.dumps(body, ensure_ascii=False).encode())

    def _send(self, code, ctype, body):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    threading.Thread(target=auto_compact_patrol, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
