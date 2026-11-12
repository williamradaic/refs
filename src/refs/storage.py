"""The only module that touches files under the Dropbox root.

Everything else works with Dropbox-relative paths ("phd/coursework/.../x.pdf").
Keeping file access in one place is what would let a hosted version swap the
local folder for the Dropbox API later without rewriting the rest.
"""

from __future__ import annotations

import hashlib
import os
import stat
import unicodedata
from dataclasses import dataclass
from typing import Iterator

from .config import Config

# macOS marks online-only cloud files ("dataless") with this flag. Reading one
# forces a download, so the scanner leaves them alone.
SF_DATALESS = 0x40000000


@dataclass(frozen=True)
class DiskFile:
    """One PDF found on disk, with the facts needed to detect changes cheaply."""

    rel: str        # Dropbox-relative path, forward slashes, NFC-normalised
    inode: int      # the file's ID on this disk: survives rename and move
    mtime: float    # last-modified time
    size: int
    online_only: bool = False


def _normalise(rel: str) -> str:
    # macOS can hand back accented names in a decomposed form; store one form
    # (NFC) so "kőszegi" always compares equal to itself.
    return unicodedata.normalize("NFC", rel)


def walk_pdfs(cfg: Config) -> Iterator[DiskFile]:
    """Yield every PDF under the configured roots, skipping ignored folders."""
    # Folders never entered: the app's own state folder and excluded paths.
    skipped = {str(cfg.absolute(rel)) for rel in [cfg.state_dir, *cfg.exclude_paths]}

    for root in cfg.roots:
        top = cfg.absolute(root)
        if not top.is_dir():
            continue
        # followlinks=False: symlinked folders are not entered, so a file is
        # only ever seen through its one real path.
        for folder, subfolders, files in os.walk(top, followlinks=False):
            # Editing `subfolders` in place tells os.walk not to descend there.
            subfolders[:] = [
                name for name in subfolders
                if name.lower() not in cfg.ignore_dirs
                and os.path.join(folder, name) not in skipped
            ]
            for name in files:
                if not name.lower().endswith(".pdf") or name.startswith("._"):
                    continue
                full = os.path.join(folder, name)
                try:
                    info = os.lstat(full)          # lstat: do not follow symlinks
                except OSError:
                    continue                       # vanished mid-scan
                if not stat.S_ISREG(info.st_mode):
                    continue                       # symlink or something odd
                rel = os.path.relpath(full, cfg.dropbox_root).replace(os.sep, "/")
                yield DiskFile(
                    rel=_normalise(rel),
                    inode=info.st_ino,
                    mtime=info.st_mtime,
                    size=info.st_size,
                    online_only=bool(getattr(info, "st_flags", 0) & SF_DATALESS),
                )


def sha256(cfg: Config, rel: str) -> str:
    """Content hash of a file: the file's identity, independent of its name."""
    with cfg.absolute(rel).open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()
