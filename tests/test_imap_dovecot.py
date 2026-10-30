"""
Against a real IMAP server (#111): Dovecot in a container, opt-in.

    uv run pytest -m imap_server

Starts DOVECOT_IMAGE with podman or docker, serving TLS with a
certificate from a throwaway CA made with openssl, and seeds each test's
own user over IMAP. The image's static passdb takes any user with
USER_PASSWORD, so each test starts with an empty mailbox. Any other run
skips them, saying how to run them (conftest.py), so the default run
stays offline; asked for, they fail without podman or docker, or with
its engine down, rather than pass having tested nothing.
"""

import csv
import gc
import imaplib
import os
import shutil
import sqlite3
import ssl
import subprocess
import time
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest

from mailcellar import cli, gmailapi, imap
from mailcellar.offload import cli as offload_cli
from mailcellar.paths import backup_folder
from mailcellar.setup import cli as setup
from test_index import make_eml

pytestmark = pytest.mark.imap_server

DOVECOT_IMAGE = os.environ.get("DOVECOT_IMAGE", "docker.io/dovecot/dovecot:2.4.5")
PASSWORD = "test-password"
IMAPS = 31993  # the rootless image's IMAPS port


@dataclass(frozen=True)
class Server:
    port: int
    cafile: Path

    @contextmanager
    def session(self, user: str):
        """A session of the test's own, to seed and change the mailbox as a client would."""
        context = ssl.create_default_context(cafile=self.cafile)
        conn = imaplib.IMAP4_SSL("localhost", self.port, ssl_context=context, timeout=30)
        try:
            conn.login(user, PASSWORD)
            yield conn
        finally:  # closed however the login or the test ends: a failed LOGOUT hides nothing
            imap.log_out(conn)


def openssl(*args, cwd: Path):
    subprocess.run(["openssl", *args], cwd=cwd, check=True, capture_output=True)


def make_certificates(folder: Path) -> None:
    """ca.crt, and tls.crt/tls.key for localhost signed by it."""
    openssl(
        *("req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2"),
        *("-keyout", "ca.key", "-out", "ca.crt", "-subj", "/CN=mailcellar test CA"),
        *("-addext", "basicConstraints=critical,CA:TRUE"),
        *("-addext", "keyUsage=critical,keyCertSign,cRLSign"),
        cwd=folder,
    )
    (folder / "leaf.cnf").write_text(
        "subjectAltName=DNS:localhost,IP:127.0.0.1\n"
        "basicConstraints=critical,CA:FALSE\n"
        "keyUsage=critical,digitalSignature,keyEncipherment\n"
        "extendedKeyUsage=serverAuth\n"
        "subjectKeyIdentifier=hash\n"
        "authorityKeyIdentifier=keyid\n"
    )
    openssl(
        *("req", "-newkey", "rsa:2048", "-nodes", "-keyout", "tls.key", "-out", "tls.csr"),
        *("-subj", "/CN=localhost"),
        cwd=folder,
    )
    openssl(
        *("x509", "-req", "-in", "tls.csr", "-CA", "ca.crt", "-CAkey", "ca.key"),
        *("-CAcreateserial", "-days", "2", "-out", "tls.crt", "-extfile", "leaf.cnf"),
        cwd=folder,
    )


