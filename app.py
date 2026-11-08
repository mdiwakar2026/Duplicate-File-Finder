"""Duplicate Finder - web interface.

Run:  python app.py      -> opens http://127.0.0.1:8765 in your browser.
Runs only on this computer (127.0.0.1); nothing is sent anywhere.
"""
import json
import os
import string
import sys
import threading
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import re
import time
from urllib.parse import parse_qs, unquote, urlparse

import dupfinder as core
import ntfs_fast
try:  # Find similar needs extra packages (see requirements.txt); duplicate finding works without them
    import similar
    SIM_ERROR = None
except ImportError as e:
    similar, SIM_ERROR = None, (f"Find similar needs extra Python packages ({e.name} is missing). "
                                "Close the app and run: python -m pip install -r requirements.txt")

HOST, PORT = "127.0.0.1", 8765
UI_FILE = Path(__file__).resolve().parent / "ui.html"

state = {"busy": False, "task": None, "log": []}
lock = threading.Lock()


def ui_log(*args):
    line = " ".join(str(a) for a in args)
    state["log"].append(f"{datetime.now():%H:%M:%S}  {line}")
    del state["log"][:-500]  # keep last 500 lines
    print(line, flush=True)


core.log = ui_log
core.AUTO_CONFIRM = True  # confirmation happens in the browser dialog before any action is sent


def run_task(name, fn, *args):
    with lock:
        if state["busy"]:
            return False
        state.update(busy=True, task=name, log=[])
        core.PROGRESS.update(phase="", step=0, pct=0.0, detail="", eta=None)

    def worker():
        try:
            fn(*args)
        except Exception as e:  # report, never crash the server
            ui_log(f"ERROR: {e}")
        finally:
            state.update(busy=False)

    threading.Thread(target=worker, daemon=True).start()
    return True


def list_folders(path):
    """Drives (when path is empty) or the sub-folders of path, for the in-page folder browser."""
    if not path:
        drives = [f"{c}:\\" for c in string.ascii_uppercase if os.path.exists(f"{c}:\\")]
        return {"path": "", "parent": None, "folders": [{"name": d, "path": d} for d in drives]}
    p = Path(path)
    if not p.is_dir():
        raise ValueError(f"Folder not found: {path}")
    folders = []
    try:
        for e in os.scandir(p):
            try:
                if e.is_dir() and not e.name.startswith(("$", ".")) and e.name != "System Volume Information":
                    folders.append({"name": e.name, "path": e.path})
            except OSError:
                pass
    except PermissionError:
        raise ValueError(f"No permission to open: {path}")
    folders.sort(key=lambda f: f["name"].lower())
    parent = str(p.parent) if p.parent != p else ""
    return {"path": str(p), "parent": parent, "folders": folders}


def summary():
    con = core.db()
    root = core.get_meta(con, "root")
    groups = core.duplicate_groups(con) if root else []
    last = con.execute("SELECT id, kind, started FROM runs WHERE undone=0 ORDER BY id DESC LIMIT 1").fetchone()
    last_run = None
    if last:
        n = con.execute("SELECT COUNT(*) FROM moves WHERE run_id=? AND status='done'", (last[0],)).fetchone()[0]
        last_run = {"id": last[0], "kind": last[1], "started": last[2], "files": n}
    return {
        "root": root,
        "scanned": core.get_meta(con, "scanned"),
        "groups": [[{"path": p, "size": s, "mtime": datetime.fromtimestamp(m).strftime("%Y-%m-%d %H:%M")}
                    for p, s, m in g] for g in groups[:2000]],  # biggest 2000 shown; full list in report
        "group_count": len(groups),
        "copies": sum(len(g) - 1 for g in groups),
        "waste": sum(g[0][1] * (len(g) - 1) for g in groups),
        "last_run": last_run,
    }


