"""Fast file listing for NTFS drives by reading the Master File Table (MFT) directly.

Walking folders on a USB hard disk costs a disk seek per folder (hours for 500k files).
The MFT is one mostly-contiguous table holding every file's name, folder, size, date and
disk position, so reading it sequentially takes minutes. Needs administrator rights.
Read-only: the volume is opened for reading and nothing is ever written.
"""
import ctypes
import os
import struct

FILETIME_EPOCH = 116444736000000000
RAW_HEAD = 64 * 1024  # bytes read for the quick check
VOLUME = {}  # drive letter + cluster size of the last volume read (used for direct reads)
ROOT_RECORD = 5
REF_MASK = (1 << 48) - 1
U16, U32, U64 = struct.Struct("<H"), struct.Struct("<I"), struct.Struct("<Q")


def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (AttributeError, OSError):
        return False


class NotAvailable(Exception):
    """Raised when the fast path can't be used; caller falls back to the normal folder walk."""


def _runs(buf, off):
    """Decode an NTFS data-run list -> [(lcn or None, length_in_clusters)]."""
    out, lcn = [], 0
    while off < len(buf) and buf[off]:
        h = buf[off]
        ls, os_ = h & 0xF, h >> 4
        off += 1
        length = int.from_bytes(buf[off:off + ls], "little")
        off += ls
        if os_:
            lcn += int.from_bytes(buf[off:off + os_], "little", signed=True)
            out.append((lcn, length))
        else:
            out.append((None, length))  # sparse
        off += os_
    return out


def _fixup(rec):
    """Apply the update-sequence fix-up (last 2 bytes of every 512-byte block). False if corrupt."""
    usa_ofs, usa_cnt = U16.unpack_from(rec, 4)[0], U16.unpack_from(rec, 6)[0]
    usn = rec[usa_ofs:usa_ofs + 2]
    for i in range(1, usa_cnt):
        pos = i * 512 - 2
        if pos + 2 > len(rec) or rec[pos:pos + 2] != usn:
            return False
        rec[pos:pos + 2] = rec[usa_ofs + 2 * i:usa_ofs + 2 * i + 2]
    return True


def _parse(rec):
    """-> (flags, base_ref, name, parent, mtime_ft, size, first_lcn, fn_size, name_rank) or None.
    name_rank: 0 = long name, 1 = POSIX name, 2 = short 8.3 name (e.g. ONEDRI~2)."""
    if rec[:4] != b"FILE" or not _fixup(rec):
        return None
    flags = U16.unpack_from(rec, 0x16)[0]
    if not flags & 1:  # record not in use (deleted file)
        return None
    base = U64.unpack_from(rec, 0x20)[0] & REF_MASK
    a = U16.unpack_from(rec, 0x14)[0]
    name = parent = mtime = size = lcn = fn_size = None
    name_ns = 99
    n = len(rec)
    while a + 16 <= n:
        t = U32.unpack_from(rec, a)[0]
        if t == 0xFFFFFFFF:
            break
        alen = U32.unpack_from(rec, a + 4)[0]
        if alen == 0 or a + alen > n:
            break
        nonres, nlen = rec[a + 8], rec[a + 9]
        if t == 0x10 and not nonres:  # $STANDARD_INFORMATION
            co = U16.unpack_from(rec, a + 0x14)[0]
            mtime = U64.unpack_from(rec, a + co + 8)[0]
        elif t == 0x30 and not nonres:  # $FILE_NAME (prefer the long name over the 8.3 DOS name)
            co = a + U16.unpack_from(rec, a + 0x14)[0]
            ns = rec[co + 0x41]
            rank = 0 if ns in (1, 3) else 1 if ns == 0 else 2
            if rank < name_ns:
                name_ns = rank
                parent = U64.unpack_from(rec, co)[0] & REF_MASK
                fn_size = U64.unpack_from(rec, co + 0x30)[0]
                nl = rec[co + 0x40]
                name = rec[co + 0x42:co + 0x42 + nl * 2].decode("utf-16-le", "replace")
                if mtime is None:
                    mtime = U64.unpack_from(rec, co + 0x10)[0]
        elif t == 0x80 and nlen == 0:  # unnamed $DATA = file content
            if nonres:
                if U64.unpack_from(rec, a + 0x10)[0] == 0:  # first extent
                    size = U64.unpack_from(rec, a + 0x30)[0]
                    r = _runs(rec, a + U16.unpack_from(rec, a + 0x20)[0])
                    if r and r[0][0] is not None:
                        # (first cluster, clusters in first run, valid-data length, attribute flags)
                        lcn = (r[0][0], r[0][1], U64.unpack_from(rec, a + 0x38)[0], U16.unpack_from(rec, a + 0x0C)[0])
            else:
                size = U32.unpack_from(rec, a + 0x10)[0]
        a += alen
    return flags, base, name, parent, mtime, size, lcn, fn_size, name_ns


