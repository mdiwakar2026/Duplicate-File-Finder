"""Find Similar - upload a photo, video or document and find related files in a folder.

Everything runs on this computer (OpenCV face models in models\\, no internet, no API keys).
  Photo with a face  -> photos (and video frames) showing the same person   (SFace face match)
  Any photo          -> visually similar photos, incl. resized/edited copies (perceptual hash)
  Video              -> videos with the same scenes + photos of those scenes (frame hashes)
  Word / PDF / text  -> documents with similar content                       (keyword similarity)

What is learned about each file is cached in dupfinder.db, so only new or changed files are read
again: the first search of a big drive is slow, later searches take seconds to minutes.
Results are only shown; nothing is copied or moved until the user approves it in the app.
"""
import json
import math
import os
import re
import shutil
import threading
import time
from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

os.environ.setdefault("OPENCV_LOG_LEVEL", "ERROR")  # hide OpenCV 5 info warnings
import cv2
import numpy as np

import dupfinder as core

MODEL_DIR = Path(__file__).resolve().parent / "models"
UPLOAD_DIR = Path(__file__).resolve().parent / "uploads"
FOUND_DIR_NAME = "_Found"
core.SKIP_DIRS.add(FOUND_DIR_NAME)  # never search inside our own results folder

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tif", ".tiff"}
VIDEO_EXT = {".mp4", ".mov", ".m4v", ".mkv", ".avi", ".3gp", ".wmv", ".mpg", ".mpeg", ".mts"}
DOC_EXT = {".pdf", ".docx", ".txt", ".md", ".csv", ".rtf", ".log"}

MIN_IMAGE_BYTES = 20 * 1024     # smaller images are icons/thumbnails
FACE_MATCH = 0.40               # SFace cosine score; OpenCV's suggested same-person threshold is 0.363
SURE_FACE = 0.50                # below this a face match is shown as "possible" and not pre-selected
HASH_MATCH = 10                 # max differing bits (of 64) for "visually similar"
DOC_MATCH = 0.30                # keyword similarity 0..1
DETECT_SIDE = 1024              # faces are detected on a copy scaled to this size
VIDEO_FRAMES = 12               # frames sampled per video
MAX_RESULTS = 2000
REF_FACES = 20                  # confident matches reused as extra reference faces (second pass)
WORKERS = 4

# Last search, shown in the app: {"query", "kind", "root", "results": [...], "note"}
RESULT = {}
CTX = {}  # current search: how to compare against what has been read so far (for Stop / early results)
UNREADABLE = []  # files skipped in the current search (shown as a warning)
_local = threading.local()


# ---------------------------------------------------------------- helpers
def kind_of(path):
    ext = Path(path).suffix.lower()
    if ext in IMAGE_EXT:
        return "image"
    if ext in VIDEO_EXT:
        return "video"
    if ext in DOC_EXT:
        return "doc"
    return None


def _db():
    con = core.db()
    con.executescript("""
        CREATE TABLE IF NOT EXISTS img_index (path TEXT PRIMARY KEY, size INTEGER, mtime REAL, hash TEXT, faces BLOB);
        CREATE TABLE IF NOT EXISTS vid_index (path TEXT PRIMARY KEY, size INTEGER, mtime REAL, hashes TEXT,
                                              faces BLOB, faces_done INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS doc_index (path TEXT PRIMARY KEY, size INTEGER, mtime REAL, terms TEXT);
    """)
    return con


MODEL_URLS = {  # OpenCV Model Zoo (Apache 2.0); downloaded once on the first face search
    "face_detection_yunet_2023mar.onnx":
        "https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx",
    "face_recognition_sface_2021dec.onnx":
        "https://github.com/opencv/opencv_zoo/raw/main/models/face_recognition_sface/face_recognition_sface_2021dec.onnx",
}
FACES = {"ok": None}  # None = not checked yet, True = models ready, False = face search unavailable


def ensure_models():
    """Download the two face model files if they are missing. Returns True when face search can run."""
    if FACES["ok"] is not None:
        return FACES["ok"]
    import urllib.request
    try:
        MODEL_DIR.mkdir(exist_ok=True)
        for name, url in MODEL_URLS.items():
            dst = MODEL_DIR / name
            if dst.exists() and dst.stat().st_size > 100_000:
                continue
            core.log(f"  Downloading face model {name} (one time only)...")
            tmp = dst.with_suffix(".part")
            urllib.request.urlretrieve(url, tmp)
            tmp.replace(dst)
        FACES["ok"] = True
    except Exception as e:
        core.log(f"  Face model download failed: {e}")
        FACES["ok"] = None  # try again on the next search
        return False
    return True


