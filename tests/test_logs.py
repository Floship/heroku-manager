"""
Tests: log parsing, extract_latest_metric, metric helpers (memory, load, R14/R15).
"""
import os
import time
import unittest
from unittest.mock import patch, MagicMock
from tests.conftest import make_dyno, BaseLockTestCase
from django.core.cache import cache

from heroku_manager.proxy import ProxyResponse


from datetime import datetime, timezone as dt_tz


def _recent_ts():
    """Return a timestamp string from 1 minute ago (well within 24h window)."""
    from django.utils import timezone
    t = timezone.now() - timezone.timedelta(seconds=5)
    return t.strftime("%Y-%m-%dT%H:%M:%S.000000+00:00")


def _make_log_sample():
    ts = _recent_ts()
    return (
        f"{ts} heroku[normal_worker.1]: source=normal_worker.1 dyno=heroku.123.abc "
        f"sample#load_avg_1m=0.11 sample#load_avg_5m=0.18 sample#load_avg_15m=0.13\n"
        f"{ts} heroku[normal_worker.1]: source=normal_worker.1 dyno=heroku.123.abc "
        f"sample#memory_total=298.00MB sample#memory_quota=512.00MB\n"
        f"{ts} heroku[normal_worker.1]: Error R14 (Memory quota exceeded)\n"
        f"{ts} heroku[normal_worker.1]: Error R15 (Memory quota vastly exceeded)\n"
    )


LOG_SAMPLE = _make_log_sample()
SELF_DYNO_ID = "11111111-1111-4111-8111-111111111111"
OLD_DYNO_ID = "22222222-2222-4222-8222-222222222222"
BAD_TIMESTAMP_LOG = "2024-99-99T99:99:99.845449+00:00 heroku[normal_worker.1]: sample#memory_total=100.00MB\n"


def _identified_log_sample(dyno_id=SELF_DYNO_ID, memory_mb=298.0):
    ts = _recent_ts()
    return (
        f"{ts} heroku[normal_worker.1]: source=normal_worker.1 "
        f"dyno=heroku.123.{dyno_id} sample#memory_total={memory_mb:.2f}MB "
        f"sample#load_avg_1m=0.11\n"
    )


def _make_log_response(text):
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"logplex_url": "https://logplex.example.com/x"}
    resp.text = text
    return resp


def _make_logplex_response(text):
    resp = MagicMock()
    resp.status_code = 200
    resp.text = text
    return resp


