# Duplicate Finder

A free, private Windows app that cleans up a messy hard disk. It:

1. **Finds duplicate files** by their content, not their name. Renamed copies are found too.
2. **Moves the extra copies** into one `_Duplicates` folder. It never deletes anything.
3. **Organizes** photos, videos, music, documents and archives into `_Organized\<Type>\<Year>`.
4. **Finds similar files**: upload a photo of a person and it finds every photo and video of that person. Upload a picture, a video or a document and it finds similar ones.

Everything runs **on your own computer**. No files are uploaded anywhere, there is no account, and there are no API keys or costs. It is built for large external drives (tested on a 1.9 TB USB disk with 1.9 million files).

---

## Why it is useful

- **Years of backups pile up**: "Old laptop backup", "Phone 2019", "PC Folder copy"... The same photos and videos end up stored 3–4 times and fill the disk.
- **Duplicate finders that compare names miss renamed copies**, and ones that delete files are scary. This app compares the actual content (SHA-256), shows you everything first, and only **moves** files. Every move can be undone with one click.
- **Finding all photos of one person** across 100,000 photos by hand is impossible. This app does it in minutes once the photos have been read.

### Safety rules (built in)
- **Nothing is ever deleted.** Files are only moved, and every move is logged *before* it happens, so Undo works even after a crash or power cut.
- **Nothing is moved without your OK.** You review the list and type a confirm word (`MOVE`, `ORGANIZE`, `COPY`, `UNDO`).
- **A file is only called a duplicate when its entire content matches.** The quick checks only narrow down the candidates.
- **The oldest copy is kept**, in its original place. If two copies are equally old, the one with the shorter path is kept.
- **Programs and code projects are never organized**: `Windows`, `Program Files`, `AppData`, `.git`, `node_modules` and similar folders stay as they are.
- **The app only listens on this computer** (`127.0.0.1`). It refuses requests from other websites.

---

## Install (Windows 10 / 11)

1. **Install Python 3.9 or newer** from <https://www.python.org/downloads/>.
   On the first installer screen tick **"Add python.exe to PATH"**.
2. **Download this project.** On GitHub: *Code → Download ZIP*, then unzip. Or run `git clone`.
3. **Double-click `Start Duplicate Finder.bat`.**
   - First start only: it installs the packages for *Find similar files* (`requirements.txt`, about 100 MB, a few minutes).
   - Windows asks *"Do you want to allow this app to make changes?"*. Click **Yes** for **fast mode** (see below). Click **No** and the app still works, only slower on whole drives.
   - Your browser opens the app at `http://127.0.0.1:8765`. Keep the black window open while you use the app. Closing it stops the app.

