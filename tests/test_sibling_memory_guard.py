"""
Unit tests for the sibling-memory downscale guard.

Covers:
  - _downscale_memory_threshold
  - is_still_high_memory_usage_for_downscale
  - any_sibling_still_high_memory (excludes self, hot/cool siblings, formation name isolation)
  - allow_downscale (R14, R15, sibling guard, tasks in queue)
  - allow_downscale_on_shutdown (R14, R15, sibling guard)
  - check_in_dyno (memory + alive published, TTL uses DYNO_ZOMBIE_THRESHOLD)
  - remove_dyno_from_alive_cache (deletes both alive + memory keys)
  - Stale/None memory edge cases
"""
import os
import logging
import time
import unittest
from unittest.mock import ANY, MagicMock, patch, PropertyMock
import django
from django.conf import settings as django_settings
from django.utils import timezone

# ── Minimal Django setup ──────────────────────────────────────────────────────
if not django_settings.configured:
    django_settings.configure(
        CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}},
        WORKER_SETTINGS_MAP={},
        DYNO_AUTOSCALE_INTERVAL=30,
        HIGH_MEM_USE_MB=400,
        UPSCALE_PERCENTAGE_HIGH_MEM_USE=80,
        DOWNSCALE_PERCENTAGE_HIGH_MEM_USE=105,
        DYNO_TIME_BETWEEN_SCALES=300,
        DYNO_ZOMBIE_THRESHOLD=300,
        USE_TZ=True,
        TIME_ZONE="UTC",
    )
    django.setup()

from django.core.cache import cache
from tests.conftest import patch_scan_backend, real_decoder, seed_index_ready
from heroku_manager.heroku import HerokuDyno, DYNO_SIZES, _update_dyno_registry


def make_dyno(dyno_name="normal_worker.1", formation_size="standard-2x"):
    """Return a HerokuDyno with minimal real attrs; everything else mocked."""
    dyno = HerokuDyno.__new__(HerokuDyno)
    dyno.app_name = "floship"
    dyno.dyno_name = dyno_name
    dyno.dyno_id = "abc123"
    dyno.formation_name = dyno_name.split(".")[0]
    dyno.heroku_api_key = "fake"
    dyno._stop_autoscale_event = MagicMock()
    dyno._stop_file_cleaning_event = MagicMock()
    dyno._autoscale_thread = None
    dyno._file_cleaning_thread = None
    import threading
    dyno._thread_lock = threading.Lock()
    dyno._last_file_cleaning = None
    dyno._formation_size_cached = (formation_size, time.time())  # pin formation size
    # Legacy fixtures opt into a seeded ready state (instance-level, never a
    # production-class mutation).  Dedicated readiness tests clear it via
    # _restore_real_index_ready and drive the real predicate.
    dyno.__dict__["index_ready"] = True
    return dyno


def _isolate_dyno_type(dyno):
    isolated_type = type(f"IsolatedHerokuDyno_{id(dyno)}", (type(dyno),), {})
    dyno.__class__ = isolated_type
    return dyno


def _restore_real_index_ready(dyno):
    """Clear the seeded instance ``index_ready`` so the REAL cached_property
    descriptor runs the runtime predicate (no production-class mutation)."""
    dyno.__dict__.pop("index_ready", None)
    return dyno


_real_decoder = real_decoder


class TestDynoRegistryWrites(unittest.TestCase):

    def test_add_uses_physical_cache_key_and_full_dyno_name(self):
        backend = MagicMock()
        client = backend.client.get_client.return_value
        backend.make_key.return_value = "prefix:1:heroku:dynos:v1:floship"

        with patch("heroku_manager.heroku.cache", backend):
            result = _update_dyno_registry("floship", "normal_worker.3", 123.5)

        self.assertTrue(result)
        backend.client.get_client.assert_called_once_with(write=True)
        backend.make_key.assert_called_once_with("heroku:dynos:v1:floship")
        client.zadd.assert_called_once_with(
            "prefix:1:heroku:dynos:v1:floship",
            {"normal_worker.3": 123.5},
        )

    def test_remove_uses_physical_cache_key_and_full_dyno_name(self):
        backend = MagicMock()
        client = backend.client.get_client.return_value
        backend.make_key.return_value = "prefix:1:heroku:dynos:v1:floship"

        with patch("heroku_manager.heroku.cache", backend):
            result = _update_dyno_registry("floship", "normal_worker.3")

        self.assertTrue(result)
        client.zrem.assert_called_once_with(
            "prefix:1:heroku:dynos:v1:floship",
            "normal_worker.3",
        )

    def test_missing_identity_or_backend_error_does_not_raise(self):
        backend = MagicMock()
        with patch("heroku_manager.heroku.cache", backend):
            self.assertFalse(_update_dyno_registry(None, "normal_worker.3", 123.5))
            self.assertFalse(_update_dyno_registry("floship", None, 123.5))
        backend.client.get_client.assert_not_called()

        with patch("heroku_manager.heroku.cache", object()):
            self.assertFalse(_update_dyno_registry("floship", "normal_worker.3", 123.5))


class TestDownscaleMemoryThreshold(unittest.TestCase):
    """_downscale_memory_threshold returns previous_size_memory * DOWNSCALE_% / 100."""

    def test_standard_2x_previous_is_standard_1x(self):
        # standard-2x previous = standard-1x (512 MB), threshold = 512 * 105 / 100 = 537.6
        dyno = make_dyno(formation_size="standard-2x")
        expected = DYNO_SIZES["standard-1x"]["memory"] * django_settings.DOWNSCALE_PERCENTAGE_HIGH_MEM_USE / 100
        self.assertAlmostEqual(dyno._downscale_memory_threshold, expected)

    def test_no_previous_or_original_size_blocks_downscale(self):
        # standard-1x has no previous and no original in cache →
        # threshold returns float('inf') to block any accidental downscale.
        import math
        dyno = make_dyno(formation_size="standard-1x")
        self.assertTrue(math.isinf(dyno._downscale_memory_threshold))


class TestIsStillHighMemoryUsageForDownscale(unittest.TestCase):
    """is_still_high_memory_usage_for_downscale delegates to _downscale_memory_threshold."""

    def _dyno_with_memory(self, current_mb, formation_size="standard-2x"):
        dyno = make_dyno(formation_size=formation_size)
        type(dyno).current_memory_usage = PropertyMock(return_value=current_mb)
        return dyno

    def test_below_threshold_returns_false(self):
        dyno = self._dyno_with_memory(298)  # 298 < 537.6
        self.assertFalse(dyno.is_still_high_memory_usage_for_downscale)

    def test_above_threshold_returns_true(self):
        dyno = self._dyno_with_memory(600)  # 600 >= 537.6
        self.assertTrue(dyno.is_still_high_memory_usage_for_downscale)

    def test_exactly_at_threshold_returns_true(self):
        threshold = DYNO_SIZES["standard-1x"]["memory"] * django_settings.DOWNSCALE_PERCENTAGE_HIGH_MEM_USE / 100
        dyno = self._dyno_with_memory(threshold)
        self.assertTrue(dyno.is_still_high_memory_usage_for_downscale)


class TestAnySiblingStillHighMemory(unittest.TestCase):

    def setUp(self):
        cache.clear()
        # Seed memory values in the locmem cache for assertions
        self._memory_store = {}

    def tearDown(self):
        cache.clear()

    def _dyno(self, dyno_name="normal_worker.1", formation_size="standard-2x"):
        dyno = make_dyno(dyno_name=dyno_name, formation_size=formation_size)
        # Dedicated fail-closed tests drive the REAL runtime predicate; the
        # fixture's seeded ready state must not mask flag-false/absent gates.
        _restore_real_index_ready(dyno)
        return dyno

    def _set_sibling_memory(self, dyno_name, memory_mb):
        key = f"heroku:dyno_memory:{dyno_name}"
        cache.set(key, memory_mb, timeout=60)
        self._memory_store[key] = memory_mb

    def _patch_keys(self, pattern_to_keys=None):
        """Patch a fake raw client so SCAN serves the seeded memory store."""
        return patch_scan_backend(self._memory_store)

    def test_no_siblings_in_cache_returns_false(self):
        dyno = self._dyno()
        with self._patch_keys({}):
            self.assertFalse(dyno.any_sibling_still_high_memory)

    def test_only_self_in_cache_returns_false(self):
        dyno = self._dyno("normal_worker.1")
        self._set_sibling_memory("normal_worker.1", 298)
        with self._patch_keys({}):
            self.assertFalse(dyno.any_sibling_still_high_memory)

    def test_sibling_below_threshold_returns_false(self):
        # threshold = 512 * 105/100 = 537.6; sibling at 400 is safe
        dyno = self._dyno("normal_worker.1")
        self._set_sibling_memory("normal_worker.2", 400)
        with self._patch_keys({}):
            self.assertFalse(dyno.any_sibling_still_high_memory)

    def test_sibling_above_threshold_returns_true(self):
        dyno = self._dyno("normal_worker.1")
        self._set_sibling_memory("normal_worker.2", 1013)
        with self._patch_keys({}):
            self.assertTrue(dyno.any_sibling_still_high_memory)

    def test_excludes_self_even_if_self_is_hot(self):
        dyno = self._dyno("normal_worker.2")
        self._set_sibling_memory("normal_worker.2", 1013)
        with self._patch_keys({}):
            self.assertFalse(dyno.any_sibling_still_high_memory)

    def test_no_previous_formation_size_returns_false(self):
        # standard-1x has no previous → threshold=0 → guard disabled
        dyno = self._dyno("normal_worker.1", formation_size="standard-1x")
        self._set_sibling_memory("normal_worker.2", 9999)
        with self._patch_keys({}):
            self.assertFalse(dyno.any_sibling_still_high_memory)

    def test_multiple_siblings_one_hot_returns_true(self):
        dyno = self._dyno("normal_worker.1")
        self._set_sibling_memory("normal_worker.3", 200)
        self._set_sibling_memory("normal_worker.4", 300)
        self._set_sibling_memory("normal_worker.5", 1013)
        with self._patch_keys({}):
            self.assertTrue(dyno.any_sibling_still_high_memory)


