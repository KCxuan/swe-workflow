"""Package source + built UI, optionally a reviewed demo video; exclude local state."""
import argparse
import os
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parents[1]
LOCAL_HOME = Path(os.environ.get("SWE_WORKFLOW_HOME", str(ROOT.with_name(ROOT.name + "-local"))))
FOLDERS = ["backend", "frontend/src", "frontend/public", "frontend/dist", "docs", "scripts", "tests"]
ROOT_FILES = ["README.md", "requirements.in", "requirements.txt", ".env.example", ".gitignore", "setup.ps1", "start.ps1", "runtime.ps1", "python.ps1"]
FRONTEND_FILES = ["index.html", "package.json", "package-lock.json", "tsconfig.json", "vite.config.ts"]


def source_files():
    files = [p for folder in FOLDERS for p in (ROOT / folder).rglob("*")
             if p.is_file() and "__pycache__" not in p.parts and p.suffix not in {".pyc", ".tsbuildinfo"}]
    files += [ROOT / name for name in ROOT_FILES]
    files += [ROOT / "frontend" / name for name in FRONTEND_FILES]
    for p in sorted(set(files)):
        if (p.name.startswith(".env") and p.name != ".env.example") or any(part in
            {"node_modules", ".venv", ".cache", ".data", ".git"} for part in p.relative_to(ROOT).parts):
            continue
        if p.is_symlink() or not p.resolve().is_relative_to(ROOT):
            raise ValueError(f"Unexpected source link: {p}")
        yield p


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", default="Homework2-source", help="Use your student number and name for the final submission")
    parser.add_argument("--video", type=Path, help="Optional demo video; confirm it is within one minute")
    args = parser.parse_args()
    if not args.name or Path(args.name).name != args.name:
        parser.error("name must be a filename, without directories")
    output = LOCAL_HOME / "submission"
    output.mkdir(parents=True, exist_ok=True)
    target = output / (args.name + ".zip")
    if args.video and not args.video.is_file():
        parser.error("video does not exist")
    with zipfile.ZipFile(target, "x", zipfile.ZIP_DEFLATED) as archive:
        for p in source_files():
            archive.write(p, p.relative_to(ROOT).as_posix())
        if args.video:
            archive.write(args.video, "demo" + args.video.suffix)
    print(f"Saved: {target} ({target.stat().st_size / 1024**2:.2f} MiB)")
    if target.stat().st_size >= 200_000_000:
        raise SystemExit("Archive exceeds the 200 MB limit; reduce the optional video before submitting")


if __name__ == "__main__":
    main()
