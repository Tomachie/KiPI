"""Write kipi/launcher/deps_manifest.json: every package standalone KiPI installs on each
platform, with its download size.

The launcher works this out live for the computer it runs on; the manifest is what it
shows when it cannot (offline, or a Python with no pip). Run before a release:

    python .github/scripts/dep_sizes.py

pip resolves requirements-standalone.txt (and, where Intel MKL exists, pypardiso) for each
platform's wheels without installing them, and PyPI gives the file sizes.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from kipi import __version__  # noqa: E402
from kipi.launcher import deps, paths  # noqa: E402

PYTHON = "3.12"             # a supported Python with wheels for everything


def _tags(prefix_versions, arch):
    return [f"{p}_{arch}" for p in prefix_versions]


_MANYLINUX = [f"manylinux_2_{v}" for v in range(17, 40)] + ["manylinux2014"]
_MACOS = [f"macosx_{v}_0" for v in range(11, 16)]
PLATFORMS = {
    "win_amd64": (["win_amd64"], True),
    "linux_x86_64": (_tags(_MANYLINUX, "x86_64"), True),
    "linux_aarch64": (_tags(_MANYLINUX, "aarch64"), False),
    "macos_arm64": (_tags(_MACOS, "arm64") + _tags(_MACOS, "universal2"), False),
    "macos_x86_64": (_tags([f"macosx_10_{v}" for v in range(9, 16)] + _MACOS, "x86_64")
                     + _tags(_MACOS, "universal2"), False),
}


def pip_args(platform_tags):
    args = ["--only-binary=:all:", "--implementation", "cp", "--python-version", PYTHON]
    for abi in (f"cp{PYTHON.replace('.', '')}", "abi3", "none"):
        args += ["--abi", abi]
    for tag in platform_tags:
        args += ["--platform", tag]
    return args


def platform_entry(tags, pardiso):
    python = [sys.executable]
    req = paths.requirements_file(ROOT)
    base = deps.resolve(python, req, pip_args=pip_args(tags))
    extra = []
    if pardiso:
        both = deps.resolve(python, req, [deps.PARDISO], pip_args=pip_args(tags))
        names = {deps.normalize(p.name) for p in base}
        extra = [p for p in both if deps.normalize(p.name) not in names]
    deps.fetch_sizes(base + extra)
    row = lambda p: [p.name, p.version, p.size]   # noqa: E731
    return {"packages": [row(p) for p in base], "pardiso": [row(p) for p in extra]}


def main() -> int:
    out = {"kipi": __version__, "generated": time.strftime("%Y-%m-%d"), "python": PYTHON,
           "requirements_hash": deps.requirements_hash(paths.requirements_file(ROOT)),
           "platforms": {}}
    for key, (tags, pardiso) in PLATFORMS.items():
        try:
            out["platforms"][key] = entry = platform_entry(tags, pardiso)
        except deps.ResolveError as e:
            print(f"{key}: could not resolve ({e}); left out", file=sys.stderr)
            continue
        total = sum(r[2] or 0 for r in entry["packages"])
        extra = sum(r[2] or 0 for r in entry["pardiso"])
        print(f"{key}: {len(entry['packages'])} packages, {deps.fmt_size(total)}"
              + (f"; PARDISO adds {len(entry['pardiso'])}, {deps.fmt_size(extra)}"
                 if entry["pardiso"] else ""))
    deps.MANIFEST.write_text(json.dumps(out, indent=1) + "\n", encoding="utf-8")
    print(f"Wrote {deps.MANIFEST.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
