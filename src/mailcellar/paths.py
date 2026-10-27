"""
paths: where the config folder (accounts, settings, OAuth files) and the
data root (backups and everything derived from them) are.

  <config>/accounts.toml, settings.toml, <name>/  ← config_root()
  <data>/<email>/                                 ← one backup per account (gyb's format)
  <data>/_pruned/<email>/<timestamp>/             ← messages moved out by backup --prune
  <data>/dedup/, offload/, restore/               ← plans, logs, indexes

config_root(): $MAILCELLAR_CONFIG, else ~/.config/mailcellar, else ./config.
data_root(): $MAILCELLAR_DATA, else data_dir in <config>/settings.toml, else
~/mailcellar (or ./data, the old default, where that already holds mailcellar's
data and ~/mailcellar doesn't exist). They are functions, so a changed setting
applies.
"""

from pathlib import Path

DATA_DIR = "~/mailcellar"  # the default data root: visible, since it holds the user's mail
LEGACY_DATA_DIR = "data"  # the old default, relative to the working directory
DATA_SUBDIRS = ("dedup", "offload", "restore", "search", "_pruned")  # what mailcellar writes there
DATA_ENV = "MAILCELLAR_DATA"
CONFIG_ENV = "MAILCELLAR_CONFIG"
SETTINGS_NAME = "settings.toml"  # in the config folder: data_dir = "~/data/mailcellar"
SETTINGS_KEYS = {"data_dir", "token_warn_days", "keep_modify_token"}
TOKEN_WARN_DAYS = 5  # days after consent when a token starts to be reported as ageing
PRUNED_DIR = "_pruned"  # <data>/_pruned/<email>/<timestamp>/, written by --prune


def _path(value: str) -> Path:
    """'~' expanded; a relative path taken from the working directory."""
    path = Path(value).expanduser()
    return path if path.is_absolute() else Path.cwd() / path


def config_setting() -> tuple[Path, str]:
    """
    (the config folder, where that came from). The folder holds
    accounts.toml, settings.toml and one folder of OAuth files per account:
    $MAILCELLAR_CONFIG, else ~/.config/mailcellar ($XDG_CONFIG_HOME
    honoured) if it exists, else ./config if it exists (the old place,
    inside the project), else ~/.config/mailcellar.
    """
    import os

    value = os.environ.get(CONFIG_ENV, "").strip()
    if value:
        return _path(value), f"${CONFIG_ENV}"
    user = _path(os.environ.get("XDG_CONFIG_HOME", "").strip() or "~/.config") / "mailcellar"
    legacy = Path.cwd() / "config"
    if not user.is_dir() and legacy.is_dir():
        return legacy, "./config (the old place, in the project)"
    return user, "default"


def config_root() -> Path:
    """The config folder; see config_setting()."""
    return config_setting()[0]


def data_setting() -> tuple[str, str]:
    """(the data root as set, where it was set): environment, settings file or default."""
    import os
    import tomllib

    value = os.environ.get(DATA_ENV, "").strip()
    if value:
        return value, f"${DATA_ENV}"
    settings_file = config_root() / SETTINGS_NAME
    if settings_file.is_file():
        with settings_file.open("rb") as fh:
            settings = tomllib.load(fh)
        unknown = set(settings) - SETTINGS_KEYS
        if unknown:
            raise ValueError(f"{settings_file}: unknown settings {sorted(unknown)}")
        value = settings.get("data_dir", "")
        if not isinstance(value, str):
            raise ValueError(f"{settings_file}: data_dir must be a path")
        if value.strip():
            return value.strip(), str(settings_file)
    if not _path(DATA_DIR).is_dir() and holds_data(_path(LEGACY_DATA_DIR)):
        return LEGACY_DATA_DIR, "./data (the old default, in this folder)"
    return DATA_DIR, "default"


def holds_data(folder: Path) -> bool:
    """Whether a folder holds mailcellar's data: a backup, or a folder mailcellar writes."""
    if not folder.is_dir():
        return False
    return any((folder / d).is_dir() for d in DATA_SUBDIRS) or bool(find_backups(folder))


def token_warn_days(config: Path | None = None) -> int:
    """
    Days after consent when a token is reported as ageing (token_warn_days in
    <config>/settings.toml; 0 turns the warning off). With the consent screen
    in Testing status, Google expires a token after 7 days; mailcellar can't
    see that status, so the warning goes by the time since consent.
    """
    import tomllib

    settings_file = (config or config_root()) / SETTINGS_NAME
    if not settings_file.is_file():
        return TOKEN_WARN_DAYS
    with settings_file.open("rb") as fh:
        value = tomllib.load(fh).get("token_warn_days", TOKEN_WARN_DAYS)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{settings_file}: token_warn_days must be a number of days, or 0")
    return value


def keep_modify_token(config: Path | None = None) -> bool:
    """
    Whether a gmail.modify token is stored, like a read-only one
    (keep_modify_token = true in <config>/settings.toml). By default it isn't:
    a command that changes Gmail signs in when it runs and holds the token in
    memory, so no file can give someone else write access to the mailbox.
    """
    import tomllib

    settings_file = (config or config_root()) / SETTINGS_NAME
    if not settings_file.is_file():
        return False
    with settings_file.open("rb") as fh:
        value = tomllib.load(fh).get("keep_modify_token", False)
    if not isinstance(value, bool):
        raise ValueError(f"{settings_file}: keep_modify_token must be true or false")
    return value


def data_root() -> Path:
    """
    Where backups and everything derived from them live: $MAILCELLAR_DATA,
    else data_dir in <config>/settings.toml, else ./data. "~" is expanded;
    a relative path is taken from the working directory. Read on every
    call, so it is never stale.
    """
    return _path(data_setting()[0])


def make_data_root(data: Path | None = None) -> None:
    """
    Create the data root owner-only, if it isn't there yet (data:
    data_root()). It holds every backed-up message; left to the first
    mkdir(parents=True) under it, it took the umask's mode and other users
    could read the mail. A root that exists keeps the mode it has.
    """
    data = data or data_root()
    if not data.is_dir():
        data.mkdir(parents=True, mode=0o700)


def backup_folder(account: str, data: Path | None = None) -> Path:
    """Where an account's backup lives: <data>/<email>/ (data: data_root())."""
    return (data or data_root()) / account


def pruned_folder(backup_dir: Path) -> Path:
    """Where `backup --prune` moves a backup's pruned messages: <data>/_pruned/<email>/."""
    return backup_dir.parent / PRUNED_DIR / backup_dir.name


def write_private(path: Path, data: bytes) -> None:
    """
    Write a file only its owner can read (mode 600), for tokens and OAuth
    clients: a new file beside it, owner-only before the first byte is
    written, renamed over `path`. Never readable by others, whatever the
    umask or the old file's mode, and never left half-written. The new file
    has a name of its own, so two processes writing the same token at once
    (the scheduled backup and a command typed by hand) can't mix their bytes.
    """
    import os
    import tempfile

    fd, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".part")
    part = Path(name)
    try:
        with open(fd, "wb") as fh:
            os.fchmod(fh.fileno(), 0o600)  # mkstemp's is 600 already; say so whatever it does
            fh.write(data)
        os.replace(part, path)
    except BaseException:
        part.unlink(missing_ok=True)
        raise


def find_backups(data: Path | None = None) -> list[Path]:
    """Every backup folder in <data> (data_root()), i.e. those with a msg-db.sqlite."""
    data = data or data_root()
    return sorted(d for d in data.glob("*") if (d / "msg-db.sqlite").is_file())