class TestAllowDownscaleWithSiblingGuard(unittest.TestCase):

    def setUp(self):
        cache.clear()
        self._memory_store = {}

    def tearDown(self):
        cache.clear()

    def _set_sibling_memory(self, dyno_name, memory_mb):
        key = f"heroku:dyno_memory:{dyno_name}"
        cache.set(key, memory_mb, timeout=60)
        self._memory_store[key] = memory_mb

    def _patch_keys(self):
        """Patch a fake raw client so SCAN serves the seeded memory store."""
        return patch_scan_backend(self._memory_store)

    def _dyno_allow_downscale_setup(self, own_memory, sibling_memory=None):
        """Helper: dyno with controlled memory, optional sibling in cache."""
        dyno = _isolate_dyno_type(make_dyno("normal_worker.1", formation_size="standard-2x"))
        type(dyno).current_memory_usage = PropertyMock(return_value=own_memory)
        type(dyno).current_memory_usage_percentage = PropertyMock(return_value=own_memory / 1024 * 100)
        type(dyno).detected_r14 = PropertyMock(return_value=False)
        type(dyno).detected_r15 = PropertyMock(return_value=False)
        type(dyno).no_tasks_in_queue = PropertyMock(return_value=True)
        type(dyno).downscale_on_non_empty_queue = PropertyMock(return_value=False)
        if sibling_memory is not None:
            self._set_sibling_memory("normal_worker.2", sibling_memory)
        return dyno

    def test_allows_downscale_when_all_siblings_cool(self):
        dyno = self._dyno_allow_downscale_setup(own_memory=298, sibling_memory=300)
        with self._patch_keys():
            self.assertTrue(dyno.allow_downscale)

    def test_blocks_downscale_when_sibling_hot(self):
        dyno = self._dyno_allow_downscale_setup(own_memory=298, sibling_memory=1013)
        with self._patch_keys():
            self.assertFalse(dyno.allow_downscale)

    def test_blocks_downscale_when_self_hot(self):
        dyno = self._dyno_allow_downscale_setup(own_memory=600, sibling_memory=None)
        with self._patch_keys():
            self.assertFalse(dyno.allow_downscale)


class TestCheckInDynoPublishesMemory(unittest.TestCase):

    def setUp(self):
        cache.clear()

    def tearDown(self):
        cache.clear()

    def test_check_in_dyno_writes_memory_key(self):
        dyno = make_dyno("normal_worker.3")
        type(dyno).current_memory_usage = PropertyMock(return_value=512)
        dyno.check_in_dyno()
        self.assertEqual(cache.get("heroku:dyno_memory:normal_worker.3"), 512)

    def test_check_in_dyno_stores_zero_memory(self):
        """P1-7: if mem is not None means 0 is a valid reading and must be stored."""
        dyno = make_dyno("normal_worker.3")
        type(dyno).current_memory_usage = PropertyMock(return_value=0)
        dyno.check_in_dyno()
        self.assertEqual(cache.get("heroku:dyno_memory:normal_worker.3"), 0)

    def test_check_in_dyno_still_writes_alive_key(self):
        dyno = make_dyno("normal_worker.3")
        type(dyno).current_memory_usage = PropertyMock(return_value=0)
        dyno.check_in_dyno()
        self.assertIsNotNone(cache.get("heroku:dyno_alive:normal_worker.3"))

    @patch("heroku_manager.heroku._update_dyno_registry")
    def test_check_in_dyno_adds_registry_member(self, update_registry):
        dyno = make_dyno("normal_worker.3")
        type(dyno).current_memory_usage = PropertyMock(return_value=0)

        dyno.check_in_dyno()

        update_registry.assert_called_once_with("floship", "normal_worker.3", ANY)


class TestRemoveDynoFromAliveCache(unittest.TestCase):

    def setUp(self):
        cache.clear()

    def tearDown(self):
        cache.clear()

    def test_removes_both_alive_and_memory_keys(self):
        cache.set("heroku:dyno_alive:normal_worker.1", "now", timeout=60)
        cache.set("heroku:dyno_memory:normal_worker.1", 512, timeout=60)
        dyno = make_dyno("normal_worker.1")
        dyno.remove_dyno_from_alive_cache()
        self.assertIsNone(cache.get("heroku:dyno_alive:normal_worker.1"))
        self.assertIsNone(cache.get("heroku:dyno_memory:normal_worker.1"))

    def test_removes_named_dyno_keys(self):
        cache.set("heroku:dyno_alive:normal_worker.5", "now", timeout=60)
        cache.set("heroku:dyno_memory:normal_worker.5", 999, timeout=60)
        dyno = make_dyno("normal_worker.1")
        dyno.remove_dyno_from_alive_cache("normal_worker.5")
        self.assertIsNone(cache.get("heroku:dyno_alive:normal_worker.5"))
        self.assertIsNone(cache.get("heroku:dyno_memory:normal_worker.5"))

    @patch("heroku_manager.heroku._update_dyno_registry")
    def test_removes_named_dyno_from_registry(self, update_registry):
        dyno = make_dyno("normal_worker.1")

        dyno.remove_dyno_from_alive_cache("normal_worker.5")

        update_registry.assert_called_once_with("floship", "normal_worker.5")


# ── Helpers shared by new test classes ───────────────────────────────────────

def _make_full_dyno(dyno_name="normal_worker.1", formation_size="standard-2x",
                    own_memory=298, r14=False, r15=False,
                    no_tasks=True, downscale_on_non_empty=False):
    """Fully wired dyno for allow_downscale / allow_downscale_on_shutdown tests."""
    dyno = _isolate_dyno_type(make_dyno(dyno_name=dyno_name, formation_size=formation_size))
    type(dyno).current_memory_usage = PropertyMock(return_value=own_memory)
    type(dyno).current_memory_usage_percentage = PropertyMock(
        return_value=own_memory / DYNO_SIZES[formation_size]["memory"] * 100
    )
    type(dyno).detected_r14 = PropertyMock(return_value=r14)
    type(dyno).detected_r15 = PropertyMock(return_value=r15)
    type(dyno).no_tasks_in_queue = PropertyMock(return_value=no_tasks)
    type(dyno).downscale_on_non_empty_queue = PropertyMock(return_value=downscale_on_non_empty)
    return dyno


def _patch_cache_keys(memory_store):
    """Patch a fake raw client so SCAN serves the provided key store."""
    return patch_scan_backend(memory_store)


class TestR14R15GuardInteractions(unittest.TestCase):
    """R14 and R15 flags interact with allow_downscale and allow_downscale_on_shutdown."""

    def setUp(self):
        cache.clear()

    def tearDown(self):
        cache.clear()

    def test_r15_blocks_allow_downscale(self):
        dyno = _make_full_dyno(own_memory=200, r15=True)
        with _patch_cache_keys({}):
            self.assertFalse(dyno.allow_downscale)

    def test_r14_does_not_block_allow_downscale_when_memory_is_cool(self):
        dyno = _make_full_dyno(own_memory=200, r14=True)
        with _patch_cache_keys({}):
            self.assertTrue(dyno.allow_downscale)

    def test_r15_blocks_allow_downscale_on_shutdown(self):
        dyno = _make_full_dyno(own_memory=200, r15=True)
        with _patch_cache_keys({}):
            self.assertFalse(dyno.allow_downscale_on_shutdown)

    def test_r14_does_not_block_allow_downscale_on_shutdown_when_memory_is_cool(self):
        dyno = _make_full_dyno(own_memory=200, r14=True)
        with _patch_cache_keys({}):
            self.assertTrue(dyno.allow_downscale_on_shutdown)

    def test_r14_and_r15_both_absent_allows_shutdown_downscale_when_cool(self):
        dyno = _make_full_dyno(own_memory=200, r14=False, r15=False)
        with _patch_cache_keys({}):
            self.assertTrue(dyno.allow_downscale_on_shutdown)

    def test_r15_without_r14_still_blocks_downscale(self):
        # R15 alone: requires_upscale=True is enough to block
        dyno = _make_full_dyno(own_memory=200, r14=False, r15=True)
        with _patch_cache_keys({}):
            self.assertFalse(dyno.allow_downscale)

    def test_neither_r14_nor_r15_cool_memory_allows_downscale(self):
        dyno = _make_full_dyno(own_memory=200, r14=False, r15=False)
        with _patch_cache_keys({}):
            self.assertTrue(dyno.allow_downscale)


