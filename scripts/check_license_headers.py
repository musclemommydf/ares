#!/usr/bin/env python3
"""Check (or --fix) the SPDX dual-license header on tracked source files.

Used by CI (.github/workflows/ci.yml) and runnable locally:

    python3 scripts/check_license_headers.py          # check; non-zero exit if any missing
    python3 scripts/check_license_headers.py --fix    # insert missing headers in place

Stdlib only — no dependencies. Enumerates files via `git ls-files`, so it
honours .gitignore and never touches node_modules / .venv / build output.
Empty files (e.g. blank __init__.py) are intentionally left bare.
"""
from __future__ import annotations

import os
import subprocess
import sys

SPDX = "SPDX-License-Identifier: MIT OR Apache-2.0"
HOLDER = "Copyright (c) 2026 Ares"

HASH = {".py", ".sh"}                                              # "# " comments
SLASH = {".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".kt", ".gradle"}  # "// "
BLOCK = {".css"}                                                   # "/* */"
EXTS = HASH | SLASH | BLOCK


def repo_root() -> str:
    return subprocess.check_output(["git", "rev-parse", "--show-toplevel"]).decode().strip()


def header(ext: str) -> list[str]:
    if ext in HASH:
        return [f"# {SPDX}", f"# {HOLDER}"]
    if ext in SLASH:
        return [f"// {SPDX}", f"// {HOLDER}"]
    return [f"/* {SPDX} */", f"/* {HOLDER} */"]


def tracked_source(root: str) -> list[str]:
    out = subprocess.check_output(["git", "-C", root, "ls-files"]).decode().splitlines()
    return [f for f in out if os.path.splitext(f)[1] in EXTS]


def main() -> int:
    fix = "--fix" in sys.argv
    root = repo_root()
    missing: list[str] = []
    fixed = 0

    for rel in tracked_source(root):
        path = os.path.join(root, rel)
        try:
            with open(path, encoding="utf-8") as f:
                content = f.read()
        except (UnicodeDecodeError, FileNotFoundError):
            continue
        if not content.strip():
            continue  # empty file (e.g. blank __init__.py)
        if SPDX in content[:600]:
            continue

        if not fix:
            missing.append(rel)
            continue

        lines = content.split("\n")
        at = 1 if lines and lines[0].startswith("#!") else 0  # keep shebang first
        pre, rest = lines[:at], lines[at:]
        new = pre + header(os.path.splitext(rel)[1])
        if rest and rest[0].strip():
            new += [""]
        new += rest
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(new))
        fixed += 1

    if fix:
        print(f"Added headers to {fixed} file(s).")
        return 0
    if missing:
        print(f"::error::{len(missing)} source file(s) missing the SPDX license header:")
        for m in missing:
            print(f"  {m}")
        print("\nFix with:  python3 scripts/check_license_headers.py --fix")
        return 1
    print("OK — all tracked source files carry the SPDX license header.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
