"""
Tests: _acquire_rate_limit_token and call_heroku_api transport selection.

HEROKU_PROXY_URL selects the signed proxy transport. Without it the original
direct Platform API transport must behave exactly as before.
"""
import json
import os
import unittest
from unittest.mock import patch

import requests
from django.core.cache import cache

from heroku_manager.heroku import HerokuDyno
from heroku_manager.proxy import HerokuProxy, ProxyResponse
from tests.conftest import BaseLockTestCase, make_dyno

LEGACY_KEY = "legacy-key"
FORMATION_URL = "https://api.heroku.com/apps/x/formation"


def make_legacy_dyno(dyno_name="normal_worker.1"):
    dyno = make_dyno(dyno_name)
    dyno.proxy = HerokuProxy("", "", "")
    dyno.heroku_api_key = LEGACY_KEY
    return dyno


def mock_response(status=200, json_data=None, headers=None, text="ok"):
    response = requests.Response()
    response.status_code = status
    body = json.dumps(json_data) if json_data else text
    response._content = body.encode() if isinstance(body, str) else body
    response.headers = requests.structures.CaseInsensitiveDict(headers or {})
    return response


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

    def test_exhausted_budget_returns_false_without_sleeping(self):
        """P0-3: rate limiter must skip (non-blocking) instead of sleeping."""
        dyno = make_dyno()
        limit = 50
        cache.set(f"heroku:api_rate:{dyno.app_name}", limit, timeout=60)
        with patch("heroku_manager.heroku.time.sleep") as mock_sleep:
            result = dyno._acquire_rate_limit_token()
        self.assertFalse(result)
        mock_sleep.assert_not_called()

    def test_key_expiry_between_get_and_incr_handled(self):
        """ValueError from cache.incr when key expired mid-call must not raise."""
        dyno = make_dyno()
        cache.set(f"heroku:api_rate:{dyno.app_name}", 1, timeout=60)
        with patch.object(cache, "incr", side_effect=ValueError("expired")):
            result = dyno._acquire_rate_limit_token()
        self.assertTrue(result)