class TestAllowDownscaleOnShutdownSiblingGuard(unittest.TestCase):
    """allow_downscale_on_shutdown must also check any_sibling_still_high_memory."""

    def setUp(self):
        cache.clear()
        self._store = {}

    def tearDown(self):
        cache.clear()

    def _set_mem(self, dyno_name, mb):
        key = f"heroku:dyno_memory:{dyno_name}"
        cache.set(key, mb, timeout=60)
        self._store[key] = mb

    def test_blocks_shutdown_downscale_when_sibling_hot(self):
        # Self is cool but sibling is hot — shutdown must NOT trigger downscale
        dyno = _make_full_dyno("normal_worker.1", own_memory=200)
        self._set_mem("normal_worker.2", 1013)
        with _patch_cache_keys(self._store):
            self.assertFalse(dyno.allow_downscale_on_shutdown)

    def test_allows_shutdown_downscale_when_all_siblings_cool(self):
        dyno = _make_full_dyno("normal_worker.1", own_memory=200)
        self._set_mem("normal_worker.2", 300)
        with _patch_cache_keys(self._store):
            self.assertTrue(dyno.allow_downscale_on_shutdown)

    def test_allows_shutdown_downscale_with_no_siblings(self):
        dyno = _make_full_dyno("normal_worker.1", own_memory=200)
        with _patch_cache_keys(self._store):
            self.assertTrue(dyno.allow_downscale_on_shutdown)


class TestStaleSiblingMemoryKeys(unittest.TestCase):
    """
    Crashed dynos (no graceful shutdown) leave stale memory keys.
    TTL must equal DYNO_ZOMBIE_THRESHOLD so they expire before causing permanent
    downscale blocks.
    """

    def setUp(self):
        cache.clear()

    def tearDown(self):
        cache.clear()

    def test_check_in_dyno_uses_zombie_threshold_as_ttl(self):
        """Memory key TTL must match DYNO_ZOMBIE_THRESHOLD, not a hardcoded 24h."""
        dyno = make_dyno("normal_worker.1")
        type(dyno).current_memory_usage = PropertyMock(return_value=512)

        expected_ttl = django_settings.DYNO_ZOMBIE_THRESHOLD

        with patch.object(cache, "set") as mock_set:
            dyno.check_in_dyno()

        # Both alive and memory keys must use the same zombie TTL
        memory_calls = [c for c in mock_set.call_args_list
                        if "dyno_memory:" in str(c) and "dyno_memory_stable" not in str(c)]
        self.assertEqual(len(memory_calls), 1)
        _, kwargs = memory_calls[0]
        ttl_used = kwargs.get("timeout") or memory_calls[0][0][2]  # positional or keyword
        self.assertEqual(ttl_used, expected_ttl)

    def test_alive_key_uses_zombie_threshold_as_ttl(self):
        dyno = make_dyno("normal_worker.1")
        type(dyno).current_memory_usage = PropertyMock(return_value=0)
        expected_ttl = django_settings.DYNO_ZOMBIE_THRESHOLD

        with patch.object(cache, "set") as mock_set:
            dyno.check_in_dyno()

        alive_calls = [c for c in mock_set.call_args_list
                       if "dyno_alive" in str(c)]
        self.assertEqual(len(alive_calls), 1)
        _, kwargs = alive_calls[0]
        ttl_used = kwargs.get("timeout") or alive_calls[0][0][2]
        self.assertEqual(ttl_used, expected_ttl)


class TestNullMemoryEdgeCases(unittest.TestCase):
    """current_memory_usage can return 0 or None — threshold comparisons must not raise."""

    def setUp(self):
        cache.clear()

    def tearDown(self):
        cache.clear()

    def test_zero_own_memory_does_not_raise_in_threshold_check(self):
        dyno = _make_full_dyno(own_memory=0)
        with _patch_cache_keys({}):
            # Must not raise TypeError
            result = dyno.is_still_high_memory_usage_for_downscale
            self.assertIsInstance(result, bool)

    def test_zero_own_memory_allow_downscale_returns_bool(self):
        dyno = _make_full_dyno(own_memory=0)
        with _patch_cache_keys({}):
            result = dyno.allow_downscale
            self.assertIsInstance(result, bool)

    def test_sibling_none_memory_in_cache_is_treated_as_absent(self):
        # cache.get returns None for missing key → sibling treated as "not hot"
        dyno = _make_full_dyno("normal_worker.1", own_memory=200)
        store = {"heroku:dyno_memory:normal_worker.2": None}
        cache.set("heroku:dyno_memory:normal_worker.2", None, timeout=60)

        def _keys(pattern):
            return ["heroku:dyno_memory:normal_worker.2"]

        with patch.object(cache, "keys", side_effect=_keys, create=True):
            # None memory must not block downscale (treated as absent/cool)
            self.assertFalse(dyno.any_sibling_still_high_memory)


class TestFormationNameIsolation(unittest.TestCase):
    """cache.keys() pattern must not bleed across formation names."""

    def setUp(self):
        cache.clear()

    def tearDown(self):
        cache.clear()

    def test_normal_worker_does_not_match_normal_worker_extra(self):
        """
        'normal_worker.*' prefix (with literal dot) must not match
        'normal_worker_extra.1' (underscore, not dot).
        """
        dyno = _make_full_dyno("normal_worker.1", own_memory=200)
        # Simulate what cache.keys returns for the correct pattern
        all_keys = {
            "heroku:dyno_memory:normal_worker.2": 300,          # same formation ✓
            "heroku:dyno_memory:normal_worker_extra.1": 9999,   # different formation — should NOT match
        }

        def _keys(pattern):
            prefix = pattern.replace(".*", ".") if ".*" in pattern else pattern
            return [k for k in all_keys if k.startswith(prefix)]

        with patch.object(cache, "keys", side_effect=_keys, create=True):
            with patch.object(cache, "get", side_effect=lambda k: all_keys.get(k)):
                # normal_worker_extra.1 at 9999MB must not block normal_worker downscale
                self.assertFalse(dyno.any_sibling_still_high_memory)

    def test_hot_sibling_in_same_formation_is_detected(self):
        dyno = _make_full_dyno("normal_worker.1", own_memory=200)
        all_keys = {
            "heroku:dyno_memory:normal_worker.2": 1013,
        }

        # Dict store: member derivation reads the store keys and cache.get
        # resolves the Python value (mirrors the legacy locmem seeding).
        with patch_scan_backend(dict(all_keys)):
            with patch.object(cache, "get", side_effect=lambda k: all_keys.get(k)):
                self.assertTrue(dyno.any_sibling_still_high_memory)


class TestBaseFormationEdgeCases(unittest.TestCase):
    """
    At base formation size (standard-1x, previous=None):
    - threshold = 0
    - is_still_high_memory_usage_for_downscale: any positive memory ≥ 0 → True → blocks
    - any_sibling_still_high_memory: early-exit when threshold=0 → False (guard disabled)
    """

    def setUp(self):
        cache.clear()

    def tearDown(self):
        cache.clear()

    def test_threshold_inf_at_base_size_with_no_original(self):
        """P0-9: no original/previous size → float('inf') threshold (block-safe sentinel)."""
        import math
        dyno = make_dyno(formation_size="standard-1x")
        self.assertTrue(math.isinf(dyno._downscale_memory_threshold))

    def test_any_memory_blocks_downscale_when_threshold_unknown(self):
        # threshold=inf → math.isinf(threshold) → is_still_high == True → downscale blocked
        dyno = _make_full_dyno(formation_size="standard-1x", own_memory=200)
        self.assertTrue(dyno.is_still_high_memory_usage_for_downscale)

    def test_sibling_guard_disabled_at_base_size(self):
        # threshold=0 → any_sibling_still_high_memory returns False (short-circuit)
        dyno = _make_full_dyno("normal_worker.1", formation_size="standard-1x", own_memory=200)
        store = {"heroku:dyno_memory:normal_worker.2": 9999}
        with _patch_cache_keys(store):
            self.assertFalse(dyno.any_sibling_still_high_memory)

    def test_allow_downscale_false_at_base_size_due_to_threshold(self):
        dyno = _make_full_dyno(formation_size="standard-1x", own_memory=1)
        with _patch_cache_keys({}):
            self.assertFalse(dyno.allow_downscale)


