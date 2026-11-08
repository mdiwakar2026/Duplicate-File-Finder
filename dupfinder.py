"""Duplicate Finder - finds duplicate files by content and MOVES them (never deletes).

Usage:
    python dupfinder.py                 # interactive menu
    python dupfinder.py scan F:\\        # scan a drive/folder
    python dupfinder.py report          # write CSV + HTML report of last scan
    python dupfinder.py move            # move duplicates to <root>\\_Duplicates (asks confirmation)
    python dupfinder.py organize        # sort kept files into <root>\\_Organized\\<Type>\\<Year> (asks confirmation)
    python dupfinder.py undo            # put back every file moved by the last move/organize run

All state lives in dupfinder.db (SQLite) next to this script.
"""
import csv
import hashlib
import html
import os
import shutil
import sqlite3
import sys
import time
from collections import defaultdict, deque
from datetime import datetime
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
DB_PATH = APP_DIR / "dupfinder.db"
REPORT_DIR = APP_DIR / "reports"
DUP_DIR_NAME = "_Duplicates"
ORG_DIR_NAME = "_Organized"
SKIP_DIRS = {DUP_DIR_NAME, ORG_DIR_NAME, "System Volume Information", "$RECYCLE.BIN", "$Recycle.Bin"}
PARTIAL_BYTES = 64 * 1024
CHUNK = 1024 * 1024
AUTO_CONFIRM = False  # the web UI sets this after the user confirms in its own dialog


def log(*args):
    """Output hook; the web UI replaces this to stream progress to the browser."""
    print(*args, flush=True)

CATEGORIES = {
    "Videos": {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".m4v", ".3gp", ".webm", ".mpg", ".mpeg", ".mts", ".vob"},
    "Images": {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tif", ".tiff", ".heic", ".webp", ".raw", ".cr2", ".nef", ".svg"},
    "Audio": {".mp3", ".wav", ".aac", ".flac", ".m4a", ".wma", ".ogg", ".amr"},
    "Documents": {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".xlsm", ".csv", ".ppt", ".pptx", ".txt", ".rtf",
                  ".odt", ".ods", ".odp", ".md", ".json", ".xml", ".html", ".htm"},
    "Archives": {".zip", ".rar", ".7z", ".tar", ".gz", ".iso"},
}


# ---------------------------------------------------------------- database
def db():
    con = sqlite3.connect(DB_PATH, timeout=60)  # wait for a busy database instead of failing
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript("""
        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS files (
            path TEXT PRIMARY KEY, size INTEGER, mtime REAL, partial TEXT, full TEXT);
        CREATE TABLE IF NOT EXISTS runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, started TEXT, undone INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS moves (
            id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER, src TEXT, dst TEXT,
            status TEXT, error TEXT);
    """)
    return con


def get_meta(con, key):
    row = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def set_meta(con, key, value):
    con.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, value))
    con.commit()


# ---------------------------------------------------------------- hashing
def hash_partial(path, size):
    """Quick fingerprint: first 64 KB only (one disk seek). Files that match here are still
    fully compared in step 3, so this only filters - it never decides a duplicate on its own."""
    with open(path, "rb") as f:
        return hashlib.sha256(f.read(PARTIAL_BYTES)).hexdigest()


