"""
senders: the top senders and domains in one account's backup, by count
and size, with the years each sender was active.

Fields come from the backup's meta.sqlite; the .eml is parsed only for
messages the sidecar is missing or has from an older parser. Years are
the year Gmail received each message (UTC); sizes are .eml file sizes.

Usage
  mailcellar report senders work
  mailcellar report senders work --top 30 --output report.txt
  mailcellar report senders work --min-count 5 --no-reply-to
"""

import argparse
import csv
import logging
import sys
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

from mailcellar.backup import sidecar
from mailcellar.backup.gyb import iter_backup
from mailcellar.message import parse_eml
from mailcellar.report.output import human_size

logger = logging.getLogger(__name__)


def domain_of(address: str) -> str:
    if "@" in address:
        return address.split("@", 1)[1]
    return "unknown"


def received_year(internal_date_ms: int | None, gyb_local_date: str) -> int | None:
    """UTC year Gmail received the message, from the sidecar or else msg-db.sqlite."""
    try:
        if internal_date_ms is None:
            internal_date_ms = datetime.fromisoformat(gyb_local_date).timestamp() * 1000
        return datetime.fromtimestamp(internal_date_ms / 1000, UTC).year
    except ValueError, OverflowError, OSError:
        return None


# ── analysis ─────────────────────────────────────────────────────────────────


def analyse(backup_dir: Path, min_count: int):
    """Walk the backup and accumulate per-sender and per-domain stats."""

    sender_count = defaultdict(int)
    sender_size = defaultdict(int)
    sender_name_map = {}  # addr → display name (last seen)
    sender_years = defaultdict(set)
    sender_replied = defaultdict(int)  # addr → messages with a Reply-To ≠ sender

    domain_count = defaultdict(int)
    domain_size = defaultdict(int)

    total = from_meta = parsed = errors = 0
    stored = sidecar.current_rows(backup_dir)

    logger.info("Scanning emails…")
    for i, bm in enumerate(iter_backup(backup_dir), 1):
        if i % 5000 == 0:
            logger.info(f"  …{i} processed")

        fields, internal_date = stored.get(bm.gmail_id, (None, None))
        if fields is not None:
            from_meta += 1
        else:
            try:
                fields = parse_eml(bm.path)
            except Exception as exc:
                logger.warning(f"Could not parse {bm.path.name}: {exc}")
                errors += 1
                continue
            parsed += 1

        addr = fields["sender_addr"] or "unknown"
        dom = domain_of(addr)
        sz = bm.path.stat().st_size
        yr = received_year(internal_date, bm.internal_date)

        sender_count[addr] += 1
        sender_size[addr] += sz
        domain_count[dom] += 1
        domain_size[dom] += sz
        total += 1

        if fields["sender_name"]:
            sender_name_map[addr] = fields["sender_name"]
        if yr:
            sender_years[addr].add(yr)
        if fields["reply_to"] and fields["reply_to"] != addr:
            sender_replied[addr] += 1

    logger.info(
        f"Scan complete. {total} messages: {from_meta} from meta.sqlite, "
        f"{parsed} parsed, {errors} skipped.\n"
    )

    # Filter by min_count
    senders = {a: c for a, c in sender_count.items() if c >= min_count}
    domains = {d: c for d, c in domain_count.items() if c >= min_count}

    return {
        "total": total,
        "errors": errors,
        "sender_count": senders,
        "sender_size": sender_size,
        "sender_name_map": sender_name_map,
        "sender_years": sender_years,
        "sender_replied": sender_replied,
        "domain_count": domains,
        "domain_size": domain_size,
    }


# ── reporting ─────────────────────────────────────────────────────────────────


def print_section(title: str, out):
    bar = "═" * 70
    out.write(f"\n{bar}\n  {title}\n{bar}\n")


