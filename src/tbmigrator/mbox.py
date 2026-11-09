"""Leitura e escrita de mensagens: mbox (formato do Thunderbird), maildir e .eml.

O leitor é feito em streaming (linha a linha), então funciona com caixas de vários GB
sem carregar o arquivo inteiro na memória.
"""

from __future__ import annotations

import datetime as dt
import email.header
import email.utils
import os
import re
import time
from dataclasses import dataclass
from email import policy
from email.parser import BytesHeaderParser
from typing import Callable, Iterator, Optional

# Flags gravadas pelo Thunderbird no cabeçalho X-Mozilla-Status (nsMsgMessageFlags)
FLAG_READ = 0x0001
FLAG_REPLIED = 0x0002
FLAG_MARKED = 0x0004
FLAG_EXPUNGED = 0x0008
FLAG_PARTIAL = 0x0400
FLAG_FORWARDED = 0x1000

_SEPARATOR = re.compile(rb"^From \S+.*\d{1,2}:\d{2}.*\d{4}\s*$")
_ESCAPED_FROM = re.compile(rb"^>(>*From )", re.M)
_NEEDS_ESCAPE = re.compile(rb"^(>*From )", re.M)
_MOZ_HEADER = re.compile(rb"^X-Mozilla-(Status2?|Keys)\s*:", re.I)

_DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


@dataclass
class RawMessage:
    data: bytes            # mensagem RFC 822 (cabeçalhos + corpo)
    from_line: bytes = b""  # linha separadora "From ..." original, se houver
    source_name: str = ""


# --------------------------------------------------------------------------- leitura

def iter_mbox(path: str, progress: Optional[Callable[[int], None]] = None) -> Iterator[RawMessage]:
    """Percorre um arquivo mbox e devolve cada mensagem.

    `progress` recebe o número de bytes lidos desde a chamada anterior.
    """
    with open(path, "rb") as fh:
        lines: list[bytes] = []
        from_line: Optional[bytes] = None
        prev_blank = True
        first = True
        pending = 0
        for line in fh:
            pending += len(line)
            if line.startswith(b"From ") and prev_blank and (first or _SEPARATOR.match(line.rstrip())):
                if from_line is not None:
                    yield _finish(from_line, lines)
                    if progress:
                        progress(pending)
                        pending = 0
                from_line = line.rstrip(b"\r\n")
                lines = []
            else:
                lines.append(line)
            first = False
            prev_blank = line in (b"\n", b"\r\n")
        if from_line is not None:
            yield _finish(from_line, lines)
        if progress and pending:
            progress(pending)


def _finish(from_line: bytes, lines: list[bytes]) -> RawMessage:
    if lines and lines[-1] in (b"\n", b"\r\n"):
        lines = lines[:-1]
    data = b"".join(lines)
    if b">From " in data:
        data = _ESCAPED_FROM.sub(rb"\1", data)
    return RawMessage(data=data, from_line=from_line)


def iter_maildir(path: str, progress: Optional[Callable[[int], None]] = None) -> Iterator[RawMessage]:
    for sub in ("cur", "new"):
        folder = os.path.join(path, sub)
        if not os.path.isdir(folder):
            continue
        for name in sorted(os.listdir(folder)):
            full = os.path.join(folder, name)
            if os.path.isfile(full):
                with open(full, "rb") as fh:
                    data = fh.read()
                if progress:
                    progress(len(data))
                yield RawMessage(data=data, source_name=name)


def iter_eml_dir(path: str, progress: Optional[Callable[[int], None]] = None) -> Iterator[RawMessage]:
    for name in sorted(os.listdir(path)):
        if name.lower().endswith(".eml"):
            with open(os.path.join(path, name), "rb") as fh:
                data = fh.read()
            if progress:
                progress(len(data))
            yield RawMessage(data=data, source_name=name)


def looks_like_mbox(path: str) -> bool:
    try:
        with open(path, "rb") as fh:
            return fh.read(5) == b"From "
    except OSError:
        return False


# --------------------------------------------------------------------------- cabeçalhos

def header_block(data: bytes) -> bytes:
    candidates = [i for i in (data.find(b"\r\n\r\n"), data.find(b"\n\n")) if i != -1]
    return data[:min(candidates)] if candidates else data


def parse_headers(data: bytes):
    return BytesHeaderParser(policy=policy.compat32).parsebytes(header_block(data) + b"\n\n")


