"""
TDD Red Phase: tests for identified gaps in autoscaling logic.

Each test targets a specific untested code path. Tests are expected to
FAIL initially, exposing bugs or missing coverage. The Green phase will
fix the production code to make them pass.

Gaps covered:
1. timezone.timedelta crash on expired-key safety-restore path
2. Upscale with unrecognized size leaves dangling upscaling flag
3. R15 + high memory near TTL expiry → restart branch exercised
4. On-original-formation R14 restart (no upscale_until key)
5. Downscale API failure leaves downscaling flag set (TTL expiry only)
"""

import unittest
from datetime import timedelta
from unittest.mock import patch, MagicMock, PropertyMock

from django.core.cache import cache
from django.core.cache.backends.locmem import LocMemCache
from django.utils import timezone

from tests.conftest import make_dyno, BaseLockTestCase


def _mock_response(status=200):
    resp = MagicMock()
    resp.status_code = status
    resp.text = "ok"
    return resp


# ─── Gap 1: expired-key safety-restore path ────────────────────────────────
class TestExpiredKeyRestoresTimer(BaseLockTestCase):
    """When upscale_until expires but allow_downscale is False,
    the code must restore the timer for DYNO_DOWNSCALE_CHECK_INTERVAL."""

    def test_restores_timer_without_crashing(self):
        """Expired upscale_until + allow_downscale=False should restore timer."""
        dyno = make_dyno(formation_size="performance-m")
        # No upscale_until key in cache — simulates expiry
        cache.set(dyno.original_size_cache_key, {"size": "standard-2x"}, timeout=None)

        with patch.object(type(dyno), "allow_downscale",
                          new_callable=PropertyMock, return_value=False):
            with patch.object(type(dyno), "current_memory_usage",
                              new_callable=PropertyMock, return_value=900.0):
                with patch.object(type(dyno), "current_memory_usage_percentage",
                                  new_callable=PropertyMock, return_value=88.0):
                    with patch.object(type(dyno), "detected_r14",
                                      new_callable=PropertyMock, return_value=False):
                        with patch.object(type(dyno), "detected_r15",
                                          new_callable=PropertyMock, return_value=False):
                            with patch.object(type(dyno), "no_tasks_in_queue",
                                              new_callable=PropertyMock, return_value=True):
                                # This should NOT raise AttributeError
                                dyno.check_and_downscale_to_original_formation_size()

        # Timer must have been restored
        restored = cache.get(dyno.upscale_until_cache_key)
        self.assertIsNotNone(restored, "Timer should be restored when allow_downscale is False")

    def test_restored_timer_has_correct_duration(self):
        """Restored timer should use DYNO_DOWNSCALE_CHECK_INTERVAL seconds."""
        dyno = make_dyno(formation_size="performance-m")
        cache.set(dyno.original_size_cache_key, {"size": "standard-2x"}, timeout=None)

        before = timezone.now()
        with patch.object(type(dyno), "allow_downscale",
                          new_callable=PropertyMock, return_value=False):
            with patch.object(type(dyno), "current_memory_usage",
                              new_callable=PropertyMock, return_value=900.0):
                with patch.object(type(dyno), "current_memory_usage_percentage",
                                  new_callable=PropertyMock, return_value=88.0):
                    with patch.object(type(dyno), "detected_r14",
                                      new_callable=PropertyMock, return_value=False):
                        with patch.object(type(dyno), "detected_r15",
                                          new_callable=PropertyMock, return_value=False):
                            with patch.object(type(dyno), "no_tasks_in_queue",
                                              new_callable=PropertyMock, return_value=True):
                                dyno.check_and_downscale_to_original_formation_size()

        restored = cache.get(dyno.upscale_until_cache_key)
        # Should be approximately now + 60s (DYNO_DOWNSCALE_CHECK_INTERVAL from conftest)
        expected_min = before + timedelta(seconds=55)
        expected_max = before + timedelta(seconds=65)
        self.assertGreaterEqual(restored, expected_min)
        self.assertLessEqual(restored, expected_max)