class TestCallHerokuApiDirect(BaseLockTestCase):
    """The original transport, exercised through the real direct branch."""

    def test_successful_patch_returns_response(self):
        dyno = make_legacy_dyno()
        with patch("heroku_manager.heroku.requests.request", return_value=mock_response()) as request, \
                patch("heroku_manager.proxy.requests.Session.send") as proxy_send:
            resp = dyno.call_heroku_api("PATCH", FORMATION_URL, data={"size": "standard-2x"})
        self.assertEqual(resp.status_code, 200)
        request.assert_called_once()
        self.assertEqual(request.call_args.kwargs["headers"]["Authorization"], f"Bearer {LEGACY_KEY}")
        self.assertEqual(request.call_args.kwargs["json"], {"size": "standard-2x"})
        proxy_send.assert_not_called()

    def test_get_response_is_cached(self):
        dyno = make_legacy_dyno()
        with patch("heroku_manager.heroku.requests.request", return_value=mock_response()) as request:
            first = dyno.call_heroku_api("GET", FORMATION_URL)
            second = dyno.call_heroku_api("GET", FORMATION_URL)
        self.assertEqual(request.call_count, 1)
        self.assertEqual(first.status_code, second.status_code)

    def test_429_backoff_then_success(self):
        dyno = make_legacy_dyno()
        with patch("heroku_manager.heroku.requests.request",
                   side_effect=[mock_response(429, headers={"Retry-After": "1"}), mock_response(200)]) as request, \
                patch("heroku_manager.heroku.time.sleep"):
            resp = dyno.call_heroku_api("PATCH", "https://api.heroku.com/x")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(request.call_count, 2)

    def test_ssl_error_retries_and_returns_none_after_max(self):
        from requests.exceptions import SSLError

        dyno = make_legacy_dyno()
        with patch("heroku_manager.heroku.requests.request", side_effect=SSLError("ssl fail")) as request, \
                patch("heroku_manager.heroku.time.sleep"):
            resp = dyno.call_heroku_api("PATCH", "https://api.heroku.com/x")
        self.assertIsNone(resp)
        self.assertEqual(request.call_count, 5)

    def test_timeout_error_retries(self):
        from requests.exceptions import Timeout

        dyno = make_legacy_dyno()
        with patch("heroku_manager.heroku.requests.request",
                   side_effect=[Timeout(), Timeout(), mock_response(200)]), \
                patch("heroku_manager.heroku.time.sleep"):
            resp = dyno.call_heroku_api("PATCH", "https://api.heroku.com/x")
        self.assertEqual(resp.status_code, 200)

    def test_rate_limit_token_exhausted_returns_none(self):
        dyno = make_legacy_dyno()
        with patch.object(dyno, "_acquire_rate_limit_token", return_value=False), \
                patch("heroku_manager.heroku.requests.request") as request:
            resp = dyno.call_heroku_api("PATCH", "https://api.heroku.com/x")
        self.assertIsNone(resp)
        request.assert_not_called()

    def test_4xx_response_is_returned_and_logged(self):
        dyno = make_legacy_dyno()
        with patch("heroku_manager.heroku.requests.request", return_value=mock_response(404, text="not found")), \
                patch("heroku_manager.heroku.logger") as logger:
            resp = dyno.call_heroku_api("DELETE", "https://api.heroku.com/x")
        self.assertEqual(resp.status_code, 404)
        logger.error.assert_called()

    def test_exec_connect_uses_direct_transport_with_only_a_key(self):
        dyno = make_legacy_dyno()
        with patch("heroku_manager.heroku.requests.request", return_value=mock_response(201, {"name": "run.1234"})) as request:
            result = dyno.exec_connect(command="echo hi")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(request.call_args.args[0], "POST")
        self.assertEqual(request.call_args.kwargs["headers"]["Authorization"], f"Bearer {LEGACY_KEY}")

    def test_custom_headers_default_is_none_not_mutable_dict(self):
        """P1-11: the default must stay None and resolve to {} per call."""
        import inspect

        import heroku_manager.heroku as hm

        signature = inspect.signature(hm.HerokuDyno.call_heroku_api)
        self.assertIsNone(signature.parameters["custom_headers"].default)


