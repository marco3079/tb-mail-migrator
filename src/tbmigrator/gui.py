"""Interface gráfica (tkinter) do TB Mail Migrator."""

from __future__ import annotations

import datetime as dt
import os
import queue
import sys
import threading
import traceback
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Callable, Optional

from . import __app_name__, __version__
from . import exporter, importer, thunderbird
from .imapconn import PROVIDERS, FolderInfo, ImapConn, ImapParams
from .util import Cancelled, Reporter, human_size

CHECK, UNCHECK, NONE = "☑", "☐", " "
SECURITY = {"SSL/TLS": "ssl", "STARTTLS": "starttls", "Nenhuma": "none"}
SECURITY_REV = {v: k for k, v in SECURITY.items()}
# Pastas "virtuais" do Gmail que duplicam mensagens de outras pastas
GMAIL_VIRTUAL = {"\\All", "\\Important", "\\Flagged"}
GMAIL_VIRTUAL_NAMES = {"todos os e-mails", "all mail", "importante", "important", "com estrela", "starred"}


def is_gmail_virtual(parts: tuple) -> bool:
    return len(parts) == 2 and parts[0].casefold() in ("[gmail]", "[google mail]") \
        and parts[1].casefold() in GMAIL_VIRTUAL_NAMES


def resource(name: str) -> str:
    base = getattr(sys, "_MEIPASS", os.path.join(os.path.dirname(__file__), "..", "..", "assets"))
    return os.path.join(base, name)


def documents_dir() -> str:
    if sys.platform == "win32":
        try:
            import ctypes
            buf = ctypes.create_unicode_buffer(260)
            ctypes.windll.shell32.SHGetFolderPathW(None, 5, None, 0, buf)
            if buf.value:
                return buf.value
        except Exception:
            pass
    return os.path.expanduser("~")


def log_file() -> str:
    folder = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "TBMailMigrator", "logs")
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, dt.date.today().strftime("migrator-%Y%m%d.log"))


