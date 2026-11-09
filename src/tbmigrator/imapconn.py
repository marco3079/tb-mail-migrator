"""Cliente IMAP enxuto sobre o imaplib, com reconexão automática."""

from __future__ import annotations

import datetime as dt
import imaplib
import re
import socket
import ssl
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from . import imap_utf7

imaplib._MAXLINE = 100_000_000  # respostas grandes (FETCH de muitos cabeçalhos)

RETRYABLE = (imaplib.IMAP4.abort, OSError, ssl.SSLError, socket.timeout, EOFError)

PROVIDERS = {
    "Gmail / Google Workspace": ("imap.gmail.com", 993, "ssl"),
    "Microsoft 365 / Outlook.com": ("outlook.office365.com", 993, "ssl"),
    "Yahoo Mail": ("imap.mail.yahoo.com", 993, "ssl"),
    "iCloud Mail": ("imap.mail.me.com", 993, "ssl"),
    "Zoho Mail": ("imap.zoho.com", 993, "ssl"),
    "Personalizado": ("", 993, "ssl"),
}

_LIST_RE = re.compile(rb'^\((?P<flags>[^)]*)\)\s+(?P<delim>"(?:\\.|[^"\\])*"|NIL)\s*(?P<name>.*)$', re.I)


class ImapError(Exception):
    pass


@dataclass
class ImapParams:
    host: str
    port: int = 993
    security: str = "ssl"  # ssl | starttls | none
    username: str = ""
    password: str = ""
    timeout: int = 120

    def describe(self) -> str:
        sec = {"ssl": "SSL/TLS", "starttls": "STARTTLS", "none": "sem criptografia"}.get(self.security, self.security)
        return f"{self.username} @ {self.host}:{self.port} ({sec})"


@dataclass
class FolderInfo:
    name: str          # nome decodificado (Unicode)
    raw: str           # nome como o servidor envia (modified UTF-7)
    delimiter: Optional[str]
    flags: set = field(default_factory=set)
    messages: Optional[int] = None

    @property
    def selectable(self) -> bool:
        return "\\noselect" not in self.flags and "\\nonexistent" not in self.flags

    @property
    def special(self) -> Optional[str]:
        for flag in ("\\All", "\\Archive", "\\Drafts", "\\Flagged", "\\Important", "\\Junk", "\\Sent", "\\Trash"):
            if flag.lower() in self.flags:
                return flag
        return "INBOX" if self.name.upper() == "INBOX" else None

    def parts(self) -> tuple:
        if self.delimiter:
            return tuple(p for p in self.name.split(self.delimiter) if p != "")
        return (self.name,)


@dataclass
class FetchedMessage:
    uid: int
    flags: list
    internal_date: Optional[dt.datetime]
    data: bytes


# --------------------------------------------------------------------------- parsing

def quote(name: str) -> str:
    encoded = imap_utf7.encode(name)
    return '"' + encoded.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _unquote(value: bytes) -> bytes:
    value = value.strip()
    if len(value) >= 2 and value[:1] == b'"' and value[-1:] == b'"':
        value = re.sub(rb"\\(.)", rb"\1", value[1:-1])
    return value


def parse_list_response(data: list) -> list[FolderInfo]:
    folders: list[FolderInfo] = []
    for item in data:
        if item is None:
            continue
        literal_name = None
        if isinstance(item, tuple):
            line, literal_name = item[0], item[1]
        else:
            line = item
        match = _LIST_RE.match(line.strip())
        if not match:
            continue
        flags = {f.lower() for f in match.group("flags").decode("ascii", "replace").split()}
        delim_raw = match.group("delim")
        delimiter = None if delim_raw.upper() == b"NIL" else _unquote(delim_raw).decode("ascii", "replace")
        raw_name = literal_name if literal_name is not None else _unquote(match.group("name"))
        raw = raw_name.decode("utf-8", "replace")
        folders.append(FolderInfo(name=imap_utf7.decode(raw), raw=raw, delimiter=delimiter, flags=flags))
    return folders


def parse_fetch_response(data: list) -> list[FetchedMessage]:
    out: list[FetchedMessage] = []
    i = 0
    while i < len(data):
        item = data[i]
        if isinstance(item, tuple):
            meta, body = item[0], item[1]
            if i + 1 < len(data) and isinstance(data[i + 1], bytes):
                meta += data[i + 1]
                i += 1
            uid = re.search(rb"UID (\d+)", meta)
            flags = re.search(rb"FLAGS \(([^)]*)\)", meta)
            idate = re.search(rb'INTERNALDATE "([^"]+)"', meta)
            when = None
            if idate:
                tup = imaplib.Internaldate2tuple(b'INTERNALDATE "' + idate.group(1) + b'"')
                if tup:
                    when = dt.datetime.fromtimestamp(time.mktime(tup)).astimezone()
            out.append(FetchedMessage(
                uid=int(uid.group(1)) if uid else 0,
                flags=flags.group(1).decode("ascii", "replace").split() if flags else [],
                internal_date=when,
                data=body or b"",
            ))
        i += 1
    return out


def _text(resp) -> str:
    parts = []
    for r in resp or []:
        if isinstance(r, tuple):
            r = r[0]
        if isinstance(r, bytes):
            parts.append(r.decode("utf-8", "replace"))
        elif r is not None:
            parts.append(str(r))
    return " ".join(parts)


# --------------------------------------------------------------------------- conexão

