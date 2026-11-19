"""
Tests: upscale, downscale, restart, autoscale orchestration.
Uses patch context managers throughout to avoid PropertyMock class-level leakage.
"""
import math
import logging
import os
import pickle
import time
import unittest
from contextlib import contextmanager
from datetime import timedelta
from unittest.mock import patch, MagicMock, PropertyMock
from requests.exceptions import Timeout
from tests.conftest import make_dyno, BaseLockTestCase, patch_index_backend
from django.core.cache import cache
from django.test import override_settings
from django.utils import timezone

import heroku_manager.heroku as heroku_module
from heroku_manager.heroku import (
    _DownscaleDecision,
    _DynoObservation,
    _IndexAdapter,
    _dyno_owner_key,
    _registry_reconverging_key,
)
from heroku_manager.proxy import ProxyResponse


def _mock_response(status=200):
    resp = MagicMock()
    resp.status_code = status
    resp.text = "ok"
    return resp


def _primary_cache(value=None, error=None):
    """Return a fake django-redis client whose primary formation value is fixed."""
    backend = MagicMock()
    client = MagicMock()
    backend.get_client.return_value = client
    backend.decode.side_effect = lambda raw: raw.decode() if isinstance(raw, bytes) else raw
    if error is not None:
        client.get.side_effect = error
    else:
        client.get.return_value = value.encode() if value is not None else None
    return backend, client


def _seed_owned_dyno(dyno, dyno_name=None, owner_token=None):
    dyno_name = dyno_name or dyno.dyno_name
    owner_token = owner_token or dyno._dyno_owner_token
    score = time.time()
    status = _IndexAdapter(dyno.app_name, primary=True).claim_and_register(
        dyno_name, owner_token, score, timezone.now(), 300,
    )
    if status != "claimed":
        raise AssertionError(f"fixture ownership claim failed: {status}")
    if dyno_name == dyno.dyno_name:
        dyno._dyno_owner_claimed = True
        dyno._last_registered_score = score
    return owner_token, score


@contextmanager
def _owned(dyno):
    """Treat the process as the current owner at a direct mutation boundary."""
    with patch.object(
        _IndexAdapter,
        "get_owner_and_score",
        side_effect=lambda *_args, **_kwargs: (
            "found", dyno._dyno_owner_token, time.time(),
        ),
    ), patch.object(
        _IndexAdapter,
        "reserve_owned_flag",
        return_value="reserved",
    ):
        yield


@contextmanager
def _authorized_downscale(dyno, *, reservation_status="reserved"):
    """Keep legacy API-focused tests on the confirmed-reservation path."""
    score = time.time()
    snapshot = (
        ((dyno.dyno_name, score),),
        {dyno.dyno_name: _DynoObservation(score, 200, False, 0.2)},
        time.time(),
        {dyno.dyno_name: dyno._dyno_owner_token},
    )
    decision = _DownscaleDecision(
        True, False, snapshot, dyno.previous_formation_size,
    )

    def reserve_flag(
        _adapter, _snapshot, _dyno_name, _owner_token, flag_key, flag_value,
        timeout, **_kwargs,
    ):
        if reservation_status == "reserved":
            heroku_module.cache.set(flag_key, flag_value, timeout=timeout)
        return reservation_status

    with patch_index_backend({}), \
            patch.object(
                dyno, "_defer_resize_for_retiring_dyno", return_value=False,
            ), \
            patch.object(
                type(dyno),
                "remote_monitoring",
                new_callable=PropertyMock,
                return_value=False,
            ), \
            patch.object(
                dyno, "_evaluate_downscale_snapshot", return_value=decision,
            ), \
            patch.object(
                dyno,
                "_formation_observation_snapshot_current",
                return_value=True,
            ), \
            patch.object(
                _IndexAdapter,
                "reserve_downscale_if_current",
                autospec=True,
                side_effect=reserve_flag,
            ):
        yield decision


@contextmanager
def _rewrite_cleanup_state_at_lock_entry(dyno, initial_time):
    """Replace a same-tier baseline and timer immediately before the lock."""
    original_lock = heroku_module.cache.lock
    new_time = initial_time + timedelta(seconds=5)
    new_timer = initial_time + timedelta(seconds=90)

    @contextmanager
    def rewriting_lock(key, *args, **kwargs):
        cache.set(
            dyno.original_size_cache_key,
            {"size": dyno.formation_size, "time": new_time},
            timeout=None,
        )
        cache.set(dyno.upscale_until_cache_key, new_timer, timeout=120)
        with original_lock(key, *args, **kwargs) as value:
            yield value

    with patch.object(heroku_module.cache, "lock", new=rewriting_lock):
        yield new_time, new_timer


class _ReplacingOwnerLock:
    def __init__(self, dyno, owner):
        self.adapter = _IndexAdapter(dyno.app_name, primary=True)
        self.dyno = dyno
        self.owner = owner

    def __enter__(self):
        self.adapter._connect()
        heroku_module.cache.set(
            heroku_module.cache.make_key(
                _dyno_owner_key(self.dyno.app_name, self.dyno.dyno_name),
            ),
            self.adapter.encode(self.owner),
            timeout=60,
        )
        return self

    def __exit__(self, *_args):
        return False


class TestUpscaleFormation(BaseLockTestCase):


    def test_upscale_skipped_when_already_at_max(self):
        dyno = make_dyno(formation_size="performance-2xl")
        dyno.__dict__["max_dyno_size"] = "performance-2xl"
        with patch.object(dyno, "call_heroku_api") as mock_api:
            dyno.upscale_formation_to_next_level()
        mock_api.assert_not_called()

    def test_upscale_skipped_when_already_upscaling(self):
        dyno = make_dyno(formation_size="standard-2x")
        dyno.set_upscaling()
        with patch.object(dyno, "call_heroku_api") as mock_api:
            dyno.upscale_formation_to_next_level()
        mock_api.assert_not_called()

    def test_upscale_success_updates_formation_size(self):
        dyno = make_dyno(formation_size="standard-2x")
        with patch.object(type(dyno), "current_memory_usage_percentage",
                          new_callable=PropertyMock, return_value=90.0):
            with patch.object(type(dyno), "current_memory_usage",
                               new_callable=PropertyMock, return_value=922):
                with patch.object(type(dyno), "remote_monitoring",
                                   new_callable=PropertyMock, return_value=True):
                    with patch.object(dyno, "call_heroku_api", return_value=_mock_response(200)):
                        with patch.object(dyno, "stop_continuous_autoscale"):
                            dyno.upscale_formation_to_next_level()
        self.assertEqual(dyno._formation_size_cached[0], "performance-m")

    def test_upscale_api_failure_logs_error(self):
        dyno = make_dyno(formation_size="standard-2x")
        with patch.object(type(dyno), "current_memory_usage_percentage",
                          new_callable=PropertyMock, return_value=90.0):
            with patch.object(type(dyno), "current_memory_usage",
                               new_callable=PropertyMock, return_value=922):
                with patch.object(type(dyno), "remote_monitoring",
                                   new_callable=PropertyMock, return_value=True):
                    with patch.object(dyno, "call_heroku_api", return_value=_mock_response(500)):
                        with patch("heroku_manager.heroku.logger") as mock_log:
                            dyno.upscale_formation_to_next_level()
        mock_log.error.assert_called()

    def test_upscale_api_returns_none_logs_error(self):
        dyno = make_dyno(formation_size="standard-2x")
        with patch.object(type(dyno), "current_memory_usage_percentage",
                          new_callable=PropertyMock, return_value=90.0):
            with patch.object(type(dyno), "current_memory_usage",
                               new_callable=PropertyMock, return_value=922):
                with patch.object(type(dyno), "remote_monitoring",
                                   new_callable=PropertyMock, return_value=True):
                    with patch.object(dyno, "call_heroku_api", return_value=None):
                        with patch("heroku_manager.heroku.logger") as mock_log:
                            dyno.upscale_formation_to_next_level()
        mock_log.error.assert_called()


class TestDownscaleFormationToOriginalSize(BaseLockTestCase):


    def _assert_remote_carried_cleanup_preserves_pre_capture_rewrite(
        self, replacement_size,
    ):
        dyno = make_dyno(
            formation_size="standard-2x", index_ready_seed=False,
        )
        score = time.time()
        initial_time = timezone.now()
        initial_baseline = {
            "size": "standard-2x",
            "time": initial_time,
        }
        initial_timer = initial_time + timedelta(seconds=30)
        replacement_baseline = {
            "size": replacement_size,
            "time": initial_time + timedelta(seconds=5),
        }
        replacement_timer = initial_time + timedelta(seconds=120)
        snapshot = (
            ((dyno.dyno_name, score),),
            {dyno.dyno_name: _DynoObservation(score, 200, False, 0.2)},
            score,
            {dyno.dyno_name: dyno._dyno_owner_token},
        )
        decision = _DownscaleDecision(
            True, False, snapshot, "standard-2x",
        )
        original_capture = _IndexAdapter.capture_formation_cleanup_state
        state = {"fired": False, "started": None, "ttl": None}

        def rewrite_then_capture(adapter, formation_name):
            if not state["fired"]:
                state["fired"] = True
                cache.set(
                    dyno.original_size_cache_key,
                    replacement_baseline,
                    timeout=None,
                )
                cache.set(
                    dyno.upscale_until_cache_key,
                    replacement_timer,
                    timeout=120,
                )
                state["started"] = time.monotonic()
                state["ttl"] = cache.ttl(dyno.upscale_until_cache_key)
            return original_capture(adapter, formation_name)

        with patch_index_backend({}):
            cache.clear()
            cache.set(
                dyno.original_size_cache_key,
                initial_baseline,
                timeout=None,
            )
            cache.set(
                dyno.upscale_until_cache_key,
                initial_timer,
                timeout=60,
            )
            with patch.dict(os.environ, {"DYNO": "web.1"}), \
                    patch.object(
                        type(dyno),
                        "remote_monitoring",
                        new_callable=PropertyMock,
                        return_value=True,
                    ), \
                    patch.object(
                        dyno,
                        "_defer_resize_for_retiring_dyno",
                        return_value=False,
                    ), \
                    patch.object(
                        _IndexAdapter,
                        "capture_formation_cleanup_state",
                        new=rewrite_then_capture,
                    ), \
                    patch.object(
                        dyno,
                        "call_heroku_api",
                        return_value=_mock_response(200),
                    ) as api:
                dyno.downscale_formation_to_original_size(decision=decision)

        self.assertTrue(state["fired"])
        api.assert_not_called()
        self.assertIsNone(cache.get(dyno.downscale_cache_key))
        self.assertEqual(
            cache.get(dyno.original_size_cache_key),
            replacement_baseline,
        )
        self.assertEqual(
            cache.get(dyno.upscale_until_cache_key),
            replacement_timer,
        )
        elapsed = time.monotonic() - state["started"]
        remaining = cache.ttl(dyno.upscale_until_cache_key)
        self.assertGreaterEqual(remaining, state["ttl"] - elapsed - 2)
        self.assertLessEqual(remaining, state["ttl"] + 1)

    def test_remote_carried_decision_preserves_pre_capture_same_target_rewrite(
        self,
    ):
        self._assert_remote_carried_cleanup_preserves_pre_capture_rewrite(
            "standard-2x",
        )

    def test_remote_carried_decision_preserves_pre_capture_target_change(self):
        self._assert_remote_carried_cleanup_preserves_pre_capture_rewrite(
            "performance-m",
        )


    def _assert_no_decision_cleanup_preserves_raw_timer(
        self, raw_timer, *, remote,
    ):
        dyno = make_dyno(
            formation_size="performance-m", index_ready_seed=not remote,
        )
        baseline = {"size": "performance-m", "time": timezone.now()}
        with patch_index_backend({}) as backend:
            heroku_module.cache.clear()
            heroku_module.cache.set(
                dyno.original_size_cache_key, baseline, timeout=None,
            )
            timer_key = heroku_module.cache.make_key(
                dyno.upscale_until_cache_key,
            )
            backend._store[timer_key] = raw_timer

            with patch.object(
                        type(dyno),
                        "remote_monitoring",
                        new_callable=PropertyMock,
                        return_value=remote,
                    ), \
                    patch.object(
                        dyno,
                        "_defer_resize_for_retiring_dyno",
                        return_value=False,
                    ), \
                    patch.object(dyno, "call_heroku_api") as api:
                if remote:
                    with patch.dict(os.environ, {"DYNO": "web.1"}):
                        dyno.downscale_formation_to_original_size()
                else:
                    dyno.downscale_formation_to_original_size()

            self.assertEqual(cache.get(dyno.original_size_cache_key), baseline)
            self.assertEqual(backend._client.get(timer_key), raw_timer)
            self.assertIsNone(cache.get(dyno.downscale_cache_key))
            api.assert_not_called()

    def test_no_decision_cleanup_preserves_invalid_upscale_timers(self):
        for raw_timer in (pickle.dumps("not-a-timer"), pickle.dumps(None)):
            with self.subTest(raw_timer=raw_timer):
                self._assert_no_decision_cleanup_preserves_raw_timer(
                    raw_timer, remote=False,
                )

    def test_remote_cleanup_preserves_invalid_upscale_timers(self):
        for raw_timer in (pickle.dumps("not-a-timer"), pickle.dumps(None)):
            with self.subTest(raw_timer=raw_timer):
                self._assert_no_decision_cleanup_preserves_raw_timer(
                    raw_timer, remote=True,
                )

    def test_no_decision_cleanup_allows_a_missing_timer(self):
        for remote in (False, True):
            with self.subTest(remote=remote):
                dyno = make_dyno(
                    formation_size="performance-m",
                    index_ready_seed=not remote,
                )
                baseline = {"size": "performance-m", "time": timezone.now()}
                with patch_index_backend({}):
                    heroku_module.cache.clear()
                    heroku_module.cache.set(
                        dyno.original_size_cache_key, baseline, timeout=None,
                    )
                    dyno_env = "web.1" if remote else dyno.dyno_name
                    with patch.dict(os.environ, {"DYNO": dyno_env}), \
                            patch.object(
                                type(dyno),
                                "remote_monitoring",
                                new_callable=PropertyMock,
                                return_value=remote,
                            ), \
                            patch.object(
                                dyno,
                                "_defer_resize_for_retiring_dyno",
                                return_value=False,
                            ), \
                            patch.object(dyno, "call_heroku_api") as api:
                        dyno.downscale_formation_to_original_size()

                    self.assertIsNone(
                        cache.get(dyno.original_size_cache_key),
                    )
                    self.assertIsNone(
                        cache.get(dyno.upscale_until_cache_key),
                    )
                    self.assertIsNone(cache.get(dyno.downscale_cache_key))
                    api.assert_not_called()

    def test_skips_when_no_previous_formation_size(self):
        dyno = make_dyno(formation_size="standard-1x")
        with patch.object(dyno, "call_heroku_api") as mock_api:
            dyno.downscale_formation_to_original_size()
        mock_api.assert_not_called()

    def test_falls_back_to_previous_tier_when_no_original_formation_size(self):
        dyno = make_dyno(formation_size="performance-m")
        with _authorized_downscale(dyno), \
                patch.object(dyno, "call_heroku_api", return_value=_mock_response(200)) as mock_api:
            dyno.downscale_formation_to_original_size()
        mock_api.assert_called_once()
        call_args = mock_api.call_args
        self.assertEqual(call_args[1]["data"]["size"], "standard-2x")

    def test_skips_when_already_at_original_size(self):
        dyno = make_dyno(formation_size="standard-2x")
        dyno.set_original_formation_size()
        with patch.object(dyno, "call_heroku_api") as mock_api:
            dyno.downscale_formation_to_original_size()
        mock_api.assert_not_called()

    def test_successful_downscale_updates_formation(self):
        dyno = make_dyno(formation_size="performance-m")
        cache.set(dyno.original_size_cache_key, {"size": "standard-2x"}, timeout=None)
        with _authorized_downscale(dyno), \
                patch.object(dyno, "call_heroku_api", return_value=_mock_response(200)):
            with patch.object(dyno, "stop_continuous_autoscale"):
                dyno.downscale_formation_to_original_size()
        self.assertEqual(dyno._formation_size_cached[0], "standard-2x")

    def test_skips_when_already_downscaling(self):
        dyno = make_dyno(formation_size="performance-m")
        cache.set(dyno.original_size_cache_key, {"size": "standard-2x"}, timeout=None)
        dyno.set_downscaling()
        with _authorized_downscale(
                    dyno, reservation_status="flag_present",
                ), patch.object(dyno, "call_heroku_api") as mock_api:
            dyno.downscale_formation_to_original_size()
        mock_api.assert_not_called()

    def test_downscale_api_failure_logs_error(self):
        dyno = make_dyno(formation_size="performance-m")
        cache.set(dyno.original_size_cache_key, {"size": "standard-2x"}, timeout=None)
        with _authorized_downscale(dyno), \
                patch.object(dyno, "call_heroku_api", return_value=_mock_response(500)):
            with patch("heroku_manager.heroku.logger") as mock_log:
                dyno.downscale_formation_to_original_size()
        mock_log.error.assert_called()

    def test_downscale_alerts_once_for_an_uncertain_proxy_request(self):
        from requests.exceptions import ReadTimeout

        dyno = make_dyno(formation_size="standard-2x")
        cache.set(dyno.original_size_cache_key, {"size": "standard-1x"}, timeout=None)
        with _authorized_downscale(dyno), \
                patch("heroku_manager.proxy.requests.Session.send", side_effect=ReadTimeout()) as send, \
                self.assertLogs("heroku_manager.heroku", level="ERROR") as first_attempt:
            dyno.downscale_formation_to_original_size()

        cache.delete(dyno.downscale_cache_key)
        with _authorized_downscale(dyno), \
                patch("heroku_manager.proxy.requests.Session.send") as retry, \
                patch("heroku_manager.heroku.logger") as logger:
            dyno.downscale_formation_to_original_size()

        send.assert_called_once()
        retry.assert_not_called()
        self.assertIn("status=502 error=proxy_outcome_unknown", first_attempt.output[0])
        logger.error.assert_not_called()
        self.assertEqual(dyno.formation_size, "standard-2x")

    def test_downscale_reports_an_actual_rate_limit(self):
        from requests import Response

        dyno = make_dyno(formation_size="standard-2x")
        dyno.set_original_formation_size("standard-1x")
        response = Response()
        response.status_code = 429
        response._content = b'{"id":"rate_limit","message":"provider details"}'
        with _authorized_downscale(dyno), \
                patch("heroku_manager.proxy.requests.Session.send", return_value=response), \
                self.assertLogs("heroku_manager.heroku", level="ERROR") as failure:
            dyno.downscale_formation_to_original_size()

        self.assertIn("HTTP 429, error=rate_limit", failure.output[0])
        self.assertNotIn("provider details", failure.output[0])

    def test_no_decision_cleanup_preserves_a_same_tier_rewrite(self):
        dyno = make_dyno(formation_size="performance-m")
        initial_time = timezone.now()
        cache.set(
            dyno.original_size_cache_key,
            {"size": "performance-m", "time": initial_time},
            timeout=None,
        )
        cache.set(
            dyno.upscale_until_cache_key,
            initial_time + timedelta(seconds=30),
            timeout=60,
        )

        with patch_index_backend({}):
            with _rewrite_cleanup_state_at_lock_entry(
                        dyno, initial_time,
                    ) as (new_time, new_timer), \
                    patch.object(
                        type(dyno),
                        "remote_monitoring",
                        new_callable=PropertyMock,
                        return_value=False,
                    ), \
                    patch.object(
                        dyno,
                        "_defer_resize_for_retiring_dyno",
                        return_value=False,
                    ), \
                    patch.object(dyno, "call_heroku_api") as api:
                dyno.downscale_formation_to_original_size()

            self.assertIsNotNone(cache.get(dyno.original_size_cache_key))
            self.assertEqual(
                cache.get(dyno.original_size_cache_key)["time"],
                new_time,
            )
            self.assertEqual(
                cache.get(dyno.upscale_until_cache_key),
                new_timer,
            )
            self.assertIsNone(cache.get(dyno.downscale_cache_key))
            api.assert_not_called()

    def test_remote_cleanup_preserves_a_same_tier_rewrite_at_lock_entry(self):
        dyno = make_dyno(
            formation_size="performance-m", index_ready_seed=False,
        )
        initial_time = timezone.now()
        cache.set(
            dyno.original_size_cache_key,
            {"size": "performance-m", "time": initial_time},
            timeout=None,
        )
        cache.set(
            dyno.upscale_until_cache_key,
            initial_time + timedelta(seconds=30),
            timeout=60,
        )

        with patch_index_backend({}), \
                patch.dict(os.environ, {"DYNO": "web.1"}), \
                _rewrite_cleanup_state_at_lock_entry(
                    dyno, initial_time,
                ) as (new_time, new_timer), \
                patch.object(
                    dyno,
                    "_defer_resize_for_retiring_dyno",
                    return_value=False,
                ), \
                patch.object(dyno, "call_heroku_api") as api:
            dyno.downscale_formation_to_original_size()

        self.assertIsNotNone(cache.get(dyno.original_size_cache_key))
        self.assertEqual(
            cache.get(dyno.original_size_cache_key)["time"],
            new_time,
        )
        self.assertEqual(cache.get(dyno.upscale_until_cache_key), new_timer)
        self.assertIsNone(cache.get(dyno.downscale_cache_key))
        api.assert_not_called()

    def test_remote_carried_decision_preserves_a_same_tier_rewrite(self):
        dyno = make_dyno(
            formation_size="performance-m", index_ready_seed=False,
        )
        initial_time = timezone.now()
        cache.set(
            dyno.original_size_cache_key,
            {"size": "performance-m", "time": initial_time},
            timeout=None,
        )
        cache.set(
            dyno.upscale_until_cache_key,
            initial_time + timedelta(seconds=30),
            timeout=60,
        )
        score = time.time()
        snapshot = (
            ((dyno.dyno_name, score),),
            {dyno.dyno_name: _DynoObservation(score, 200, False, 0.2)},
            score,
            {dyno.dyno_name: dyno._dyno_owner_token},
        )
        decision = _DownscaleDecision(
            True, False, snapshot, "performance-m",
        )

        with patch_index_backend({}), \
                patch.dict(os.environ, {"DYNO": "web.1"}), \
                _rewrite_cleanup_state_at_lock_entry(
                    dyno, initial_time,
                ) as (new_time, new_timer), \
                patch.object(
                    dyno,
                    "_defer_resize_for_retiring_dyno",
                    return_value=False,
                ), \
                patch.object(dyno, "call_heroku_api") as api:
            dyno.downscale_formation_to_original_size(decision=decision)

        self.assertIsNotNone(cache.get(dyno.original_size_cache_key))
        self.assertEqual(
            cache.get(dyno.original_size_cache_key)["time"],
            new_time,
        )
        self.assertEqual(cache.get(dyno.upscale_until_cache_key), new_timer)
        self.assertIsNone(cache.get(dyno.downscale_cache_key))
        api.assert_not_called()

    def test_wrong_type_tier_cleanup_fails_closed(self):
        from redis.exceptions import ResponseError

        dyno = make_dyno(formation_size="performance-m")
        baseline = {"size": "performance-m", "time": timezone.now()}
        timer = baseline["time"] + timedelta(seconds=90)
        cache.set(dyno.original_size_cache_key, baseline, timeout=None)
        cache.set(dyno.upscale_until_cache_key, timer, timeout=120)

        with patch_index_backend({}) as backend:
            tier_key = heroku_module.cache.make_key(
                f"heroku:formation_size:{dyno.app_name}:{dyno.formation_name}",
            )
            original_get = backend._client.get
            original_mget = backend._client.mget

            def wrong_type_get(key):
                if key == tier_key:
                    raise ResponseError("WRONGTYPE Operation against a key")
                return original_get(key)

            def wrong_type_mget(keys):
                # Redis MGET reports nil for a wrong-type member.  Model the
                # predecessor behavior so this case bites if GET is removed.
                values = original_mget(keys)
                return [
                    None if key == tier_key else value
                    for key, value in zip(keys, values)
                ]

            backend._client.get = wrong_type_get
            backend._client.mget = wrong_type_mget
            with patch.object(
                        type(dyno),
                        "remote_monitoring",
                        new_callable=PropertyMock,
                        return_value=False,
                    ), \
                    patch.object(
                        dyno,
                        "_defer_resize_for_retiring_dyno",
                        return_value=False,
                    ), \
                    patch.object(dyno, "call_heroku_api") as api:
                dyno.downscale_formation_to_original_size()

        self.assertEqual(cache.get(dyno.original_size_cache_key), baseline)
        self.assertEqual(cache.get(dyno.upscale_until_cache_key), timer)
        self.assertIsNone(cache.get(dyno.downscale_cache_key))
        api.assert_not_called()