class CheckTree(ttk.Frame):
    """Treeview com caixas de seleção (marcar um pai marca todos os filhos)."""

    def __init__(self, master, columns: list[tuple[str, str, int]], **kw):
        super().__init__(master, **kw)
        self.tree = ttk.Treeview(self, columns=[c[0] for c in columns], selectmode="browse")
        self.tree.heading("#0", text="Pasta", anchor="w")
        self.tree.column("#0", width=360, stretch=True)
        for key, title, width in columns:
            self.tree.heading(key, text=title, anchor="w")
            self.tree.column(key, width=width, stretch=False, anchor="w")
        scroll = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")
        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)
        self.data: dict[str, object] = {}
        self.labels: dict[str, str] = {}
        self.checked: set[str] = set()
        self.checkable: set[str] = set()
        self.on_change: Callable[[], None] = lambda: None
        self.tree.bind("<Button-1>", self._click)
        self.tree.bind("<space>", lambda e: self.toggle(self.tree.focus()))

    def clear(self) -> None:
        self.tree.delete(*self.tree.get_children())
        self.data.clear()
        self.labels.clear()
        self.checked.clear()
        self.checkable.clear()
        self.on_change()

    def add(self, parent: str, label: str, values: tuple, obj=None, checkable=True, checked=False) -> str:
        iid = self.tree.insert(parent, "end", text="", values=values, open=True)
        self.data[iid] = obj
        self.labels[iid] = label
        if checkable:
            self.checkable.add(iid)
            if checked:
                self.checked.add(iid)
        self._render(iid)
        return iid

    def _render(self, iid: str) -> None:
        mark = (CHECK if iid in self.checked else UNCHECK) if iid in self.checkable else NONE
        self.tree.item(iid, text=f"{mark}  {self.labels[iid]}")

    def _descendants(self, iid: str):
        for child in self.tree.get_children(iid):
            yield child
            yield from self._descendants(child)

    def toggle(self, iid: str) -> None:
        if not iid:
            return
        new_state = iid not in self.checked if iid in self.checkable else not any(
            c in self.checked for c in self._descendants(iid))
        for target in [iid, *self._descendants(iid)]:
            if target in self.checkable:
                (self.checked.add if new_state else self.checked.discard)(target)
                self._render(target)
        self.on_change()

    def _click(self, event) -> None:
        if "indicator" in self.tree.identify_element(event.x, event.y):
            return
        if self.tree.identify_region(event.x, event.y) in ("tree", "cell"):
            self.toggle(self.tree.identify_row(event.y))

    def set_all(self, state: bool) -> None:
        self.checked = set(self.checkable) if state else set()
        for iid in self.data:
            self._render(iid)
        self.on_change()

    def selected(self) -> list:
        order = []

        def walk(parent):
            for iid in self.tree.get_children(parent):
                if iid in self.checked and self.data.get(iid) is not None:
                    order.append(self.data[iid])
                walk(iid)
        walk("")
        return order


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"{__app_name__} {__version__}")
        self.geometry("1080x780")
        self.minsize(900, 640)
        try:
            self.iconbitmap(resource("icon.ico"))
        except Exception:
            pass
        self._style()
        self.events: queue.Queue = queue.Queue()
        self.cancel = threading.Event()
        self.busy = False
        self.profiles: list[thunderbird.Profile] = []
        self.accounts: list[thunderbird.Account] = []
        self.server_folders: Optional[list[FolderInfo]] = None
        self.action_buttons: list[ttk.Button] = []
        self._log_fh = open(log_file(), "a", encoding="utf-8")

        self._build_header()
        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill="both", expand=True, padx=14, pady=(4, 6))
        self._build_export_tab()
        self._build_import_tab()
        self._build_footer()
        self.refresh()
        self.after(100, self._poll)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------------ estilo
    def _style(self) -> None:
        style = ttk.Style(self)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        self.option_add("*Font", ("Segoe UI", 10))
        style.configure("Treeview", rowheight=26, font=("Segoe UI", 10))
        style.configure("Header.TFrame", background="#0f4c81")
        style.configure("Title.TLabel", background="#0f4c81", foreground="white", font=("Segoe UI Semibold", 16))
        style.configure("Sub.TLabel", background="#0f4c81", foreground="#cfe3f5", font=("Segoe UI", 10))
        style.configure("Hint.TLabel", foreground="#5b6472", font=("Segoe UI", 9))
        style.configure("Accent.TButton", font=("Segoe UI Semibold", 10), padding=(16, 6))

    def _build_header(self) -> None:
        header = ttk.Frame(self, style="Header.TFrame", padding=(16, 12))
        header.pack(fill="x")
        ttk.Label(header, text=__app_name__, style="Title.TLabel").pack(side="left")
        self.detect_var = tk.StringVar(value="Procurando o Thunderbird...")
        ttk.Label(header, textvariable=self.detect_var, style="Sub.TLabel").pack(side="left", padx=18)
        ttk.Button(header, text="Atualizar", command=self.refresh).pack(side="right")
        ttk.Button(header, text="Abrir perfil...", command=self.open_profile).pack(side="right", padx=6)

    # ------------------------------------------------------------------ aba exportar
    def _build_export_tab(self) -> None:
        tab = ttk.Frame(self.notebook, padding=12)
        self.notebook.add(tab, text="  1. Exportar  ")
        tab.columnconfigure(1, weight=1)

        ttk.Label(tab, text="Conta:").grid(row=0, column=0, sticky="w")
        self.exp_account = ttk.Combobox(tab, state="readonly")
        self.exp_account.grid(row=0, column=1, columnspan=3, sticky="ew", pady=3)
        self.exp_account.bind("<<ComboboxSelected>>", lambda e: self._export_account_changed())

        ttk.Label(tab, text="Origem:").grid(row=1, column=0, sticky="nw", pady=(6, 0))
        src = ttk.Frame(tab)
        src.grid(row=1, column=1, columnspan=3, sticky="ew", pady=(6, 0))
        self.exp_source = tk.StringVar(value="local")
        ttk.Radiobutton(src, text="Arquivos locais do Thunderbird (rápido, apenas o que já está baixado no PC)",
                        variable=self.exp_source, value="local",
                        command=self._export_source_changed).pack(anchor="w")
        ttk.Radiobutton(src, text="Baixar do servidor IMAP da conta (completo, pede a senha)",
                        variable=self.exp_source, value="imap",
                        command=self._export_source_changed).pack(anchor="w")

        self.exp_imap_row = ttk.Frame(tab)
        self.exp_imap_row.grid(row=2, column=1, columnspan=3, sticky="ew", pady=4)
        self.exp_server_lbl = ttk.Label(self.exp_imap_row, text="", style="Hint.TLabel")
        self.exp_server_lbl.pack(side="left")
        self.exp_password = ttk.Entry(self.exp_imap_row, show="•", width=26)
        self.exp_list_btn = ttk.Button(self.exp_imap_row, text="Conectar e listar pastas",
                                       command=self.load_server_folders)
        self.exp_list_btn.pack(side="right")
        self.exp_password.pack(side="right", padx=6)
        ttk.Label(self.exp_imap_row, text="Senha:").pack(side="right")

        self.exp_tree = CheckTree(tab, [("size", "Tamanho", 110), ("status", "Situação", 260)])
        self.exp_tree.grid(row=3, column=0, columnspan=4, sticky="nsew", pady=6)
        tab.rowconfigure(3, weight=1)
        self.exp_tree.on_change = self._export_summary

        sel = ttk.Frame(tab)
        sel.grid(row=4, column=0, columnspan=4, sticky="ew")
        ttk.Button(sel, text="Marcar tudo", command=lambda: self.exp_tree.set_all(True)).pack(side="left")
        ttk.Button(sel, text="Desmarcar tudo", command=lambda: self.exp_tree.set_all(False)).pack(side="left", padx=6)
        self.exp_summary = ttk.Label(sel, text="", style="Hint.TLabel")
        self.exp_summary.pack(side="left", padx=10)

        ttk.Label(tab, text="Salvar em:").grid(row=5, column=0, sticky="w", pady=(10, 0))
        default_out = os.path.join(documents_dir(), "Thunderbird-Export-" + dt.date.today().strftime("%Y-%m-%d"))
        self.exp_out = tk.StringVar(value=default_out)
        ttk.Entry(tab, textvariable=self.exp_out).grid(row=5, column=1, sticky="ew", pady=(10, 0))
        ttk.Button(tab, text="Procurar...", command=lambda: self._pick_dir(self.exp_out)).grid(
            row=5, column=2, padx=6, pady=(10, 0))

        fmt = ttk.Frame(tab)
        fmt.grid(row=6, column=1, sticky="w", pady=6)
        self.fmt_mbox = tk.BooleanVar(value=True)
        self.fmt_eml = tk.BooleanVar(value=False)
        ttk.Label(fmt, text="Formatos:").pack(side="left")
        ttk.Checkbutton(fmt, text="MBOX (uma caixa por pasta, recomendado)", variable=self.fmt_mbox).pack(side="left", padx=8)
        ttk.Checkbutton(fmt, text="EML (um arquivo por mensagem)", variable=self.fmt_eml).pack(side="left")
        btn = ttk.Button(tab, text="Exportar", style="Accent.TButton", command=self.do_export)
        btn.grid(row=6, column=2, columnspan=2, sticky="e")
        self.action_buttons += [btn, self.exp_list_btn]

    # ------------------------------------------------------------------ aba importar
    def _build_import_tab(self) -> None:
        tab = ttk.Frame(self.notebook, padding=12)
        self.notebook.add(tab, text="  2. Importar  ")
        tab.columnconfigure(1, weight=1)

        ttk.Label(tab, text="Exportação:").grid(row=0, column=0, sticky="w")
        self.imp_src = tk.StringVar()
        ttk.Entry(tab, textvariable=self.imp_src).grid(row=0, column=1, sticky="ew")
        ttk.Button(tab, text="Procurar...", command=self._pick_import_source).grid(row=0, column=2, padx=6)
        ttk.Button(tab, text="Carregar", command=self.load_import_source).grid(row=0, column=3)
        ttk.Label(tab, text="Escolha a pasta criada pela exportação (ou qualquer pasta com arquivos .mbox / .eml).",
                  style="Hint.TLabel").grid(row=1, column=1, columnspan=3, sticky="w")

        self.imp_tree = CheckTree(tab, [("size", "Tamanho", 110), ("info", "Mensagens / formato", 200)])
        self.imp_tree.grid(row=2, column=0, columnspan=4, sticky="nsew", pady=6)
        tab.rowconfigure(2, weight=1)

        dest = ttk.LabelFrame(tab, text=" Destino ", padding=10)
        dest.grid(row=3, column=0, columnspan=4, sticky="ew", pady=(4, 0))
        dest.columnconfigure(1, weight=1)
        dest.columnconfigure(3, weight=1)
        self.imp_mode = tk.StringVar(value="tb")
        ttk.Radiobutton(dest, text="Conta do Thunderbird:", variable=self.imp_mode, value="tb",
                        command=self._import_mode_changed).grid(row=0, column=0, sticky="w")
        self.imp_account = ttk.Combobox(dest, state="readonly")
        self.imp_account.grid(row=0, column=1, columnspan=3, sticky="ew", pady=3)
        self.imp_account.bind("<<ComboboxSelected>>", lambda e: self._import_mode_changed())

        ttk.Radiobutton(dest, text="Outro servidor IMAP:", variable=self.imp_mode, value="imap",
                        command=self._import_mode_changed).grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.imp_provider = ttk.Combobox(dest, state="readonly", values=list(PROVIDERS))
        self.imp_provider.grid(row=1, column=1, sticky="ew", pady=(6, 0))
        self.imp_provider.bind("<<ComboboxSelected>>", lambda e: self._provider_changed())

        self.imp_host, self.imp_port, self.imp_user = tk.StringVar(), tk.StringVar(value="993"), tk.StringVar()
        self.imp_security = tk.StringVar(value="SSL/TLS")
        row = ttk.Frame(dest)
        row.grid(row=2, column=1, columnspan=3, sticky="ew", pady=3)
        ttk.Label(row, text="Servidor").pack(side="left")
        self.imp_host_e = ttk.Entry(row, textvariable=self.imp_host, width=26)
        self.imp_host_e.pack(side="left", padx=(4, 10))
        ttk.Label(row, text="Porta").pack(side="left")
        self.imp_port_e = ttk.Entry(row, textvariable=self.imp_port, width=6)
        self.imp_port_e.pack(side="left", padx=(4, 10))
        ttk.Label(row, text="Segurança").pack(side="left")
        self.imp_sec_e = ttk.Combobox(row, textvariable=self.imp_security, values=list(SECURITY),
                                      state="readonly", width=10)
        self.imp_sec_e.pack(side="left", padx=(4, 10))
        ttk.Label(row, text="Usuário").pack(side="left")
        self.imp_user_e = ttk.Entry(row, textvariable=self.imp_user, width=28)
        self.imp_user_e.pack(side="left", padx=4, fill="x", expand=True)

        row2 = ttk.Frame(dest)
        row2.grid(row=3, column=0, columnspan=4, sticky="ew", pady=(8, 0))
        ttk.Label(row2, text="Senha:").pack(side="left")
        self.imp_password = ttk.Entry(row2, show="•", width=26)
        self.imp_password.pack(side="left", padx=6)
        test_btn = ttk.Button(row2, text="Testar conexão", command=self.test_connection)
        test_btn.pack(side="left")
        self.imp_target_lbl = ttk.Label(row2, text="", style="Hint.TLabel")
        self.imp_target_lbl.pack(side="left", padx=10)

        opts = ttk.Frame(tab)
        opts.grid(row=4, column=0, columnspan=4, sticky="ew", pady=(10, 0))
        ttk.Label(opts, text="Colocar dentro da pasta:").pack(side="left")
        self.imp_prefix = tk.StringVar(value="")
        ttk.Entry(opts, textvariable=self.imp_prefix, width=22).pack(side="left", padx=6)
        ttk.Label(opts, text="(vazio = mesma estrutura na raiz)", style="Hint.TLabel").pack(side="left")
        self.imp_map = tk.BooleanVar(value=True)
        self.imp_dedupe = tk.BooleanVar(value=True)
        ttk.Checkbutton(opts, text="Mapear Entrada/Enviados/Lixeira...", variable=self.imp_map).pack(side="left", padx=12)
        ttk.Checkbutton(opts, text="Ignorar duplicadas", variable=self.imp_dedupe).pack(side="left")
        btn = ttk.Button(opts, text="Importar", style="Accent.TButton", command=self.do_import)
        btn.pack(side="right")
        self.action_buttons += [btn, test_btn]

    # ------------------------------------------------------------------ rodapé
    def _build_footer(self) -> None:
        foot = ttk.Frame(self, padding=(14, 0, 14, 12))
        foot.pack(fill="both")
        bar = ttk.Frame(foot)
        bar.pack(fill="x")
        self.progress = ttk.Progressbar(bar, maximum=1000)
        self.progress.pack(side="left", fill="x", expand=True)
        self.cancel_btn = ttk.Button(bar, text="Cancelar", command=self.cancel.set, state="disabled")
        self.cancel_btn.pack(side="right", padx=(8, 0))
        self.status = ttk.Label(foot, text="Pronto.", style="Hint.TLabel")
        self.status.pack(anchor="w", pady=(2, 4))
        logf = ttk.Frame(foot)
        logf.pack(fill="both")
        self.log_text = tk.Text(logf, height=8, wrap="word", font=("Consolas", 9), relief="flat",
                                background="#f5f7fa", state="disabled")
        sc = ttk.Scrollbar(logf, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=sc.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        sc.pack(side="right", fill="y")

    # ------------------------------------------------------------------ detecção
    def refresh(self) -> None:
        install = thunderbird.find_install()
        self.profiles = thunderbird.load_profiles()
        self._apply_profiles(install)

    def open_profile(self) -> None:
        path = filedialog.askdirectory(title="Escolha a pasta de um perfil do Thunderbird (contém prefs.js)")
        if not path:
            return
        if not os.path.isfile(os.path.join(path, "prefs.js")):
            messagebox.showwarning(__app_name__, "Essa pasta não tem prefs.js - não parece um perfil do Thunderbird.")
            return
        profile = thunderbird.load_profile(os.path.normpath(path))
        self.profiles = [profile] + [p for p in self.profiles if os.path.normcase(p.path) != os.path.normcase(profile.path)]
        self._apply_profiles(thunderbird.find_install())

    def _apply_profiles(self, install) -> None:
        self.accounts = thunderbird.all_accounts(self.profiles)
        tb = f"Thunderbird {install.version} detectado" if install else "Thunderbird não encontrado"
        self.detect_var.set(f"{tb}  •  {len(self.profiles)} perfil(is)  •  {len(self.accounts)} conta(s)")
        labels = [self._account_label(a) for a in self.accounts]
        self.exp_account["values"] = labels
        self.imp_account["values"] = labels
        if self.accounts:
            self.exp_account.current(0)
            self.imp_account.current(0)
        self.log(f"{tb}." + (f" Executável: {install.exe}" if install else ""))
        for p in self.profiles:
            self.log(f"Perfil '{p.name}'{' (padrão)' if p.is_default else ''}: {p.path}")
            for a in p.accounts:
                self.log(f"   - {a.label}  {a.username}  ({human_size(a.local_size)} no disco)")
        if not self.accounts:
            self.log("Nenhuma conta encontrada. Use 'Abrir perfil...' para indicar a pasta do perfil manualmente.")
        self._export_account_changed()
        self._import_mode_changed()

    def _account_label(self, a: thunderbird.Account) -> str:
        user = f" - {a.username}" if a.username and a.type != "none" and a.username.lower() != a.name.lower() else ""
        return f"{a.label}{user}   (perfil {a.profile_name})"

    def _account(self, combo: ttk.Combobox) -> Optional[thunderbird.Account]:
        idx = combo.current()
        return self.accounts[idx] if 0 <= idx < len(self.accounts) else None

    # ------------------------------------------------------------------ exportar: UI
    def _export_account_changed(self) -> None:
        acc = self._account(self.exp_account)
        self.server_folders = None
        if acc and not acc.is_imap and self.exp_source.get() == "imap":
            self.exp_source.set("local")
        self._export_source_changed()

    def _export_source_changed(self) -> None:
        acc = self._account(self.exp_account)
        imap = self.exp_source.get() == "imap"
        if imap and acc and not acc.is_imap:
            messagebox.showinfo(__app_name__, "Essa conta não é IMAP; só é possível exportar os arquivos locais.")
            self.exp_source.set("local")
        self._export_controls()
        if acc and acc.is_imap:
            self.exp_server_lbl.configure(text=f"{acc.username} @ {acc.hostname}:{acc.port}")
        else:
            self.exp_server_lbl.configure(text="")
        self._fill_export_tree()

    def _export_controls(self) -> None:
        state = "normal" if self.exp_source.get() == "imap" and not self.busy else "disabled"
        self.exp_password.configure(state="normal" if self.exp_source.get() == "imap" else "disabled")
        self.exp_list_btn.configure(state=state)

    def _fill_export_tree(self) -> None:
        tree = self.exp_tree
        tree.clear()
        acc = self._account(self.exp_account)
        if not acc:
            return
        if self.exp_source.get() == "local":
            def add(parent, folder: thunderbird.Folder):
                virtual = is_gmail_virtual(folder.parts)
                if folder.has_local_data:
                    status = "baixado no PC"
                elif acc.is_imap:
                    status = "não baixado (só no servidor)"
                else:
                    status = "vazia"
                if virtual:
                    status += "  (virtual: duplica outras)"
                iid = tree.add(parent, folder.name, (human_size(folder.size) if folder.size else "-", status),
                               obj=folder if folder.has_local_data else None,
                               checkable=True, checked=folder.has_local_data and not virtual)
                for child in folder.children:
                    add(iid, child)
            for f in acc.folders:
                add("", f)
            missing = [f for f in acc.all_folders() if not f.has_local_data]
            if acc.is_imap and missing:
                self.log(f"Aviso: {len(missing)} pasta(s) de '{acc.name}' não estão baixadas no PC. "
                         "Para exportar tudo, escolha 'Baixar do servidor IMAP'.")
        elif self.server_folders is not None:
            nodes: dict[tuple, str] = {}
            for f in sorted(self.server_folders, key=lambda x: (x.name.upper() != "INBOX", x.name.casefold())):
                parts = f.parts()
                parent = ""
                for i in range(1, len(parts)):
                    key = parts[:i]
                    if key not in nodes:
                        nodes[key] = tree.add(nodes.get(parts[:i - 1], ""), parts[i - 1], ("", ""), None, True, False)
                    parent = nodes[key]
                if not f.selectable:
                    nodes[parts] = tree.add(parent, parts[-1], ("", "contêiner"), None, True, False)
                    continue
                virtual = f.special in GMAIL_VIRTUAL or is_gmail_virtual(parts)
                status = f"{f.messages if f.messages is not None else '?'} mensagens"
                if virtual:
                    status += "  (pasta virtual: duplica outras)"
                nodes[parts] = tree.add(parent, parts[-1], ("", status), f, True, not virtual)
        self._export_summary()

    def _export_summary(self) -> None:
        items = self.exp_tree.selected()
        if self.exp_source.get() == "local":
            size = sum(getattr(i, "size", 0) for i in items)
            self.exp_summary.configure(text=f"{len(items)} pasta(s) selecionada(s), {human_size(size)}")
        else:
            msgs = sum((getattr(i, "messages", 0) or 0) for i in items)
            self.exp_summary.configure(text=f"{len(items)} pasta(s) selecionada(s), {msgs} mensagens")

    def _export_params(self) -> Optional[ImapParams]:
        acc = self._account(self.exp_account)
        pwd = self.exp_password.get()
        if not pwd:
            messagebox.showwarning(__app_name__, "Digite a senha da conta (no Gmail/Workspace use uma senha de app).")
            return None
        return ImapParams(acc.hostname, acc.port, acc.security, acc.username, pwd)

    # ------------------------------------------------------------------ exportar: ações
    def load_server_folders(self) -> None:
        params = self._export_params()
        if not params:
            return

        def job(rep):
            folders = exporter.list_server_folders(params, rep)
            self.events.put(("call", lambda: self._set_server_folders(folders)))
            return None
        self.run_task("Listando pastas do servidor", job, quiet=True)

    def _set_server_folders(self, folders) -> None:
        self.server_folders = folders
        self._fill_export_tree()

    def do_export(self) -> None:
        acc = self._account(self.exp_account)
        if not acc:
            return
        items = self.exp_tree.selected()
        if not items:
            messagebox.showwarning(__app_name__, "Marque pelo menos uma pasta para exportar.")
            return
        formats = {k for k, v in (("mbox", self.fmt_mbox), ("eml", self.fmt_eml)) if v.get()}
        if not formats:
            messagebox.showwarning(__app_name__, "Escolha pelo menos um formato (MBOX ou EML).")
            return
        out = self.exp_out.get().strip()
        if not out:
            return
        if self.exp_source.get() == "local":
            job = lambda rep: exporter.export_local(acc, items, out, formats, rep)
        else:
            params = self._export_params()
            if not params:
                return
            job = lambda rep: exporter.export_imap(acc.name, params, items, out, formats, rep)

        def done(result):
            if result:
                self.imp_src.set(out)
                self.load_import_source(silent=True)
        self.run_task("Exportando", job, on_done=done)

    # ------------------------------------------------------------------ importar: UI
    def _pick_import_source(self) -> None:
        if self._pick_dir(self.imp_src):
            self.load_import_source()

    def load_import_source(self, silent: bool = False) -> None:
        root = self.imp_src.get().strip()
        tree = self.imp_tree
        tree.clear()
        if not root or not os.path.isdir(root):
            if not silent:
                messagebox.showwarning(__app_name__, "Escolha uma pasta de exportação válida.")
            return
        sources = importer.scan_source(root)
        groups: dict[str, str] = {}
        nodes: dict[tuple, str] = {}
        for sf in sources:
            if sf.account not in groups:
                groups[sf.account] = tree.add("", sf.account, ("", "conta exportada"), None, True, True)
            parent = groups[sf.account]
            for i in range(1, len(sf.parts)):
                key = (sf.account,) + sf.parts[:i]
                if key not in nodes:
                    nodes[key] = tree.add(parent, sf.parts[i - 1], ("", ""), None, True, True)
                parent = nodes[key]
            info = f"{sf.messages} mensagens" if sf.messages is not None else ""
            info = f"{info}  ({sf.kind.upper()})".strip()
            nodes[(sf.account,) + sf.parts] = tree.add(parent, sf.parts[-1], (human_size(sf.size), info), sf, True, True)
        self.log(f"Origem '{root}': {len(sources)} pasta(s) para importar.")
        if not sources and not silent:
            messagebox.showinfo(__app_name__, "Nenhum arquivo .mbox ou .eml encontrado nessa pasta.")

    def _import_mode_changed(self) -> None:
        tb = self.imp_mode.get() == "tb"
        acc = self._account(self.imp_account)
        self.imp_account.configure(state="readonly" if tb else "disabled")
        for w in (self.imp_host_e, self.imp_port_e, self.imp_user_e):
            w.configure(state="disabled" if tb else "normal")
        self.imp_provider.configure(state="disabled" if tb else "readonly")
        self.imp_sec_e.configure(state="disabled" if tb else "readonly")
        if tb and acc:
            if acc.is_imap:
                self.imp_host.set(acc.hostname)
                self.imp_port.set(str(acc.port))
                self.imp_user.set(acc.username)
                self.imp_security.set(SECURITY_REV.get(acc.security, "SSL/TLS"))
                self.imp_password.configure(state="normal")
                self.imp_target_lbl.configure(text="As mensagens serão enviadas ao servidor da conta via IMAP.")
            else:
                self.imp_password.configure(state="disabled")
                self.imp_target_lbl.configure(text="Gravação direta nos arquivos do Thunderbird (feche-o antes).")
        else:
            self.imp_password.configure(state="normal")
            self.imp_target_lbl.configure(text="")

    def _provider_changed(self) -> None:
        host, port, sec = PROVIDERS[self.imp_provider.get()]
        self.imp_host.set(host)
        self.imp_port.set(str(port))
        self.imp_security.set(SECURITY_REV[sec])

    def _import_params(self) -> Optional[ImapParams]:
        host = self.imp_host.get().strip()
        user = self.imp_user.get().strip()
        pwd = self.imp_password.get()
        if not host or not user or not pwd:
            messagebox.showwarning(__app_name__, "Preencha servidor, usuário e senha do destino.")
            return None
        try:
            port = int(self.imp_port.get())
        except ValueError:
            messagebox.showwarning(__app_name__, "Porta inválida.")
            return None
        return ImapParams(host, port, SECURITY.get(self.imp_security.get(), "ssl"), user, pwd)

    def _import_target_is_local(self) -> Optional[thunderbird.Account]:
        if self.imp_mode.get() == "tb":
            acc = self._account(self.imp_account)
            if acc and not acc.is_imap:
                return acc
        return None

    # ------------------------------------------------------------------ importar: ações
    def test_connection(self) -> None:
        if self._import_target_is_local():
            ok = not thunderbird.is_running()
            messagebox.showinfo(__app_name__, "Conta local pronta para receber mensagens." if ok else
                                "Feche o Thunderbird antes de importar para uma conta local.")
            return
        params = self._import_params()
        if not params:
            return

        def job(rep):
            conn = ImapConn(params, rep.log)
            conn.connect()
            folders = conn.list_folders()
            conn.close()
            rep.log(f"Conexão OK: {len(folders)} pasta(s) no destino.")
            return {"message": f"Conexão bem-sucedida.\n{len(folders)} pasta(s) no servidor de destino."}
        self.run_task("Testando conexão", job)

    def do_import(self) -> None:
        sources = self.imp_tree.selected()
        if not sources:
            messagebox.showwarning(__app_name__, "Carregue uma exportação e marque as pastas para importar.")
            return
        prefix, mapping, dedupe = self.imp_prefix.get().strip(), self.imp_map.get(), self.imp_dedupe.get()
        local = self._import_target_is_local()
        if local:
            if thunderbird.is_running():
                messagebox.showwarning(__app_name__, "Feche o Thunderbird antes de importar para uma conta local.")
                return
            target = local.name
            job = lambda rep: importer.import_local(sources, local, prefix, mapping, dedupe, rep)
        else:
            params = self._import_params()
            if not params:
                return
            target = params.describe()
            job = lambda rep: importer.import_imap(sources, params, prefix, mapping, dedupe, rep)
        total = sum(s.size for s in sources)
        if not messagebox.askyesno(__app_name__, f"Importar {len(sources)} pasta(s) ({human_size(total)}) para:\n\n"
                                                 f"{target}\n\nContinuar?"):
            return
        self.run_task("Importando", job)

    # ------------------------------------------------------------------ execução em segundo plano
    def run_task(self, title: str, job, on_done=None, quiet: bool = False) -> None:
        if self.busy:
            return
        self.busy = True
        self.cancel.clear()
        for b in self.action_buttons:
            b.configure(state="disabled")
        self.cancel_btn.configure(state="normal")
        self.progress["value"] = 0
        self.status.configure(text=title + "...")
        self.log(f"=== {title} ===")
        rep = Reporter(
            log=lambda m: self.events.put(("log", m)),
            progress=lambda f, t: self.events.put(("progress", f, t)),
            cancel=self.cancel,
        )

        def worker():
            try:
                result = job(rep)
                self.events.put(("done", True, title, result, on_done, quiet))
            except Cancelled:
                self.events.put(("done", False, title, "Operação cancelada pelo usuário.", None, False))
            except Exception as exc:
                self.events.put(("log", traceback.format_exc()))
                self.events.put(("done", False, title, str(exc), None, False))
        threading.Thread(target=worker, daemon=True).start()

    def _poll(self) -> None:
        last_progress = None
        try:
            while True:
                event = self.events.get_nowait()
                kind = event[0]
                if kind == "log":
                    self.log(event[1])
                elif kind == "progress":
                    last_progress = event
                elif kind == "call":
                    event[1]()
                elif kind == "done":
                    self._finish(*event[1:])
        except queue.Empty:
            pass
        if last_progress:
            self.progress["value"] = int(last_progress[1] * 1000)
            self.status.configure(text=last_progress[2])
        self.after(100, self._poll)

    def _finish(self, ok: bool, title: str, result, on_done, quiet: bool) -> None:
        self.busy = False
        for b in self.action_buttons:
            b.configure(state="normal")
        self.cancel_btn.configure(state="disabled")
        self._export_controls()
        if ok:
            self.progress["value"] = 1000
            self.status.configure(text=f"{title}: concluído.")
            if on_done:
                on_done(result)
            if isinstance(result, dict) and not quiet:
                if "message" in result:
                    text = result["message"]
                else:
                    text = f"{title} concluído.\n\nPastas: {result.get('folders', 0)}\nMensagens: {result.get('messages', 0)}"
                    if result.get("duplicates"):
                        text += f"\nDuplicadas ignoradas: {result['duplicates']}"
                    if result.get("deleted"):
                        text += f"\nExcluídas ignoradas: {result['deleted']}"
                    if result.get("partial"):
                        text += f"\nIncompletas (só cabeçalho/parcial): {result['partial']}"
                    if result.get("errors"):
                        text += f"\nErros: {result['errors']} (veja o log)"
                    if result.get("path"):
                        text += f"\n\nSalvo em:\n{result['path']}"
                self.log(text.replace("\n\n", "\n"))
                messagebox.showinfo(__app_name__, text)
        else:
            self.status.configure(text=f"{title}: {result}")
            self.log(f"ERRO: {result}")
            messagebox.showerror(__app_name__, str(result))

    # ------------------------------------------------------------------ helpers
    def log(self, message: str) -> None:
        stamp = dt.datetime.now().strftime("%H:%M:%S")
        line = f"[{stamp}] {message}\n"
        self.log_text.configure(state="normal")
        self.log_text.insert("end", line)
        self.log_text.see("end")
        self.log_text.configure(state="disabled")
        try:
            self._log_fh.write(line)
            self._log_fh.flush()
        except Exception:
            pass

    def _pick_dir(self, var: tk.StringVar) -> bool:
        path = filedialog.askdirectory(initialdir=var.get() or documents_dir())
        if path:
            var.set(os.path.normpath(path))
            return True
        return False

    def _on_close(self) -> None:
        if self.busy and not messagebox.askyesno(__app_name__, "Há uma operação em andamento. Sair mesmo assim?"):
            return
        self.cancel.set()
        self.destroy()


def main() -> None:
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    App().mainloop()
