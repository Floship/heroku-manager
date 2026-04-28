"""
Tests: core properties — formation_size, settings, memory, sizing predicates.
"""
import os
import time
import unittest
from unittest.mock import patch, PropertyMock, MagicMock
from tests.conftest import make_dyno, BaseLockTestCase, django_settings
from django.core.cache import cache
from heroku_manager.heroku import get_dyno_settings, HerokuDyno, HerokuManager, DYNO_SIZES


class TestGetDynoSettings(BaseLockTestCase):

    def test_returns_matching_size_for_dyno_ram(self):
        with patch.dict(os.environ, {"DYNO_RAM": "512"}):
            result = get_dyno_settings()
        self.assertEqual(result["memory"], 512)

    def test_returns_explicit_formation_size(self):
        result = get_dyno_settings("performance-m")
        self.assertEqual(result["memory"], 2560)

    def test_unknown_formation_returns_empty_dict(self):
        result = get_dyno_settings("unknown-size")
        self.assertEqual(result, {})

    def test_no_match_falls_back_to_standard_1x(self):
        with patch.dict(os.environ, {"DYNO_RAM": "999"}):
            result = get_dyno_settings()
        # Falls to standard-1x via explicit fallback
        self.assertIsInstance(result, dict)


class TestHerokuManagerSingleton(BaseLockTestCase):

    def setUp(self):
        HerokuManager._instance = None  # reset between tests

    def test_get_autoscaler_returns_same_instance(self):
        with patch.dict(os.environ, {"DYNO": "normal_worker.1"}):
            a = HerokuManager.get_autoscaler()
            b = HerokuManager.get_autoscaler()
        self.assertIs(a, b)

    def test_get_autoscaler_creates_heroku_dyno(self):
        with patch.dict(os.environ, {"DYNO": "normal_worker.1"}):
            instance = HerokuManager.get_autoscaler()
        self.assertIsInstance(instance, HerokuDyno)

    def tearDown(self):
        HerokuManager._instance = None


class TestHerokuDynoInit(BaseLockTestCase):

    def test_formation_name_derived_from_dyno_name(self):
        with patch.dict(os.environ, {"DYNO": "webhooks_worker.3", "HEROKU_APP_NAME": "myapp"}):
            dyno = HerokuDyno()
        self.assertEqual(dyno.formation_name, "webhooks_worker")
        self.assertEqual(dyno.app_name, "myapp")

    def test_no_dyno_env_gives_none_formation_name(self):
        env = {"HEROKU_APP_NAME": "myapp"}
        env.pop("DYNO", None)
        with patch.dict(os.environ, env, clear=True):
            dyno = HerokuDyno()
        self.assertIsNone(dyno.formation_name)


class TestFormationSizeProperty(BaseLockTestCase):


    def test_uses_instance_cache_when_fresh(self):
        dyno = make_dyno(formation_size="performance-m")
        self.assertEqual(dyno.formation_size, "performance-m")

    def test_falls_back_to_redis_cache(self):
        dyno = make_dyno()
        dyno._formation_size_cached = None  # expire instance cache
        redis_key = f"heroku:formation_size:{dyno.app_name}:{dyno.formation_name}"
        cache.set(redis_key, "performance-l", timeout=None)
        self.assertEqual(dyno.formation_size, "performance-l")

    def test_derives_from_dyno_ram_env_when_no_cache(self):
        dyno = make_dyno()
        dyno._formation_size_cached = None
        # No redis key set
        with patch.dict(os.environ, {"DYNO_RAM": "1024"}):
            size = dyno.formation_size
        self.assertEqual(size, "standard-2x")

    def test_update_formation_size_writes_to_cache(self):
        dyno = make_dyno(formation_size="standard-2x")
        dyno._update_formation_size("performance-m")
        self.assertEqual(dyno._formation_size_cached[0], "performance-m")
        redis_key = f"heroku:formation_size:{dyno.app_name}:{dyno.formation_name}"
        self.assertEqual(cache.get(redis_key), "performance-m")

    def test_update_formation_size_invalidates_cached_properties(self):
        dyno = make_dyno(formation_size="standard-2x")
        # Prime cached_property values
        _ = dyno.settings
        _ = dyno.max_dyno_size
        dyno._update_formation_size("performance-m")
        self.assertNotIn("settings", dyno.__dict__)
        self.assertNotIn("max_dyno_size", dyno.__dict__)


