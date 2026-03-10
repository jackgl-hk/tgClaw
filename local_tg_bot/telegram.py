from __future__ import annotations

from typing import Iterable
import json
import os
import sys
import subprocess
from datetime import datetime
import httpx

_FORCE_CURL = False


def set_force_curl(value: bool) -> None:
    global _FORCE_CURL
    _FORCE_CURL = bool(value)


def split_text(text: str, max_chars: int) -> Iterable[str]:
    if max_chars <= 0:
        yield text
        return
    for i in range(0, len(text), max_chars):
        yield text[i : i + max_chars]


def post(
    base_url: str,
    path: str,
    payload: dict,
    timeout: int = 30,
    proxy: str | None = None,
) -> dict | None:
    url = base_url + path
    if _FORCE_CURL:
        return _post_with_curl(url, payload, timeout, proxy, f"{path} forced-curl")
    try:
        with httpx.Client(timeout=timeout, proxy=proxy, trust_env=True) as client:
            resp = client.post(url, json=payload)
            if resp.status_code != 200:
                _log(f"{path} status={resp.status_code} body={resp.text[:200]!r}")
                return _post_with_curl(url, payload, timeout, proxy, f"{path} status")
            data = resp.json()
            if not data.get("ok"):
                _log(f"{path} ok=false desc={data.get('description')!r}")
                return _post_with_curl(url, payload, timeout, proxy, f"{path} ok=false")
            return data
    except Exception as exc:
        _log(f"{path} error={exc!r}")
        return _post_with_curl(url, payload, timeout, proxy, f"{path} exception")


def _log(message: str) -> None:
    ts = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    print(f"[{ts}] telegram {message}", file=sys.stderr)


def _post_with_curl(
    url: str,
    payload: dict,
    timeout: int,
    proxy: str | None,
    reason: str,
) -> dict | None:
    curl_bin = "/usr/bin/curl"
    args = [
        curl_bin,
        "-sS",
        "--max-time",
        str(timeout),
        "-H",
        "Content-Type: application/json",
        "-d",
        json.dumps(payload),
        "-w",
        "\n%{http_code}",
    ]
    if proxy:
        args += ["-x", proxy]
    args.append(url)
    try:
        proc = subprocess.run(args, capture_output=True, text=True)
    except Exception as exc:
        _log(f"{reason} curl error={exc!r}")
        return None
    if proc.returncode != 0:
        _log(f"{reason} curl exit={proc.returncode} err={proc.stderr.strip()!r}")
        return None
    output = proc.stdout
    if "\n" not in output:
        _log(f"{reason} curl no status line")
        return None
    body, status_line = output.rsplit("\n", 1)
    status_line = status_line.strip()
    if not status_line.isdigit():
        _log(f"{reason} curl bad status={status_line!r}")
        return None
    status = int(status_line)
    if status != 200:
        _log(f"{reason} curl status={status} body={body[:200]!r}")
        return None
    try:
        data = json.loads(body)
    except Exception as exc:
        _log(f"{reason} curl json error={exc!r} body={body[:200]!r}")
        return None
    if not data.get("ok"):
        _log(f"{reason} curl ok=false desc={data.get('description')!r}")
        return None
    _log(f"{reason} curl ok=true")
    return data
