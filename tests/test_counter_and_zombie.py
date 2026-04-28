"""
Tests: increment_dyno_counter, check_for_sibling_zombie_dynos.
"""
import unittest
from unittest.mock import patch, MagicMock
from tests.conftest import make_dyno, BaseLockTestCase
from django.core.cache import cache
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

    def _set_alive(self, dyno_name, seconds_ago):
        t = timezone.now() - timezone.timedelta(seconds=seconds_ago)
        cache.set(f"heroku:dyno_alive:{dyno_name}", t, timeout=3600)

    def _patch_alive_keys(self, names):
        keys = [f"heroku:dyno_alive:{n}" for n in names]
        return patch.object(cache, "keys", return_value=keys, create=True)

    def test_no_zombies_no_restart(self):
        dyno = make_dyno()
        self._set_alive("normal_worker.2", 10)  # recent
        with self._patch_alive_keys(["normal_worker.2"]):
            with patch.object(dyno, "restart_zombie_dyno") as mock_restart:
                dyno.check_for_sibling_zombie_dynos()
        mock_restart.assert_not_called()

    def test_zombie_detected_and_restarted(self):
        from django.conf import settings as ds
        dyno = make_dyno()
        threshold = ds.DYNO_ZOMBIE_THRESHOLD
        self._set_alive("normal_worker.2", threshold + 10)  # stale
        with self._patch_alive_keys(["normal_worker.2"]):
            with patch.object(dyno, "restart_zombie_dyno") as mock_restart:
                dyno.check_for_sibling_zombie_dynos()
        mock_restart.assert_called_once_with("normal_worker.2")

    def test_restart_zombie_dyno_delegates_to_restart_dyno(self):
        dyno = make_dyno()
        with patch.object(dyno, "restart_dyno") as mock_restart:
            dyno.restart_zombie_dyno("normal_worker.3")
        mock_restart.assert_called_once_with("normal_worker.3")


if __name__ == "__main__":
    unittest.main()
