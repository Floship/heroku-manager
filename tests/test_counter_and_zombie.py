"""
Tests: increment_dyno_counter, check_for_sibling_zombie_dynos.
"""
import pickle
import os
import time
import unittest
from unittest.mock import patch, MagicMock
from tests.conftest import make_dyno, BaseLockTestCase, patch_scan_backend, real_decoder, seed_index_ready
from django.core.cache import cache
from django.conf import settings as django_settings
from django.utils import timezone
from django_redis.client.default import DefaultClient


class TestIncrementDynoCounter(BaseLockTestCase):

    def test_returns_zero_when_no_dyno_name(self):
        dyno = make_dyno()
        dyno.dyno_name = None
        result = dyno.increment_dyno_counter()
        self.assertEqual(result, 0)

    def test_initializes_counter_to_1_on_first_call(self):
        dyno = make_dyno()
        result = dyno.increment_dyno_counter()
        self.assertEqual(result, 1)

    def test_increments_counter_on_subsequent_calls(self):
        dyno = make_dyno()
        dyno.increment_dyno_counter()
        result = dyno.increment_dyno_counter()
        self.assertEqual(result, 2)

    def test_restarts_dyno_at_threshold(self):
        from django.conf import settings as ds
        dyno = make_dyno()
        threshold = getattr(ds, "DYNO_RESTART_THRESHOLD", 15)
        key = f"heroku:dyno_counter:{dyno.dyno_name}"
        cache.set(key, threshold - 1, timeout=3600)
        with patch.object(dyno, "restart_dyno", return_value=True) as mock_restart:
            result = dyno.increment_dyno_counter()
        mock_restart.assert_called_once_with(dyno.dyno_name)
        self.assertEqual(result, 0)

    def test_counter_deleted_after_restart(self):
        from django.conf import settings as ds
        dyno = make_dyno()
        threshold = getattr(ds, "DYNO_RESTART_THRESHOLD", 15)
        key = f"heroku:dyno_counter:{dyno.dyno_name}"
        cache.set(key, threshold - 1, timeout=3600)
        with patch.object(dyno, "restart_dyno", return_value=True):
            dyno.increment_dyno_counter()
        self.assertIsNone(cache.get(key))

    def test_counter_uses_named_dyno(self):
        dyno = make_dyno()
        result = dyno.increment_dyno_counter("other_worker.2")
        self.assertEqual(result, 1)
        self.assertEqual(cache.get("heroku:dyno_counter:other_worker.2"), 1)

    def test_restart_returns_zero_int(self):
        """
        increment_dyno_counter must return int 0 at threshold (not the old 'restarted' string).
        """
        from django.conf import settings as ds
        dyno = make_dyno()
        threshold = getattr(ds, "DYNO_RESTART_THRESHOLD", 15)
        key = f"heroku:dyno_counter:{dyno.dyno_name}"
        cache.set(key, threshold - 1, timeout=3600)
        with patch.object(dyno, "restart_dyno", return_value=True):
            result = dyno.increment_dyno_counter()
        self.assertIsInstance(result, int)
        self.assertEqual(result, 0)