class Server(ThreadingHTTPServer):
    allow_reuse_address = False  # on Windows reuse would let two apps share a port silently
    daemon_threads = True


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        url = urlparse(self.path)
        if url.path.startswith(("/api/thumb", "/api/qthumb", "/api/similar")) and similar is None:
            return self.send(200, {"result": {}}) if url.path == "/api/similar" else self.send(404, {"error": SIM_ERROR})
        if url.path in ("/api/thumb", "/api/qthumb"):  # small previews for the Find similar results
            q = parse_qs(url.query)
            try:
                if url.path == "/api/thumb":
                    img = similar.thumbnail(int(q.get("i", ["-1"])[0]))
                else:
                    img = similar.query_thumbnail(upload_path(q.get("name", [""])[0]))
            except Exception:  # unreadable or damaged file: no preview
                img = None
            return self.send(200, img, "image/jpeg") if img else self.send(404, {"error": "no preview"})
        if url.path == "/api/similar":
            return self.send(200, {"result": similar.RESULT})
        if self.path == "/":
            self.send(200, UI_FILE.read_bytes(), "text/html; charset=utf-8")
        elif self.path == "/api/status":
            self.send(200, {"busy": state["busy"], "task": state["task"], "log": state["log"][-200:],
                            "progress": core.PROGRESS, "admin": ntfs_fast.is_admin(), "now": time.time(),
                            "sim_version": similar.RESULT.get("version", 0) if similar else 0,
                            "sim_error": SIM_ERROR})
        elif self.path == "/api/whoami":
            self.send(200, {"app": "duplicate-finder", "admin": ntfs_fast.is_admin()})
        elif self.path == "/api/summary":
            self.send(200, summary())
        else:
            self.send(404, {"error": "not found"})

    def do_POST(self):
        # Only accept requests from our own page (blocks other websites from triggering moves)
        origin = self.headers.get("Origin", "")
        if origin and origin != f"http://{HOST}:{PORT}":
            return self.send(403, {"error": "forbidden"})
        length = int(self.headers.get("Content-Length", 0) or 0)
        if similar is None and self.path in ("/api/upload", "/api/similar", "/api/collect"):
            self.rfile.read(length)
            return self.send(400, {"error": SIM_ERROR})
        if self.path == "/api/upload":  # file for Find similar; streamed to disk in 1 MB pieces
            return self.receive_upload(length)
        body = json.loads(self.rfile.read(length) or b"{}")
        if self.path == "/api/similar":
            root = body.get("root", "")
            try:
                query = upload_path(body.get("name", ""))
            except ValueError as e:
                return self.send(400, {"error": str(e)})
            if not Path(root).is_dir():
                return self.send(400, {"error": f"Folder not found: {root}"})
            if not run_task("Finding similar", similar.search, query, root):
                return self.send(409, {"error": "Another task is still running."})
            return self.send(200, {"ok": True})
        if self.path == "/api/collect":
            mode = body.get("mode")
            idx = [int(i) for i in body.get("indexes", [])]
            if mode not in ("copy", "move") or not idx:
                return self.send(400, {"error": "Choose files and Copy or Move."})
            if not run_task("Collecting files", similar.collect, idx, mode):
                return self.send(409, {"error": "Another task is still running."})
            return self.send(200, {"ok": True})
        if self.path == "/api/quit":  # close this app (used when a newer copy starts)
            core.STOP["requested"] = True
            self.send(200, {"ok": True})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return
        if self.path == "/api/elevate":  # restart as administrator (Windows asks the user first)
            if state["busy"]:
                return self.send(409, {"error": "Stop the running task first."})
            bat = Path(__file__).resolve().parent / "Start Duplicate Finder.bat"
            try:
                os.startfile(str(bat), "runas")  # UAC prompt; the new copy closes this one
            except OSError:
                return self.send(400, {"error": "Windows did not allow it (the prompt was declined)."})
            return self.send(200, {"ok": True})
        if self.path == "/api/stop":
            core.STOP["requested"] = True
            return self.send(200, {"ok": True})
        if self.path == "/api/folders":
            try:
                return self.send(200, list_folders(body.get("path", "")))
            except ValueError as e:
                return self.send(400, {"error": str(e)})
        actions = {
            "/api/scan": ("Scanning", core.scan, body.get("path", ""), int(body.get("min_size", 100 * 1024))),
            "/api/move": ("Moving duplicates", core.move_duplicates),
            "/api/organize": ("Organizing", core.organize),
            "/api/undo": ("Undoing", core.undo),
            "/api/report": ("Exporting report", core.report),
        }
        if self.path not in actions:
            return self.send(404, {"error": "not found"})
        name, fn, *args = actions[self.path]
        if self.path == "/api/scan" and not Path(args[0]).is_dir():
            return self.send(400, {"error": f"Folder not found: {args[0]}"})
        if not run_task(name, fn, *args):
            return self.send(409, {"error": "Another task is still running."})
        self.send(200, {"ok": True})

    def receive_upload(self, length):
        name = safe_name(unquote(self.headers.get("X-Filename", "")))
        if not similar.kind_of(name):
            self.rfile.read(length)
            return self.send(400, {"error": "Use a photo (jpg, png), a video (mp4, mov) or a document (pdf, docx, txt)."})
        if length > 8 * 1024 ** 3:
            return self.send(400, {"error": "File is larger than 8 GB."})
        similar.UPLOAD_DIR.mkdir(exist_ok=True)
        dst = similar.UPLOAD_DIR / name
        with open(dst, "wb") as f:
            left = length
            while left:
                chunk = self.rfile.read(min(left, 1024 * 1024))
                if not chunk:
                    break
                f.write(chunk)
                left -= len(chunk)
        self.send(200, {"name": name, "kind": similar.kind_of(name), "size": length})


