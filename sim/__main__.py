"""`python -m sim` 的入口（把 `sim.cli.main` 接出来）。"""

from __future__ import annotations

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