class TestThresholdBoundary(unittest.TestCase):
    """Memory == threshold is treated as 'still hot' (>= not >)."""

    def setUp(self):
        cache.clear()

    def tearDown(self):
        cache.clear()

    def test_memory_exactly_at_threshold_blocks_downscale(self):
        # standard-2x → previous=standard-1x (512MB) → threshold = 512 * 105/100 = 537.6
        threshold = DYNO_SIZES["standard-1x"]["memory"] * django_settings.DOWNSCALE_PERCENTAGE_HIGH_MEM_USE / 100
        dyno = _make_full_dyno(own_memory=threshold)
        self.assertTrue(dyno.is_still_high_memory_usage_for_downscale)

    def test_memory_one_below_threshold_allows_downscale_check(self):
        threshold = DYNO_SIZES["standard-1x"]["memory"] * django_settings.DOWNSCALE_PERCENTAGE_HIGH_MEM_USE / 100
        dyno = _make_full_dyno(own_memory=threshold - 0.01)
        self.assertFalse(dyno.is_still_high_memory_usage_for_downscale)

    def test_sibling_exactly_at_threshold_blocks(self):
        threshold = DYNO_SIZES["standard-1x"]["memory"] * django_settings.DOWNSCALE_PERCENTAGE_HIGH_MEM_USE / 100
        dyno = _make_full_dyno("normal_worker.1", own_memory=200)
        store = {"heroku:dyno_memory:normal_worker.2": threshold}
        cache.set("heroku:dyno_memory:normal_worker.2", threshold, timeout=60)
        with _patch_cache_keys(store):
            self.assertTrue(dyno.any_sibling_still_high_memory)

    def test_at_upscale_threshold_does_not_upscale(self):
        # requires_upscale uses strict > not >=
        # standard-2x available=1024, UPSCALE_PERCENTAGE=80 → upscale at >81.92% (>839.2MB)
        available = DYNO_SIZES["standard-2x"]["memory"]
        exact_pct = django_settings.UPSCALE_PERCENTAGE_HIGH_MEM_USE  # 80
        memory_at_threshold = available * exact_pct / 100
        dyno = _make_full_dyno(own_memory=memory_at_threshold)
        # At exactly 80% — must NOT upscale (strict >)
        self.assertFalse(dyno.requires_upscale)


class TestAnySiblingRequiresUpscale(unittest.TestCase):
    """any_sibling_requires_upscale: True when a sibling's memory exceeds
    the upscale percentage threshold for the CURRENT tier (not the downscale
    threshold).  Used to trigger chain upscale on behalf of a stalled sibling."""

    def setUp(self):
        cache.clear()
        self._store = {}

    def tearDown(self):
        cache.clear()

    def _set_mem(self, dyno_name, mb):
        key = f"heroku:dyno_memory:{dyno_name}"
        cache.set(key, mb, timeout=60)
        self._store[key] = mb

    def test_sibling_above_upscale_threshold_returns_true(self):
        # standard-2x: 1024 MB * 80% = 819.2 MB; sibling at 2183 MB → True
        dyno = make_dyno("normal_worker.2", formation_size="standard-2x")
        self._set_mem("normal_worker.1", 2183)
        with _patch_cache_keys(self._store):
            self.assertTrue(dyno.any_sibling_requires_upscale)

    def test_sibling_below_upscale_threshold_returns_false(self):
        # standard-2x: threshold 819.2 MB; sibling at 317 MB → False
        dyno = make_dyno("normal_worker.2", formation_size="standard-2x")
        self._set_mem("normal_worker.1", 317)
        with _patch_cache_keys(self._store):
            self.assertFalse(dyno.any_sibling_requires_upscale)

    def test_excludes_self(self):
        dyno = make_dyno("normal_worker.1", formation_size="standard-2x")
        self._set_mem("normal_worker.1", 2183)
        with _patch_cache_keys(self._store):
            self.assertFalse(dyno.any_sibling_requires_upscale)

    def test_no_siblings_returns_false(self):
        dyno = make_dyno("normal_worker.1", formation_size="standard-2x")
        with _patch_cache_keys(self._store):
            self.assertFalse(dyno.any_sibling_requires_upscale)

    def test_formation_name_isolation(self):
        # normal_worker_extra.1 at 9999 MB must NOT match normal_worker
        dyno = make_dyno("normal_worker.2", formation_size="standard-2x")
        self._set_mem("normal_worker_extra.1", 9999)
        with _patch_cache_keys(self._store):
            self.assertFalse(dyno.any_sibling_requires_upscale)

    def test_at_exact_threshold_returns_false(self):
        # Strict > (not >=): threshold = 1024 * 80% = 819.2; sibling at 819.2 → False
        dyno = make_dyno("normal_worker.2", formation_size="standard-2x")
        threshold = DYNO_SIZES["standard-2x"]["memory"] * django_settings.UPSCALE_PERCENTAGE_HIGH_MEM_USE / 100
        self._set_mem("normal_worker.1", threshold)
        with _patch_cache_keys(self._store):
            self.assertFalse(dyno.any_sibling_requires_upscale)

    def test_none_memory_treated_as_absent(self):
        dyno = make_dyno("normal_worker.2", formation_size="standard-2x")
        key = "heroku:dyno_memory:normal_worker.1"
        cache.set(key, None, timeout=60)
        self._store[key] = None
        with _patch_cache_keys(self._store):
            self.assertFalse(dyno.any_sibling_requires_upscale)


class TestQueueGating(unittest.TestCase):
    """allow_downscale respects downscale_on_non_empty_queue and no_tasks_in_queue."""

    def setUp(self):
        cache.clear()

    def tearDown(self):
        cache.clear()

    def test_tasks_in_queue_blocks_downscale_when_flag_false(self):
        dyno = _make_full_dyno(own_memory=200, no_tasks=False, downscale_on_non_empty=False)
        with _patch_cache_keys({}):
            self.assertFalse(dyno.allow_downscale)

    def test_tasks_in_queue_allows_downscale_when_flag_true(self):
        dyno = _make_full_dyno(own_memory=200, no_tasks=False, downscale_on_non_empty=True)
        with _patch_cache_keys({}):
            self.assertTrue(dyno.allow_downscale)

    def test_no_tasks_in_queue_allows_downscale_regardless_of_flag(self):
        dyno = _make_full_dyno(own_memory=200, no_tasks=True, downscale_on_non_empty=False)
        with _patch_cache_keys({}):
            self.assertTrue(dyno.allow_downscale)



