"""Shared fail-closed, non-sensitive CLI boundary for Phase 11 tools."""
from __future__ import annotations

import json
import sys
from collections.abc import Callable


def run_safely(main: Callable[[], int], error_code: str) -> int:
    """Convert operational exceptions to one stable aggregate-only error."""
    try:
        return main()
    except Exception:
        print(json.dumps({"status": "failed", "error_code": error_code}), file=sys.stderr)
        return 2