# ─── Gap 2: Dangling upscaling flag on unrecognized next_formation_size ─────
class TestUpscaleDanglingFlag(BaseLockTestCase):
    """If next_formation_size returns None after set_upscaling() is called,
    the upscaling flag is left dangling until TTL expiry."""

    def test_upscaling_flag_cleared_when_no_next_size(self):
        """Upscaling at the top of the size graph (where next is None)
        but not at max_dyno_size should not leave the upscaling flag set."""
        dyno = make_dyno(formation_size="performance-l")
        # max_dyno_size is performance-2xl, so the max guard doesn't trip
        dyno.__dict__["max_dyno_size"] = "performance-2xl"

        with patch.object(type(dyno), "next_formation_size",
                          new_callable=PropertyMock, return_value=None):
            with patch.object(type(dyno), "current_memory_usage_percentage",
                              new_callable=PropertyMock, return_value=90.0):
                with patch.object(type(dyno), "current_memory_usage",
                                  new_callable=PropertyMock, return_value=3500.0):
                    with patch.object(dyno, "call_heroku_api") as mock_api:
                        dyno.upscale_formation_to_next_level()

        mock_api.assert_not_called()
        # The upscaling flag should NOT be left dangling
        self.assertFalse(dyno.is_upscaling,
                         "Upscaling flag should be cleared when next_formation_size is None")


# ─── Gap 3: R15 + high memory near expiry → restart ────────────────────────
class TestR15HighMemoryNearExpiry(BaseLockTestCase):
    """Near TTL expiry, if allow_downscale is False and memory is still high
    with both R15 and high memory usage, the restart branch fires because
    of the `or is_still_high_memory_usage_for_downscale` clause."""

    def test_r15_plus_high_memory_near_expiry_restarts(self):
        """R15=True + is_still_high_memory_usage=True + no_tasks=True near
        expiry should trigger restart (via the `or` branch), even though
        the `r14 and not r15` part is False."""
        dyno = make_dyno(formation_size="performance-m")
        until = timezone.now() + timedelta(seconds=59)
        cache.set(dyno.upscale_until_cache_key, until, timeout=59)
        cache.set(dyno.original_size_cache_key, {"size": "standard-2x"}, timeout=None)

        with patch.object(LocMemCache, "ttl", return_value=59):
            with patch.object(type(dyno), "allow_downscale",
                              new_callable=PropertyMock, return_value=False):
                with patch.object(type(dyno), "detected_r14",
                                  new_callable=PropertyMock, return_value=False):
                    with patch.object(type(dyno), "detected_r15",
                                      new_callable=PropertyMock, return_value=True):
                        with patch.object(type(dyno), "is_still_high_memory_usage_for_downscale",
                                          new_callable=PropertyMock, return_value=True):
                            with patch.object(type(dyno), "no_tasks_in_queue",
                                              new_callable=PropertyMock, return_value=True):
                                with patch.object(dyno, "restart_dyno") as mock_restart:
                                    dyno.check_and_downscale_to_original_formation_size()

        mock_restart.assert_called_once()

    def test_r15_plus_high_memory_near_expiry_but_tasks_queued_extends(self):
        """Same scenario but with tasks in queue — should extend timer, not restart."""
        dyno = make_dyno(formation_size="performance-m")
        until = timezone.now() + timedelta(seconds=59)
        cache.set(dyno.upscale_until_cache_key, until, timeout=59)
        cache.set(dyno.original_size_cache_key, {"size": "standard-2x"}, timeout=None)

        with patch.object(LocMemCache, "ttl", return_value=59):
            with patch.object(type(dyno), "allow_downscale",
                              new_callable=PropertyMock, return_value=False):
                with patch.object(type(dyno), "detected_r14",
                                  new_callable=PropertyMock, return_value=False):
                    with patch.object(type(dyno), "detected_r15",
                                      new_callable=PropertyMock, return_value=True):
                        with patch.object(type(dyno), "is_still_high_memory_usage_for_downscale",
                                          new_callable=PropertyMock, return_value=True):
                            with patch.object(type(dyno), "no_tasks_in_queue",
                                              new_callable=PropertyMock, return_value=False):
                                with patch.object(type(dyno), "tasks_in_queue",
                                                  new_callable=PropertyMock, return_value=5):
                                    with patch.object(type(dyno), "avg_load_1min",
                                                      new_callable=PropertyMock, return_value=0.5):
                                        with patch.object(type(dyno), "current_memory_usage",
                                                          new_callable=PropertyMock, return_value=900.0):
                                            with patch.object(type(dyno), "current_memory_usage_percentage",
                                                              new_callable=PropertyMock, return_value=88.0):
                                                with patch.object(type(dyno), "available_memory",
                                                                  new_callable=PropertyMock, return_value=1024):
                                                    with patch.object(dyno, "restart_dyno") as mock_restart:
                                                        dyno.check_and_downscale_to_original_formation_size()

        mock_restart.assert_not_called()
        # Timer should have been extended
        new_until = cache.get(dyno.upscale_until_cache_key)
        self.assertGreater(new_until, until)