class TestCheckAndDownscale(BaseLockTestCase):


    def test_calls_downscale_when_no_upscale_until_key_and_memory_is_cool(self):
        """When upscale_until expired AND memory is OK → downscale allowed."""
        dyno = make_dyno(formation_size="performance-m")
        cache.set(dyno.original_size_cache_key, {"size": "standard-2x"}, timeout=None)
        with _authorized_downscale(dyno) as decision, \
                patch.object(type(dyno), "current_memory_usage",
                             new_callable=PropertyMock, return_value=200), \
                patch.object(type(dyno), "current_memory_usage_percentage",
                             new_callable=PropertyMock, return_value=10.0), \
                patch.object(type(dyno), "detected_r14",
                             new_callable=PropertyMock, return_value=False), \
                patch.object(type(dyno), "detected_r15",
                             new_callable=PropertyMock, return_value=False), \
                patch.object(type(dyno), "requires_upscale",
                             new_callable=PropertyMock, return_value=False), \
                patch.object(type(dyno), "downscale_on_non_empty_queue",
                             new_callable=PropertyMock, return_value=True), \
                patch.object(dyno, "call_heroku_api") as api:
            with patch.object(dyno, "downscale_formation_to_original_size") as mock_down:
                dyno.check_and_downscale_to_original_formation_size()
        mock_down.assert_called_once()
        self.assertIs(mock_down.call_args.kwargs["decision"], decision)
        api.assert_not_called()

    # --- REGRESSION: FP downscale-while-hot (upscale_until expired path) ---

    def test_no_downscale_when_upscale_until_expired_but_memory_still_high(self):
        """
        Regression: upscale_until key expired while dyno still hot.
        Old code fell through to downscale_formation_to_original_size() unconditionally.
        Fixed code must check allow_downscale before downscaling on the expired-key path.
        """
        dyno = make_dyno(formation_size="performance-m")
        cache.set(dyno.original_size_cache_key, {"size": "standard-1x"}, timeout=None)
        # upscale_until key is intentionally absent (simulates TTL expiry)
        with patch.object(type(dyno), "allow_downscale",
                          new_callable=PropertyMock, return_value=False):
            with patch.object(dyno, "downscale_formation_to_original_size") as mock_down:
                dyno.check_and_downscale_to_original_formation_size()
        mock_down.assert_not_called()

    def test_threshold_uses_original_size_not_previous_size(self):
        """
        Regression: _downscale_memory_threshold was computed from previous_formation_size
        (Standard-2X = 1024 MB → threshold 1075 MB) instead of original_formation_size
        (Standard-1X = 512 MB → threshold 537 MB).
        Memory at 1034 MB passed the wrong threshold and triggered a downscale to
        Standard-1X, causing immediate R14.
        After the fix the threshold must reflect the *target* (original) size.
        """
        dyno = make_dyno(formation_size="performance-m")
        # original formation was Standard-1X; that is what we downscale back to
        cache.set(dyno.original_size_cache_key, {"size": "standard-1x"}, timeout=None)
        # Standard-1X has 512 MB; DOWNSCALE_PERCENTAGE_HIGH_MEM_USE = 105
        expected_threshold = 512 * 105 / 100  # 537.6 MB
        with patch_index_backend({}):
            self.assertAlmostEqual(
                dyno._downscale_memory_threshold,
                expected_threshold,
                places=1,
            )

    def test_no_early_downscale_when_not_safe_and_ttl_high(self):
        """When memory is still elevated and TTL is high, no downscale should occur."""
        dyno = make_dyno(formation_size="performance-m")
        from django.utils import timezone
        from django.core.cache.backends.locmem import LocMemCache
        until = timezone.now() + timezone.timedelta(seconds=600)
        cache.set(dyno.upscale_until_cache_key, until, timeout=600)
        with patch.object(LocMemCache, "ttl", return_value=600):
            with patch.object(type(dyno), "allow_downscale",
                              new_callable=PropertyMock, return_value=False):
                with patch.object(dyno, "downscale_formation_to_original_size") as mock_down:
                    dyno.check_and_downscale_to_original_formation_size()
        mock_down.assert_not_called()

    def test_no_early_downscale_when_safe_if_ttl_high(self):
        """Keep the formation upscaled until the final TTL window even if current memory is low."""
        dyno = make_dyno(formation_size="performance-m")
        from django.utils import timezone
        from django.core.cache.backends.locmem import LocMemCache
        cache.set(dyno.original_size_cache_key, {"size": "standard-2x"}, timeout=None)
        until = timezone.now() + timezone.timedelta(seconds=600)
        cache.set(dyno.upscale_until_cache_key, until, timeout=600)
        with patch.object(LocMemCache, "ttl", return_value=600):
            with patch.object(type(dyno), "allow_downscale",
                              new_callable=PropertyMock, return_value=True):
                with patch.object(dyno, "downscale_formation_to_original_size") as mock_down:
                    dyno.check_and_downscale_to_original_formation_size()
        mock_down.assert_not_called()

    def test_no_downscale_when_r15_present_near_ttl_expiry(self):
        """R15 must keep the formation upscaled even in the final TTL window."""
        dyno = make_dyno(formation_size="performance-m")
        from django.utils import timezone
        from django.core.cache.backends.locmem import LocMemCache
        cache.set(dyno.original_size_cache_key, {"size": "standard-2x"}, timeout=None)
        until = timezone.now() + timezone.timedelta(seconds=60)
        cache.set(dyno.upscale_until_cache_key, until, timeout=60)
        with patch.object(LocMemCache, "ttl", return_value=60):
            with patch.object(type(dyno), "allow_downscale",
                              new_callable=PropertyMock, return_value=False):
                with patch.object(type(dyno), "detected_r14",
                                  new_callable=PropertyMock, return_value=False):
                    with patch.object(type(dyno), "detected_r15",
                                      new_callable=PropertyMock, return_value=True):
                        with patch.object(type(dyno), "is_still_high_memory_usage_for_downscale",
                                          new_callable=PropertyMock, return_value=False):
                            with patch.object(type(dyno), "no_tasks_in_queue",
                                              new_callable=PropertyMock, return_value=True):
                                with patch.object(dyno, "downscale_formation_to_original_size") as mock_down:
                                    with patch.object(dyno, "restart_dyno") as mock_restart:
                                        dyno.check_and_downscale_to_original_formation_size()
        mock_down.assert_not_called()
        mock_restart.assert_not_called()

    def test_restarts_dyno_when_r14_and_high_memory_and_no_tasks(self):
        dyno = make_dyno(formation_size="performance-m")
        with patch.object(type(dyno), "allow_downscale",
                          new_callable=PropertyMock, return_value=False):
            with patch.object(type(dyno), "detected_r14",
                               new_callable=PropertyMock, return_value=True):
                with patch.object(type(dyno), "detected_r15",
                                   new_callable=PropertyMock, return_value=False):
                    with patch.object(type(dyno), "is_still_high_memory_usage_for_downscale",
                                       new_callable=PropertyMock, return_value=True):
                        with patch.object(type(dyno), "no_tasks_in_queue",
                                           new_callable=PropertyMock, return_value=True):
                            with patch.object(type(dyno), "current_memory_usage_percentage",
                                               new_callable=PropertyMock, return_value=110):
                                with patch.object(dyno, "restart_dyno") as mock_restart:
                                    with patch.object(dyno, "downscale_formation_to_original_size"):
                                        with patch_index_backend({}):
                                            dyno.check_and_downscale_to_original_formation_size()
        mock_restart.assert_called_once()


class TestRestartDyno(BaseLockTestCase):


    def test_skips_when_no_app_name(self):
        dyno = make_dyno()
        dyno.app_name = None
        with patch.object(dyno, "call_heroku_api") as mock_api:
            dyno.restart_dyno()
        mock_api.assert_not_called()

    def test_restart_success_stops_autoscale_for_self(self):
        dyno = make_dyno("normal_worker.1")
        with patch.object(dyno, "call_heroku_api", return_value=_mock_response(202)):
            with patch.object(dyno, "stop_continuous_autoscale") as mock_stop:
                dyno.restart_dyno()
        mock_stop.assert_called_once()

    def test_restart_other_dyno_removes_from_alive_cache(self):
        dyno = make_dyno("normal_worker.1")
        with patch_index_backend({}):
            _seed_owned_dyno(dyno, "normal_worker.2", owner_token="sibling-owner")
            heroku_module.cache.set("heroku:dyno_alive:normal_worker.2", "now", timeout=60)
            with patch.object(dyno, "call_heroku_api", return_value=_mock_response(202)):
                dyno.restart_dyno("normal_worker.2")
            self.assertIsNone(heroku_module.cache.get("heroku:dyno_alive:normal_worker.2"))

    def test_delayed_restart_response_leaves_a_replacement_owned(self):
        dyno = make_dyno("normal_worker.1")
        with patch_index_backend({}):
            old_owner, old_score = _seed_owned_dyno(
                dyno, "normal_worker.2", owner_token="old-owner",
            )

            def replace_during_dispatch(*args, **kwargs):
                adapter = _IndexAdapter(dyno.app_name, primary=True)
                keys = [
                    f"heroku:dyno_observation:v1:{dyno.app_name}:normal_worker.2",
                    "heroku:dyno_alive:normal_worker.2",
                    "heroku:dyno_memory:normal_worker.2",
                    "heroku:dyno_memory_stable:normal_worker.2",
                    "heroku:dyno_load:normal_worker.2",
                ]
                self.assertEqual(
                    adapter.delete_owned_dyno(
                        "normal_worker.2", old_owner, old_score, keys,
                    ),
                    "deleted",
                )
                replacement_score = old_score + 1
                self.assertEqual(
                    adapter.claim_and_register(
                        "normal_worker.2",
                        "replacement-owner",
                        replacement_score,
                        timezone.now(),
                        300,
                    ),
                    "claimed",
                )
                return _mock_response(202)

            with patch.object(dyno, "call_heroku_api", side_effect=replace_during_dispatch):
                dyno.restart_dyno("normal_worker.2")

            status, owner, _ = _IndexAdapter(
                dyno.app_name, primary=True,
            ).get_owner_and_score("normal_worker.2")
            self.assertEqual((status, owner), ("found", "replacement-owner"))

    def test_restart_deduplication_via_cache(self):
        dyno = make_dyno()
        cache.set(f"heroku:restart_dyno:{dyno.dyno_name}", True, timeout=300)
        with patch.object(dyno, "call_heroku_api") as mock_api:
            dyno.restart_dyno()
        mock_api.assert_not_called()

    def test_restart_api_failure_logs_error(self):
        dyno = make_dyno()
        with patch.object(dyno, "call_heroku_api", return_value=_mock_response(500)):
            with patch("heroku_manager.heroku.logger") as mock_log:
                dyno.restart_dyno()
        mock_log.error.assert_called()


