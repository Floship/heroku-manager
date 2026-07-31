"""
Tests: upscale, downscale, restart, autoscale orchestration.
Uses patch context managers throughout to avoid PropertyMock class-level leakage.
"""
import math
import unittest
from unittest.mock import patch, MagicMock, PropertyMock
from tests.conftest import make_dyno, BaseLockTestCase, patch_cache_keys
from django.core.cache import cache


def _mock_response(status=200):
    resp = MagicMock()
    resp.status_code = status
    resp.text = "ok"
    return resp


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


    def test_skips_when_no_previous_formation_size(self):
        dyno = make_dyno(formation_size="standard-1x")
        with patch.object(dyno, "call_heroku_api") as mock_api:
            dyno.downscale_formation_to_original_size()
        mock_api.assert_not_called()

    def test_falls_back_to_previous_tier_when_no_original_formation_size(self):
        dyno = make_dyno(formation_size="performance-m")
        with patch.object(dyno, "call_heroku_api", return_value=_mock_response(200)) as mock_api:
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
        with patch.object(dyno, "call_heroku_api", return_value=_mock_response(200)):
            with patch.object(dyno, "stop_continuous_autoscale"):
                dyno.downscale_formation_to_original_size()
        self.assertEqual(dyno._formation_size_cached[0], "standard-2x")

    def test_skips_when_already_downscaling(self):
        dyno = make_dyno(formation_size="performance-m")
        cache.set(dyno.original_size_cache_key, {"size": "standard-2x"}, timeout=None)
        dyno.set_downscaling()
        with patch.object(dyno, "call_heroku_api") as mock_api:
            dyno.downscale_formation_to_original_size()
        mock_api.assert_not_called()

    def test_downscale_api_failure_logs_error(self):
        dyno = make_dyno(formation_size="performance-m")
        cache.set(dyno.original_size_cache_key, {"size": "standard-2x"}, timeout=None)
        with patch.object(dyno, "call_heroku_api", return_value=_mock_response(500)):
            with patch("heroku_manager.heroku.logger") as mock_log:
                dyno.downscale_formation_to_original_size()
        mock_log.error.assert_called()


class TestCheckAndDownscale(BaseLockTestCase):


    def test_calls_downscale_when_no_upscale_until_key_and_memory_is_cool(self):
        """When upscale_until expired AND memory is OK → downscale allowed."""
        dyno = make_dyno(formation_size="performance-m")
        cache.set(dyno.original_size_cache_key, {"size": "standard-2x"}, timeout=None)
        with patch.object(type(dyno), "allow_downscale",
                          new_callable=PropertyMock, return_value=True):
            with patch.object(dyno, "downscale_formation_to_original_size") as mock_down:
                dyno.check_and_downscale_to_original_formation_size()
        mock_down.assert_called_once()

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
        self.assertAlmostEqual(dyno._downscale_memory_threshold, expected_threshold, places=1)

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
        cache.set("heroku:dyno_alive:normal_worker.2", "now", timeout=60)
        with patch.object(dyno, "call_heroku_api", return_value=_mock_response(202)):
            dyno.restart_dyno("normal_worker.2")
        self.assertIsNone(cache.get("heroku:dyno_alive:normal_worker.2"))

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


