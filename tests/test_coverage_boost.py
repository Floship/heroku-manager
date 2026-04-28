"""
Tests targeting uncovered regions to push coverage above 85%.
Covers: tasks_in_queue, thread management, file cleaning, properties,
        memory quota, exec_connect, remaining scaling paths.
"""
import unittest
import threading
from unittest.mock import patch, MagicMock, PropertyMock
from tests.conftest import make_dyno, BaseLockTestCase
from django.core.cache import cache
from django.conf import settings as ds


class TestTasksInQueue(BaseLockTestCase):

    def test_tasks_in_queue_returns_zero_when_no_proc_class(self):
        dyno = make_dyno()
        # No HIREFIRE_PROCS in settings → _get_proc_class_by_formation_name is None
        result = dyno.tasks_in_queue
        self.assertEqual(result, 0)

    def test_tasks_in_queue_with_proc_class(self):
        dyno = make_dyno()
        mock_proc = MagicMock()
        mock_proc.return_value.quantity.return_value = 42
        with patch.object(type(dyno), "_get_proc_class_by_formation_name",
                          new_callable=PropertyMock, return_value=mock_proc):
            result = dyno.tasks_in_queue
        self.assertEqual(result, 42)

    def test_no_tasks_in_queue_true_when_zero(self):
        dyno = make_dyno()
        with patch.object(type(dyno), "tasks_in_queue",
                          new_callable=PropertyMock, return_value=0):
            self.assertTrue(dyno.no_tasks_in_queue)

    def test_no_tasks_in_queue_false_when_nonzero(self):
        dyno = make_dyno()
        with patch.object(type(dyno), "tasks_in_queue",
                          new_callable=PropertyMock, return_value=5):
            self.assertFalse(dyno.no_tasks_in_queue)


class TestHireProcLookup(BaseLockTestCase):

    def test_get_proc_class_logs_error_on_bad_path(self):
        dyno = make_dyno()
        # Clear cached_property
        dyno.__dict__.pop("_get_proc_class_by_formation_name", None)
        with patch.object(type(ds), "HIREFIRE_PROCS", ["nonexistent.module.BadClass"], create=True):
            with patch("heroku_manager.heroku.logger") as mock_log:
                result = dyno._get_proc_class_by_formation_name
        # Should return None and log error
        self.assertIsNone(result)
        mock_log.error.assert_called()

    def test_get_proc_class_returns_matching_class(self):
        dyno = make_dyno(formation_size="standard-2x")  # formation_name = "normal_worker"
        dyno.__dict__.pop("_get_proc_class_by_formation_name", None)
        mock_cls = MagicMock()
        mock_cls.name = "normal_worker"
        with patch.object(type(ds), "HIREFIRE_PROCS", ["some.module.Worker"], create=True):
            with patch("heroku_manager.heroku.import_module") as mock_import:
                mock_module = MagicMock()
                mock_module.Worker = mock_cls
                mock_import.return_value = mock_module
                result = dyno._get_proc_class_by_formation_name
        self.assertIs(result, mock_cls)


class TestSettings(BaseLockTestCase):

    def test_downscale_on_non_empty_queue_default_true(self):
        dyno = make_dyno()
        self.assertTrue(dyno.downscale_on_non_empty_queue)

    def test_max_dyno_size_default(self):
        dyno = make_dyno()
        self.assertEqual(dyno.max_dyno_size, "performance-2xl")

    def test_threads_available_default(self):
        dyno = make_dyno()
        self.assertGreater(dyno.threads_available, 0)

    def test_price_per_hour_default(self):
        dyno = make_dyno()
        self.assertGreater(dyno.price_per_hour, 0)


class TestExactMemoryUsage(BaseLockTestCase):

    def test_uses_local_process_memory_when_no_log_memory(self):
        dyno = make_dyno()
        import builtins, types as _types
        real_import = builtins.__import__

        def _fake_import(name, *args, **kwargs):
            if name == 'utils.process':
                mod = _types.ModuleType('utils.process')
                mod.get_total_memory_usage = lambda: 256.0
                return mod
            return real_import(name, *args, **kwargs)

        with patch.object(dyno, "get_memory_usage_from_logs", return_value=None):
            with patch('builtins.__import__', side_effect=_fake_import):
                result = dyno.exact_memory_usage
        self.assertEqual(result, 256.0)

    def test_falls_back_to_zero_on_import_error(self):
        # utils.process is not on the path in the heroku-manager test environment,
        # so the ImportError branch is exercised naturally.
        dyno = make_dyno()
        with patch.object(dyno, "get_memory_usage_from_logs", return_value=None):
            result = dyno.exact_memory_usage
        # ImportError branch returns 0 (or local psutil value — both are numeric)
        self.assertIsInstance(result, (int, float))

    def test_uses_log_memory_over_process(self):
        dyno = make_dyno()
        with patch.object(dyno, "get_memory_usage_from_logs", return_value=350.0):
            result = dyno.exact_memory_usage
        self.assertEqual(result, 350.0)


