"""Scenario-style autoscale tests for repeated memory spike patterns.

These tests simulate real production shapes rather than isolated predicates:
- slow/gradual rise
- rapid rise straight to R15
- Heroku kill followed by low-memory restart
- rapid rise again while still in the keep-upscaled window
- second spike after a successful downscale
"""

from unittest.mock import PropertyMock, patch

from datetime import timedelta

from django.core.cache import cache
from django.core.cache.backends.locmem import LocMemCache
from django.utils import timezone

from tests.conftest import BaseLockTestCase, make_dyno


def _set_upscaled_window(dyno, original_size="standard-2x", ttl=600):
    until = timezone.now() + timedelta(seconds=ttl)
    cache.set(dyno.original_size_cache_key, {"size": original_size}, timeout=None)
    cache.set(dyno.upscale_until_cache_key, until, timeout=ttl)
    return until


class TestMemorySpikeScenarios(BaseLockTestCase):

    def test_gradual_rise_below_threshold_does_not_upscale(self):
        dyno = make_dyno(formation_size="standard-2x")
        with patch.object(type(dyno), "current_memory_usage_percentage",
                          new_callable=PropertyMock, return_value=79.0):
            with patch.object(type(dyno), "detected_r15",
                              new_callable=PropertyMock, return_value=False):
                self.assertFalse(dyno.requires_upscale)

    def test_gradual_rise_past_threshold_triggers_upscale(self):
        dyno = make_dyno(formation_size="standard-2x")
        with patch.object(type(dyno), "requires_upscale",
                          new_callable=PropertyMock, return_value=True):
            with patch.object(dyno, "upscale_formation_to_next_level") as mock_up:
                with patch.object(dyno, "check_and_downscale_to_original_formation_size") as mock_down:
                    dyno.autoscale(continuous=False)
        mock_up.assert_called_once()
        mock_down.assert_not_called()

    def test_rapid_r15_spike_triggers_upscale_even_below_percent_threshold(self):
        dyno = make_dyno(formation_size="standard-2x")
        with patch.object(type(dyno), "current_memory_usage_percentage",
                          new_callable=PropertyMock, return_value=40.0):
            with patch.object(type(dyno), "detected_r15",
                              new_callable=PropertyMock, return_value=True):
                self.assertTrue(dyno.requires_upscale)

    def test_r15_does_not_trigger_second_upscale_during_keep_upscaled_window(self):
        dyno = make_dyno(formation_size="performance-m")
        _set_upscaled_window(dyno, original_size="standard-2x", ttl=600)

        with patch.object(type(dyno), "detected_r15",
                          new_callable=PropertyMock, return_value=True):
            with patch.object(type(dyno), "any_sibling_requires_upscale",
                              new_callable=PropertyMock, return_value=False):
                with patch.object(dyno, "call_heroku_api") as mock_api:
                    dyno.upscale_formation_to_next_level()

        mock_api.assert_not_called()

    def test_r15_kill_then_low_memory_does_not_downscale_while_ttl_is_high(self):
        dyno = make_dyno(formation_size="performance-m")
        _set_upscaled_window(dyno, original_size="standard-2x", ttl=600)
        with patch.object(LocMemCache, "ttl", return_value=600):
            with patch.object(type(dyno), "allow_downscale",
                              new_callable=PropertyMock, return_value=True):
                with patch.object(dyno, "downscale_formation_to_original_size") as mock_down:
                    dyno.check_and_downscale_to_original_formation_size()
        mock_down.assert_not_called()

    def test_r15_kill_then_rapid_rise_again_near_expiry_extends_window(self):
        dyno = make_dyno(formation_size="performance-m")
        original_until = _set_upscaled_window(dyno, original_size="standard-2x", ttl=59)
        with patch.object(LocMemCache, "ttl", return_value=59):
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
        self.assertGreater(cache.get(dyno.upscale_until_cache_key), original_until)

    def test_cool_recovery_near_expiry_downscales(self):
        dyno = make_dyno(formation_size="performance-m")
        _set_upscaled_window(dyno, original_size="standard-2x", ttl=59)
        with patch.object(LocMemCache, "ttl", return_value=59):
            with patch.object(type(dyno), "allow_downscale",
                              new_callable=PropertyMock, return_value=True):
                with patch.object(dyno, "downscale_formation_to_original_size") as mock_down:
                    dyno.check_and_downscale_to_original_formation_size()
        mock_down.assert_called_once()

    def test_second_spike_after_downscale_can_upscale_again(self):
        dyno = make_dyno(formation_size="performance-m")
        cache.set(dyno.original_size_cache_key, {"size": "standard-2x"}, timeout=None)
        cache.set(dyno.upscaling_cache_key, True, timeout=450)

        response = type("Resp", (), {"status_code": 200, "text": "ok"})()

        with patch.object(dyno, "call_heroku_api", return_value=response):
            with patch.object(type(dyno), "remote_monitoring",
                              new_callable=PropertyMock, return_value=True):
                dyno.downscale_formation_to_original_size()

        self.assertIsNone(cache.get(dyno.upscaling_cache_key))

        with patch.object(type(dyno), "current_memory_usage_percentage",
                          new_callable=PropertyMock, return_value=95.0):
            with patch.object(type(dyno), "current_memory_usage",
                              new_callable=PropertyMock, return_value=972.0):
                with patch.object(type(dyno), "remote_monitoring",
                                  new_callable=PropertyMock, return_value=True):
                    with patch.object(dyno, "call_heroku_api", return_value=response) as mock_api:
                        with patch.object(dyno, "stop_continuous_autoscale"):
                            dyno.upscale_formation_to_next_level()
        mock_api.assert_called_once()
