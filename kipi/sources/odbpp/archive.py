"""An ODB++ job as a set of text files, from a folder, a ``.zip`` or a ``.tgz``.

Files are read in place, never unpacked to disk. Names are matched without regard to
case: ODB++ names its folders in lower case, but tools differ, and so do file systems.
"""

from __future__ import annotations

import tarfile
import zipfile
from pathlib import Path
from typing import Dict, List, Optional

ARCHIVE_SUFFIXES = (".zip", ".tgz", ".tar.gz", ".tar")
MAX_FILE_BYTES = 1 << 30        # one ODB++ file larger than this is not a board


class OdbError(ValueError):
    """The file is not an ODB++ job KiPI can read; the message says why, for the user."""


def job_name(path: Path) -> str:
    """The job's name: the archive or folder name without its archive suffix."""
    name = path.name
    if path.is_file() and name.lower() == "matrix":           # picked matrix/matrix
        return path.parent.parent.name
    for suffix in ARCHIVE_SUFFIXES:
        if name.lower().endswith(suffix):
            return name[:-len(suffix)]
    return name


class Job:
    """The files of one ODB++ job, by path relative to the job's top folder."""

    def __init__(self, path):
        self.path = Path(path)
        self._zip: Optional[zipfile.ZipFile] = None
        self._tar: Optional[tarfile.TarFile] = None
        self._dir: Optional[Path] = None
        names: List[str] = []
        p = self.path
        try:
            if p.is_dir():
                self._dir = p
                names = [f.relative_to(p).as_posix() for f in p.rglob("*") if f.is_file()]
            elif p.is_file() and p.name.lower() == "matrix" and p.parent.name.lower() == "matrix":
                self._dir = p.parent.parent
                names = [f.relative_to(self._dir).as_posix()
                         for f in self._dir.rglob("*") if f.is_file()]
            elif zipfile.is_zipfile(p):
                self._zip = zipfile.ZipFile(p)
                names = [i.filename for i in self._zip.infolist() if not i.is_dir()]
            elif p.is_file() and tarfile.is_tarfile(p):
                self._tar = tarfile.open(p)
                names = [m.name for m in self._tar.getmembers() if m.isfile()]
            else:
                raise OdbError(f"{p.name} is not an ODB++ job (a folder, .zip or .tgz)")
        except (OSError, zipfile.BadZipFile, tarfile.TarError) as e:
            raise OdbError(f"{p.name} could not be opened as an ODB++ job ({e})") from e

        # The job's top folder is the one holding matrix/matrix; an archive often wraps
        # it in a folder of its own.
        tops = [n[:-len("matrix/matrix")] for n in names
                if n.lower().endswith("matrix/matrix")
                and (len(n) == len("matrix/matrix") or n[-len("matrix/matrix") - 1] == "/")]
        if not tops:
            self.close()
            raise OdbError(f"{p.name} is not an ODB++ job: it has no matrix/matrix file")
        self.prefix = min(tops, key=len)
        self._names: Dict[str, str] = {}
        for n in names:
            if n.lower().startswith(self.prefix.lower()):
                self._names[n[len(self.prefix):].lower()] = n

    def exists(self, rel: str) -> bool:
        return rel.lower() in self._names

    def list(self, folder: str) -> List[str]:
        """The names directly in ``folder`` (files and folders), lower case."""
        folder = folder.lower().rstrip("/") + "/"
        out = set()
        for n in self._names:
            if n.startswith(folder):
                out.add(n[len(folder):].split("/", 1)[0])
        return sorted(out)

    def read(self, rel: str) -> Optional[str]:
        """The text of the file ``rel``, or None if the job has no such file."""
        name = self._names.get(rel.lower())
        if name is None:
            if rel.lower() + ".z" in self._names:
                raise OdbError(f"{rel}.Z is compressed with Unix compress, which KiPI cannot "
                               "read; export the ODB++ job without file compression")
            return None
        if self._dir is not None:
            data = (self._dir / name).read_bytes()
        elif self._zip is not None:
            if self._zip.getinfo(name).file_size > MAX_FILE_BYTES:
                raise OdbError(f"{rel} is too large to be part of a board")
            data = self._zip.read(name)
        else:
            member = self._tar.getmember(name)
            if member.size > MAX_FILE_BYTES:
                raise OdbError(f"{rel} is too large to be part of a board")
            f = self._tar.extractfile(member)
            data = f.read() if f is not None else b""
        return data.decode("utf-8", errors="replace")

    def close(self):
        for a in (self._zip, self._tar):
            if a is not None:
                a.close()
        self._zip = self._tar = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
