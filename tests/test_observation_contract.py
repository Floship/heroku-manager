import os
import sys
import time
import unittest
from contextlib import ExitStack, contextmanager
from datetime import timedelta
from types import ModuleType
from unittest.mock import MagicMock, PropertyMock, patch

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

import heroku_manager.heroku as heroku_module
from heroku_manager.heroku import (
    _dyno_memory_veto_key,
    _dyno_owner_key,
    _dyno_owner_ttl,
    _IndexAdapter,
    _log_dyno_identity,
    _MetricReading,
    _observation_key,
)
from tests.conftest import make_dyno, patch_index_backend

SELF_DYNO_ID = "11111111-1111-4111-8111-111111111111"
OTHER_DYNO_ID = "22222222-2222-4222-8222-222222222222"


def _make_identified_dyno(dyno_name="normal_worker.1", dyno_id=SELF_DYNO_ID):
    dyno = make_dyno(dyno_name)
    dyno.dyno_id = dyno_id
    return dyno


def _metric_log(
    dyno_name, dyno_id, message, timestamp, *, include_source=True,
):
    if include_source:
        message = f"source={dyno_name} dyno=heroku.123.{dyno_id} {message}"
    return {
        "timestamp": timestamp,
        "message": message,
        "source_dyno_name": dyno_name if include_source else None,
        "source_dyno_id": dyno_id if include_source else None,
    }


def _reading(value=512, source_epoch=None):
    return _MetricReading(value, time.time() if source_epoch is None else source_epoch)


def _owned_values(observations, *, owner="owner", contention=None, veto=None):
    values = []
    for observation in observations:
        values.extend([owner, observation])
    return values


def _strict_log_pair(timestamp, memory=200, load=0.2):
    dyno_name = os.environ["DYNO"]
    return [
        _metric_log(
            dyno_name,
            SELF_DYNO_ID,
            f"sample#memory_total={memory:.2f}MB",
            timestamp,
        ),
        _metric_log(
            dyno_name,
            SELF_DYNO_ID,
            f"sample#load_avg_1m={load:.2f}",
            timestamp,
        ),
    ]


@contextmanager
def _strict_dyno(own_memory=200, formation_size="standard-2x"):
    dyno = make_dyno(formation_size=formation_size)
    with ExitStack() as stack:
        stack.enter_context(patch.object(
            type(dyno), "current_memory_usage",
            new_callable=PropertyMock, return_value=own_memory,
        ))
        stack.enter_context(patch.object(
            type(dyno), "current_memory_usage_percentage",
            new_callable=PropertyMock,
            return_value=(own_memory / dyno.available_memory * 100),
        ))
        stack.enter_context(patch.object(
            type(dyno), "avg_load_1min",
            new_callable=PropertyMock, return_value=0.2,
        ))
        stack.enter_context(patch.object(
            type(dyno), "detected_r14",
            new_callable=PropertyMock, return_value=False,
        ))
        stack.enter_context(patch.object(
            type(dyno), "detected_r15",
            new_callable=PropertyMock, return_value=False,
        ))
        stack.enter_context(patch.object(
            type(dyno), "no_tasks_in_queue",
            new_callable=PropertyMock, return_value=True,
        ))
        yield dyno


@contextmanager
def _strict_index(
    rosters, observations, *, owners=True, contention=None, veto=None,
    final_owner=None, final_contention=None, final_veto=None,
    final_degraded=False, final_owner_raw=None, final_owner_wrong_type=False,
    final_watch_error=False,
):
    backend = MagicMock()
    client = MagicMock()
    backend.client.get_client.return_value = client
    backend.client.decode.side_effect = (
        lambda value: None
        if value == b"serialized-owner-none"
        else value
    )
    backend.client.encode.side_effect = lambda value: value
    backend.make_key.side_effect = lambda key: key
    backend.get.return_value = None
    client.decode.side_effect = lambda value: value
    client.get.return_value = None
    client.zrange.return_value = []
    pipeline = MagicMock()
    pipeline.watch.return_value = pipeline
    pipeline.multi.return_value = pipeline
    pipeline.ping.return_value = pipeline
    client.pipeline.return_value = pipeline
    client.pipeline_instance = pipeline
    if callable(rosters):
        client.zrangebyscore.side_effect = rosters
        pipeline.zrangebyscore.side_effect = rosters
    else:
        pending_rosters = list(rosters)

        def next_roster(*args, **kwargs):
            if len(pending_rosters) > 1:
                return pending_rosters.pop(0)
            return pending_rosters[0]

        client.zrangebyscore.side_effect = next_roster
        pipeline.zrangebyscore.side_effect = next_roster
    observation_values = list(observations)
    observation_index = 0

    def initial_values(keys):
        nonlocal observation_index
        values = []
        for key in keys:
            if "heroku:dyno_owner:v1:" in key:
                values.append("owner" if owners else None)
            else:
                if observation_index >= len(observation_values):
                    values.append(None)
                else:
                    values.append(observation_values[observation_index])
                    observation_index += 1
        return values

    def final_values(keys):
        values = []
        for key in keys:
            if "heroku:dyno_owner:v1:" in key:
                if not owners:
                    values.append(None)
                elif final_owner_wrong_type:
                    values.append(None)
                elif final_owner_raw is not None:
                    values.append(final_owner_raw)
                else:
                    values.append(final_owner or "owner")
            else:
                values.append(None)
        return values

    def marker_count(marker, keys):
        if marker is None:
            return 0
        return sum(1 for key in keys if marker in key)

    def initial_exists(*keys):
        return (
            marker_count("heroku:dyno_contention:v1:", keys)
            if contention is not None else 0
        ) + (
            marker_count("heroku:dyno_memory_veto:v1:", keys)
            if veto is not None else 0
        )

    def final_exists(*keys):
        count = (
            marker_count("heroku:dyno_contention:v1:", keys)
            if final_contention is not None else 0
        ) + (
            marker_count("heroku:dyno_memory_veto:v1:", keys)
            if final_veto is not None else 0
        )
        if final_owner_wrong_type:
            count += marker_count("heroku:dyno_owner:v1:", keys)
        return count

    client.mget.side_effect = initial_values
    pipeline.mget.side_effect = final_values
    client.exists.side_effect = initial_exists
    pipeline.exists.side_effect = final_exists
    reconverging_key = heroku_module._registry_reconverging_key("floship")

    def final_get(key):
        if key == reconverging_key and final_degraded:
            return b"reconverging"
        return None

    pipeline.get.side_effect = final_get
    pipeline.zrange.return_value = [(b"normal_worker.2", 1.0)] if final_degraded else []
    if final_watch_error:
        from redis.exceptions import WatchError
        pipeline.execute.side_effect = WatchError("roster changed")
    else:
        pipeline.execute.return_value = True
    with patch("heroku_manager.heroku.cache", backend):
        yield client