def container_tool(tool: str):
    """run(*args, timeout) → stdout, failing the tests with the tool's own words."""

    def run(*args, timeout: int = 60, check: bool = True) -> str:
        what = f"{Path(tool).name} {args[0]}"
        try:
            done = subprocess.run([tool, *args], capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            pytest.fail(f"{what}: no answer in {timeout} s", pytrace=False)
        if check and done.returncode:  # a stopped VM, an image that can't be pulled
            pytest.fail(f"{what}: {done.stderr.strip()}", pytrace=False)
        return done.stdout + done.stderr  # logs replays the container's stderr on stderr

    return run


@pytest.fixture(scope="module")
def dovecot(tmp_path_factory):
    # Asked for with -m imap_server (conftest.py skips them otherwise), so what's
    # missing fails them: a skip would pass a run that tested nothing.
    tool = shutil.which("podman") or shutil.which("docker")
    if tool is None or shutil.which("openssl") is None:
        pytest.fail("-m imap_server needs podman or docker, and openssl", pytrace=False)
    run = container_tool(tool)
    run("info", timeout=30)  # the engine answers (podman machine start?) before any work
    folder = tmp_path_factory.mktemp("dovecot")
    make_certificates(folder)
    name = f"mailcellar-test-{os.getpid()}"
    try:
        run(
            *("create", "--name", name, "-e", f"USER_PASSWORD={PASSWORD}"),
            *("-p", f"127.0.0.1::{IMAPS}", DOVECOT_IMAGE),
            timeout=600,  # the first run pulls the image
        )
        for part in ("tls.crt", "tls.key"):
            run("cp", str(folder / part), f"{name}:/etc/dovecot/ssl/{part}")
        run("start", name)
        port = int(run("port", name, f"{IMAPS}/tcp").split()[0].rpartition(":")[2])
        server = Server(port, folder / "ca.crt")
        for _ in range(60):  # until it answers a login
            try:
                with server.session("probe@example.org"):
                    break
            except OSError, imaplib.IMAP4.error:
                time.sleep(0.5)
        else:
            pytest.fail(f"Dovecot didn't come up:\n{run('logs', name, check=False)}")
        yield server
    finally:
        run("rm", "-f", name, check=False)


# ── helpers ──────────────────────────────────────────────────────────────────


def set_up(
    dovecot: Server, monkeypatch, user: str, password: str = PASSWORD, cafile=True, extra=()
):
    """mailcellar setup USER --imap localhost …, as account `home`."""
    monkeypatch.setattr(setup.getpass, "getpass", lambda prompt: password)
    ca = ["--cafile", str(dovecot.cafile)] if cafile else []
    return setup.main(
        [user, "--imap", "localhost", "--port", str(dovecot.port), "--name", "home", *ca, *extra]
    )


def uid_of(conn, folder: str, msgid: str) -> bytes:
    conn.select(f'"{folder}"')
    typ, data = conn.uid("SEARCH", None, "HEADER", "Message-ID", msgid)
    assert typ == "OK" and data[0], (folder, msgid)
    return data[0].split()[0]


def move(conn, src: str, msgid: str, dst: str) -> None:
    """COPY, then expunge the original: a move any server can do."""
    uid = uid_of(conn, src, msgid)
    assert conn.uid("COPY", uid, f'"{dst}"')[0] == "OK"
    conn.uid("STORE", uid, "+FLAGS", r"(\Deleted)")
    conn.expunge()


def trash_folder(conn) -> str:
    """The folder the server marks \\Trash, created if there is none."""
    _typ, lines = conn.list()
    marked = [name for name, _d, flags in imap.list_entries(lines) if "\\Trash" in flags]
    if marked:
        return marked[0]
    assert conn.create("Trash")[0] == "OK"
    return "Trash"


def unseen(conn, folder: str) -> int:
    conn.select(f'"{folder}"', readonly=True)
    _typ, data = conn.search(None, "UNSEEN")
    return len(data[0].split())


def labels(address: str) -> dict[str, set[str]]:
    """{Message-ID: labels} from the backup's gyb index."""
    folder = backup_folder(address)
    con = sqlite3.connect(folder / "msg-db.sqlite")
    try:
        out = {}
        for filename, num in con.execute("SELECT message_filename, message_num FROM messages"):
            raw = (folder / filename).read_bytes()
            mid = raw.split(b"Message-ID: ")[1].split(b"\n")[0].decode().strip()
            out[mid] = {
                lb
                for (lb,) in con.execute("SELECT label FROM labels WHERE message_num = ?", (num,))
            }
        return out
    finally:
        con.close()


# ── the tests ────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def no_connection_left_open():
    """A socket left open fails the test that left it, not some later one."""
    yield
    gc.collect()


def test_setup_then_backups_follow_the_mailbox(dovecot, monkeypatch, capsys):
    user = "flow@example.org"
    with dovecot.session(user) as c:
        c.create("Projects")
        trash = trash_folder(c)
        c.append("INBOX", r"(\Seen)", None, make_eml(msgid="<a@x>"))
        c.append("INBOX", None, None, make_eml(msgid="<b@x>"))
        c.append("Projects", r"(\Seen \Flagged)", None, make_eml(msgid="<c@x>"))

        assert set_up(dovecot, monkeypatch, user) == 0
        assert "folders backed up" in capsys.readouterr().out
        assert cli.main(["backup", "home"]) == 0
        assert labels(user) == {
            "<a@x>": {"INBOX"},
            "<b@x>": {"INBOX", "UNREAD"},
            "<c@x>": {"Projects", "STARRED"},
        }
        assert unseen(c, "INBOX") == 1  # backing up read b@x without marking it read

        move(c, "INBOX", "<a@x>", "Projects")  # filed
        move(c, "INBOX", "<b@x>", trash)  # thrown away
        c.append("INBOX", r"(\Seen)", None, make_eml(msgid="<d@x>"))  # new mail
        c.uid("STORE", uid_of(c, "Projects", "<c@x>"), "+FLAGS", r"(\Deleted)")
        c.expunge()  # c@x deleted for good
        capsys.readouterr()
        assert cli.main(["backup", "home"]) == 0
        out = capsys.readouterr().out
        assert "Downloaded 1 messages" in out
        assert "1 backed-up messages were permanently deleted from the server" in out
        got = labels(user)
        assert got["<a@x>"] == {"Projects"} and got["<d@x>"] == {"INBOX"}
        assert "TRASH" in got["<b@x>"] and "<c@x>" in got  # both still in the backup

        assert cli.main(["count", "home"]) == 0
        assert cli.main(["verify", "home", "--chain"]) == 0


class CutShort:
    """A real connection whose SEARCH ALL keeps only the highest UIDs, as MESSAGELIMIT does."""

    def __init__(self, conn, limit: int):
        self._conn, self._limit = conn, limit

    def uid(self, command, *args):
        typ, data = self._conn.uid(command, *args)
        if command == "SEARCH" and args == (None, "ALL"):
            kept = sorted(imap.search_uids(data))[-self._limit :]
            data = [b" ".join(str(u).encode() for u in kept)]
        return typ, data

    def __getattr__(self, name):
        return getattr(self._conn, name)


def test_a_real_server_answers_the_listings_later_passes(dovecot):
    user = "passes@example.org"
    with dovecot.session(user) as c:
        for n in range(5):
            c.append("INBOX", r"(\Seen)", None, make_eml(msgid=f"<p{n}@x>"))
    context = ssl.create_default_context(cafile=dovecot.cafile)
    settings = gmailapi.ImapSettings("localhost", dovecot.port, user, dovecot.cafile)
    box = imap.ImapAccount(
        user,
        settings,
        PASSWORD,
        connect=lambda: CutShort(
            imaplib.IMAP4_SSL("localhost", dovecot.port, ssl_context=context, timeout=30), 2
        ),
    )
    with closing(box):
        # Dovecot parses UID SEARCH UID 1:n for the passes below the first 2.
        assert len(box.list_message_ids()) == 5


def test_a_wrong_password_writes_nothing(dovecot, monkeypatch, tmp_path):
    with pytest.raises(SystemExit, match="refused the login.*nothing was written"):
        set_up(dovecot, monkeypatch, "wrong@example.org", password="not it")
    assert not (tmp_path / "config").exists()


def test_a_certificate_from_an_unknown_ca_is_refused(dovecot, monkeypatch, tmp_path):
    with pytest.raises(SystemExit, match="(?i)certificate verify failed"):
        set_up(dovecot, monkeypatch, "noca@example.org", cafile=False)
    assert not (tmp_path / "config").exists()


# ── the write side (#113): offload, then restore, on a real server ───────────

OLD = imaplib.Time2Internaldate(1_420_106_400)  # 2015-01-01 10:00 UTC
OLD_MS = 1_420_106_400_000
OFFLOAD = ["offload", "apply", "--plan", "plan.csv", "--log", "log.csv"]


@pytest.fixture
def no_second_copy_check(monkeypatch):
    """offload's report of other copies of the backup (Time Machine's), not this test's."""
    monkeypatch.setattr(offload_cli, "second_copy_report", lambda d, ms: ["(report)"])


def seeded(dovecot, monkeypatch, tmp_path, user, review="keyword"):
    """Three old messages in Projects, the account set up and backed up, a plan made of them."""
    monkeypatch.chdir(tmp_path)
    with dovecot.session(user) as c:
        assert c.create("Projects")[0] == "OK"
        for n in range(3):
            raw = make_eml(msgid=f"<p{n}-{user}>").replace(b"\n", b"\r\n")
            assert c.append("Projects", r"(\Seen)", OLD, raw)[0] == "OK"
    assert set_up(dovecot, monkeypatch, user, extra=["--review", review]) == 0
    assert cli.main(["backup", "home"]) == 0
    (tmp_path / "rules.toml").write_text(
        '[[rule]]\nname = "old"\nlabel = "Projects"\nbefore = "2020-01-01"\n'
    )
    cli.main(["offload", "plan", "--rules", "rules.toml", "--plan", "plan.csv"])
    assert len(log_rows(tmp_path / "plan.csv")) == 3  # all three planned


def log_rows(path) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def outcomes(tmp_path, mode: str) -> list[str]:
    """offload apply's outcomes for one mode (label, trash), from its log."""
    rows = log_rows(tmp_path / "log.csv")
    return sorted(r["outcome"] for r in rows if r["mode"].startswith(mode))


def counts(dovecot, user) -> dict[str, int]:
    """Each folder's message count, in one session."""
    with dovecot.session(user) as c:
        _typ, lines = c.list()
        names = [n for n, _d, flags in imap.list_entries(lines) if not flags & imap.SKIP]
        return {n: int(c.select(f'"{n}"', readonly=True)[1][0]) for n in names}


def projects(dovecot, user) -> list[imap.Fetched]:
    """Every message in Projects, with its flags and INTERNALDATE."""
    with dovecot.session(user) as c:
        c.select('"Projects"', readonly=True)
        _typ, data = c.uid("FETCH", "1:*", "(FLAGS INTERNALDATE)")
        return list(imap.parse_fetch(data).values())


def test_offload_by_keyword_then_restore_from_trash(
    dovecot, monkeypatch, tmp_path, no_second_copy_check
):
    user = "kw@example.org"
    seeded(dovecot, monkeypatch, tmp_path, user)
    cli.main([*OFFLOAD, "--label"])
    assert outcomes(tmp_path, "label") == ["labelled"] * 3
    with dovecot.session(user) as c:  # the user keeps one: clears its keyword
        c.select('"Projects"')
        _typ, data = c.uid("SEARCH", None, "KEYWORD", "$mailcellar-offload-candidate")
        assert len(data[0].split()) == 3
        kept = data[0].split()[0]
        assert c.uid("STORE", kept, "-FLAGS", "($mailcellar-offload-candidate)")[0] == "OK"
    assert cli.main(["backup", "home"]) == 0
    cli.main([*OFFLOAD, "--trash", "--yes"])
    assert outcomes(tmp_path, "trash") == ["skipped", "trashed", "trashed"]
    now = counts(dovecot, user)
    assert now["Trash"] == 2 and now["Projects"] == 1
    assert cli.main(["restore", "home", "--offloaded", "--yes"]) == 0
    now = counts(dovecot, user)
    assert now["Trash"] == 0 and now["Projects"] == 3
    back = projects(dovecot, user)
    assert all("\\Seen" in m.flags and m.internal == OLD_MS for m in back)  # as they were
    assert not any("$mailcellar-offload-candidate" in m.flags for m in back)  # mark taken off
    restored = [m for m in back if any(f.startswith("$mailcellar-restored_") for f in m.flags)]
    assert len(restored) == 2  # the keyword, '.' and all, kept by a real server


@pytest.mark.parametrize("appenduid", [True, False])
def test_offload_by_folder_then_restore_after_trash_is_emptied(
    dovecot, monkeypatch, tmp_path, capsys, no_second_copy_check, appenduid
):
    user = f"folder-{appenduid}@example.org".lower()
    if not appenduid:  # as a server without UIDPLUS: matched from UIDNEXT by Message-ID and date
        monkeypatch.setattr(imap, "append_uid", lambda data: None)
    review = "mailcellar/review/offload-candidate"
    seeded(dovecot, monkeypatch, tmp_path, user, review="folder")
    cli.main([*OFFLOAD, "--label"])
    assert outcomes(tmp_path, "label") == ["labelled"] * 3
    now = counts(dovecot, user)
    assert now[review] == 3 and now["Projects"] == 0
    with dovecot.session(user) as c:  # the user keeps one: moves it back out
        c.select(f'"{review}"')
        _typ, data = c.uid("SEARCH", None, "ALL")
        assert c.uid("MOVE", data[0].split()[0], '"Projects"')[0] == "OK"
    assert cli.main(["backup", "home"]) == 0
    cli.main([*OFFLOAD, "--trash", "--yes"])
    assert outcomes(tmp_path, "trash") == ["skipped", "trashed", "trashed"]
    now = counts(dovecot, user)
    assert now["Trash"] == 2 and now[review] == 0
    with dovecot.session(user) as c:  # the server, or a mail client, empties Trash
        c.select('"Trash"')
        c.store("1:*", "+FLAGS", r"(\Deleted)")
        c.expunge()
    assert cli.main(["backup", "home"]) == 0
    capsys.readouterr()
    assert cli.main(["restore", "home", "--offloaded", "--yes"]) == 0
    assert "couldn't be read back" not in capsys.readouterr().err  # read back, not predicted
    back = projects(dovecot, user)
    assert len(back) == 3  # appended into the folder they lived in
    assert all("\\Seen" in m.flags and m.internal == OLD_MS for m in back)  # flags, date
    capsys.readouterr()
    assert cli.main(["backup", "home"]) == 0
    assert "Downloaded 0 messages" in capsys.readouterr().out  # the same IDs on a real server