class TestDownscaleFlagOwnership(BaseLockTestCase):

    def _run_failed_downscale(self, response):
        dyno = make_dyno("normal_worker.1", formation_size="performance-m")
        with patch_index_backend({}):
            heroku_module.cache.set(
                dyno.original_size_cache_key,
                {"size": "standard-1x"},
                timeout=None,
            )
            with _authorized_downscale(dyno), \
                    patch.object(dyno, "call_heroku_api", return_value=response):
                dyno.downscale_formation_to_original_size()
            return heroku_module.cache.get(dyno.downscale_cache_key)

    def test_definite_proxy_rejections_clear_the_attempt_flag(self):
        responses = [
            ProxyResponse(400, '{"id":"bad_request"}'),
            ProxyResponse(429, '{"id":"cooldown"}', headers={"retry-after": "30"}),
            ProxyResponse(429, '{"id":"cooldown"}', headers={"retry-after": "bad"}),
            ProxyResponse(429, '{"id":"rate_limit"}'),
        ]
        for response in responses:
            with self.subTest(response=response):
                self.assertIsNone(self._run_failed_downscale(response))

    def test_proxy_none_is_pre_dispatch_and_clears_the_flag(self):
        self.assertIsNone(self._run_failed_downscale(None))

    def test_reconciliation_responses_keep_the_flag(self):
        response = ProxyResponse(
            409,
            '{"id":"proxy_reconciliation_required"}',
            request_id="00000000-0000-4000-8000-000000000001",
        )
        response.reconciliation_required = True
        self.assertIsNotNone(self._run_failed_downscale(response))

    def test_all_direct_resize_failures_preserve_the_flag(self):
        for response, expected_present in (
            (_mock_response(400), True),
            (_mock_response(401), True),
            (_mock_response(408), True),
            (_mock_response(500), True),
            (None, True),
        ):
            with self.subTest(status=getattr(response, "status_code", None)):
                dyno = make_dyno("normal_worker.1", formation_size="performance-m")
                with patch_index_backend({}):
                    heroku_module.cache.set(
                        dyno.original_size_cache_key,
                        {"size": "standard-1x"},
                        timeout=None,
                    )
                    with _authorized_downscale(dyno), \
                            patch.object(type(dyno.proxy), "enabled",
                                         new_callable=PropertyMock, return_value=False), \
                            patch.object(dyno, "call_heroku_api", return_value=response):
                        dyno.downscale_formation_to_original_size()
                    present = heroku_module.cache.get(dyno.downscale_cache_key) is not None
                    self.assertEqual(present, expected_present)

    def test_direct_timeout_then_401_still_preserves_the_flag(self):
        dyno = make_dyno("normal_worker.1", formation_size="performance-m")
        with patch_index_backend({}):
            heroku_module.cache.set(
                dyno.original_size_cache_key,
                {"size": "standard-1x"},
                timeout=None,
            )
            with _authorized_downscale(dyno), \
                    patch.object(type(dyno.proxy), "enabled",
                                 new_callable=PropertyMock, return_value=False), \
                    patch("heroku_manager.heroku.requests.request",
                          side_effect=[Timeout("timed out"), _mock_response(401)]) as request, \
                    patch("heroku_manager.heroku.time.sleep"):
                dyno.downscale_formation_to_original_size()
        self.assertEqual(request.call_count, 2)
        self.assertIsNotNone(heroku_module.cache.get(dyno.downscale_cache_key))

    def test_late_clear_cannot_delete_a_replacement_flag(self):
        dyno = make_dyno("normal_worker.1")
        with patch_index_backend({}):
            first = dyno.set_downscaling()
            second = dyno.set_downscaling()
            self.assertFalse(dyno.clear_downscaling(first))
            self.assertEqual(heroku_module.cache.get(dyno.downscale_cache_key), second)


class TestDownscaleTransactionAuthorization(BaseLockTestCase):
    def _decision(self, dyno, roster, owners):
        score = time.time()
        observations = {
            member: _DynoObservation(score, 200, False, 0.2)
            for member, _ in roster
        }
        return _DownscaleDecision(
            True,
            False,
            (tuple(roster), observations, score, dict(owners)),
            dyno.previous_formation_size,
        )

    def _run_consumer_path(self, *, current_members, watch_error=False):
        from redis.exceptions import WatchError

        dyno = make_dyno(
            "normal_worker.1", formation_size="performance-m",
        )
        cache.set(
            dyno.original_size_cache_key, {"size": "standard-2x"}, timeout=None,
        )
        score = time.time()
        expected_roster = [(dyno.dyno_name, score)]
        expected_owners = {dyno.dyno_name: dyno._dyno_owner_token}
        decision = self._decision(dyno, expected_roster, expected_owners)
        current_roster = [(member, score) for member in current_members]

        client = MagicMock()
        client.decode.side_effect = lambda value: value
        client.encode.side_effect = lambda value: value
        pipeline = MagicMock()
        pipeline.watch.return_value = pipeline
        pipeline.multi.return_value = pipeline
        pipeline.zrangebyscore.return_value = current_roster
        pipeline.get.return_value = None
        pipeline.zrange.return_value = []

        def batched_values(keys):
            if all("dyno_owner" in str(key) for key in keys):
                return [expected_owners["normal_worker.1"]] * len(keys)
            raise AssertionError(f"unexpected key batch: {keys!r}")

        pipeline.mget.side_effect = batched_values
        pipeline.exists.return_value = 0
        if watch_error:
            aborted = []

            def execute_with_timer_change():
                if not aborted:
                    cache.set(
                        dyno.upscale_until_cache_key,
                        timezone.now() + timedelta(seconds=90),
                        timeout=120,
                    )
                    aborted.append(True)
                    raise WatchError("upscale timer changed before EXEC")
                return True

            pipeline.execute.side_effect = execute_with_timer_change
            pipeline.watch_events = aborted
        else:
            pipeline.execute.return_value = True
        client.pipeline.return_value = pipeline

        def connect(adapter):
            adapter._client = client
            adapter._decoder = client.decode
            adapter._encoder = client.encode
            adapter._index_key = cache.make_key(
                f"heroku:dynos:v1:{adapter.app_name}",
            )
            return client

        with patch("heroku_manager.heroku._index_fail_closed", return_value=False), \
                patch.dict(os.environ, {"DYNO": dyno.dyno_name}), \
                patch.object(type(dyno), "requires_upscale",
                             new_callable=PropertyMock, return_value=False), \
                patch.object(dyno, "_evaluate_downscale_snapshot",
                             return_value=decision), \
                patch.object(dyno, "_defer_resize_for_retiring_dyno", return_value=False), \
                patch.object(_IndexAdapter, "_connect", autospec=True, side_effect=connect), \
                patch.object(dyno, "call_heroku_api") as api:
            dyno.downscale_formation_if_allowed(on_shutdown=True)

        api.assert_not_called()
        self.assertIsNone(cache.get(dyno.downscale_cache_key))
        return pipeline, api

    def test_roster_join_at_lock_entry_blocks_the_consumer_patch(self):
        self._run_consumer_path(
            current_members=["normal_worker.1", "normal_worker.2"],
        )

    def test_owner_handoff_between_watch_and_exec_blocks_the_consumer_patch(self):
        self._run_consumer_path(
            current_members=["normal_worker.1"],
            watch_error=True,
        )

    def test_single_watch_abort_ends_consumer_invocation(self):
        pipeline, api = self._run_consumer_path(
            current_members=["normal_worker.1"],
            watch_error=True,
        )
        self.assertEqual(pipeline.execute.call_count, 1)
        self.assertEqual(pipeline.watch_events, [True])
        api.assert_not_called()

    def test_changed_baseline_target_blocks_the_approved_downscale(self):
        dyno = make_dyno(
            "normal_worker.1", formation_size="performance-m",
            index_ready_seed=False,
        )
        with patch_index_backend({}):
            dyno.set_original_formation_size("standard-2x")
            owner, score = _seed_owned_dyno(dyno)
            heroku_module.cache.delete(
                _registry_reconverging_key(dyno.app_name),
            )
            decision = _DownscaleDecision(
                True,
                False,
                (
                    ((dyno.dyno_name, score),),
                    {
                        dyno.dyno_name: _DynoObservation(
                            score, 200, False, 0.2,
                        ),
                    },
                    score,
                    {dyno.dyno_name: owner},
                ),
                "standard-2x",
            )
            original_lock = heroku_module.cache.lock

            @contextmanager
            def changing_lock(key, *args, **kwargs):
                heroku_module.cache.set(
                    dyno.original_size_cache_key,
                    {"size": "standard-1x", "time": timezone.now()},
                    timeout=None,
                )
                with original_lock(key, *args, **kwargs) as value:
                    yield value

            with patch.object(
                        type(dyno), "requires_upscale",
                        new_callable=PropertyMock, return_value=False,
                    ), \
                    patch.dict(os.environ, {"DYNO": dyno.dyno_name}), \
                    patch.object(
                        dyno, "_defer_resize_for_retiring_dyno",
                        return_value=False,
                    ), \
                    patch.object(heroku_module.cache, "lock", new=changing_lock), \
                    patch.object(dyno, "call_heroku_api") as api:
                dyno.downscale_formation_to_original_size(decision=decision)

            api.assert_not_called()
            self.assertIsNone(heroku_module.cache.get(dyno.downscale_cache_key))

    def test_invalid_present_baseline_cannot_reach_patch_or_reserve_a_flag(self):
        for raw_value in (pickle.dumps(None), b"not-a-pickle"):
            with self.subTest(raw_value=raw_value):
                dyno = make_dyno(
                    "normal_worker.1", formation_size="performance-m",
                    index_ready_seed=False,
                )
                with patch_index_backend({}):
                    heroku_module.cache.clear()
                    owner, score = _seed_owned_dyno(dyno)
                    heroku_module.cache.set(
                        dyno.original_size_cache_key, raw_value, timeout=None,
                    )
                    heroku_module.cache.delete(
                        _registry_reconverging_key(dyno.app_name),
                    )
                    decision = _DownscaleDecision(
                        True,
                        False,
                        (
                            ((dyno.dyno_name, score),),
                            {
                                dyno.dyno_name: _DynoObservation(
                                    score, 200, False, 0.2,
                                ),
                            },
                            score,
                            {dyno.dyno_name: owner},
                        ),
                        "standard-2x",
                    )
                    with patch.object(
                                type(dyno), "original_formation_size",
                                new_callable=PropertyMock,
                                return_value="standard-2x",
                            ), \
                            patch.object(
                                type(dyno), "requires_upscale",
                                new_callable=PropertyMock, return_value=False,
                            ), \
                            patch.dict(os.environ, {"DYNO": dyno.dyno_name}), \
                            patch.object(
                                dyno, "_defer_resize_for_retiring_dyno",
                                return_value=False,
                            ), \
                            patch.object(dyno, "call_heroku_api") as api:
                        dyno.downscale_formation_to_original_size(decision=decision)

                    api.assert_not_called()
                    self.assertIsNone(
                        heroku_module.cache.get(dyno.downscale_cache_key),
                    )

    def test_changed_baseline_at_current_tier_preserves_cleanup_keys(self):
        dyno = make_dyno(
            "normal_worker.1", formation_size="standard-2x",
            index_ready_seed=False,
        )
        with patch_index_backend({}):
            dyno.set_original_formation_size("standard-1x")
            owner, score = _seed_owned_dyno(dyno)
            heroku_module.cache.delete(
                _registry_reconverging_key(dyno.app_name),
            )
            decision = _DownscaleDecision(
                True,
                False,
                (
                    ((dyno.dyno_name, score),),
                    {
                        dyno.dyno_name: _DynoObservation(
                            score, 200, False, 0.2,
                        ),
                    },
                    score,
                    {dyno.dyno_name: owner},
                ),
                "standard-1x",
            )
            dyno.set_original_formation_size("standard-2x")
            heroku_module.cache.set(
                dyno.upscale_until_cache_key,
                timezone.now() + timezone.timedelta(seconds=120),
                timeout=300,
            )

            with patch.object(
                        type(dyno), "requires_upscale",
                        new_callable=PropertyMock, return_value=False,
                    ), \
                    patch.dict(os.environ, {"DYNO": dyno.dyno_name}), \
                    patch.object(
                        dyno, "_defer_resize_for_retiring_dyno",
                        return_value=False,
                    ), \
                    patch.object(dyno, "call_heroku_api") as api:
                dyno.downscale_formation_to_original_size(decision=decision)

            api.assert_not_called()
            self.assertEqual(dyno.original_formation_size, "standard-2x")
            self.assertIsNotNone(
                heroku_module.cache.get(dyno.upscale_until_cache_key),
            )
            self.assertIsNone(heroku_module.cache.get(dyno.downscale_cache_key))

    def test_invalid_upscale_timers_preserve_cleanup_keys(self):
        for raw_timer in (pickle.dumps("not-a-timer"), pickle.dumps(None)):
            with self.subTest(raw_timer=raw_timer):
                dyno = make_dyno(
                    "normal_worker.1", formation_size="performance-m",
                    index_ready_seed=False,
                )
                with patch_index_backend({}) as backend:
                    heroku_module.cache.clear()
                    dyno.set_original_formation_size("performance-m")
                    owner, score = _seed_owned_dyno(dyno)
                    heroku_module.cache.delete(
                        _registry_reconverging_key(dyno.app_name),
                    )
                    timer_key = heroku_module.cache.make_key(
                        dyno.upscale_until_cache_key,
                    )
                    backend._store[timer_key] = raw_timer
                    decision = _DownscaleDecision(
                        True,
                        False,
                        (
                            ((dyno.dyno_name, score),),
                            {
                                dyno.dyno_name: _DynoObservation(
                                    score, 200, False, 0.2,
                                ),
                            },
                            score,
                            {dyno.dyno_name: owner},
                        ),
                        "performance-m",
                    )

                    with patch.object(
                                type(dyno), "requires_upscale",
                                new_callable=PropertyMock, return_value=False,
                            ), \
                            patch.dict(os.environ, {"DYNO": dyno.dyno_name}), \
                            patch.object(
                                dyno, "_defer_resize_for_retiring_dyno",
                                return_value=False,
                            ), \
                            patch.object(dyno, "call_heroku_api") as api:
                        dyno.downscale_formation_to_original_size(
                            decision=decision,
                        )

                    api.assert_not_called()
                    self.assertEqual(
                        dyno.original_formation_size, "performance-m",
                    )
                    self.assertEqual(backend._client.get(timer_key), raw_timer)
                    self.assertIsNone(
                        heroku_module.cache.get(dyno.downscale_cache_key),
                    )

    def test_missing_upscale_timer_still_clears_cleanup_keys(self):
        dyno = make_dyno(
            "normal_worker.1", formation_size="performance-m",
            index_ready_seed=False,
        )
        with patch_index_backend({}):
            heroku_module.cache.clear()
            dyno.set_original_formation_size("performance-m")
            owner, score = _seed_owned_dyno(dyno)
            heroku_module.cache.delete(
                _registry_reconverging_key(dyno.app_name),
            )
            decision = _DownscaleDecision(
                True,
                False,
                (
                    ((dyno.dyno_name, score),),
                    {
                        dyno.dyno_name: _DynoObservation(
                            score, 200, False, 0.2,
                        ),
                    },
                    score,
                    {dyno.dyno_name: owner},
                ),
                "performance-m",
            )

            with patch.object(
                        type(dyno), "requires_upscale",
                        new_callable=PropertyMock, return_value=False,
                    ), \
                    patch.dict(os.environ, {"DYNO": dyno.dyno_name}), \
                    patch.object(
                        dyno, "_defer_resize_for_retiring_dyno",
                        return_value=False,
                    ), \
                    patch.object(dyno, "call_heroku_api") as api:
                dyno.downscale_formation_to_original_size(decision=decision)

            api.assert_not_called()
            self.assertIsNone(dyno.original_formation_size)
            self.assertIsNone(heroku_module.cache.get(dyno.upscale_until_cache_key))
            self.assertIsNone(
                heroku_module.cache.get(dyno.downscale_cache_key),
            )

    def test_unchanged_baseline_target_still_reserves_and_downsizes(self):
        dyno = make_dyno(
            "normal_worker.1", formation_size="performance-m",
            index_ready_seed=False,
        )
        with patch_index_backend({}):
            dyno.set_original_formation_size("standard-2x")
            owner, score = _seed_owned_dyno(dyno)
            heroku_module.cache.delete(
                _registry_reconverging_key(dyno.app_name),
            )
            decision = _DownscaleDecision(
                True,
                False,
                (
                    ((dyno.dyno_name, score),),
                    {
                        dyno.dyno_name: _DynoObservation(
                            score, 200, False, 0.2,
                        ),
                    },
                    score,
                    {dyno.dyno_name: owner},
                ),
                "standard-2x",
            )

            with patch.object(
                        type(dyno), "requires_upscale",
                        new_callable=PropertyMock, return_value=False,
                    ), \
                    patch.dict(os.environ, {"DYNO": dyno.dyno_name}), \
                    patch.object(
                        dyno, "_defer_resize_for_retiring_dyno",
                        return_value=False,
                    ), \
                    patch.object(
                        dyno, "call_heroku_api",
                        return_value=_mock_response(200),
                    ) as api:
                dyno.downscale_formation_to_original_size(decision=decision)

            api.assert_called_once()
            self.assertEqual(api.call_args.kwargs["data"]["size"], "standard-2x")