def safe_name(name):
    """Keep only a plain file name (no folders, no characters Windows rejects)."""
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", Path(name).name).strip(" .")
    return name[:150] or "upload"


def upload_path(name):
    p = similar.UPLOAD_DIR / safe_name(name)
    if not name or not p.is_file():
        raise ValueError("Upload a file first.")
    return p



def find_running():
    """Ports where a Duplicate Finder is already running -> {port: is_admin}."""
    import urllib.request
    found = {}
    for port in range(8765, 8800):
        try:
            with urllib.request.urlopen(f"http://{HOST}:{port}/api/whoami", timeout=0.3) as r:
                j = json.loads(r.read())
                if j.get("app") == "duplicate-finder":
                    found[port] = j.get("admin", False)
        except Exception:
            pass
    return found


def is_busy(port):
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://{HOST}:{port}/api/status", timeout=3) as r:
            return bool(json.loads(r.read()).get("busy"))
    except Exception:
        return False


def quit_app(port):
    import urllib.request
    req = urllib.request.Request(f"http://{HOST}:{port}/api/quit", data=b"{}", method="POST",
                                 headers={"Origin": f"http://{HOST}:{port}", "Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=3)
    except Exception:
        pass


if __name__ == "__main__":
    # Only one copy may run (two copies writing the same database caused "database is locked").
    running = find_running()
    busy = [p for p in running if is_busy(p)]
    if busy and "--restart" not in sys.argv:
        print(f"Duplicate Finder is already running a task at http://{HOST}:{busy[0]} - opening it.")
        webbrowser.open(f"http://{HOST}:{busy[0]}")
        sys.exit(0)
    for p in running:  # idle copies: close them so this (newest) copy is the only one
        quit_app(p)
    for _ in range(20):  # wait until the old copy has exited, so this one gets the same address
        if not running or not find_running():
            break
        time.sleep(0.5)
    for PORT in range(8765, 8800):  # use the next free port if 8765 is busy
        try:
            server = Server((HOST, PORT), Handler)
            break
        except OSError:
            continue
    else:
        sys.exit("No free port between 8765 and 8799.")
    url = f"http://{HOST}:{PORT}"
    print(f"Duplicate Finder running at {url}  (close this window to stop)")
    if "--no-browser" not in sys.argv and not running:  # replacing a copy: its open page reconnects
        webbrowser.open(url)
    server.serve_forever()
