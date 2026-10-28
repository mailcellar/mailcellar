---
paths:
  - "src/mailcellar/**/*.py"
---

# Fixing a bug

Before proposing a fix, find every path that reaches the changed code, and say
in the proposal which were checked:

- **Callers:** every call site of each function the fix changes (grep for it),
  and what each passes and expects back.
- **Both providers:** the same code runs for Gmail and IMAP accounts. Check the
  fix on each, and on an IMAP server with and without what it may lack
  (MOVE, UIDPLUS, custom keywords, MESSAGELIMIT).
- **The failure modes the fix adds:** a dropped connection, a retry, a server
  that answers differently from Dovecot, an empty or partial answer.
- **Output:** anything the fix prints or logs on a path an IMAP account
  reaches names the mailbox (`mailbox.mailbox_name`), not Gmail.

Fix the mechanism, not the one place a bug showed: if the cause is in shared
code, fix it there, and name the other places it reaches in the commit message.
