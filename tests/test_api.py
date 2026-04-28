"""
Tests: _acquire_rate_limit_token and call_heroku_api.
"""
import unittest
from unittest.mock import patch, MagicMock, call
from tests.conftest import make_dyno, BaseLockTestCase
from django.core.cache import cache
import requests as req_lib


class TestAcquireRateLimitToken(BaseLockTestCase):


    def test_first_call_sets_counter_and_returns_true(self):
        dyno = make_dyno()
        result = dyno._acquire_rate_limit_token()
        self.assertTrue(result)
        key = f"heroku:api_rate:{dyno.app_name}"
        self.assertEqual(cache.get(key), 1)

    def test_second_call_within_limit_increments_and_returns_true(self):
        dyno = make_dyno()
        cache.set(f"heroku:api_rate:{dyno.app_name}", 5, timeout=60)
        result = dyno._acquire_rate_limit_token()
        self.assertTrue(result)

    def test_exhausted_budget_sleeps_and_returns_false_after_3_attempts(self):
        dyno = make_dyno()
        from django.conf import settings
        limit = 50
        cache.set(f"heroku:api_rate:{dyno.app_name}", limit, timeout=60)
        # Patch ttl to return 1 so wait is minimal
        from django.core.cache.backends.locmem import LocMemCache
        with patch.object(LocMemCache, "ttl", return_value=1):
            with patch("heroku_manager.heroku.time.sleep") as mock_sleep:
                result = dyno._acquire_rate_limit_token()
        self.assertFalse(result)
        self.assertEqual(mock_sleep.call_count, 3)

    def test_key_expiry_between_get_and_incr_handled(self):
        """ValueError from cache.incr when key expired mid-call must not raise."""
        dyno = make_dyno()
        cache.set(f"heroku:api_rate:{dyno.app_name}", 1, timeout=60)
        with patch.object(cache, "incr", side_effect=ValueError("expired")):
            result = dyno._acquire_rate_limit_token()
        self.assertTrue(result)


class TestCallHerokuApi(BaseLockTestCase):


    def _mock_response(self, status=200, json_data=None, headers=None, text="ok"):
        import requests as _req
        import json as _json
        resp = _req.Response()
        resp.status_code = status
        body = _json.dumps(json_data) if json_data else text
        resp._content = body.encode() if isinstance(body, str) else body
        resp.headers = _req.structures.CaseInsensitiveDict(headers or {})
        return resp

    def test_successful_patch_returns_response(self):
        dyno = make_dyno()
        mock_resp = self._mock_response(200)
        with patch("heroku_manager.heroku.requests.request", return_value=mock_resp) as mock_req:
            resp = dyno.call_heroku_api("PATCH", "https://api.heroku.com/apps/x/formation/y",
                                        data={"size": "standard-2x"})
        self.assertEqual(resp.status_code, 200)
        mock_req.assert_called_once()

    def test_get_response_is_cached(self):
        dyno = make_dyno()
        mock_resp = self._mock_response(200)
        with patch("heroku_manager.heroku.requests.request", return_value=mock_resp) as mock_req:
            r1 = dyno.call_heroku_api("GET", "https://api.heroku.com/apps/x/formation")
            r2 = dyno.call_heroku_api("GET", "https://api.heroku.com/apps/x/formation")
        # Second call should use cache, not hit requests
        self.assertEqual(mock_req.call_count, 1)
        self.assertEqual(r1.status_code, r2.status_code)

    def test_429_backoff_then_success(self):
        dyno = make_dyno()
        resp_429 = self._mock_response(429, headers={"Retry-After": "1"})
        resp_200 = self._mock_response(200)
        with patch("heroku_manager.heroku.requests.request",
                   side_effect=[resp_429, resp_200]):
            with patch("heroku_manager.heroku.time.sleep"):
                resp = dyno.call_heroku_api("PATCH", "https://api.heroku.com/x")
        self.assertEqual(resp.status_code, 200)

    def test_ssl_error_retries_and_returns_none_after_max(self):
        dyno = make_dyno()
        from requests.exceptions import SSLError
        with patch("heroku_manager.heroku.requests.request",
                   side_effect=SSLError("ssl fail")):
            with patch("heroku_manager.heroku.time.sleep"):
                resp = dyno.call_heroku_api("PATCH", "https://api.heroku.com/x")
        self.assertIsNone(resp)

    def test_timeout_error_retries(self):
        dyno = make_dyno()
        from requests.exceptions import Timeout
        good = self._mock_response(200)
        with patch("heroku_manager.heroku.requests.request",
                   side_effect=[Timeout(), Timeout(), good]):
            with patch("heroku_manager.heroku.time.sleep"):
                resp = dyno.call_heroku_api("PATCH", "https://api.heroku.com/x")
        self.assertEqual(resp.status_code, 200)

    def test_rate_limit_token_exhausted_returns_none(self):
        dyno = make_dyno()
        with patch.object(dyno, "_acquire_rate_limit_token", return_value=False):
            resp = dyno.call_heroku_api("PATCH", "https://api.heroku.com/x")
        self.assertIsNone(resp)

    def test_4xx_response_is_returned_and_logged(self):
        dyno = make_dyno()
        mock_resp = self._mock_response(404, text="not found")
        with patch("heroku_manager.heroku.requests.request", return_value=mock_resp):
            with patch("heroku_manager.heroku.logger") as mock_log:
                resp = dyno.call_heroku_api("DELETE", "https://api.heroku.com/x")
        self.assertEqual(resp.status_code, 404)
        mock_log.error.assert_called()

    def test_mutable_default_custom_headers_bug(self):
        """
        BUG: custom_headers={} is a mutable default argument.
        Two calls that mutate headers would share state.
        This test documents the current (broken) behavior so a fix can be verified.
        """
        dyno = make_dyno()
        import heroku_manager.heroku as hm
        import inspect
        sig = inspect.signature(hm.HerokuDyno.call_heroku_api)
        default_headers = sig.parameters["custom_headers"].default
        # The default is a dict instance — mutable default argument is present
        self.assertIsInstance(default_headers, dict)
        # Documenting: if caller mutates it, next caller sees mutated state
        # Fix: should be `custom_headers=None` with `headers.update(custom_headers or {})`


if __name__ == "__main__":
    unittest.main()
