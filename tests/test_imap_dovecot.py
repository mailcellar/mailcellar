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

import gc
import imaplib
import os
import shutil
import sqlite3
import ssl
import subprocess
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest

from mailcellar import cli, imap
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


def set_up(dovecot: Server, monkeypatch, user: str, password: str = PASSWORD, cafile=True):
    """mailcellar setup USER --imap localhost …, as account `home`."""
    monkeypatch.setattr(setup.getpass, "getpass", lambda prompt: password)
    extra = ["--cafile", str(dovecot.cafile)] if cafile else []
    return setup.main(
        [user, "--imap", "localhost", "--port", str(dovecot.port), "--name", "home", *extra]
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


def test_a_wrong_password_writes_nothing(dovecot, monkeypatch, tmp_path):
    with pytest.raises(SystemExit, match="refused the login.*nothing was written"):
        set_up(dovecot, monkeypatch, "wrong@example.org", password="not it")
    assert not (tmp_path / "config").exists()


def test_a_certificate_from_an_unknown_ca_is_refused(dovecot, monkeypatch, tmp_path):
    with pytest.raises(SystemExit, match="(?i)certificate verify failed"):
        set_up(dovecot, monkeypatch, "noca@example.org", cafile=False)
    assert not (tmp_path / "config").exists()
