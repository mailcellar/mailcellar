"""
FakeImap: an IMAP server as imaplib.IMAP4 presents one, for ImapAccount's
tests. It answers LOGIN, LIST, EXAMINE (select with readonly=True),
UID SEARCH and UID FETCH in imaplib's own shapes: (typ, data), a fetched
literal as a (prefix, bytes) tuple followed by b")". It fails any command
that would change the server, SELECT without readonly included, so a test
that passes shows ImapAccount never writes.
"""

import imaplib
import re
from email.parser import BytesHeaderParser

from test_index import make_eml

DATE = "17-Jul-2026 02:44:25 +0000"


def quoted(name: str) -> bytes:
    return b'"' + name.replace("\\", "\\\\").replace('"', '\\"').encode() + b'"'


class FakeImap:
    """Folders of messages; tests add, move and flag them as a mail client would."""

    def __init__(self, password="secret"):
        self.password = password
        self.folders: dict[str, dict] = {}
        self.commands: list[tuple] = []
        self.selected: str | None = None
        self.drops = 0  # how many of the next commands fail as a dropped connection
        self.refused: set[str] = set()  # folders EXAMINE answers NO for
        self.bodiless: set[int] = set()  # UIDs whose BODY.PEEK[] comes back without a body
        self.add_folder("INBOX")

    # ── setting it up ────────────────────────────────────────────────────────
    def add_folder(self, name, special=(), uidvalidity=1, selectable=True):
        flags = ["\\HasNoChildren", *special] + ([] if selectable else ["\\Noselect"])
        self.folders[name] = {"flags": flags, "uidvalidity": uidvalidity, "next": 1, "uids": {}}

    def add(self, folder, raw=None, flags=(), date=DATE, msgid="<m@example.com>"):
        raw = raw if raw is not None else make_eml(msgid=msgid)
        f = self.folders[folder]
        uid = f["next"]
        f["next"] += 1
        f["uids"][uid] = {"raw": raw, "flags": set(flags), "date": date}
        return uid

    def move(self, src, uid, dst):
        """As MOVE does: the message keeps its date, flags and bytes."""
        message = self.folders[src]["uids"].pop(uid)
        f = self.folders[dst]
        new = f["next"]
        f["next"] += 1
        f["uids"][new] = message
        return new

    def reset_uidvalidity(self, folder):
        """A server rebuilding a folder's index: new UIDVALIDITY, UIDs renumbered."""
        f = self.folders[folder]
        f["uidvalidity"] += 1
        messages = list(f["uids"].values())
        f["uids"] = {i + 1: m for i, m in enumerate(messages)}
        f["next"] = len(messages) + 1

    # ── imaplib.IMAP4's surface ──────────────────────────────────────────────
    def _command(self, *command):
        self.commands.append(command)
        if self.drops:
            self.drops -= 1
            raise imaplib.IMAP4.abort("socket error: EOF")

    def login(self, user, password):
        self._command("LOGIN", user)
        if password != self.password:
            raise imaplib.IMAP4.error(b"[AUTHENTICATIONFAILED] Authentication failed.")
        return "OK", [b"Logged in"]

    def list(self):
        self._command("LIST")
        lines = [
            b"(" + " ".join(f["flags"]).encode() + b') "/" ' + quoted(name)
            for name, f in self.folders.items()
        ]
        return "OK", lines

    def select(self, mailbox="INBOX", readonly=False):
        self._command("SELECT" if not readonly else "EXAMINE", mailbox)
        if not readonly:
            raise AssertionError(f"SELECT {mailbox}: opens the folder for writing")
        name = mailbox[1:-1].replace('\\"', '"').replace("\\\\", "\\")
        if name not in self.folders or name in self.refused:
            return "NO", [b"Mailbox doesn't exist"]
        self.selected = name
        return "OK", [str(len(self.folders[name]["uids"])).encode()]

    def response(self, code):
        assert code == "UIDVALIDITY"
        return code, [str(self.folders[self.selected]["uidvalidity"]).encode()]

    def uid(self, command, *args):
        self._command("UID", command, *args)
        command = command.upper()
        uids = self.folders[self.selected]["uids"]
        if command == "SEARCH":
            return "OK", [b" ".join(str(u).encode() for u in sorted(uids))]
        if command != "FETCH":
            raise AssertionError(f"UID {command}: changes the server")
        wanted, items = args
        if "BODY[" in items or "STORE" in items:
            raise AssertionError(f"FETCH {items}: BODY[] sets \\Seen; BODY.PEEK[] doesn't")
        data = []
        for seq, uid in enumerate(sorted(uids), 1):
            if str(uid) not in wanted.split(","):
                continue
            m = uids[uid]
            prefix = f"{seq} (UID {uid}"
            if "FLAGS" in items:
                prefix += f" FLAGS ({' '.join(sorted(m['flags']))})"
            if "INTERNALDATE" in items:
                prefix += f' INTERNALDATE "{m["date"]}"'
            if "RFC822.SIZE" in items:
                prefix += f" RFC822.SIZE {len(m['raw'])}"
            literal = None
            if "HEADER.FIELDS (MESSAGE-ID)" in items:
                mid = BytesHeaderParser().parsebytes(m["raw"]).get("Message-ID")
                literal = (f"Message-ID: {mid}\r\n" if mid else "").encode() + b"\r\n"
                prefix += " BODY[HEADER.FIELDS (MESSAGE-ID)]"
            elif re.search(r"BODY\.PEEK\[\]", items) and uid not in self.bodiless:
                literal = m["raw"]
                prefix += " BODY[]"
            if literal is None:
                data.append((prefix + ")").encode())
            else:
                data.append(((prefix + f" {{{len(literal)}}}").encode(), literal))
                data.append(b")")
        return "OK", data or [None]

    def logout(self):
        self._command("LOGOUT")
        return "BYE", [b"Logging out"]

    def __getattr__(self, name):  # store, copy, expunge, append, create, delete, …
        raise AssertionError(f"{name}: not something a read-only backup sends")
