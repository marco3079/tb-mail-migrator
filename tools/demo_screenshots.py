"""Gera as capturas de tela do README com um perfil fictício (nenhum dado real)."""

import ctypes
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))

from PIL import ImageGrab  # noqa: E402

from tbmigrator import exporter, gui, mbox, thunderbird  # noqa: E402
from tbmigrator.util import Reporter  # noqa: E402

DOCS = os.path.join(HERE, "..", "docs")


def fake_message(n: int) -> bytes:
    return (b"From: Cliente <cliente@exemplo.com.br>\r\nTo: contato@empresa-exemplo.com.br\r\n"
            b"Subject: Pedido %d\r\nDate: Mon, 01 Jan 2024 10:00:00 -0300\r\n"
            b"Message-ID: <demo%d@exemplo.com.br>\r\n\r\nMensagem de exemplo.\r\n" % (n, n))


def build_profile(root: str) -> str:
    prof = os.path.join(root, "Profiles", "demo.default-release")
    imap = os.path.join(prof, "ImapMail", "imap.gmail.com")
    local = os.path.join(prof, "Mail", "Local Folders")
    os.makedirs(os.path.join(imap, "[Gmail].sbd"))
    os.makedirs(os.path.join(imap, "Clientes.sbd"))
    os.makedirs(local)
    with open(os.path.join(root, "profiles.ini"), "w") as fh:
        fh.write("[Profile0]\nName=default-release\nIsRelative=1\nPath=Profiles/demo.default-release\n"
                 "[InstallX]\nDefault=Profiles/demo.default-release\n")
    prefs = {
        "mail.accountmanager.accounts": "account1,account2",
        "mail.account.account1.server": "server1", "mail.account.account2.server": "server2",
        "mail.server.server1.directory-rel": "[ProfD]ImapMail/imap.gmail.com",
        "mail.server.server1.hostname": "imap.gmail.com", "mail.server.server1.type": "imap",
        "mail.server.server1.name": "contato@empresa-exemplo.com.br",
        "mail.server.server1.userName": "contato@empresa-exemplo.com.br",
        "mail.server.server2.directory-rel": "[ProfD]Mail/Local Folders",
        "mail.server.server2.hostname": "Local Folders", "mail.server.server2.type": "none",
        "mail.server.server2.name": "Pastas locais",
    }
    with open(os.path.join(prof, "prefs.js"), "w") as fh:
        for k, v in prefs.items():
            fh.write(f'user_pref("{k}", "{v}");\n')
        fh.write('user_pref("mail.server.server1.port", 993);\nuser_pref("mail.server.server1.socketType", 3);\n')

    def box(path, n):
        with mbox.MboxWriter(path) as w:
            for i in range(n):
                w.add(fake_message(i) + b"x" * 40000)
        open(path + ".msf", "wb").close()
    box(os.path.join(imap, "INBOX"), 120)
    box(os.path.join(imap, "[Gmail].sbd", "E-mails enviados"), 40)
    box(os.path.join(imap, "[Gmail].sbd", "Todos os e-mails"), 160)
    open(os.path.join(imap, "[Gmail].sbd", "Lixeira.msf"), "wb").close()
    open(os.path.join(imap, "[Gmail].msf"), "wb").close()
    open(os.path.join(imap, "Clientes.msf"), "wb").close()
    box(os.path.join(imap, "Clientes.sbd", "Projetos 2024"), 30)
    open(os.path.join(imap, "Clientes.sbd", "Arquivo antigo.msf"), "wb").close()
    for n in ("Trash", "Unsent Messages"):
        open(os.path.join(local, n), "wb").close()
    return root


def shot(app, name):
    app.update()
    x, y = app.winfo_rootx(), app.winfo_rooty()
    ImageGrab.grab(bbox=(x, y, x + app.winfo_width(), y + app.winfo_height())).save(os.path.join(DOCS, name))


def main():
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
    tmp = tempfile.mkdtemp()
    build_profile(tmp)
    profiles = thunderbird.load_profiles(tmp)
    acc = profiles[0].accounts[0]
    export_dir = os.path.join(tmp, "Thunderbird-Export")
    exporter.export_local(acc, [f for f in acc.all_folders() if f.has_local_data], export_dir, {"mbox"},
                          Reporter(log=lambda m: None))

    thunderbird.load_profiles = lambda root=None: profiles
    thunderbird.find_install = lambda: thunderbird.Install("C:\\Program Files\\Mozilla Thunderbird\\thunderbird.exe", "140.0")
    gui.log_file = lambda: os.path.join(tmp, "log.txt")
    gui.documents_dir = lambda: "C:\\Users\\voce\\Documents"
    app = gui.App()
    app.exp_out.set("C:\\Users\\voce\\Documents\\Thunderbird-Export")
    app.update()

    def step2():
        shot(app, "screenshot-export.png")
        app.notebook.select(1)
        app.imp_src.set(export_dir)
        app.load_import_source(silent=True)
        app.imp_src.set("C:\\Users\\voce\\Documents\\Thunderbird-Export")
        app.imp_mode.set("imap")
        app.imp_provider.set("Microsoft 365 / Outlook.com")
        app._import_mode_changed()
        app._provider_changed()
        app.imp_user.set("contato@novodominio.com.br")
        app.imp_prefix.set("")
        app.log_text.configure(state="normal")
        app.log_text.delete("1.0", "end")
        app.log_text.configure(state="disabled")
        app.log("Origem 'C:\\Users\\voce\\Documents\\Thunderbird-Export': 4 pasta(s) para importar.")
        app.after(600, lambda: (shot(app, "screenshot-import.png"), app.destroy()))
    app.after(1200, step2)
    app.mainloop()


if __name__ == "__main__":
    main()
