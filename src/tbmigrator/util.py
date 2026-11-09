"""Utilidades compartilhadas: relatório de progresso, cancelamento e nomes de arquivo."""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from typing import Callable

_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

GMAIL_ROOTS = {"[gmail]", "[google mail]"}


class Cancelled(Exception):
    pass


@dataclass
class Reporter:
    log: Callable[[str], None] = print
    progress: Callable[[float, str], None] = lambda frac, text: None
    cancel: threading.Event = field(default_factory=threading.Event)

    def check(self) -> None:
        if self.cancel.is_set():
            raise Cancelled()


def safe_name(name: str, max_len: int = 120) -> str:
    cleaned = _ILLEGAL.sub("_", name).strip().rstrip(". ")
    if not cleaned:
        cleaned = "_"
    if cleaned.upper().split(".")[0] in _RESERVED:
        cleaned = "_" + cleaned
    return cleaned[:max_len]


def human_size(num: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if num < 1024 or unit == "GB":
            return f"{num:.0f} {unit}" if unit == "B" else f"{num:.1f} {unit}"
        num /= 1024
    return f"{num:.1f} TB"


def drop_gmail_root(parts: tuple) -> tuple:
    return tuple(p for p in parts if p.casefold() not in GMAIL_ROOTS)


SPECIAL_NAMES = {
    "\\Sent": {"sent", "sent items", "sent mail", "sent messages", "enviados", "itens enviados",
               "e-mails enviados", "emails enviados", "mensagens enviadas", "enviadas"},
    "\\Drafts": {"drafts", "draft", "rascunhos", "rascunho"},
    "\\Trash": {"trash", "deleted items", "deleted messages", "lixeira", "itens excluídos",
                "itens excluidos", "lixo", "bin"},
    "\\Junk": {"junk", "spam", "junk e-mail", "junk email", "lixo eletrônico", "lixo eletronico", "bulk mail"},
    "\\Archive": {"archive", "archives", "arquivo", "arquivos", "arquivados"},
}


def classify_special(parts: tuple, special: str | None = None) -> str | None:
    """Identifica Entrada/Enviados/Rascunhos/Lixeira/Spam/Arquivo pelo flag IMAP ou pelo nome."""
    if special in SPECIAL_NAMES or special == "INBOX":
        return special
    parts = drop_gmail_root(parts)
    if len(parts) != 1:
        return None
    name = parts[0].casefold()
    if name == "inbox":
        return "INBOX"
    for key, names in SPECIAL_NAMES.items():
        if name in names:
            return key
    return None