class TestRetiringDynoResizeGuard(BaseLockTestCase):

    def _stale_dyno(self):
        dyno = make_dyno("normal_worker.1", formation_size="standard-2x")
        dyno.dyno_id = "11111111-1111-4111-8111-111111111111"
        return dyno

    def test_fenced_same_tier_direct_upscale_never_reaches_patch(self):
        dyno = self._stale_dyno()
        dyno._dyno_owner_claimed = True
        dyno._dyno_owner_fenced = True

        with _owned(dyno), \
                patch.dict(os.environ, {"DYNO": dyno.dyno_name, "DYNO_RAM": "1024"}), \
                patch.object(cache, "lock") as lock, \
                patch.object(dyno, "set_upscaling") as set_flag, \
                patch.object(dyno, "call_heroku_api") as api:
            dyno.upscale_formation_to_next_level()

        lock.assert_not_called()
        set_flag.assert_not_called()
        api.assert_not_called()

    def test_missing_current_owner_blocks_same_tier_downscale(self):
        dyno = self._stale_dyno()
        cache.set(dyno.original_size_cache_key, {"size": "standard-1x"}, timeout=None)

        with patch.dict(os.environ, {"DYNO": dyno.dyno_name, "DYNO_RAM": "1024"}), \
                patch.object(_IndexAdapter, "get_owner_and_score",
                             return_value=("missing", None, None)), \
                patch.object(cache, "lock") as lock, \
                patch.object(dyno, "set_downscaling") as set_flag, \
                patch.object(dyno, "call_heroku_api") as api:
            dyno.downscale_formation_to_original_size()

        lock.assert_not_called()
        set_flag.assert_not_called()
        api.assert_not_called()

    def test_stale_direct_upscale_defers_before_lock_flag_or_patch(self):
        dyno = self._stale_dyno()
        backend, _ = _primary_cache("standard-1x")

        with _owned(dyno), \
                patch.dict(os.environ, {"DYNO": dyno.dyno_name, "DYNO_RAM": "1024"}), \
                patch.object(cache, "client", backend, create=True), \
                patch.object(type(dyno), "current_memory_usage_percentage",
                             new_callable=PropertyMock, return_value=90.0), \
                patch.object(type(dyno), "current_memory_usage",
                             new_callable=PropertyMock, return_value=922), \
                patch.object(cache, "lock") as lock, \
                patch.object(dyno, "set_upscaling") as set_flag, \
                patch.object(dyno, "call_heroku_api") as api, \
                patch("heroku_manager.heroku.logger") as logger:
            dyno.upscale_formation_to_next_level()

        lock.assert_not_called()
        set_flag.assert_not_called()
        api.assert_not_called()
        backend.get_client.assert_called_once_with(write=True)
        info = logger.info.call_args
        self.assertEqual(info.args[1], "upscale")
        self.assertEqual(info.args[4], "standard-2x")
        self.assertEqual(info.args[5], "primary formation size standard-1x")

    def test_stale_direct_downscale_defers_before_lock_flag_or_patch(self):
        dyno = self._stale_dyno()
        cache.set(dyno.original_size_cache_key, {"size": "standard-1x"}, timeout=None)
        backend, _ = _primary_cache("standard-1x")

        with _owned(dyno), \
                patch.dict(os.environ, {"DYNO": dyno.dyno_name, "DYNO_RAM": "1024"}), \
                patch.object(cache, "client", backend, create=True), \
                patch.object(cache, "lock") as lock, \
                patch.object(dyno, "set_downscaling") as set_flag, \
                patch.object(dyno, "call_heroku_api") as api, \
                patch("heroku_manager.heroku.logger") as logger:
            dyno.downscale_formation_to_original_size()

        lock.assert_not_called()
        set_flag.assert_not_called()
        api.assert_not_called()
        backend.get_client.assert_called_once_with(write=True)
        self.assertEqual(logger.info.call_args.args[1], "downscale")

    def test_shutdown_shaped_caller_uses_the_guarded_scale_methods(self):
        dyno = self._stale_dyno()
        backend, _ = _primary_cache("standard-1x")

        def shutdown_scale_calls():
            dyno.upscale_formation_to_next_level()
            dyno.downscale_formation_to_original_size()

        with _owned(dyno), \
                patch.dict(os.environ, {"DYNO": dyno.dyno_name, "DYNO_RAM": "1024"}), \
                patch.object(cache, "client", backend, create=True), \
                patch.object(type(dyno), "current_memory_usage_percentage",
                             new_callable=PropertyMock, return_value=90.0), \
                patch.object(type(dyno), "current_memory_usage",
                             new_callable=PropertyMock, return_value=922), \
                patch.object(cache, "lock") as lock, \
                patch.object(dyno, "set_upscaling") as set_upscale, \
                patch.object(dyno, "set_downscaling") as set_downscale, \
                patch.object(dyno, "call_heroku_api") as api:
            shutdown_scale_calls()

        lock.assert_not_called()
        set_upscale.assert_not_called()
        set_downscale.assert_not_called()
        api.assert_not_called()

    def test_primary_read_failure_defers_direct_upscale(self):
        dyno = self._stale_dyno()
        backend, _ = _primary_cache(error=RuntimeError("primary unavailable"))

        with _owned(dyno), \
                patch.dict(os.environ, {"DYNO": dyno.dyno_name, "DYNO_RAM": "1024"}), \
                patch.object(cache, "client", backend, create=True), \
                patch.object(type(dyno), "current_memory_usage_percentage",
                             new_callable=PropertyMock, return_value=90.0), \
                patch.object(type(dyno), "current_memory_usage",
                             new_callable=PropertyMock, return_value=922), \
                patch.object(cache, "lock") as lock, \
                patch.object(dyno, "set_upscaling") as set_flag, \
                patch.object(dyno, "call_heroku_api") as api, \
                patch("heroku_manager.heroku.logger") as logger:
            dyno.upscale_formation_to_next_level()

        lock.assert_not_called()
        set_flag.assert_not_called()
        api.assert_not_called()
        self.assertEqual(logger.warning.call_count, 1)
        self.assertIn("primary formation read unavailable", logger.info.call_args.args)

    def test_invalid_primary_value_defers_direct_upscale(self):
        dyno = self._stale_dyno()
        backend, _ = _primary_cache("unknown-size")

        with _owned(dyno), \
                patch.dict(os.environ, {"DYNO": dyno.dyno_name, "DYNO_RAM": "1024"}), \
                patch.object(cache, "client", backend, create=True), \
                patch.object(type(dyno), "current_memory_usage_percentage",
                             new_callable=PropertyMock, return_value=90.0), \
                patch.object(type(dyno), "current_memory_usage",
                             new_callable=PropertyMock, return_value=922), \
                patch.object(cache, "lock") as lock, \
                patch.object(dyno, "set_upscaling") as set_flag, \
                patch.object(dyno, "call_heroku_api") as api, \
                patch("heroku_manager.heroku.logger") as logger:
            dyno.upscale_formation_to_next_level()

        lock.assert_not_called()
        set_flag.assert_not_called()
        api.assert_not_called()
        logger.warning.assert_called_once()
        self.assertIn("primary formation read unavailable", logger.info.call_args.args)

    def test_undecodable_primary_value_defers_direct_upscale(self):
        dyno = self._stale_dyno()
        backend, _ = _primary_cache("standard-1x")
        backend.decode.side_effect = RuntimeError("decode failed")

        with _owned(dyno), \
                patch.dict(os.environ, {"DYNO": dyno.dyno_name, "DYNO_RAM": "1024"}), \
                patch.object(cache, "client", backend, create=True), \
                patch.object(type(dyno), "current_memory_usage_percentage",
                             new_callable=PropertyMock, return_value=90.0), \
                patch.object(type(dyno), "current_memory_usage",
                             new_callable=PropertyMock, return_value=922), \
                patch.object(cache, "lock") as lock, \
                patch.object(dyno, "set_upscaling") as set_flag, \
                patch.object(dyno, "call_heroku_api") as api, \
                patch("heroku_manager.heroku.logger") as logger:
            dyno.upscale_formation_to_next_level()

        lock.assert_not_called()
        set_flag.assert_not_called()
        api.assert_not_called()
        logger.warning.assert_called_once()
        self.assertIn("primary formation read unavailable", logger.info.call_args.args)

    def test_serialized_none_primary_value_defers_direct_upscale(self):
        dyno = self._stale_dyno()
        backend, _ = _primary_cache("standard-1x")
        backend.decode.side_effect = None
        backend.decode.return_value = None

        with _owned(dyno), \
                patch.dict(os.environ, {"DYNO": dyno.dyno_name, "DYNO_RAM": "1024"}), \
                patch.object(cache, "client", backend, create=True), \
                patch.object(type(dyno), "current_memory_usage_percentage",
                             new_callable=PropertyMock, return_value=90.0), \
                patch.object(type(dyno), "current_memory_usage",
                             new_callable=PropertyMock, return_value=922), \
                patch.object(cache, "lock") as lock, \
                patch.object(dyno, "set_upscaling") as set_flag, \
                patch.object(dyno, "call_heroku_api") as api, \
                patch("heroku_manager.heroku.logger") as logger:
            dyno.upscale_formation_to_next_level()

        lock.assert_not_called()
        set_flag.assert_not_called()
        api.assert_not_called()
        logger.warning.assert_called_once()
        self.assertIn("primary formation read unavailable", logger.info.call_args.args)

    def test_matching_primary_tier_replaces_a_stale_instance_tier(self):
        dyno = make_dyno("normal_worker.1", formation_size="standard-1x")
        dyno._formation_size_cached = ("standard-1x", time.time())
        self.assertEqual(dyno.threads_available, 4)
        backend, _ = _primary_cache("standard-2x")
        threads_at_patch = []

        def record_threads(*_args, **_kwargs):
            threads_at_patch.append(dyno.threads_available)
            return _mock_response(200)

        with _owned(dyno), \
                patch.dict(os.environ, {"DYNO": dyno.dyno_name, "DYNO_RAM": "1024"}), \
                patch.object(cache, "client", backend, create=True), \
                patch.object(type(dyno), "current_memory_usage_percentage",
                             new_callable=PropertyMock, return_value=90.0), \
                patch.object(type(dyno), "current_memory_usage",
                             new_callable=PropertyMock, return_value=922), \
                patch.object(dyno, "call_heroku_api", side_effect=record_threads) as api, \
                patch.object(dyno, "stop_continuous_autoscale"):
            dyno.upscale_formation_to_next_level()

        self.assertEqual(api.call_args[1]["data"]["size"], "performance-m")
        self.assertEqual(backend.get_client.call_count, 2)
        self.assertEqual(threads_at_patch, [8])

    def test_replacement_at_upscale_lock_acquisition_blocks_patch(self):
        dyno = self._stale_dyno()
        with patch.dict(os.environ, {"DYNO": dyno.dyno_name, "DYNO_RAM": "1024"}), \
                patch_index_backend({}):
            _seed_owned_dyno(dyno)
            heroku_module.cache.set(
                f"heroku:formation_size:{dyno.app_name}:{dyno.formation_name}",
                "standard-2x",
                timeout=None,
            )
            with patch.object(
                        heroku_module.cache,
                        "lock",
                        return_value=_ReplacingOwnerLock(dyno, "replacement-owner"),
                    ), \
                    patch.object(type(dyno), "current_memory_usage_percentage",
                                 new_callable=PropertyMock, return_value=90.0), \
                    patch.object(type(dyno), "current_memory_usage",
                                 new_callable=PropertyMock, return_value=922), \
                    patch.object(dyno, "call_heroku_api") as api:
                dyno.upscale_formation_to_next_level()
        api.assert_not_called()

    def test_owner_replacement_during_diagnostic_sampling_blocks_patch(self):
        dyno = self._stale_dyno()
        with patch.dict(os.environ, {"DYNO": dyno.dyno_name, "DYNO_RAM": "1024"}), \
                patch_index_backend({}):
            _seed_owned_dyno(dyno)
            heroku_module.cache.set(
                f"heroku:formation_size:{dyno.app_name}:{dyno.formation_name}",
                "standard-2x",
                timeout=None,
            )
            sampled = []

            def replace_during_sampling():
                if not sampled:
                    heroku_module.cache.set(
                        heroku_module.cache.make_key(
                            _dyno_owner_key(dyno.app_name, dyno.dyno_name),
                        ),
                        "replacement-owner",
                        timeout=60,
                    )
                    sampled.append(True)
                return 922

            with patch.object(
                        type(dyno),
                        "current_memory_usage",
                        new_callable=PropertyMock,
                        side_effect=replace_during_sampling,
                    ), \
                    patch.object(dyno, "set_upscaling") as set_flag, \
                    patch.object(dyno, "call_heroku_api") as api:
                dyno.upscale_formation_to_next_level()

            self.assertEqual(sampled, [True])
            set_flag.assert_not_called()
            api.assert_not_called()
            self.assertIsNone(heroku_module.cache.get(dyno.upscaling_cache_key))

    def test_replacement_at_downscale_lock_acquisition_blocks_patch(self):
        dyno = self._stale_dyno()
        with patch.dict(os.environ, {"DYNO": dyno.dyno_name, "DYNO_RAM": "1024"}), \
                patch_index_backend({}):
            _seed_owned_dyno(dyno)
            heroku_module.cache.set(
                f"heroku:formation_size:{dyno.app_name}:{dyno.formation_name}",
                "standard-2x",
                timeout=None,
            )
            heroku_module.cache.set(
                dyno.original_size_cache_key,
                {"size": "standard-1x"},
                timeout=None,
            )
            with patch.object(
                        heroku_module.cache,
                        "lock",
                        return_value=_ReplacingOwnerLock(dyno, "replacement-owner"),
                    ), \
                    patch.object(dyno, "call_heroku_api") as api:
                dyno.downscale_formation_to_original_size()
            self.assertIsNone(heroku_module.cache.get(dyno.downscale_cache_key))
        api.assert_not_called()

    def test_primary_miss_preserves_the_instance_tier_behavior(self):
        dyno = make_dyno("normal_worker.1", formation_size="standard-2x")
        backend, _ = _primary_cache()

        with _owned(dyno), \
                patch.dict(os.environ, {"DYNO": dyno.dyno_name, "DYNO_RAM": "1024"}), \
                patch.object(cache, "client", backend, create=True), \
                patch.object(type(dyno), "current_memory_usage_percentage",
                             new_callable=PropertyMock, return_value=90.0), \
                patch.object(type(dyno), "current_memory_usage",
                             new_callable=PropertyMock, return_value=922), \
                patch.object(dyno, "call_heroku_api", return_value=_mock_response(200)) as api, \
                patch.object(dyno, "stop_continuous_autoscale"):
            dyno.upscale_formation_to_next_level()

        self.assertEqual(api.call_args[1]["data"]["size"], "performance-m")

    def test_remote_monitoring_skips_the_local_primary_guard(self):
        dyno = make_dyno("normal_worker.1", formation_size="standard-2x")
        backend, _ = _primary_cache(error=RuntimeError("must not be read"))

        with patch.dict(os.environ, {"DYNO": "web.1", "DYNO_RAM": "1024"}), \
                patch.object(cache, "client", backend, create=True), \
                patch.object(type(dyno), "current_memory_usage_percentage",
                             new_callable=PropertyMock, return_value=90.0), \
                patch.object(type(dyno), "current_memory_usage",
                             new_callable=PropertyMock, return_value=922), \
                patch.object(dyno, "call_heroku_api", return_value=_mock_response(200)) as api, \
                patch.object(dyno, "stop_continuous_autoscale"):
            dyno.upscale_formation_to_next_level()

        api.assert_called_once()
        backend.get_client.assert_not_called()

    def test_remote_explicit_downscale_preserves_the_manual_entry_point(self):
        dyno = make_dyno(
            "normal_worker.1", formation_size="performance-m",
            index_ready_seed=False,
        )
        with patch_index_backend({}):
            dyno.set_original_formation_size("standard-2x")
            heroku_module.cache.set(
                _dyno_owner_key(dyno.app_name, dyno.dyno_name),
                dyno._dyno_owner_token,
                timeout=60,
            )
            with patch.dict(os.environ, {"DYNO": "web.1"}), \
                    patch.object(
                        dyno, "call_heroku_api",
                        return_value=_mock_response(200),
                    ) as api:
                self.assertFalse(dyno.index_ready)
                dyno.downscale_formation_to_original_size()

            api.assert_called_once()
            self.assertEqual(api.call_args.kwargs["data"]["size"], "standard-2x")

    def test_remote_manual_wrong_type_timer_bypasses_cleanup_capture(self):
        from redis.exceptions import ResponseError

        dyno = make_dyno(
            "normal_worker.1", formation_size="performance-m",
            index_ready_seed=False,
        )
        with patch_index_backend({}) as backend:
            baseline = {"size": "standard-1x", "time": timezone.now()}
            timer = ["wrong-type"]
            cache.set(dyno.original_size_cache_key, baseline, timeout=None)
            cache.set(dyno.upscale_until_cache_key, timer, timeout=60)
            timer_key = cache.make_key(dyno.upscale_until_cache_key)
            original_get = backend._client.get

            def wrong_type_get(key):
                if key == timer_key:
                    raise ResponseError("WRONGTYPE Operation against a key")
                return original_get(key)

            backend._client.get = wrong_type_get
            with patch.dict(os.environ, {"DYNO": "web.1"}), \
                    patch.object(
                        dyno,
                        "call_heroku_api",
                        return_value=ProxyResponse(
                            400, '{"id":"bad_request"}',
                        ),
                    ) as api:
                dyno.downscale_formation_to_original_size()

            api.assert_called_once()
            self.assertEqual(api.call_args.kwargs["data"]["size"], "standard-1x")
            self.assertIsNone(cache.get(dyno.downscale_cache_key))
            self.assertEqual(cache.get(dyno.upscale_until_cache_key), timer)

    def test_remote_downscale_raw_miss_uses_previous_tier(self):
        dyno = make_dyno(
            "normal_worker.1", formation_size="performance-m",
            index_ready_seed=False,
        )
        with patch_index_backend({}):
            with patch.dict(os.environ, {"DYNO": "web.1"}), \
                    patch.object(
                        dyno, "call_heroku_api",
                        return_value=_mock_response(200),
                    ) as api:
                dyno.downscale_formation_to_original_size()

            api.assert_called_once()
            self.assertEqual(api.call_args.kwargs["data"]["size"], "standard-2x")

    def test_remote_downscale_rejects_present_invalid_baselines(self):
        for raw_value in (pickle.dumps(None), b"not-a-pickle"):
            with self.subTest(raw_value=raw_value):
                dyno = make_dyno(
                    "normal_worker.1", formation_size="performance-m",
                    index_ready_seed=False,
                )
                with patch_index_backend({}):
                    cache.set(
                        dyno.original_size_cache_key, raw_value, timeout=None,
                    )
                    with patch.dict(os.environ, {"DYNO": "web.1"}), \
                            patch.object(dyno, "call_heroku_api") as api:
                        dyno.downscale_formation_to_original_size()

                    api.assert_not_called()
                    self.assertIsNone(cache.get(dyno.downscale_cache_key))

    def test_unknown_local_size_skips_the_primary_guard(self):
        dyno = make_dyno("normal_worker.1", formation_size="standard-2x")
        backend, _ = _primary_cache(error=RuntimeError("must not be read"))

        with _owned(dyno), \
                patch.dict(os.environ, {"DYNO": dyno.dyno_name}, clear=True), \
                patch.object(cache, "client", backend, create=True), \
                patch.object(type(dyno), "current_memory_usage_percentage",
                             new_callable=PropertyMock, return_value=90.0), \
                patch.object(type(dyno), "current_memory_usage",
                             new_callable=PropertyMock, return_value=922), \
                patch.object(dyno, "call_heroku_api", return_value=_mock_response(200)) as api, \
                patch.object(dyno, "stop_continuous_autoscale"):
            dyno.upscale_formation_to_next_level()

        api.assert_called_once()
        backend.get_client.assert_not_called()

    def test_owner_reservation_preserves_a_raw_primary_tier_miss(self):
        dyno = make_dyno("normal_worker.1", formation_size="standard-2x")
        with patch_index_backend({}):
            _seed_owned_dyno(dyno)
            adapter = _IndexAdapter("floship", primary=True)
            status = adapter.reserve_owned_flag(
                dyno.upscaling_cache_key,
                dyno.dyno_name,
                dyno._dyno_owner_token,
                True,
                300,
                formation_name=dyno.formation_name,
                expected_formation_size=dyno.formation_size,
            )
            self.assertEqual(status, "reserved")
            self.assertTrue(heroku_module.cache.get(dyno.upscaling_cache_key))

            heroku_module.cache.delete(dyno.upscaling_cache_key)
            heroku_module.cache.set(
                f"heroku:formation_size:{dyno.app_name}:{dyno.formation_name}",
                "not-a-tier",
                timeout=None,
            )
            self.assertEqual(
                adapter.reserve_owned_flag(
                    dyno.upscaling_cache_key,
                    dyno.dyno_name,
                    dyno._dyno_owner_token,
                    True,
                    300,
                    formation_name=dyno.formation_name,
                    expected_formation_size=dyno.formation_size,
                ),
                "tier_changed",
            )


