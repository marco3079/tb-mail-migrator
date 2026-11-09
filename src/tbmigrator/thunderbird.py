"""Detecção da instalação do Thunderbird, dos perfis, das contas e das pastas no disco."""

from __future__ import annotations

import configparser
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Iterator, Optional

TYPE_LABELS = {
    "imap": "IMAP",
    "pop3": "POP3",
    "none": "Pastas locais",
    "rss": "Feeds",
    "movemail": "Movemail",
    "nntp": "Newsgroups",
}

_PREF_RE = re.compile(r'^\s*user_pref\(\s*"((?:\\.|[^"\\])*)"\s*,\s*(.*?)\s*\)\s*;\s*$')
_SKIP_EXT = {".msf", ".dat", ".html", ".htm", ".json", ".sqlite", ".js", ".txt", ".bak",
             ".tmp", ".lock", ".mozmsgs", ".log", ".ini", ".rdf", ".mab"}


@dataclass
class Folder:
    name: str
    parts: tuple
    mbox_path: Optional[str] = None
    maildir_path: Optional[str] = None
    size: int = 0
    children: list = field(default_factory=list)

    @property
    def has_local_data(self) -> bool:
        return self.size > 0

    def walk(self) -> Iterator["Folder"]:
        yield self
        for child in self.children:
            yield from child.walk()


@dataclass
class Account:
    key: str
    server_key: str
    name: str
    type: str
    hostname: str
    port: int
    username: str
    socket_type: int
    directory: str
    store: str
    profile_name: str
    profile_path: str
    folders: list = field(default_factory=list)

    @property
    def is_imap(self) -> bool:
        return self.type == "imap"

    @property
    def type_label(self) -> str:
        return TYPE_LABELS.get(self.type, self.type or "?")

    @property
    def label(self) -> str:
        return f"{self.name}  [{self.type_label}]"

    @property
    def security(self) -> str:
        if self.socket_type == 3 or (self.socket_type == 0 and self.port == 993):
            return "ssl"
        if self.socket_type == 2:
            return "starttls"
        return "none"

    def all_folders(self) -> Iterator[Folder]:
        for folder in self.folders:
            yield from folder.walk()

    @property
    def local_size(self) -> int:
        return sum(f.size for f in self.all_folders())


@dataclass
class Profile:
    name: str
    path: str
    is_default: bool
    accounts: list = field(default_factory=list)


@dataclass
class Install:
    exe: str
    version: str


# --------------------------------------------------------------------------- instalação

def find_install() -> Optional[Install]:
    candidates: list[str] = []
    if sys.platform == "win32":
        import winreg
        keys = [
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Mozilla\Mozilla Thunderbird"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Mozilla\Mozilla Thunderbird"),
            (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Mozilla\Mozilla Thunderbird"),
        ]
        for hive, sub in keys:
            try:
                with winreg.OpenKey(hive, sub) as key:
                    current = winreg.QueryValueEx(key, "CurrentVersion")[0]
                    with winreg.OpenKey(key, current + r"\Main") as main:
                        candidates.append(winreg.QueryValueEx(main, "PathToExe")[0])
            except OSError:
                pass
    for env in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
        base = os.environ.get(env)
        if base:
            candidates.append(os.path.join(base, "Mozilla Thunderbird", "thunderbird.exe"))
    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidates.append(os.path.join(local, "Programs", "Mozilla Thunderbird", "thunderbird.exe"))

    for exe in candidates:
        if exe and os.path.isfile(exe):
            return Install(exe=exe, version=_read_version(os.path.dirname(exe)))
    return None


def _read_version(install_dir: str) -> str:
    ini = os.path.join(install_dir, "application.ini")
    cp = configparser.ConfigParser(interpolation=None, strict=False)
    try:
        cp.read(ini, encoding="utf-8")
        return cp.get("App", "Version", fallback="?")
    except Exception:
        return "?"


def is_running() -> bool:
    if sys.platform != "win32":
        return False
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq thunderbird.exe", "/NH"],
            capture_output=True, text=True, timeout=15, creationflags=0x08000000,
        ).stdout
        return "thunderbird.exe" in out.lower()
    except Exception:
        return False


# --------------------------------------------------------------------------- perfis

def profiles_root() -> str:
    return os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "Thunderbird")


def load_profiles(root: Optional[str] = None) -> list[Profile]:
    root = root or profiles_root()
    ini = os.path.join(root, "profiles.ini")
    if not os.path.isfile(ini):
        return []
    cp = configparser.ConfigParser(interpolation=None, strict=False)
    cp.optionxform = str
    cp.read(ini, encoding="utf-8")

    install_defaults = {
        cp[s]["Default"].replace("\\", "/") for s in cp.sections()
        if s.startswith("Install") and "Default" in cp[s]
    }
    profiles: list[Profile] = []
    for section in cp.sections():
        if not section.startswith("Profile"):
            continue
        sec = cp[section]
        rel_path = sec.get("Path", "")
        if not rel_path:
            continue
        relative = sec.get("IsRelative", "1") == "1"
        full = os.path.normpath(os.path.join(root, rel_path) if relative else rel_path)
        if install_defaults:
            is_default = rel_path.replace("\\", "/") in install_defaults
        else:
            is_default = sec.get("Default") == "1"
        if os.path.isdir(full):
            profiles.append(load_profile(full, sec.get("Name") or os.path.basename(full), is_default))
    profiles.sort(key=lambda p: (not p.is_default, not p.accounts, p.name.lower()))
    return profiles