class TestIndexedDynoReadiness(unittest.TestCase):
    """Phase B: runtime readiness = env flag true AND own fresh index score AND
    every compatibility-SCAN member represented in the index."""

    def setUp(self):
        cache.clear()

    def tearDown(self):
        cache.clear()

    def _dyno(self, dyno_name="normal_worker.1", formation_size="standard-2x"):
        # Same opt-out as TestIndexedDynoReadiness: these tests exercise the
        # real readiness-gated destructive paths.
        dyno = make_dyno(dyno_name=dyno_name, formation_size=formation_size)
        _restore_real_index_ready(dyno)
        return dyno

    def _patch_backend(self, client_attrs=None, backend_attrs=None):
        """Patch heroku_manager.heroku.cache with a mock django-redis backend."""
        backend = MagicMock()
        client = MagicMock()
        for name, value in (client_attrs or {}).items():
            setattr(client, name, value)
        backend.client.get_client.return_value = client
        backend.make_key.side_effect = lambda key: key
        backend.get.return_value = None
        for name, value in (backend_attrs or {}).items():
            setattr(backend, name, value)
        self._cache_patcher = patch("heroku_manager.heroku.cache", backend)
        self._cache_patcher.start()
        self.addCleanup(self._cache_patcher.stop)
        return backend, client

    def test_flag_defaults_false_when_env_missing(self):
        dyno = self._dyno()
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("HEROKU_DYNO_INDEX_V1_READY", None)
            self.assertFalse(dyno.index_ready)

    def test_not_ready_without_fresh_own_index_score(self):
        dyno = self._dyno()
        with patch.dict(os.environ, {"HEROKU_DYNO_INDEX_V1_READY": "true"}):
            self._patch_backend(client_attrs={"zscore": MagicMock(return_value=None)})
            self.assertFalse(dyno.index_ready)

    def test_ready_when_flag_fresh_score_and_scan_reconciled(self):
        dyno = self._dyno("normal_worker.1")
        with patch.dict(os.environ, {"HEROKU_DYNO_INDEX_V1_READY": "true"}):
            self._patch_backend(client_attrs={
                "zscore": MagicMock(return_value=time.time()),
                "zrangebyscore": MagicMock(return_value=["normal_worker.1"]),
                "scan": MagicMock(return_value=(0, ["heroku:dyno_alive:normal_worker.1"])),
            })
            self.assertTrue(dyno.index_ready)

    def test_not_ready_when_scan_member_missing_from_index(self):
        # Compatibility scan sees old-writer dyno not yet in the v1 index → not ready
        dyno = self._dyno("normal_worker.1")
        with patch.dict(os.environ, {"HEROKU_DYNO_INDEX_V1_READY": "true"}):
            self._patch_backend(client_attrs={
                "zscore": MagicMock(return_value=time.time()),
                "zrangebyscore": MagicMock(return_value=[]),
                "scan": MagicMock(return_value=(0, ["heroku:dyno_alive:normal_worker.1"])),
            })
            self.assertFalse(dyno.index_ready)

    @patch("heroku_manager.heroku.time.time", return_value=1_700_000_000.0)
    def test_not_ready_when_own_index_score_equals_cutoff(self, mock_time):
        # Score at/under the prune cutoff is stale: readiness must be false.
        from heroku_manager.heroku import _stale_cutoff
        dyno = self._dyno("normal_worker.1")
        with patch.dict(os.environ, {"HEROKU_DYNO_INDEX_V1_READY": "true"}):
            self._patch_backend(client_attrs={
                "zscore": MagicMock(return_value=_stale_cutoff()),
                "zrangebyscore": MagicMock(return_value=["normal_worker.1"]),
                "scan": MagicMock(return_value=(0, ["heroku:dyno_alive:normal_worker.1"])),
            })
            self.assertFalse(dyno.index_ready)

    def test_fresh_cutoff_is_exclusive_and_matches_prune_cutoff(self):
        # Score exactly one microsecond above the prune cutoff is fresh, and
        # the fresh range lower bound must equal the prune upper bound exactly
        # (prune <= cutoff, fresh > cutoff).
        from heroku_manager.heroku import _stale_cutoff
        dyno = self._dyno("normal_worker.1")
        with patch("heroku_manager.heroku.time.time", return_value=1_700_000_000.0):
            fresh_score = _stale_cutoff() + 10  # safely above cutoff
            with patch.dict(os.environ, {"HEROKU_DYNO_INDEX_V1_READY": "true"}):
                _, client = self._patch_backend(client_attrs={
                    "zscore": MagicMock(return_value=fresh_score),
                    "zrangebyscore": MagicMock(return_value=["normal_worker.1"]),
                    "scan": MagicMock(return_value=(0, ["heroku:dyno_alive:normal_worker.1"])),
                    "zremrangebyscore": MagicMock(return_value=0),
                })
                self.assertTrue(dyno.index_ready)
                # Non-vacuous: a prune must actually run so the prune cutoff is
                # observed, not asserted against a never-called mock.
                dyno.prune_stale_index_members()
            # zremrangebyscore(key, min, max): the prune cutoff is the MAX bound.
            lower = client.zrangebyscore.call_args[0][1]
            prune = client.zremrangebyscore.call_args[0][2]
            self.assertEqual(lower, f'({prune}')


class TestIndexedSiblingValues(unittest.TestCase):
    """Phase B: _iter_sibling_values uses the index; never KEYS; exact formation
    prefix; own dyno excluded; missing values skipped; batched MGET."""

    def setUp(self):
        cache.clear()

    def tearDown(self):
        cache.clear()

    def _dyno(self, dyno_name="normal_worker.1", formation_size="standard-2x"):
        # Same opt-out as TestIndexedDynoReadiness: these tests exercise the
        # real readiness-gated destructive paths.
        dyno = make_dyno(dyno_name=dyno_name, formation_size=formation_size)
        _restore_real_index_ready(dyno)
        return dyno

    def _patch_backend(self, client_attrs=None, backend_attrs=None):
        backend = MagicMock()
        client = MagicMock()
        for name, value in (client_attrs or {}).items():
            setattr(client, name, value)
        backend.client.get_client.return_value = client
        backend.make_key.side_effect = lambda key: key
        backend.get.return_value = None
        for name, value in (backend_attrs or {}).items():
            setattr(backend, name, value)
        self._cache_patcher = patch("heroku_manager.heroku.cache", backend)
        self._cache_patcher.start()
        self.addCleanup(self._cache_patcher.stop)
        return backend, client

    def test_no_keys_call_in_any_readiness_state(self):
        # Bounded SCAN must replace cache.keys entirely
        import pickle
        dyno = self._dyno("normal_worker.1")
        with patch.dict(os.environ, {"HEROKU_DYNO_INDEX_V1_READY": "true"}):
            backend, _ = self._patch_backend(client_attrs={
                "zscore": MagicMock(return_value=time.time()),
                "zrangebyscore": MagicMock(return_value=["normal_worker.1", "normal_worker.2"]),
                "mget": MagicMock(return_value=[None, pickle.dumps(300)]),
                "scan": MagicMock(return_value=(0, ["heroku:dyno_alive:normal_worker.1", "heroku:dyno_alive:normal_worker.2"])),
            })
            backend.client.decode = _real_decoder()
            backend.keys = MagicMock(side_effect=AssertionError("KEYS forbidden"))
            list(dyno._iter_sibling_values("memory"))
        backend.keys.assert_not_called()

    def test_bounded_scan_used_when_not_ready(self):
        # Mixed window: bounded SCAN over the exact prefix, not KEYS
        dyno = self._dyno("normal_worker.1")
        scan_calls = []
        def fake_scan(cursor=0, match=None, count=None):
            scan_calls.append((match, count))
            return (0, ["heroku:dyno_memory:normal_worker.2"])
        with patch.dict(os.environ, {"HEROKU_DYNO_INDEX_V1_READY": "false"}):
            backend, _client = self._patch_backend(client_attrs={"scan": fake_scan})
            backend.get.return_value = 300
            values = list(dyno._iter_sibling_values("memory"))
        self.assertEqual([v for _, v in values], [300])
        self.assertTrue(scan_calls, "SCAN must be used when not ready")
        match, count = scan_calls[0]
        self.assertTrue(match.startswith("heroku:dyno_memory:normal_worker."))
        self.assertIsNotNone(count)

    def test_exact_formation_prefix_filters_index_members(self):
        # normal_worker_extra.1 must not match normal_worker
        import pickle
        dyno = self._dyno("normal_worker.1")
        members = ["normal_worker.1", "normal_worker.2", "normal_worker_extra.1"]
        with patch.dict(os.environ, {"HEROKU_DYNO_INDEX_V1_READY": "true"}):
            backend, _ = self._patch_backend(client_attrs={
                "zscore": MagicMock(return_value=time.time()),
                "zrangebyscore": MagicMock(return_value=members),
                "mget": MagicMock(return_value=[pickle.dumps(400), pickle.dumps(9999)]),
                "scan": MagicMock(return_value=(0, [f"heroku:dyno_alive:{m}" for m in members])),
            })
            backend.client.decode = _real_decoder()
            values = list(dyno._iter_sibling_values("memory"))
        names = [n for n, _ in values]
        self.assertNotIn("normal_worker_extra.1", names)
        self.assertEqual(names, ["normal_worker.2"])
        self.assertEqual([v for _, v in values], [400])

    def test_own_dyno_excluded(self):
        # Own dyno's hot value must never be treated as a sibling's
        import pickle
        dyno = self._dyno("normal_worker.2")
        with patch.dict(os.environ, {"HEROKU_DYNO_INDEX_V1_READY": "true"}):
            backend, _ = self._patch_backend(client_attrs={
                "zscore": MagicMock(return_value=time.time()),
                "zrangebyscore": MagicMock(return_value=["normal_worker.1", "normal_worker.2"]),
                "mget": MagicMock(return_value=[pickle.dumps(300), pickle.dumps(9999)]),
                "scan": MagicMock(return_value=(0, ["heroku:dyno_alive:normal_worker.1", "heroku:dyno_alive:normal_worker.2"])),
            })
            backend.client.decode = _real_decoder()
            values = list(dyno._iter_sibling_values("memory"))
        self.assertEqual(values, [("normal_worker.1", 300)])

    def test_missing_metric_values_skipped(self):
        import pickle
        dyno = self._dyno("normal_worker.1")
        members = ["normal_worker.1", "normal_worker.2", "normal_worker.3"]
        with patch.dict(os.environ, {"HEROKU_DYNO_INDEX_V1_READY": "true"}):
            backend, _ = self._patch_backend(client_attrs={
                "zscore": MagicMock(return_value=time.time()),
                "zrangebyscore": MagicMock(return_value=members),
                "mget": MagicMock(return_value=[None, pickle.dumps(300)]),
                "scan": MagicMock(return_value=(0, [f"heroku:dyno_alive:{m}" for m in members])),
            })
            backend.client.decode = _real_decoder()
            values = list(dyno._iter_sibling_values("memory"))
        self.assertEqual(values, [("normal_worker.3", 300)])

    def test_batched_mget_used(self):
        import pickle
        dyno = self._dyno("normal_worker.1")
        members = ["normal_worker.1", "normal_worker.2", "normal_worker.3"]
        with patch.dict(os.environ, {"HEROKU_DYNO_INDEX_V1_READY": "true"}):
            backend, client = self._patch_backend(client_attrs={
                "zscore": MagicMock(return_value=time.time()),
                "zrangebyscore": MagicMock(return_value=members),
                "mget": MagicMock(return_value=[pickle.dumps(300), pickle.dumps(400)]),
                "scan": MagicMock(return_value=(0, [f"heroku:dyno_alive:{m}" for m in members])),
            })
            backend.client.decode = _real_decoder()
            list(dyno._iter_sibling_values("memory"))
        client.mget.assert_called_once()
        args = client.mget.call_args[0][0]
        self.assertIn("heroku:dyno_memory:normal_worker.2", args)
        self.assertIn("heroku:dyno_memory:normal_worker.3", args)