class TestAutoscale(BaseLockTestCase):

    @override_settings(DYNO_LOG_THREADS_USED=False)
    def test_info_cycle_does_not_sample_diagnostics(self):
        dyno = make_dyno()
        dyno_type = type(dyno)
        with (
            _owned(dyno),
            patch("heroku_manager.heroku.logger") as logger,
            patch.object(dyno_type, "requires_upscale", new_callable=PropertyMock, return_value=True),
            patch.object(dyno_type, "tasks_in_queue", new_callable=PropertyMock) as queue,
            patch.object(dyno_type, "current_memory_usage", new_callable=PropertyMock) as memory,
            patch.object(dyno_type, "avg_load_1min", new_callable=PropertyMock) as load,
            patch.object(dyno_type, "detected_r14", new_callable=PropertyMock) as r14,
            patch.object(dyno_type, "detected_r15", new_callable=PropertyMock) as r15,
            patch.object(dyno, "get_threads_used") as get_threads,
            patch.object(dyno, "set_threads_used") as set_threads,
            patch.object(dyno, "upscale_formation_to_next_level") as upscale,
        ):
            logger.isEnabledFor.return_value = False
            dyno.autoscale()
        logger.isEnabledFor.assert_called_once_with(logging.DEBUG)
        logger.debug.assert_not_called()
        upscale.assert_called_once()
        for metric in (queue, memory, load, r14, r15, get_threads, set_threads):
            metric.assert_not_called()

    def test_retiring_dyno_skips_normal_scale_decision(self):
        dyno = make_dyno("normal_worker.1", formation_size="standard-2x")
        dyno.dyno_id = "11111111-1111-4111-8111-111111111111"
        dyno._formation_size_cached = ("standard-2x", time.time())
        backend, _ = _primary_cache("standard-1x")

        with patch.dict(os.environ, {"DYNO": dyno.dyno_name, "DYNO_RAM": "1024"}), \
                patch.object(cache, "client", backend, create=True), \
                patch.object(dyno, "upscale_formation_to_next_level") as upscale, \
                patch.object(dyno, "check_and_downscale_to_original_formation_size") as downscale:
            dyno.autoscale(continuous=False)

        upscale.assert_not_called()
        downscale.assert_not_called()

    def test_current_dyno_runs_normal_scale_decision(self):
        dyno = make_dyno("normal_worker.1", formation_size="standard-2x")
        dyno.dyno_id = "11111111-1111-4111-8111-111111111111"
        dyno._formation_size_cached = ("standard-2x", time.time())
        backend, _ = _primary_cache("standard-2x")

        with _owned(dyno), \
                patch.dict(os.environ, {"DYNO": dyno.dyno_name, "DYNO_RAM": "1024"}), \
                patch.object(cache, "client", backend, create=True), \
                patch.object(type(dyno), "requires_upscale",
                             new_callable=PropertyMock, return_value=False), \
                patch.object(type(dyno), "any_sibling_requires_upscale",
                             new_callable=PropertyMock, return_value=False), \
                patch.object(dyno, "check_and_downscale_to_original_formation_size") as downscale:
            dyno.autoscale(continuous=False)

        downscale.assert_called_once()

    @override_settings(DYNO_LOG_THREADS_USED=True)
    def test_debug_publishes_local_thread_count_once(self):
        dyno = make_dyno()
        dyno_type = type(dyno)
        with (
            _owned(dyno),
            patch("heroku_manager.heroku.logger") as logger,
            patch.object(dyno_type, "requires_upscale", new_callable=PropertyMock, return_value=True),
            patch.object(dyno_type, "remote_monitoring", new_callable=PropertyMock, return_value=False),
            patch.object(dyno_type, "tasks_in_queue", new_callable=PropertyMock, return_value=0) as queue,
            patch.object(dyno_type, "current_memory_usage", new_callable=PropertyMock, return_value=200),
            patch.object(dyno_type, "avg_load_1min", new_callable=PropertyMock, return_value=0.2),
            patch.object(dyno_type, "detected_r14", new_callable=PropertyMock, return_value=False),
            patch.object(dyno_type, "detected_r15", new_callable=PropertyMock, return_value=False),
            patch.object(dyno, "set_threads_used", wraps=dyno.set_threads_used) as publish,
            patch.object(dyno, "upscale_formation_to_next_level"),
        ):
            logger.isEnabledFor.return_value = True
            dyno.autoscale()
        publish.assert_called_once()
        queue.assert_called_once()
        self.assertGreater(cache.get(dyno.threads_used_cache_key), 0)
        logger.debug.assert_called_once()

    def test_upscale_path_called_when_requires_upscale(self):
        dyno = make_dyno(formation_size="standard-2x")
        with patch.object(type(dyno), "requires_upscale",
                          new_callable=PropertyMock, return_value=True):
            with patch.object(type(dyno), "tasks_in_queue",
                               new_callable=PropertyMock, return_value=0):
                with patch.object(type(dyno), "current_memory_usage",
                                   new_callable=PropertyMock, return_value=900):
                    with patch.object(type(dyno), "avg_load_1min",
                                       new_callable=PropertyMock, return_value=0.5):
                        with patch.object(type(dyno), "detected_r14",
                                           new_callable=PropertyMock, return_value=False):
                            with patch.object(type(dyno), "detected_r15",
                                               new_callable=PropertyMock, return_value=True):
                                with patch.object(type(dyno), "threads_used",
                                                   new_callable=PropertyMock, return_value=5):
                                    with patch.object(dyno, "upscale_formation_to_next_level") as mock_up:
                                        with patch.object(dyno, "check_and_downscale_to_original_formation_size") as mock_down:
                                            dyno.autoscale(continuous=False)
        mock_up.assert_called_once()
        mock_down.assert_not_called()

    def test_downscale_path_called_when_not_requires_upscale(self):
        dyno = make_dyno(formation_size="standard-2x")
        with patch.object(type(dyno), "requires_upscale",
                          new_callable=PropertyMock, return_value=False):
            with patch.object(type(dyno), "any_sibling_requires_upscale",
                              new_callable=PropertyMock, return_value=False):
                with patch.object(type(dyno), "tasks_in_queue",
                                   new_callable=PropertyMock, return_value=0):
                    with patch.object(type(dyno), "current_memory_usage",
                                       new_callable=PropertyMock, return_value=200):
                        with patch.object(type(dyno), "avg_load_1min",
                                           new_callable=PropertyMock, return_value=0.1):
                            with patch.object(type(dyno), "detected_r14",
                                               new_callable=PropertyMock, return_value=False):
                                with patch.object(type(dyno), "detected_r15",
                                                   new_callable=PropertyMock, return_value=False):
                                    with patch.object(type(dyno), "threads_used",
                                                       new_callable=PropertyMock, return_value=2):
                                        with patch.object(dyno, "check_and_downscale_to_original_formation_size") as mock_down:
                                            with patch.object(dyno, "upscale_formation_to_next_level") as mock_up:
                                                dyno.autoscale(continuous=False)
        mock_down.assert_called_once()
        mock_up.assert_not_called()

    def test_beatworker_skipped_when_flag_false(self):
        dyno = make_dyno("beatworker.1")
        from django.conf import settings as ds
        ds.DYNO_AUTOSCALE_ENABLED_FOR_BEATWORKER = False
        try:
            with patch.object(dyno, "upscale_formation_to_next_level") as mock_up:
                with patch.object(dyno, "check_and_downscale_to_original_formation_size") as mock_down:
                    with patch.object(type(dyno), "tasks_in_queue", new_callable=PropertyMock) as queue:
                        dyno.autoscale()
                    queue.assert_not_called()
            mock_up.assert_not_called()
            mock_down.assert_not_called()
        finally:
            ds.DYNO_AUTOSCALE_ENABLED_FOR_BEATWORKER = True

    def test_autoscale_exception_is_caught(self):
        dyno = make_dyno(formation_size="standard-2x")
        with patch.object(type(dyno), "requires_upscale",
                          new_callable=PropertyMock, side_effect=RuntimeError("boom")):
            with patch.object(type(dyno), "tasks_in_queue",
                               new_callable=PropertyMock, return_value=0):
                with patch.object(type(dyno), "current_memory_usage",
                                   new_callable=PropertyMock, return_value=200):
                    with patch.object(type(dyno), "avg_load_1min",
                                       new_callable=PropertyMock, return_value=0.0):
                        with patch.object(type(dyno), "detected_r14",
                                           new_callable=PropertyMock, return_value=False):
                            with patch.object(type(dyno), "detected_r15",
                                               new_callable=PropertyMock, return_value=False):
                                with patch.object(type(dyno), "threads_used",
                                                   new_callable=PropertyMock, return_value=1):
                                    try:
                                        dyno.autoscale(continuous=False)
                                    except Exception:
                                        self.fail("autoscale() raised unexpectedly")


class TestCheckFormationOnStartup(BaseLockTestCase):


    def test_sets_original_size_when_not_set(self):
        dyno = make_dyno(formation_size="standard-2x")
        dyno._check_formation_on_startup()
        # Formation is above lowest tier with no original — assumes previous tier
        self.assertEqual(dyno.original_formation_size, "standard-1x")
        # Also restores downscale timer
        self.assertIsNotNone(cache.get(dyno.upscale_until_cache_key))

    def test_sets_current_size_as_baseline_at_lowest_tier(self):
        dyno = make_dyno(formation_size="standard-1x")
        dyno._check_formation_on_startup()
        self.assertEqual(dyno.original_formation_size, "standard-1x")
        # No downscale timer needed — already at lowest tier
        self.assertIsNone(cache.get(dyno.upscale_until_cache_key))

    def test_restores_downscale_timer_when_stuck_upscaled(self):
        dyno = make_dyno(formation_size="performance-m")
        cache.set(dyno.original_size_cache_key, {"size": "standard-2x"}, timeout=None)
        dyno._check_formation_on_startup()
        self.assertIsNotNone(cache.get(dyno.upscale_until_cache_key))

    def test_no_action_when_formation_not_upscaled(self):
        dyno = make_dyno(formation_size="standard-2x")
        cache.set(dyno.original_size_cache_key, {"size": "standard-2x"}, timeout=None)
        dyno._check_formation_on_startup()
        self.assertIsNone(cache.get(dyno.upscale_until_cache_key))


class TestRemoteMonitoringGuard(BaseLockTestCase):
    """P0-6: stop_continuous_autoscale must NOT be called after scale on remote-monitored dynos."""

    def test_upscale_does_not_stop_autoscale_for_remote_monitoring(self):
        dyno = make_dyno(formation_size="standard-2x")
        with patch.object(type(dyno), "remote_monitoring",
                          new_callable=PropertyMock, return_value=True):
            with patch.object(dyno, "call_heroku_api", return_value=_mock_response(200)):
                with patch.object(dyno, "stop_continuous_autoscale") as mock_stop:
                    dyno.upscale_formation_to_next_level()
        mock_stop.assert_not_called()

    def test_upscale_stops_autoscale_for_local_dyno(self):
        dyno = make_dyno(formation_size="standard-2x")
        with _owned(dyno), patch.object(type(dyno), "remote_monitoring",
                          new_callable=PropertyMock, return_value=False):
            with patch.object(type(dyno), "current_memory_usage_percentage",
                              new_callable=PropertyMock, return_value=90.0):
                with patch.object(type(dyno), "current_memory_usage",
                                  new_callable=PropertyMock, return_value=922):
                    with patch.object(dyno, "call_heroku_api", return_value=_mock_response(200)):
                        with patch.object(dyno, "stop_continuous_autoscale") as mock_stop:
                            dyno.upscale_formation_to_next_level()
        mock_stop.assert_called_once()

    def test_downscale_does_not_stop_autoscale_for_remote_monitoring(self):
        dyno = make_dyno(formation_size="performance-m")
        cache.set(dyno.original_size_cache_key, {"size": "standard-2x"}, timeout=None)
        with _authorized_downscale(dyno), \
                patch.object(type(dyno), "remote_monitoring",
                             new_callable=PropertyMock, return_value=True):
            with patch.object(dyno, "call_heroku_api", return_value=_mock_response(200)):
                with patch.object(dyno, "stop_continuous_autoscale") as mock_stop:
                    dyno.downscale_formation_to_original_size()
        mock_stop.assert_not_called()


class TestClearUpscalingOnFailure(BaseLockTestCase):
    """P1-2: clear_upscaling() must be called when upscale API call fails so retries are not blocked."""

    def test_clear_upscaling_called_on_api_error_response(self):
        dyno = make_dyno(formation_size="standard-2x")
        with patch.object(dyno, "call_heroku_api", return_value=_mock_response(500)):
            with patch.object(dyno, "clear_upscaling") as mock_clear:
                dyno.upscale_formation_to_next_level()
        mock_clear.assert_called_once()

    def test_clear_upscaling_called_on_api_none_response(self):
        dyno = make_dyno(formation_size="standard-2x")
        with patch.object(dyno, "call_heroku_api", return_value=None):
            with patch.object(dyno, "clear_upscaling") as mock_clear:
                dyno.upscale_formation_to_next_level()
        mock_clear.assert_called_once()

    def test_clear_upscaling_not_called_on_success(self):
        dyno = make_dyno(formation_size="standard-2x")
        with patch.object(type(dyno), "remote_monitoring",
                          new_callable=PropertyMock, return_value=True):
            with patch.object(dyno, "call_heroku_api", return_value=_mock_response(200)):
                with patch.object(dyno, "clear_upscaling") as mock_clear:
                    dyno.upscale_formation_to_next_level()
        mock_clear.assert_not_called()