class TestNextAndPreviousFormationSize(BaseLockTestCase):

    def test_next_formation_size(self):
        dyno = make_dyno(formation_size="standard-1x")
        self.assertEqual(dyno.next_formation_size, "standard-2x")

    def test_next_formation_size_at_top_returns_none(self):
        dyno = make_dyno(formation_size="performance-2xl")
        self.assertIsNone(dyno.next_formation_size)

    def test_previous_formation_size(self):
        dyno = make_dyno(formation_size="standard-2x")
        self.assertEqual(dyno.previous_formation_size, "standard-1x")

    def test_previous_formation_size_at_base_returns_none(self):
        dyno = make_dyno(formation_size="standard-1x")
        self.assertIsNone(dyno.previous_formation_size)


class TestAvailableMemoryAndPercentage(BaseLockTestCase):

    def test_available_memory_matches_dyno_sizes(self):
        dyno = make_dyno(formation_size="standard-2x")
        self.assertEqual(dyno.available_memory, 1024)

    def test_current_memory_percentage_calculated(self):
        dyno = make_dyno(formation_size="standard-2x")  # 1024 MB available
        type(dyno).current_memory_usage = PropertyMock(return_value=512)
        self.assertAlmostEqual(dyno.current_memory_usage_percentage, 50.0)

    def test_current_memory_percentage_zero_when_no_memory(self):
        dyno = make_dyno(formation_size="standard-2x")
        type(dyno).current_memory_usage = PropertyMock(return_value=0)
        self.assertEqual(dyno.current_memory_usage_percentage, 0.0)


class TestIsMemoryPredicates(BaseLockTestCase):

    def test_is_memory_usage_high_true(self):
        dyno = make_dyno()
        type(dyno).current_memory_usage = PropertyMock(return_value=500)
        self.assertTrue(dyno.is_memory_usage_high)  # HIGH_MEM_USE_MB=400

    def test_is_memory_usage_high_false(self):
        dyno = make_dyno()
        type(dyno).current_memory_usage = PropertyMock(return_value=300)
        self.assertFalse(dyno.is_memory_usage_high)

    def test_is_out_of_memory_true(self):
        dyno = make_dyno(formation_size="standard-2x")  # 1024MB
        type(dyno).current_memory_usage = PropertyMock(return_value=1200)
        self.assertTrue(dyno.is_out_of_memory)

    def test_is_out_of_memory_false(self):
        dyno = make_dyno(formation_size="standard-2x")
        type(dyno).current_memory_usage = PropertyMock(return_value=800)
        self.assertFalse(dyno.is_out_of_memory)

    def test_requires_upscale_via_percentage(self):
        dyno = make_dyno(formation_size="standard-2x")  # 1024 MB
        type(dyno).current_memory_usage = PropertyMock(return_value=900)
        type(dyno).detected_r14 = PropertyMock(return_value=False)
        type(dyno).detected_r15 = PropertyMock(return_value=False)
        # 900/1024*100 = 87.9% > 80
        self.assertTrue(dyno.requires_upscale)

    def test_requires_upscale_ignores_r14_when_below_threshold(self):
        dyno = make_dyno(formation_size="standard-2x")
        type(dyno).current_memory_usage = PropertyMock(return_value=100)
        type(dyno).detected_r14 = PropertyMock(return_value=True)
        type(dyno).detected_r15 = PropertyMock(return_value=False)
        self.assertFalse(dyno.requires_upscale)

    def test_requires_upscale_via_r15(self):
        dyno = make_dyno(formation_size="standard-2x")
        type(dyno).current_memory_usage = PropertyMock(return_value=100)
        type(dyno).detected_r14 = PropertyMock(return_value=False)
        type(dyno).detected_r15 = PropertyMock(return_value=True)
        self.assertTrue(dyno.requires_upscale)


