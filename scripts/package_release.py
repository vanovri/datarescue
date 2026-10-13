"""Build a dependency-free source ZIP, including compiled local web assets."""

import hashlib
import json
import subprocess
import sys
import zipfile
from pathlib import Path

root = Path(__file__).resolve().parents[1]
destination = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else root.parent / "DataRescue-v0.1.0.zip"
tracked = subprocess.check_output(["git", "ls-files", "-z"], cwd=root).decode().split("\0")
files = [root / name for name in tracked if name] + sorted((root / "frontend" / "dist").rglob("*"))
files = [path for path in files if path.is_file()]
if not (root / "frontend" / "dist" / "index.html").exists():
    raise SystemExit("Build frontend assets before packaging.")
with zipfile.ZipFile(destination, "x", zipfile.ZIP_DEFLATED) as archive:
    for path in files:
        relative = path.relative_to(root).as_posix()
        assert not any(part in (".git", ".venv", "node_modules", "__pycache__") for part in path.relative_to(root).parts)
        archive.write(path, "DataRescue/" + relative)
with zipfile.ZipFile(destination) as archive:
    assert archive.testzip() is None
    assert "DataRescue/frontend/dist/index.html" in archive.namelist()
    count = len(archive.namelist())
result = {"archive": str(destination), "files": count, "bytes": destination.stat().st_size,
          "sha256": hashlib.sha256(destination.read_bytes()).hexdigest()}
manifest = destination.with_suffix(".manifest.json")
with manifest.open("x", encoding="utf-8") as output:
    output.write(json.dumps(result, indent=2) + "\n")
print(json.dumps(result, indent=2))