class TestGetHerokuLogs(BaseLockTestCase):

    def test_returns_none_when_no_transport_is_configured(self):
        dyno = make_dyno()
        from heroku_manager.proxy import HerokuProxy
        dyno.proxy = HerokuProxy("", "", "")
        result = dyno.get_heroku_logs()
        self.assertIsNone(result)

    def test_uses_cache_on_second_call(self):
        dyno = make_dyno()
        with patch.object(dyno, "call_heroku_api", return_value=_make_log_response(LOG_SAMPLE)) as mock_api:
            with patch("heroku_manager.heroku.requests.get",
                       return_value=_make_logplex_response(LOG_SAMPLE)):
                r1 = dyno.get_heroku_logs()
                r2 = dyno.get_heroku_logs()
        # API called once; second call uses cache
        mock_api.assert_called_once()
        self.assertEqual(r1, r2)

    def test_rate_limit_pauses_new_log_sessions_across_dynos_for_retry_after(self):
        first = make_dyno()
        sibling = make_dyno("normal_worker.2")
        cached = make_dyno("normal_worker.3")
        independent = make_dyno()
        independent.app_name = "other-app"
        throttled = ProxyResponse(429, '{"id":"rate_limit"}', {"Retry-After": "45"})

        with patch.object(cached, "call_heroku_api", return_value=_make_log_response(LOG_SAMPLE)), \
                patch("heroku_manager.heroku.requests.get", return_value=_make_logplex_response(LOG_SAMPLE)):
            self.assertTrue(cached.get_heroku_logs())

        with patch.object(first, "call_heroku_api", return_value=throttled) as rejected, \
                patch.object(sibling, "call_heroku_api", return_value=_make_log_response(LOG_SAMPLE)) as next_call, \
                patch.object(cached, "call_heroku_api") as cache_miss, \
                patch.object(independent, "call_heroku_api", return_value=_make_log_response(LOG_SAMPLE)) as other, \
                patch("heroku_manager.heroku.requests.get", return_value=_make_logplex_response(LOG_SAMPLE)):
            self.assertIsNone(first.get_heroku_logs())
            self.assertIsNone(sibling.get_heroku_logs())
            rejected.assert_called_once()
            next_call.assert_not_called()
            self.assertTrue(cached.get_heroku_logs())
            cache_miss.assert_not_called()
            self.assertTrue(independent.get_heroku_logs())
            other.assert_called_once()
            deadline = cache.get("heroku:log_session_backoff:floship")
            self.assertIsNotNone(deadline)
            self.assertGreater(deadline, time.time() + 43)
            with patch("heroku_manager.heroku.time.time", return_value=deadline + 1):
                self.assertTrue(sibling.get_heroku_logs())
            next_call.assert_called_once()

    def test_relaxed_reader_reuses_the_same_incarnations_strict_logs(self):
        dyno = make_dyno()
        dyno.dyno_id = SELF_DYNO_ID
        sample = _identified_log_sample()
        with patch.dict(os.environ, {"DYNO": dyno.dyno_name}), \
                patch.object(dyno, "call_heroku_api", return_value=_make_log_response(sample)) as api, \
                patch("heroku_manager.heroku.requests.get", return_value=_make_logplex_response(sample)):
            strict = dyno.get_heroku_logs(strict_observation=True)
            relaxed = dyno.get_heroku_logs()

        api.assert_called_once()
        self.assertEqual(strict, relaxed)
        self.assertEqual(relaxed[0]["source_dyno_id"], SELF_DYNO_ID)

    def test_strict_reader_refetches_after_a_relaxed_read(self):
        dyno = make_dyno()
        dyno.dyno_id = SELF_DYNO_ID
        sample = _identified_log_sample()
        with patch.dict(os.environ, {"DYNO": dyno.dyno_name}), \
                patch.object(dyno, "call_heroku_api", return_value=_make_log_response(sample)) as api, \
                patch("heroku_manager.heroku.requests.get", return_value=_make_logplex_response(sample)):
            self.assertTrue(dyno.get_heroku_logs())
            self.assertTrue(dyno.get_heroku_logs(strict_observation=True))

        self.assertEqual(api.call_count, 2)

    def test_relaxed_reader_refetches_for_another_source_or_incarnation(self):
        dyno = make_dyno()
        dyno.dyno_id = SELF_DYNO_ID
        sample = _identified_log_sample()
        with patch.dict(os.environ, {"DYNO": dyno.dyno_name}), \
                patch.object(dyno, "call_heroku_api", return_value=_make_log_response(sample)) as api, \
                patch("heroku_manager.heroku.requests.get", return_value=_make_logplex_response(sample)):
            self.assertTrue(dyno.get_heroku_logs(strict_observation=True))
            self.assertTrue(dyno.get_heroku_logs(source="app"))
            dyno.dyno_id = OLD_DYNO_ID
            self.assertTrue(dyno.get_heroku_logs())

        self.assertEqual(api.call_count, 3)

    def test_parses_log_entries_correctly(self):
        dyno = make_dyno()
        with patch.object(dyno, "call_heroku_api", return_value=_make_log_response(LOG_SAMPLE)):
            with patch("heroku_manager.heroku.requests.get",
                       return_value=_make_logplex_response(LOG_SAMPLE)):
                logs = dyno.get_heroku_logs()
        self.assertIsInstance(logs, list)
        self.assertGreater(len(logs), 0)
        first = logs[0]
        self.assertIn("timestamp", first)
        self.assertIn("message", first)
        self.assertIn("dyno_name", first)

    def test_parses_the_source_dyno_identity(self):
        dyno = make_dyno()
        sample = _identified_log_sample()
        with patch.object(dyno, "call_heroku_api", return_value=_make_log_response(sample)):
            with patch("heroku_manager.heroku.requests.get",
                       return_value=_make_logplex_response(sample)):
                logs = dyno.get_heroku_logs()

        self.assertEqual(logs[0]["source_dyno_name"], "normal_worker.1")
        self.assertEqual(logs[0]["source_dyno_id"], SELF_DYNO_ID)

    def test_raw_log_cache_is_scoped_to_the_current_incarnation(self):
        dyno = make_dyno()
        dyno.dyno_id = SELF_DYNO_ID
        old_cache_key = f"heroku:logs:{dyno.app_name}:{dyno.dyno_name}:{OLD_DYNO_ID}"
        cache.set(old_cache_key, "old incarnation logs", timeout=90)
        sample = _identified_log_sample(memory_mb=392.0)

        with patch.dict(os.environ, {"DYNO": dyno.dyno_name}), \
                patch.object(dyno, "call_heroku_api", return_value=_make_log_response(sample)) as api, \
                patch("heroku_manager.heroku.requests.get",
                      return_value=_make_logplex_response(sample)):
            logs = dyno.get_heroku_logs()

        api.assert_called_once()
        self.assertIn("392.00MB", logs[0]["message"])
        self.assertEqual(logs[0]["source_dyno_id"], SELF_DYNO_ID)

    def test_returns_none_when_log_session_api_fails(self):
        dyno = make_dyno()
        with patch.object(dyno, "call_heroku_api", return_value=None):
            result = dyno.get_heroku_logs()
        self.assertIsNone(result)

    def test_returns_none_when_log_session_4xx(self):
        dyno = make_dyno()
        resp = MagicMock()
        resp.status_code = 403
        resp.text = "forbidden"
        with patch.object(dyno, "call_heroku_api", return_value=resp):
            result = dyno.get_heroku_logs()
        self.assertIsNone(result)

    def test_returns_none_when_logplex_url_missing(self):
        dyno = make_dyno()
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {}  # no logplex_url
        with patch.object(dyno, "call_heroku_api", return_value=resp):
            result = dyno.get_heroku_logs()
        self.assertIsNone(result)

    def test_returns_none_when_logplex_fetch_fails(self):
        dyno = make_dyno()
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"logplex_url": "https://logplex.example.com/x"}
        logplex_resp = MagicMock()
        logplex_resp.status_code = 503
        logplex_resp.text = ""
        with patch.object(dyno, "call_heroku_api", return_value=resp):
            with patch("heroku_manager.heroku.requests.get", return_value=logplex_resp):
                result = dyno.get_heroku_logs()
        self.assertIsNone(result)

    def test_skips_entries_with_bad_timestamp(self):
        dyno = make_dyno()
        with patch.object(dyno, "call_heroku_api",
                          return_value=_make_log_response(BAD_TIMESTAMP_LOG)):
            with patch("heroku_manager.heroku.requests.get",
                       return_value=_make_logplex_response(BAD_TIMESTAMP_LOG)):
                logs = dyno.get_heroku_logs()
        # Bad timestamp skipped; empty list
        self.assertEqual(logs, [])

    def test_handles_expired_lock_gracefully(self):
        """Lock expires before release -> NotAcquired from __exit__.
        Should log warning and return parsed logs, not crash."""
        from heroku_manager.heroku import _NotAcquired

        dyno = make_dyno()

        # Mock lock whose __exit__ raises NotAcquired (simulating expired lock)
        lock_cm = MagicMock()
        lock_cm.__enter__ = MagicMock(return_value=None)
        lock_cm.__exit__ = MagicMock(side_effect=_NotAcquired("Lock expired"))

        with patch.object(cache, "lock", return_value=lock_cm):
            with patch.object(dyno, "call_heroku_api",
                              return_value=_make_log_response(LOG_SAMPLE)):
                with patch("heroku_manager.heroku.requests.get",
                           return_value=_make_logplex_response(LOG_SAMPLE)):
                    logs = dyno.get_heroku_logs()

        # Should return parsed logs despite lock expiry
        self.assertIsInstance(logs, list)
        self.assertGreater(len(logs), 0)

    def test_returns_none_when_api_fails_and_lock_expires(self):
        """If call_heroku_api returns None inside the lock body and __exit__
        raises NotAcquired, the abandoned 'return None' must not crash on
        logs.split() — should return None gracefully."""
        from heroku_manager.heroku import _NotAcquired

        dyno = make_dyno()

        lock_cm = MagicMock()
        lock_cm.__enter__ = MagicMock(return_value=None)
        lock_cm.__exit__ = MagicMock(side_effect=_NotAcquired("Lock expired"))

        with patch.object(cache, "lock", return_value=lock_cm):
            with patch.object(dyno, "call_heroku_api", return_value=None):
                result = dyno.get_heroku_logs()

        # Should return None, not crash with AttributeError
        self.assertIsNone(result)


