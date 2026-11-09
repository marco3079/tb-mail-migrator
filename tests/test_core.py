import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

from tbmigrator import exporter, imap_utf7, importer, mbox, thunderbird  # noqa: E402
from tbmigrator.imapconn import ImapParams, parse_list_response  # noqa: E402
from tbmigrator.util import Reporter, classify_special, safe_name  # noqa: E402
from fake_imap import FakeImapServer  # noqa: E402


def msg(n, body=b"Corpo da mensagem.\r\n", status=None, mid=True):
    head = b""
    if status is not None:
        head += b"X-Mozilla-Status: %04x\r\nX-Mozilla-Status2: 00000000\r\n" % status
    head += b"From: Alice <alice@example.com>\r\nTo: bob@example.com\r\n"
    head += b"Subject: Teste %d\r\nDate: Mon, 01 Jan 2024 10:%02d:00 -0300\r\n" % (n, n % 60)
    if mid:
        head += b"Message-ID: <msg%d@example.com>\r\n" % n
    return head + b"\r\n" + body


def quiet():
    return Reporter(log=lambda m: None)


def make_profile(root):
    """Cria um perfil falso do Thunderbird com uma conta IMAP e as Pastas Locais."""
    prof = os.path.join(root, "Profiles", "abc.default-release")
    imap_dir = os.path.join(prof, "ImapMail", "imap.example.com")
    local_dir = os.path.join(prof, "Mail", "Local Folders")
    os.makedirs(os.path.join(imap_dir, "Projetos.sbd"))
    os.makedirs(local_dir)
    with open(os.path.join(root, "profiles.ini"), "w") as fh:
        fh.write("[Profile0]\nName=default-release\nIsRelative=1\nPath=Profiles/abc.default-release\n\n"
                 "[InstallXYZ]\nDefault=Profiles/abc.default-release\n")
    with open(os.path.join(prof, "prefs.js"), "w", encoding="utf-8") as fh:
        fh.write('user_pref("mail.accountmanager.accounts", "account1,account2");\n'
                 'user_pref("mail.account.account1.server", "server1");\n'
                 'user_pref("mail.account.account2.server", "server2");\n'
                 'user_pref("mail.server.server1.directory-rel", "[ProfD]ImapMail/imap.example.com");\n'
                 'user_pref("mail.server.server1.hostname", "imap.example.com");\n'
                 'user_pref("mail.server.server1.name", "joao@example.com");\n'
                 'user_pref("mail.server.server1.port", 993);\n'
                 'user_pref("mail.server.server1.socketType", 3);\n'
                 'user_pref("mail.server.server1.type", "imap");\n'
                 'user_pref("mail.server.server1.userName", "joao@example.com");\n'
                 'user_pref("mail.server.server2.directory-rel", "[ProfD]Mail/Local Folders");\n'
                 'user_pref("mail.server.server2.hostname", "Local Folders");\n'
                 'user_pref("mail.server.server2.name", "Pastas locais");\n'
                 'user_pref("mail.server.server2.type", "none");\n')
    with mbox.MboxWriter(os.path.join(imap_dir, "INBOX")) as w:
        w.add(msg(1, status=mbox.FLAG_READ))
        w.add(msg(2, body=b"Linha normal\r\nFrom the start of a line\r\n", status=0))
        w.add(msg(3, status=mbox.FLAG_EXPUNGED))
    open(os.path.join(imap_dir, "INBOX.msf"), "wb").close()
    open(os.path.join(imap_dir, "Projetos.msf"), "wb").close()  # só índice (não baixada)
    with mbox.MboxWriter(os.path.join(imap_dir, "Projetos.sbd", "Cliente.2024")) as w:
        w.add(msg(4, status=mbox.FLAG_MARKED))
    open(os.path.join(imap_dir, "Projetos.sbd", "Cliente.2024.msf"), "wb").close()
    open(os.path.join(imap_dir, "msgFilterRules.dat"), "wb").close()
    for name in ("Trash", "Unsent Messages"):
        open(os.path.join(local_dir, name), "wb").close()
    return prof


class Utf7Test(unittest.TestCase):
    def test_roundtrip(self):
        for name in ["INBOX", "Itens Enviados", "Lixo Eletrônico", "R&D", "日本語", "[Gmail]/E-mails enviados"]:
            self.assertEqual(imap_utf7.decode(imap_utf7.encode(name)), name)
        self.assertEqual(imap_utf7.encode("Lixo Eletrônico"), "Lixo Eletr&APQ-nico")
        self.assertEqual(imap_utf7.encode("R&D"), "R&-D")


class MboxTest(unittest.TestCase):
    def test_roundtrip_with_from_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "box.mbox")
            bodies = [msg(1), msg(2, body=b"Ola\r\nFrom here on\r\n>From quoted\r\n"), msg(3, body=b"fim")]
            with mbox.MboxWriter(path) as w:
                for b in bodies:
                    w.add(b)
            read = [m.data for m in mbox.iter_mbox(path)]
            self.assertEqual(len(read), 3)
            self.assertEqual(read[1], bodies[1])
            self.assertTrue(read[2].endswith(b"fim\r\n") or read[2].endswith(b"fim"))

    def test_flags_and_headers(self):
        data = msg(5, status=mbox.FLAG_READ | mbox.FLAG_MARKED)
        h = mbox.parse_headers(data)
        self.assertEqual(mbox.imap_flags(h), ["\\Seen", "\\Flagged"])
        self.assertEqual(mbox.message_id(h), "<msg5@example.com>")
        self.assertNotIn(b"X-Mozilla", mbox.strip_mozilla_headers(data))
        self.assertEqual(mbox.message_date(h).year, 2024)