class TestIndexedFailClosed(unittest.TestCase):
    """Phase B: uncertain/incomplete/error index state must never authorize
    downscale, formation-idle downscale, or zombie restart; safe upscale stays."""

    def setUp(self):
        cache.clear()

    def tearDown(self):
        cache.clear()

    def _dyno(self, dyno_name="normal_worker.1", formation_size="standard-2x"):
        dyno = make_dyno(dyno_name=dyno_name, formation_size=formation_size)
        # Dedicated fail-closed tests drive the REAL runtime predicate; the
        # fixture's seeded ready state must not mask flag-false/absent gates.
        _restore_real_index_ready(dyno)
        return dyno

    def _patch_backend(self, client_attrs=None, backend_attrs=None):
        backend = MagicMock()
        client = MagicMock()
        for name, value in (client_attrs or {}).items():
            setattr(client, name, value)
        backend.client.get_client.return_value = client
        backend.make_key.side_effect = lambda key: key
        backend.get.return_value = None
        for name, value in (backend_attrs or {}).items():
            setattr(backend, name, value)
        self._cache_patcher = patch("heroku_manager.heroku.cache", backend)
        self._cache_patcher.start()
        self.addCleanup(self._cache_patcher.stop)
        return backend, client

    def _cool_full_dyno(self, dyno_name="normal_worker.1", formation_size="standard-2x"):
        dyno = _isolate_dyno_type(make_dyno(dyno_name=dyno_name, formation_size=formation_size))
        # Dedicated fail-closed tests drive the REAL runtime predicate; the
        # fixture's seeded ready state must not mask flag-false/absent gates.
        _restore_real_index_ready(dyno)
        type(dyno).current_memory_usage = PropertyMock(return_value=200)
        type(dyno).current_memory_usage_percentage = PropertyMock(return_value=200 / 1024 * 100)
        type(dyno).detected_r14 = PropertyMock(return_value=False)
        type(dyno).detected_r15 = PropertyMock(return_value=False)
        type(dyno).no_tasks_in_queue = PropertyMock(return_value=True)
        type(dyno).downscale_on_non_empty_queue = PropertyMock(return_value=False)
        type(dyno).original_formation_size = PropertyMock(return_value="standard-1x")
        type(dyno).previous_formation_size = PropertyMock(return_value="standard-1x")
        return dyno

    def test_flag_false_blocks_downscale(self):
        # Phase B contract: destructive actions (downscale) require runtime
        # readiness, which requires the flag true.  Flag false/absent must
        # never authorize a downscale even when memory is cool.
        dyno = self._cool_full_dyno()
        with patch.dict(os.environ, {"HEROKU_DYNO_INDEX_V1_READY": "false"}, clear=False):
            backend, _ = self._patch_backend(client_attrs={
                "scan": MagicMock(return_value=(0, [])),
            })
            backend.get.return_value = None
            self.assertFalse(dyno.allow_downscale)

    def test_flag_false_blocks_shutdown_downscale(self):
        dyno = self._cool_full_dyno()
        with patch.dict(os.environ, {"HEROKU_DYNO_INDEX_V1_READY": "false"}, clear=False):
            self._patch_backend(client_attrs={
                "scan": MagicMock(return_value=(0, [])),
            })
            self.assertFalse(dyno.allow_downscale_on_shutdown)

    def test_flag_absent_blocks_formation_idle(self):
        dyno = self._cool_full_dyno()
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("HEROKU_DYNO_INDEX_V1_READY", None)
            self._patch_backend(client_attrs={
                "scan": MagicMock(return_value=(0, [])),
            })
            with patch.object(type(dyno), "avg_load_1min",
                              new_callable=PropertyMock, return_value=0.4):
                self.assertFalse(dyno.is_formation_idle)

    def test_flag_true_but_unreconciled_blocks_downscale(self):
        dyno = self._cool_full_dyno()
        # own score missing → not ready → downscale blocked
        with patch.dict(os.environ, {"HEROKU_DYNO_INDEX_V1_READY": "true"}):
            self._patch_backend(client_attrs={"zscore": MagicMock(return_value=None)})
            self.assertFalse(dyno.allow_downscale)

    def test_flag_true_reconciled_allows_shutdown_downscale_when_cool(self):
        dyno = self._cool_full_dyno()
        with patch.dict(os.environ, {"HEROKU_DYNO_INDEX_V1_READY": "true"}):
            self._patch_backend(client_attrs={
                "zscore": MagicMock(return_value=time.time()),
                "zrangebyscore": MagicMock(return_value=["normal_worker.1"]),
                "scan": MagicMock(return_value=(0, ["heroku:dyno_alive:normal_worker.1"])),
            })
            self.assertTrue(dyno.allow_downscale_on_shutdown)

    def test_flag_true_reconciled_allows_downscale(self):
        dyno = self._cool_full_dyno()
        with patch.dict(os.environ, {"HEROKU_DYNO_INDEX_V1_READY": "true"}):
            self._patch_backend(client_attrs={
                "zscore": MagicMock(return_value=time.time()),
                "zrangebyscore": MagicMock(return_value=["normal_worker.1"]),
                "scan": MagicMock(return_value=(0, ["heroku:dyno_alive:normal_worker.1"])),
            })
            self.assertTrue(dyno.allow_downscale)

    def test_flag_false_blocks_zombie_restart(self):
        # Phase B contract: zombie restart is destructive and requires index
        # readiness.  Flag false must block the restart even when the SCAN
        # sees a stale sibling.
        dyno = self._dyno("normal_worker.1")
        stale = timezone.now() - timezone.timedelta(seconds=django_settings.DYNO_ZOMBIE_THRESHOLD + 10)
        with patch.dict(os.environ, {"HEROKU_DYNO_INDEX_V1_READY": "false"}, clear=False):
            backend, _ = self._patch_backend(client_attrs={
                "scan": MagicMock(return_value=(0, ["heroku:dyno_alive:normal_worker.2"])),
                "zremrangebyscore": MagicMock(return_value=1),
            })
            backend.get.return_value = stale
            with patch.object(dyno, "restart_zombie_dyno") as mock_restart:
                dyno.check_for_sibling_zombie_dynos()
        mock_restart.assert_not_called()

    def test_flag_false_still_prunes_stale_index_members_once(self):
        # Prune is a non-destructive cleanup: it must run once under the lock
        # even when readiness is false, then the check returns without
        # restarting.
        dyno = self._dyno("normal_worker.1")
        with patch.dict(os.environ, {"HEROKU_DYNO_INDEX_V1_READY": "false"}, clear=False):
            _, client = self._patch_backend(client_attrs={
                "scan": MagicMock(return_value=(0, [])),
                "zremrangebyscore": MagicMock(return_value=3),
            })
            with patch.object(dyno, "restart_zombie_dyno") as mock_restart:
                dyno.check_for_sibling_zombie_dynos()
        client.zremrangebyscore.assert_called_once()
        mock_restart.assert_not_called()

    def test_flag_true_zombie_restart_uses_index_members(self):
        import pickle
        dyno = self._dyno("normal_worker.1")
        stale = timezone.now() - timezone.timedelta(seconds=django_settings.DYNO_ZOMBIE_THRESHOLD + 10)
        with patch.dict(os.environ, {"HEROKU_DYNO_INDEX_V1_READY": "true"}):
            backend, client = self._patch_backend(client_attrs={
                "zscore": MagicMock(return_value=time.time()),
                "zrangebyscore": MagicMock(return_value=["normal_worker.2"]),
                "mget": MagicMock(return_value=[pickle.dumps(stale)]),
                "scan": MagicMock(return_value=(0, ["heroku:dyno_alive:normal_worker.2"])),
            })
            backend.client.decode = _real_decoder()
            with patch.object(dyno, "restart_zombie_dyno") as mock_restart:
                dyno.check_for_sibling_zombie_dynos()
        mock_restart.assert_called_once_with("normal_worker.2")

    def test_incomplete_index_never_authorizes_zombie_restart(self):
        dyno = self._dyno("normal_worker.1")
        with patch.dict(os.environ, {"HEROKU_DYNO_INDEX_V1_READY": "true"}):
            # SCAN finds member not yet in index → incomplete → no restart
            backend, _ = self._patch_backend(client_attrs={
                "zscore": MagicMock(return_value=time.time()),
                "zrangebyscore": MagicMock(return_value=[]),
                "scan": MagicMock(return_value=(0, ["heroku:dyno_alive:normal_worker.2"])),
            })
            backend.client.decode = _real_decoder()
            with patch.object(dyno, "restart_zombie_dyno") as mock_restart:
                dyno.check_for_sibling_zombie_dynos()
        mock_restart.assert_not_called()

    def test_incomplete_index_logs_no_misleading_restarting(self):
        # Flag true but index incomplete: no restart may happen, so no
        # "Restarting..." log may be emitted either.
        dyno = self._dyno("normal_worker.1")
        stale = timezone.now() - timezone.timedelta(seconds=django_settings.DYNO_ZOMBIE_THRESHOLD + 10)
        with patch.dict(os.environ, {"HEROKU_DYNO_INDEX_V1_READY": "true"}):
            backend, client = self._patch_backend(client_attrs={
                "zscore": MagicMock(return_value=time.time()),
                "zrangebyscore": MagicMock(return_value=[]),
                "scan": MagicMock(return_value=(0, ["heroku:dyno_alive:normal_worker.2"])),
            })
            backend.get.return_value = stale
            with patch.object(logging.getLogger("heroku_manager.heroku"), "error") as mock_error:
                with patch.object(dyno, "restart_zombie_dyno") as mock_restart:
                    dyno.check_for_sibling_zombie_dynos()
        mock_restart.assert_not_called()
        # Non-vacuous: the readiness predicate actually evaluated the own
        # index score (missing here → not ready), so the check had a real
        # reason to return before any restart could be considered.
        client.zscore.assert_called_once()
        mock_error.assert_not_called()


