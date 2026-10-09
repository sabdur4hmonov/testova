"""Telegram ids for tests that can never belong to a real account.

WHY THIS EXISTS. Eight test files used to pass the real admin's telegram_id
(8206475760, the `ADMIN_IDS` entry in `.env`) as the actor of admin actions —
seven as a literal, one via `settings.ADMIN_IDS[0]`. While those files ran
against the live database, every `admin_log` row they leaked carried that id,
so 3,146 fabricated audit entries (setbooklimit / setprice / plan / ...) sat in
the real admin's audit trail and could not be told apart from real actions by
actor at all. See logs/deleted-rows/admin-log-FINDINGS-*.json.

WHY 2**52 AND UP. The Bot API documents user identifiers as having "at most 52
significant bits", so any id >= 2**52 is structurally impossible for a real
user. It stays below 2**53, so it also round-trips exactly through a JSON ->
JavaScript number (the admin panel), fits a Postgres bigint, and is positive,
so nothing that parses ids as `int > 0` rejects it.

The admin handlers authorise on `is_admin OR telegram_id in ADMIN_IDS`; tests
pass `is_admin=True`, so no `ADMIN_IDS` monkeypatch is needed to use these.
"""

TELEGRAM_USER_ID_LIMIT = 1 << 52
"""Exclusive upper bound of real Telegram user ids (Bot API: at most 52 bits)."""

TEST_ADMIN_ID = TELEGRAM_USER_ID_LIMIT + 1
"""The actor for admin actions in tests. Never a real account."""

TEST_TARGET_ID = TELEGRAM_USER_ID_LIMIT + 2
"""A fixed recipient/target for tests that need one. Never a real account —
test_direct_message.py used to log a message addressed to a real teacher."""