def _models():
    """One detector + recognizer per worker thread (OpenCV models are not thread-safe)."""
    if not hasattr(_local, "det"):
        _local.det = cv2.FaceDetectorYN.create(str(MODEL_DIR / "face_detection_yunet_2023mar.onnx"), "", (320, 320),
                                               0.85, 0.3, 5000)
        _local.rec = cv2.FaceRecognizerSF.create(str(MODEL_DIR / "face_recognition_sface_2021dec.onnx"), "")
    return _local.det, _local.rec


def decode(data):
    """Decode image bytes. Big JPEGs are decoded at half size: much faster, still plenty for faces."""
    flag = cv2.IMREAD_REDUCED_COLOR_2 if len(data) > 3_000_000 else cv2.IMREAD_COLOR
    return cv2.imdecode(data, flag)


def read_image(path):
    return decode(np.fromfile(path, dtype=np.uint8))  # np.fromfile works with non-English paths


def dhash(img):
    """64-bit perceptual fingerprint: survives resizing, compression and small edits."""
    g = cv2.resize(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY), (9, 8), interpolation=cv2.INTER_AREA)
    bits = (g[:, 1:] > g[:, :-1]).flatten()
    return int("".join("1" if b else "0" for b in bits), 2)


def _hamming(a, b):
    return bin(a ^ b).count("1")


def faces_of(img):
    """128-number face fingerprints (unit length) for every clear face in the image."""
    if not FACES["ok"]:
        return np.zeros((0, 128), dtype=np.float16)
    det, rec = _models()
    h, w = img.shape[:2]
    scale = min(1.0, DETECT_SIDE / max(h, w))
    small = cv2.resize(img, (int(w * scale), int(h * scale))) if scale < 1 else img
    det.setInputSize((small.shape[1], small.shape[0]))
    _, found = det.detect(small)
    feats = []
    for f in (found if found is not None else []):
        if min(f[2], f[3]) < 40:  # face too small to recognise reliably
            continue
        feat = rec.feature(rec.alignCrop(small, f)).flatten()
        feats.append(feat / (np.linalg.norm(feat) + 1e-9))
    return np.array(feats, dtype=np.float16).reshape(-1, 128)


def video_frames(path, n=VIDEO_FRAMES):
    """n frames spread evenly over the video (skipping the first/last 5%)."""
    cap = cv2.VideoCapture(str(path))
    try:
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        if total <= 0:
            return []
        frames = []
        for i in range(n):
            cap.set(cv2.CAP_PROP_POS_FRAMES, int(total * (0.05 + 0.9 * i / max(n - 1, 1))))
            ok, frame = cap.read()
            if ok and frame is not None:
                frames.append(frame)
        return frames
    finally:
        cap.release()


# ---------------------------------------------------------------- documents
_WORD = re.compile(r"[a-z][a-z0-9]{2,}|\d{4,}")  # words of 3+ letters, and numbers like invoice/years


def doc_text(path, data=None):
    import io
    ext = Path(path).suffix.lower()
    if data is None:
        with open(path, "rb") as f:
            data = f.read()
    if ext == ".pdf":
        import pymupdf as fitz  # PyMuPDF (the old 'fitz' import name is deprecated)
        with fitz.open(stream=data, filetype="pdf") as pdf:
            if pdf.needs_pass and not pdf.authenticate(""):
                return ""  # password-protected: its text can't be read
            return "\n".join(pdf[i].get_text() for i in range(min(len(pdf), 30)))
    if ext == ".docx":
        import docx
        d = docx.Document(io.BytesIO(data))
        parts = [p.text for p in d.paragraphs]
        for t in d.tables:
            for row in t.rows:
                parts.extend(c.text for c in row.cells)
        return "\n".join(parts)
    return data[:2_000_000].decode("utf-8", errors="ignore")


STOP_WORDS = set("""about above after again against all also although always among and another any are around
because been before being below between both but can cannot could did does doing done down during each either else
even ever every few for from further had has have having her here hers herself him himself his how however into its
itself just least less many may might more most much must myself neither never nor not now off once one only onto
other others otherwise our ours ourselves out over own per perhaps please rather same see seem seemed seems several
she should since some still such than that the their theirs them themselves then there therefore these they this
those though through thus too toward towards under until upon very via was well were what whatever when whenever
where whereas wherever whether which while who whoever whole whom whose why will with within without would yet you
your yours yourself yourselves""".split())