class TestRestartDynoReturnValue(BaseLockTestCase):
    """P0-8: restart_dyno must return True on success and False on failure."""

    def test_returns_true_on_202(self):
        dyno = make_dyno()
        with patch.object(dyno, "call_heroku_api", return_value=_mock_response(202)):
            with patch.object(dyno, "stop_continuous_autoscale"):
                result = dyno.restart_dyno()
        self.assertTrue(result)

    def test_returns_false_on_api_error(self):
        dyno = make_dyno()
        with patch.object(dyno, "call_heroku_api", return_value=_mock_response(500)):
            result = dyno.restart_dyno()
        self.assertFalse(result)

    def test_returns_false_when_already_restarting(self):
        dyno = make_dyno()
        cache.set(f"heroku:restart_dyno:{dyno.dyno_name}", True, timeout=300)
        result = dyno.restart_dyno()
        self.assertFalse(result)


class TestCounterNoDeleteOnFailedRestart(BaseLockTestCase):
    """P1-6: counter must NOT be deleted when restart_dyno returns False."""

    def test_counter_preserved_when_restart_fails(self):
        from django.conf import settings as ds
        dyno = make_dyno()
        threshold = getattr(ds, "DYNO_RESTART_THRESHOLD", 15)
        key = f"heroku:dyno_counter:{dyno.dyno_name}"
        cache.set(key, threshold - 1, timeout=3600)
        with patch.object(dyno, "restart_dyno", return_value=False):
            dyno.increment_dyno_counter()
        # Counter should still exist because restart failed
        self.assertIsNotNone(cache.get(key))

    def test_counter_deleted_when_restart_succeeds(self):
        from django.conf import settings as ds
        dyno = make_dyno()
        threshold = getattr(ds, "DYNO_RESTART_THRESHOLD", 15)
        key = f"heroku:dyno_counter:{dyno.dyno_name}"
        cache.set(key, threshold - 1, timeout=3600)
        with patch.object(dyno, "restart_dyno", return_value=True):
            dyno.increment_dyno_counter()
        self.assertIsNone(cache.get(key))


# ─── Chain upscale: standard-2x → performance-m when still hot ──────────────

class TestChainUpscaleWhenStillHot(BaseLockTestCase):
    """Production bug: upscale_formation_to_next_level() unconditionally
    blocks chaining when upscale_until key exists. This prevents
    standard-2x → performance-m even when memory is at 213% on 2x."""

    def test_chain_upscale_allowed_when_memory_above_threshold(self):
        """On standard-2x, upscale_until exists, memory at 150% (>80%) →
        should call Heroku API to upscale to performance-m."""
        dyno = make_dyno(formation_size="standard-2x")
        from django.utils import timezone
        from datetime import timedelta
        until = timezone.now() + timedelta(seconds=300)
        cache.set(dyno.upscale_until_cache_key, until, timeout=300)

        with patch.object(type(dyno), "current_memory_usage_percentage",
                          new_callable=PropertyMock, return_value=150.0):
            with patch.object(type(dyno), "current_memory_usage",
                              new_callable=PropertyMock, return_value=1536.0):
                with patch.object(type(dyno), "any_sibling_requires_upscale",
                                  new_callable=PropertyMock, return_value=False):
                    with patch.object(type(dyno), "remote_monitoring",
                                      new_callable=PropertyMock, return_value=True):
                        with patch.object(dyno, "call_heroku_api", return_value=_mock_response(200)) as mock_api:
                            dyno.upscale_formation_to_next_level()
        mock_api.assert_called_once()
        self.assertEqual(dyno._formation_size_cached[0], "performance-m")

    def test_chain_upscale_blocked_when_memory_below_threshold(self):
        """On standard-2x, upscale_until exists, memory at 78% (stale R15) →
        should NOT chain upscale."""
        dyno = make_dyno(formation_size="standard-2x")
        from django.utils import timezone
        from datetime import timedelta
        until = timezone.now() + timedelta(seconds=300)
        cache.set(dyno.upscale_until_cache_key, until, timeout=300)

        with patch.object(type(dyno), "current_memory_usage_percentage",
                          new_callable=PropertyMock, return_value=78.0):
            with patch.object(type(dyno), "any_sibling_requires_upscale",
                              new_callable=PropertyMock, return_value=False):
                with patch.object(dyno, "call_heroku_api") as mock_api:
                    dyno.upscale_formation_to_next_level()
        mock_api.assert_not_called()

    def test_chain_upscale_at_exact_threshold_not_allowed(self):
        """Memory exactly at threshold (80% in test settings) should NOT chain."""
        dyno = make_dyno(formation_size="standard-2x")
        from django.utils import timezone
        from datetime import timedelta
        until = timezone.now() + timedelta(seconds=300)
        cache.set(dyno.upscale_until_cache_key, until, timeout=300)

        with patch.object(type(dyno), "current_memory_usage_percentage",
                          new_callable=PropertyMock, return_value=80.0):
            with patch.object(type(dyno), "any_sibling_requires_upscale",
                              new_callable=PropertyMock, return_value=False):
                with patch.object(dyno, "call_heroku_api") as mock_api:
                    dyno.upscale_formation_to_next_level()
        mock_api.assert_not_called()

    def test_chain_upscale_with_r15_and_high_memory(self):
        """R15 active + memory above threshold → chain upscale allowed."""
        dyno = make_dyno(formation_size="standard-2x")
        from django.utils import timezone
        from datetime import timedelta
        until = timezone.now() + timedelta(seconds=300)
        cache.set(dyno.upscale_until_cache_key, until, timeout=300)

        with patch.object(type(dyno), "current_memory_usage_percentage",
                          new_callable=PropertyMock, return_value=213.0):
            with patch.object(type(dyno), "current_memory_usage",
                              new_callable=PropertyMock, return_value=2183.0):
                with patch.object(type(dyno), "detected_r15",
                                  new_callable=PropertyMock, return_value=True):
                    with patch.object(type(dyno), "any_sibling_requires_upscale",
                                      new_callable=PropertyMock, return_value=False):
                        with patch.object(type(dyno), "remote_monitoring",
                                          new_callable=PropertyMock, return_value=True):
                            with patch.object(dyno, "call_heroku_api", return_value=_mock_response(200)) as mock_api:
                                dyno.upscale_formation_to_next_level()
        mock_api.assert_called_once()
        self.assertEqual(dyno._formation_size_cached[0], "performance-m")

    def test_chain_upscale_production_scenario(self):
        """Full production scenario: standard-1x → standard-2x → memory keeps
        rising → standard-2x → performance-m."""
        dyno = make_dyno(formation_size="standard-2x")
        from django.utils import timezone
        from datetime import timedelta
        cache.set(dyno.original_size_cache_key,
                  {"size": "standard-1x", "time": timezone.now()}, timeout=None)
        until = timezone.now() + timedelta(seconds=630)
        cache.set(dyno.upscale_until_cache_key, until, timeout=630)

        with patch.object(type(dyno), "current_memory_usage_percentage",
                          new_callable=PropertyMock, return_value=213.0):
            with patch.object(type(dyno), "current_memory_usage",
                              new_callable=PropertyMock, return_value=2183.0):
                with patch.object(type(dyno), "any_sibling_requires_upscale",
                                  new_callable=PropertyMock, return_value=False):
                    with patch.object(type(dyno), "remote_monitoring",
                                      new_callable=PropertyMock, return_value=True):
                        with patch.object(dyno, "call_heroku_api", return_value=_mock_response(200)):
                            dyno.upscale_formation_to_next_level()

        self.assertEqual(dyno._formation_size_cached[0], "performance-m")
        new_until = cache.get(dyno.upscale_until_cache_key)
        self.assertIsNotNone(new_until)

    def test_no_chain_upscale_when_no_upscale_until_key(self):
        """Without upscale_until key, normal upscale should still work
        (regression check — the guard should only affect chain upscales)."""
        dyno = make_dyno(formation_size="standard-2x")
        with patch.object(type(dyno), "current_memory_usage_percentage",
                          new_callable=PropertyMock, return_value=90.0):
            with patch.object(type(dyno), "current_memory_usage",
                              new_callable=PropertyMock, return_value=922.0):
                with patch.object(type(dyno), "remote_monitoring",
                                  new_callable=PropertyMock, return_value=True):
                    with patch.object(dyno, "call_heroku_api", return_value=_mock_response(200)) as mock_api:
                        dyno.upscale_formation_to_next_level()
        mock_api.assert_called_once()


# ─── Sibling-triggered chain upscale: cool dyno advocates for hot sibling ────

class TestSiblingTriggeredChainUpscale(BaseLockTestCase):
    """Production bug: when a hot dyno's autoscale thread stalls under memory
    pressure, cool siblings must trigger chain upscale on behalf of the formation.

    Scenario from production:
      - normal_worker.1 at 213% (2183 MB) on standard-2x, autoscale thread stalled
      - normal_worker.2 at 31% (317 MB), running autoscale normally
      - .2 keeps "Extending" the timer but never escalates to performance-m
    """

    def setUp(self):
        super().setUp()
        self._store = {}

    def _set_mem(self, dyno_name, mb):
        key = f"heroku:dyno_memory:{dyno_name}"
        cache.set(key, mb, timeout=60)
        self._store[key] = mb

    def test_autoscale_triggers_upscale_when_sibling_hot(self):
        """Cool dyno (.2) at 31% should trigger upscale when sibling (.1) at 213%."""
        dyno = make_dyno("normal_worker.2", formation_size="standard-2x")
        self._set_mem("normal_worker.1", 2183)  # 213% of 1024 MB

        with patch_index_backend(self._store):
            with patch.object(type(dyno), "current_memory_usage_percentage",
                              new_callable=PropertyMock, return_value=31.0):
                with patch.object(type(dyno), "current_memory_usage",
                                  new_callable=PropertyMock, return_value=317):
                    with patch.object(type(dyno), "detected_r15",
                                      new_callable=PropertyMock, return_value=False):
                        with patch.object(type(dyno), "avg_load_1min",
                                          new_callable=PropertyMock, return_value=0.1):
                            with patch.object(type(dyno), "tasks_in_queue",
                                              new_callable=PropertyMock, return_value=0):
                                with patch.object(type(dyno), "detected_r14",
                                                  new_callable=PropertyMock, return_value=False):
                                    with patch.object(dyno, "upscale_formation_to_next_level") as mock_upscale:
                                        dyno.autoscale(continuous=False)
        mock_upscale.assert_called_once()

    def test_autoscale_no_upscale_when_siblings_cool(self):
        """Cool dyno (.2) with cool siblings should go to downscale path."""
        dyno = make_dyno("normal_worker.2", formation_size="standard-2x")
        self._set_mem("normal_worker.1", 317)  # 31% — cool

        with patch_index_backend(self._store):
            with patch.object(type(dyno), "current_memory_usage_percentage",
                              new_callable=PropertyMock, return_value=31.0):
                with patch.object(type(dyno), "current_memory_usage",
                                  new_callable=PropertyMock, return_value=317):
                    with patch.object(type(dyno), "detected_r15",
                                      new_callable=PropertyMock, return_value=False):
                        with patch.object(type(dyno), "avg_load_1min",
                                          new_callable=PropertyMock, return_value=0.1):
                            with patch.object(type(dyno), "tasks_in_queue",
                                              new_callable=PropertyMock, return_value=0):
                                with patch.object(type(dyno), "detected_r14",
                                                  new_callable=PropertyMock, return_value=False):
                                    with patch.object(dyno, "check_and_downscale_to_original_formation_size") as mock_ds:
                                        dyno.autoscale(continuous=False)
        mock_ds.assert_called_once()

    def test_chain_guard_allows_sibling_triggered_upscale(self):
        """Chain guard should allow upscale when cool dyno triggers it for a hot sibling."""
        from django.utils import timezone
        from datetime import timedelta
        dyno = make_dyno("normal_worker.2", formation_size="standard-2x")
        self._set_mem("normal_worker.1", 2183)  # 213% of 1024 MB

        until = timezone.now() + timedelta(seconds=300)
        cache.set(dyno.upscale_until_cache_key, until, timeout=300)

        with patch_index_backend(self._store):
            with patch.object(type(dyno), "current_memory_usage_percentage",
                              new_callable=PropertyMock, return_value=31.0):
                with patch.object(type(dyno), "current_memory_usage",
                                  new_callable=PropertyMock, return_value=317):
                    with patch.object(type(dyno), "remote_monitoring",
                                      new_callable=PropertyMock, return_value=True):
                        with patch.object(dyno, "call_heroku_api", return_value=_mock_response(200)) as mock_api:
                            dyno.upscale_formation_to_next_level()
        mock_api.assert_called_once()
        self.assertEqual(dyno._formation_size_cached[0], "performance-m")

    def test_chain_guard_blocks_when_neither_self_nor_sibling_hot(self):
        """Chain guard blocks when both self and all siblings are cool."""
        from django.utils import timezone
        from datetime import timedelta
        dyno = make_dyno("normal_worker.2", formation_size="standard-2x")
        self._set_mem("normal_worker.1", 317)  # cool

        until = timezone.now() + timedelta(seconds=300)
        cache.set(dyno.upscale_until_cache_key, until, timeout=300)

        with patch_index_backend(self._store):
            with patch.object(type(dyno), "current_memory_usage_percentage",
                              new_callable=PropertyMock, return_value=31.0):
                with patch.object(dyno, "call_heroku_api") as mock_api:
                    dyno.upscale_formation_to_next_level()
        mock_api.assert_not_called()

    def test_production_scenario_full_sibling_advocacy(self):
        """Full production scenario: .1 at 213% on 2x (thread stalled),
        .2 at 31% detects via sibling memory and chains to performance-m."""
        from django.utils import timezone
        from datetime import timedelta
        dyno = make_dyno("normal_worker.2", formation_size="standard-2x")
        self._set_mem("normal_worker.1", 2183)  # .1 is hot

        cache.set(dyno.original_size_cache_key,
                  {"size": "standard-1x", "time": timezone.now()}, timeout=None)
        until = timezone.now() + timedelta(seconds=630)
        cache.set(dyno.upscale_until_cache_key, until, timeout=630)

        with patch_index_backend(self._store):
            with patch.object(type(dyno), "current_memory_usage_percentage",
                              new_callable=PropertyMock, return_value=31.0):
                with patch.object(type(dyno), "current_memory_usage",
                                  new_callable=PropertyMock, return_value=317):
                    with patch.object(type(dyno), "detected_r15",
                                      new_callable=PropertyMock, return_value=False):
                        with patch.object(type(dyno), "detected_r14",
                                          new_callable=PropertyMock, return_value=False):
                            with patch.object(type(dyno), "remote_monitoring",
                                              new_callable=PropertyMock, return_value=True):
                                with patch.object(type(dyno), "avg_load_1min",
                                                  new_callable=PropertyMock, return_value=0.1):
                                    with patch.object(type(dyno), "tasks_in_queue",
                                                      new_callable=PropertyMock, return_value=0):
                                        with patch.object(dyno, "call_heroku_api", return_value=_mock_response(200)):
                                            dyno.autoscale(continuous=False)

        self.assertEqual(dyno._formation_size_cached[0], "performance-m")