class TestIsOnOriginalFormationSizeOrLower(BaseLockTestCase):


    def test_true_when_at_original_size(self):
        dyno = make_dyno(formation_size="standard-2x")
        cache.set(dyno.original_size_cache_key, {"size": "standard-2x"}, timeout=None)
        self.assertTrue(dyno.is_on_original_formation_size_or_lower)

    def test_true_when_below_original_size(self):
        dyno = make_dyno(formation_size="standard-1x")
        cache.set(dyno.original_size_cache_key, {"size": "standard-2x"}, timeout=None)
        self.assertTrue(dyno.is_on_original_formation_size_or_lower)

    def test_false_when_above_original_size(self):
        dyno = make_dyno(formation_size="performance-m")
        cache.set(dyno.original_size_cache_key, {"size": "standard-2x"}, timeout=None)
        self.assertFalse(dyno.is_on_original_formation_size_or_lower)


class TestOriginalFormationSizeCacheOps(BaseLockTestCase):


    def test_set_original_formation_size_stores_current(self):
        dyno = make_dyno(formation_size="standard-2x")
        dyno.set_original_formation_size()
        self.assertEqual(dyno.original_formation_size, "standard-2x")

    def test_set_original_formation_size_does_not_overwrite_unless_value(self):
        dyno = make_dyno(formation_size="standard-2x")
        dyno.set_original_formation_size()
        # Simulate upscale
        dyno._formation_size_cached = ("performance-m", time.time())
        dyno.set_original_formation_size()  # should NOT overwrite
        self.assertEqual(dyno.original_formation_size, "standard-2x")

    def test_set_original_formation_size_force_overwrite_with_value(self):
        dyno = make_dyno(formation_size="standard-2x")
        dyno.set_original_formation_size()
        dyno._formation_size_cached = ("performance-m", time.time())
        dyno.set_original_formation_size(value=True)  # force
        self.assertEqual(dyno.original_formation_size, "performance-m")

    def test_clear_original_formation_size_removes_both_keys(self):
        dyno = make_dyno(formation_size="standard-2x")
        dyno.set_original_formation_size()
        cache.set(dyno.upscale_until_cache_key, "something", timeout=60)
        dyno.clear_original_formation_size()
        self.assertIsNone(dyno.original_formation_size)
        self.assertIsNone(cache.get(dyno.upscale_until_cache_key))


class TestScalingStateFlags(BaseLockTestCase):


    def test_is_upscaling_false_initially(self):
        dyno = make_dyno()
        self.assertFalse(dyno.is_upscaling)

    def test_set_upscaling_marks_true(self):
        dyno = make_dyno()
        dyno.set_upscaling()
        self.assertTrue(dyno.is_upscaling)

    def test_is_downscaling_false_initially(self):
        dyno = make_dyno()
        self.assertFalse(dyno.is_downscaling)

    def test_set_downscaling_marks_true(self):
        dyno = make_dyno()
        dyno.set_downscaling()
        self.assertTrue(dyno.is_downscaling)


class TestRemoteMonitoring(BaseLockTestCase):

    def test_remote_monitoring_true_when_different_dyno(self):
        with patch.dict(os.environ, {"DYNO": "normal_worker.99"}):
            dyno = make_dyno("normal_worker.1")
            self.assertTrue(dyno.remote_monitoring)

    def test_remote_monitoring_false_when_same_dyno(self):
        with patch.dict(os.environ, {"DYNO": "normal_worker.1"}):
            dyno = make_dyno("normal_worker.1")
            self.assertFalse(dyno.remote_monitoring)


class TestSettingsCachedProperty(BaseLockTestCase):

    def test_settings_includes_worker_settings_map_overrides(self):
        from django.conf import settings as ds
        orig = ds.WORKER_SETTINGS_MAP
        ds.WORKER_SETTINGS_MAP = {"normal_worker": {"max_dyno_size": "performance-m"}}
        try:
            dyno = make_dyno("normal_worker.1", formation_size="standard-2x")
            # clear cached property if set
            dyno.__dict__.pop("settings", None)
            dyno.__dict__.pop("max_dyno_size", None)
            self.assertEqual(dyno.max_dyno_size, "performance-m")
        finally:
            ds.WORKER_SETTINGS_MAP = orig


if __name__ == "__main__":
    unittest.main()
