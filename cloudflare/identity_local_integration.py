"""Verify the D1 identity repository against disposable local D1."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

from backend.cloudflare.d1 import D1BatchClient, D1BatchOutcomeUnknown, D1Statement
from backend.cloudflare.identity import D1IdentityRepository

client = D1BatchClient("http://127.0.0.1:8899", "test-secret-only-not-for-production-1234567890", allow_insecure_localhost=True)
source = Path("cloudflare/migrations/0001_identity_saved_views.sql").read_text(encoding="utf-8")
client.batch([D1Statement(sql.strip()) for sql in source.split(";") if sql.strip()])
client.batch([D1Statement("DELETE FROM users")])
identity = D1IdentityRepository(client)
# First-run bootstrap must create only one administrator, even under concurrency.
with ThreadPoolExecutor(max_workers=2) as pool:
    candidates = list(pool.map(lambda email: identity.seed_first_admin(email, "admin-hash"),
                               [" Admin@Example.test ", "second@example.test"]))
admins = [candidate for candidate in candidates if candidate is not None]
assert len(admins) == 1 and admins[0].role == "admin", candidates
admin = admins[0]
assert identity.seed_first_admin("third@example.test", "hash") is None
alice = identity.create_user(" Alice@Example.test ", "alice-hash")
assert alice is not None and alice.email == "alice@example.test" and alice.role == "viewer"
assert identity.create_user("alice@example.test", "other") is None
assert identity.get_by_id(alice.id).email == alice.email
assert identity.get_by_email(" ALICE@example.test ").id == alice.id
now = datetime.now(timezone.utc)
expiry = now + timedelta(days=1)
assert identity.record_login_and_issue_refresh(alice.id, "old-jti", expiry)
assert identity.get_by_id(alice.id).last_login is not None
assert not identity.record_login_and_issue_refresh("missing", "never-created", expiry)
assert not identity.rotate_refresh(admin.id, "old-jti", "wrong-user", expiry)
assert identity.rotate_refresh(alice.id, "old-jti", "new-jti", expiry)
assert not identity.rotate_refresh(alice.id, "old-jti", "replay", expiry)
assert identity.record_login_and_issue_refresh(alice.id, "expired-jti", now - timedelta(seconds=1))
assert not identity.rotate_refresh(alice.id, "expired-jti", "expired-new", expiry)

# A conflict during the first statement must not revoke the original token.
assert identity.record_login_and_issue_refresh(alice.id, "rollback-old", expiry)
try:
    identity.rotate_refresh(alice.id, "rollback-old", "new-jti", expiry)
except D1BatchOutcomeUnknown:
    pass
else:
    raise AssertionError("duplicate refresh token identifier should fail")
assert identity.rotate_refresh(alice.id, "rollback-old", "rollback-new", expiry)

# A failure after inserting a replacement must roll the insertion back as well.
assert identity.record_login_and_issue_refresh(alice.id, "trigger-old", expiry)
client.batch([D1Statement("CREATE TRIGGER reject_revocation BEFORE UPDATE ON refresh_tokens "
                          "WHEN OLD.jti = 'trigger-old' BEGIN SELECT RAISE(ABORT, 'test'); END")])
try:
    identity.rotate_refresh(alice.id, "trigger-old", "trigger-new", expiry)
except D1BatchOutcomeUnknown:
    pass
else:
    raise AssertionError("trigger should abort token revocation")
finally:
    client.batch([D1Statement("DROP TRIGGER reject_revocation")])
rows = client.batch([D1Statement("SELECT jti, revoked_at FROM refresh_tokens WHERE jti IN (?,?)",
                                 ["trigger-old", "trigger-new"])])[0]["results"]
assert len(rows) == 1 and rows[0]["jti"] == "trigger-old" and rows[0]["revoked_at"] is None, rows
assert identity.rotate_refresh(alice.id, "trigger-old", "trigger-new", expiry)

# D1 serializes each batch: two concurrent refreshes may produce only one token.
assert identity.record_login_and_issue_refresh(alice.id, "race-old", expiry)
def rotate(jti):
    return identity.rotate_refresh(alice.id, "race-old", jti, expiry)
with ThreadPoolExecutor(max_workers=2) as pool:
    outcomes = list(pool.map(rotate, ["race-a", "race-b"]))
assert sorted(outcomes) == [False, True], outcomes
rows = client.batch([D1Statement("SELECT jti FROM refresh_tokens WHERE jti IN (?,?)", ["race-a", "race-b"])])[0]["results"]
assert len(rows) == 1, rows
print("D1 identity: bootstrap, uniqueness, expiry, replay, rollback and concurrent rotation verified")
