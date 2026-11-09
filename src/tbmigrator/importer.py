"""Importação de uma exportação (MBOX/EML) para um servidor IMAP ou para as Pastas Locais do Thunderbird."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Iterator, Optional

from . import mbox
from .exporter import MANIFEST
from .imapconn import ImapConn, ImapParams
from .thunderbird import Account, is_running
from .util import Reporter, classify_special, drop_gmail_root, human_size, safe_name

# Nomes no disco das pastas especiais das Pastas Locais do Thunderbird
LOCAL_SPECIAL = {"INBOX": "Inbox", "\\Sent": "Sent", "\\Drafts": "Drafts", "\\Trash": "Trash",
                 "\\Junk": "Junk", "\\Archive": "Archives"}


@dataclass
class SourceFolder:
    account: str
    parts: tuple
    kind: str            # mbox | eml
    path: str
    size: int
    special: Optional[str] = None
    messages: Optional[int] = None

    @property
    def label(self) -> str:
        return "/".join(self.parts)


# --------------------------------------------------------------------------- origem

def scan_source(root: str) -> list[SourceFolder]:
    """Encontra pastas importáveis: exportações deste app (manifest.json) ou .mbox/.eml soltos."""
    root = os.path.normpath(root)
    manifests = []
    if os.path.isfile(os.path.join(root, MANIFEST)):
        manifests.append(root)
    else:
        for entry in sorted(os.listdir(root)):
            if os.path.isfile(os.path.join(root, entry, MANIFEST)):
                manifests.append(os.path.join(root, entry))
    if manifests:
        found: list[SourceFolder] = []
        for base in manifests:
            found.extend(_from_manifest(base))
        return found
    return _generic_scan(root)


def _from_manifest(base: str) -> list[SourceFolder]:
    with open(os.path.join(base, MANIFEST), encoding="utf-8") as fh:
        manifest = json.load(fh)
    account = manifest.get("account") or os.path.basename(base)
    out = []
    for entry in manifest.get("folders", []):
        parts = tuple(entry.get("parts") or [])
        if not parts:
            continue
        kind, rel = ("mbox", entry.get("mbox")) if entry.get("mbox") else ("eml", entry.get("eml"))
        if not rel:
            continue
        path = os.path.join(base, rel)
        if not os.path.exists(path):
            continue
        size = os.path.getsize(path) if kind == "mbox" else _dir_bytes(path)
        out.append(SourceFolder(account, parts, kind, path, size, entry.get("special"), entry.get("messages")))
    return out


def _generic_scan(root: str) -> list[SourceFolder]:
    account = os.path.basename(root) or root
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        rel_dir = os.path.relpath(dirpath, root)
        dir_parts = () if rel_dir == "." else tuple(p[:-4] if p.endswith(".sbd") else p for p in rel_dir.split(os.sep))
        emls = [f for f in filenames if f.lower().endswith(".eml")]
        if emls:
            out.append(SourceFolder(account, dir_parts or (account,), "eml", dirpath,
                                    sum(os.path.getsize(os.path.join(dirpath, f)) for f in emls)))
        for name in sorted(filenames):
            full = os.path.join(dirpath, name)
            stem, ext = os.path.splitext(name)
            if ext.lower() == ".mbox" or (ext == "" and mbox.looks_like_mbox(full)):
                out.append(SourceFolder(account, dir_parts + (stem if ext else name,), "mbox", full,
                                        os.path.getsize(full)))
    return out


def _dir_bytes(path: str) -> int:
    return sum(os.path.getsize(os.path.join(path, f)) for f in os.listdir(path)
               if os.path.isfile(os.path.join(path, f)))


def iter_source(sf: SourceFolder, progress=None) -> Iterator[mbox.RawMessage]:
    if sf.kind == "mbox":
        return mbox.iter_mbox(sf.path, progress)
    return mbox.iter_eml_dir(sf.path, progress)


# --------------------------------------------------------------------------- destino IMAP

def import_imap(sources: list[SourceFolder], params: ImapParams, prefix: str, map_special: bool,
                skip_duplicates: bool, rep: Reporter) -> dict:
    conn = ImapConn(params, rep.log)
    rep.log(f"Conectando em {params.describe()}...")
    conn.connect()
    totals = {"folders": 0, "messages": 0, "duplicates": 0, "deleted": 0, "errors": 0}
    try:
        delim = conn.delimiter() or "/"
        server_folders = conn.list_folders()
        existing = {f.name.casefold() for f in server_folders}
        special_map = {}
        for f in server_folders:
            sp = f.special
            if sp and sp not in special_map and f.selectable:
                special_map[sp] = f.name
        for f in server_folders:  # fallback por nome quando o servidor não informa SPECIAL-USE
            key = classify_special(f.parts())
            if key and key not in special_map and f.selectable:
                special_map[key] = f.name
        special_map.setdefault("INBOX", "INBOX")
        prefix_parts = tuple(p for p in prefix.replace("\\", "/").split("/") if p.strip())

        total_bytes = sum(s.size for s in sources) or 1
        done_bytes = 0
        rep.log(f"Importando {len(sources)} pasta(s) ({human_size(total_bytes)}) para {params.host}")

        for sf in sources:
            rep.check()
            target = _imap_target(sf, prefix_parts, delim, special_map if map_special else {})
            comps = target.split(delim)
            for i in range(1, len(comps) + 1):
                name = delim.join(comps[:i])
                if name.casefold() not in existing and name.upper() != "INBOX":
                    conn.create(name)
                    existing.add(name.casefold())
                    rep.log(f"   pasta criada: {name}")
            rep.log(f"> {sf.label}  ->  {target}")
            known = conn.message_ids(target) if skip_duplicates else set()
            if known:
                rep.log(f"   {len(known)} mensagens já existem no destino")
            sent = dup = deleted = 0

            def on_bytes(n: int) -> None:
                nonlocal done_bytes
                done_bytes += n

            for msg in iter_source(sf, on_bytes):
                rep.check()
                headers = mbox.parse_headers(msg.data)
                moz = mbox.mozilla_flags(headers)
                if moz is not None and moz & mbox.FLAG_EXPUNGED:
                    deleted += 1
                    continue
                mid = mbox.message_id(headers)
                if skip_duplicates and mid and mid in known:
                    dup += 1
                    continue
                data = mbox.strip_mozilla_headers(msg.data)
                try:
                    conn.append(target, mbox.imap_flags(headers), mbox.message_date(headers, msg.from_line), data)
                except Exception as exc:
                    totals["errors"] += 1
                    rep.log(f"   ERRO ao enviar '{mbox.subject(headers)[:60]}': {exc}")
                    continue
                if mid:
                    known.add(mid)
                sent += 1
                rep.progress(min(done_bytes / total_bytes, 1.0), f"{target}: {sent} enviadas")
            info = f"   {sent} mensagens enviadas"
            if dup:
                info += f", {dup} duplicadas ignoradas"
            rep.log(info)
            totals["folders"] += 1
            totals["messages"] += sent
            totals["duplicates"] += dup
            totals["deleted"] += deleted
    finally:
        conn.close()
    rep.progress(1.0, "Importação concluída")
    return totals


def _imap_target(sf: SourceFolder, prefix_parts: tuple, delim: str, special_map: dict) -> str:
    parts = drop_gmail_root(sf.parts) or sf.parts
    if not prefix_parts and special_map:
        key = classify_special(sf.parts, sf.special)
        if key in special_map:
            return special_map[key]
    if not prefix_parts and len(parts) == 1 and parts[0].upper() == "INBOX":
        return "INBOX"
    comps = [p.replace(delim, "_") for p in prefix_parts + parts]
    return delim.join(comps)


# --------------------------------------------------------------------------- destino Pastas Locais

def import_local(sources: list[SourceFolder], account: Account, prefix: str, map_special: bool,
                 skip_duplicates: bool, rep: Reporter) -> dict:
    if is_running():
        raise RuntimeError("Feche o Thunderbird antes de importar para uma conta local "
                           "(ele trava os arquivos de pasta enquanto está aberto).")
    base = account.directory
    if not base or not os.path.isdir(base):
        raise RuntimeError(f"Diretório da conta não encontrado: {base}")
    prefix_parts = tuple(p for p in prefix.replace("\\", "/").split("/") if p.strip())
    totals = {"folders": 0, "messages": 0, "duplicates": 0, "deleted": 0, "errors": 0}
    total_bytes = sum(s.size for s in sources) or 1
    done_bytes = 0
    rep.log(f"Importando {len(sources)} pasta(s) para '{account.name}' ({base})")

    for sf in sources:
        rep.check()
        parts = drop_gmail_root(sf.parts) or sf.parts
        key = classify_special(sf.parts, sf.special) if map_special and not prefix_parts else None
        if key in LOCAL_SPECIAL:
            parts = (LOCAL_SPECIAL[key],)
        parts = tuple(safe_name(p) for p in prefix_parts + parts)
        path = _ensure_local_path(base, parts)
        rep.log(f"> {sf.label}  ->  {'/'.join(parts)}")

        known: set = set()
        if skip_duplicates and os.path.getsize(path) > 0:
            for existing in mbox.iter_mbox(path):
                mid = mbox.message_id(mbox.parse_headers(existing.data))
                if mid:
                    known.add(mid)
        added = dup = deleted = 0

        def on_bytes(n: int) -> None:
            nonlocal done_bytes
            done_bytes += n

        with mbox.MboxWriter(path, append=True) as writer:
            for msg in iter_source(sf, on_bytes):
                rep.check()
                headers = mbox.parse_headers(msg.data)
                moz = mbox.mozilla_flags(headers)
                if moz is not None and moz & mbox.FLAG_EXPUNGED:
                    deleted += 1
                    continue
                mid = mbox.message_id(headers)
                if skip_duplicates and mid and mid in known:
                    dup += 1
                    continue
                writer.add(msg.data, mbox.message_date(headers, msg.from_line), msg.from_line)
                if mid:
                    known.add(mid)
                added += 1
                if added % 50 == 0:
                    rep.progress(min(done_bytes / total_bytes, 1.0), f"{'/'.join(parts)}: {added}")
        # Remove o índice para o Thunderbird reconstruir a pasta com as novas mensagens
        msf = path + ".msf"
        if os.path.exists(msf):
            os.remove(msf)
        info = f"   {added} mensagens gravadas"
        if dup:
            info += f", {dup} duplicadas ignoradas"
        rep.log(info)
        totals["folders"] += 1
        totals["messages"] += added
        totals["duplicates"] += dup
        totals["deleted"] += deleted
    rep.progress(1.0, "Importação concluída")
    return totals


def _ensure_local_path(base: str, parts: tuple) -> str:
    current = base
    path = base
    for i, part in enumerate(parts):
        path = os.path.join(current, part)
        if not os.path.exists(path):
            open(path, "wb").close()
        if i < len(parts) - 1:
            current = os.path.join(current, part + ".sbd")
            os.makedirs(current, exist_ok=True)
    return path