def terms_of(text):
    """The 150 most frequent meaningful words of a document."""
    words = [w for w in _WORD.findall(text.lower()) if w not in STOP_WORDS]
    return dict(Counter(words).most_common(150))


# ---------------------------------------------------------------- indexing
def _index(con, table, files, analyse, label, weight0, weight, force=()):
    """Analyse new/changed files and cache the result. files: [(path, size, mtime, pos)].
    Files are read one at a time in disk order (parallel reads make a USB hard disk thrash);
    the CPU work (decoding, face model, text) runs in parallel."""
    known = {p: (s, m) for p, s, m in con.execute(f"SELECT path, size, mtime FROM {table}")}
    todo = [e for e in files if known.get(e[0]) != (e[1], e[2]) or e[0] in force]
    todo.sort(key=lambda e: (abs(e[3] or 0), e[0]))  # disk order: less seeking on a hard disk
    n = len(todo)
    core.log(f"  {label}: {len(files):,} files, {len(files) - n:,} already known, {n:,} to read.")
    if not n:
        return
    rate, done, pending = core._Rate(), 0, deque()
    rate.add(0)

    def safe(e, data):
        try:
            return e, analyse(e[0], data)
        except Exception:  # unreadable or damaged file: remember it as empty so it isn't retried
            return e, None

    def save(fut):
        nonlocal done
        e, row = fut.result()
        row = row or analyse.empty
        con.execute(f"INSERT OR REPLACE INTO {table} VALUES (?,?,?,{','.join('?' * len(row))})", (e[0], e[1], e[2], *row))
        done += 1
        if done % 32 == 0 or done == n:
            con.commit()
            r = rate.add(done)
            core._set_progress(1, f"Reading {label}", weight0 + weight * done / n,
                               f"{done:,} / {n:,} files" + (f" ({r:,.1f}/s)" if r else ""),
                               int((n - done) / r) if r else None, 100 * done / n)

    workers = WORKERS if analyse.reads_bytes else 1
    unreadable = []
    with ThreadPoolExecutor(workers) as ex:
        try:
            for e in todo:
                core._check_stop()
                data = None
                try:
                    if analyse.reads_bytes:
                        data = np.fromfile(e[0], dtype=np.uint8) if e[1] <= 300_000_000 else None
                        head = data[:65536] if data is not None else b""
                    else:
                        with open(e[0], "rb") as f:
                            head = f.read(65536)
                    if len(head) and not any(head):
                        raise OSError("contents not available")  # cloud placeholder that returns zeros
                except OSError:
                    # Not cached, so it is tried again next time. Typical cause: a OneDrive
                    # "online-only" file while OneDrive isn't running (it can't be downloaded).
                    unreadable.append(e[0])
                    done += 1
                    continue
                pending.append(ex.submit(safe, e, data))
                while len(pending) > workers * 2:
                    save(pending.popleft())
            while pending:
                save(pending.popleft())
        finally:
            for f in pending:
                f.cancel()
            con.commit()
            if unreadable:
                UNREADABLE.extend(unreadable)
                core.log(f"  WARNING: {len(unreadable):,} {label} could not be read and were skipped, "
                         f"e.g. {unreadable[0]}")


def _analyse_image(path, data):
    img = decode(data) if data is not None else None
    if img is None:
        return None
    return (format(dhash(img), "x"), faces_of(img).tobytes())


_analyse_image.empty, _analyse_image.reads_bytes = ("", b""), True


def _make_video_analyser(with_faces):
    def analyse(path, _data):
        frames = video_frames(path)
        hashes = ",".join(format(dhash(f), "x") for f in frames)
        faces = np.concatenate([faces_of(f) for f in frames]) if with_faces and frames else np.zeros((0, 128), np.float16)
        return (hashes, faces.tobytes(), int(with_faces))
    analyse.empty, analyse.reads_bytes = ("", b"", int(with_faces)), False
    return analyse


def _analyse_doc(path, data):
    return (json.dumps(terms_of(doc_text(path, data.tobytes() if data is not None else None))),)


_analyse_doc.empty, _analyse_doc.reads_bytes = ("{}",), True