class ImapConn:
    def __init__(self, params: ImapParams, log: Optional[Callable[[str], None]] = None):
        self.p = params
        self.m: Optional[imaplib.IMAP4] = None
        self.selected: Optional[tuple] = None
        self.log = log or (lambda _msg: None)

    # -- ciclo de vida
    def connect(self) -> None:
        ctx = ssl.create_default_context()
        try:
            if self.p.security == "ssl":
                m = imaplib.IMAP4_SSL(self.p.host, self.p.port, ssl_context=ctx, timeout=self.p.timeout)
            else:
                m = imaplib.IMAP4(self.p.host, self.p.port, timeout=self.p.timeout)
                if self.p.security == "starttls":
                    m.starttls(ssl_context=ctx)
        except (OSError, imaplib.IMAP4.error) as exc:
            raise ImapError(f"Não foi possível conectar em {self.p.host}:{self.p.port} - {exc}") from exc
        try:
            m.login(self.p.username, self.p.password)
        except imaplib.IMAP4.error as exc:
            try:
                m.shutdown()
            except Exception:
                pass
            msg = str(exc)
            hint = ""
            if "gmail" in self.p.host or "google" in msg.lower():
                hint = " (Gmail/Workspace exige uma 'senha de app': myaccount.google.com/apppasswords)"
            raise ImapError(f"Falha no login de {self.p.username}: {msg}{hint}") from exc
        self.m = m
        if self.selected:
            name, readonly = self.selected
            self.selected = None
            self.select(name, readonly)

    def close(self) -> None:
        if self.m is not None:
            try:
                self.m.logout()
            except Exception:
                pass
        self.m = None

    def reconnect(self) -> None:
        self.close()
        self.connect()

    def _retry(self, what: str, fn, *args):
        last = None
        for attempt in range(4):
            try:
                if self.m is None:
                    self.connect()
                return fn(*args)
            except RETRYABLE as exc:
                last = exc
                self.log(f"   conexão interrompida ({what}: {exc}); reconectando ({attempt + 1}/3)...")
                time.sleep(2 * (attempt + 1))
                try:
                    self.close()
                except Exception:
                    pass
                self.m = None
        raise ImapError(f"Falha de conexão persistente em {what}: {last}")

    # -- comandos
    def list_folders(self) -> list[FolderInfo]:
        typ, data = self._retry("LIST", self.m_call, "list")
        if typ != "OK":
            raise ImapError("O servidor recusou o comando LIST: " + _text(data))
        return parse_list_response(data)

    def m_call(self, method: str, *args):
        return getattr(self.m, method)(*args)

    def delimiter(self) -> str:
        typ, data = self._retry("LIST", self.m_call, "list", '""', '""')
        if typ == "OK":
            found = parse_list_response(data)
            if found and found[0].delimiter:
                return found[0].delimiter
        return "/"

    def status_messages(self, folder: FolderInfo) -> Optional[int]:
        typ, data = self._retry("STATUS", self.m_call, "status", quote(folder.name), "(MESSAGES)")
        if typ != "OK":
            return None
        match = re.search(rb"MESSAGES (\d+)", b" ".join(d if isinstance(d, bytes) else d[0] for d in data if d))
        return int(match.group(1)) if match else None

    def select(self, name: str, readonly: bool = True) -> int:
        def do():
            typ, data = self.m.select(quote(name), readonly=readonly)
            if typ != "OK":
                raise ImapError(f"Não foi possível abrir a pasta '{name}': {_text(data)}")
            return int(data[0] or 0) if data and data[0] and data[0].isdigit() else 0
        count = self._retry("SELECT", do)
        self.selected = (name, readonly)
        return count

    def uids(self) -> list[int]:
        typ, data = self._retry("SEARCH", self.m_call, "uid", "SEARCH", "ALL")
        if typ != "OK":
            raise ImapError("Falha no SEARCH: " + _text(data))
        return [int(x) for x in (data[0] or b"").split()]

    def fetch(self, uids: list[int]) -> list[FetchedMessage]:
        if not uids:
            return []
        uid_set = ",".join(str(u) for u in uids)
        typ, data = self._retry("FETCH", self.m_call, "uid", "FETCH", uid_set,
                                "(UID FLAGS INTERNALDATE BODY.PEEK[])")
        if typ != "OK":
            raise ImapError("Falha no FETCH: " + _text(data))
        return parse_fetch_response(data)

    def message_ids(self, name: str) -> set:
        from .mbox import message_id, parse_headers
        count = self.select(name, readonly=True)
        ids: set = set()
        if count == 0:
            return ids
        typ, data = self._retry("FETCH", self.m_call, "uid", "FETCH", "1:*",
                                "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])")
        if typ != "OK":
            return ids
        for item in data:
            if isinstance(item, tuple) and item[1]:
                mid = message_id(parse_headers(item[1]))
                if mid:
                    ids.add(mid)
        return ids

    def create(self, name: str) -> None:
        typ, data = self._retry("CREATE", self.m_call, "create", quote(name))
        text = _text(data).lower()
        if typ != "OK" and "exist" not in text and "already" not in text:
            raise ImapError(f"Não foi possível criar a pasta '{name}': {_text(data)}")
        try:
            self._retry("SUBSCRIBE", self.m_call, "subscribe", quote(name))
        except Exception:
            pass

    def append(self, name: str, flags: list, when: Optional[dt.datetime], data: bytes) -> None:
        flag_str = "(" + " ".join(flags) + ")" if flags else None
        date_str = imaplib.Time2Internaldate(when) if when else None
        typ, resp = self._retry("APPEND", self.m_call, "append", quote(name), flag_str, date_str, data)
        if typ != "OK":
            raise ImapError(f"O servidor recusou a mensagem em '{name}': {_text(resp)}")