class DetectionTest(unittest.TestCase):
    def test_profile_accounts_and_folders(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_profile(tmp)
            profiles = thunderbird.load_profiles(tmp)
            self.assertEqual(len(profiles), 1)
            self.assertTrue(profiles[0].is_default)
            imap_acc, local_acc = profiles[0].accounts
            self.assertEqual((imap_acc.type, imap_acc.hostname, imap_acc.security), ("imap", "imap.example.com", "ssl"))
            self.assertEqual(local_acc.type, "none")
            names = ["/".join(f.parts) for f in imap_acc.all_folders()]
            self.assertEqual(names, ["INBOX", "Projetos", "Projetos/Cliente.2024"])
            projetos = imap_acc.folders[1]
            self.assertFalse(projetos.has_local_data)
            self.assertTrue(projetos.children[0].has_local_data)


class ExportImportLocalTest(unittest.TestCase):
    def test_export_then_import_into_local_folders(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_profile(tmp)
            imap_acc, local_acc = thunderbird.load_profiles(tmp)[0].accounts
            out = os.path.join(tmp, "export")
            folders = [f for f in imap_acc.all_folders() if f.has_local_data]
            res = exporter.export_local(imap_acc, folders, out, {"mbox", "eml"}, quiet())
            self.assertEqual(res["messages"], 3)
            self.assertEqual(res["deleted"], 1)
            eml_dir = os.path.join(res["path"], "eml", "INBOX")
            self.assertEqual(len(os.listdir(eml_dir)), 2)

            sources = importer.scan_source(out)
            self.assertEqual(sorted(s.label for s in sources), ["INBOX", "Projetos/Cliente.2024"])
            with mock.patch.object(importer, "is_running", return_value=False):
                r1 = importer.import_local(sources, local_acc, "Importado", True, True, quiet())
                r2 = importer.import_local(sources, local_acc, "Importado", True, True, quiet())
            self.assertEqual(r1["messages"], 3)
            self.assertEqual(r2["messages"], 0)
            self.assertEqual(r2["duplicates"], 3)
            base = local_acc.directory
            inbox = os.path.join(base, "Importado.sbd", "INBOX")
            self.assertTrue(os.path.isfile(os.path.join(base, "Importado")))
            msgs = list(mbox.iter_mbox(inbox))
            self.assertEqual(len(msgs), 2)
            self.assertIn(b"From the start of a line", msgs[1].data)
            # pasta aparece para o Thunderbird ao reescanear
            local_again = thunderbird.load_profiles(tmp)[0].accounts[1]
            self.assertIn("Importado/Projetos/Cliente.2024", ["/".join(f.parts) for f in local_again.all_folders()])


class ImapTest(unittest.TestCase):
    def test_list_parsing(self):
        data = [b'(\\HasNoChildren) "/" "INBOX"',
                b'(\\HasNoChildren \\Sent) "/" "[Gmail]/E-mails enviados"',
                (b'(\\HasNoChildren) "/" {16}', b"Lixo Eletr&APQ-nico"),
                b'(\\Noselect \\HasChildren) "/" "[Gmail]"']
        folders = parse_list_response(data)
        self.assertEqual([f.name for f in folders], ["INBOX", "[Gmail]/E-mails enviados", "Lixo Eletrônico", "[Gmail]"])
        self.assertEqual(folders[1].special, "\\Sent")
        self.assertFalse(folders[3].selectable)

    def test_export_from_server_and_import_to_server(self):
        with FakeImapServer() as src, FakeImapServer() as dst, tempfile.TemporaryDirectory() as tmp:
            src.store.add("INBOX", msg(1), ["\\Seen"])
            src.store.add("INBOX", msg(2), [])
            src.store.add("Sent", msg(3), ["\\Seen"])
            dst.store.add("INBOX", msg(1), ["\\Seen"])  # já existe -> deve ser ignorada
            p_src = ImapParams("127.0.0.1", src.port, "none", "user", "secret", 10)
            p_dst = ImapParams("127.0.0.1", dst.port, "none", "user", "secret", 10)

            folders = exporter.list_server_folders(p_src, quiet())
            self.assertEqual({f.name: f.messages for f in folders}, {"INBOX": 2, "Sent": 1})
            res = exporter.export_imap("conta", p_src, folders, tmp, {"mbox"}, quiet())
            self.assertEqual(res["messages"], 3)

            sources = importer.scan_source(tmp)
            r = importer.import_imap(sources, p_dst, "", True, True, quiet())
            self.assertEqual((r["messages"], r["duplicates"]), (2, 1))
            self.assertEqual(len(dst.store.folders["INBOX"]), 2)
            self.assertEqual(dst.store.folders["Sent"][0]["flags"], ["\\Seen"])
            self.assertNotIn(b"X-Mozilla", dst.store.folders["Sent"][0]["data"])

            r2 = importer.import_imap(sources, p_dst, "Backup/2024", True, False, quiet())
            self.assertEqual(r2["messages"], 3)
            self.assertIn("Backup/2024/Sent", dst.store.folders)

    def test_bad_password(self):
        from tbmigrator.imapconn import ImapConn, ImapError
        with FakeImapServer() as srv:
            conn = ImapConn(ImapParams("127.0.0.1", srv.port, "none", "user", "wrong", 10))
            with self.assertRaises(ImapError):
                conn.connect()


class UtilTest(unittest.TestCase):
    def test_names(self):
        self.assertEqual(safe_name('a/b:c*?'), "a_b_c__")
        self.assertEqual(safe_name("CON"), "_CON")
        self.assertEqual(classify_special(("[Gmail]", "E-mails enviados")), "\\Sent")
        self.assertEqual(classify_special(("Projetos", "Enviados")), None)
        self.assertEqual(classify_special(("Inbox",)), "INBOX")


if __name__ == "__main__":
    unittest.main()