class TestIndexedPrune(unittest.TestCase):
    """Phase B: one bounded ZREMRANGEBYSCORE under the existing zombie-check lock."""

    def setUp(self):
        cache.clear()

    def tearDown(self):
        cache.clear()

    def _dyno(self, dyno_name="normal_worker.1"):
        dyno = make_dyno(dyno_name=dyno_name)
        _restore_real_index_ready(dyno)
        return dyno

    def _patch_backend(self, client_attrs=None, backend_attrs=None):
        backend = MagicMock()
        client = MagicMock()
        for name, value in (client_attrs or {}).items():
            setattr(client, name, value)
        backend.client.get_client.return_value = client
        backend.make_key.side_effect = lambda key: key
        backend.get.return_value = None
        for name, value in (backend_attrs or {}).items():
            setattr(backend, name, value)
        self._cache_patcher = patch("heroku_manager.heroku.cache", backend)
        self._cache_patcher.start()
        self.addCleanup(self._cache_patcher.stop)
        return backend, client

    def test_prune_uses_bounded_zremrangebyscore_under_lock(self):
        # The zombie check holds the existing lock; the prune must run inside it
        # as exactly one bounded ZREMRANGEBYSCORE.
        dyno = self._dyno()
        with patch.dict(os.environ, {"HEROKU_DYNO_INDEX_V1_READY": "false"}):
            _, client = self._patch_backend(client_attrs={
                "scan": MagicMock(return_value=(0, [])),
                "zremrangebyscore": MagicMock(return_value=1),
            })
            with patch.object(dyno, "restart_zombie_dyno") as mock_restart:
                dyno.check_for_sibling_zombie_dynos()
        client.zremrangebyscore.assert_called_once()
        args = client.zremrangebyscore.call_args[0]
        self.assertIn("heroku:dynos:v1:floship", str(args[0]))
        self.assertEqual(args[1], "-inf")
        self.assertIsInstance(args[2], (int, float))
        mock_restart.assert_not_called()

    def test_cap_failure_blocks_destructive_actions(self):
        # Cap exceeded: 501 keys > 500 cap → readers must fail closed (no
        # downscale, no zombie restart) with no KEYS fallback.
        dyno = self._dyno("normal_worker.1")
        many_keys = [f"heroku:dyno_alive:normal_worker.{i}" for i in range(1, 502)]
        with patch.dict(os.environ, {"HEROKU_DYNO_INDEX_V1_READY": "false"}):
            backend, _ = self._patch_backend(client_attrs={
                "scan": MagicMock(return_value=(0, many_keys)),
                "zremrangebyscore": MagicMock(return_value=0),
            })
            backend.keys = MagicMock(side_effect=AssertionError("KEYS forbidden"))
            with patch.object(dyno, "restart_zombie_dyno") as mock_restart:
                dyno.check_for_sibling_zombie_dynos()
        mock_restart.assert_not_called()
        backend.keys.assert_not_called()

    def test_no_keys_route_when_raw_client_missing(self):
        # Missing raw client → readers return uncertain, never fall back to
        # Django cache.keys() (dynamic or lexical).
        dyno = self._dyno("normal_worker.1")
        backend = MagicMock()
        backend.client.get_client.side_effect = AttributeError("no raw client")
        backend.make_key.side_effect = lambda key: key
        backend.get.return_value = None
        backend.keys = MagicMock(side_effect=AssertionError("KEYS forbidden"))
        with patch.dict(os.environ, {"HEROKU_DYNO_INDEX_V1_READY": "false"}):
            with patch("heroku_manager.heroku.cache", backend):
                with patch.object(dyno, "restart_zombie_dyno") as mock_restart:
                    dyno.check_for_sibling_zombie_dynos()
        mock_restart.assert_not_called()
        backend.keys.assert_not_called()

    def test_no_keys_route_when_scan_cap_exceeded(self):
        # Cap exceeded → uncertain, never KEYS fallback.
        dyno = self._dyno("normal_worker.1")
        many_keys = [f"heroku:dyno_alive:normal_worker.{i}" for i in range(1, 502)]
        backend = MagicMock()
        client = MagicMock()
        client.scan.return_value = (0, many_keys)
        client.zremrangebyscore.return_value = 0
        backend.client.get_client.return_value = client
        backend.make_key.side_effect = lambda key: key
        backend.get.return_value = None
        backend.keys = MagicMock(side_effect=AssertionError("KEYS forbidden"))
        with patch.dict(os.environ, {"HEROKU_DYNO_INDEX_V1_READY": "false"}):
            with patch("heroku_manager.heroku.cache", backend):
                with patch.object(dyno, "restart_zombie_dyno") as mock_restart:
                    dyno.check_for_sibling_zombie_dynos()
        mock_restart.assert_not_called()
        backend.keys.assert_not_called()