class TestLostBaselineRecovery(BaseLockTestCase):
    """
    v0.2.8 — When original_formation_size is lost (Redis eviction, phantom
    clear, etc.) and the formation is above the lowest tier, the system
    must still be able to downscale instead of staying stuck forever.
    """

    # ── startup safety check ────────────────────────────────────────────

    def test_startup_records_previous_tier_when_above_lowest(self):
        """standard-2x with no original → records standard-1x as original."""
        dyno = make_dyno(formation_size="standard-2x")
        dyno._check_formation_on_startup()
        self.assertEqual(dyno.original_formation_size, "standard-1x")

    def test_startup_sets_downscale_timer_when_above_lowest(self):
        dyno = make_dyno(formation_size="standard-2x")
        dyno._check_formation_on_startup()
        self.assertIsNotNone(cache.get(dyno.upscale_until_cache_key))

    def test_startup_records_current_at_lowest_tier(self):
        """standard-1x has no previous → records standard-1x as baseline."""
        dyno = make_dyno(formation_size="standard-1x")
        dyno._check_formation_on_startup()
        self.assertEqual(dyno.original_formation_size, "standard-1x")
        self.assertIsNone(cache.get(dyno.upscale_until_cache_key))

    def test_startup_perf_m_records_standard_2x(self):
        dyno = make_dyno(formation_size="performance-m")
        dyno._check_formation_on_startup()
        self.assertEqual(dyno.original_formation_size, "standard-2x")
        self.assertIsNotNone(cache.get(dyno.upscale_until_cache_key))

    def test_startup_does_not_overwrite_existing_original(self):
        dyno = make_dyno(formation_size="performance-m")
        cache.set(dyno.original_size_cache_key, {"size": "standard-1x"}, timeout=None)
        dyno._check_formation_on_startup()
        # Existing original must not be overwritten
        self.assertEqual(dyno.original_formation_size, "standard-1x")

    # ── downscale fallback to previous tier ──────────────────────────────

    def test_downscale_uses_previous_tier_when_original_lost(self):
        dyno = make_dyno(formation_size="standard-2x")
        # No original set — should fall back to previous tier (standard-1x)
        with _authorized_downscale(dyno), \
                patch.object(dyno, "call_heroku_api", return_value=_mock_response(200)) as mock_api:
            dyno.downscale_formation_to_original_size()
        mock_api.assert_called_once()
        self.assertEqual(mock_api.call_args[1]["data"]["size"], "standard-1x")

    def test_downscale_perf_m_falls_back_to_standard_2x(self):
        dyno = make_dyno(formation_size="performance-m")
        with _authorized_downscale(dyno), \
                patch.object(dyno, "call_heroku_api", return_value=_mock_response(200)) as mock_api:
            dyno.downscale_formation_to_original_size()
        self.assertEqual(mock_api.call_args[1]["data"]["size"], "standard-2x")

    def test_downscale_noop_at_lowest_tier_no_original(self):
        """standard-1x with no original → nothing to downscale."""
        dyno = make_dyno(formation_size="standard-1x")
        with patch.object(dyno, "call_heroku_api") as mock_api:
            dyno.downscale_formation_to_original_size()
        mock_api.assert_not_called()

    # ── set_original_formation_size with explicit value ──────────────────

    def test_set_original_formation_size_with_string_value(self):
        dyno = make_dyno(formation_size="standard-2x")
        dyno.set_original_formation_size(value="standard-1x")
        self.assertEqual(dyno.original_formation_size, "standard-1x")

    def test_set_original_overwrites_with_string_value(self):
        dyno = make_dyno(formation_size="performance-m")
        dyno.set_original_formation_size()  # stores performance-m
        dyno.set_original_formation_size(value="standard-1x")  # overwrites
        self.assertEqual(dyno.original_formation_size, "standard-1x")

    # ── end-to-end: phantom clear → recovery ─────────────────────────────

    def test_phantom_clear_then_startup_recovers(self):
        """Reproduces the NAF bug: formation stuck at 2x after phantom clear."""
        dyno = make_dyno(formation_size="standard-2x")
        # Step 1: Startup records wrong baseline (old behavior would record 2x)
        dyno._check_formation_on_startup()
        # With fix: original is now standard-1x
        self.assertEqual(dyno.original_formation_size, "standard-1x")
        # Phantom detector should NOT fire because 2x > 1x
        self.assertFalse(dyno.is_on_original_formation_size_or_lower)

    def test_full_recovery_cycle_from_stuck_2x(self):
        """
        Simulates: formation at 2x, original lost, startup recovers,
        allow_downscale lets downscale proceed.
        """
        dyno = make_dyno(formation_size="standard-2x")
        # Step 1: startup sets original to standard-1x + timer
        dyno._check_formation_on_startup()
        self.assertEqual(dyno.original_formation_size, "standard-1x")

        # Step 2: Simulate timer expiry + low memory (allow downscale)
        cache.delete(dyno.upscale_until_cache_key)
        cache.set(f"heroku:dyno_memory:{dyno.dyno_name}", 400, timeout=60)
        cache.set(f"heroku:dyno_load:{dyno.dyno_name}", 0.0, timeout=60)
        with patch_index_backend({}):
            _seed_owned_dyno(dyno)
            heroku_module.cache.delete(
                _registry_reconverging_key(dyno.app_name),
            )
            with patch.dict(os.environ, {"DYNO": dyno.dyno_name}):
                with patch.object(type(dyno), "current_memory_usage",
                                  new_callable=PropertyMock, return_value=400):
                    with patch.object(type(dyno), "detected_r14",
                                      new_callable=PropertyMock, return_value=False):
                        with patch.object(type(dyno), "detected_r15",
                                          new_callable=PropertyMock, return_value=False):
                            with patch.object(type(dyno), "no_tasks_in_queue",
                                              new_callable=PropertyMock, return_value=True):
                                with patch.object(type(dyno), "any_sibling_requires_upscale",
                                                  new_callable=PropertyMock, return_value=False):
                                    with patch.object(dyno, "call_heroku_api",
                                                      return_value=_mock_response(200)) as mock_api:
                                        dyno.check_and_downscale_to_original_formation_size()
        # Should have downscaled to standard-1x
        mock_api.assert_called_once()
        self.assertEqual(mock_api.call_args[1]["data"]["size"], "standard-1x")


class TestMemoryStabilityDetection(BaseLockTestCase):
    """Tests for RSS plateau detection and stability-based forced downscale."""

    def _seed_stable_history(self, dyno, base_mem=15000, count=8, tolerance=10):
        """Seed a flat memory history (readings within ±tolerance of base_mem)."""
        import time as _time
        now = _time.time()
        history = [
            (now - (count - i) * 30, base_mem + (i % 2) * tolerance)
            for i in range(count)
        ]
        cache.set(dyno.memory_history_cache_key, history, timeout=600)
        cache.set(f"heroku:dyno_memory:{dyno.dyno_name}", history[-1][1], timeout=60)
        cache.set(f"heroku:dyno_memory_stable:{dyno.dyno_name}", True, timeout=60)
        cache.set(f"heroku:dyno_load:{dyno.dyno_name}", 0.2, timeout=60)

    def _seed_rising_history(self, dyno, start=10000, step=200, count=8):
        """Seed a steadily rising memory history."""
        import time as _time
        now = _time.time()
        history = [
            (now - (count - i) * 30, start + i * step)
            for i in range(count)
        ]
        cache.set(dyno.memory_history_cache_key, history, timeout=600)
        cache.set(f"heroku:dyno_memory:{dyno.dyno_name}", history[-1][1], timeout=60)
        cache.set(f"heroku:dyno_memory_stable:{dyno.dyno_name}", False, timeout=60)
        cache.set(f"heroku:dyno_load:{dyno.dyno_name}", 0.2, timeout=60)

    # --- is_memory_stabilized ---

    def test_stabilized_true_when_readings_are_flat(self):
        dyno = make_dyno(formation_size="performance-l-ram")
        self._seed_stable_history(dyno, base_mem=15000, count=8, tolerance=10)
        self.assertTrue(dyno.is_memory_stabilized)

    def test_stabilized_false_when_memory_is_rising(self):
        dyno = make_dyno(formation_size="performance-l-ram")
        self._seed_rising_history(dyno, start=10000, step=200, count=8)
        self.assertFalse(dyno.is_memory_stabilized)

    def test_stabilized_false_when_too_few_readings(self):
        dyno = make_dyno(formation_size="performance-l-ram")
        import time as _time
        now = _time.time()
        history = [(now - 30, 15000), (now, 15010)]
        cache.set(dyno.memory_history_cache_key, history, timeout=600)
        self.assertFalse(dyno.is_memory_stabilized)

    def test_stabilized_false_when_readings_bunched_in_short_burst(self):
        """Even with enough readings, if they all arrive within a few seconds,
        that's not a real plateau — reject."""
        dyno = make_dyno(formation_size="performance-l-ram")
        import time as _time
        now = _time.time()
        # 8 readings all within 10 seconds — well under window/2 (150s)
        history = [(now - 10 + i, 15000 + i) for i in range(8)]
        cache.set(dyno.memory_history_cache_key, history, timeout=600)
        self.assertFalse(dyno.is_memory_stabilized)

    def test_stabilized_uses_proportional_tolerance_for_large_memory(self):
        """At 15 GB RSS, the 1% proportional tolerance (150 MB) should be used
        instead of the fixed 50 MB floor, so readings within ±100 MB pass."""
        dyno = make_dyno(formation_size="performance-l-ram")
        import time as _time
        now = _time.time()
        # Readings swing ±100 MB around 15000 — exceeds 50 MB but within 1% (150 MB)
        readings_vals = [15000, 15100, 14900, 15050, 14950, 15080, 14920, 15030]
        history = [
            (now - (len(readings_vals) - i) * 30, readings_vals[i])
            for i in range(len(readings_vals))
        ]
        cache.set(dyno.memory_history_cache_key, history, timeout=600)
        self.assertTrue(dyno.is_memory_stabilized)

    def test_stabilized_false_when_no_history(self):
        dyno = make_dyno(formation_size="performance-l-ram")
        self.assertFalse(dyno.is_memory_stabilized)

    # --- allow_downscale with stability override ---

    def test_allow_downscale_true_when_memory_stable_above_threshold(self):
        """Upscaled dyno with flat RSS at 15 GB should be allowed to downscale."""
        dyno = make_dyno(formation_size="performance-l-ram")
        cache.set(dyno.original_size_cache_key, {"size": "standard-1x"}, timeout=None)
        self._seed_stable_history(dyno, base_mem=15000)
        with patch_index_backend({}):
            with patch.object(type(dyno), "current_memory_usage",
                              new_callable=PropertyMock, return_value=15000):
                with patch.object(type(dyno), "detected_r14",
                                  new_callable=PropertyMock, return_value=False):
                    with patch.object(type(dyno), "detected_r15",
                                      new_callable=PropertyMock, return_value=False):
                        with patch.object(type(dyno), "no_tasks_in_queue",
                                          new_callable=PropertyMock, return_value=True):
                            self.assertTrue(dyno.allow_downscale)

    def test_allow_downscale_false_when_memory_stable_but_r15(self):
        """Stable memory with active R15 means the dyno is still OOM — don't downscale."""
        dyno = make_dyno(formation_size="performance-l-ram")
        cache.set(dyno.original_size_cache_key, {"size": "standard-1x"}, timeout=None)
        self._seed_stable_history(dyno, base_mem=30000)
        with patch_index_backend({}):
            with patch.object(type(dyno), "current_memory_usage",
                              new_callable=PropertyMock, return_value=30000):
                with patch.object(type(dyno), "current_memory_usage_percentage",
                                  new_callable=PropertyMock, return_value=98.0):
                    with patch.object(type(dyno), "detected_r14",
                                      new_callable=PropertyMock, return_value=False):
                        with patch.object(type(dyno), "detected_r15",
                                          new_callable=PropertyMock, return_value=True):
                            with patch.object(type(dyno), "no_tasks_in_queue",
                                              new_callable=PropertyMock, return_value=True):
                                self.assertFalse(dyno.allow_downscale)

    def test_allow_downscale_false_when_memory_still_rising(self):
        """Rising memory means heavy work is still happening — don't downscale."""
        dyno = make_dyno(formation_size="performance-l-ram")
        cache.set(dyno.original_size_cache_key, {"size": "standard-1x"}, timeout=None)
        self._seed_rising_history(dyno, start=10000, step=200)
        with patch_index_backend({}):
            with patch.object(type(dyno), "current_memory_usage",
                              new_callable=PropertyMock, return_value=11400):
                with patch.object(type(dyno), "detected_r14",
                                  new_callable=PropertyMock, return_value=False):
                    with patch.object(type(dyno), "detected_r15",
                                      new_callable=PropertyMock, return_value=False):
                        with patch.object(type(dyno), "no_tasks_in_queue",
                                          new_callable=PropertyMock, return_value=True):
                            self.assertFalse(dyno.allow_downscale)

    def test_allow_downscale_false_when_already_at_original_size(self):
        """Stability override should not fire when already at original size."""
        dyno = make_dyno(formation_size="standard-1x")
        cache.set(dyno.original_size_cache_key, {"size": "standard-1x"}, timeout=None)
        self._seed_stable_history(dyno, base_mem=300)
        with patch_index_backend({}):
            with patch.object(type(dyno), "current_memory_usage",
                              new_callable=PropertyMock, return_value=300):
                with patch.object(type(dyno), "detected_r14",
                                  new_callable=PropertyMock, return_value=False):
                    with patch.object(type(dyno), "detected_r15",
                                      new_callable=PropertyMock, return_value=False):
                        with patch.object(type(dyno), "no_tasks_in_queue",
                                          new_callable=PropertyMock, return_value=True):
                            # The normal path handles this because memory is cool.
                            self.assertTrue(dyno.allow_downscale)

    # --- Sibling stability ---

    def test_sibling_high_but_stable_does_not_block_downscale(self):
        """Sibling with high RSS but stabilized memory should not block downscale."""
        dyno = make_dyno(formation_size="performance-l-ram")
        cache.set(dyno.original_size_cache_key, {"size": "standard-1x"}, timeout=None)
        # Set up sibling memory + stability
        cache.set("heroku:dyno_memory:normal_worker.2", 15000, timeout=300)
        cache.set("heroku:dyno_memory_stable:normal_worker.2", True, timeout=300)
        memory_store = {
            f"heroku:dyno_memory:{dyno.dyno_name}": 15000,
            "heroku:dyno_memory:normal_worker.2": 15000,
        }
        with patch_index_backend(memory_store):
            self.assertFalse(dyno.any_sibling_still_high_memory)

    def test_sibling_high_and_unstable_blocks_downscale(self):
        """Sibling with high and still-rising memory MUST block downscale."""
        dyno = make_dyno(formation_size="performance-l-ram")
        cache.set(dyno.original_size_cache_key, {"size": "standard-1x"}, timeout=None)
        # Sibling is high and NOT stable
        cache.set("heroku:dyno_memory:normal_worker.2", 15000, timeout=300)
        cache.set("heroku:dyno_memory_stable:normal_worker.2", False, timeout=300)
        memory_store = {
            f"heroku:dyno_memory:{dyno.dyno_name}": 15000,
            "heroku:dyno_memory:normal_worker.2": 15000,
        }
        with patch_index_backend(memory_store):
            self.assertTrue(dyno.any_sibling_still_high_memory)

    def test_sibling_high_with_no_stability_key_blocks_downscale(self):
        """If sibling has no stability key published, assume unstable — block."""
        dyno = make_dyno(formation_size="performance-l-ram")
        cache.set(dyno.original_size_cache_key, {"size": "standard-1x"}, timeout=None)
        cache.set("heroku:dyno_memory:normal_worker.2", 15000, timeout=300)
        # No dyno_memory_stable key for sibling
        memory_store = {
            f"heroku:dyno_memory:{dyno.dyno_name}": 15000,
            "heroku:dyno_memory:normal_worker.2": 15000,
        }
        with patch_index_backend(memory_store):
            self.assertTrue(dyno.any_sibling_still_high_memory)

    # --- record_memory_reading ---

    def test_record_memory_reading_appends_to_history(self):
        dyno = make_dyno(formation_size="performance-l-ram")
        dyno.record_memory_reading(15000)
        dyno.record_memory_reading(15020)
        history = cache.get(dyno.memory_history_cache_key)
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0][1], 15000)
        self.assertEqual(history[1][1], 15020)

    def test_record_memory_reading_prunes_old_entries(self):
        import time as _time
        dyno = make_dyno(formation_size="performance-l-ram")
        # Seed a reading that's outside the window
        old_time = _time.time() - 600
        history = [(old_time, 10000)]
        cache.set(dyno.memory_history_cache_key, history, timeout=600)
        dyno.record_memory_reading(15000)
        history = cache.get(dyno.memory_history_cache_key)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0][1], 15000)

    # --- Downscale clears memory history ---

    def test_downscale_clears_memory_history(self):
        dyno = make_dyno(formation_size="performance-m")
        cache.set(dyno.original_size_cache_key, {"size": "standard-2x"}, timeout=None)
        self._seed_stable_history(dyno, base_mem=2000)
        with _authorized_downscale(dyno), \
                patch.object(dyno, "call_heroku_api", return_value=_mock_response(200)):
            with patch.object(dyno, "stop_continuous_autoscale"):
                dyno.downscale_formation_to_original_size()
        self.assertIsNone(cache.get(dyno.memory_history_cache_key))

    def test_upscale_clears_memory_history(self):
        """Upscale triggers a restart, so stale pre-upscale readings must be cleared."""
        dyno = make_dyno(formation_size="standard-2x")
        self._seed_stable_history(dyno, base_mem=900)
        with patch.object(type(dyno), "current_memory_usage_percentage",
                          new_callable=PropertyMock, return_value=90.0):
            with patch.object(type(dyno), "current_memory_usage",
                               new_callable=PropertyMock, return_value=922):
                with patch.object(type(dyno), "remote_monitoring",
                                   new_callable=PropertyMock, return_value=True):
                    with patch.object(dyno, "call_heroku_api", return_value=_mock_response(200)):
                        with patch.object(dyno, "stop_continuous_autoscale"):
                            dyno.upscale_formation_to_next_level()
        self.assertIsNone(cache.get(dyno.memory_history_cache_key))

    # --- allow_downscale_on_shutdown with stability override ---

    def test_allow_downscale_on_shutdown_true_when_stable(self):
        dyno = make_dyno(formation_size="performance-l-ram")
        cache.set(dyno.original_size_cache_key, {"size": "standard-1x"}, timeout=None)
        self._seed_stable_history(dyno, base_mem=15000)
        with patch_index_backend({}):
            with patch.object(type(dyno), "current_memory_usage",
                              new_callable=PropertyMock, return_value=15000):
                with patch.object(type(dyno), "detected_r14",
                                  new_callable=PropertyMock, return_value=False):
                    with patch.object(type(dyno), "detected_r15",
                                      new_callable=PropertyMock, return_value=False):
                        self.assertTrue(dyno.allow_downscale_on_shutdown)

    # --- Near-expiry forced downscale on stability ---

    def test_near_expiry_forces_downscale_when_stable_and_queue_not_empty(self):
        """When memory is stabilized and queue has tasks (so allow_downscale is False
        due to downscale_on_non_empty_queue=False), the near-expiry path should
        force a downscale instead of a pointless restart."""
        dyno = make_dyno(formation_size="performance-l-ram")
        dyno.__dict__["downscale_on_non_empty_queue"] = False
        cache.set(dyno.original_size_cache_key, {"size": "standard-1x"}, timeout=None)
        self._seed_stable_history(dyno, base_mem=15000)
        from django.utils import timezone as tz
        from django.core.cache.backends.locmem import LocMemCache
        until = tz.now() + tz.timedelta(seconds=60)
        cache.set(dyno.upscale_until_cache_key, until, timeout=60)
        with patch_index_backend({}):
            with patch.object(LocMemCache, "ttl", return_value=59):
                with patch.object(type(dyno), "current_memory_usage",
                                  new_callable=PropertyMock, return_value=15000):
                    with patch.object(type(dyno), "current_memory_usage_percentage",
                                      new_callable=PropertyMock, return_value=49.0):
                        with patch.object(type(dyno), "detected_r14",
                                          new_callable=PropertyMock, return_value=False):
                            with patch.object(type(dyno), "detected_r15",
                                              new_callable=PropertyMock, return_value=False):
                                with patch.object(type(dyno), "no_tasks_in_queue",
                                                  new_callable=PropertyMock, return_value=False):
                                    with patch.object(dyno, "downscale_formation_to_original_size") as mock_down:
                                        with patch.object(dyno, "restart_dyno") as mock_restart:
                                            dyno.check_and_downscale_to_original_formation_size()
        mock_down.assert_called_once()
        mock_restart.assert_not_called()

    def test_near_expiry_does_not_force_downscale_when_r15_active(self):
        """Even with stable memory, if R15 is active we must NOT force downscale."""
        dyno = make_dyno(formation_size="performance-l-ram")
        cache.set(dyno.original_size_cache_key, {"size": "standard-1x"}, timeout=None)
        self._seed_stable_history(dyno, base_mem=30000)
        from django.utils import timezone as tz
        from django.core.cache.backends.locmem import LocMemCache
        until = tz.now() + tz.timedelta(seconds=60)
        cache.set(dyno.upscale_until_cache_key, until, timeout=60)
        with patch_index_backend({}):
            with patch.object(LocMemCache, "ttl", return_value=59):
                with patch.object(type(dyno), "current_memory_usage",
                                  new_callable=PropertyMock, return_value=30000):
                    with patch.object(type(dyno), "current_memory_usage_percentage",
                                      new_callable=PropertyMock, return_value=98.0):
                        with patch.object(type(dyno), "detected_r14",
                                          new_callable=PropertyMock, return_value=False):
                            with patch.object(type(dyno), "detected_r15",
                                              new_callable=PropertyMock, return_value=True):
                                with patch.object(type(dyno), "no_tasks_in_queue",
                                                  new_callable=PropertyMock, return_value=True):
                                    with patch.object(dyno, "downscale_formation_to_original_size") as mock_down:
                                        with patch.object(dyno, "restart_dyno"):
                                            dyno.check_and_downscale_to_original_formation_size()
        mock_down.assert_not_called()

    # --- remove_dyno_from_alive_cache cleans stable key ---

    def test_remove_dyno_cleans_stable_key(self):
        dyno = make_dyno("normal_worker.1")
        with patch.dict(os.environ, {"DYNO": dyno.dyno_name}), \
                patch_index_backend({}):
            _seed_owned_dyno(dyno)
            heroku_module.cache.set(f"heroku:dyno_memory_stable:{dyno.dyno_name}", True, timeout=300)
            heroku_module.cache.set(f"heroku:dyno_memory:{dyno.dyno_name}", 15000, timeout=300)
            heroku_module.cache.set(f"heroku:dyno_alive:{dyno.dyno_name}", "now", timeout=300)
            heroku_module.cache.set(f"heroku:dyno_load:{dyno.dyno_name}", 0.5, timeout=300)
            heroku_module.cache.set(
                f"heroku:dyno_memory_veto:v1:{dyno.app_name}:{dyno.dyno_name}",
                (dyno._dyno_owner_token, time.time(), 700),
                timeout=300,
            )
            dyno.remove_dyno_from_alive_cache()
            self.assertIsNone(heroku_module.cache.get(f"heroku:dyno_memory_stable:{dyno.dyno_name}"))
            self.assertIsNone(heroku_module.cache.get(f"heroku:dyno_memory:{dyno.dyno_name}"))
            self.assertIsNone(heroku_module.cache.get(f"heroku:dyno_alive:{dyno.dyno_name}"))
            self.assertIsNone(heroku_module.cache.get(f"heroku:dyno_load:{dyno.dyno_name}"))
            self.assertIsNone(heroku_module.cache.get(
                f"heroku:dyno_memory_veto:v1:{dyno.app_name}:{dyno.dyno_name}",
            ))

    # --- _downscale_memory_threshold edge case ---

    def test_downscale_threshold_blocks_when_target_memory_is_zero(self):
        """If target size somehow has 0 memory, threshold must be inf (block downscale)."""
        dyno = make_dyno(formation_size="performance-m")
        cache.set(dyno.original_size_cache_key, {"size": "standard-1x"}, timeout=None)
        with patch.dict("heroku_manager.heroku.DYNO_SIZES",
                        {"standard-1x": {"memory": 0, "next": "standard-2x", "previous": None,
                                          "threads_available": 1, "price_per_hour": 0}},
                        clear=False):
            self.assertTrue(math.isinf(dyno._downscale_memory_threshold))