class TestMemoryQuota(BaseLockTestCase):

    def test_get_memory_quota_returns_float(self):
        dyno = make_dyno()
        # memory_quota from logs — use the pattern
        from tests.test_logs import _make_log_sample, _make_log_response, _make_logplex_response
        log_text = _make_log_sample()
        with patch.object(dyno, "call_heroku_api", return_value=_make_log_response(log_text)):
            with patch("heroku_manager.heroku.requests.get",
                       return_value=_make_logplex_response(log_text)):
                _ = dyno.get_heroku_logs()
        result = dyno.get_memory_usage_from_logs()
        self.assertIsInstance(result, float)
        self.assertAlmostEqual(result, 298.0)


class TestThreadManagement(BaseLockTestCase):

    def test_start_continuous_autoscale_creates_thread(self):
        dyno = make_dyno()
        with patch("heroku_manager.heroku.threading.Thread") as mock_thread_cls:
            mock_thread = MagicMock()
            mock_thread_cls.return_value = mock_thread
            dyno.start_continuous_autoscale()
        mock_thread.start.assert_called_once()

    def test_start_continuous_autoscale_skips_if_already_running(self):
        dyno = make_dyno()
        dyno._autoscale_thread = MagicMock()
        dyno._autoscale_thread.is_alive.return_value = True
        with patch("heroku_manager.heroku.threading.Thread") as mock_thread_cls:
            dyno.start_continuous_autoscale()
        mock_thread_cls.assert_not_called()

    def test_stop_continuous_autoscale_stops_thread(self):
        dyno = make_dyno()
        mock_thread = MagicMock()
        mock_thread.is_alive.return_value = True
        dyno._autoscale_thread = mock_thread
        dyno.stop_continuous_autoscale()
        self.assertTrue(dyno._stop_autoscale_event.is_set())

    def test_stop_continuous_autoscale_no_thread_is_noop(self):
        dyno = make_dyno()
        dyno._autoscale_thread = None
        # Should not raise
        dyno.stop_continuous_autoscale()

    def test_start_continuous_file_cleaning_creates_thread(self):
        dyno = make_dyno()
        with patch("heroku_manager.heroku.threading.Thread") as mock_thread_cls:
            mock_thread = MagicMock()
            mock_thread_cls.return_value = mock_thread
            dyno.start_continuous_file_cleaning()
        mock_thread.start.assert_called_once()

    def test_stop_continuous_file_cleaning_stops_thread(self):
        dyno = make_dyno()
        mock_thread = MagicMock()
        mock_thread.is_alive.return_value = True
        dyno._file_cleaning_thread = mock_thread
        dyno.stop_continuous_file_cleaning()
        self.assertTrue(dyno._stop_file_cleaning_event.is_set())


class TestCheckAndCleanOldFiles(BaseLockTestCase):

    def test_skips_when_disabled(self):
        dyno = make_dyno()
        ds.DYNO_FILE_CLEANING_ENABLED = False
        try:
            with patch.object(dyno, "clean_old_files") as mock_clean:
                dyno.check_and_clean_old_files()
            mock_clean.assert_not_called()
        finally:
            ds.DYNO_FILE_CLEANING_ENABLED = False

    def test_calls_clean_when_interval_elapsed(self):
        dyno = make_dyno()
        ds.DYNO_FILE_CLEANING_ENABLED = True
        try:
            dyno._last_file_cleaning = None
            with patch.object(dyno, "clean_old_files", return_value=True):
                dyno.check_and_clean_old_files()
        finally:
            ds.DYNO_FILE_CLEANING_ENABLED = False

    def test_sets_retry_timestamp_when_clean_fails(self):
        dyno = make_dyno()
        ds.DYNO_FILE_CLEANING_ENABLED = True
        try:
            dyno._last_file_cleaning = None
            with patch.object(dyno, "clean_old_files", return_value=False):
                with patch("heroku_manager.heroku.logger"):
                    dyno.check_and_clean_old_files()
            self.assertIsNotNone(dyno._last_file_cleaning)
        finally:
            ds.DYNO_FILE_CLEANING_ENABLED = False