class TestObservationWriter(unittest.TestCase):

    def setUp(self):
        cache.clear()
        self._env_patch = patch.dict(os.environ, {"DYNO": "normal_worker.1"})
        self._env_patch.start()
        self._backend_patch = patch_index_backend({})
        self._backend_patch.start()

    def tearDown(self):
        self._backend_patch.stop()
        self._env_patch.stop()
        cache.clear()

    def test_registry_score_is_visible_before_observation_tuple(self):
        dyno = make_dyno("normal_worker.1")
        key = _observation_key(dyno.app_name, dyno.dyno_name)
        tuple_at_registration = []
        published_scores = []

        def update_registry(
            adapter, dyno_name, owner_token, score, alive_value, alive_ttl,
            **kwargs,
        ):
            tuple_at_registration.append(cache.get(key))
            published_scores.append(score)
            return original_claim(
                adapter, dyno_name, owner_token, score, alive_value, alive_ttl,
                **kwargs,
            )

        original_claim = _IndexAdapter.claim_and_register

        with patch.object(type(dyno), "memory_reading_for_observation",
                          new_callable=PropertyMock, return_value=_reading(512)), \
                patch.object(type(dyno), "load_1min_for_observation",
                             new_callable=PropertyMock, return_value=0.25), \
                patch.object(_IndexAdapter, "claim_and_register",
                             new=update_registry):
            dyno.check_in_dyno()

        observation = cache.get(key)
        self.assertEqual(tuple_at_registration, [None])
        self.assertEqual(observation.check_in_epoch, published_scores[0])
        self.assertEqual(observation.memory_mb, 512)
        self.assertEqual(observation.load_1m, 0.25)
        self.assertEqual(cache.get("heroku:dyno_memory:normal_worker.1"), 512)
        self.assertEqual(cache.get("heroku:dyno_load:normal_worker.1"), 0.25)

    def test_empty_dyno_scope_is_rejected(self):
        self.assertEqual(
            _log_dyno_identity(
                f"source=normal_worker.1 dyno=heroku..{SELF_DYNO_ID} "
                "sample#memory_total=512.0MB"
            ),
            (None, None),
        )

    def test_stability_uses_the_history_with_the_new_sample_once(self):
        dyno = make_dyno("normal_worker.1")
        now = time.time()
        history = [(now - (8 - index) * 30, 15000) for index in range(8)]
        cache.set(dyno.memory_history_cache_key, history, timeout=600)

        with patch.object(type(dyno), "memory_reading_for_observation",
                          new_callable=PropertyMock, return_value=_reading(20000, now)), \
                patch.object(type(dyno), "load_1min_for_observation",
                             new_callable=PropertyMock, return_value=0.2), \
                patch.object(type(dyno), "is_memory_stabilized",
                             new_callable=PropertyMock, return_value=True), \
                patch.object(heroku_module.cache, "get",
                             wraps=heroku_module.cache.get) as get:
            dyno.check_in_dyno()

        observation = cache.get(_observation_key(dyno.app_name, dyno.dyno_name))
        self.assertFalse(observation.stable)
        history_reads = [
            call for call in get.call_args_list
            if call.args[0] == dyno.memory_history_cache_key
        ]
        self.assertEqual(len(history_reads), 1)

    def test_registry_is_published_before_slow_telemetry_reads(self):
        dyno = make_dyno("normal_worker.1")
        order = []

        original_claim = _IndexAdapter.claim_and_register

        def update_registry(
            adapter, dyno_name, owner_token, score, alive_value, alive_ttl,
            **kwargs,
        ):
            order.append("registry")
            return original_claim(
                adapter, dyno_name, owner_token, score, alive_value, alive_ttl,
                **kwargs,
            )

        def read_memory():
            order.append("memory")
            return _reading(512)

        def read_load():
            order.append("load")
            return 0.2

        with patch.object(_IndexAdapter, "claim_and_register",
                          new=update_registry), \
                patch.object(type(dyno), "memory_reading_for_observation",
                             new_callable=PropertyMock, side_effect=read_memory), \
                patch.object(type(dyno), "load_1min_for_observation",
                             new_callable=PropertyMock, side_effect=read_load):
            dyno.check_in_dyno()

        self.assertEqual(order, ["registry", "memory", "load"])

    def test_failed_publication_keeps_strict_readers_closed_and_legacy_keys(self):
        dyno = make_dyno("normal_worker.1")
        observation_key = _observation_key(dyno.app_name, dyno.dyno_name)
        cache.set("heroku:dyno_memory:normal_worker.1", 480, timeout=300)
        cache.set("heroku:dyno_memory_stable:normal_worker.1", False, timeout=300)
        cache.set("heroku:dyno_load:normal_worker.1", 0.1, timeout=300)

        with patch.object(type(dyno), "memory_reading_for_observation",
                          new_callable=PropertyMock, return_value=_reading(512)), \
                patch.object(type(dyno), "load_1min_for_observation",
                             new_callable=PropertyMock, return_value=0.25), \
                patch.object(_IndexAdapter, "publish_owned_values",
                             return_value="unavailable"):
            dyno.check_in_dyno()

        self.assertIsNone(cache.get(observation_key))
        self.assertEqual(cache.get("heroku:dyno_memory:normal_worker.1"), 480)
        self.assertEqual(cache.get("heroku:dyno_load:normal_worker.1"), 0.1)
        score = time.time()
        with _strict_index([[(dyno.dyno_name, score)]], [None]):
            self.assertIsNone(dyno._formation_observation_snapshot())

    def test_missing_measurements_remain_none_with_the_zombie_ttl(self):
        dyno = make_dyno("normal_worker.1")
        original_publish = _IndexAdapter.publish_owned_values
        published_values = []

        def capture_publish(
            adapter, dyno_name, owner_token, score, values, **kwargs,
        ):
            published_values.extend(values)
            return original_publish(
                adapter, dyno_name, owner_token, score, values, **kwargs,
            )

        with patch.object(type(dyno), "memory_reading_for_observation",
                          new_callable=PropertyMock, return_value=None), \
                patch.object(type(dyno), "load_1min_for_observation",
                             new_callable=PropertyMock, return_value=None), \
                patch.object(_IndexAdapter, "publish_owned_values", new=capture_publish):
            dyno.check_in_dyno()

        key = _observation_key(dyno.app_name, dyno.dyno_name)
        observation = cache.get(key)
        self.assertIsNone(observation.memory_mb)
        self.assertIsNone(observation.load_1m)
        self.assertFalse(observation.stable)
        observation_values = [value for cached_key, value, _ in published_values if cached_key == key]
        self.assertEqual(len(observation_values), 1)
        observation_timeouts = [timeout for cached_key, _, timeout in published_values if cached_key == key]
        self.assertEqual(observation_timeouts, [settings.DYNO_ZOMBIE_THRESHOLD])
        self.assertIsNone(cache.get("heroku:dyno_memory:normal_worker.1"))
        self.assertIsNone(cache.get("heroku:dyno_load:normal_worker.1"))

    def test_memory_pressure_veto_rejects_old_publish_then_clears_on_fresh_sample(self):
        dyno = make_dyno("normal_worker.1")
        dyno.dyno_id = SELF_DYNO_ID
        veto_key = f"heroku:dyno_memory_veto:v1:{dyno.app_name}:{dyno.dyno_name}"
        nudge_epoch = time.time()

        with patch_index_backend({}), \
                patch.object(type(dyno), "memory_reading_for_observation",
                             new_callable=PropertyMock,
                             return_value=_MetricReading(200, nudge_epoch - 10)), \
                patch.object(type(dyno), "load_1min_for_observation",
                             new_callable=PropertyMock, return_value=0.2):
            self.assertTrue(dyno.check_in_dyno())
            with patch("heroku_manager.heroku.time.time", return_value=nudge_epoch):
                self.assertTrue(dyno.record_memory_pressure(700))

            self.assertIsNone(dyno._formation_observation_snapshot())
            self.assertEqual(cache.get(veto_key)[2], 700)
            self.assertEqual(cache.get("heroku:dyno_memory:normal_worker.1"), 700)
            self.assertFalse(cache.get("heroku:dyno_memory_stable:normal_worker.1"))

            with patch.object(
                        type(dyno),
                        "memory_reading_for_observation",
                        new_callable=PropertyMock,
                        return_value=_MetricReading(210, nudge_epoch - 1),
                    ):
                self.assertFalse(dyno.check_in_dyno())
            self.assertIsNotNone(cache.get(veto_key))
            self.assertEqual(cache.get("heroku:dyno_memory:normal_worker.1"), 700)

            with patch.object(
                        type(dyno),
                        "memory_reading_for_observation",
                        new_callable=PropertyMock,
                        return_value=_MetricReading(210, nudge_epoch + 1),
                    ):
                self.assertTrue(dyno.check_in_dyno())
            self.assertIsNone(cache.get(veto_key))
            snapshot = dyno._formation_observation_snapshot()
            self.assertIsNotNone(snapshot)
            self.assertEqual(snapshot[1][dyno.dyno_name].memory_mb, 210)
            self.assertFalse(snapshot[1][dyno.dyno_name].stable)

    def test_memory_pressure_requires_the_current_owner_and_never_falls_back(self):
        dyno = make_dyno("normal_worker.1")
        veto_key = f"heroku:dyno_memory_veto:v1:{dyno.app_name}:{dyno.dyno_name}"
        with patch_index_backend({}):
            with patch.object(type(dyno), "memory_reading_for_observation",
                              new_callable=PropertyMock,
                              return_value=_MetricReading(200, time.time())), \
                    patch.object(type(dyno), "load_1min_for_observation",
                                 new_callable=PropertyMock, return_value=0.2):
                self.assertTrue(dyno.check_in_dyno())
            heroku_module.cache.set(
                _dyno_owner_key(dyno.app_name, dyno.dyno_name),
                "replacement-owner",
                timeout=60,
            )
            self.assertFalse(dyno.record_memory_pressure(700))
            self.assertIsNone(cache.get(veto_key))
            self.assertEqual(cache.get("heroku:dyno_memory:normal_worker.1"), 200)

    def test_memory_pressure_veto_requires_a_valid_newer_memory_sample(self):
        dyno = make_dyno("normal_worker.1")
        dyno.dyno_id = SELF_DYNO_ID
        veto_key = f"heroku:dyno_memory_veto:v1:{dyno.app_name}:{dyno.dyno_name}"
        now = time.time()

        with patch_index_backend({}), \
                patch.object(type(dyno), "memory_reading_for_observation",
                             new_callable=PropertyMock,
                             return_value=_MetricReading(200, now - 10)), \
                patch.object(type(dyno), "load_1min_for_observation",
                             new_callable=PropertyMock, return_value=0.2):
            self.assertTrue(dyno.check_in_dyno())
            self.assertTrue(dyno.record_memory_pressure(700))
            with patch.object(
                        type(dyno),
                        "memory_reading_for_observation",
                        new_callable=PropertyMock,
                        return_value=_MetricReading(0, now + 1),
                    ):
                self.assertFalse(dyno.check_in_dyno())

        self.assertIsNotNone(cache.get(veto_key))
        self.assertEqual(cache.get("heroku:dyno_memory:normal_worker.1"), 700)

    def test_veto_arriving_after_the_preliminary_read_forces_an_unstable_tuple(self):
        dyno = make_dyno("normal_worker.1")
        dyno.dyno_id = SELF_DYNO_ID
        veto_key = f"heroku:dyno_memory_veto:v1:{dyno.app_name}:{dyno.dyno_name}"
        now = time.time()
        original_read = dyno._current_memory_pressure_veto
        nudged = False

        def read_then_nudge():
            nonlocal nudged
            value = original_read()
            if not nudged:
                cache.set(
                    veto_key,
                    (dyno._dyno_owner_token, now - 1, 700),
                    timeout=60,
                )
                nudged = True
            return value

        with patch_index_backend({}), \
                patch.object(type(dyno), "memory_reading_for_observation",
                             new_callable=PropertyMock,
                             return_value=_MetricReading(200, now)), \
                patch.object(type(dyno), "load_1min_for_observation",
                             new_callable=PropertyMock, return_value=0.2), \
                patch.object(dyno, "record_memory_reading", return_value=True), \
                patch.object(dyno, "_current_memory_pressure_veto",
                             side_effect=read_then_nudge):
            self.assertTrue(dyno.check_in_dyno())

        observation = cache.get(
            _observation_key(dyno.app_name, dyno.dyno_name),
        )
        self.assertIsNotNone(observation)
        self.assertFalse(observation.stable)
        self.assertFalse(cache.get("heroku:dyno_memory_stable:normal_worker.1"))
        self.assertIsNone(cache.get(veto_key))

    def test_reversed_memory_pressure_nudges_keep_the_newest_veto(self):
        dyno = make_dyno("normal_worker.1")
        dyno.dyno_id = SELF_DYNO_ID
        veto_key = f"heroku:dyno_memory_veto:v1:{dyno.app_name}:{dyno.dyno_name}"
        newest_epoch = time.time()

        with patch_index_backend({}), \
                patch.object(
                    type(dyno),
                    "memory_reading_for_observation",
                    new_callable=PropertyMock,
                    return_value=_MetricReading(200, newest_epoch - 30),
                ), \
                patch.object(
                    type(dyno),
                    "load_1min_for_observation",
                    new_callable=PropertyMock,
                    return_value=0.2,
                ):
            self.assertTrue(dyno.check_in_dyno())
            with patch("heroku_manager.heroku.time.time", return_value=newest_epoch):
                self.assertTrue(dyno.record_memory_pressure(700))
            with patch(
                "heroku_manager.heroku.time.time",
                return_value=newest_epoch - 20,
            ):
                self.assertFalse(dyno.record_memory_pressure(900))

            self.assertEqual(
                cache.get(veto_key),
                (dyno._dyno_owner_token, newest_epoch, 700),
            )
            self.assertEqual(
                cache.get("heroku:dyno_memory:normal_worker.1"), 700,
            )

            with patch.object(
                        type(dyno),
                        "memory_reading_for_observation",
                        new_callable=PropertyMock,
                        return_value=_MetricReading(210, newest_epoch - 5),
                    ):
                self.assertFalse(dyno.check_in_dyno())
            self.assertEqual(
                cache.get("heroku:dyno_memory:normal_worker.1"), 700,
            )

            with patch.object(
                        type(dyno),
                        "memory_reading_for_observation",
                        new_callable=PropertyMock,
                        return_value=_MetricReading(210, newest_epoch + 1),
                    ):
                self.assertTrue(dyno.check_in_dyno())

        self.assertIsNone(cache.get(veto_key))
        observation = cache.get(
            _observation_key(dyno.app_name, dyno.dyno_name),
        )
        self.assertIsNotNone(observation)
        self.assertFalse(observation.stable)
        self.assertFalse(cache.get("heroku:dyno_memory_stable:normal_worker.1"))

    def test_six_checkins_share_one_strict_fetch_and_form_a_plateau(self):
        dyno = make_dyno(
            "normal_worker.1", formation_size="performance-l-ram",
        )
        dyno.dyno_id = SELF_DYNO_ID
        now = timezone.now()
        logs = [
            _strict_log_pair(now - timedelta(seconds=150 - index * 30))
            for index in range(6)
        ]

        with patch_index_backend({}), \
                patch.object(
                    dyno,
                    "get_heroku_logs",
                    side_effect=logs,
                ) as fetch:
            for _ in range(6):
                self.assertTrue(dyno.check_in_dyno())

        self.assertEqual(fetch.call_count, 6)
        self.assertTrue(all(
            call.kwargs == {"strict_observation": True}
            for call in fetch.call_args_list
        ))
        history = cache.get(dyno.memory_history_cache_key)
        self.assertEqual(len(history), 6)
        self.assertTrue(dyno.is_memory_stabilized)

    def test_repeated_stale_log_epoch_never_forms_a_plateau(self):
        dyno = make_dyno(
            "normal_worker.1", formation_size="performance-l-ram",
        )
        dyno.dyno_id = SELF_DYNO_ID
        stale = timezone.now() - timedelta(seconds=30)

        with patch_index_backend({}), \
                patch.object(
                    dyno,
                    "get_heroku_logs",
                    return_value=_strict_log_pair(stale),
                ) as fetch:
            for _ in range(6):
                self.assertTrue(dyno.check_in_dyno())

        self.assertEqual(fetch.call_count, 6)
        history = cache.get(dyno.memory_history_cache_key)
        self.assertEqual(len(history), 1)
        self.assertFalse(dyno.is_memory_stabilized)

    def test_strict_raw_log_cache_is_separate_and_expires_each_interval(self):
        dyno = make_dyno("normal_worker.1")
        dyno.dyno_id = SELF_DYNO_ID
        log_response = MagicMock(status_code=200, text=(
            f"{timezone.now().isoformat()} heroku[normal_worker.1]: "
            f"source=normal_worker.1 dyno=heroku.123.{SELF_DYNO_ID} "
            "sample#memory_total=200.00MB sample#load_avg_1m=0.20\n"
        ))
        session_response = MagicMock(status_code=200)
        session_response.json.return_value = {"logplex_url": "https://logs.example"}

        with patch.object(dyno, "call_heroku_api", return_value=session_response), \
                patch("heroku_manager.heroku.requests.get", return_value=log_response), \
                patch.object(heroku_module.cache, "get", return_value=None), \
                patch.object(heroku_module.cache, "lock") as lock, \
                patch.object(heroku_module.cache, "set") as set_cache:
            lock.return_value.__enter__.return_value = None
            lock.return_value.__exit__.return_value = False
            parsed = dyno.get_heroku_logs(strict_observation=True)

        self.assertTrue(parsed)
        cache_key, _logs = set_cache.call_args.args
        self.assertIn(":strict:v1", cache_key)
        self.assertEqual(set_cache.call_args.kwargs["timeout"], 30)

    def test_failed_strict_check_in_fetch_is_shared_by_memory_and_load(self):
        dyno = _make_identified_dyno()

        with patch_index_backend({}), \
                patch.dict(os.environ, {"DYNO": dyno.dyno_name}), \
                patch.object(dyno, "get_heroku_logs", return_value=None) as fetch:
            self.assertTrue(dyno.check_in_dyno())

        self.assertEqual(fetch.call_count, 1)
        self.assertNotIn("_strict_observation_logs", dyno.__dict__)

    def test_failed_standalone_strict_read_is_retried_by_the_next_metric(self):
        dyno = _make_identified_dyno()
        fresh = timezone.now()
        logs = [
            _metric_log(
                dyno.dyno_name,
                SELF_DYNO_ID,
                "sample#memory_total=512.00MB",
                fresh,
            ),
            _metric_log(
                dyno.dyno_name,
                SELF_DYNO_ID,
                "sample#load_avg_1m=0.20",
                fresh,
            ),
        ]

        with patch.dict(os.environ, {"DYNO": dyno.dyno_name}), \
                patch.object(
                    dyno,
                    "get_heroku_logs",
                    side_effect=[None, logs],
                ) as fetch:
            self.assertIsNone(dyno.memory_mb_for_observation)
            self.assertEqual(dyno.load_1min_for_observation, 0.2)

        self.assertEqual(fetch.call_count, 2)
        self.assertNotIn("_strict_observation_logs", dyno.__dict__)

    def test_stale_cached_measurements_are_rejected_when_logs_are_unavailable(self):
        dyno = _make_identified_dyno()
        memory_key = (
            f"heroku:memory_total:{dyno.app_name}:{dyno.dyno_name}:{SELF_DYNO_ID}"
        )
        load_key = (
            f"heroku:load_avg_1m:{dyno.app_name}:{dyno.dyno_name}:{SELF_DYNO_ID}"
        )
        cache.set(memory_key, 512, timeout=1)
        cache.set(load_key, 0.2, timeout=1)

        with patch.dict(os.environ, {"DYNO": dyno.dyno_name}), \
                patch.object(dyno, "get_heroku_logs", return_value=None):
            self.assertIsNone(dyno.memory_mb_for_observation)
            self.assertIsNone(dyno.load_1min_for_observation)

    def test_freshly_cached_legacy_value_cannot_supply_a_strict_observation(self):
        dyno = _make_identified_dyno()
        memory_key = (
            f"heroku:memory_total:{dyno.app_name}:{dyno.dyno_name}:{SELF_DYNO_ID}"
        )
        load_key = (
            f"heroku:load_avg_1m:{dyno.app_name}:{dyno.dyno_name}:{SELF_DYNO_ID}"
        )
        cache.set(memory_key, 120, timeout=300)
        cache.set(load_key, 0.1, timeout=300)

        with patch.dict(os.environ, {"DYNO": dyno.dyno_name}), \
                patch.object(dyno, "get_heroku_logs", return_value=None):
            self.assertIsNone(dyno.memory_mb_for_observation)
            self.assertIsNone(dyno.load_1min_for_observation)

    def test_old_matching_log_line_is_rejected(self):
        dyno = _make_identified_dyno()
        old_timestamp = timezone.now() - timedelta(hours=12)
        logs = [_metric_log(
            dyno.dyno_name, SELF_DYNO_ID,
            "sample#memory_total=120.0MB sample#load_avg_1m=0.1",
            old_timestamp,
        )]

        with patch.dict(os.environ, {"DYNO": dyno.dyno_name}), \
                patch.object(dyno, "get_heroku_logs", return_value=logs):
            self.assertIsNone(dyno.memory_mb_for_observation)
            self.assertIsNone(dyno.load_1min_for_observation)

    def test_fresh_source_sample_is_cached_with_its_timestamp(self):
        dyno = _make_identified_dyno()
        sample_time = timezone.now() - timedelta(seconds=10)
        logs = [_metric_log(
            dyno.dyno_name, SELF_DYNO_ID,
            "sample#memory_total=512.0MB sample#load_avg_1m=0.25",
            sample_time,
        )]

        with patch.dict(os.environ, {"DYNO": dyno.dyno_name}), \
                patch.object(dyno, "get_heroku_logs", return_value=logs):
            self.assertEqual(dyno.memory_mb_for_observation, 512)
            self.assertEqual(dyno.load_1min_for_observation, 0.25)

        memory_key = (
            f"heroku:memory_total:{dyno.app_name}:{dyno.dyno_name}:{SELF_DYNO_ID}"
            ":source-timestamped:v1"
        )
        load_key = (
            f"heroku:load_avg_1m:{dyno.app_name}:{dyno.dyno_name}:{SELF_DYNO_ID}"
            ":source-timestamped:v1"
        )
        self.assertEqual(cache.get(memory_key)[0], 512)
        self.assertAlmostEqual(cache.get(load_key)[0], 0.25)
        self.assertAlmostEqual(cache.get(memory_key)[1], sample_time.timestamp())

        with patch.dict(os.environ, {"DYNO": dyno.dyno_name}), \
                patch.object(dyno, "get_heroku_logs", return_value=None):
            self.assertEqual(dyno.memory_mb_for_observation, 512)
            self.assertEqual(dyno.load_1min_for_observation, 0.25)

    def test_same_name_resize_ignores_the_old_incarnation_cache_and_history(self):
        dyno = _make_identified_dyno()
        sample_time = timezone.now() - timedelta(seconds=10)
        old_memory_key = (
            f"heroku:memory_total:{dyno.app_name}:{dyno.dyno_name}:{OTHER_DYNO_ID}"
        )
        old_load_key = (
            f"heroku:load_avg_1m:{dyno.app_name}:{dyno.dyno_name}:{OTHER_DYNO_ID}"
        )
        old_history_key = (
            f"heroku:memory_history:{dyno.app_name}:{dyno.dyno_name}:{OTHER_DYNO_ID}"
        )
        cache.set(old_memory_key, 738, timeout=300)
        cache.set(old_load_key, 0.4, timeout=300)
        cache.set(old_history_key, [(time.time(), 738)], timeout=300)
        logs = [
            _metric_log(
                dyno.dyno_name, OTHER_DYNO_ID,
                "sample#memory_total=738.0MB sample#load_avg_1m=0.4",
                sample_time - timedelta(seconds=1),
            ),
            _metric_log(
                dyno.dyno_name, SELF_DYNO_ID,
                "sample#memory_total=392.0MB sample#load_avg_1m=0.05",
                sample_time,
            ),
        ]

        with patch.dict(os.environ, {"DYNO": dyno.dyno_name}), \
                patch.object(dyno, "get_heroku_logs", return_value=logs):
            self.assertEqual(dyno.memory_mb_for_observation, 392)
            self.assertEqual(dyno.load_1min_for_observation, 0.05)

        with patch.dict(os.environ, {"DYNO": dyno.dyno_name}):
            self.assertTrue(dyno.memory_history_cache_key.endswith(SELF_DYNO_ID))
        self.assertNotEqual(dyno.memory_history_cache_key, old_history_key)

    def test_strict_observation_rejects_missing_source_identity(self):
        dyno = _make_identified_dyno()
        sample_time = timezone.now() - timedelta(seconds=10)
        logs = [_metric_log(
            dyno.dyno_name, SELF_DYNO_ID,
            "sample#memory_total=512.0MB sample#load_avg_1m=0.25",
            sample_time,
            include_source=False,
        )]

        with patch.dict(os.environ, {"DYNO": dyno.dyno_name}), \
                patch.object(dyno, "get_heroku_logs", return_value=logs):
            self.assertIsNone(dyno.get_memory_usage_from_logs(require_fresh=True))
            self.assertIsNone(dyno.load_1min_for_observation)

    def test_real_local_memory_can_fill_a_missing_strict_log_sample(self):
        dyno = _make_identified_dyno()
        local_process = ModuleType("utils.process")
        local_process.get_total_memory_usage = lambda: 321

        with patch.dict(os.environ, {"DYNO": dyno.dyno_name}), \
                patch.dict(sys.modules, {"utils.process": local_process}), \
                patch.object(dyno, "get_heroku_logs", return_value=None):
            self.assertEqual(dyno.memory_mb_for_observation, 321)
            self.assertIsNone(dyno.load_1min_for_observation)

    def test_remote_monitoring_keeps_legacy_reads_and_rejects_strict_samples(self):
        dyno = _make_identified_dyno()
        sample_time = timezone.now() - timedelta(seconds=10)
        logs = [_metric_log(
            dyno.dyno_name, SELF_DYNO_ID,
            "sample#memory_total=512.0MB sample#load_avg_1m=0.25",
            sample_time,
        )]

        with patch.dict(os.environ, {"DYNO": "web.1"}), \
                patch.object(dyno, "get_heroku_logs", return_value=logs):
            self.assertTrue(dyno.remote_monitoring)
            self.assertEqual(dyno.get_memory_usage_from_logs(), 512)
            self.assertIsNone(dyno.get_memory_usage_from_logs(require_fresh=True))
            self.assertIsNone(dyno.load_1min_for_observation)