def hash_full(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(CHUNK):
            h.update(chunk)
    return h.hexdigest()


def human(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


# ---------------------------------------------------------------- scan
class Cancelled(Exception):
    pass


# Live progress, read by the web UI. pct = overall 0-100, step_pct = current step 0-100.
PROGRESS = {"phase": "", "step": 0, "pct": 0.0, "step_pct": 0.0, "detail": "", "eta": None,
            "started": None, "method": ""}
STOP = {"requested": False}
MIN_SIZE = {"bytes": 100 * 1024}  # files smaller than this are ignored (set from the UI)


class _Rate:
    """Speed over the last 2 minutes only, so a fast start (cached work) doesn't fake the ETA."""

    def __init__(self, window=120):
        self.window, self.samples = window, deque()

    def add(self, value):
        now = time.time()
        self.samples.append((now, value))
        while len(self.samples) > 2 and now - self.samples[0][0] > self.window:
            self.samples.popleft()
        (t0, v0), (t1, v1) = self.samples[0], self.samples[-1]
        return (v1 - v0) / (t1 - t0) if t1 - t0 >= 5 else None  # None = still measuring


def _check_stop():
    if STOP["requested"]:
        raise Cancelled()


def _set_progress(step, phase, pct, detail, eta=None, step_pct=None):
    PROGRESS.update(step=step, phase=phase, pct=round(min(pct, 100), 1), detail=detail, eta=eta,
                    step_pct=round(min(step_pct if step_pct is not None else pct, 100), 1))


def _walk(root, skip=SKIP_DIRS):
    """Standard listing (fallback). Yields (path, size, mtime, disk_position=None)."""
    stack = [str(root)]
    while stack:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            if e.name not in skip:
                                stack.append(e.path)
                        elif e.is_file(follow_symlinks=False):
                            st = e.stat()
                            yield e.path, st.st_size, st.st_mtime, None
                    except OSError:
                        continue
        except OSError:
            continue


def _hash_full_tracked(path, on_bytes):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(CHUNK):
            h.update(chunk)
            on_bytes(len(chunk))
    return h.hexdigest()


def scan(root, min_size=None):
    STOP["requested"] = False
    if min_size is not None:
        MIN_SIZE["bytes"] = int(min_size)
    root = Path(root).resolve()
    if not root.is_dir():
        log(f"Folder not found: {root}")
        return
    con = db()
    con.executescript("""
        CREATE TABLE IF NOT EXISTS hash_cache (path TEXT PRIMARY KEY, size INTEGER, mtime REAL, full TEXT);
        CREATE TABLE IF NOT EXISTS partial_cache2 (path TEXT PRIMARY KEY, size INTEGER, mtime REAL, partial TEXT);
    """)
    t0 = time.time()
    PROGRESS.update(started=t0)
    try:
        _scan(con, root)
        log(f"Finished in {time.strftime('%H:%M:%S', time.gmtime(time.time() - t0))}.")
    except Cancelled:
        con.commit()
        _set_progress(0, "Stopped", PROGRESS["pct"], "Scan stopped. Finished work is saved, so the next scan resumes quickly.",
                      step_pct=PROGRESS["step_pct"])
        log("Scan stopped by user. Finished work is saved; scanning again will skip it.")


def _list(root, weight, label="Step 1 of 3", skip=None):
    """Step 1. Fast MFT read when possible, otherwise the standard folder walk."""
    skip = SKIP_DIRS if skip is None else skip
    try:
        import ntfs_fast

        def prog(done, total, nfiles):
            f = done / total if total else 0
            _set_progress(1, f"{label} - Reading the drive's file table", weight * f,
                          f"{nfiles:,} files found", step_pct=100 * f)

        gen = ntfs_fast.list_files(root, skip, prog, _check_stop)
        first = next(gen, None)  # raises NotAvailable here if the fast path can't be used
        PROGRESS["method"] = "fast"
        log("  Fast mode: read the NTFS file table directly.")
        if first:
            yield first
        yield from gen
        return
    except Cancelled:
        raise
    except Exception as e:  # NotAvailable or anything unexpected -> safe fallback
        PROGRESS["method"] = "standard"
        log(f"  Standard mode ({e}). Tip: start the app as administrator for much faster scans of whole drives.")
    expected = shutil.disk_usage(root).used if root.parent == root else 0
    seen, n = 0, 0
    for e in _walk(root, skip):
        seen += e[1]
        n += 1
        if n % 2000 == 0:
            _check_stop()
            f = min(seen / expected, 0.99) if expected else 0
            _set_progress(1, f"{label} - Listing files", weight * f, f"{n:,} files found ({human(seen)})",
                          step_pct=100 * f)
        yield e


def _scan(con, root):
    log(f"Scanning {root} ... " + (f"(ignoring files smaller than {human(MIN_SIZE['bytes'])})" if MIN_SIZE['bytes'] else "(checking every file)"))
    W1, W2 = 5, 30  # % of the overall bar for step 1 and 2; step 3 gets the rest

    # Step 1: list files, keep only sizes worth comparing
    by_size, total = defaultdict(list), 0
    for p, size, mtime, pos in _list(root, W1):
        total += 1
        if size >= max(MIN_SIZE["bytes"], 1):
            by_size[size].append((p, size, mtime, pos or 0))
    candidates = [e for grp in by_size.values() if len(grp) > 1 for e in grp]
    del by_size
    candidates.sort(key=lambda e: (abs(e[3]), e[0]))  # read in on-disk order: far fewer seeks on a hard disk
    log(f"  {total:,} files found; {len(candidates):,} share a size with another file -> checking content")

    # Step 2: quick check (first + last 64 KB), cached so a stopped scan resumes
    pcache = {r[0]: r[1:] for r in con.execute("SELECT path, size, mtime, partial FROM partial_cache2")}
    by_partial, errors, t2 = defaultdict(list), 0, time.time()
    n = len(candidates) or 1
    todo2 = []
    for e in candidates:
        c = pcache.get(e[0])
        if c and c[0] == e[1] and round(c[1], 3) == round(e[2], 3):
            by_partial[(e[1], c[2])].append(e)  # done in an earlier (stopped) scan
        else:
            todo2.append(e)
    done0 = len(candidates) - len(todo2)

    def work(e):
        try:
            return e, hash_partial(e[0], e[1])
        except OSError:
            return e, None

    # Fast mode: read each file's first 64 KB straight from the disk in one sweep (disk order).
    # Otherwise open files one at a time: parallel opens made a USB hard disk thrash.
    raw = None
    if PROGRESS["method"] == "fast" and any(e[3] > 0 for e in todo2):
        try:
            import ntfs_fast
            raw = ntfs_fast.RawReader()
            log("  Quick check reads straight from the disk (no folder lookups).")
        except OSError:
            raw = None

    def results():
        for e in todo2:
            if raw and e[3] > 0:
                try:
                    yield e, hashlib.sha256(raw.read_head(e[3], e[1])).hexdigest()
                    continue
                except OSError:
                    pass
            yield work(e)

    rate2 = _Rate()
    rate2.add(done0)
    try:
        for i, (e, ph) in enumerate(results(), done0 + 1):
            if ph is None:
                errors += 1
            else:
                by_partial[(e[1], ph)].append(e)
                con.execute("INSERT OR REPLACE INTO partial_cache2 VALUES (?,?,?,?)", (e[0], e[1], e[2], ph))
            if i % 200 == 0 or i == len(candidates):
                con.commit()
                _check_stop()
                rate = rate2.add(i)
                _set_progress(2, "Step 2 of 3 - Quick check of same-size files", W1 + W2 * i / n,
                              f"{i:,} / {len(candidates):,} files" + (f" ({rate:,.0f} files/s)" if rate else ""),
                              int((n - i) / rate) if rate else None, 100 * i / n)
    finally:
        if raw:
            raw.close()
    del pcache, candidates

    # Step 3: full compare of files that still match, in on-disk order, with % by bytes and ETA
    todo = sorted((r for grp in by_partial.values() if len(grp) > 1 for r in grp), key=lambda e: (abs(e[3]), e[0]))
    ph_of = {r[0]: ph for (size, ph), grp in by_partial.items() for r in grp}
    del by_partial
    cache = {r[0]: r[1:] for r in con.execute("SELECT path, size, mtime, full FROM hash_cache")}

    def cached(p, size, mtime):
        c = cache.get(p)
        return c[2] if c and c[0] == size and round(c[1], 3) == round(mtime, 3) else None

    total_bytes = sum(r[1] for r in todo if r[1] > PARTIAL_BYTES and not cached(*r[:3])) or 1
    log(f"  {len(todo):,} files need a full comparison ({human(total_bytes)} to read)")
    done_bytes, rows, rate3 = 0, [], _Rate()
    rate3.add(0)
    W3 = 100 - W1 - W2

    def on_bytes(nb, force=False):
        nonlocal done_bytes
        done_bytes += nb
        _check_stop()
        if force or done_bytes % (64 * CHUNK) < nb:  # update roughly every 64 MB
            rate = rate3.add(done_bytes)
            f = done_bytes / total_bytes
            _set_progress(3, "Step 3 of 3 - Comparing full file contents", W1 + W2 + W3 * f,
                          f"{human(done_bytes)} / {human(total_bytes)} read" + (f" ({human(rate)}/s)" if rate else ""),
                          int((total_bytes - done_bytes) / rate) if rate and rate > 1 else None, 100 * f)

    for i, (p, size, mtime, pos) in enumerate(todo, 1):
        ph = ph_of[p]
        try:
            if size <= PARTIAL_BYTES:
                fh = ph  # small file: quick check already read the whole file
            elif (fh := cached(p, size, mtime)) is None:
                fh = _hash_full_tracked(p, on_bytes)
                con.execute("INSERT OR REPLACE INTO hash_cache VALUES (?,?,?,?)", (p, size, mtime, fh))
            rows.append((p, size, mtime, ph, fh))
        except OSError:
            errors += 1
        if i % 50 == 0:
            con.commit()
            on_bytes(0, force=True)

    # save results only when the scan completed
    con.execute("DELETE FROM files")
    con.executemany("INSERT OR REPLACE INTO files VALUES (?,?,?,?,?)", rows)
    set_meta(con, "root", str(root))
    set_meta(con, "scanned", datetime.now().isoformat(timespec="seconds"))
    con.commit()
    groups = duplicate_groups(con)
    waste = sum(g[0][1] * (len(g) - 1) for g in groups)
    _set_progress(0, "Scan complete", 100, f"{len(groups):,} duplicate groups, {human(waste)} can be freed",
                  step_pct=100)
    log(f"Done. {len(groups)} duplicate groups, {sum(len(g) - 1 for g in groups)} extra copies, "
        f"{human(waste)} can be freed. ({errors} files could not be read)")
    log("Next: review the list, then 'Move duplicates'.")


def duplicate_groups(con):
    """List of groups; each group sorted so index 0 is the KEEPER (oldest, then shortest path)."""
    by_hash = defaultdict(list)
    moved = {r[0] for r in con.execute("SELECT src FROM moves WHERE status='done'")}  # moved away by us
    for path, size, mtime, full in con.execute("SELECT path, size, mtime, full FROM files"):
        if path in moved:
            continue
        by_hash[(size, full)].append((path, size, mtime))
    groups = []
    for grp in by_hash.values():
        if len(grp) > 1:
            grp.sort(key=lambda r: (r[2], len(r[0]), r[0]))
            groups.append(grp)
    groups.sort(key=lambda g: -g[0][1] * (len(g) - 1))
    return groups


# ---------------------------------------------------------------- report
def report():
    con = db()
    root = get_meta(con, "root")
    if not root:
        log("Run 'scan' first.")
        return
    groups = duplicate_groups(con)
    REPORT_DIR.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path = REPORT_DIR / f"duplicates_{stamp}.csv"
    html_path = REPORT_DIR / f"duplicates_{stamp}.html"

    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["Group", "Action", "Path", "Size", "Modified"])
        for i, g in enumerate(groups, 1):
            for j, (p, size, mtime) in enumerate(g):
                w.writerow([i, "KEEP" if j == 0 else "MOVE", p, size,
                            datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")])

    waste = sum(g[0][1] * (len(g) - 1) for g in groups)
    parts = [f"<html><head><meta charset='utf-8'><title>Duplicate report</title><style>"
             "body{font-family:Segoe UI,Arial;margin:24px;color:#222}"
             "table{border-collapse:collapse;width:100%;margin-bottom:18px}"
             "td,th{border:1px solid #ddd;padding:4px 8px;font-size:13px;text-align:left}"
             ".keep{background:#e7f6e7}.move{background:#fdf0e6}h3{margin:18px 0 4px}"
             "</style></head><body>",
             f"<h1>Duplicate report</h1><p>Scanned: <b>{html.escape(root)}</b> at {get_meta(con, 'scanned')}<br>"
             f"{len(groups)} groups &middot; {sum(len(g) - 1 for g in groups)} copies to move &middot; "
             f"<b>{human(waste)}</b> to free</p>"]
    for i, g in enumerate(groups, 1):
        parts.append(f"<h3>Group {i} &middot; {human(g[0][1])} each &middot; {len(g)} copies</h3>"
                     "<table><tr><th>Action</th><th>Path</th><th>Modified</th></tr>")
        for j, (p, size, mtime) in enumerate(g):
            cls, act = ("keep", "KEEP") if j == 0 else ("move", "MOVE")
            parts.append(f"<tr class='{cls}'><td>{act}</td><td>{html.escape(p)}</td>"
                         f"<td>{datetime.fromtimestamp(mtime):%Y-%m-%d %H:%M}</td></tr>")
        parts.append("</table>")
    parts.append("</body></html>")
    html_path.write_text("".join(parts), encoding="utf-8")
    log(f"Report written:\n  {html_path}\n  {csv_path}")
    try:
        os.startfile(html_path)  # opens in browser on Windows
    except (AttributeError, OSError):
        pass


# ---------------------------------------------------------------- moving
def unique_dest(dst):
    dst = Path(dst)
    if not dst.exists():
        return dst
    n = 1
    while True:
        cand = dst.with_name(f"{dst.stem} ({n}){dst.suffix}")
        if not cand.exists():
            return cand
        n += 1


def confirm(word, message):
    log(message)
    if AUTO_CONFIRM:
        return True
    return input(f"Type {word} to continue (anything else cancels): ").strip() == word


def execute_moves(con, kind, plan):
    """plan: list of (src, dst). Every move is logged BEFORE it happens (checkpoint)."""
    cur = con.execute("INSERT INTO runs (kind, started) VALUES (?,?)",
                      (kind, datetime.now().isoformat(timespec="seconds")))
    run_id = cur.lastrowid
    con.commit()
    ok = fail = 0
    STOP["requested"] = False
    p0, span = PROGRESS["pct"], 100 - PROGRESS["pct"]
    rate = _Rate()
    rate.add(0)
    for i, (src, dst) in enumerate(plan, 1):
        if STOP["requested"]:  # stop between files: everything moved so far is logged and undoable
            log(f"Stopped by user after {ok} files. 'Undo' puts them back.")
            break
        dst = unique_dest(dst)
        mid = con.execute("INSERT INTO moves (run_id, src, dst, status) VALUES (?,?,?,'pending')",
                          (run_id, src, str(dst))).lastrowid
        con.commit()
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(src, dst)
            con.execute("UPDATE moves SET status='done' WHERE id=?", (mid,))
            ok += 1
        except OSError as e:
            con.execute("UPDATE moves SET status='failed', error=? WHERE id=?", (str(e), mid))
            fail += 1
        con.commit()
        if i % 20 == 0 or i == len(plan):
            r = rate.add(i)
            _set_progress(2, f"Moving files ({kind})", p0 + span * i / len(plan),
                          f"{i:,} / {len(plan):,} files moved" + (f" ({r:,.0f}/s)" if r else ""),
                          int((len(plan) - i) / r) if r else None, 100 * i / len(plan))
        if i % 1000 == 0:
            log(f"  {i:,}/{len(plan):,}")
    _set_progress(0, "Done", 100, f"Moved {ok:,} files, {fail:,} failed.", step_pct=100)
    log(f"Moved {ok} files, {fail} failed. (run #{run_id}; 'undo' reverses it)")


def move_duplicates():
    PROGRESS.update(started=time.time())
    con = db()
    root = get_meta(con, "root")
    if not root:
        log("Run 'scan' first.")
        return
    dup_root = Path(root) / DUP_DIR_NAME
    plan, skipped = [], 0
    for g in duplicate_groups(con):
        for p, size, mtime in g[1:]:
            # re-check the file is unchanged since the scan
            try:
                if os.path.getsize(p) != size:
                    skipped += 1
                    continue
            except OSError:
                skipped += 1
                continue
            plan.append((p, dup_root / Path(p).relative_to(root)))
    if not plan:
        log("No duplicates to move.")
        return
    total = sum(os.path.getsize(s) for s, _ in plan)
    if skipped:
        log(f"{skipped} files changed or vanished since scan and will be skipped.")
    if confirm("MOVE", f"{len(plan)} duplicate files ({human(total)}) will be MOVED to\n  {dup_root}\n"
                       "Original folder structure is kept inside. Nothing is deleted."):
        execute_moves(con, "duplicates", plan)
    else:
        log("Cancelled. Nothing moved.")


def category(path):
    ext = Path(path).suffix.lower()
    for cat, exts in CATEGORIES.items():
        if ext in exts:
            return cat
    return "Others"


# Folders whose files belong together (software, code projects, system). Moving single files out of
# them would break programs, so Organize leaves them untouched.
KEEP_TOGETHER = {"windows", "program files", "program files (x86)", "programdata", "appdata", "node_modules",
                 "__pycache__", "venv", ".venv", "env", "site-packages", ".git", ".svn", ".idea", ".vscode"}


def _organizable(rel_parts):
    for part in rel_parts[:-1]:
        low = part.lower()
        if low in KEEP_TOGETHER or part.startswith((".", "$")):
            return False
    return not rel_parts[-1].startswith((".", "~$"))  # hidden and Office temp files stay


def organize():
    """Moves photos, videos, audio, documents and archives into _Organized\\<Type>\\<Year>.
    Other files (programs, project folders, unknown types) stay where they are."""
    STOP["requested"] = False
    PROGRESS.update(started=time.time(), method="")
    con = db()
    root = get_meta(con, "root")
    if not root:
        log("Run 'scan' first.")
        return
    root = Path(root)
    org_root = root / ORG_DIR_NAME
    log(f"Listing files in {root} ...")
    plan, left = [], 0
    try:
        for p, size, mtime, _pos in _list(root, 30, "Organize"):
            if category(p) == "Others":
                left += 1
                continue
            parts = Path(p).relative_to(root).parts
            if not _organizable(parts):
                left += 1
                continue
            try:
                year = datetime.fromtimestamp(mtime).year
            except (OSError, ValueError, OverflowError):
                year = "Unknown year"
            plan.append((p, org_root / category(p) / str(year) / parts[-1]))
    except Cancelled:
        _set_progress(0, "Stopped", PROGRESS["pct"], "Organize stopped before anything was moved.")
        log("Stopped. Nothing moved.")
        return
    if not plan:
        _set_progress(0, "Done", 100, "Nothing to organize.")
        log("Nothing to organize.")
        return
    counts = defaultdict(int)
    for _, d in plan:
        counts[d.parent.parent.name] += 1
    summary = "\n".join(f"  {k}: {v:,}" for k, v in sorted(counts.items()))
    if confirm("ORGANIZE", f"{len(plan):,} files will be MOVED into {org_root}\\<Type>\\<Year>\\:\n{summary}\n"
                           f"{left:,} other files (programs, project folders, unknown types) stay where they are.\n"
                           "Same-name files get ' (1)', ' (2)' suffixes. Nothing is deleted."):
        execute_moves(con, "organize", plan)
    else:
        log("Cancelled. Nothing moved.")


def undo():
    con = db()
    run = con.execute("SELECT id, kind, started FROM runs WHERE undone=0 ORDER BY id DESC LIMIT 1").fetchone()
    if not run:
        log("Nothing to undo.")
        return
    run_id, kind, started = run
    moves = con.execute("SELECT id, src, dst FROM moves WHERE run_id=? AND status='done' ORDER BY id DESC",
                        (run_id,)).fetchall()
    if not confirm("UNDO", f"Run #{run_id} ({kind}, {started}): {len(moves)} files will be moved back."):
        log("Cancelled.")
        return
    ok = fail = 0
    for mid, src, dst in moves:
        try:
            if os.path.exists(src):
                raise OSError("original location is occupied")
            Path(src).parent.mkdir(parents=True, exist_ok=True)
            shutil.move(dst, src)
            con.execute("UPDATE moves SET status='undone' WHERE id=?", (mid,))
            ok += 1
        except OSError as e:
            con.execute("UPDATE moves SET error=? WHERE id=?", (f"undo failed: {e}", mid))
            fail += 1
        con.commit()
    if fail == 0:
        con.execute("UPDATE runs SET undone=1 WHERE id=?", (run_id,))
        con.commit()
    log(f"Restored {ok} files, {fail} failed." + (" Run 'undo' again to retry." if fail else ""))


# ---------------------------------------------------------------- menu
def menu():
    actions = {"1": ("Scan a drive / folder", None), "2": ("Open report", report),
               "3": ("Move duplicates to _Duplicates", move_duplicates),
               "4": ("Organize kept files by Type/Year", organize), "5": ("Undo last move", undo),
               "0": ("Exit", None)}
    while True:
        con = db()
        root = get_meta(con, "root")
        log("\n=== Duplicate Finder ===" + (f"   (last scan: {root})" if root else ""))
        for k, (label, _) in actions.items():
            log(f" {k}. {label}")
        choice = input("Choose: ").strip()
        if choice == "0":
            break
        if choice == "1":
            scan(input("Folder or drive to scan (e.g. F:\\): ").strip().strip('"'))
        elif choice in actions:
            actions[choice][1]()


if __name__ == "__main__":
    cmds = {"report": report, "move": move_duplicates, "organize": organize, "undo": undo}
    if len(sys.argv) == 1:
        menu()
    elif sys.argv[1] == "scan" and len(sys.argv) > 2:
        scan(sys.argv[2])
    elif sys.argv[1] in cmds:
        cmds[sys.argv[1]]()
    else:
        log(__doc__)