def mozilla_flags(headers) -> Optional[int]:
    value = headers.get("X-Mozilla-Status")
    if not value:
        return None
    try:
        return int(str(value).strip()[:8], 16)
    except ValueError:
        return None


def message_id(headers) -> str:
    value = headers.get("Message-ID") or headers.get("Message-Id") or ""
    return str(value).strip()


def subject(headers) -> str:
    value = headers.get("Subject") or ""
    try:
        parts = email.header.decode_header(str(value))
        return str(email.header.make_header(parts)).strip()
    except Exception:
        return str(value).strip()


def message_date(headers, from_line: bytes = b"") -> dt.datetime:
    """Data da mensagem com fuso: cabeçalho Date, depois a linha From, depois agora."""
    value = headers.get("Date")
    if value:
        try:
            parsed = email.utils.parsedate_to_datetime(str(value))
            if parsed is not None:
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=dt.timezone.utc)
                if 1971 <= parsed.year <= 2100:
                    return parsed
        except Exception:
            pass
    if from_line:
        tokens = from_line.decode("latin-1").split()
        if len(tokens) >= 5:
            try:
                st = time.strptime(" ".join(tokens[-5:]), "%a %b %d %H:%M:%S %Y")
                return dt.datetime.fromtimestamp(time.mktime(st)).astimezone()
            except (ValueError, OverflowError):
                pass
    return dt.datetime.now().astimezone()


def imap_flags(headers) -> list[str]:
    """Traduz as flags do Thunderbird (ou do cabeçalho Status do mbox) para flags IMAP."""
    flags: list[str] = []
    moz = mozilla_flags(headers)
    if moz is not None:
        if moz & FLAG_READ:
            flags.append("\\Seen")
        if moz & FLAG_REPLIED:
            flags.append("\\Answered")
        if moz & FLAG_MARKED:
            flags.append("\\Flagged")
        return flags
    status = str(headers.get("Status") or "")
    xstatus = str(headers.get("X-Status") or "")
    if "R" in status:
        flags.append("\\Seen")
    if "A" in xstatus:
        flags.append("\\Answered")
    if "F" in xstatus:
        flags.append("\\Flagged")
    return flags


def flags_to_mozilla(imap: list[str]) -> int:
    lowered = {f.lower() for f in imap}
    value = 0
    if "\\seen" in lowered:
        value |= FLAG_READ
    if "\\answered" in lowered:
        value |= FLAG_REPLIED
    if "\\flagged" in lowered:
        value |= FLAG_MARKED
    if "$forwarded" in lowered:
        value |= FLAG_FORWARDED
    return value


def strip_mozilla_headers(data: bytes) -> bytes:
    head = header_block(data)
    rest = data[len(head):]
    kept: list[bytes] = []
    skipping = False
    for line in head.splitlines(keepends=True):
        if line[:1] in (b" ", b"\t") and skipping:
            continue
        skipping = bool(_MOZ_HEADER.match(line))
        if not skipping:
            kept.append(line)
    return b"".join(kept) + rest


def set_mozilla_status(data: bytes, flags: int) -> bytes:
    nl = b"\r\n" if b"\r\n" in data[:4096] else b"\n"
    header = b"X-Mozilla-Status: %04x" % (flags & 0xFFFF) + nl
    header += b"X-Mozilla-Status2: 00000000" + nl
    return header + strip_mozilla_headers(data)


# --------------------------------------------------------------------------- escrita

def from_line_for(date: Optional[dt.datetime]) -> bytes:
    d = (date or dt.datetime.now().astimezone()).astimezone()
    text = "From - %s %s %02d %02d:%02d:%02d %04d" % (
        _DAYS[d.weekday()], _MONTHS[d.month - 1], d.day, d.hour, d.minute, d.second, d.year)
    return text.encode("ascii")


class MboxWriter:
    """Grava mensagens em um arquivo mbox compatível com o Thunderbird."""

    def __init__(self, path: str, append: bool = False):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.path = path
        self._fh = open(path, "ab" if append else "wb")
        self.count = 0

    def add(self, data: bytes, date: Optional[dt.datetime] = None, from_line: bytes = b"") -> None:
        nl = b"\r\n" if b"\r\n" in data[:4096] else b"\n"
        if not (from_line and _SEPARATOR.match(from_line)):
            from_line = from_line_for(date)
        body = _NEEDS_ESCAPE.sub(rb">\1", data)
        if not body.endswith(b"\n"):
            body += nl
        self._fh.write(from_line + nl + body + nl)
        self.count += 1

    def close(self) -> None:
        self._fh.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
