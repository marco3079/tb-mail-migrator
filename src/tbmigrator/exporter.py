"""Exportação de pastas do Thunderbird (arquivos locais) ou do servidor IMAP para MBOX/EML."""

from __future__ import annotations

import datetime as dt
import json
import os
from typing import Optional

from . import __version__
from . import mbox
from .imapconn import FolderInfo, ImapConn, ImapParams
from .thunderbird import Account, Folder
from .util import Reporter, human_size, safe_name

MANIFEST = "manifest.json"
FETCH_BATCH = 25


class FolderSink:
    """Destino de uma pasta exportada: arquivo .mbox e/ou diretório de .eml."""

    def __init__(self, base: str, parts: tuple, formats: set):
        safe_parts = [safe_name(p) for p in parts]
        self.parts = parts
        self.mbox_rel = None
        self.eml_rel = None
        self.writer = None
        self.eml_dir = None
        if "mbox" in formats:
            self.mbox_rel = os.path.join("mbox", *safe_parts[:-1], safe_parts[-1] + ".mbox")
            self.writer = mbox.MboxWriter(os.path.join(base, self.mbox_rel))
        if "eml" in formats:
            self.eml_rel = os.path.join("eml", *safe_parts)
            self.eml_dir = os.path.join(base, self.eml_rel)
            os.makedirs(self.eml_dir, exist_ok=True)
        self.count = 0
        self.bytes = 0

    def add(self, data: bytes, headers, date: Optional[dt.datetime], from_line: bytes = b"") -> None:
        self.count += 1
        self.bytes += len(data)
        if self.writer:
            self.writer.add(data, date=date, from_line=from_line)
        if self.eml_dir:
            subj = safe_name(mbox.subject(headers) or "sem assunto", 60)
            path = os.path.join(self.eml_dir, f"{self.count:06d} - {subj}.eml")
            with open(path, "wb") as fh:
                fh.write(data)

    def close(self) -> None:
        if self.writer:
            self.writer.close()


def _new_manifest(account_label: str, source: str, extra: dict) -> dict:
    return {
        "tool": "TB Mail Migrator",
        "version": __version__,
        "created": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "account": account_label,
        "source": source,
        **extra,
        "folders": [],
    }


def _save_manifest(base: str, manifest: dict) -> None:
    with open(os.path.join(base, MANIFEST), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2)


# --------------------------------------------------------------------------- arquivos locais

def export_local(account: Account, folders: list[Folder], out_dir: str, formats: set,
                 rep: Reporter) -> dict:
    base = os.path.join(out_dir, safe_name(account.name))
    os.makedirs(base, exist_ok=True)
    manifest = _new_manifest(account.name, "thunderbird-local", {
        "account_type": account.type, "host": account.hostname, "username": account.username,
        "profile": account.profile_name,
    })
    folders = [f for f in folders if f.has_local_data]
    total_bytes = sum(f.size for f in folders) or 1
    done_bytes = 0
    totals = {"folders": 0, "messages": 0, "deleted": 0, "partial": 0}
    rep.log(f"Exportando {len(folders)} pasta(s) de '{account.name}' ({human_size(total_bytes)}) para {base}")

    for folder in folders:
        rep.check()
        label = "/".join(folder.parts)
        rep.log(f"> {label}")
        sink = FolderSink(base, folder.parts, formats)
        deleted = partial = 0

        def on_bytes(n: int) -> None:
            nonlocal done_bytes
            done_bytes += n
            rep.progress(done_bytes / total_bytes, f"{label}: {sink.count} mensagens")

        source = (mbox.iter_maildir(folder.maildir_path, on_bytes) if folder.maildir_path
                  else mbox.iter_mbox(folder.mbox_path, on_bytes))
        try:
            for msg in source:
                rep.check()
                headers = mbox.parse_headers(msg.data)
                flags = mbox.mozilla_flags(headers)
                if flags is not None and flags & mbox.FLAG_EXPUNGED:
                    deleted += 1
                    continue
                if flags is not None and flags & mbox.FLAG_PARTIAL:
                    partial += 1
                sink.add(msg.data, headers, mbox.message_date(headers, msg.from_line), msg.from_line)
        finally:
            sink.close()
        info = f"   {sink.count} mensagens exportadas"
        if deleted:
            info += f", {deleted} excluídas ignoradas"
        if partial:
            info += f", {partial} incompletas (só parte baixada - use a exportação pelo servidor)"
        rep.log(info)
        manifest["folders"].append({
            "parts": list(folder.parts), "special": None, "mbox": sink.mbox_rel, "eml": sink.eml_rel,
            "messages": sink.count, "bytes": sink.bytes, "skipped_deleted": deleted, "partial": partial,
        })
        totals["folders"] += 1
        totals["messages"] += sink.count
        totals["deleted"] += deleted
        totals["partial"] += partial
        _save_manifest(base, manifest)

    rep.progress(1.0, "Exportação concluída")
    totals["path"] = base
    return totals


