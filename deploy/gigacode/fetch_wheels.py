"""Download the wheelhouse for an offline Hermes install on Linux x86_64 / CPython 3.14.

Runs on any machine with pip and access to a PyPI mirror (Python 3.9+, any version: the
requirement markers are evaluated for the TARGET, not for this interpreter, which is why a plain
``pip download -r`` on another Python silently skips almost every dependency).

    python3 fetch_wheels.py requirements.lock.txt wheels/

Besides the wheels, writes ``wheels/requirements.txt`` (the target-filtered pins) for the
offline install: ``pip install --no-index --find-links wheels -r wheels/requirements.txt``.
"""

import subprocess
import sys
from pathlib import Path

try:
    from packaging.markers import Marker
except ImportError:  # pip vendors packaging; enough for marker evaluation
    from pip._vendor.packaging.markers import Marker

TARGET = {
    "python_version": "3.14", "python_full_version": "3.14.7", "implementation_name": "cpython",
    "implementation_version": "3.14.7", "platform_python_implementation": "CPython",
    "sys_platform": "linux", "platform_system": "Linux", "platform_machine": "x86_64", "os_name": "posix",
    "platform_release": "", "platform_version": "", "extra": "",
}
PIP_TARGET = [
    "--only-binary=:all:", "--python-version", "3.14", "--implementation", "cp",
    "--abi", "cp314", "--abi", "abi3", "--abi", "none",
    "--platform", "manylinux_2_28_x86_64", "--platform", "manylinux_2_17_x86_64",
    "--platform", "manylinux2014_x86_64", "--platform", "linux_x86_64", "--platform", "any",
]
BUILD_TOOLS = ["setuptools==83.0.0", "wheel", "pip"]
DOWNLOAD_TIMEOUT = 3600  # ~50 MB through a corporate mirror


def target_requirements(lock_path: str) -> list[str]:
    """Pinned requirements from a ``uv export`` file whose markers hold on the target."""
    selected = []
    with open(lock_path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.split("#", 1)[0].strip()
            if not line or line.startswith("-"):
                continue
            spec, _, marker = line.partition(";")
            if not marker.strip() or Marker(marker.strip()).evaluate(TARGET):
                selected.append(spec.strip())
    return selected


def main(lock_path: str, out_dir: str) -> int:
    requirements = target_requirements(lock_path)
    print(f"{len(requirements)} requirements for linux x86_64 / CPython 3.14", flush=True)
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    Path(out_dir, "requirements.txt").write_text("\n".join(requirements) + "\n", encoding="utf-8")
    return subprocess.call([sys.executable, "-m", "pip", "download", "-d", out_dir, *PIP_TARGET,
                            *requirements, *BUILD_TOOLS], timeout=DOWNLOAD_TIMEOUT)


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:3]))
