"""
Tests: upscale, downscale, restart, autoscale orchestration.
Uses patch context managers throughout to avoid PropertyMock class-level leakage.
"""
import unittest
from unittest.mock import patch, MagicMock, PropertyMock
from tests.conftest import make_dyno, BaseLockTestCase
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

    def test_skips_when_no_original_formation_size(self):
        dyno = make_dyno(formation_size="performance-m")
        with patch.object(dyno, "call_heroku_api") as mock_api:
            dyno.downscale_formation_to_original_size()
        mock_api.assert_not_called()

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
        self.assertEqual(dyno.original_formation_size, "standard-2x")

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


if __name__ == "__main__":
    unittest.main()