class TestFormationIdleGate(BaseLockTestCase):
    """Tests for is_formation_idle and its role in gating stability-based downscale."""

    def test_idle_true_when_own_load_below_threshold(self):
        dyno = make_dyno(formation_size="performance-l-ram")
        cache.set(f"heroku:dyno_load:{dyno.dyno_name}", 0.3, timeout=300)
        with patch_index_backend({}):
            self.assertTrue(dyno.is_formation_idle)

    def test_idle_false_when_own_load_above_threshold(self):
        dyno = make_dyno(formation_size="performance-l-ram")
        cache.set(f"heroku:dyno_load:{dyno.dyno_name}", 1.5, timeout=300)
        with patch_index_backend({}):
            self.assertFalse(dyno.is_formation_idle)

    def test_idle_false_when_sibling_load_above_threshold(self):
        dyno = make_dyno(formation_size="performance-l-ram")
        cache.set("heroku:dyno_load:normal_worker.2", 2.3, timeout=300)
        load_store = {
            f"heroku:dyno_load:{dyno.dyno_name}": 0.3,
            "heroku:dyno_load:normal_worker.2": 2.3,
        }
        with patch_index_backend(load_store):
            self.assertFalse(dyno.is_formation_idle)

    def test_idle_true_when_all_siblings_below_threshold(self):
        dyno = make_dyno(formation_size="performance-l-ram")
        cache.set("heroku:dyno_load:normal_worker.2", 0.4, timeout=300)
        load_store = {
            f"heroku:dyno_load:{dyno.dyno_name}": 0.3,
            "heroku:dyno_load:normal_worker.2": 0.4,
        }
        with patch_index_backend(load_store):
            self.assertTrue(dyno.is_formation_idle)

    def test_idle_at_exact_threshold_is_not_idle(self):
        """Load at exactly 1.0 should be considered NOT idle (>= threshold)."""
        dyno = make_dyno(formation_size="performance-l-ram")
        cache.set(f"heroku:dyno_load:{dyno.dyno_name}", 1.0, timeout=300)
        with patch_index_backend({}):
            self.assertFalse(dyno.is_formation_idle)

    def test_allow_downscale_false_when_stable_but_load_high(self):
        """Memory stabilized + high load = heavy task still running. Don't downscale."""
        dyno = make_dyno(formation_size="performance-l-ram")
        cache.set(dyno.original_size_cache_key, {"size": "standard-1x"}, timeout=None)
        from tests.test_scale_operations import TestMemoryStabilityDetection
        # Reuse the seeding helper via a fresh instance
        helper = TestMemoryStabilityDetection()
        helper._seed_stable_history(dyno, base_mem=15000)
        cache.set(f"heroku:dyno_load:{dyno.dyno_name}", 2.0, timeout=300)
        with patch_index_backend({}):
            with patch.object(type(dyno), "current_memory_usage",
                              new_callable=PropertyMock, return_value=15000):
                with patch.object(type(dyno), "detected_r14",
                                  new_callable=PropertyMock, return_value=False):
                    with patch.object(type(dyno), "detected_r15",
                                      new_callable=PropertyMock, return_value=False):
                        with patch.object(type(dyno), "no_tasks_in_queue",
                                          new_callable=PropertyMock, return_value=True):
                            self.assertFalse(dyno.allow_downscale)

    def test_allow_downscale_true_when_stable_and_load_settled(self):
        """Memory stabilized + low load = work finished. Allow downscale."""
        dyno = make_dyno(formation_size="performance-l-ram")
        cache.set(dyno.original_size_cache_key, {"size": "standard-1x"}, timeout=None)
        from tests.test_scale_operations import TestMemoryStabilityDetection
        helper = TestMemoryStabilityDetection()
        helper._seed_stable_history(dyno, base_mem=15000)
        with patch_index_backend({}):
            with patch.object(type(dyno), "current_memory_usage",
                              new_callable=PropertyMock, return_value=15000):
                with patch.object(type(dyno), "detected_r14",
                                  new_callable=PropertyMock, return_value=False):
                    with patch.object(type(dyno), "detected_r15",
                                      new_callable=PropertyMock, return_value=False):
                        with patch.object(type(dyno), "no_tasks_in_queue",
                                          new_callable=PropertyMock, return_value=True):
                            self.assertTrue(dyno.allow_downscale)

    def test_near_expiry_blocks_downscale_when_stable_but_load_high(self):
        """Near timer expiry: stable memory but high load — don't force downscale.
        The normal restart-if-hot path may still fire, but the formation must NOT
        be downsized while a heavy task is running."""
        dyno = make_dyno(formation_size="performance-l-ram")
        dyno.__dict__["downscale_on_non_empty_queue"] = True
        cache.set(dyno.original_size_cache_key, {"size": "standard-1x"}, timeout=None)
        from tests.test_scale_operations import TestMemoryStabilityDetection
        helper = TestMemoryStabilityDetection()
        helper._seed_stable_history(dyno, base_mem=15000)
        cache.set(f"heroku:dyno_load:{dyno.dyno_name}", 2.0, timeout=300)
        from django.utils import timezone as tz
        from django.core.cache.backends.locmem import LocMemCache
        until = tz.now() + tz.timedelta(seconds=60)
        cache.set(dyno.upscale_until_cache_key, until, timeout=60)
        with patch_index_backend({}):
            with patch.object(LocMemCache, "ttl", return_value=59):
                with patch.object(type(dyno), "current_memory_usage",
                                  new_callable=PropertyMock, return_value=15000):
                    with patch.object(type(dyno), "current_memory_usage_percentage",
                                      new_callable=PropertyMock, return_value=49.0):
                        with patch.object(type(dyno), "detected_r14",
                                          new_callable=PropertyMock, return_value=False):
                            with patch.object(type(dyno), "detected_r15",
                                              new_callable=PropertyMock, return_value=False):
                                with patch.object(type(dyno), "no_tasks_in_queue",
                                                  new_callable=PropertyMock, return_value=True):
                                    with patch.object(dyno, "downscale_formation_to_original_size") as mock_down:
                                        with patch.object(dyno, "restart_dyno"):
                                            dyno.check_and_downscale_to_original_formation_size()
        # Key assertion: formation must NOT be downsized during active work
        mock_down.assert_not_called()


class TestLockExpiryHandling(BaseLockTestCase):
    """All cache.lock sites handle known lock-expiry exceptions without crashing."""

    def _expired_lock(self):
        """Mock lock whose __exit__ raises NotAcquired."""
        from heroku_manager.heroku import _NotAcquired
        lock_cm = MagicMock()
        lock_cm.__enter__ = MagicMock(return_value=None)
        lock_cm.__exit__ = MagicMock(side_effect=_NotAcquired("Lock expired"))
        return lock_cm
    def _expired_lock_lno(self):
        """Mock lock whose __exit__ raises LockNotOwnedError."""
        from heroku_manager.heroku import LockNotOwnedError
        lock_cm = MagicMock()
        lock_cm.__enter__ = MagicMock(return_value=None)
        lock_cm.__exit__ = MagicMock(side_effect=LockNotOwnedError("Lock expired"))
        return lock_cm


    def test_restart_dyno_handles_expired_lock(self):
        dyno = make_dyno()
        with patch.object(cache, "lock", return_value=self._expired_lock()):
            with patch.object(dyno, "call_heroku_api", return_value=_mock_response(202)):
                with patch.object(dyno, "stop_continuous_autoscale"):
                    result = dyno.restart_dyno()
        # Should proceed to API call and succeed despite lock expiry
        self.assertTrue(result)

    def test_restart_handles_lock_not_owned_error(self):
        dyno = make_dyno()
        with patch.object(cache, "lock", return_value=self._expired_lock_lno()):
            with patch.object(dyno, "call_heroku_api", return_value=_mock_response(202)):
                with patch.object(dyno, "stop_continuous_autoscale"):
                    result = dyno.restart_dyno()
        self.assertTrue(result)

    def test_upscale_handles_expired_lock(self):
        dyno = make_dyno(formation_size="standard-2x")
        dyno.__dict__["max_dyno_size"] = "performance-l"
        with patch.object(cache, "lock", return_value=self._expired_lock()):
            with patch.object(type(dyno), "is_upscaling",
                              new_callable=PropertyMock, return_value=False):
                with patch.object(dyno, "set_upscaling"):
                    with patch.object(dyno, "call_heroku_api", return_value=_mock_response(200)) as mock_api:
                        with patch.object(dyno, "set_original_formation_size"):
                            with patch.object(dyno, "_update_formation_size"):
                                dyno.upscale_formation_to_next_level()
        mock_api.assert_not_called()

    def test_downscale_handles_expired_lock(self):
        dyno = make_dyno(formation_size="standard-2x")
        cache.set("heroku:formation:normal_worker:previous_size", "standard-1x", timeout=300)
        cache.set("heroku:formation:normal_worker:original_size", "standard-1x", timeout=300)
        with patch.object(type(dyno), "previous_formation_size",
                          new_callable=PropertyMock, return_value="standard-1x"):
            with patch.object(type(dyno), "original_formation_size",
                              new_callable=PropertyMock, return_value="standard-1x"):
                with patch.object(type(dyno), "is_on_original_formation_size_or_lower",
                                  new_callable=PropertyMock, return_value=False):
                    with patch.object(type(dyno), "is_downscaling",
                                      new_callable=PropertyMock, return_value=False):
                        with _authorized_downscale(dyno):
                            with patch.object(cache, "lock", return_value=self._expired_lock()):
                                with patch.object(dyno, "call_heroku_api", return_value=_mock_response(200)) as mock_api:
                                    with patch.object(dyno, "_update_formation_size"):
                                        with patch.object(dyno, "clear_upscaling"):
                                            with patch.object(dyno, "clear_original_formation_size"):
                                                dyno.downscale_formation_to_original_size()
        mock_api.assert_not_called()

    def test_upscale_does_not_patch_when_lock_acquisition_fails(self):
        from heroku_manager.heroku import _NotAcquired

        dyno = make_dyno(formation_size="standard-2x")
        with patch.object(cache, "lock", side_effect=_NotAcquired("not acquired")), \
                patch.object(dyno, "call_heroku_api") as mock_api:
            dyno.upscale_formation_to_next_level()
        mock_api.assert_not_called()
        self.assertIsNone(cache.get(dyno.upscaling_cache_key))

    def test_downscale_does_not_patch_when_lock_acquisition_fails(self):
        from heroku_manager.heroku import _NotAcquired

        dyno = make_dyno(formation_size="performance-m")
        cache.set(dyno.original_size_cache_key, {"size": "standard-2x"}, timeout=None)
        with _authorized_downscale(dyno), \
                patch.object(cache, "lock", side_effect=_NotAcquired("not acquired")), \
                patch.object(dyno, "call_heroku_api") as mock_api:
            dyno.downscale_formation_to_original_size()
        mock_api.assert_not_called()
        self.assertIsNone(cache.get(dyno.downscale_cache_key))

    def test_zombie_check_handles_expired_lock(self):
        dyno = make_dyno()
        with patch.object(cache, "lock", return_value=self._expired_lock()):
            with patch_index_backend({}):
                # Should not raise
                dyno.check_for_sibling_zombie_dynos()

    def test_upscale_skips_api_when_already_upscaling_and_lock_expires(self):
        """If is_upscaling is True inside the lock body and __exit__ raises
        NotAcquired, the upscale must NOT proceed to the API call."""
        dyno = make_dyno(formation_size="standard-2x")
        dyno.__dict__["max_dyno_size"] = "performance-l"
        with patch.object(cache, "lock", return_value=self._expired_lock()):
            with patch.object(type(dyno), "is_upscaling",
                              new_callable=PropertyMock, return_value=True):
                with patch.object(dyno, "call_heroku_api") as mock_api:
                    dyno.upscale_formation_to_next_level()
        mock_api.assert_not_called()

    def test_downscale_skips_api_when_already_at_original_and_lock_expires(self):
        """If is_on_original_formation_size_or_lower is True inside the lock
        body and __exit__ raises, the downscale must NOT proceed."""
        dyno = make_dyno(formation_size="standard-2x")
        with patch.object(type(dyno), "original_formation_size",
                          new_callable=PropertyMock, return_value="standard-1x"):
            with patch.object(type(dyno), "is_on_original_formation_size_or_lower",
                              new_callable=PropertyMock, return_value=True):
                with patch.object(dyno, "clear_original_formation_size"):
                    with patch.object(cache, "lock", return_value=self._expired_lock()):
                        with patch.object(dyno, "call_heroku_api") as mock_api:
                            dyno.downscale_formation_to_original_size()
        mock_api.assert_not_called()

    def test_restart_skips_api_when_already_in_progress_and_lock_expires(self):
        """If restart is already in progress and __exit__ raises, the restart
        must NOT proceed to the API call."""
        dyno = make_dyno()
        restart_key = f'heroku:restart_dyno:{dyno.dyno_name}'
        cache.set(restart_key, True, timeout=300)
        with patch.object(cache, "lock", return_value=self._expired_lock()):
            with patch.object(dyno, "call_heroku_api") as mock_api:
                result = dyno.restart_dyno()
        mock_api.assert_not_called()
        self.assertFalse(result)


if __name__ == "__main__":
    unittest.main()