class TestExtractLatestMetric(BaseLockTestCase):

    def _dyno_with_logs(self, log_text=LOG_SAMPLE):
        dyno = make_dyno()
        with patch.object(dyno, "call_heroku_api",
                          return_value=_make_log_response(log_text)):
            with patch("heroku_manager.heroku.requests.get",
                       return_value=_make_logplex_response(log_text)):
                _ = dyno.get_heroku_logs()  # warm the log cache
        return dyno

    def test_get_memory_usage_from_logs_returns_float(self):
        dyno = self._dyno_with_logs()
        result = dyno.get_memory_usage_from_logs()
        self.assertIsInstance(result, float)
        self.assertAlmostEqual(result, 298.0)

    def test_get_load_1min_avg_returns_float(self):
        dyno = self._dyno_with_logs()
        result = dyno.get_load_1min_avg()
        self.assertIsInstance(result, float)
        self.assertAlmostEqual(result, 0.11)

    def test_get_load_1min_avg_defaults_when_not_found(self):
        dyno = make_dyno()
        with patch.object(dyno, "get_heroku_logs", return_value=None):
            result = dyno.get_load_1min_avg()
        self.assertEqual(result, 0.4)

    def test_get_r14_from_logs_returns_true(self):
        dyno = self._dyno_with_logs()
        result = dyno.get_r14_from_logs()
        self.assertTrue(result)

    def test_get_r15_from_logs_returns_true(self):
        dyno = self._dyno_with_logs()
        result = dyno.get_r15_from_logs()
        self.assertTrue(result)

    def test_returns_cached_value_when_logs_unavailable(self):
        dyno = make_dyno()
        cache_key = f"heroku:memory_total:{dyno.app_name}:{dyno.dyno_name}"
        cache.set(cache_key, 123.0, timeout=3600)
        with patch.object(dyno, "get_heroku_logs", return_value=None):
            result = dyno.get_memory_usage_from_logs()
        self.assertEqual(result, 123.0)

    def test_returns_none_when_no_logs_and_no_cache(self):
        dyno = make_dyno()
        with patch.object(dyno, "get_heroku_logs", return_value=None):
            result = dyno.get_memory_usage_from_logs()
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
