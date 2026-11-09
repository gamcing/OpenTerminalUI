"""Integration test against the disposable local Wrangler D1 gateway."""
from backend.cloudflare.d1 import D1BatchClient, D1BatchOutcomeUnknown, D1Statement

client = D1BatchClient(
    "http://127.0.0.1:8899",
    "test-secret-only-not-for-production-1234567890",
    allow_insecure_localhost=True,
)
try:
    client.batch([
        D1Statement("INSERT INTO bridge_test VALUES (?, ?)", (10, "three")),
        D1Statement("INSERT INTO bridge_test VALUES (?, ?)", (11, "three")),
    ])
except D1BatchOutcomeUnknown:
    pass
else:
    raise AssertionError("a duplicate unique value should fail the batch")
rows = client.batch([D1Statement("SELECT count(*) AS n FROM bridge_test")])[0]["results"]
assert rows[0]["n"] == 2, "failed batch must not persist a partial write"
print("Python client and local D1: failed batch rolled back")