def report(data: dict, top: int, show_reply_ratio: bool, out):
    total = data["total"]
    out.write("GYB Sender / Domain Analysis\n")
    out.write(f"Generated : {datetime.now().strftime('%Y-%m-%d %H:%M')}\n")
    out.write(f"Total msgs: {total:,}\n")

    # ── Top senders ──────────────────────────────────────────────────────────
    print_section(f"Top {top} Senders by Message Count", out)
    out.write(f"{'#':<5} {'Address':<45} {'Name':<25} {'Count':>7} {'% Total':>8} {'Size':>10}\n")
    out.write(f"{'-' * 5} {'-' * 45} {'-' * 25} {'-' * 7} {'-' * 8} {'-' * 10}\n")

    ranked = sorted(data["sender_count"].items(), key=lambda x: x[1], reverse=True)[:top]
    for rank, (addr, count) in enumerate(ranked, 1):
        name = data["sender_name_map"].get(addr, "")[:24]
        pct = count / total * 100 if total else 0
        sz = human_size(data["sender_size"].get(addr, 0))
        out.write(f"{rank:<5} {addr:<45} {name:<25} {count:>7,} {pct:>7.1f}% {sz:>10}\n")

    # ── Top senders by size ───────────────────────────────────────────────────
    print_section(f"Top {top} Senders by Total Size", out)
    out.write(f"{'#':<5} {'Address':<45} {'Size':>10} {'Msgs':>7}\n")
    out.write(f"{'-' * 5} {'-' * 45} {'-' * 10} {'-' * 7}\n")

    by_size = sorted(data["sender_size"].items(), key=lambda x: x[1], reverse=True)[:top]
    for rank, (addr, sz) in enumerate(by_size, 1):
        if addr not in data["sender_count"]:
            continue
        count = data["sender_count"].get(addr, 0)
        out.write(f"{rank:<5} {addr:<45} {human_size(sz):>10} {count:>7,}\n")

    # ── Top domains ───────────────────────────────────────────────────────────
    print_section(f"Top {top} Domains by Message Count", out)
    out.write(f"{'#':<5} {'Domain':<40} {'Count':>7} {'% Total':>8} {'Size':>10}\n")
    out.write(f"{'-' * 5} {'-' * 40} {'-' * 7} {'-' * 8} {'-' * 10}\n")

    dom_ranked = sorted(data["domain_count"].items(), key=lambda x: x[1], reverse=True)[:top]
    for rank, (dom, count) in enumerate(dom_ranked, 1):
        pct = count / total * 100 if total else 0
        sz = human_size(data["domain_size"].get(dom, 0))
        out.write(f"{rank:<5} {dom:<40} {count:>7,} {pct:>7.1f}% {sz:>10}\n")

    # ── Reply-To ratio (senders you've never replied to) ─────────────────────
    if show_reply_ratio:
        print_section("Senders with no Reply-To (likely automated / newsletters)", out)
        out.write("  These senders never set a Reply-To — strong candidates for clearing out.\n\n")
        out.write(f"{'Address':<50} {'Msgs':>7}\n")
        out.write(f"{'-' * 50} {'-' * 7}\n")

        no_reply = [
            (addr, cnt)
            for addr, cnt in data["sender_count"].items()
            if data["sender_replied"].get(addr, 0) == 0
        ]
        no_reply.sort(key=lambda x: x[1], reverse=True)
        for addr, cnt in no_reply[:top]:
            out.write(f"{addr:<50} {cnt:>7,}\n")

    # ── Year activity per sender ──────────────────────────────────────────────
    print_section(f"Sender Activity Years (top {top} senders)", out)
    out.write("  Shows which years each sender was active — helps spot stale senders.\n\n")
    out.write(f"{'Address':<50} {'Active years'}\n")
    out.write(f"{'-' * 50} {'-' * 30}\n")

    for addr, _count in ranked[:top]:
        years = sorted(data["sender_years"].get(addr, []))
        out.write(f"{addr:<50} {', '.join(str(y) for y in years)}\n")

    out.write("\n")


def export_csv(data: dict, csv_path: Path):
    """Write a flat CSV of all sender stats for further analysis."""
    total = data["total"]
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(
            [
                "address",
                "display_name",
                "domain",
                "count",
                "pct_total",
                "total_size_bytes",
                "has_reply_to",
                "active_years",
            ]
        )
        for addr, count in sorted(data["sender_count"].items(), key=lambda x: x[1], reverse=True):
            pct = round(count / total * 100, 4) if total else 0
            name = data["sender_name_map"].get(addr, "")
            dom = domain_of(addr)
            sz = data["sender_size"].get(addr, 0)
            has_reply = "yes" if data["sender_replied"].get(addr, 0) > 0 else "no"
            years = ",".join(str(y) for y in sorted(data["sender_years"].get(addr, [])))
            w.writerow([addr, name, dom, count, pct, sz, has_reply, years])
    logger.info(f"CSV exported → {csv_path}")


# ── main ─────────────────────────────────────────────────────────────────────


def main(argv=None, prog: str | None = None):
    p = argparse.ArgumentParser(prog=prog, description="Sender/Domain analysis of a Gmail backup.")
    p.add_argument(
        "--folder",
        required=True,
        help="Backup folder, e.g. <data root>/user@example.com (contains msg-db.sqlite)",
    )
    p.add_argument(
        "--top", type=int, default=25, help="Number of top senders/domains to show (default: 25)"
    )
    p.add_argument(
        "--min-count",
        type=int,
        default=1,
        help="Minimum message count to include a sender/domain (default: 1)",
    )
    p.add_argument(
        "--output", default=None, help="Write text report to this file (default: print to stdout)"
    )
    p.add_argument("--csv", default=None, help="Also write a CSV of all sender stats to this file")
    p.add_argument(
        "--no-reply-to",
        "--no-reply-ratio",
        dest="no_reply_to",
        action="store_true",
        help="Include a section listing senders with no Reply-To header",
    )
    args = p.parse_args(argv)

    backup_dir = Path(args.folder).expanduser().resolve()
    if not (backup_dir / "msg-db.sqlite").is_file():
        sys.exit(f"error: '{backup_dir}' is not a backup (no msg-db.sqlite).")

    data = analyse(backup_dir, args.min_count)

    if args.output:
        out_path = Path(args.output)
        with out_path.open("w", encoding="utf-8") as fh:
            report(data, args.top, args.no_reply_to, fh)
        print(f"Report written → {out_path}")
    else:
        report(data, args.top, args.no_reply_to, sys.stdout)

    if args.csv:
        export_csv(data, Path(args.csv))
