"""Build the uploadable ARC-Bench agent archive.

Usage (from the project root):

    python scripts/package.py                    -> dist/arc_agent.zip
    python scripts/package.py -o dist/v2.zip

The archive root holds main.py and requirements.txt side by side, which is what
the ARC-Bench uploader requires. Development-only state is excluded, and the
result is re-opened and inspected before the command reports success.

This script does not need Git, so it also works on a copy of the project.
"""

import argparse
from pathlib import Path
import sys
import zipfile


# Directories that never belong in the uploaded agent bundle.
EXCLUDED_DIRECTORIES = {
    ".git", ".github", ".venv", "venv", "env", "node_modules", "dist", "build",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".arc",
    "output", "outputs", "out", "runs", "test-results", "playwright-report",
    ".idea", ".vscode",
}

# Files that must never leave this machine, or that only describe a local run.
EXCLUDED_FILES = {".env", ".env.local", ".DS_Store", "Thumbs.db"}

EXCLUDED_SUFFIXES = {".pyc", ".pyo", ".zip", ".log"}


TRANSIENT_PREFIXES = (".", "pip-", "tmp", "_probe")


def included(path: Path, root: Path) -> bool:
    """Decide whether one path belongs in the bundle."""
    relative = path.relative_to(root)
    ancestors = relative.parts[:-1]
    if any(part in EXCLUDED_DIRECTORIES for part in ancestors):
        return False
    # Local working state must never ship: hidden caches (.npm-cache, .playwright, .tmp*),
    # pip/temp scratch and probe scripts. A top-level "requirements/" directory is the local
    # copy of the benchmark tasks; nested packages may legitimately share the name.
    if any(part.startswith(TRANSIENT_PREFIXES) for part in ancestors):
        return False
    if ancestors and ancestors[0] == "requirements":
        return False
    if path.is_dir():
        if path.name.startswith(TRANSIENT_PREFIXES):
            return False
        if path.name == "requirements" and len(relative.parts) == 1:
            return False
        return path.name not in EXCLUDED_DIRECTORIES
    if path.name.startswith(("pip-", "tmp", "_probe")):
        return False
    if path.name in EXCLUDED_FILES:
        return False
    return path.suffix.lower() not in EXCLUDED_SUFFIXES


def collect(root: Path, archive_path: Path) -> list[Path]:
    """List every file to archive, skipping the output archive itself."""
    entries = []
    for path in sorted(root.rglob("*")):
        if not included(path, root):
            continue
        if path.is_file() and path.resolve() != archive_path.resolve():
            entries.append(path)
    return entries


def build(root: Path, output: Path) -> int:
    """Write the archive and return its entry count."""
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.tmp")
    entries = collect(root, output)
    try:
        with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED,
                compresslevel=6, allowZip64=True, strict_timestamps=False) as archive:
            for path in entries:
                archive.write(path, path.relative_to(root).as_posix())
        temporary.replace(output)
    finally:
        temporary.unlink(missing_ok=True)
    return len(entries)


def verify(output: Path) -> list[str]:
    """Re-read the archive and report upload-contract violations."""
    problems = []
    with zipfile.ZipFile(output) as archive:
        names = archive.namelist()
        bad = archive.testzip()
        if bad is not None:
            problems.append(f"corrupt entry: {bad}")
    for required in ("main.py", "requirements.txt"):
        if required not in names:
            problems.append(f"{required} is missing from the archive root")
    for name in names:
        parts = name.split("/")
        if parts[0] in EXCLUDED_DIRECTORIES:
            problems.append(f"excluded directory leaked into the archive: {name}")
        if parts[-1] in EXCLUDED_FILES:
            problems.append(f"secret or local file leaked into the archive: {name}")
    if "infrastructure/compiler.py" not in names:
        problems.append("infrastructure/compiler.py is missing; the pipeline would not import")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-o", "--output", type=Path, default=Path("dist/arc_agent.zip"),
        help="Archive path (default: dist/arc_agent.zip)")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1],
        help="Project root to archive (default: the parent of this script)")
    args = parser.parse_args(argv)

    root = args.root.resolve()
    output = (args.output if args.output.is_absolute() else root / args.output).resolve()
    print(f"Source : {root}")
    print(f"Archive: {output}")
    count = build(root, output)
    problems = verify(output)
    if problems:
        for problem in problems:
            print(f"FAIL {problem}", file=sys.stderr)
        return 1
    print(f"OK {count} files, {output.stat().st_size / 1024:.0f} KiB")
    with zipfile.ZipFile(output) as archive:
        tops = sorted({name.split("/")[0] for name in archive.namelist()})
    print("Top level: " + ", ".join(tops))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