class TestMemoryHistoryAndOwnership(unittest.TestCase):

    def setUp(self):
        cache.clear()
        self._env_patch = patch.dict(os.environ, {"DYNO": "normal_worker.1"})
        self._env_patch.start()

    def tearDown(self):
        self._env_patch.stop()
        cache.clear()

    def test_repeated_source_epoch_cannot_form_a_plateau(self):
        dyno = make_dyno("normal_worker.1", formation_size="performance-l-ram")
        source_epoch = time.time()
        stable = False
        for _ in range(8):
            stable = dyno.record_memory_reading(12000, source_epoch=source_epoch)
        self.assertFalse(stable)
        self.assertEqual(cache.get(dyno.memory_history_cache_key), [(source_epoch, 12000.0)])

    def test_distinct_source_epochs_can_form_a_plateau(self):
        dyno = make_dyno("normal_worker.1", formation_size="performance-l-ram")
        now = time.time()
        stable = False
        for offset in (150, 120, 90, 60, 30, 0):
            stable = dyno.record_memory_reading(
                12000, source_epoch=now - offset,
            )
        self.assertTrue(stable)
        self.assertEqual(len(cache.get(dyno.memory_history_cache_key)), 6)

    def test_out_of_order_source_epoch_is_rejected(self):
        dyno = make_dyno("normal_worker.1", formation_size="performance-l-ram")
        now = time.time()
        cache.set(
            dyno.memory_history_cache_key,
            [(now - 60, 12000), (now, 12000)],
            timeout=600,
        )
        self.assertFalse(dyno.record_memory_reading(999, source_epoch=now - 30))
        self.assertEqual(
            cache.get(dyno.memory_history_cache_key),
            [(now - 60, 12000.0), (now, 12000.0)],
        )

    def test_existing_older_epoch_is_still_rejected(self):
        dyno = make_dyno("normal_worker.1", formation_size="performance-l-ram")
        now = time.time()
        history = [(now - offset, 12000) for offset in (150, 120, 90, 60, 30, 0)]
        cache.set(dyno.memory_history_cache_key, history, timeout=600)
        self.assertFalse(dyno.record_memory_reading(999, source_epoch=now - 30))
        self.assertEqual(
            cache.get(dyno.memory_history_cache_key),
            [(now - offset, 12000.0) for offset in (150, 120, 90, 60, 30, 0)],
        )

    def test_source_epoch_outside_the_window_is_rejected(self):
        dyno = make_dyno("normal_worker.1", formation_size="performance-l-ram")
        now = time.time()
        history = [(now - offset, 12000) for offset in (150, 120, 90, 60, 30, 0)]
        cache.set(dyno.memory_history_cache_key, history, timeout=600)
        self.assertFalse(dyno.record_memory_reading(999, source_epoch=now - 400))
        self.assertEqual(
            cache.get(dyno.memory_history_cache_key),
            [(now - offset, 12000.0) for offset in (150, 120, 90, 60, 30, 0)],
        )

    def test_replacing_a_value_at_the_same_epoch_recomputes_stability(self):
        dyno = make_dyno("normal_worker.1", formation_size="performance-l-ram")
        now = time.time()
        history = [(now - offset, 12000) for offset in (150, 120, 90, 60, 30, 0)]
        cache.set(dyno.memory_history_cache_key, history, timeout=600)
        self.assertFalse(dyno.record_memory_reading(20000, source_epoch=now))
        updated = cache.get(dyno.memory_history_cache_key)
        self.assertEqual(len(updated), 6)
        self.assertEqual(updated[-1], (now, 20000.0))

    def test_strict_metric_hit_does_not_require_a_ttl_read(self):
        dyno = _make_identified_dyno()
        now = time.time()
        key = (
            f"heroku:memory_total:{dyno.app_name}:{dyno.dyno_name}:{SELF_DYNO_ID}"
            ":source-timestamped:v1"
        )
        cache.set(key, (512, now), timeout=300)
        with patch.dict(os.environ, {"DYNO": dyno.dyno_name}), \
                patch.object(heroku_module.cache, "ttl") as ttl:
            self.assertEqual(dyno.memory_mb_for_observation, 512)
        ttl.assert_not_called()

    def test_process_fallback_identity_is_stable_per_instance(self):
        first = make_dyno("normal_worker.1")
        second = make_dyno("normal_worker.1")
        self.assertIn(":process:", first.memory_history_cache_key)
        first_key = first.memory_history_cache_key
        self.assertEqual(
            first.memory_history_cache_key,
            first_key,
        )
        self.assertNotEqual(
            first.memory_history_cache_key,
            second.memory_history_cache_key,
        )

    def test_fork_resets_inherited_fence_before_claim(self):
        dyno = make_dyno("normal_worker.1")
        dyno.__dict__["_process_owner_token"] = (
            os.getpid() + 1,
            "process:inherited-parent",
        )
        dyno._dyno_owner_claimed = True
        dyno._dyno_owner_fenced = True
        with patch_index_backend({}), \
                patch.object(type(dyno), "memory_reading_for_observation",
                             new_callable=PropertyMock,
                             return_value=_MetricReading(512, time.time())), \
                patch.object(type(dyno), "load_1min_for_observation",
                             new_callable=PropertyMock, return_value=0.2):
            self.assertTrue(dyno.check_in_dyno())
        self.assertFalse(dyno._dyno_owner_fenced)
        self.assertTrue(dyno._dyno_owner_claimed)

    def test_remote_monitoring_does_not_claim_dyno_ownership(self):
        dyno = make_dyno("normal_worker.1")
        with patch.dict(os.environ, {"DYNO": "web.1"}), \
                patch_index_backend({}), \
                patch.object(_IndexAdapter, "claim_and_register") as claim:
            self.assertFalse(dyno.check_in_dyno())
        claim.assert_not_called()

    def test_remote_monitoring_cannot_remove_its_self_registration(self):
        dyno = make_dyno("normal_worker.1")
        with patch.dict(os.environ, {"DYNO": "web.1"}), \
                patch.object(_IndexAdapter, "delete_owned_dyno") as delete:
            self.assertFalse(dyno.remove_dyno_from_alive_cache())
        delete.assert_not_called()

    def test_ownership_lease_covers_three_check_in_intervals(self):
        from django.test import override_settings
        with override_settings(DYNO_AUTOSCALE_INTERVAL=30):
            self.assertEqual(_dyno_owner_ttl(), 90)
        with override_settings(DYNO_AUTOSCALE_INTERVAL=10):
            self.assertEqual(_dyno_owner_ttl(), 60)

    def test_replacement_wins_and_late_predecessor_write_and_cleanup_are_ignored(self):
        predecessor = make_dyno("normal_worker.1")
        predecessor.__dict__["_dyno_owner_token_override"] = "process:predecessor"
        with patch_index_backend({}), \
                patch.object(type(predecessor), "memory_reading_for_observation",
                             new_callable=PropertyMock,
                             return_value=_MetricReading(738, time.time())), \
                patch.object(type(predecessor), "load_1min_for_observation",
                             new_callable=PropertyMock, return_value=0.4):
            self.assertTrue(predecessor.check_in_dyno())
            self.assertTrue(predecessor.remove_dyno_from_alive_cache())

            replacement = make_dyno("normal_worker.1")
            replacement.__dict__["_dyno_owner_token_override"] = "process:replacement"
            with patch.object(type(replacement), "memory_reading_for_observation",
                              new_callable=PropertyMock,
                              return_value=_MetricReading(392, time.time())), \
                    patch.object(type(replacement), "load_1min_for_observation",
                                 new_callable=PropertyMock, return_value=0.05):
                self.assertTrue(replacement.check_in_dyno())

            observed = cache.get(_observation_key("floship", "normal_worker.1"))
            self.assertEqual(observed.memory_mb, 392)
            with patch.object(type(predecessor), "memory_reading_for_observation",
                              new_callable=PropertyMock,
                              return_value=_MetricReading(900, time.time())), \
                    patch.object(type(predecessor), "load_1min_for_observation",
                                 new_callable=PropertyMock, return_value=1.0):
                self.assertFalse(predecessor.check_in_dyno())
            self.assertTrue(predecessor._dyno_owner_fenced)
            self.assertFalse(predecessor.remove_dyno_from_alive_cache())

            observed = cache.get(_observation_key("floship", "normal_worker.1"))
            self.assertEqual(observed.memory_mb, 392)
            status, owner, _ = _IndexAdapter(
                "floship", primary=True,
            ).get_owner_and_score("normal_worker.1")
            self.assertEqual((status, owner), ("found", "process:replacement"))

    def test_publish_with_a_superseded_owner_writes_nothing(self):
        dyno = make_dyno("normal_worker.1")
        score = time.time()
        with patch_index_backend({}):
            adapter = _IndexAdapter(dyno.app_name, primary=True)
            self.assertEqual(
                adapter.claim_and_register(
                    dyno.dyno_name, "new-owner", score, timezone.now(), 60,
                ),
                "claimed",
            )
            self.assertEqual(
                adapter.publish_owned_values(
                    dyno.dyno_name,
                    "old-owner",
                    score,
                    [(_observation_key(dyno.app_name, dyno.dyno_name), (score, 999, False, 0.1), 60)],
                ),
                "owner_mismatch",
            )
            self.assertEqual(
                adapter.publish_owned_values(
                    dyno.dyno_name,
                    "new-owner",
                    score + 1,
                    [(_observation_key(dyno.app_name, dyno.dyno_name), (score + 1, 999, False, 0.1), 60)],
                ),
                "owner_mismatch",
            )
            self.assertIsNone(cache.get(_observation_key(dyno.app_name, dyno.dyno_name)))

    def test_an_expired_owner_cannot_be_reclaimed_without_a_restart(self):
        dyno = make_dyno("normal_worker.1")
        dyno.__dict__["_dyno_owner_token_override"] = "process:owner"
        with patch_index_backend({}), \
                patch.object(type(dyno), "memory_reading_for_observation",
                             new_callable=PropertyMock,
                             return_value=_MetricReading(512, time.time())), \
                patch.object(type(dyno), "load_1min_for_observation",
                             new_callable=PropertyMock, return_value=0.2):
            self.assertTrue(dyno.check_in_dyno())
            heroku_module.cache.delete(_dyno_owner_key(dyno.app_name, dyno.dyno_name))
            self.assertFalse(dyno.check_in_dyno())
        self.assertTrue(dyno._dyno_owner_fenced)

    def test_failed_check_in_skips_automatic_mutations_for_the_tick(self):
        dyno = make_dyno("normal_worker.1")

        def stop_after_tick(_delay):
            dyno._stop_autoscale_event.set()

        with patch.object(dyno, "_check_formation_on_startup"), \
                patch.object(dyno, "_claim_current_dyno", return_value=(False, None)), \
                patch.object(dyno, "check_for_sibling_zombie_dynos") as zombies, \
                patch.object(dyno, "autoscale") as scale, \
                patch("heroku_manager.heroku.time.sleep", side_effect=stop_after_tick):
            dyno._run_continuous()
        zombies.assert_not_called()
        scale.assert_not_called()

    def test_retained_veto_allows_only_current_owner_r15_upscale(self):
        for r15, owner_replaced in ((True, False), (False, False), (True, True)):
            with self.subTest(r15=r15, owner_replaced=owner_replaced):
                cache.clear()
                dyno = make_dyno()
                owner_key = _dyno_owner_key(dyno.app_name, dyno.dyno_name)
                veto_key = _dyno_memory_veto_key(dyno.app_name, dyno.dyno_name)
                observation_key = _observation_key(dyno.app_name, dyno.dyno_name)
                with patch_index_backend({}) as backend, \
                        patch.object(type(dyno), "memory_reading_for_observation",
                                     new_callable=PropertyMock,
                                     return_value=_reading(200, time.time() - 10)), \
                        patch.object(type(dyno), "load_1min_for_observation",
                                     new_callable=PropertyMock, return_value=0.2), \
                        patch.object(type(dyno), "current_memory_usage",
                                     new_callable=PropertyMock, return_value=200), \
                        patch.object(dyno, "_check_formation_on_startup"), \
                        patch.object(dyno, "check_for_sibling_zombie_dynos") as zombies, \
                        patch.object(dyno, "check_and_downscale_to_original_formation_size") as downscale, \
                        patch.object(dyno, "call_heroku_api", return_value=MagicMock(status_code=422)) as api, \
                        patch("heroku_manager.heroku.time.sleep",
                              side_effect=lambda _: dyno._stop_autoscale_event.set()):
                    self.assertTrue(dyno.check_in_dyno())
                    self.assertTrue(dyno.record_memory_pressure(700))
                    prior_observation = backend.get(observation_key)
                    veto = backend.get(veto_key)

                    def check_r15():
                        if owner_replaced:
                            backend.set(owner_key, "process:replacement", timeout=60)
                        return r15

                    with patch.object(type(dyno), "detected_r15", new_callable=PropertyMock,
                                      side_effect=check_r15) as r15_check:
                        dyno._run_continuous()

                    if r15 and not owner_replaced:
                        api.assert_called_once_with(
                            "PATCH", f"https://api.heroku.com/apps/{dyno.app_name}/formation/{dyno.formation_name}",
                            data={"size": "performance-m"},
                        )
                    else:
                        api.assert_not_called()
                    if owner_replaced:
                        r15_check.assert_called_once()
                        self.assertEqual(backend.get(owner_key), "process:replacement")
                    zombies.assert_not_called()
                    downscale.assert_not_called()
                    self.assertEqual(backend.get(veto_key), veto)
                    self.assertEqual(backend.get(observation_key), prior_observation)
                    self.assertIsNone(backend.get(dyno.upscaling_cache_key))
                    self.assertIsNone(backend.get(dyno.downscale_cache_key))