class TestIndexedPhysicalKeyContract(unittest.TestCase):
    """Phase B contract: every raw SCAN/MGET key derives through cache.make_key
    physical prefix; raw bytes normalize safely; fresh range is (cutoff, +inf);
    one readiness SCAN per autoscale cycle."""

    def setUp(self):
        cache.clear()

    def tearDown(self):
        cache.clear()

    def _dyno(self, dyno_name="normal_worker.1", formation_size="standard-2x"):
        dyno = make_dyno(dyno_name=dyno_name, formation_size=formation_size)
        _restore_real_index_ready(dyno)
        return dyno

    def _patch_backend(self, client_attrs=None, backend_attrs=None):
        backend = MagicMock()
        client = MagicMock()
        for name, value in (client_attrs or {}).items():
            setattr(client, name, value)
        backend.client.get_client.return_value = client
        backend.make_key.side_effect = lambda key, version=None: f"prefix:1:{key}"
        backend.get.return_value = None
        for name, value in (backend_attrs or {}).items():
            setattr(backend, name, value)
        self._cache_patcher = patch("heroku_manager.heroku.cache", backend)
        self._cache_patcher.start()
        self.addCleanup(self._cache_patcher.stop)
        return backend, client

    @patch("heroku_manager.heroku.time.time", return_value=1_700_000_000.0)
    def test_fresh_members_uses_cutoff_to_plus_inf(self, mock_time):
        from heroku_manager.heroku import _stale_cutoff
        dyno = self._dyno()
        with patch.dict(os.environ, {"HEROKU_DYNO_INDEX_V1_READY": "true"}):
            _, client = self._patch_backend(client_attrs={
                "zscore": MagicMock(return_value=time.time()),
                "zrangebyscore": MagicMock(return_value=["normal_worker.1"]),
                "scan": MagicMock(return_value=(0, ["prefix:1:heroku:dyno_alive:normal_worker.1"])),
            })
            dyno.index_ready
        args = client.zrangebyscore.call_args[0]
        self.assertEqual(args[0], "prefix:1:heroku:dynos:v1:floship")
        self.assertEqual(args[1], f'({_stale_cutoff()}')
        self.assertEqual(args[2], "+inf")

    def test_scan_uses_physical_make_key_prefix_and_bytes(self):
        # Non-destructive sibling path in the mixed window: the compatibility
        # SCAN must use the physical make_key prefix, normalize raw bytes, and
        # still surface a hot sibling for safe upscale advocacy.
        dyno = self._dyno("normal_worker.1", formation_size="standard-2x")
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("HEROKU_DYNO_INDEX_V1_READY", None)
            backend, client = self._patch_backend(client_attrs={
                "scan": MagicMock(return_value=(
                    0,
                    [b"prefix:1:heroku:dyno_memory:normal_worker.2"],
                )),
            })
            # SCAN physical keys decode into the logical memory keys; the
            # sibling reader resolves values via cache.get.
            backend.get.side_effect = lambda key: (
                2183 if key == "heroku:dyno_memory:normal_worker.2" else None
            )
            values = list(dyno._iter_sibling_values("memory"))
        self.assertEqual(values, [("normal_worker.2", 2183)])
        match = client.scan.call_args.kwargs["match"]
        self.assertEqual(match, "prefix:1:heroku:dyno_memory:normal_worker.*")

    def test_mget_uses_physical_make_key_keys(self):
        import pickle
        dyno = self._dyno("normal_worker.1")
        with patch.dict(os.environ, {"HEROKU_DYNO_INDEX_V1_READY": "true"}):
            backend, client = self._patch_backend(client_attrs={
                "zscore": MagicMock(return_value=time.time()),
                "zrangebyscore": MagicMock(return_value=["normal_worker.1", "normal_worker.2"]),
                "mget": MagicMock(return_value=[pickle.dumps(300)]),
                "scan": MagicMock(return_value=(
                    0,
                    ["prefix:1:heroku:dyno_alive:normal_worker.1",
                     "prefix:1:heroku:dyno_alive:normal_worker.2"],
                )),
            })
            backend.client.decode = _real_decoder()
            values = list(dyno._iter_sibling_values("memory"))
        self.assertEqual(values, [("normal_worker.2", 300)])
        self.assertEqual(
            client.mget.call_args[0][0],
            ["prefix:1:heroku:dyno_memory:normal_worker.2"],
        )

    def test_raw_zset_bytes_members_normalize_to_dyno_names(self):
        # redis-py returns ZSET members as bytes; normalization must turn them
        # into str dyno names before the formation prefix/own-dyno filtering
        # and MGET key construction.
        import pickle
        dyno = self._dyno("normal_worker.1")
        with patch.dict(os.environ, {"HEROKU_DYNO_INDEX_V1_READY": "true"}):
            backend, client = self._patch_backend(client_attrs={
                "zscore": MagicMock(return_value=time.time()),
                "zrangebyscore": MagicMock(return_value=[
                    b"normal_worker.1", b"normal_worker.2", b"normal_worker_extra.1",
                ]),
                "mget": MagicMock(return_value=[pickle.dumps(300)]),
                "scan": MagicMock(return_value=(
                    0,
                    ["prefix:1:heroku:dyno_alive:normal_worker.1",
                     "prefix:1:heroku:dyno_alive:normal_worker.2"],
                )),
            })
            backend.client.decode = _real_decoder()
            values = list(dyno._iter_sibling_values("memory"))
        self.assertEqual(values, [("normal_worker.2", 300)])
        self.assertEqual(
            client.mget.call_args[0][0],
            ["prefix:1:heroku:dyno_memory:normal_worker.2"],
        )

    def test_mget_values_decoded_through_django_redis_decode(self):
        # Raw MGET results are serialized bytes; callers compare Python
        # numbers/datetimes, so each non-None raw value must be decoded with
        # the django-redis DefaultClient.decode (never compared as bytes).
        from django_redis.client.default import DefaultClient
        import pickle
        dyno = self._dyno("normal_worker.1")
        # pickle.dumps(300) is not int()-parseable, so DefaultClient.decode
        # falls through to the serializer and returns 300.
        raw = pickle.dumps(300)
        with patch.dict(os.environ, {"HEROKU_DYNO_INDEX_V1_READY": "true"}):
            backend, _ = self._patch_backend(client_attrs={
                "zscore": MagicMock(return_value=time.time()),
                "zrangebyscore": MagicMock(return_value=[
                    "normal_worker.1", "normal_worker.2",
                ]),
                "mget": MagicMock(return_value=[raw, None]),
                "scan": MagicMock(return_value=(
                    0,
                    ["prefix:1:heroku:dyno_alive:normal_worker.1",
                     "prefix:1:heroku:dyno_alive:normal_worker.2"],
                )),
            })
            # The production adapter captures the decoder from cache.client
            # (the django-redis DefaultClient); give the mock backend a real
            # DefaultClient.decode and wrap it to prove the decode path runs.
            with patch.object(DefaultClient, "decode", wraps=DefaultClient.decode) as decode:
                backend.client.decode = real_decoder()
                values = list(dyno._iter_sibling_values("memory"))
        self.assertEqual(values, [("normal_worker.2", 300)])
        self.assertGreaterEqual(decode.call_count, 1)

    def test_stale_index_member_pruned_not_restarted(self):
        dyno = self._dyno("normal_worker.1")
        with patch.dict(os.environ, {"HEROKU_DYNO_INDEX_V1_READY": "true"}):
            _, client = self._patch_backend(client_attrs={
                "zscore": MagicMock(return_value=time.time()),
                "zrangebyscore": MagicMock(return_value=["normal_worker.2"]),
                "mget": MagicMock(return_value=[None]),
                "scan": MagicMock(return_value=(
                    0,
                    ["prefix:1:heroku:dyno_alive:normal_worker.1",
                     "prefix:1:heroku:dyno_alive:normal_worker.2"],
                )),
                "zremrangebyscore": MagicMock(return_value=1),
            })
            with patch.object(dyno, "restart_zombie_dyno") as mock_restart:
                dyno.check_for_sibling_zombie_dynos()
        # Stale member (score ≤ cutoff) is pruned once and never restarted.
        client.zremrangebyscore.assert_called_once()
        mock_restart.assert_not_called()

    def test_readiness_reused_one_scan_per_cycle(self):
        dyno = self._dyno("normal_worker.1")
        scan_returns = [
            (0, ["prefix:1:heroku:dyno_alive:normal_worker.1"]),
            (0, ["prefix:1:heroku:dyno_alive:normal_worker.2"]),
        ]
        scan_calls = []
        def fake_scan(cursor=0, match=None, count=None):
            scan_calls.append((cursor, match, count))
            return scan_returns.pop(0)
        with patch.dict(os.environ, {"HEROKU_DYNO_INDEX_V1_READY": "true"}):
            _, client = self._patch_backend(client_attrs={
                "zscore": MagicMock(return_value=time.time()),
                "zrangebyscore": MagicMock(return_value=["normal_worker.1"]),
                "scan": fake_scan,
                "mget": MagicMock(return_value=[None]),
                "zremrangebyscore": MagicMock(return_value=0),
            })
            # readiness, memory sibling read, load sibling read, and zombie check
            # within one autoscale cycle must reuse the first SCAN result.
            dyno.index_ready
            list(dyno._iter_sibling_values("memory"))
            list(dyno._iter_sibling_values("load"))
            with patch.object(dyno, "restart_zombie_dyno") as mock_restart:
                dyno.check_for_sibling_zombie_dynos()
        self.assertEqual(len(scan_calls), 1)
        mock_restart.assert_not_called()

    def test_flag_false_compat_scan_keeps_sibling_upscale_evidence(self):
        # Mixed window with physical keys: bounded SCAN must still surface a hot
        # sibling so safe upscale advocacy works (never silently empty).
        dyno = self._dyno("normal_worker.1", formation_size="standard-2x")
        with patch.dict(os.environ, {"HEROKU_DYNO_INDEX_V1_READY": "false"}):
            backend, client = self._patch_backend(client_attrs={
                "scan": MagicMock(return_value=(
                    0,
                    [b"prefix:1:heroku:dyno_memory:normal_worker.2"],
                )),
                "zremrangebyscore": MagicMock(return_value=0),
            })
            backend.get.side_effect = lambda key: (
                2183 if key == "heroku:dyno_memory:normal_worker.2" else None
            )
            self.assertTrue(dyno.any_sibling_requires_upscale)
        self.assertEqual(
            client.scan.call_args.kwargs["match"],
            "prefix:1:heroku:dyno_memory:normal_worker.*",
        )

    def test_not_ready_compat_scan_still_detects_hot_sibling_for_upscale(self):
        # Non-destructive sibling upscale evidence must remain available in the
        # not-ready compatibility-SCAN window.
        dyno = self._dyno("normal_worker.1", formation_size="standard-2x")
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("HEROKU_DYNO_INDEX_V1_READY", None)
            backend, _ = self._patch_backend(client_attrs={
                "scan": MagicMock(return_value=(
                    0,
                    [b"prefix:1:heroku:dyno_memory:normal_worker.2"],
                )),
            })
            backend.get.side_effect = lambda key: (
                2183 if key == "heroku:dyno_memory:normal_worker.2" else None
            )
            self.assertTrue(dyno.any_sibling_requires_upscale)


if __name__ == "__main__":
    unittest.main()