# ---------------------------------------------------------------- search
def search(query_path, root):
    core.STOP["requested"] = False
    core.PROGRESS.update(started=time.time(), method="")
    RESULT.clear()
    UNREADABLE.clear()
    query_path, root = Path(query_path), Path(root)
    kind = kind_of(query_path)
    con = _db()
    CTX.clear()
    try:
        _search(con, query_path, root, kind)
    except core.Cancelled:
        con.commit()
        core.log("Stopped. Files read so far are remembered, so the next search is faster.")
        if CTX.get("match"):  # still show what can be found among the files read so far
            _publish(query_path, kind, root, CTX["match"](),
                     "Stopped early: only the files read so far were compared. Search again to continue.")
        core._set_progress(0, "Stopped", core.PROGRESS["pct"], "Stopped. Results so far are shown; the next search continues where this one stopped.")


def _publish(query, kind, root, results, note=""):
    """Make results visible in the app (also used for early/partial results)."""
    results = [r for r in results if Path(r["path"]) != query]
    results.sort(key=lambda r: -r["score"])
    if UNREADABLE:
        cloud = sum(1 for p in UNREADABLE if "onedrive" in p.lower())
        note += (f" {len(UNREADABLE):,} files could not be read, so they were not compared. "
                 + ("Most are OneDrive online-only files: start OneDrive (or right-click the folder > "
                    "'Always keep on this device'), then search again. " if cloud >= len(UNREADABLE) / 2 else "")
                 + "They will be tried again next time.")
    RESULT.update(query=query.name, kind=kind, root=str(root), results=results[:MAX_RESULTS], note=note.strip(),
                  version=RESULT.get("version", 0) + 1)
    return results


def _search(con, query, root, kind):
    core.log(f"Looking for files similar to {query.name} in {root}")
    # 1. Understand the uploaded file
    q_faces, q_hashes, q_terms = np.zeros((0, 128), np.float32), [], {}
    if kind in ("image", "video") and not ensure_models():
        # never read photos without the face model: they would be remembered without their faces
        return core.log("ERROR: the face models are needed for photo/video search and could not be downloaded. "
                        "Connect to the internet and search again.")
    if kind == "image":
        img = read_image(str(query))
        if img is None:
            return core.log("ERROR: could not open this image.")
        q_hashes = [dhash(img)]
        q_faces = faces_of(img).astype(np.float32)
        if len(q_faces) > 1:  # several people: search for the most prominent (largest) face
            core.log(f"  {len(q_faces)} faces found in the photo; searching for the first (largest) one.")
        q_faces = q_faces[:1]
        core.log("  Face found: searching for the same person." if len(q_faces) else
                 "  No face found: searching for visually similar pictures.")
    elif kind == "video":
        q_hashes = [dhash(f) for f in video_frames(query)]
        if not q_hashes:
            return core.log("ERROR: could not read this video.")
        core.log(f"  Took {len(q_hashes)} sample frames from the video.")
    elif kind == "doc":
        try:
            q_terms = terms_of(doc_text(str(query)))
        except Exception as e:
            return core.log(f"ERROR: could not read this document ({e}).")
        if not q_terms:
            return core.log("ERROR: no readable text in this document. It may be password-protected or a scanned image "
                            "(scanned documents are not supported yet).")
        core.log("  Key words: " + ", ".join(list(q_terms)[:12]))
    else:
        return core.log("ERROR: unsupported file type.")

    # 2. List the folder (fast NTFS read when running as administrator)
    want = DOC_EXT if kind == "doc" else IMAGE_EXT | VIDEO_EXT
    files = {"image": [], "video": [], "doc": []}
    for p, size, mtime, pos in core._list(root, 10, "Find similar", core.SKIP_DIRS - {core.ORG_DIR_NAME}):  # organized files are searched too
        ext = os.path.splitext(p)[1].lower()
        if ext in want and size > 0:
            k = kind_of(p)
            if k != "image" or size >= MIN_IMAGE_BYTES:
                files[k].append((p, size, mtime, pos))

    # 3. Read new files into the cache, then compare
    results = []
    if kind == "doc":
        CTX["match"] = lambda: _match_docs(con, q_terms, root)
        _index(con, "doc_index", files["doc"], _analyse_doc, "documents", 10, 85)
        results = _match_docs(con, q_terms, root)
    else:
        with_faces = len(q_faces) > 0
        CTX["match"] = lambda: _match_media(con, q_faces, q_hashes, root)
        _index(con, "img_index", files["image"], _analyse_image, "photos", 10, 50 if files["video"] else 85)
        if files["video"]:  # show photo results now; videos can take hours on a big drive
            n = len(_publish(query, kind, root, CTX["match"](),
                             "Photo results are ready. Videos are still being checked and will be added when done."))
            core.log(f"  Photos done: {n:,} matches so far (shown now). Checking videos next; you can Stop at any time.")
        force = ()
        if with_faces:  # videos read earlier without looking for faces must be read again
            force = {p for p, in con.execute("SELECT path FROM vid_index WHERE faces_done=0")}
        _index(con, "vid_index", files["video"], _make_video_analyser(with_faces), "videos", 60, 35, force)
        results = _match_media(con, q_faces, q_hashes, root)

    results = _publish(query, kind, root, results)
    if RESULT["note"]:
        core.log("NOTE: " + RESULT["note"])
    core._set_progress(0, "Search complete", 100, f"{len(results):,} similar files found.", step_pct=100)
    core.log(f"Found {len(results):,} similar files." + (f" Showing the best {MAX_RESULTS:,}." if len(results) > MAX_RESULTS else ""))