# ─── Gap 4: On-original-formation R14 restart (no upscale_until key) ───────
class TestOriginalFormationR14Restart(BaseLockTestCase):
    """When no upscale_until key exists, high memory + R14 (no R15) + empty
    queue triggers a restart to clear memory without upscaling."""

    def test_r14_high_memory_no_upscale_key_restarts(self):
        dyno = make_dyno(formation_size="standard-2x")
        # No upscale_until key in cache

        with patch.object(type(dyno), "current_memory_usage_percentage",
                          new_callable=PropertyMock, return_value=110.0):
            with patch.object(type(dyno), "detected_r14",
                              new_callable=PropertyMock, return_value=True):
                with patch.object(type(dyno), "detected_r15",
                                  new_callable=PropertyMock, return_value=False):
                    with patch.object(type(dyno), "no_tasks_in_queue",
                                      new_callable=PropertyMock, return_value=True):
                        with patch.object(dyno, "restart_dyno") as mock_restart:
                            with patch.object(dyno, "downscale_formation_to_original_size") as mock_down:
                                dyno.check_and_downscale_to_original_formation_size()

        mock_restart.assert_called_once()
        mock_down.assert_not_called()

    def test_r14_high_memory_with_r15_does_not_restart(self):
        """R15 present should suppress the R14 restart path."""
        dyno = make_dyno(formation_size="standard-2x")

        with patch.object(type(dyno), "current_memory_usage_percentage",
                          new_callable=PropertyMock, return_value=110.0):
            with patch.object(type(dyno), "detected_r14",
                              new_callable=PropertyMock, return_value=True):
                with patch.object(type(dyno), "detected_r15",
                                  new_callable=PropertyMock, return_value=True):
                    with patch.object(type(dyno), "no_tasks_in_queue",
                                      new_callable=PropertyMock, return_value=True):
                        with patch.object(type(dyno), "allow_downscale",
                                          new_callable=PropertyMock, return_value=False):
                            with patch.object(type(dyno), "current_memory_usage",
                                              new_callable=PropertyMock, return_value=900.0):
                                with patch.object(dyno, "restart_dyno") as mock_restart:
                                    dyno.check_and_downscale_to_original_formation_size()

        mock_restart.assert_not_called()

    def test_r14_high_memory_tasks_in_queue_does_not_restart(self):
        """Tasks in queue should suppress the R14 restart path."""
        dyno = make_dyno(formation_size="standard-2x")

        with patch.object(type(dyno), "current_memory_usage_percentage",
                          new_callable=PropertyMock, return_value=110.0):
            with patch.object(type(dyno), "detected_r14",
                              new_callable=PropertyMock, return_value=True):
                with patch.object(type(dyno), "detected_r15",
                                  new_callable=PropertyMock, return_value=False):
                    with patch.object(type(dyno), "no_tasks_in_queue",
                                      new_callable=PropertyMock, return_value=False):
                        with patch.object(type(dyno), "allow_downscale",
                                          new_callable=PropertyMock, return_value=False):
                            with patch.object(type(dyno), "current_memory_usage",
                                              new_callable=PropertyMock, return_value=900.0):
                                with patch.object(dyno, "restart_dyno") as mock_restart:
                                    dyno.check_and_downscale_to_original_formation_size()

        mock_restart.assert_not_called()


# ─── Gap 5: Downscale API failure leaves downscaling flag set ───────────────
class TestDownscaleFailureLeavesFlag(BaseLockTestCase):
    """On API failure, the downscaling flag is NOT cleared — it must
    expire via TTL. This is intentional but should be explicitly tested."""

    def test_downscale_api_error_keeps_downscaling_flag(self):
        dyno = make_dyno(formation_size="performance-m")
        cache.set(dyno.original_size_cache_key, {"size": "standard-2x"}, timeout=None)

        with patch.object(type(dyno), "remote_monitoring",
                          new_callable=PropertyMock, return_value=True):
            with patch.object(dyno, "call_heroku_api", return_value=_mock_response(500)):
                dyno.downscale_formation_to_original_size()

        self.assertTrue(dyno.is_downscaling,
                        "Downscaling flag should remain set after API failure")

    def test_downscale_api_none_keeps_downscaling_flag(self):
        dyno = make_dyno(formation_size="performance-m")
        cache.set(dyno.original_size_cache_key, {"size": "standard-2x"}, timeout=None)

        with patch.object(type(dyno), "remote_monitoring",
                          new_callable=PropertyMock, return_value=True):
            with patch.object(dyno, "call_heroku_api", return_value=None):
                dyno.downscale_formation_to_original_size()

        self.assertTrue(dyno.is_downscaling,
                        "Downscaling flag should remain set after API None response")


if __name__ == "__main__":
    unittest.main()
