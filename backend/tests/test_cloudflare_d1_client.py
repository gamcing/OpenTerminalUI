from __future__ import annotations

import io
import json
import unittest
import urllib.error

from backend.cloudflare.d1 import D1BatchClient, D1BatchOutcomeUnknown, D1BatchRejected, D1Statement

TOKEN = "test-secret-only-not-for-production-1234567890"


class BatchClientTest(unittest.TestCase):
    def test_sends_one_batch_without_retry(self):
        seen = []

        def opener(request, timeout):
            seen.append((request, timeout))
            return io.BytesIO(b'{"results":[{"success":true},{"success":true}]}')

        client = D1BatchClient("https://d1.example", TOKEN, opener=opener)
        result = client.batch([D1Statement("INSERT INTO t VALUES (?)", [7]), D1Statement("SELECT * FROM t")])
        self.assertEqual(len(result), 2)
        self.assertEqual(len(seen), 1)
        request, timeout = seen[0]
        self.assertEqual(request.full_url, "https://d1.example/batch")
        self.assertEqual(request.get_header("Authorization"), f"Bearer {TOKEN}")
        self.assertEqual(json.loads(request.data)["statements"][0]["params"], [7])
        self.assertEqual(timeout, 30)

    def test_rejects_insecure_or_malformed_origins_and_short_tokens(self):
        for origin in ("http://remote.example", "https://user:pass@example.com", "https://example.com/path", "https://example.com?x=1"):
            with self.subTest(origin=origin), self.assertRaises(ValueError):
                D1BatchClient(origin, TOKEN)
        with self.assertRaises(ValueError):
            D1BatchClient("https://example.com", "short")
        D1BatchClient("http://127.0.0.1:8899", TOKEN, allow_insecure_localhost=True)
        with self.assertRaises(ValueError):
            D1BatchClient("http://remote.example", TOKEN, allow_insecure_localhost=True)
        D1BatchClient("http://openterminal-d1.internal", TOKEN, allow_intercepted_container_origin=True)
        with self.assertRaises(ValueError):
            D1BatchClient("http://openterminal-d1.internal", TOKEN)
        with self.assertRaises(ValueError):
            D1BatchClient("http://remote.example", TOKEN, allow_intercepted_container_origin=True)

    def test_no_retry_on_failed_or_ambiguous_response(self):
        seen = []
        def opener(request, timeout):
            seen.append(request)
            raise urllib.error.HTTPError(request.full_url, 422, "failure", {}, None)
        client = D1BatchClient("https://d1.example", TOKEN, opener=opener)
        with self.assertRaises(D1BatchOutcomeUnknown):
            client.batch([D1Statement("UPDATE t SET value = 1")])
        self.assertEqual(len(seen), 1)

    def test_validation_and_unexpected_response(self):
        def rejected(request, timeout):
            raise urllib.error.HTTPError(request.full_url, 400, "bad", {}, None)
        client = D1BatchClient("https://d1.example", TOKEN, opener=rejected)
        with self.assertRaises(D1BatchRejected):
            client.batch([D1Statement("SELECT 1")])
        client = D1BatchClient("https://d1.example", TOKEN, opener=lambda *args, **kwargs: io.BytesIO(b'{}'))
        with self.assertRaises(D1BatchOutcomeUnknown):
            client.batch([D1Statement("SELECT 1")])
        with self.assertRaises(ValueError):
            client.batch([])


if __name__ == "__main__":
    unittest.main()
