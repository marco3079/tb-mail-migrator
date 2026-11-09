"""Servidor IMAP mínimo em memória, apenas para os testes automatizados."""

from __future__ import annotations

import re
import socketserver
import threading


class Store:
    def __init__(self):
        self.folders = {"INBOX": [], "Sent": []}
        self.special = {"Sent": "\\Sent"}
        self.next_uid = 1

    def add(self, folder, data, flags=(), date='01-Jan-2024 10:00:00 +0000'):
        self.folders.setdefault(folder, []).append(
            {"uid": self.next_uid, "data": data, "flags": list(flags), "date": date})
        self.next_uid += 1


def _q(name: str) -> str:
    return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'


class Handler(socketserver.StreamRequestHandler):
    store: Store

    def send(self, text):
        self.wfile.write(text.encode() if isinstance(text, str) else text)

    def read_arg_line(self):
        line = self.rfile.readline()
        return line

    def handle(self):
        store = self.server.store
        selected = None
        self.send("* OK fake IMAP ready\r\n")
        while True:
            line = self.rfile.readline()
            if not line:
                return
            text = line.decode("utf-8", "replace").rstrip("\r\n")
            literal = re.search(r"\{(\d+)\}$", text)
            payload = b""
            if literal:
                self.send("+ go ahead\r\n")
                payload = self.rfile.read(int(literal.group(1)))
                self.rfile.readline()
            tag, _, rest = text.partition(" ")
            cmd, _, args = rest.partition(" ")
            cmd = cmd.upper()
            if cmd == "UID":
                cmd2, _, args = args.partition(" ")
                cmd = "UID " + cmd2.upper()
            names = [m.group(1) or m.group(2) for m in re.finditer(r'"((?:\\.|[^"\\])*)"|(\S+)', args)]
            names = [re.sub(r"\\(.)", r"\1", n) if n else n for n in names]
            if cmd == "CAPABILITY":
                self.send("* CAPABILITY IMAP4rev1 AUTH=PLAIN\r\n" + f"{tag} OK done\r\n")
            elif cmd == "LOGIN":
                ok = names[0] == "user" and names[1] == "secret"
                self.send(f"{tag} OK logged in\r\n" if ok else f"{tag} NO [AUTHENTICATIONFAILED] bad\r\n")
            elif cmd == "LOGOUT":
                self.send("* BYE\r\n" + f"{tag} OK bye\r\n")
                return
            elif cmd == "LIST":
                if names[1] == "":
                    self.send('* LIST (\\Noselect) "/" ""\r\n')
                else:
                    for name in store.folders:
                        flags = "\\HasNoChildren " + store.special.get(name, "")
                        self.send(f'* LIST ({flags.strip()}) "/" {_q(name)}\r\n')
                self.send(f"{tag} OK list\r\n")
            elif cmd == "STATUS":
                n = len(store.folders.get(names[0], []))
                self.send(f"* STATUS {_q(names[0])} (MESSAGES {n})\r\n{tag} OK status\r\n")
            elif cmd in ("SELECT", "EXAMINE"):
                if names[0] not in store.folders:
                    self.send(f"{tag} NO no such folder\r\n")
                    continue
                selected = names[0]
                self.send(f"* {len(store.folders[selected])} EXISTS\r\n{tag} OK selected\r\n")
            elif cmd == "UID SEARCH":
                uids = " ".join(str(m["uid"]) for m in store.folders[selected])
                self.send(f"* SEARCH {uids}\r\n{tag} OK search\r\n")
            elif cmd == "UID FETCH":
                spec, items = args.split(" ", 1)
                msgs = store.folders[selected]
                if spec != "1:*":
                    wanted = {int(x) for x in spec.split(",")}
                    msgs = [m for m in msgs if m["uid"] in wanted]
                for seq, m in enumerate(msgs, 1):
                    if "HEADER.FIELDS" in items:
                        hdr = re.search(rb"^Message-ID:.*?\r?\n", m["data"], re.M | re.I)
                        body = (hdr.group(0) if hdr else b"") + b"\r\n"
                        self.send(f"* {seq} FETCH (UID {m['uid']} BODY[HEADER.FIELDS (MESSAGE-ID)] {{{len(body)}}}\r\n".encode() + body + b")\r\n")
                    else:
                        data = m["data"]
                        flags = " ".join(m["flags"])
                        head = f"* {seq} FETCH (UID {m['uid']} FLAGS ({flags}) INTERNALDATE \"{m['date']}\" BODY[] {{{len(data)}}}\r\n"
                        self.send(head.encode() + data + b")\r\n")
                self.send(f"{tag} OK fetch\r\n")
            elif cmd == "CREATE":
                if names[0] in store.folders:
                    self.send(f"{tag} NO [ALREADYEXISTS] exists\r\n")
                else:
                    store.folders[names[0]] = []
                    self.send(f"{tag} OK created\r\n")
            elif cmd == "SUBSCRIBE":
                self.send(f"{tag} OK subscribed\r\n")
            elif cmd == "APPEND":
                folder = names[0]
                if folder not in store.folders:
                    self.send(f"{tag} NO [TRYCREATE] no folder\r\n")
                    continue
                flags = re.search(r"\(([^)]*)\)", args)
                store.add(folder, payload, flags.group(1).split() if flags else [])
                self.send(f"{tag} OK appended\r\n")
            elif cmd == "NOOP":
                self.send(f"{tag} OK noop\r\n")
            else:
                self.send(f"{tag} BAD unknown {cmd}\r\n")


class FakeImapServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), Handler)
        self.store = Store()
        self.thread = threading.Thread(target=self.serve_forever, daemon=True)

    @property
    def port(self):
        return self.server_address[1]

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.shutdown()
        self.server_close()