The first face search downloads two face-recognition model files (38 MB, OpenCV Model Zoo, Apache 2.0) into `models\`. After that everything works offline.

> **Manual install** (if you prefer): `python -m pip install -r requirements.txt`, then `python app.py`.
> Finding, moving and organizing duplicates needs only Python itself. The packages are just for *Find similar files*.

### Fast mode vs standard mode
| | Fast mode (clicked **Yes**) | Standard mode (clicked **No**) |
|---|---|---|
| How it lists files | Reads the drive's NTFS file table directly (read-only) | Opens every folder one by one |
| 1.9 million files on a USB disk | **~1.5 minutes** | **many hours** (over 4.5 h and still not finished in our test) |
| Needs | Administrator rights, NTFS drive | Nothing special |

---

## How to use

### 1. Find duplicates
1. **Select drive or folder** → **Browse…** → pick e.g. `E:\` → **Select this folder**.
2. **Skip files smaller than**: keep *100 KB* (recommended). Tiny files are rarely worth it and slow things down.
3. Click **Scan for duplicates**. The progress card shows the overall %, the current step, the time running and the time left. **Stop** is safe at any time: finished work is saved and the next scan continues from there.

A scan has 3 steps:
| Step | What it does |
|---|---|
| 1. List files | Gets every file's name, size and date |
| 2. Quick check | Reads the first 64 KB of files that share their size with another file |
| 3. Full compare | Reads the files that still match from start to end, in disk order |

### 2. Review
The **Review results** card lists every duplicate group, biggest first: **KEEP** (green) is the copy that stays, **MOVE** (orange) are the extra copies. Use the filter box, or **Export report** for an HTML + CSV list.

### 3. Take action
- **Move duplicates**: extra copies go to `<drive>\_Duplicates\`, keeping their original folder structure. Type `MOVE`.
- **Organize files** (optional): photos, videos, audio, documents and archives go to `<drive>\_Organized\<Type>\<Year>\`. Type `ORGANIZE`.
- **Undo last action**: puts every file of the last move/organize back. Type `UNDO`. Press it again to undo the run before that.

When you are happy, look through `_Duplicates` yourself and delete it to free the space. **The app never deletes anything.**

### 4. Find similar files
1. **Choose a file** (or drag it in): a photo, a video (`mp4`, `mov`…), a Word file (`docx`), a PDF or a text file.
2. Pick the folder to search (defaults to the scanned drive) and click **Find similar**.
3. Results appear as cards with a preview, a **match %** and the reason.
4. Tick/untick, choose **Copy** (originals stay where they are) or **Move** (Undo available), and confirm.
   The files go to `<folder>\_Found\<your file name> <date>\`, which opens when done.

| You upload | It finds |
|---|---|
| Photo with a person | Photos and videos with the **same face** |
| Photo without a face | Visually similar photos (resized, edited, re-saved copies) |
| Video | Videos with the same scenes, and photos of those scenes |
| Word / PDF / text | Documents with **similar content** (other versions, drafts, the same subject) |

**How to read face results**
- **Same face** (50 % or more, ticked): compared directly with your upload. In our checks these were always the right person.
- **Possible same face** (40–50 %, not ticked): usually right, check them yourself.
- **Possible: looks like your other matches** (not ticked): a second pass uses the confident matches to find side views, sunglasses and full-body shots. Very useful in a folder of one person's photos. In big group-photo collections it can suggest look-alikes, so check these.
- Best results come from a **clear, front-facing photo** of the person.

**Results appear early:** photo results are shown as soon as all photos are checked, while videos continue in the background. **Stop** keeps the results found so far.

---

## How long does it take?

Real measurements on a **1.9 TB USB 3 hard disk with 1.9 million files** (fast mode, normal laptop). A USB hard disk reads at most ~90–100 MB/s; that is usually the limit, not the app.

### Duplicate scan
| Step | Example (whole 1.9 TB drive) | Rule of thumb |
|---|---|---|
| 1. List files | 1.9 million files in **~1.5 min** (seconds when repeated) | Standard mode: hours |
| 2. Quick check | 94,000 candidate files in **~8 min** | ~150–200 files/s |
| 3. Full compare | 572 GB of possible duplicates in **~1 h 45 min** | Size of the duplicate data ÷ ~90 MB/s |
| **Total** | **≈ 2 hours** | |

More examples (estimates using the same speeds):
| What you scan | Expected time |
|---|---|
| A 50 GB photo folder, ~20,000 files | 5–15 min |
| A 500 GB backup drive | 30–60 min |
| A 2 TB drive full of copies of copies | 2–3 hours |
| Repeating a scan after it was stopped | Much faster: finished work is reused |

The time mostly depends on **how much duplicate data there is**, because every possible duplicate is read completely.

### Moving and organizing
| Action | Example |
|---|---|
| Move duplicates | 58,455 files in **~1 h 25 min** (~11 files/s on a USB disk; each move is logged first so Undo is always possible) |
| Organize | Similar speed per file. Listing takes 1–2 min first. |
| Undo | About as long as the move it reverses |

### Find similar files
The first search of a folder has to **read every photo / video / document once**. That is remembered (in `dupfinder.db`), so later searches only read new or changed files.

| Search | First time | Later searches |
|---|---|---|
| Face, 3,500 photos (one folder) | ~2.5 min | seconds |
| Face, **92,000 photos** (whole drive) | **~1 h 50 min** (~14 photos/s) | **~10 seconds** |
| Face in **10,800 videos** (whole drive) | **~6 hours** (12 frames per video) | seconds |
| Documents, 15,600 Word/PDF files | ~10 min | seconds |
| Folder synced by OneDrive (online-only files) | Each file is downloaded first, so it depends on your internet | seconds |

**Tip:** search a specific folder first (for example `E:\Photos`) for fast results, and run a whole-drive search when you can leave the computer on, e.g. overnight. It's safe to stop and continue later.

---

## Good to know / limits
- **Windows only** (uses Windows file-table reading, Explorer and the Windows permission prompt).
- **Only one copy of the app runs at a time.** Starting it again while it is busy just opens the running one.
- **OneDrive "online-only" files** can only be read while OneDrive is running. The app tells you how many files it couldn't read; they are tried again next time.
- **Not supported yet:** scanned or password-protected PDFs (no readable text), old `.doc` files, iPhone `HEIC` photos.
- **Files the app never scans for duplicates:** `_Duplicates`, `_Organized`, `_Found`, `System Volume Information` and the Recycle Bin. *Find similar* does search `_Organized`.

## Files
| File | Purpose |
|---|---|
| `Start Duplicate Finder.bat` | Start here: checks Python, installs packages on first run, asks for fast mode |
| `app.py` | Local web server and API (Python standard library) |
| `ui.html` | The app's page |
| `dupfinder.py` | Scan, move, organize, undo, reports. Can also run as a command-line menu: `python dupfinder.py` |
| `ntfs_fast.py` | Fast mode: reads the NTFS file table (read-only) |
| `similar.py` | Find similar files: faces, pictures, videos, documents |
| `requirements.txt` | Packages for *Find similar files* |
| `models\` | Face model files (downloaded automatically) |

Created while running (personal, excluded from git by `.gitignore`): `dupfinder.db` (scan results, move log, search cache), `uploads\`, `reports\`.

## Troubleshooting
| Problem | Fix |
|---|---|
| "Python 3.9 or newer is required" | Install Python (see Install) and tick **Add python.exe to PATH** |
| The page says the app is not running | Double-click `Start Duplicate Finder.bat` again and keep its window open |
| Scanning a whole drive is very slow | You are in standard mode. Click **Restart in fast mode** on the page and choose **Yes** |
| "Find similar needs extra Python packages" | Run `python -m pip install -r requirements.txt` and restart the app |
| Face search: "face models could not be downloaded" | Connect to the internet once and search again |
| Many files "could not be read" in a OneDrive folder | Start OneDrive, or right-click the folder → *Always keep on this device* |
| Want to start completely fresh | Close the app and delete `dupfinder.db` (your files are not affected) |

## License
[MIT](LICENSE): free to use, copy, modify and share, including commercially, as long as the license notice is kept. Provided "as is", without warranty.
The face model files downloaded at first use come from the [OpenCV Model Zoo](https://github.com/opencv/opencv_zoo) under the Apache 2.0 license.