# --------------------------------------------------------------------------- servidor IMAP

def export_imap(account_label: str, params: ImapParams, folders: list[FolderInfo], out_dir: str,
                formats: set, rep: Reporter) -> dict:
    base = os.path.join(out_dir, safe_name(account_label))
    os.makedirs(base, exist_ok=True)
    manifest = _new_manifest(account_label, "imap", {"host": params.host, "username": params.username})
    conn = ImapConn(params, rep.log)
    rep.log(f"Conectando em {params.describe()}...")
    conn.connect()
    totals = {"folders": 0, "messages": 0, "deleted": 0, "partial": 0}
    try:
        folders = [f for f in folders if f.selectable]
        for f in folders:
            if f.messages is None:
                f.messages = conn.status_messages(f) or 0
        grand_total = sum(f.messages or 0 for f in folders) or 1
        done = 0
        rep.log(f"Baixando {len(folders)} pasta(s), cerca de {grand_total} mensagens, para {base}")

        for folder in folders:
            rep.check()
            rep.log(f"> {folder.name}")
            conn.select(folder.name, readonly=True)
            uids = conn.uids()
            sink = FolderSink(base, folder.parts(), formats)
            try:
                for start in range(0, len(uids), FETCH_BATCH):
                    rep.check()
                    for msg in conn.fetch(uids[start:start + FETCH_BATCH]):
                        moz = mbox.flags_to_mozilla(msg.flags)
                        data = mbox.set_mozilla_status(msg.data, moz)
                        headers = mbox.parse_headers(data)
                        sink.add(data, headers, msg.internal_date or mbox.message_date(headers))
                        done += 1
                    rep.progress(min(done / grand_total, 1.0),
                                 f"{folder.name}: {sink.count}/{len(uids)} mensagens")
            finally:
                sink.close()
            rep.log(f"   {sink.count} mensagens baixadas ({human_size(sink.bytes)})")
            manifest["folders"].append({
                "parts": list(folder.parts()), "special": folder.special, "mbox": sink.mbox_rel,
                "eml": sink.eml_rel, "messages": sink.count, "bytes": sink.bytes,
                "skipped_deleted": 0, "partial": 0, "imap_name": folder.name,
            })
            totals["folders"] += 1
            totals["messages"] += sink.count
            _save_manifest(base, manifest)
    finally:
        conn.close()
    rep.progress(1.0, "Exportação concluída")
    totals["path"] = base
    return totals


def list_server_folders(params: ImapParams, rep: Reporter) -> list[FolderInfo]:
    conn = ImapConn(params, rep.log)
    rep.log(f"Conectando em {params.describe()}...")
    conn.connect()
    try:
        folders = conn.list_folders()
        for i, f in enumerate(folders):
            rep.check()
            if f.selectable:
                f.messages = conn.status_messages(f)
            rep.progress((i + 1) / max(len(folders), 1), f"Lendo pastas: {f.name}")
        rep.log(f"{len(folders)} pasta(s) encontradas no servidor.")
        return folders
    finally:
        conn.close()
