from __future__ import annotations

from pathlib import Path


def parse_allowed_roots(raw: str) -> list[str]:
    roots = []
    for item in raw.split(","):
        item = item.strip()
        if item:
            roots.append(item)
    return roots


def is_path_allowed(path: str, allowed_roots: list[str]) -> bool:
    try:
        target = Path(path).resolve()
    except Exception:
        return False
    for root in allowed_roots:
        try:
            root_path = Path(root).resolve()
        except Exception:
            continue
        if str(target).startswith(str(root_path)):
            return True
    return False


def needs_delete_approval(text: str, keywords: list[str]) -> bool:
    lower = text.lower()
    return any(k in lower for k in keywords if k)
