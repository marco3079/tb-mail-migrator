"""Codificação "modified UTF-7" usada nos nomes de pastas IMAP (RFC 3501, seção 5.1.3)."""

from __future__ import annotations

import base64


def encode(name: str) -> str:
    """Converte um nome de pasta Unicode para modified UTF-7."""
    out: list[str] = []
    pending: list[str] = []

    def flush() -> None:
        if pending:
            raw = "".join(pending).encode("utf-16-be")
            b64 = base64.b64encode(raw).decode("ascii").rstrip("=").replace("/", ",")
            out.append("&" + b64 + "-")
            pending.clear()

    for ch in name:
        if 0x20 <= ord(ch) <= 0x7E:
            flush()
            out.append("&-" if ch == "&" else ch)
        else:
            pending.append(ch)
    flush()
    return "".join(out)


def decode(name: str | bytes) -> str:
    """Converte um nome em modified UTF-7 para Unicode."""
    if isinstance(name, bytes):
        name = name.decode("ascii", "replace")
    out: list[str] = []
    i = 0
    while i < len(name):
        ch = name[i]
        if ch != "&":
            out.append(ch)
            i += 1
            continue
        end = name.find("-", i)
        if end == -1:
            out.append(name[i:])
            break
        if end == i + 1:
            out.append("&")
        else:
            chunk = name[i + 1:end].replace(",", "/")
            chunk += "=" * (-len(chunk) % 4)
            try:
                out.append(base64.b64decode(chunk).decode("utf-16-be"))
            except Exception:
                out.append(name[i:end + 1])
        i = end + 1
    return "".join(out)