class TestAutoscale(BaseLockTestCase):


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
                    dyno.autoscale(continuous=False)
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
        with patch.object(type(dyno), "remote_monitoring",
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
        with patch.object(type(dyno), "remote_monitoring",
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

        with patch_cache_keys(self._store):
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

        with patch_cache_keys(self._store):
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

        with patch_cache_keys(self._store):
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

        with patch_cache_keys(self._store):
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

        with patch_cache_keys(self._store):
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
        with patch.object(dyno, "call_heroku_api", return_value=_mock_response(200)) as mock_api:
            dyno.downscale_formation_to_original_size()
        mock_api.assert_called_once()
        self.assertEqual(mock_api.call_args[1]["data"]["size"], "standard-1x")

    def test_downscale_perf_m_falls_back_to_standard_2x(self):
        dyno = make_dyno(formation_size="performance-m")
        with patch.object(dyno, "call_heroku_api", return_value=_mock_response(200)) as mock_api:
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
                            with patch.object(type(dyno), "any_sibling_still_high_memory",
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

    def _seed_rising_history(self, dyno, start=10000, step=200, count=8):
        """Seed a steadily rising memory history."""
        import time as _time
        now = _time.time()
        history = [
            (now - (count - i) * 30, start + i * step)
            for i in range(count)
        ]
        cache.set(dyno.memory_history_cache_key, history, timeout=600)

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
        with patch.object(type(dyno), "current_memory_usage",
                          new_callable=PropertyMock, return_value=15000):
            with patch.object(type(dyno), "detected_r14",
                              new_callable=PropertyMock, return_value=False):
                with patch.object(type(dyno), "detected_r15",
                                  new_callable=PropertyMock, return_value=False):
                    with patch.object(type(dyno), "no_tasks_in_queue",
                                      new_callable=PropertyMock, return_value=True):
                        with patch.object(type(dyno), "any_sibling_still_high_memory",
                                          new_callable=PropertyMock, return_value=False):
                            with patch.object(type(dyno), "is_formation_idle",
                                              new_callable=PropertyMock, return_value=True):
                                self.assertTrue(dyno.allow_downscale)

    def test_allow_downscale_false_when_memory_stable_but_r15(self):
        """Stable memory with active R15 means the dyno is still OOM — don't downscale."""
        dyno = make_dyno(formation_size="performance-l-ram")
        cache.set(dyno.original_size_cache_key, {"size": "standard-1x"}, timeout=None)
        self._seed_stable_history(dyno, base_mem=30000)
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
                            with patch.object(type(dyno), "any_sibling_still_high_memory",
                                              new_callable=PropertyMock, return_value=False):
                                self.assertFalse(dyno.allow_downscale)

    def test_allow_downscale_false_when_memory_still_rising(self):
        """Rising memory means heavy work is still happening — don't downscale."""
        dyno = make_dyno(formation_size="performance-l-ram")
        cache.set(dyno.original_size_cache_key, {"size": "standard-1x"}, timeout=None)
        self._seed_rising_history(dyno, start=10000, step=200)
        with patch.object(type(dyno), "current_memory_usage",
                          new_callable=PropertyMock, return_value=11400):
            with patch.object(type(dyno), "detected_r14",
                              new_callable=PropertyMock, return_value=False):
                with patch.object(type(dyno), "detected_r15",
                                  new_callable=PropertyMock, return_value=False):
                    with patch.object(type(dyno), "no_tasks_in_queue",
                                      new_callable=PropertyMock, return_value=True):
                        with patch.object(type(dyno), "any_sibling_still_high_memory",
                                          new_callable=PropertyMock, return_value=False):
                            self.assertFalse(dyno.allow_downscale)

    def test_allow_downscale_false_when_already_at_original_size(self):
        """Stability override should not fire when already at original size."""
        dyno = make_dyno(formation_size="standard-1x")
        cache.set(dyno.original_size_cache_key, {"size": "standard-1x"}, timeout=None)
        self._seed_stable_history(dyno, base_mem=300)
        with patch.object(type(dyno), "current_memory_usage",
                          new_callable=PropertyMock, return_value=300):
            with patch.object(type(dyno), "detected_r14",
                              new_callable=PropertyMock, return_value=False):
                with patch.object(type(dyno), "detected_r15",
                                  new_callable=PropertyMock, return_value=False):
                    with patch.object(type(dyno), "no_tasks_in_queue",
                                      new_callable=PropertyMock, return_value=True):
                        with patch.object(type(dyno), "any_sibling_still_high_memory",
                                          new_callable=PropertyMock, return_value=False):
                            # Normal path should handle this (memory IS below threshold)
                            result = dyno.allow_downscale
                            # Should be True via normal path, not stability override
                            self.assertTrue(result)

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
        with patch_cache_keys(memory_store):
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
        with patch_cache_keys(memory_store):
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
        with patch_cache_keys(memory_store):
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
        with patch.object(dyno, "call_heroku_api", return_value=_mock_response(200)):
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
        with patch.object(type(dyno), "current_memory_usage",
                          new_callable=PropertyMock, return_value=15000):
            with patch.object(type(dyno), "detected_r14",
                              new_callable=PropertyMock, return_value=False):
                with patch.object(type(dyno), "detected_r15",
                                  new_callable=PropertyMock, return_value=False):
                    with patch.object(type(dyno), "any_sibling_still_high_memory",
                                      new_callable=PropertyMock, return_value=False):
                        with patch.object(type(dyno), "is_formation_idle",
                                          new_callable=PropertyMock, return_value=True):
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
                                with patch.object(type(dyno), "any_sibling_still_high_memory",
                                                  new_callable=PropertyMock, return_value=False):
                                    with patch.object(type(dyno), "is_formation_idle",
                                                      new_callable=PropertyMock, return_value=True):
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
                                with patch.object(type(dyno), "any_sibling_still_high_memory",
                                                  new_callable=PropertyMock, return_value=False):
                                    with patch.object(type(dyno), "is_formation_idle",
                                                      new_callable=PropertyMock, return_value=True):
                                        with patch.object(dyno, "downscale_formation_to_original_size") as mock_down:
                                            with patch.object(dyno, "restart_dyno") as mock_restart:
                                                dyno.check_and_downscale_to_original_formation_size()
        mock_down.assert_not_called()

    # --- remove_dyno_from_alive_cache cleans stable key ---

    def test_remove_dyno_cleans_stable_key(self):
        dyno = make_dyno("normal_worker.1")
        cache.set(f"heroku:dyno_memory_stable:{dyno.dyno_name}", True, timeout=300)
        cache.set(f"heroku:dyno_memory:{dyno.dyno_name}", 15000, timeout=300)
        cache.set(f"heroku:dyno_alive:{dyno.dyno_name}", "now", timeout=300)
        cache.set(f"heroku:dyno_load:{dyno.dyno_name}", 0.5, timeout=300)
        dyno.remove_dyno_from_alive_cache()
        self.assertIsNone(cache.get(f"heroku:dyno_memory_stable:{dyno.dyno_name}"))
        self.assertIsNone(cache.get(f"heroku:dyno_memory:{dyno.dyno_name}"))
        self.assertIsNone(cache.get(f"heroku:dyno_alive:{dyno.dyno_name}"))
        self.assertIsNone(cache.get(f"heroku:dyno_load:{dyno.dyno_name}"))

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
        with patch.object(type(dyno), "avg_load_1min",
                          new_callable=PropertyMock, return_value=0.3):
            with patch_cache_keys({}):
                self.assertTrue(dyno.is_formation_idle)

    def test_idle_false_when_own_load_above_threshold(self):
        dyno = make_dyno(formation_size="performance-l-ram")
        with patch.object(type(dyno), "avg_load_1min",
                          new_callable=PropertyMock, return_value=1.5):
            self.assertFalse(dyno.is_formation_idle)

    def test_idle_false_when_sibling_load_above_threshold(self):
        dyno = make_dyno(formation_size="performance-l-ram")
        cache.set("heroku:dyno_load:normal_worker.2", 2.3, timeout=300)
        load_store = {
            f"heroku:dyno_load:{dyno.dyno_name}": 0.3,
            "heroku:dyno_load:normal_worker.2": 2.3,
        }
        with patch.object(type(dyno), "avg_load_1min",
                          new_callable=PropertyMock, return_value=0.3):
            with patch_cache_keys(load_store):
                self.assertFalse(dyno.is_formation_idle)

    def test_idle_true_when_all_siblings_below_threshold(self):
        dyno = make_dyno(formation_size="performance-l-ram")
        cache.set("heroku:dyno_load:normal_worker.2", 0.4, timeout=300)
        load_store = {
            f"heroku:dyno_load:{dyno.dyno_name}": 0.3,
            "heroku:dyno_load:normal_worker.2": 0.4,
        }
        with patch.object(type(dyno), "avg_load_1min",
                          new_callable=PropertyMock, return_value=0.3):
            with patch_cache_keys(load_store):
                self.assertTrue(dyno.is_formation_idle)

    def test_idle_at_exact_threshold_is_not_idle(self):
        """Load at exactly 1.0 should be considered NOT idle (>= threshold)."""
        dyno = make_dyno(formation_size="performance-l-ram")
        with patch.object(type(dyno), "avg_load_1min",
                          new_callable=PropertyMock, return_value=1.0):
            self.assertFalse(dyno.is_formation_idle)

    def test_allow_downscale_false_when_stable_but_load_high(self):
        """Memory stabilized + high load = heavy task still running. Don't downscale."""
        dyno = make_dyno(formation_size="performance-l-ram")
        cache.set(dyno.original_size_cache_key, {"size": "standard-1x"}, timeout=None)
        from tests.test_scale_operations import TestMemoryStabilityDetection
        # Reuse the seeding helper via a fresh instance
        helper = TestMemoryStabilityDetection()
        helper._seed_stable_history(dyno, base_mem=15000)
        with patch.object(type(dyno), "current_memory_usage",
                          new_callable=PropertyMock, return_value=15000):
            with patch.object(type(dyno), "detected_r14",
                              new_callable=PropertyMock, return_value=False):
                with patch.object(type(dyno), "detected_r15",
                                  new_callable=PropertyMock, return_value=False):
                    with patch.object(type(dyno), "no_tasks_in_queue",
                                      new_callable=PropertyMock, return_value=True):
                        with patch.object(type(dyno), "any_sibling_still_high_memory",
                                          new_callable=PropertyMock, return_value=False):
                            with patch.object(type(dyno), "is_formation_idle",
                                              new_callable=PropertyMock, return_value=False):
                                self.assertFalse(dyno.allow_downscale)

    def test_allow_downscale_true_when_stable_and_load_settled(self):
        """Memory stabilized + low load = work finished. Allow downscale."""
        dyno = make_dyno(formation_size="performance-l-ram")
        cache.set(dyno.original_size_cache_key, {"size": "standard-1x"}, timeout=None)
        from tests.test_scale_operations import TestMemoryStabilityDetection
        helper = TestMemoryStabilityDetection()
        helper._seed_stable_history(dyno, base_mem=15000)
        with patch.object(type(dyno), "current_memory_usage",
                          new_callable=PropertyMock, return_value=15000):
            with patch.object(type(dyno), "detected_r14",
                              new_callable=PropertyMock, return_value=False):
                with patch.object(type(dyno), "detected_r15",
                                  new_callable=PropertyMock, return_value=False):
                    with patch.object(type(dyno), "no_tasks_in_queue",
                                      new_callable=PropertyMock, return_value=True):
                        with patch.object(type(dyno), "any_sibling_still_high_memory",
                                          new_callable=PropertyMock, return_value=False):
                            with patch.object(type(dyno), "is_formation_idle",
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
        from django.utils import timezone as tz
        from django.core.cache.backends.locmem import LocMemCache
        until = tz.now() + tz.timedelta(seconds=60)
        cache.set(dyno.upscale_until_cache_key, until, timeout=60)
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
                                with patch.object(type(dyno), "any_sibling_still_high_memory",
                                                  new_callable=PropertyMock, return_value=False):
                                    with patch.object(type(dyno), "is_formation_idle",
                                                      new_callable=PropertyMock, return_value=False):
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
                    with patch.object(dyno, "call_heroku_api", return_value=_mock_response(200)):
                        with patch.object(dyno, "set_original_formation_size"):
                            with patch.object(dyno, "_update_formation_size"):
                                # Should not raise
                                dyno.upscale_formation_to_next_level()

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
                        with patch.object(dyno, "set_downscaling"):
                            with patch.object(cache, "lock", return_value=self._expired_lock()):
                                with patch.object(dyno, "call_heroku_api", return_value=_mock_response(200)):
                                    with patch.object(dyno, "_update_formation_size"):
                                        with patch.object(dyno, "clear_upscaling"):
                                            with patch.object(dyno, "clear_original_formation_size"):
                                                # Should not raise
                                                dyno.downscale_formation_to_original_size()

    def test_zombie_check_handles_expired_lock(self):
        dyno = make_dyno()
        with patch.object(cache, "lock", return_value=self._expired_lock()):
            with patch_cache_keys({}):
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
