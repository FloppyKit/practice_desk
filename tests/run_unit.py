"""Tiny runner so we do not need pytest on the box."""
from __future__ import annotations

import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests import test_demo_store, test_oss_hygiene, test_staff_auth, test_week_people  # noqa: E402


def main() -> int:
    failed = 0
    ran = 0
    for mod in (test_week_people, test_staff_auth, test_oss_hygiene, test_demo_store):
        for name in sorted(dir(mod)):
            if not name.startswith("test_"):
                continue
            fn = getattr(mod, name)
            if not callable(fn):
                continue
            ran += 1
            try:
                fn()
                print("ok", mod.__name__ + "." + name)
            except Exception:
                failed += 1
                print("FAIL", mod.__name__ + "." + name)
                traceback.print_exc()
    print("ran", ran, "failed", failed)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
