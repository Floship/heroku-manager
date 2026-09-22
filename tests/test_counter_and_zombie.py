"""
Tests: increment_dyno_counter, check_for_sibling_zombie_dynos.
"""
import pickle
import os
import time
import unittest
from unittest.mock import patch, MagicMock
from tests.conftest import make_dyno, BaseLockTestCase
from django.core.cache import cache
from django.conf import settings as django_settings
from django.utils import timezone


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

    def _ready_backend(self, stale, fresh, alive_values):
        """django-redis-shaped backend with distinct stale/fresh member sets."""
        backend = MagicMock()
        client = MagicMock()
        client.get.return_value = None  # degraded marker absent unless the test sets it
        client.zscore.return_value = time.time()
        client.zrangebyscore.side_effect = lambda key, mn, mx: stale if mn == "-inf" else fresh
        client.mget.return_value = alive_values
        backend.client.get_client.return_value = client
        backend.client.decode = pickle.loads
        backend.make_key.side_effect = lambda key: key
        backend.get.return_value = None
        return backend, client

    def test_no_zombies_no_restart(self):
        # Ready indexed path: fresh members, fresh alive timestamps, no restart.
        dyno = make_dyno()
        now = pickle.dumps(timezone.now())
        backend, _ = self._ready_backend(
            stale=[],
            fresh=["normal_worker.1", "normal_worker.2"],
            alive_values=[now, now],
        )
        with patch("heroku_manager.heroku.cache", backend):
            with patch.object(dyno, "restart_zombie_dyno") as mock_restart:
                dyno.check_for_sibling_zombie_dynos()
        mock_restart.assert_not_called()

    def test_zombie_detected_and_restarted(self):
        # Ready indexed path: fresh member whose alive timestamp is stale.
        from django.conf import settings as ds
        threshold = ds.DYNO_ZOMBIE_THRESHOLD
        dyno = make_dyno()
        old = pickle.dumps(timezone.now() - timezone.timedelta(seconds=threshold + 10))
        backend, _ = self._ready_backend(
            stale=[],
            fresh=["normal_worker.1", "normal_worker.2"],
            alive_values=[pickle.dumps(timezone.now()), old],
        )
        with patch("heroku_manager.heroku.cache", backend):
            with patch.object(dyno, "restart_zombie_dyno") as mock_restart:
                dyno.check_for_sibling_zombie_dynos()
        mock_restart.assert_called_once_with("normal_worker.2")

    def test_stale_index_member_evaluated_and_restarted_before_prune(self):
        # Stale member restarted from its alive timestamp BEFORE the prune
        # removes it; the fresh set is empty, so only stale evaluation can
        # produce this restart.
        dyno = make_dyno()
        threshold = django_settings.DYNO_ZOMBIE_THRESHOLD
        backend, client = self._ready_backend(
            stale=["normal_worker.2"], fresh=[],
            alive_values=[pickle.dumps(timezone.now() - timezone.timedelta(seconds=threshold + 10))],
        )
        with patch("heroku_manager.heroku.cache", backend):
            with patch.object(dyno, "restart_zombie_dyno") as mock_restart:
                dyno.check_for_sibling_zombie_dynos()
        mock_restart.assert_called_once_with("normal_worker.2")
        client.zremrangebyscore.assert_called_once()

    def test_stale_member_missing_alive_not_restarted_but_pruned(self):
        # Missing alive value: never restart, but the member is still pruned.
        dyno = make_dyno()
        backend, client = self._ready_backend(
            stale=["normal_worker.2"], fresh=[], alive_values=[None],
        )
        with patch("heroku_manager.heroku.cache", backend):
            with patch.object(dyno, "restart_zombie_dyno") as mock_restart:
                dyno.check_for_sibling_zombie_dynos()
        mock_restart.assert_not_called()
        client.zremrangebyscore.assert_called_once()

    def test_alive_evaluation_ordered_before_prune(self):
        # Alive reads must precede the single ZREMRANGEBYSCORE prune.
        dyno = make_dyno()
        backend, client = self._ready_backend(
            stale=["normal_worker.2"], fresh=[],
            alive_values=[pickle.dumps(timezone.now())],
        )
        with patch("heroku_manager.heroku.cache", backend):
            dyno.check_for_sibling_zombie_dynos()
        calls = [name for name, args, kwargs in client.method_calls]
        self.assertLess(calls.index("mget"), calls.index("zremrangebyscore"))

    def test_prune_uses_write_client_not_read_replica(self):
        # Not-ready path: readiness reads through the read client; the one
        # prune goes through get_client(write=True).  No write rides on the
        # read client.
        dyno = make_dyno(index_ready_seed=False)
        backend = MagicMock()
        read_client = MagicMock()
        read_client.zscore.return_value = None
        write_client = MagicMock()
        write_client.zremrangebyscore.return_value = 1
        backend.client.get_client.side_effect = (
            lambda write=False: write_client if write else read_client
        )
        backend.make_key.side_effect = lambda key: key
        backend.get.return_value = None
        with patch("heroku_manager.heroku.cache", backend):
            dyno.check_for_sibling_zombie_dynos()
        backend.client.get_client.assert_any_call(write=True)
        read_client.zremrangebyscore.assert_not_called()
        write_client.zremrangebyscore.assert_called_once()

    def test_prune_runs_once_before_readiness_check_then_returns_immediately(self):
        # The zombie check prunes once under the lock; when the own score is
        # missing it returns immediately, with no member read and no restart.
        dyno = make_dyno(index_ready_seed=False)
        backend = MagicMock()
        client = MagicMock()
        client.get.return_value = None  # degraded marker absent unless the test sets it
        client.zremrangebyscore.return_value = 2
        client.zscore.return_value = None
        backend.client.get_client.return_value = client
        backend.make_key.side_effect = lambda key: key
        backend.get.return_value = None
        with patch("heroku_manager.heroku.cache", backend):
            with patch.object(dyno, "restart_zombie_dyno") as mock_restart:
                dyno.check_for_sibling_zombie_dynos()
        client.zremrangebyscore.assert_called_once()
        client.zscore.assert_called_once()
        client.zrangebyscore.assert_not_called()
        client.mget.assert_not_called()
        client.scan.assert_not_called()
        mock_restart.assert_not_called()

    def test_cached_ready_none_fresh_members_or_mget_fails_closed(self):
        # Cached-ready dyno, but a transient stale-member or alive-value read
        # returns None: evaluation is incomplete, so the check must return
        # immediately (no restart, no prune, no raise).
        for members, mget_values in ((None, None), (["normal_worker.2"], None)):
            with self.subTest(members=members):
                dyno = make_dyno()  # cached-ready seed
                backend = MagicMock()
                client = MagicMock()
                client.zremrangebyscore.return_value = 0
                client.zrangebyscore.return_value = members
                client.mget.return_value = mget_values
                backend.client.get_client.return_value = client
                backend.make_key.side_effect = lambda key: key
                backend.get.return_value = None
                with patch("heroku_manager.heroku.cache", backend):
                    with patch.object(dyno, "restart_zombie_dyno") as mock_restart:
                        dyno.check_for_sibling_zombie_dynos()
                mock_restart.assert_not_called()
                client.zremrangebyscore.assert_not_called()

    def test_stale_members_use_same_cutoff_as_prune(self):
        # Ready path: stale-member evaluation and the prune must share one max
        # cutoff so no boundary member is pruned unevaluated.
        dyno = make_dyno()
        cutoff = 12345.0
        backend, client = self._ready_backend(
            stale=["normal_worker.2"], fresh=[],
            alive_values=[pickle.dumps(timezone.now())],
        )
        with patch("heroku_manager.heroku._stale_cutoff", return_value=cutoff):
            with patch("heroku_manager.heroku.cache", backend):
                dyno.check_for_sibling_zombie_dynos()
        stale_call = [c for c in client.zrangebyscore.call_args_list if c.args[1] == "-inf"][0]
        self.assertEqual(stale_call.args[2], cutoff)
        self.assertEqual(client.zremrangebyscore.call_args.args[2], cutoff)

    def test_restart_zombie_dyno_delegates_to_restart_dyno(self):
        dyno = make_dyno()
        with patch.object(dyno, "restart_dyno") as mock_restart:
            dyno.restart_zombie_dyno("normal_worker.3")
        mock_restart.assert_called_once_with("normal_worker.3")


if __name__ == "__main__":
    unittest.main()
