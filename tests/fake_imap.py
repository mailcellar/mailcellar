"""
FakeImap: an IMAP server as imaplib.IMAP4 presents one, for ImapAccount's
tests. It answers LOGIN, CAPABILITY, LIST, EXAMINE (select with readonly=True),
UID SEARCH and UID FETCH in imaplib's own shapes: (typ, data), a fetched
literal as a (prefix, bytes) tuple followed by b")". It fails any command
that would change the server, SELECT without readonly included, so a test
that passes shows ImapAccount never writes; unless `writable` is set, when
it also answers SELECT, CREATE, APPEND, keyword UID STORE, UID MOVE and UID COPY
(step 3's writes),
and still fails anything that expunges, sets \\Deleted or deletes.
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
        self.stalls = 0  # how many of the next commands time out
        self.breaks = 0  # how many of the next commands stop halfway (Ctrl-C, say)
        self.message_limit: int | None = None  # MESSAGELIMIT (RFC 9738): highest N only
        self.no_exists = False  # EXAMINE answers without a message count
        self.writable = False  # SELECT, STORE of keywords and MOVE allowed (step 3)
        self.permanentflags = b"(\\Answered \\Flagged \\Deleted \\Seen \\Draft \\*)"
        self.read_write = False  # the selected folder was SELECTed, not EXAMINEd
        self.refused: set[str] = set()  # folders EXAMINE answers NO for
        self.keywordless: set[str] = set()  # folders whose PERMANENTFLAGS has no \\*
        self.append_adds = b""  # a header line the server adds to what is appended, as some do
        self.drop_after_append = 0  # how many APPENDs store the message, then lose the connection
        self.bodiless: set[int] = set()  # UIDs whose BODY.PEEK[] comes back without a body
        self.appended_gm = 9_000  # the last X-GM-MSGID Gmail gave an appended message
        self.capabilities = ("IMAP4REV1", "LITERAL+", "AUTH=PLAIN")  # the greeting's, as imaplib
        self.signed_in = ("IMAP4rev1", "LITERAL+", "SPECIAL-USE", "MOVE", "UIDPLUS")  # CAPABILITY
        self.add_folder("INBOX")

    # ── setting it up ────────────────────────────────────────────────────────
    def add_folder(self, name, special=(), uidvalidity=1, selectable=True):
        flags = ["\\HasNoChildren", *special] + ([] if selectable else ["\\Noselect"])
        self.folders[name] = {"flags": flags, "uidvalidity": uidvalidity, "next": 1, "uids": {}}

    def add(
        self, folder, raw=None, flags=(), date=DATE, msgid="<m@example.com>", gm=None, thread=None
    ):
        """
        A message in `folder`; `gm`, Gmail's X-GM-MSGID, the same for each copy
        of one, and `thread` its X-GM-THRID (its own X-GM-MSGID if not given).
        """
        raw = raw if raw is not None else make_eml(msgid=msgid)
        f = self.folders[folder]
        uid = f["next"]
        f["next"] += 1
        thread = thread if thread is not None else gm
        f["uids"][uid] = {"raw": raw, "flags": set(flags), "date": date, "gm": gm, "thr": thread}
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
        if self.stalls:
            self.stalls -= 1
            raise TimeoutError("timed out")
        if self.breaks:
            self.breaks -= 1
            raise KeyboardInterrupt

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

    def capability(self):
        self._command("CAPABILITY")
        return "OK", [" ".join(self.signed_in).encode()]

    def select(self, mailbox="INBOX", readonly=False):
        self._command("SELECT" if not readonly else "EXAMINE", mailbox)
        if not readonly and not self.writable:
            raise AssertionError(f"SELECT {mailbox}: opens the folder for writing")
        self.read_write = not readonly
        name = mailbox[1:-1].replace('\\"', '"').replace("\\\\", "\\")
        if name not in self.folders or name in self.refused:
            return "NO", [b"Mailbox doesn't exist"]
        self.selected = name
        return "OK", [None if self.no_exists else str(len(self.folders[name]["uids"])).encode()]

    def response(self, code):
        if code == "UIDNEXT":
            return code, [str(self.folders[self.selected]["next"]).encode()]
        if code == "PERMANENTFLAGS":
            if self.selected in self.keywordless:
                return code, [b"(\\Seen \\Flagged)"]
            return code, [self.permanentflags]
        assert code == "UIDVALIDITY", code
        return code, [str(self.folders[self.selected]["uidvalidity"]).encode()]

    def uid(self, command, *args):
        self._command("UID", command, *args)
        command = command.upper()
        uids = self.folders[self.selected]["uids"]
        if command == "SEARCH":
            wanted = sorted(uids)
            if args[1:2] == ("UID",):  # UID SEARCH UID lo:hi
                lo, hi = map(int, args[2].split(":"))
                wanted = [u for u in wanted if lo <= u <= hi]
            # Over the limit: the highest UIDs only, and an OK that doesn't say so to imaplib.
            wanted = wanted[-self.message_limit :] if self.message_limit else wanted
            return "OK", [b" ".join(str(u).encode() for u in wanted)]
        if command in ("STORE", "MOVE", "COPY"):
            return self._write(command, uids, *args)
        if command != "FETCH":
            raise AssertionError(f"UID {command}: changes the server")
        wanted, items = args
        if wanted.endswith(":*"):  # n:* is n and up, and the highest UID if all are lower
            low = int(wanted[:-2])
            wanted = ",".join(str(u) for u in sorted(uids) if u >= low or u == max(uids, default=0))
        if self.message_limit and len(wanted.split(",")) > self.message_limit:
            raise AssertionError(f"FETCH of {len(wanted.split(','))}: over MESSAGELIMIT")
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
            for item, key in (("X-GM-MSGID", "gm"), ("X-GM-THRID", "thr")):
                if item in items:
                    if "X-GM-EXT-1" not in self.signed_in:
                        raise AssertionError(f"FETCH {item}: a server without Gmail's extension")
                    prefix += f" {item} {m[key]}" if m[key] is not None else ""
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

    def shutdown(self):
        self._command("SHUTDOWN")  # the socket closed: not an IMAP command

    def _write(self, command, uids, wanted, *rest):
        if not (self.writable and self.read_write):
            raise AssertionError(f"UID {command}: changes the server")
        targets = [u for u in map(int, wanted.split(",")) if u in uids]
        if command == "STORE":
            how, flags = rest
            if "\\Deleted" in flags or not how.startswith(("+FLAGS", "-FLAGS")):
                raise AssertionError(f"UID STORE {how} {flags}: deletes, or replaces flags")
            names = set(flags.strip("()").split())
            keeps = b"\\*" in self.permanentflags and self.selected not in self.keywordless
            if not keeps and any(not n.startswith("\\") for n in names):
                # A keyword the server doesn't keep: BAD, as some servers answer; imaplib raises.
                raise imaplib.IMAP4.error("UID command error: BAD [b'arguments invalid']")
            for u in targets:
                f = uids[u]["flags"]
                uids[u]["flags"] = f | names if how.startswith("+") else f - names
            return "OK", [None]
        (dest,) = rest
        name = dest[1:-1].replace('\\"', '"').replace("\\\\", "\\")
        if name not in self.folders:
            return "NO", [b"[TRYCREATE] no such mailbox"]
        for u in targets:
            if command == "MOVE":
                self.move(self.selected, u, name)
            else:
                m = uids[u]
                copied = {"flags": set(m["flags"]), "gm": m["gm"], "thread": m["thr"]}
                self.add(name, raw=m["raw"], date=m["date"], **copied)
        return "OK", [None]

    def append(self, mailbox, flags, date_time, message):
        self._command("APPEND", mailbox, flags)
        if not self.writable:
            raise AssertionError(f"APPEND {mailbox}: changes the server")
        name = mailbox[1:-1].replace('\\"', '"').replace("\\\\", "\\")
        if name not in self.folders:
            return "NO", [b"[TRYCREATE] no such mailbox"]
        names = set((flags or "()").strip("()").split())
        gm = None
        if "X-GM-EXT-1" in self.signed_in:  # Gmail gives appended mail an X-GM-MSGID of its own
            self.appended_gm += 1
            gm = self.appended_gm
        raw = self.append_adds + message
        uid = self.add(name, raw=raw, flags=names, date=date_time.strip('"'), gm=gm)
        if self.drop_after_append:  # stored, but the answer never arrives
            self.drop_after_append -= 1
            raise imaplib.IMAP4.abort("socket error: EOF")
        if "UIDPLUS" not in self.signed_in:
            return "OK", [b"Append completed"]
        validity = self.folders[name]["uidvalidity"]
        return "OK", [f"[APPENDUID {validity} {uid}] Append completed".encode()]

    def create(self, mailbox):
        self._command("CREATE", mailbox)
        if not self.writable:
            raise AssertionError(f"CREATE {mailbox}: changes the server")
        name = mailbox[1:-1].replace('\\"', '"').replace("\\\\", "\\")
        if name in self.folders:
            return "NO", [b"[ALREADYEXISTS] Mailbox already exists"]
        self.add_folder(name)
        return "OK", [b"Create completed"]

    def logout(self):
        self._command("LOGOUT")
        return "BYE", [b"Logging out"]

    def __getattr__(self, name):  # store, copy, expunge, append, create, delete, …
        raise AssertionError(f"{name}: not something a read-only backup sends")