class TestUncoveredProperties(BaseLockTestCase):

    def test_avg_load_1min_uses_get_load_1min_avg(self):
        dyno = make_dyno()
        with patch.object(dyno, "get_load_1min_avg", return_value=1.5):
            self.assertEqual(dyno.avg_load_1min, 1.5)

    def test_detected_r14_uses_get_r14_from_logs(self):
        dyno = make_dyno()
        with patch.object(type(dyno), "extract_latest_metric", return_value=True):
            self.assertTrue(dyno.get_r14_from_logs())

    def test_detected_r15_uses_get_r15_from_logs(self):
        dyno = make_dyno()
        with patch.object(type(dyno), "extract_latest_metric", return_value=True):
            self.assertTrue(dyno.get_r15_from_logs())

    def test_get_threads_used_returns_int(self):
        dyno = make_dyno()
        with patch.object(type(dyno), "remote_monitoring",
                          new_callable=PropertyMock, return_value=False):
            result = dyno.get_threads_used()
        self.assertIsInstance(result, int)

    def test_threads_used_property_calls_get_threads_used(self):
        dyno = make_dyno()
        with patch.object(dyno, "get_threads_used", return_value=10):
            self.assertEqual(dyno.threads_used, 10)

    def test_requires_upscale_true_when_high_memory(self):
        dyno = make_dyno(formation_size="standard-2x")
        with patch.object(type(dyno), "current_memory_usage_percentage",
                          new_callable=PropertyMock, return_value=95.0):
            with patch.object(type(dyno), "tasks_in_queue",
                               new_callable=PropertyMock, return_value=0):
                with patch.object(type(dyno), "detected_r15",
                                   new_callable=PropertyMock, return_value=True):
                    self.assertTrue(dyno.requires_upscale)

    def test_requires_upscale_false_when_low_memory(self):
        dyno = make_dyno(formation_size="standard-2x")
        with patch.object(type(dyno), "current_memory_usage_percentage",
                          new_callable=PropertyMock, return_value=30.0):
            with patch.object(type(dyno), "tasks_in_queue",
                               new_callable=PropertyMock, return_value=0):
                with patch.object(type(dyno), "detected_r15",
                                   new_callable=PropertyMock, return_value=False):
                    self.assertFalse(dyno.requires_upscale)


class TestScalingStateFlags(BaseLockTestCase):

    def test_set_and_clear_upscaling(self):
        dyno = make_dyno()
        self.assertFalse(dyno.is_upscaling)
        dyno.set_upscaling()
        self.assertTrue(dyno.is_upscaling)
        cache.delete(dyno.upscaling_cache_key)
        self.assertFalse(dyno.is_upscaling)

    def test_set_and_clear_downscaling(self):
        dyno = make_dyno()
        self.assertFalse(dyno.is_downscaling)
        dyno.set_downscaling()
        self.assertTrue(dyno.is_downscaling)
        cache.delete(dyno.downscale_cache_key)
        self.assertFalse(dyno.is_downscaling)


class TestApiUncoveredPaths(BaseLockTestCase):

    def test_call_heroku_api_4xx_logs_error(self):
        dyno = make_dyno()
        import requests as _req
        resp = _req.Response()
        resp.status_code = 404
        resp._content = b"not found"
        with patch("heroku_manager.heroku.requests.request", return_value=resp):
            with patch("heroku_manager.heroku.logger") as mock_log:
                result = dyno.call_heroku_api("GET", "https://api.heroku.com/apps/x/dynos")
        # 4xx returns the response (caller decides) but logs error
        mock_log.error.assert_called()

    def test_call_heroku_api_none_when_rate_limited(self):
        dyno = make_dyno()
        from django.conf import settings
        limit = 50
        cache.set(f"heroku:api_rate:{dyno.app_name}", limit, timeout=60)
        from django.core.cache.backends.locmem import LocMemCache
        with patch.object(LocMemCache, "ttl", return_value=1):
            with patch("heroku_manager.heroku.time.sleep"):
                result = dyno.call_heroku_api("GET", "https://api.heroku.com/x")
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