class TestCallHerokuApiProxy(BaseLockTestCase):
    """The signed proxy transport selected by HEROKU_PROXY_URL."""

    def test_successful_request_uses_signed_proxy(self):
        dyno = make_dyno()
        with patch("heroku_manager.proxy.requests.Session.send", return_value=mock_response()) as request:
            resp = dyno.call_heroku_api("PATCH", FORMATION_URL, data={"size": "standard-2x"})
        self.assertEqual(resp.status_code, 200)
        request.assert_called_once()

    def test_responses_are_not_cached_as_transport_objects(self):
        dyno = make_dyno()
        with patch("heroku_manager.proxy.requests.Session.send", return_value=mock_response()) as request:
            first = dyno.call_heroku_api("GET", FORMATION_URL)
            second = dyno.call_heroku_api("GET", FORMATION_URL)
        self.assertEqual(request.call_count, 2)
        self.assertEqual(first.status_code, second.status_code)

    def test_429_is_returned_without_automatic_mutation_retry(self):
        dyno = make_dyno()
        with patch("heroku_manager.proxy.requests.Session.send",
                   side_effect=[mock_response(429, headers={"Retry-After": "1"}), mock_response(200)]) as send:
            resp = dyno.call_heroku_api("PATCH", "https://api.heroku.com/x")
        self.assertEqual(resp.status_code, 429)
        send.assert_called_once()

    def test_ssl_error_returns_unknown_outcome_without_retry(self):
        from requests.exceptions import SSLError

        dyno = make_dyno()
        with patch("heroku_manager.proxy.requests.Session.send", side_effect=SSLError("ssl fail")) as send:
            resp = dyno.call_heroku_api("PATCH", "https://api.heroku.com/x")
        self.assertEqual(resp.status_code, 502)
        send.assert_called_once()

    def test_timeout_never_retries_a_mutation(self):
        from requests.exceptions import Timeout

        dyno = make_dyno()
        with patch("heroku_manager.proxy.requests.Session.send",
                   side_effect=[Timeout(), Timeout(), mock_response(200)]) as send:
            resp = dyno.call_heroku_api("PATCH", "https://api.heroku.com/x")
        self.assertEqual(resp.status_code, 502)
        send.assert_called_once()

    def test_uncertain_mutation_survives_the_next_tick_and_a_new_autoscaler(self):
        from requests.exceptions import Timeout

        first = make_dyno()
        method, url, data = "PATCH", "https://api.heroku.com/apps/floship/formation/normal_worker", {"size": "standard-2x"}
        with patch("heroku_manager.proxy.requests.Session.send", side_effect=Timeout()) as send:
            unknown = first.call_heroku_api(method, url, data=data)
            second = make_dyno()
            blocked = second.call_heroku_api(method, url, data=data)
        self.assertEqual(unknown.status_code, 502)
        self.assertEqual(blocked.status_code, 409)
        self.assertEqual(unknown.request_id, blocked.request_id)
        send.assert_called_once()
        self.assertFalse(second.reconcile_proxy_request(method, url, data, "wrong-request"))
        self.assertTrue(second.reconcile_proxy_request(method, url, data, unknown.request_id))
        with patch.object(second.proxy, "request", return_value=ProxyResponse(200, "{}")) as send:
            self.assertEqual(second.call_heroku_api(method, url, data=data).status_code, 200)
        send.assert_called_once()

    def test_rate_limit_token_exhausted_returns_none(self):
        dyno = make_dyno()
        with patch.object(dyno, "_acquire_rate_limit_token", return_value=False), \
                patch("heroku_manager.proxy.requests.Session.send") as send:
            resp = dyno.call_heroku_api("PATCH", "https://api.heroku.com/x")
        self.assertIsNone(resp)
        send.assert_not_called()

    def test_log_session_timeout_does_not_block_the_next_read(self):
        dyno = make_dyno()
        url = "https://api.heroku.com/apps/floship/log-sessions"
        with patch("heroku_manager.proxy.requests.Session.send", side_effect=[
            requests.Timeout(), mock_response(201, {"logplex_url": "https://logs.example.invalid"})
        ]) as send:
            first = dyno.call_heroku_api("POST", url, data={"lines": 100})
            second = dyno.call_heroku_api("POST", url, data={"lines": 100})
        self.assertEqual(first.status_code, 502)
        self.assertEqual(second.status_code, 201)
        self.assertEqual(send.call_count, 2)
        ids = [call.args[0].headers["idempotency-key"] for call in send.call_args_list]
        self.assertNotEqual(ids[0], ids[1])

    def test_log_session_provider_failure_does_not_block_the_next_read(self):
        dyno = make_dyno()
        url = "https://api.heroku.com/apps/floship/log-sessions"
        with patch("heroku_manager.proxy.requests.Session.send", side_effect=[
            mock_response(503), mock_response(201, {"logplex_url": "https://logs.example.invalid"})
        ]) as send:
            self.assertEqual(dyno.call_heroku_api("POST", url, data={"lines": 100}).status_code, 503)
            self.assertEqual(dyno.call_heroku_api("POST", url, data={"lines": 100}).status_code, 201)
        self.assertEqual(send.call_count, 2)

    def test_invalid_local_mutation_request_releases_its_reservation(self):
        dyno = make_dyno()
        with patch("heroku_manager.proxy.requests.Session.send", return_value=mock_response()) as send:
            invalid = dyno.call_heroku_api("PATCH", FORMATION_URL, custom_headers={"Authorization": "forbidden"})
            corrected = dyno.call_heroku_api("PATCH", FORMATION_URL)
        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(corrected.status_code, 200)
        send.assert_called_once()

    def test_definite_rejections_allow_a_corrected_mutation(self):
        for status in (400, 401, 403, 404, 405, 413, 422, 429):
            with self.subTest(status=status):
                dyno = make_dyno()
                with patch("heroku_manager.proxy.requests.Session.send", side_effect=[
                    mock_response(status), mock_response(200)
                ]) as send:
                    self.assertEqual(dyno.call_heroku_api("PATCH", FORMATION_URL).status_code, status)
                    self.assertEqual(dyno.call_heroku_api("PATCH", FORMATION_URL).status_code, 200)
                self.assertEqual(send.call_count, 2)

    def test_4xx_response_is_returned_and_logged(self):
        dyno = make_dyno()
        with patch("heroku_manager.proxy.requests.Session.send", return_value=mock_response(404, text="not found")), \
                patch("heroku_manager.heroku.logger") as logger:
            resp = dyno.call_heroku_api("DELETE", "https://api.heroku.com/x")
        self.assertEqual(resp.status_code, 404)
        logger.warning.assert_called()


