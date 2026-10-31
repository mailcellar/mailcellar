"""
Keep / remove / review rules applied by plan, and the label sets they use.
"""

# Gmail system label IDs as stored by gyb (system labels keep their ID).
STAR_LABELS = {
    "STARRED",
    "YELLOW_STAR",
    "ORANGE_STAR",
    "RED_STAR",
    "PURPLE_STAR",
    "BLUE_STAR",
    "GREEN_STAR",
    "RED_BANG",
    "ORANGE_GUILLEMET",
    "YELLOW_BANG",
    "GREEN_CHECK",
    "BLUE_INFO",
    "PURPLE_QUESTION",
}
SYSTEM_LABELS = STAR_LABELS | {
    "INBOX",
    "SPAM",
    "TRASH",
    "UNREAD",
    "IMPORTANT",
    "SENT",
    "DRAFT",
    "CHAT",
}


def is_user_label(label: str) -> bool:
    return label not in SYSTEM_LABELS and not label.startswith("CATEGORY_")


# --keep value for the rule-based keeper: keep the copy in the account a
# message was auto-forwarded from, and touch nothing else.
ORIGIN = "origin"


def removal_reason(labels: set[str], keeper_labels: set[str], ignored: set[str]) -> str:
    """
    Why a non-keeper copy must go to review rather than be removed;
    "" if it is safe to remove. keeper_labels is the union over every
    copy in the keep account.
    """
    if "SENT" in labels or "DRAFT" in labels:
        return "sent or draft copy"
    if keeper_labels & {"TRASH", "SPAM"}:
        return "keeper in trash or spam"
    if labels & STAR_LABELS and not keeper_labels & STAR_LABELS:
        return "starred, keeper not"
    missing = sorted(
        label for label in labels - keeper_labels if is_user_label(label) and label not in ignored
    )
    if missing:
        return "labels not on keeper: " + ", ".join(missing)
    return ""


def removal_decision(c: dict, keepers: list[dict], ignored: set[str]) -> tuple[str, str]:
    """(action, reason) for a copy that is a removal candidate."""
    keeper_labels = set().union(*(k["label_set"] for k in keepers))
    reason = removal_reason(c["label_set"], keeper_labels, ignored)
    return ("review", reason) if reason else ("remove", "")


def plan_fixed_group(group: list[dict], keep: str, ignored: set[str]):
    """--keep <account>: keep that account's copies, remove the rest."""
    keepers = [c for c in group if c["account"] == keep]
    keeper_id = keepers[0]["id"] if keepers else ""
    for c in group:
        if c["account"] == keep:
            yield c, "keep", "", keep, keeper_id
        elif not keepers:
            yield c, "review", "no copy in keep account", "", ""
        else:
            yield c, *removal_decision(c, keepers, ignored), keep, keeper_id


def forward_origins(c: dict, accounts: set[str]) -> set[str]:
    """Other accounts in the group that this copy was auto-forwarded from."""
    return set(c["forwarded_from"].split()) & (accounts - {c["account"]})


def plan_origin_group(group: list[dict], _keep: str, ignored: set[str]):
    """
    --keep origin: only copies created by auto-forwarding between the
    indexed accounts are removal candidates; the copy in the account the
    message was forwarded from is kept. Everything else is left untouched.
    """
    accounts = {c["account"] for c in group}
    origins_by_copy = [(c, forward_origins(c, accounts)) for c in group]
    origins = set().union(*(o for _c, o in origins_by_copy))

    if not origins:
        for c in group:
            yield c, "keep", "independent delivery", "", ""
        return
    if len(origins) > 1:
        for c, o in origins_by_copy:
            if o:
                yield c, "review", "forwarded in both directions", "", ""
            else:
                yield c, "keep", "not forwarded", "", ""
        return

    (origin,) = origins
    keepers = [c for c in group if c["account"] == origin]
    keeper_id = keepers[0]["id"]
    for c, o in origins_by_copy:
        if c["account"] == origin:
            yield c, "keep", "", origin, keeper_id
        elif o:
            yield c, *removal_decision(c, keepers, ignored), origin, keeper_id
        else:
            yield c, "keep", "not forwarded", "", ""


def labels_note(labels: set[str], keeper_labels: set[str]) -> str:
    added = sorted(labels - keeper_labels)
    missing = sorted(keeper_labels - labels)
    return (
        "labels differ:"
        + (" +" + ",".join(added) if added else "")
        + (" -" + ",".join(missing) if missing else "")
    )


def plan_within_group(group: list[dict], ignored: set[str]):
    """
    A group of copies inside one account (all with the same content). Keep
    one copy from the largest set of copies with the same labels (ties: the
    lowest Gmail ID); copies with the same labels are removal candidates,
    subject to the safety rules; copies with other labels go to review.
    """
    classes: dict[frozenset, list[dict]] = {}
    for c in group:
        classes.setdefault(frozenset(c["label_set"]), []).append(c)
    best = min(classes.values(), key=lambda cs: (-len(cs), min(c["id"] for c in cs)))
    keeper = min(best, key=lambda c: c["id"])
    account, keeper_id = keeper["account"], keeper["id"]
    for c in group:
        if c is keeper:
            yield c, "keep", "", account, keeper_id
        elif c["label_set"] != keeper["label_set"]:
            yield c, "review", labels_note(c["label_set"], keeper["label_set"]), account, keeper_id
        else:
            yield c, *removal_decision(c, [keeper], ignored), account, keeper_id