def _under(path, root):
    r = os.path.normcase(str(root))
    return os.path.normcase(path).startswith(r if r.endswith(os.sep) else r + os.sep)


def _faces(blob):
    return np.frombuffer(blob, dtype=np.float16).reshape(-1, 128).astype(np.float32) if blob else np.zeros((0, 128), np.float32)


def _match_media(con, q_faces, q_hashes, root):
    """Faces: ticked ("sure") matches come ONLY from the uploaded face (score >= SURE_FACE).
    A second pass uses the sure matches as extra reference faces and suggests unticked "possible"
    matches: a photo qualifies when it agrees with at least 40% of those references and still looks
    a bit like the upload. Tested: finds side views / sunglasses / full-body shots of the person
    (39 extra, all correct, in one folder) but can suggest look-alikes in big group-photo
    collections, which is why these are never pre-selected."""
    core._set_progress(1, "Comparing", 96, "Comparing with every photo and video…")
    items = []  # (path, size, mtime, kind, faces, image hash, video hashes)
    for p, size, mtime, h, faces in con.execute("SELECT path, size, mtime, hash, faces FROM img_index"):
        if _under(p, root):
            items.append((p, size, mtime, "image", _faces(faces), int(h, 16) if h else None, None))
    for p, size, mtime, hashes, faces, _ in con.execute("SELECT * FROM vid_index"):
        if _under(p, root):
            items.append((p, size, mtime, "video", _faces(faces), None, [int(x, 16) for x in hashes.split(",") if x]))

    q = q_faces[0] if len(q_faces) else None
    refs, ref_src = None, []
    if q is not None:
        first = sorted(((float((it[4] @ q).max()), it[0], it[4][int((it[4] @ q).argmax())])
                        for it in items if it[3] == "image" and len(it[4])), key=lambda x: -x[0])
        first = [x for x in first if x[0] >= SURE_FACE][:REF_FACES]
        if len(first) >= 3:  # too few references would make the agreement test meaningless
            refs, ref_src = np.array([f for _, _, f in first]), [p for _, p, _ in first]
            core.log(f"  Second pass: comparing with {len(first)} confident matches to suggest more possible matches.")

    out = []
    for p, size, mtime, kind, faces, h, vh in items:
        tag = " (in video)" if kind == "video" else ""
        if q is not None and len(faces):
            sq = faces @ q  # each face in this file vs the uploaded face
            fs = float(sq.max())
            if fs >= FACE_MATCH:
                if os.path.exists(p):
                    sure = fs >= SURE_FACE
                    out.append(_row(p, size, mtime, fs, ("Same face" if sure else "Possible same face") + tag, sure))
                continue
            if refs is not None:
                e = faces @ refs.T
                for j, src in enumerate(ref_src):  # a photo must not vouch for itself
                    if src == p:
                        e[:, j] = -1
                agree = ((e >= 0.40).mean(1)) * (sq >= 0.25)  # per face: share of references that match
                if agree.max() >= 0.40 and os.path.exists(p):
                    k = int(agree.argmax())
                    out.append(_row(p, size, mtime, float(e[k].max()), "Possible: looks like your other matches" + tag, False))
                    continue
        if kind == "image" and h is not None:
            hd = min((_hamming(h, x) for x in q_hashes), default=64)
            if hd <= HASH_MATCH and os.path.exists(p):
                out.append(_row(p, size, mtime, 1 - hd / 64, "Looks the same" if hd <= 3 else "Looks similar", hd <= 6))
        elif kind == "video" and q_hashes and vh:
            hits = sum(1 for x in q_hashes if any(_hamming(x, v) <= HASH_MATCH for v in vh))
            if hits / len(q_hashes) >= (1.0 if len(q_hashes) == 1 else 0.3) and os.path.exists(p):
                out.append(_row(p, size, mtime, hits / len(q_hashes), "Same scenes" if len(q_hashes) > 1 else "Frame looks the same", True))
    return out


