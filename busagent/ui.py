"""Small terminal presentation helpers; no third-party dependencies."""

from __future__ import annotations

import os
import sys
from typing import Any

COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None

DIM, BOLD, GREEN, RED, YELLOW, CYAN = "2", "1", "32", "31", "33", "36"


def c(text: Any, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if COLOR else str(text)


def head(title: str) -> None:
    print()
    print(c("─" * 70, DIM))
    print(c(f" {title}", BOLD))
    print(c("─" * 70, DIM))


def kv(key: str, value: Any, ok: bool | None = None) -> None:
    mark = "" if ok is None else c("  ✓" if ok else "  ✗", GREEN if ok else RED)
    print(f"  {c(key.ljust(18), DIM)} {value}{mark}")


def body(text: str, color: str = CYAN) -> None:
    print(c("  " + text.replace("\n", "\n  "), color))
