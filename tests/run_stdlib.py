#!/usr/bin/env python3
"""Run all tests using only the Python standard library (no pytest required)."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent


def main() -> int:
    passed = 0
    failed = 0
    for test_file in sorted(TESTS_DIR.glob("test_*.py")):
        spec = importlib.util.spec_from_file_location(test_file.stem, test_file)
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        for name in sorted(dir(mod)):
            if not name.startswith("test_"):
                continue
            fn = getattr(mod, name)
            if not callable(fn):
                continue
            try:
                fn()
                print(f"PASS  {test_file.name}::{name}")
                passed += 1
            except Exception as exc:
                print(f"FAIL  {test_file.name}::{name}: {exc}", file=sys.stderr)
                failed += 1
    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
