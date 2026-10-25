"""
paths: where the config folder (accounts, settings, OAuth files) and the
data root (backups and everything derived from them) are.

  <config>/accounts.toml, settings.toml, <name>/  ← config_root()
  <data>/<email>/                                 ← one backup per account (gyb's format)
  <data>/_pruned/<email>/<timestamp>/             ← messages moved out by backup --sync
  <data>/dedup/, offload/, restore/               ← plans, logs, indexes

config_root(): $MAILCELLAR_CONFIG, else ~/.config/mailcellar, else ./config.
data_root(): $MAILCELLAR_DATA, else data_dir in <config>/settings.toml, else ./data.
They are functions, so a changed setting applies.
"""

from pathlib import Path

DATA_DIR = "data"  # the default data root, relative to the working directory
DATA_ENV = "MAILCELLAR_DATA"
CONFIG_ENV = "MAILCELLAR_CONFIG"
SETTINGS_NAME = "settings.toml"  # in the config folder: data_dir = "~/data/mailcellar"
PRUNED_DIR = "_pruned"  # <data>/_pruned/<email>/<timestamp>/, written by --sync


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
        unknown = set(settings) - {"data_dir"}
        if unknown:
            raise ValueError(f"{settings_file}: unknown settings {sorted(unknown)}")
        value = settings.get("data_dir", "")
        if not isinstance(value, str):
            raise ValueError(f"{settings_file}: data_dir must be a path")
        if value.strip():
            return value.strip(), str(settings_file)
    return DATA_DIR, "default"


def data_root() -> Path:
    """
    Where backups and everything derived from them live: $MAILCELLAR_DATA,
    else data_dir in <config>/settings.toml, else ./data. "~" is expanded;
    a relative path is taken from the working directory. Read on every
    call, so it is never stale.
    """
    return _path(data_setting()[0])


def backup_folder(account: str, data: Path | None = None) -> Path:
    """Where an account's backup lives: <data>/<email>/ (data: data_root())."""
    return (data or data_root()) / account


def pruned_folder(backup_dir: Path) -> Path:
    """Where `backup --sync` moves a backup's pruned messages: <data>/_pruned/<email>/."""
    return backup_dir.parent / PRUNED_DIR / backup_dir.name


def find_backups(data: Path | None = None) -> list[Path]:
    """Every backup folder in <data> (data_root()), i.e. those with a msg-db.sqlite."""
    data = data or data_root()
    return sorted(d for d in data.glob("*") if (d / "msg-db.sqlite").is_file())