class TestTransportSelection(BaseLockTestCase):
    """HEROKU_PROXY_URL alone switches transport, and misconfiguration fails closed."""

    def assert_fails_closed(self, dyno):
        self.assertFalse(dyno.heroku_api_configured)
        dyno.app_name = dyno.app_name or "synthetic"
        with patch("heroku_manager.heroku.requests.request") as direct, \
                patch("heroku_manager.proxy.requests.Session.send") as proxy_send, \
                patch("heroku_manager.heroku.logger") as logger:
            result = dyno.call_heroku_api("PATCH", FORMATION_URL, data={"size": "standard-2x"})
            self.assertIsNone(dyno.get_heroku_logs())
            self.assertIsNone(dyno.exec_connect(command="echo synthetic", dyno_name="worker.1"))
        self.assertIsNone(result)
        direct.assert_not_called()
        proxy_send.assert_not_called()
        logger.error.assert_called()

    def test_uncertain_mutation_can_be_reconciled_using_its_logged_handle(self):
        dyno = make_dyno()
        method, url, data = "PATCH", FORMATION_URL, {"size": "standard-2x"}
        with patch("heroku_manager.proxy.requests.Session.send", side_effect=requests.Timeout()), \
                patch("heroku_manager.heroku.logger") as logger:
            response = dyno.call_heroku_api(method, url, data=data)
        pending = dyno._proxy_pending_key(method, url, data)
        logger.warning.assert_any_call(
            "Heroku proxy outcome requires reconciliation, request_id=%s pending_key=%s",
            response.request_id, pending)
        self.assertFalse(dyno.reconcile_proxy_request(request_id="wrong", pending_key=pending))
        self.assertFalse(dyno.reconcile_proxy_request(request_id=response.request_id, pending_key="unrelated:key"))
        self.assertTrue(dyno.reconcile_proxy_request(request_id=response.request_id, pending_key=pending))
        self.assertIsNone(cache.get(pending))

    def test_configured_proxy_ignores_a_legacy_key_on_the_instance(self):
        dyno = make_dyno()
        dyno.heroku_api_key = LEGACY_KEY
        self.assertTrue(dyno.heroku_api_configured)
        with patch("heroku_manager.heroku.requests.request") as direct, \
                patch("heroku_manager.proxy.requests.Session.send", return_value=mock_response()) as proxy_send:
            dyno.call_heroku_api("GET", FORMATION_URL)
        direct.assert_not_called()
        proxy_send.assert_called_once()

    def test_proxy_url_without_signing_settings_fails_closed_even_with_legacy_key(self):
        with patch.dict(os.environ, {
            "HEROKU_PROXY_URL": "https://manager.example/v1/heroku",
            "HEROKU_API_KEY": LEGACY_KEY,
        }, clear=True):
            dyno = HerokuDyno()
        self.assertTrue(dyno.proxy.enabled)
        self.assertIsNone(dyno.heroku_api_key)
        self.assert_fails_closed(dyno)

    def test_selected_proxy_never_reads_the_legacy_token(self):
        for url in ("https://manager.example/v1/heroku", "///"):
            with self.subTest(url=url), patch.dict(os.environ, {
                "HEROKU_PROXY_URL": url, "HEROKU_API_KEY": LEGACY_KEY,
            }, clear=True):
                original_get = os.environ.get

                def lookup(name, default=None):
                    if name == "HEROKU_API_KEY":
                        self.fail("The selected proxy read the legacy credential")
                    return original_get(name, default)

                with patch.object(os.environ, "get", side_effect=lookup):
                    dyno = HerokuDyno()
                self.assertTrue(dyno.proxy.enabled)
                self.assertIsNone(dyno.heroku_api_key)

    def test_legacy_key_without_a_proxy_url_selects_the_direct_transport(self):
        with patch.dict(os.environ, {"HEROKU_API_KEY": LEGACY_KEY}, clear=True):
            dyno = HerokuDyno()
        self.assertFalse(dyno.proxy.enabled)
        self.assertTrue(dyno.heroku_api_configured)
        with patch("heroku_manager.heroku.requests.request", return_value=mock_response()) as direct:
            self.assertEqual(dyno.call_heroku_api("GET", FORMATION_URL).status_code, 200)
        direct.assert_called_once()

    def test_malformed_secret_fails_closed_even_with_legacy_key(self):
        for secret in ("not-hex", "01" * 8):
            with self.subTest(secret=secret):
                dyno = make_dyno()
                dyno.proxy = HerokuProxy("https://manager.example", "portal", secret)
                dyno.heroku_api_key = LEGACY_KEY
                self.assertTrue(dyno.proxy.enabled)
                self.assertIn("HEROKU_PROXY_SECRET", dyno.proxy.config_error)
                self.assert_fails_closed(dyno)

    def test_missing_client_id_fails_closed_even_with_legacy_key(self):
        dyno = make_dyno()
        dyno.proxy = HerokuProxy("https://manager.example", "", "01" * 32)
        dyno.heroku_api_key = LEGACY_KEY
        self.assert_fails_closed(dyno)

    def test_invalid_proxy_url_fails_closed_even_with_legacy_key(self):
        dyno = make_dyno()
        dyno.proxy = HerokuProxy("http://manager.example", "portal", "01" * 32)
        dyno.heroku_api_key = LEGACY_KEY
        self.assertTrue(dyno.proxy.enabled)
        self.assert_fails_closed(dyno)

    def test_slash_only_urls_and_invalid_ports_cannot_reactivate_the_direct_key(self):
        for url in ("/", "//", "///", "https://manager.example:0",
                    "https://manager.example:99999", "https://manager.example:invalid"):
            with self.subTest(url=url):
                dyno = make_dyno()
                dyno.proxy = HerokuProxy(url, "portal", "01" * 32)
                dyno.heroku_api_key = LEGACY_KEY
                self.assertTrue(dyno.proxy.enabled)
                self.assertIsNotNone(dyno.proxy.config_error)
                self.assert_fails_closed(dyno)

    def test_no_proxy_url_and_no_key_is_not_configured(self):
        dyno = make_dyno()
        dyno.proxy = HerokuProxy("", "", "")
        dyno.heroku_api_key = None
        self.assertFalse(dyno.heroku_api_configured)
        self.assertIsNone(dyno.get_heroku_logs())

    def test_direct_and_proxy_results_share_the_public_contract(self):
        direct = mock_response(200, {"a": 1}, {"Content-Type": "application/json"})
        proxied = ProxyResponse(200, '{"a": 1}', {"Content-Type": "application/json"})
        for result in (direct, proxied):
            with self.subTest(result=type(result).__name__):
                self.assertEqual(result.status_code, 200)
                self.assertEqual(result.text, '{"a": 1}')
                self.assertEqual(result.json(), {"a": 1})
                self.assertEqual(result.headers["Content-Type"], "application/json")
                self.assertTrue(result)


if __name__ == "__main__":
    unittest.main()