def load_profile(path: str, name: Optional[str] = None, is_default: bool = False) -> Profile:
    name = name or os.path.basename(path)
    prefs = read_prefs(os.path.join(path, "prefs.js"))
    profile = Profile(name=name, path=path, is_default=is_default)
    for acc in str(prefs.get("mail.accountmanager.accounts", "")).split(","):
        acc = acc.strip()
        server = prefs.get(f"mail.account.{acc}.server")
        if not acc or not server:
            continue

        def get(key, default=None):
            return prefs.get(f"mail.server.{server}.{key}", default)

        typ = str(get("type", ""))
        if typ == "nntp":
            continue
        directory = _resolve_dir(path, get("directory-rel"), get("directory"))
        store = "maildir" if "maildir" in str(get("storeContractID", "")) else "mbox"
        socket_type = int(get("socketType", 0) or 0)
        default_port = 993 if socket_type == 3 else 143
        account = Account(
            key=acc,
            server_key=server,
            name=str(get("name") or get("hostname") or acc),
            type=typ,
            hostname=str(get("hostname", "")),
            port=int(get("port", default_port) or default_port),
            username=str(get("userName", "")),
            socket_type=socket_type,
            directory=directory,
            store=store,
            profile_name=name,
            profile_path=path,
        )
        if directory and os.path.isdir(directory):
            account.folders = scan_folders(directory, store)
        profile.accounts.append(account)
    return profile


def read_prefs(path: str) -> dict:
    prefs: dict = {}
    if not os.path.isfile(path):
        return prefs
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            match = _PREF_RE.match(line)
            if not match:
                continue
            try:
                key = json.loads('"' + match.group(1) + '"')
            except ValueError:
                key = match.group(1)
            raw = match.group(2)
            try:
                value = json.loads(raw)
            except ValueError:
                value = raw.strip('"')
            prefs[key] = value
    return prefs


def _resolve_dir(profile_path: str, rel: Optional[str], absolute: Optional[str]) -> str:
    if rel:
        rel = str(rel)
        for token in ("[ProfD]", "[ProfLD]"):
            if rel.startswith(token):
                candidate = os.path.normpath(os.path.join(profile_path, rel[len(token):]))
                if os.path.isdir(candidate) or not absolute:
                    return candidate
    return os.path.normpath(str(absolute)) if absolute else ""


# --------------------------------------------------------------------------- pastas

def _order(name: str):
    return (name.upper() != "INBOX", name.casefold())


def scan_folders(directory: str, store: str = "mbox", parent: tuple = ()) -> list[Folder]:
    try:
        entries = os.listdir(directory)
    except OSError:
        return []
    entry_set = set(entries)
    names: set[str] = set()
    for entry in entries:
        full = os.path.join(directory, entry)
        if os.path.isdir(full):
            if entry.endswith(".sbd"):
                names.add(entry[:-4])
            elif store == "maildir" and os.path.isdir(os.path.join(full, "cur")):
                names.add(entry)
        elif entry.endswith(".msf"):
            names.add(entry[:-4])
        else:
            ext = os.path.splitext(entry)[1].lower()
            # Pastas mbox são arquivos sem extensão (ex.: "INBOX") ou que têm um .msf ao lado
            # (ex.: "Projeto.2024" + "Projeto.2024.msf").
            if (entry + ".msf") in entry_set or ext == "" or (
                    ext not in _SKIP_EXT and _starts_like_mbox(full)):
                names.add(entry)

    folders: list[Folder] = []
    for name in sorted(names, key=_order):
        folder = Folder(name=name, parts=parent + (name,))
        path = os.path.join(directory, name)
        if store == "maildir" and os.path.isdir(os.path.join(path, "cur")):
            folder.maildir_path = path
            folder.size = _dir_size(path)
        elif os.path.isfile(path):
            folder.mbox_path = path
            folder.size = os.path.getsize(path)
        sbd = os.path.join(directory, name + ".sbd")
        if os.path.isdir(sbd):
            folder.children = scan_folders(sbd, store, folder.parts)
        folders.append(folder)
    return folders


def _starts_like_mbox(path: str) -> bool:
    try:
        with open(path, "rb") as fh:
            return fh.read(5) == b"From "
    except OSError:
        return False


def _dir_size(path: str) -> int:
    total = 0
    for sub in ("cur", "new"):
        folder = os.path.join(path, sub)
        if os.path.isdir(folder):
            for name in os.listdir(folder):
                try:
                    total += os.path.getsize(os.path.join(folder, name))
                except OSError:
                    pass
    return total


def all_accounts(profiles: list[Profile]) -> list[Account]:
    return [acc for p in profiles for acc in p.accounts]
