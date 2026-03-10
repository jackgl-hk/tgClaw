from __future__ import annotations

import re
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
    lower = " ".join(text.lower().split())
    if not lower:
        return False

    safe_patterns = (
        r"\bflutter clean\b",
        r"\bdart fix\b",
        r"\brm\s+-rf\s+(build|dist|\.dart_tool|\.turbo|\.next|deriveddata|pods)\b",
        r"\b(delete|remove|clean)\s+(the\s+)?(build|dist|cache|deriveddata|temporary|temp|tmp)\b",
    )
    if any(re.search(pattern, lower) for pattern in safe_patterns):
        return False

    high_risk_patterns = (
        r"\bgit\s+reset\s+--hard\b",
        r"\bgit\s+checkout\s+--\b",
        r"\bgit\s+clean\s+-fdx\b",
        r"\brm\s+-rf\s+(/|~|/users|/volumes|/root|\.git)\b",
        r"\bsudo\s+rm\b",
        r"\bdrop\s+table\b",
        r"\btruncate\s+table\b",
        r"\bdelete\s+from\b",
        r"\b(remove|delete|wipe)\b.*\b(database|db|production|prod|server|backup|secret|key|credential)\b",
    )
    if any(re.search(pattern, lower) for pattern in high_risk_patterns):
        return True

    shell_delete = (
        r"\brm\s+-[^\n]*\b",
        r"\brmdir\b",
        r"\bdel\s+/",
    )
    if any(re.search(pattern, lower) for pattern in shell_delete):
        return True

    sensitive_target = re.search(
        r"\b(delete|remove|trash|wipe|drop|reset)\b.*\b(database|db|server|prod|production|backup|secret|key|credential|table)\b",
        lower,
    )
    if sensitive_target:
        return True

    generic_hit = any(k and k in lower for k in keywords if k)
    if not generic_hit:
        return False

    # Generic wording like "remove unused code" should not block routine development work.
    unsafe_context = (
        "/" in lower
        or "~" in lower
        or ".git" in lower
        or "database" in lower
        or "server" in lower
        or "production" in lower
    )
    return unsafe_context