class TestStrictObservationReaders(unittest.TestCase):

    def setUp(self):
        cache.clear()

    def tearDown(self):
        cache.clear()

    def test_healthy_singleton_can_downscale(self):
        score = time.time()
        with _strict_dyno() as dyno, _strict_index(
            [[(dyno.dyno_name, score)]], [(score, 200, False, 0.2)],
        ):
            self.assertTrue(dyno.allow_downscale)

    def test_mixed_version_member_without_tuple_blocks_downscale(self):
        score = time.time()
        roster = [("normal_worker.1", score), ("normal_worker.2", score)]
        with _strict_dyno() as dyno, _strict_index(
            [roster], [(score, 200, False, 0.2), None],
        ):
            self.assertFalse(dyno.allow_downscale)
            self.assertTrue(dyno.any_sibling_still_high_memory)

    def test_same_member_score_change_during_validation_blocks(self):
        score = time.time()
        changed = score + 1
        with _strict_dyno() as dyno, _strict_index(
            [[(dyno.dyno_name, score)], [(dyno.dyno_name, changed)]],
            [(score, 200, False, 0.2)],
        ):
            self.assertFalse(dyno.allow_downscale)

    def test_roster_join_during_validation_blocks(self):
        score = time.time()
        sibling = ("normal_worker.2", score)
        with _strict_dyno() as dyno, _strict_index(
            [[(dyno.dyno_name, score)], [(dyno.dyno_name, score), sibling]],
            [(score, 200, False, 0.2)],
        ):
            self.assertFalse(dyno.allow_downscale)

    def test_roster_departure_during_validation_blocks(self):
        score = time.time()
        sibling = ("normal_worker.2", score)
        with _strict_dyno() as dyno, _strict_index(
            [[(dyno.dyno_name, score), sibling], [(dyno.dyno_name, score)]],
            [(score, 200, False, 0.2), (score, 200, False, 0.2)],
        ):
            self.assertFalse(dyno.allow_downscale)

    def test_member_expiry_during_validation_uses_a_fresh_cutoff(self):
        score = 100.0
        first_cutoff = 99.0
        second_cutoff = 101.0
        with _strict_dyno() as dyno:
            def roster_for_cutoff(_key, min_score, *_args, **_kwargs):
                cutoff = float(str(min_score).removeprefix("("))
                if cutoff < score:
                    return [(dyno.dyno_name, score)]
                return []

            with patch("heroku_manager.heroku._stale_cutoff",
                       side_effect=[first_cutoff, second_cutoff]), \
                    _strict_index(roster_for_cutoff, [(score, 200, False, 0.2)]) as client:
                self.assertFalse(dyno.allow_downscale)

        self.assertEqual(
            [
                call.args[1]
                for call in (
                    client.zrangebyscore.call_args_list
                    + client.pipeline_instance.zrangebyscore.call_args_list
                )
            ],
            ["(99.0", "(101.0"],
        )

    def test_degradation_change_before_acceptance_blocks(self):
        score = time.time()
        with _strict_dyno() as dyno, _strict_index(
            [[(dyno.dyno_name, score)]], [(score, 200, False, 0.2)],
        ), patch("heroku_manager.heroku._registry_degraded", side_effect=[False, True]):
            self.assertFalse(dyno.allow_downscale)

    def test_unstable_hot_sibling_blocks_but_stable_hot_sibling_does_not(self):
        score = time.time()
        roster = [("normal_worker.1", score), ("normal_worker.2", score)]
        with _strict_dyno() as dyno, _strict_index(
            [roster], [(score, 200, False, 0.2), (score, 700, False, 0.2)],
        ):
            self.assertFalse(dyno.allow_downscale)
        with _strict_dyno() as dyno, _strict_index(
            [roster], [(score, 200, False, 0.2), (score, 700, True, 0.2)],
        ):
            self.assertTrue(dyno.allow_downscale)

    def test_plateau_requires_load_from_the_same_observation_batch(self):
        score = time.time()
        roster = [("normal_worker.1", score), ("normal_worker.2", score)]
        with _strict_dyno(own_memory=15000, formation_size="performance-l-ram") as dyno:
            cache.set(dyno.original_size_cache_key, {"size": "standard-1x"}, timeout=None)
            with _strict_index(
                [roster], [(score, 15000, True, 0.2), (score, 200, True, None)],
            ):
                self.assertFalse(dyno.allow_downscale)

    def test_genuine_zero_load_still_permits_a_stable_downscale(self):
        score = time.time()
        with _strict_dyno(own_memory=15000, formation_size="performance-l-ram") as dyno:
            cache.set(dyno.original_size_cache_key, {"size": "standard-1x"}, timeout=None)
            with _strict_index(
                [[(dyno.dyno_name, score)]], [(score, 15000, True, 0.0)],
            ):
                self.assertTrue(dyno.allow_downscale)

    def test_missing_memory_blocks_both_downscale_paths(self):
        score = time.time()
        with _strict_dyno() as dyno, _strict_index(
            [[(dyno.dyno_name, score)]], [(score, None, False, 0.2)],
        ):
            self.assertFalse(dyno.allow_downscale)
            self.assertFalse(dyno.allow_downscale_on_shutdown)

    def test_missing_owner_blocks_strict_downscale(self):
        score = time.time()
        with _strict_dyno() as dyno, _strict_index(
            [[(dyno.dyno_name, score)]],
            [(score, 200, False, 0.2)],
            owners=False,
        ):
            self.assertFalse(dyno.allow_downscale)
            self.assertFalse(dyno.allow_downscale_on_shutdown)

    def test_owner_replacement_during_final_validation_blocks_downscale(self):
        score = time.time()
        with _strict_dyno() as dyno, _strict_index(
            [[(dyno.dyno_name, score)]],
            [(score, 200, False, 0.2)],
            final_owner="replacement-owner",
        ):
            self.assertFalse(dyno.allow_downscale)
            self.assertFalse(dyno.allow_downscale_on_shutdown)

    def test_contention_marker_blocks_strict_downscale_until_it_clears(self):
        score = time.time()
        with _strict_dyno() as dyno, _strict_index(
            [[(dyno.dyno_name, score)]],
            [(score, 200, False, 0.2)],
            final_contention="replacement-owner",
        ):
            self.assertFalse(dyno.allow_downscale)
        with _strict_dyno() as dyno, _strict_index(
            [[(dyno.dyno_name, score)]],
            [(score, 200, False, 0.2)],
        ):
            self.assertTrue(dyno.allow_downscale)

    def test_list_valued_markers_block_strict_downscale(self):
        score = time.time()
        for marker_field in ("contention", "veto"):
            with self.subTest(marker_field=marker_field):
                with _strict_dyno() as dyno, _strict_index(
                    [[(dyno.dyno_name, score)]],
                    [(score, 200, False, 0.2)],
                    **{marker_field: ["wrong-type"]},
                ):
                    self.assertFalse(dyno.allow_downscale)
                    self.assertFalse(dyno.allow_downscale_on_shutdown)

    def test_list_valued_markers_keep_distinct_final_statuses(self):
        score = time.time()
        cases = (
            ("final_contention", "contended"),
            ("final_veto", "memory_pressure"),
        )
        for marker_field, expected_status in cases:
            with self.subTest(marker_field=marker_field):
                with _strict_dyno() as dyno, _strict_index(
                    [[(dyno.dyno_name, score)]],
                    [(score, 200, False, 0.2)],
                    **{marker_field: ["wrong-type"]},
                ):
                    snapshot = dyno._formation_observation_snapshot()
                    self.assertIsNotNone(snapshot)
                    adapter = _IndexAdapter(dyno.app_name, primary=True)
                    status, _, _, _ = adapter.read_formation_validation_state(
                        f"{dyno.formation_name}.", snapshot[2], snapshot[0],
                    )
                    self.assertEqual(status, expected_status)
                    reservation = adapter.reserve_downscale_if_current(
                        snapshot,
                        dyno.dyno_name,
                        "owner",
                        dyno.downscale_cache_key,
                        "attempt",
                        300,
                        formation_name=dyno.formation_name,
                        expected_formation_size=dyno.formation_size,
                        expected_target_size=dyno.previous_formation_size,
                    )
                    self.assertEqual(reservation, expected_status)

    def test_veto_precedes_degradation_at_both_boundaries(self):
        score = time.time()
        with _strict_dyno() as dyno, _strict_index(
            [[(dyno.dyno_name, score)]],
            [(score, 200, False, 0.2)],
            final_veto="wrong-type",
            final_degraded=True,
        ):
            snapshot = dyno._formation_observation_snapshot()
            self.assertIsNotNone(snapshot)
            adapter = _IndexAdapter(dyno.app_name, primary=True)
            validation = adapter.read_formation_validation_state(
                f"{dyno.formation_name}.", snapshot[2], snapshot[0],
            )
            reservation = adapter.reserve_downscale_if_current(
                snapshot,
                dyno.dyno_name,
                "owner",
                dyno.downscale_cache_key,
                "attempt",
                300,
                formation_name=dyno.formation_name,
                expected_formation_size=dyno.formation_size,
                expected_target_size=dyno.previous_formation_size,
            )
            self.assertEqual(validation[0], "memory_pressure")
            self.assertEqual(reservation, "memory_pressure")
            self.assertIsNone(cache.get(dyno.downscale_cache_key))

    def test_invalid_owner_batch_precedes_veto_at_both_boundaries(self):
        score = time.time()
        with _strict_dyno() as dyno, _strict_index(
            [[(dyno.dyno_name, score)]],
            [(score, 200, False, 0.2)],
            final_veto="wrong-type",
            final_owner_raw=b"serialized-owner-none",
        ):
            snapshot = dyno._formation_observation_snapshot()
            self.assertIsNotNone(snapshot)
            adapter = _IndexAdapter(dyno.app_name, primary=True)
            validation = adapter.read_formation_validation_state(
                f"{dyno.formation_name}.", snapshot[2], snapshot[0],
            )
            reservation = adapter.reserve_downscale_if_current(
                snapshot,
                dyno.dyno_name,
                "owner",
                dyno.downscale_cache_key,
                "attempt",
                300,
                formation_name=dyno.formation_name,
                expected_formation_size=dyno.formation_size,
                expected_target_size=dyno.previous_formation_size,
            )
            self.assertEqual(validation[0], "unavailable")
            self.assertEqual(reservation, "unavailable")
            self.assertIsNone(cache.get(dyno.downscale_cache_key))

    def test_wrong_type_owner_precedes_veto_at_both_boundaries(self):
        score = time.time()
        with _strict_dyno() as dyno, _strict_index(
            [[(dyno.dyno_name, score)]],
            [(score, 200, False, 0.2)],
            final_veto="wrong-type",
            final_owner_wrong_type=True,
        ) as client:
            snapshot = dyno._formation_observation_snapshot()
            self.assertIsNotNone(snapshot)
            adapter = _IndexAdapter(dyno.app_name, primary=True)
            validation = adapter.read_formation_validation_state(
                f"{dyno.formation_name}.", snapshot[2], snapshot[0],
            )
            reservation = adapter.reserve_downscale_if_current(
                snapshot,
                dyno.dyno_name,
                "owner",
                dyno.downscale_cache_key,
                "attempt",
                300,
                formation_name=dyno.formation_name,
                expected_formation_size=dyno.formation_size,
                expected_target_size=dyno.previous_formation_size,
            )
            self.assertEqual(validation[0], "unavailable")
            self.assertEqual(reservation, "unavailable")
            self.assertIsNone(cache.get(dyno.downscale_cache_key))
            owner_exists_calls = [
                call for call in client.pipeline_instance.exists.call_args_list
                if call.args
                and all("heroku:dyno_owner:v1:" in key for key in call.args)
            ]
            self.assertEqual(len(owner_exists_calls), 2)
            self.assertTrue(all(len(call.args) == 1 for call in owner_exists_calls))

    def test_owner_presence_check_is_one_batch_for_multiple_nil_slots(self):
        score = time.time()
        roster = [
            ("normal_worker.1", score),
            ("normal_worker.2", score),
            ("normal_worker.3", score),
        ]
        with _strict_dyno() as dyno, _strict_index(
            [roster],
            [
                (score, 200, False, 0.2),
                (score, 200, False, 0.2),
                (score, 200, False, 0.2),
            ],
            final_owner_wrong_type=True,
        ) as client:
            snapshot = dyno._formation_observation_snapshot()
            self.assertIsNotNone(snapshot)
            adapter = _IndexAdapter(dyno.app_name, primary=True)
            validation = adapter.read_formation_validation_state(
                f"{dyno.formation_name}.", snapshot[2], snapshot[0],
            )
            reservation = adapter.reserve_downscale_if_current(
                snapshot,
                dyno.dyno_name,
                "owner",
                dyno.downscale_cache_key,
                "attempt",
                300,
                formation_name=dyno.formation_name,
                expected_formation_size=dyno.formation_size,
                expected_target_size=dyno.previous_formation_size,
            )
            self.assertEqual(validation[0], "unavailable")
            self.assertEqual(reservation, "unavailable")
            owner_exists_calls = [
                call for call in client.pipeline_instance.exists.call_args_list
                if call.args
                and all("heroku:dyno_owner:v1:" in key for key in call.args)
            ]
            self.assertEqual(len(owner_exists_calls), 2)
            self.assertTrue(all(len(call.args) == 3 for call in owner_exists_calls))

    def test_valid_owner_batch_adds_no_presence_read(self):
        score = time.time()
        with _strict_dyno() as dyno, _strict_index(
            [[(dyno.dyno_name, score)]],
            [(score, 200, False, 0.2)],
        ) as client:
            snapshot = dyno._formation_observation_snapshot()
            self.assertIsNotNone(snapshot)
            adapter = _IndexAdapter(dyno.app_name, primary=True)
            validation = adapter.read_formation_validation_state(
                f"{dyno.formation_name}.", snapshot[2], snapshot[0],
            )
            reservation = adapter.reserve_downscale_if_current(
                snapshot,
                dyno.dyno_name,
                "owner",
                dyno.downscale_cache_key,
                "attempt",
                300,
                formation_name=dyno.formation_name,
                expected_formation_size=dyno.formation_size,
                expected_target_size=dyno.previous_formation_size,
            )
            self.assertEqual(validation[0], "ok")
            self.assertEqual(reservation, "reserved")
            owner_exists_calls = [
                call for call in client.pipeline_instance.exists.call_args_list
                if call.args
                and all("heroku:dyno_owner:v1:" in key for key in call.args)
            ]
            self.assertEqual(owner_exists_calls, [])

    def test_genuine_absent_owner_is_distinct_from_a_wrong_type_key(self):
        score = time.time()
        roster = ((dyno_name := "normal_worker.1", score),)
        snapshot = (roster, {}, score, {dyno_name: None})
        with _strict_dyno() as dyno, _strict_index(
            [roster], [(score, 200, False, 0.2)], owners=False,
        ) as client:
            pipeline = client.pipeline_instance
            multi_open = False
            owner_presence_after_multi = []
            original_exists = pipeline.exists.side_effect

            def watch(*keys):
                nonlocal multi_open
                multi_open = False
                return pipeline

            def multi():
                nonlocal multi_open
                multi_open = True
                return pipeline

            def exists(*keys):
                if any("heroku:dyno_owner:v1:" in key for key in keys):
                    owner_presence_after_multi.append(multi_open)
                return original_exists(*keys)

            pipeline.watch.side_effect = watch
            pipeline.multi.side_effect = multi
            pipeline.exists.side_effect = exists
            adapter = _IndexAdapter(dyno.app_name, primary=True)
            validation = adapter.read_formation_validation_state(
                f"{dyno.formation_name}.", score - 1, roster,
            )
            reservation = adapter.reserve_downscale_if_current(
                snapshot,
                dyno.dyno_name,
                "owner",
                dyno.downscale_cache_key,
                "attempt",
                300,
                formation_name=dyno.formation_name,
                expected_formation_size=dyno.formation_size,
                expected_target_size=dyno.previous_formation_size,
            )
            self.assertEqual(validation[0], "ok")
            self.assertEqual(reservation, "owner_changed")
            self.assertEqual(owner_presence_after_multi, [False, False])

        with _strict_dyno() as dyno, _strict_index(
            [roster],
            [(score, 200, False, 0.2)],
            owners=False,
            final_veto="wrong-type",
        ):
            adapter = _IndexAdapter(dyno.app_name, primary=True)
            validation = adapter.read_formation_validation_state(
                f"{dyno.formation_name}.", score - 1, roster,
            )
            reservation = adapter.reserve_downscale_if_current(
                snapshot,
                dyno.dyno_name,
                "owner",
                dyno.downscale_cache_key,
                "attempt",
                300,
                formation_name=dyno.formation_name,
                expected_formation_size=dyno.formation_size,
                expected_target_size=dyno.previous_formation_size,
            )
            self.assertEqual(validation[0], "memory_pressure")
            self.assertEqual(reservation, "memory_pressure")
            self.assertIsNone(cache.get(dyno.downscale_cache_key))

    def test_owner_presence_invalid_reply_fails_closed(self):
        adapter = _IndexAdapter("floship", primary=True)
        for invalid_reply in (True, -1, 2, "1"):
            with self.subTest(invalid_reply=invalid_reply):
                pipe = MagicMock()
                pipe.exists.return_value = invalid_reply
                with self.assertRaises(ValueError):
                    adapter._decode_watched_owner_batch(
                        pipe, ["owner-key"], [None],
                    )

    def test_registry_degradation_is_explicit_at_both_boundaries(self):
        score = time.time()
        with _strict_dyno() as dyno, _strict_index(
            [[(dyno.dyno_name, score)]],
            [(score, 200, False, 0.2)],
            final_degraded=True,
        ):
            snapshot = dyno._formation_observation_snapshot()
            self.assertIsNotNone(snapshot)
            adapter = _IndexAdapter(dyno.app_name, primary=True)
            validation = adapter.read_formation_validation_state(
                f"{dyno.formation_name}.", snapshot[2], snapshot[0],
            )
            reservation = adapter.reserve_downscale_if_current(
                snapshot,
                dyno.dyno_name,
                "owner",
                dyno.downscale_cache_key,
                "attempt",
                300,
                formation_name=dyno.formation_name,
                expected_formation_size=dyno.formation_size,
                expected_target_size=dyno.previous_formation_size,
            )
            self.assertEqual(validation[0], "degraded")
            self.assertEqual(validation[3], True)
            self.assertEqual(reservation, "degraded")
            self.assertIsNone(cache.get(dyno.downscale_cache_key))

    def test_final_watch_race_blocks_downscale(self):
        score = time.time()
        with _strict_dyno() as dyno, _strict_index(
            [[(dyno.dyno_name, score)]],
            [(score, 200, False, 0.2)],
            final_watch_error=True,
        ):
            self.assertFalse(dyno.allow_downscale)

    def test_near_expiry_normal_path_uses_one_observation_batch_before_roster_join(self):
        score = time.time()
        roster = [("normal_worker.1", score), ("normal_worker.2", score)]
        with _strict_dyno(own_memory=200, formation_size="performance-m") as dyno:
            cache.set(dyno.original_size_cache_key, {"size": "standard-1x"}, timeout=None)
            cache.set(
                dyno.upscale_until_cache_key,
                timezone.now() + timedelta(seconds=59),
                timeout=59,
            )
            with patch_index_backend({}), \
                    patch("heroku_manager.heroku._index_fail_closed", return_value=False), \
                    patch.object(type(dyno), "requires_upscale",
                                 new_callable=PropertyMock, return_value=False), \
                    patch.object(type(dyno), "downscale_on_non_empty_queue",
                                 new_callable=PropertyMock, return_value=True), \
                    patch.object(_IndexAdapter, "fresh_scored_members",
                                 return_value=roster), \
                    patch.object(_IndexAdapter, "mget",
                                 return_value=_owned_values([
                                     (score, 200, False, 0.2),
                                     (score, 200, False, 0.2),
                                 ])) as batch, \
                    patch.object(_IndexAdapter, "exists", return_value=0), \
                    patch.object(_IndexAdapter, "read_formation_validation_state",
                                 return_value=("changed", None, None, False)), \
                    patch.object(heroku_module.cache, "ttl", return_value=59), \
                    patch.object(dyno, "downscale_formation_to_original_size") as downscale:
                dyno.check_and_downscale_to_original_formation_size()
        batch.assert_called_once()
        downscale.assert_not_called()

    def test_near_expiry_stability_path_uses_one_observation_batch_before_roster_join(self):
        score = time.time()
        roster = [("normal_worker.1", score), ("normal_worker.2", score)]
        with _strict_dyno(own_memory=2000, formation_size="performance-m") as dyno:
            cache.set(dyno.original_size_cache_key, {"size": "standard-1x"}, timeout=None)
            cache.set(
                dyno.upscale_until_cache_key,
                timezone.now() + timedelta(seconds=59),
                timeout=59,
            )
            with patch_index_backend({}), \
                    patch("heroku_manager.heroku._index_fail_closed", return_value=False), \
                    patch.object(type(dyno), "requires_upscale",
                                 new_callable=PropertyMock, return_value=False), \
                    patch.object(type(dyno), "downscale_on_non_empty_queue",
                                 new_callable=PropertyMock, return_value=False), \
                    patch.object(type(dyno), "no_tasks_in_queue",
                                 new_callable=PropertyMock, return_value=False), \
                    patch.object(_IndexAdapter, "fresh_scored_members",
                                 return_value=roster), \
                    patch.object(_IndexAdapter, "mget",
                                 return_value=_owned_values([
                                     (score, 2000, True, 0.2),
                                     (score, 600, True, 0.3),
                                 ])) as batch, \
                    patch.object(_IndexAdapter, "exists", return_value=0), \
                    patch.object(_IndexAdapter, "read_formation_validation_state",
                                 return_value=("changed", None, None, False)), \
                    patch.object(heroku_module.cache, "ttl", return_value=59), \
                    patch.object(dyno, "downscale_formation_to_original_size") as downscale:
                dyno.check_and_downscale_to_original_formation_size()
        batch.assert_called_once()
        downscale.assert_not_called()


class TestAdvisoryMetricValidation(unittest.TestCase):

    def setUp(self):
        cache.clear()

    def tearDown(self):
        cache.clear()

    def test_oversized_value_does_not_hide_a_valid_hot_sibling(self):
        dyno = make_dyno("normal_worker.1", formation_size="standard-2x")
        backend = MagicMock()
        client = MagicMock()
        backend.client.get_client.return_value = client
        backend.client.decode.side_effect = lambda value: value
        backend.make_key.side_effect = lambda key: key
        backend.get.return_value = None
        client.decode.side_effect = lambda value: value
        client.zrangebyscore.return_value = [
            "normal_worker.1", "normal_worker.2", "normal_worker.3",
        ]
        client.mget.return_value = [10 ** 500, 1000]

        with patch("heroku_manager.heroku.cache", backend):
            self.assertTrue(dyno.any_sibling_requires_upscale)