class TestCheckForSiblingZombieDynos(BaseLockTestCase):

    def _set_alive(self, dyno_name, seconds_ago):
        t = timezone.now() - timezone.timedelta(seconds=seconds_ago)
        cache.set(f"heroku:dyno_alive:{dyno_name}", t, timeout=3600)

    def _patch_alive_keys(self, names):
        keys = [f"heroku:dyno_alive:{n}" for n in names]
        return patch_scan_backend(keys)

    def test_no_zombies_no_restart(self):
        # Ready indexed path: fresh members, decoded fresh timestamps, no restart.
        dyno = make_dyno()
        self._set_alive("normal_worker.2", 10)  # recent
        backend = MagicMock()
        client = MagicMock()
        client.zscore.return_value = time.time()
        client.zrangebyscore.return_value = ["normal_worker.1", "normal_worker.2"]
        client.mget.return_value = [pickle.dumps(timezone.now()), pickle.dumps(timezone.now())]
        client.scan.return_value = (
            0,
            ["heroku:dyno_alive:normal_worker.1", "heroku:dyno_alive:normal_worker.2"],
        )
        backend.client.get_client.return_value = client
        backend.client.decode = pickle.loads
        backend.make_key.side_effect = lambda key: key
        backend.get.return_value = None
        with patch.dict(os.environ, {"HEROKU_DYNO_INDEX_V1_READY": "true"}):
            with patch("heroku_manager.heroku.cache", backend):
                with patch.object(dyno, "restart_zombie_dyno") as mock_restart:
                    dyno.check_for_sibling_zombie_dynos()
        mock_restart.assert_not_called()

    def test_no_zombies_no_restart_legacy_fixture(self):
        # Not-ready (flag absent): prune once, return immediately, never SCAN-restart.
        dyno = make_dyno()
        self._set_alive("normal_worker.2", 10)  # recent
        backend = MagicMock()
        client = MagicMock()
        client.zremrangebyscore.return_value = 0
        client.scan.side_effect = AssertionError("not-ready must not scan for zombies")
        backend.client.get_client.return_value = client
        backend.make_key.side_effect = lambda key: key
        backend.get.return_value = None
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("HEROKU_DYNO_INDEX_V1_READY", None)
            with patch("heroku_manager.heroku.cache", backend):
                with patch.object(dyno, "restart_zombie_dyno") as mock_restart:
                    dyno.check_for_sibling_zombie_dynos()
        mock_restart.assert_not_called()

    def test_zombie_detected_and_restarted(self):
        # Ready indexed path: stale decoded timestamp triggers one restart.
        from django.conf import settings as ds
        dyno = make_dyno()
        threshold = ds.DYNO_ZOMBIE_THRESHOLD
        self._set_alive("normal_worker.2", threshold + 10)  # stale
        backend = MagicMock()
        client = MagicMock()
        client.zscore.return_value = time.time()
        client.zrangebyscore.return_value = ["normal_worker.1", "normal_worker.2"]
        client.mget.return_value = [pickle.dumps(timezone.now()), pickle.dumps(
            timezone.now() - timezone.timedelta(seconds=threshold + 10)
        )]
        client.scan.return_value = (
            0,
            ["heroku:dyno_alive:normal_worker.1", "heroku:dyno_alive:normal_worker.2"],
        )
        backend.client.get_client.return_value = client
        backend.client.decode = pickle.loads
        backend.make_key.side_effect = lambda key: key
        backend.get.return_value = None
        with patch.dict(os.environ, {"HEROKU_DYNO_INDEX_V1_READY": "true"}):
            with patch("heroku_manager.heroku.cache", backend):
                with patch.object(dyno, "restart_zombie_dyno") as mock_restart:
                    dyno.check_for_sibling_zombie_dynos()
        mock_restart.assert_called_once_with("normal_worker.2")

    def test_zombie_detected_and_restarted_legacy_fixture(self):
        # Same ready indexed path; kept as a named legacy-fixture regression so
        # the conftest-seeded ready state is exercised end to end.
        dyno = make_dyno()
        threshold = django_settings.DYNO_ZOMBIE_THRESHOLD
        self._set_alive("normal_worker.2", threshold + 10)
        backend = MagicMock()
        client = MagicMock()
        client.zscore.return_value = time.time()
        client.zrangebyscore.return_value = ["normal_worker.1", "normal_worker.2"]
        client.mget.return_value = [
            pickle.dumps(timezone.now()),
            pickle.dumps(timezone.now() - timezone.timedelta(seconds=threshold + 10)),
        ]
        client.scan.return_value = (
            0,
            ["heroku:dyno_alive:normal_worker.1", "heroku:dyno_alive:normal_worker.2"],
        )
        backend.client.get_client.return_value = client
        backend.client.decode = pickle.loads
        backend.make_key.side_effect = lambda key: key
        backend.get.return_value = None
        with patch.dict(os.environ, {"HEROKU_DYNO_INDEX_V1_READY": "true"}):
            with patch("heroku_manager.heroku.cache", backend):
                with patch.object(dyno, "restart_zombie_dyno") as mock_restart:
                    dyno.check_for_sibling_zombie_dynos()
        mock_restart.assert_called_once_with("normal_worker.2")

    def test_ready_indexed_zombie_restart(self):
        # Phase B: with readiness, the zombie check reads fresh index members,
        # MGETs serialized alive timestamps, decodes them through
        # django-redis, and restarts a stale sibling.
        from django.conf import settings as ds
        dyno = make_dyno()
        threshold = ds.DYNO_ZOMBIE_THRESHOLD
        stale = timezone.now() - timezone.timedelta(seconds=threshold + 10)
        backend = MagicMock()
        client = MagicMock()
        client.zscore.return_value = time.time()
        client.zrangebyscore.return_value = ["normal_worker.1", "normal_worker.2"]
        client.mget.return_value = [pickle.dumps(timezone.now()), pickle.dumps(stale)]
        client.scan.return_value = (
            0,
            ["heroku:dyno_alive:normal_worker.1", "heroku:dyno_alive:normal_worker.2"],
        )
        backend.client.get_client.return_value = client
        backend.make_key.side_effect = lambda key: key
        backend.get.return_value = None
        with patch.dict(os.environ, {"HEROKU_DYNO_INDEX_V1_READY": "true"}):
            with patch("heroku_manager.heroku.cache", backend):
                with patch.object(DefaultClient, "decode", wraps=DefaultClient.decode) as decode:
                    backend.client.decode = real_decoder()
                    with patch.object(dyno, "restart_zombie_dyno") as mock_restart:
                        dyno.check_for_sibling_zombie_dynos()
        mock_restart.assert_called_once_with("normal_worker.2")
        self.assertGreaterEqual(decode.call_count, 1)

    def test_ready_indexed_no_zombies_no_restart(self):
        dyno = make_dyno()
        backend = MagicMock()
        client = MagicMock()
        client.zscore.return_value = time.time()
        client.zrangebyscore.return_value = ["normal_worker.1", "normal_worker.2"]
        client.mget.return_value = [pickle.dumps(timezone.now()), pickle.dumps(timezone.now())]
        client.scan.return_value = (
            0,
            ["heroku:dyno_alive:normal_worker.1", "heroku:dyno_alive:normal_worker.2"],
        )
        backend.client.get_client.return_value = client
        backend.client.decode = pickle.loads
        backend.make_key.side_effect = lambda key: key
        backend.get.return_value = None
        with patch.dict(os.environ, {"HEROKU_DYNO_INDEX_V1_READY": "true"}):
            with patch("heroku_manager.heroku.cache", backend):
                with patch.object(dyno, "restart_zombie_dyno") as mock_restart:
                    dyno.check_for_sibling_zombie_dynos()
        mock_restart.assert_not_called()

    def test_prune_runs_once_before_readiness_check_then_returns_immediately(self):
        # The zombie check prunes once under the lock; when not ready it must
        # return immediately (no SCAN-based restart evaluation).
        dyno = make_dyno()
        backend = MagicMock()
        client = MagicMock()
        client.zremrangebyscore.return_value = 2
        client.scan.side_effect = AssertionError("not-ready must not scan for zombies")
        backend.client.get_client.return_value = client
        backend.make_key.side_effect = lambda key: key
        backend.get.return_value = None
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("HEROKU_DYNO_INDEX_V1_READY", None)
            with patch("heroku_manager.heroku.cache", backend):
                with patch.object(dyno, "restart_zombie_dyno") as mock_restart:
                    dyno.check_for_sibling_zombie_dynos()
        client.zremrangebyscore.assert_called_once()
        mock_restart.assert_not_called()

    def test_restart_zombie_dyno_delegates_to_restart_dyno(self):
        dyno = make_dyno()
        with patch.object(dyno, "restart_dyno") as mock_restart:
            dyno.restart_zombie_dyno("normal_worker.3")
        mock_restart.assert_called_once_with("normal_worker.3")


if __name__ == "__main__":
    unittest.main()