def list_files(root, skip_dirs, progress=None, check_stop=None):
    """Yield (path, size, mtime, disk_position) for every file under root (an NTFS path).
    disk_position > 0 means read_head() can read the file's first 64 KB straight from the disk."""
    if os.name != "nt" or not is_admin():
        raise NotAvailable("needs administrator rights")
    root = os.path.abspath(str(root))
    drive = os.path.splitdrive(root)[0]
    if len(drive) != 2:
        raise NotAvailable("not a lettered drive")
    try:
        vol = open(rf"\\.\{drive}", "rb", buffering=0)
    except OSError as e:
        raise NotAvailable(f"cannot open volume: {e}")
    with vol:
        boot = vol.read(512)
        if boot[3:11] != b"NTFS    ":
            raise NotAvailable("drive is not NTFS")
        bps = U16.unpack_from(boot, 0x0B)[0]
        spc = boot[0x0D]
        spc = 1 << (256 - spc) if spc > 128 else spc
        cluster = bps * spc
        mft_lcn = U64.unpack_from(boot, 0x30)[0]
        cpr = struct.unpack_from("<b", boot, 0x40)[0]
        rec_size = cpr * cluster if cpr > 0 else 1 << -cpr
        VOLUME.update(drive=drive, cluster=cluster)

        # Record 0 = $MFT itself; its $DATA run list says where the whole table lives.
        vol.seek(mft_lcn * cluster)
        first = bytearray(vol.read(max(cluster, rec_size, bps)))[:rec_size]
        if first[:4] != b"FILE" or not _fixup(first):
            raise NotAvailable("MFT header unreadable")
        a = U16.unpack_from(first, 0x14)[0]
        mft_runs = mft_size = None
        while a + 16 <= rec_size:
            t, alen = U32.unpack_from(first, a)[0], U32.unpack_from(first, a + 4)[0]
            if t == 0xFFFFFFFF or alen == 0:
                break
            if t == 0x80 and first[a + 8]:
                mft_size = U64.unpack_from(first, a + 0x30)[0]
                mft_runs = _runs(first, a + U16.unpack_from(first, a + 0x20)[0])
                break
            a += alen
        if not mft_runs or sum(n for _, n in mft_runs) * cluster < mft_size:
            raise NotAvailable("MFT layout not supported")

        total_recs = mft_size // rec_size
        files, dirs, ext, ext_names, ranks = {}, {}, {}, {}, {}
        recno, chunk_bytes, read_bytes = 0, 16 * 1024 * 1024, 0
        for lcn, length in mft_runs:
            remaining = length * cluster
            pos = (lcn or 0) * cluster
            while remaining > 0 and recno < total_recs:
                n = min(chunk_bytes, remaining)
                if lcn is None:
                    buf = bytes(n)
                else:
                    vol.seek(pos)
                    buf = vol.read(n)
                pos += n
                remaining -= n
                read_bytes += n
                for off in range(0, len(buf) - rec_size + 1, rec_size):
                    if recno >= total_recs:
                        break
                    r = _parse(bytearray(buf[off:off + rec_size]))
                    if r:
                        flags, base, name, parent, mtime, size, dlcn, fn_size, rank = r
                        if base:  # extension record: may hold the $DATA or the long name of a file
                            if size is not None:
                                ext[base] = (size, dlcn)
                            if name is not None and rank < ext_names.get(base, (99,))[0]:
                                ext_names[base] = (rank, parent, name)
                        else:  # name may be None here: then it is only in an extension record
                            ranks[recno] = rank
                            if flags & 2:
                                dirs[recno] = (parent, name)
                            else:
                                files[recno] = [parent, name, mtime, size, dlcn, fn_size]
                    recno += 1
                if check_stop:
                    check_stop()
                if progress:
                    progress(read_bytes, mft_size, len(files))

    # Long names kept in an extension record (common for OneDrive folders): without this a folder
    # shows up under its short 8.3 name like ONEDRI~2 and nothing inside it matches the real path.
    for rn, (rank, parent, name) in ext_names.items():
        if rank < ranks.get(rn, 99):
            if rn in dirs:
                dirs[rn] = (parent, name)
            elif rn in files:
                files[rn][0], files[rn][1] = parent, name
    for d in (dirs, files):  # still nameless: unusable
        for rn in [rn for rn, v in d.items() if v[1] is None]:
            del d[rn]
    for rn, (size, dlcn) in ext.items():  # $DATA stored in an extension record
        f = files.get(rn)
        if f is not None and f[3] is None:
            f[3], f[4] = size, dlcn

    # Rebuild folder paths from parent links
    skip = {s.lower() for s in skip_dirs}
    cache = {ROOT_RECORD: drive + "\\"}

    def dir_path(rn):
        chain = []
        while rn not in cache:
            d = dirs.get(rn)
            if d is None or len(chain) > 512:
                return None  # orphan or loop
            chain.append(rn)
            rn = d[0]
        base = cache[rn]
        for c in reversed(chain):
            parent_path, name = base, dirs[c][1]
            if base is None or name.lower() in skip or (dirs[c][0] == ROOT_RECORD and name.startswith("$")):
                base = None
            else:
                base = parent_path.rstrip("\\") + "\\" + name
            cache[c] = base
        return base

    prefix = root.rstrip("\\").lower() + "\\"
    for rn, (parent, name, mtime, size, dlcn, fn_size) in files.items():
        if rn < 24:
            continue  # NTFS system files
        if size is None:
            size = fn_size or 0
        d = dir_path(parent)
        if d is None:
            continue
        path = d.rstrip("\\") + "\\" + name
        if not path.lower().startswith(prefix):
            continue
        # Disk position: positive = the file's start can be read straight from the disk (one
        # contiguous, uncompressed, fully written first run); negative = position only, open the file.
        pos = 0
        if dlcn:
            lcn, run_len, valid, aflags = dlcn
            need = min(size, RAW_HEAD)
            safe = not aflags & 0xC001 and run_len * cluster >= need and valid >= need
            pos = lcn if safe else -lcn
        yield path, size, ((mtime or FILETIME_EPOCH) - FILETIME_EPOCH) / 1e7, pos


class RawReader:
    """Reads a file's first 64 KB straight from the volume by its cluster number (admin, read-only).
    Same bytes as opening the file, but without Windows looking up every folder in its path first."""

    def __init__(self):
        self.cluster = VOLUME["cluster"]
        self.vol = open(rf"\\.\{VOLUME['drive']}", "rb", buffering=0)

    def read_head(self, lcn, size):
        self.vol.seek(lcn * self.cluster)
        return self.vol.read(RAW_HEAD)[:min(size, RAW_HEAD)]  # 64 KB = whole sectors, aligned

    def close(self):
        self.vol.close()
