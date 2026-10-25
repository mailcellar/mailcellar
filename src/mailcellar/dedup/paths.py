"""
Default locations of everything dedup writes, next to the backups (in the
data root, paths.data_root()). Functions, so a changed setting applies.
"""

from pathlib import Path

from mailcellar.paths import data_root


def dedup_dir() -> Path:
    return data_root() / "dedup"


def default_db() -> Path:
    return dedup_dir() / "dedup.sqlite"


def default_plan() -> Path:
    return dedup_dir() / "dedup-plan.csv"


def default_log() -> Path:
    return dedup_dir() / "dedup-apply-log.csv"
