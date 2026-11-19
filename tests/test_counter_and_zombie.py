"""
Tests: increment_dyno_counter, check_for_sibling_zombie_dynos.
"""
import pickle
import os
import time
import unittest
from unittest.mock import patch, MagicMock, PropertyMock
from tests.conftest import make_dyno, BaseLockTestCase, patch_index_backend
from django.core.cache import cache
from django.conf import settings as django_settings
from django.test import override_settings
from django.utils import timezone
import heroku_manager.heroku as heroku_module
from heroku_manager.heroku import (
    _IndexAdapter,
    _dyno_owner_key,
    _dyno_owner_ttl,
)


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
        client.zrange.return_value = []  # no failed-writer ledger unless the test sets it
        owner_value = object()
        backend.client.encode.return_value = owner_value
        client.get.side_effect = lambda key: owner_value if "dyno_owner" in str(key) else None
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
            mock_restart.assert_called_once_with("normal_worker.2", 300)

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
            mock_restart.assert_called_once_with("normal_worker.2", 300)
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

    @override_settings(DYNO_AUTOSCALE_INTERVAL=30, DYNO_ZOMBIE_THRESHOLD=240)
    def test_readiness_failure_prunes_only_after_heartbeat_grace(self):
        # The zombie check prunes once under the lock; when the own score is
        # missing it returns immediately, with no member read and no restart.
        dyno = make_dyno(index_ready_seed=False)
        now_epoch = 1_700_000_000.0
        prune_cutoff = now_epoch - (240 + _dyno_owner_ttl())
        backend = MagicMock()
        client = MagicMock()
        client.zrange.return_value = []  # no failed-writer ledger unless the test sets it
        owner_value = object()
        backend.client.encode.return_value = owner_value
        client.get.side_effect = lambda key: owner_value if "dyno_owner" in str(key) else None
        client.zremrangebyscore.return_value = 2
        client.zscore.return_value = None
        backend.client.get_client.return_value = client
        backend.make_key.side_effect = lambda key: key
        backend.get.return_value = None
        with patch("heroku_manager.heroku.time.time", return_value=now_epoch), \
                patch("heroku_manager.heroku.cache", backend):
            with patch.object(dyno, "restart_zombie_dyno") as mock_restart:
                dyno.check_for_sibling_zombie_dynos()
        client.zremrangebyscore.assert_called_once()
        self.assertEqual(client.zremrangebyscore.call_args.args[2], prune_cutoff)
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
                client.zrange.return_value = []  # no failed-writer ledger unless the test sets it
                client.get.return_value = None  # degraded marker absent unless the test sets it
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

    @override_settings(DYNO_AUTOSCALE_INTERVAL=30, DYNO_ZOMBIE_THRESHOLD=240)
    def test_prune_waits_for_heartbeat_grace_after_stale_evaluation(self):
        dyno = make_dyno()
        now_epoch = 1_700_000_000.0
        evaluation_cutoff = now_epoch - 240
        prune_cutoff = now_epoch - (240 + _dyno_owner_ttl())
        backend, client = self._ready_backend(
            stale=["normal_worker.2"], fresh=[],
            alive_values=[pickle.dumps(timezone.now())],
        )
        with patch("heroku_manager.heroku.time.time", return_value=now_epoch), \
                patch("heroku_manager.heroku.cache", backend), \
                patch.object(dyno, "restart_zombie_dyno"):
            dyno.check_for_sibling_zombie_dynos()
        stale_call = [c for c in client.zrangebyscore.call_args_list if c.args[1] == "-inf"][0]
        self.assertEqual(stale_call.args[2], evaluation_cutoff)
        self.assertEqual(client.zremrangebyscore.call_args.args[2], prune_cutoff)
        self.assertLess(prune_cutoff, evaluation_cutoff)

    def test_restart_zombie_dyno_delegates_to_restart_dyno(self):
        dyno = make_dyno()
        alive = timezone.now() - timezone.timedelta(seconds=301)
        with patch.object(_IndexAdapter, "read_owned_liveness",
                          return_value=("found", "owner", 10.0, alive)), \
                patch.object(dyno, "restart_dyno") as mock_restart:
            dyno.restart_zombie_dyno("normal_worker.3")
        mock_restart.assert_called_once_with(
            "normal_worker.3", zombie_threshold=300,
        )

    def test_fresh_primary_liveness_cancels_stale_batch_restart(self):
        dyno = make_dyno()
        threshold = django_settings.DYNO_ZOMBIE_THRESHOLD
        backend, _ = self._ready_backend(
            stale=[],
            fresh=["normal_worker.1", "normal_worker.2"],
            alive_values=[
                pickle.dumps(timezone.now()),
                pickle.dumps(timezone.now() - timezone.timedelta(seconds=threshold + 10)),
            ],
        )
        with patch("heroku_manager.heroku.cache", backend), \
                patch.object(
                    _IndexAdapter,
                    "read_owned_liveness",
                    return_value=("found", "owner", 10.0, timezone.now()),
                ), \
                patch.object(dyno, "restart_dyno") as mock_restart:
            dyno.check_for_sibling_zombie_dynos()
        mock_restart.assert_not_called()

    @override_settings(DYNO_AUTOSCALE_INTERVAL=30, DYNO_ZOMBIE_THRESHOLD=240)
    def test_expired_owner_lease_still_preserves_stale_zombie_evidence(self):
        dyno = make_dyno()
        sibling = "normal_worker.3"
        alive = timezone.now() - timezone.timedelta(seconds=300)
        with patch_index_backend({}):
            adapter = _IndexAdapter(dyno.app_name, primary=True)
            self.assertEqual(
                adapter.claim_and_register(
                    sibling, "dead-owner", time.time(), alive, 240,
                ),
                "claimed",
            )
            heroku_module.cache.delete(
                _dyno_owner_key(dyno.app_name, sibling),
            )
            status, owner, _, _ = adapter.read_owned_liveness(sibling)
            self.assertEqual((status, owner), ("owner_expired", None))
            self.assertLess(_dyno_owner_ttl(), 240)

            with patch.object(
                dyno, "call_heroku_api", return_value=MagicMock(status_code=202),
            ) as api:
                self.assertTrue(dyno.restart_zombie_dyno(sibling, 240))
            self.assertEqual(api.call_args.args[0], "DELETE")

    @override_settings(DYNO_AUTOSCALE_INTERVAL=30, DYNO_ZOMBIE_THRESHOLD=240)
    def test_normal_heartbeat_survives_the_next_scan_after_threshold(self):
        sibling = "normal_worker.3"
        dead = make_dyno(sibling)
        reading = MagicMock(value=300, source_epoch=time.time())
        claimed_ttls = []
        original_claim = _IndexAdapter.claim_and_register

        def capture_claim(
                adapter, dyno_name, owner_token, score, alive_value, alive_ttl,
                **kwargs,
        ):
            claimed_ttls.append(alive_ttl)
            return original_claim(
                adapter, dyno_name, owner_token, score, alive_value, alive_ttl,
                **kwargs,
            )

        with patch_index_backend({}):
            with patch.dict(os.environ, {"DYNO": sibling}), \
                    patch.object(type(dead), "memory_reading_for_observation",
                                 new_callable=PropertyMock, return_value=reading), \
                    patch.object(type(dead), "load_1min_for_observation",
                                 new_callable=PropertyMock, return_value=0.2), \
                    patch.object(_IndexAdapter, "claim_and_register", new=capture_claim):
                self.assertTrue(dead.check_in_dyno())

            self.assertEqual(claimed_ttls, [240 + _dyno_owner_ttl()])
            self.assertGreater(claimed_ttls[0], 240)

            heroku_module.cache.delete(_dyno_owner_key(dead.app_name, sibling))
            next_scan = timezone.now() + timezone.timedelta(seconds=241)
            dyno = make_dyno()
            with patch.object(heroku_module.timezone, "now", return_value=next_scan), \
                    patch.object(
                        dyno, "call_heroku_api", return_value=MagicMock(status_code=202),
                    ) as api:
                self.assertTrue(dyno.restart_zombie_dyno(sibling, 240))
        api.assert_called_once()
        self.assertEqual(api.call_args.args[0], "DELETE")

    @override_settings(DYNO_AUTOSCALE_INTERVAL=30, DYNO_ZOMBIE_THRESHOLD=240)
    def test_live_contention_blocks_zombie_restart_until_it_clears(self):
        dyno = make_dyno()
        sibling = "normal_worker.3"
        stale = timezone.now() - timezone.timedelta(seconds=301)

        with patch_index_backend({}):
            adapter = _IndexAdapter(dyno.app_name, primary=True)
            self.assertEqual(
                adapter.claim_and_register(
                    sibling, "dead-owner", time.time(), stale, 300,
                ),
                "claimed",
            )
            heroku_module.cache.delete(_dyno_owner_key(dyno.app_name, sibling))
            heroku_module.cache.set(
                f"heroku:dyno_contention:v1:{dyno.app_name}:{sibling}",
                "replacement-owner",
                timeout=60,
            )
            self.assertEqual(
                adapter.read_owned_liveness(sibling),
                ("contended", None, None, None),
            )

            with patch.object(dyno, "call_heroku_api") as api:
                self.assertFalse(dyno.restart_zombie_dyno(sibling, 240))
            api.assert_not_called()
            self.assertIsNone(
                heroku_module.cache.get(f"heroku:restart_dyno:{sibling}"),
            )

            heroku_module.cache.delete(
                f"heroku:dyno_contention:v1:{dyno.app_name}:{sibling}",
            )
            with patch.object(
                dyno, "call_heroku_api", return_value=MagicMock(status_code=202),
            ) as api:
                self.assertTrue(dyno.restart_zombie_dyno(sibling, 240))
            api.assert_called_once()
            self.assertEqual(api.call_args.args[0], "DELETE")

    @override_settings(DYNO_AUTOSCALE_INTERVAL=30, DYNO_ZOMBIE_THRESHOLD=240)
    def test_contested_scan_preserves_score_for_the_next_scheduled_retry(self):
        dyno = make_dyno("normal_worker.1")
        sibling = "normal_worker.3"
        now = timezone.now()
        stale_score = time.time() - 300

        with patch_index_backend({}):
            heroku_module.cache.client.get_client(write=False)._backend._ensure_index()
            adapter = _IndexAdapter(dyno.app_name, primary=True)
            self.assertEqual(
                adapter.claim_and_register(
                    sibling,
                    "dead-owner",
                    stale_score,
                    now - timezone.timedelta(seconds=301),
                    240 + _dyno_owner_ttl(),
                ),
                "claimed",
            )
            raw_client = heroku_module.cache.client.get_client(write=False)
            raw_client._backend._ensure_index = lambda: None

            def zrangebyscore(_key, min_score, max_score, withscores=False):
                if min_score == "-inf":
                    selected = [
                        (member, score)
                        for member, score in raw_client._backend._index_scores.items()
                        if score <= float(max_score)
                    ]
                else:
                    lower = float(str(min_score).removeprefix("("))
                    selected = [
                        (member, score)
                        for member, score in raw_client._backend._index_scores.items()
                        if score > lower
                    ]
                return selected if withscores else [member for member, _ in selected]

            def zremrangebyscore(_key, _min_score, max_score):
                removed = [
                    member
                    for member, score in raw_client._backend._index_scores.items()
                    if score <= float(max_score)
                ]
                for member in removed:
                    raw_client._backend._index_scores.pop(member, None)
                return len(removed)

            raw_client.zrangebyscore = zrangebyscore
            raw_client.zremrangebyscore = zremrangebyscore
            heroku_module.cache.set(
                f"heroku:dyno_contention:v1:{dyno.app_name}:{sibling}",
                "replacement-owner",
                timeout=60,
            )

            with patch.object(dyno, "call_heroku_api") as api:
                dyno.check_for_sibling_zombie_dynos()
            api.assert_not_called()
            preserved_score = raw_client.zscore(adapter._index_key, sibling)
            self.assertEqual(preserved_score, stale_score)
            self.assertIsNone(
                heroku_module.cache.get(f"heroku:restart_dyno:{sibling}"),
            )

            heroku_module.cache.delete(
                f"heroku:dyno_contention:v1:{dyno.app_name}:{sibling}",
            )
            self.assertIn(sibling, adapter.stale_members(time.time() - 240))
            self.assertIsNotNone(dyno._stale_zombie_evidence(sibling, 240))
            self.assertTrue(dyno.index_ready)
            self.assertFalse(heroku_module._index_fail_closed(dyno))
            with patch.object(
                dyno, "restart_zombie_dyno", wraps=dyno.restart_zombie_dyno,
            ) as restart, patch.object(
                dyno, "call_heroku_api", return_value=MagicMock(status_code=202),
            ) as api:
                dyno.check_for_sibling_zombie_dynos()
            restart.assert_called_once_with(sibling, 240)
            api.assert_called_once()
            self.assertEqual(api.call_args.args[0], "DELETE")

    def test_zombie_restart_uses_lock_captured_owner_for_cleanup(self):
        dyno = make_dyno()
        sibling = "normal_worker.3"
        stale = timezone.now() - timezone.timedelta(seconds=301)
        with patch_index_backend({}):
            adapter = _IndexAdapter(dyno.app_name, primary=True)
            score = time.time()
            self.assertEqual(
                adapter.claim_and_register(
                    sibling, "owner-at-lock", score, stale, 300,
                ),
                "claimed",
            )
            api_response = MagicMock(status_code=202)
            with patch.object(
                dyno, "call_heroku_api", return_value=api_response,
            ) as api:
                self.assertTrue(dyno.restart_zombie_dyno(sibling, 300))
            self.assertEqual(api.call_args.args[0], "DELETE")
            self.assertEqual(
                adapter.get_owner_and_score(sibling),
                ("missing", None, None),
            )

    def test_fresh_replacement_at_restart_lock_acquisition_cancels_dispatch(self):
        dyno = make_dyno()
        sibling = "normal_worker.3"
        stale = timezone.now() - timezone.timedelta(seconds=301)

        class ReplacingLock:
            def __enter__(self):
                adapter = _IndexAdapter(dyno.app_name, primary=True)
                heroku_module.cache.delete(
                    _dyno_owner_key(dyno.app_name, sibling),
                )
                self.claim_status = adapter.claim_and_register(
                    sibling,
                    "replacement-owner",
                    time.time() + 1,
                    timezone.now(),
                    300,
                )
                return self

            def __exit__(self, *_args):
                return False

        with patch_index_backend({}):
            adapter = _IndexAdapter(dyno.app_name, primary=True)
            self.assertEqual(
                adapter.claim_and_register(
                    sibling, "dead-owner", time.time(), stale, 300,
                ),
                "claimed",
            )
            replacing_lock = ReplacingLock()
            with patch.object(heroku_module.cache, "lock",
                              return_value=replacing_lock), \
                    patch.object(dyno, "call_heroku_api") as api:
                dyno.restart_zombie_dyno(sibling, 300)
            self.assertEqual(replacing_lock.claim_status, "claimed")
            api.assert_not_called()
            self.assertIsNone(
                heroku_module.cache.get(f"heroku:restart_dyno:{sibling}"),
            )


if __name__ == "__main__":
    unittest.main()