def _match_docs(con, q_terms, root):
    core._set_progress(1, "Comparing", 96, "Comparing with every document…")
    rows = [(p, s, m, json.loads(t)) for p, s, m, t in con.execute("SELECT path, size, mtime, terms FROM doc_index")
            if _under(p, root)]
    n = len(rows) or 1
    df = Counter(w for *_, t in rows for w in t if w in q_terms)   # rare shared words count more
    idf = {w: math.log((n + 1) / (df[w] + 1)) + 1 for w in q_terms}
    qv = {w: (1 + math.log(c)) * idf[w] for w, c in q_terms.items()}
    qn = math.sqrt(sum(v * v for v in qv.values()))
    out = []
    for p, s, m, t in rows:
        if not t:
            continue
        dv = {w: (1 + math.log(c)) * idf.get(w, 1.0) for w, c in t.items()}
        dn = math.sqrt(sum(v * v for v in dv.values()))
        score = sum(qv[w] * dv[w] for w in qv if w in dv) / (qn * dn)
        if score >= DOC_MATCH and os.path.exists(p):
            out.append(_row(p, s, m, score, "Similar content" if score < 0.9 else "Nearly identical", score >= 0.4))
    return out


def _row(p, size, mtime, score, reason, sure):
    return {"path": p, "size": size, "mtime": datetime.fromtimestamp(mtime).strftime("%Y-%m-%d") if mtime > 0 else "",
            "score": round(min(score, 1.0), 3), "reason": reason, "kind": kind_of(p), "sure": bool(sure)}


# ---------------------------------------------------------------- copy / move the chosen results
def collect(indexes, mode):
    """Copy or move the chosen results into <root>\\_Found\\<upload name> <date>\\. Approved in the app first.
    Moves are logged and can be undone; copies leave the originals untouched."""
    core.STOP["requested"] = False
    core.PROGRESS.update(started=time.time())
    res = RESULT.get("results") or []
    chosen = [res[i] for i in indexes if 0 <= i < len(res)]
    if not chosen:
        return core.log("Nothing selected.")
    dest = Path(RESULT["root"]) / FOUND_DIR_NAME / f"{Path(RESULT['query']).stem} {datetime.now():%Y-%m-%d %H%M}"
    plan = [(r["path"], dest / Path(r["path"]).name) for r in chosen]
    core.log(f"{'Moving' if mode == 'move' else 'Copying'} {len(plan):,} files to {dest}")
    if mode == "move":
        core.execute_moves(core.db(), "found", plan)
    else:
        ok = fail = 0
        dest.mkdir(parents=True, exist_ok=True)
        for i, (src, dst) in enumerate(plan, 1):
            if core.STOP["requested"]:
                core.log(f"Stopped after {ok} files.")
                break
            try:
                shutil.copy2(src, core.unique_dest(dst))
                ok += 1
            except OSError as e:
                fail += 1
                core.log(f"  could not copy {src}: {e}")
            core._set_progress(2, "Copying files", 100 * i / len(plan), f"{i:,} / {len(plan):,} files", step_pct=100 * i / len(plan))
        core._set_progress(0, "Done", 100, f"Copied {ok:,} files, {fail:,} failed.", step_pct=100)
        core.log(f"Copied {ok} files, {fail} failed. Originals are unchanged.")
    RESULT["collected_to"] = str(dest)
    try:
        os.startfile(dest)  # open the folder so the user can look at it
    except OSError:
        pass


def thumbnail(i):
    """Small JPEG preview of result i (photos and videos only)."""
    res = RESULT.get("results") or []
    if not 0 <= i < len(res) or res[i]["kind"] not in ("image", "video"):
        return None
    p = res[i]["path"]
    img = read_image(p) if res[i]["kind"] == "image" else next(iter(video_frames(p, 1)), None)
    return _jpeg(img)


def query_thumbnail(path):
    k = kind_of(path)
    img = read_image(str(path)) if k == "image" else next(iter(video_frames(path, 1)), None) if k == "video" else None
    return _jpeg(img)


def _jpeg(img):
    if img is None:
        return None
    h, w = img.shape[:2]
    s = 200 / max(h, w)
    if s < 1:
        img = cv2.resize(img, (int(w * s), int(h * s)), interpolation=cv2.INTER_AREA)
    return cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 80])[1].tobytes()
