"""KiPI's dependencies: every package that will be installed, and how big they are.

The list is worked out live for this computer: pip resolves ``requirements-standalone.txt``
without installing anything (``--dry-run --report``) and PyPI says how big each file is.
Offline, or with a Python that has no pip, the launcher shows the list saved in
:data:`MANIFEST` when KiPI was released (``.github/scripts/dep_sizes.py`` writes it) and,
failing that, just the names of what KiPI asks for.
"""

from __future__ import annotations

import hashlib
import json
import platform as _platform
import subprocess
import tempfile
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from kipi.launcher.system import child_kwargs

PARDISO = "pypardiso"
MANIFEST = Path(__file__).with_name("deps_manifest.json")
# Installed size over download size, for the "on disk" estimate: wheels are compressed.
# Measured: KiPI 0.8.0's Windows environment is 810 MB from 254 MB of downloads.
INSTALLED_PER_DOWNLOAD = 3.2


class ResolveError(RuntimeError):
    pass


@dataclass
class Package:
    name: str
    version: str
    size: Optional[int] = None      # download, in bytes; None if unknown
    url: str = ""


@dataclass
class Plan:
    """What setup will install: ``packages`` always, and ``pardiso`` on top if chosen."""
    packages: List[Package]
    pardiso: List[Package] = field(default_factory=list)
    source: str = ""                # "live", "manifest" or "" (only the names are known)
    note: str = ""                  # why the list is not live

    def chosen(self, with_pardiso: bool) -> List[Package]:
        return self.packages + (self.pardiso if with_pardiso else [])

    def download(self, with_pardiso: bool) -> Optional[int]:
        """Total download, or None if any size is unknown."""
        sizes = [p.size for p in self.chosen(with_pardiso)]
        return None if not sizes or None in sizes else sum(sizes)

    def installed(self, with_pardiso: bool) -> Optional[int]:
        d = self.download(with_pardiso)
        return None if d is None else int(d * INSTALLED_PER_DOWNLOAD)


def normalize(name: str) -> str:
    return name.lower().replace("_", "-").replace(".", "-")


def requirement_lines(path: Path) -> List[str]:
    lines = (line.split("#", 1)[0].strip() for line in path.read_text().splitlines())
    return [line for line in lines if line]


def requirements_hash(path: Path) -> str:
    """Changes when the requirements do (comments and blank lines aside)."""
    text = "\n".join(sorted(line.replace(" ", "") for line in requirement_lines(path)))
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def platform_key(system: Optional[str] = None, machine: Optional[str] = None) -> str:
    """This computer's entry in the manifest: win_amd64, linux_x86_64, macos_arm64, ..."""
    system = (system or _platform.system()).lower()
    machine = (machine or _platform.machine()).lower()
    machine = {"amd64": "x86_64", "x64": "x86_64", "arm64": "arm64",
               "aarch64": "aarch64"}.get(machine, machine)
    if system == "windows":
        return "win_amd64" if machine == "x86_64" else f"win_{machine}"
    if system == "darwin":
        return f"macos_{'arm64' if machine in ('arm64', 'aarch64') else machine}"
    return f"{system}_{machine}"


# --- Live -------------------------------------------------------------------------------

def resolve(python: Sequence[str], requirements: Path, extra: Sequence[str] = (),
            pip_args: Sequence[str] = (), timeout: float = 300.0) -> List[Package]:
    """Every package pip would install for ``requirements`` (and ``extra``), without
    installing anything. ``python`` runs the pip to ask."""
    with tempfile.TemporaryDirectory(prefix="kipi-resolve-") as tmp:
        report = Path(tmp) / "report.json"
        cmd = [*python, "-m", "pip", "install", "--dry-run", "--quiet", "--ignore-installed",
               "--disable-pip-version-check", "--report", str(report),
               "-r", str(requirements), *extra, *pip_args]
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                                 **child_kwargs())
        except (OSError, subprocess.SubprocessError) as e:
            raise ResolveError(f"could not run pip: {e}") from e
        if out.returncode != 0 or not report.is_file():
            tail = (out.stderr or out.stdout).strip().splitlines()[-3:]
            raise ResolveError(" ".join(tail) or f"pip exited with {out.returncode}")
        data = json.loads(report.read_text(encoding="utf-8"))
    return [Package(i["metadata"]["name"], i["metadata"]["version"],
                    url=i.get("download_info", {}).get("url", ""))
            for i in data.get("install", [])]


def _head_size(url: str, timeout: float) -> Optional[int]:
    try:
        req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "KiPI-launcher"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            n = int(resp.headers.get("Content-Length", 0))
            return n or None
    except Exception:  # noqa: BLE001 - a size we cannot get is shown as unknown
        return None


def fetch_sizes(packages: List[Package], timeout: float = 10.0, workers: int = 8) -> None:
    """Fill in each package's download size from its file on PyPI."""
    todo = [p for p in packages if p.url and p.size is None]
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for p, size in zip(todo, ex.map(lambda p: _head_size(p.url, timeout), todo)):
            p.size = size


def live_plan(python: Sequence[str], requirements: Path, offer_pardiso: bool) -> Plan:
    with ThreadPoolExecutor(max_workers=2) as ex:
        base_f = ex.submit(resolve, python, requirements)
        both_f = ex.submit(resolve, python, requirements, [PARDISO]) if offer_pardiso else None
        base = base_f.result()
        both = both_f.result() if both_f is not None else []
    names = {normalize(p.name) for p in base}
    pardiso = [p for p in both if normalize(p.name) not in names]
    fetch_sizes(base + pardiso)
    return Plan(base, pardiso, "live")


# --- Saved with the release ---------------------------------------------------------------

def load_manifest(path: Path = MANIFEST) -> Dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def manifest_plan(key: str, manifest: Optional[Dict] = None) -> Optional[Plan]:
    data = load_manifest() if manifest is None else manifest
    entry = data.get("platforms", {}).get(key)
    if not entry:
        return None

    def packages(rows):
        return [Package(name, version, size) for name, version, size in rows]

    return Plan(packages(entry.get("packages", [])), packages(entry.get("pardiso", [])),
                "manifest", f"as of KiPI {data.get('kipi', '?')}")


def names_plan(requirements: Path, note: str) -> Plan:
    """Only what KiPI asks for, by name: the list pip would install is not known."""
    pkgs = []
    for line in requirement_lines(requirements):
        name = line
        for sep in ("<", ">", "=", "!", "~", ";", "["):
            name = name.split(sep, 1)[0]
        pkgs.append(Package(name.strip(), line[len(name.strip()):].strip()))
    return Plan(pkgs, [Package(PARDISO, "")], "", note)


def make_plan(python: Sequence[str], requirements: Path, offer_pardiso: bool,
              key: Optional[str] = None) -> Plan:
    """The live plan, else the release's, else the names alone; never raises."""
    try:
        return live_plan(python, requirements, offer_pardiso)
    except (ResolveError, OSError, ValueError, KeyError) as e:
        why = f"could not check with PyPI ({e})"
    saved = manifest_plan(key or platform_key())
    if saved is not None:
        saved.note = f"{why}; sizes {saved.note}"
        if not offer_pardiso:
            saved.pardiso = []
        return saved
    return names_plan(requirements, why)


def fmt_size(n: Optional[int]) -> str:
    if n is None:
        return "size unknown"
    if n >= 1e9:
        return f"{n / 1e9:.1f} GB"
    if n >= 1e6:
        return f"{n / 1e6:.0f} MB"
    return f"{max(1, round(n / 1e3))} kB"
