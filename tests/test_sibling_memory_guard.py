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
from tests.conftest import patch_index_backend, real_decoder
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
        """Patch a fake raw client so the index reader serves the seeded memory store."""
        return patch_index_backend(self._memory_store)

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
        """Patch a fake raw client so the index reader serves the seeded memory store."""
        return patch_index_backend(self._memory_store)

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


def _patch_index_backend(memory_store):
    """Patch a fake raw client so the index reader serves the provided key store."""
    return patch_index_backend(memory_store)


class TestR14R15GuardInteractions(unittest.TestCase):
    """R14 and R15 flags interact with allow_downscale and allow_downscale_on_shutdown."""

    def setUp(self):
        cache.clear()

    def tearDown(self):
        cache.clear()

    def test_r15_blocks_allow_downscale(self):
        dyno = _make_full_dyno(own_memory=200, r15=True)
        with _patch_index_backend({}):
            self.assertFalse(dyno.allow_downscale)

    def test_r14_does_not_block_allow_downscale_when_memory_is_cool(self):
        dyno = _make_full_dyno(own_memory=200, r14=True)
        with _patch_index_backend({}):
            self.assertTrue(dyno.allow_downscale)

    def test_r15_blocks_allow_downscale_on_shutdown(self):
        dyno = _make_full_dyno(own_memory=200, r15=True)
        with _patch_index_backend({}):
            self.assertFalse(dyno.allow_downscale_on_shutdown)

    def test_r14_does_not_block_allow_downscale_on_shutdown_when_memory_is_cool(self):
        dyno = _make_full_dyno(own_memory=200, r14=True)
        with _patch_index_backend({}):
            self.assertTrue(dyno.allow_downscale_on_shutdown)

    def test_r14_and_r15_both_absent_allows_shutdown_downscale_when_cool(self):
        dyno = _make_full_dyno(own_memory=200, r14=False, r15=False)
        with _patch_index_backend({}):
            self.assertTrue(dyno.allow_downscale_on_shutdown)

    def test_r15_without_r14_still_blocks_downscale(self):
        # R15 alone: requires_upscale=True is enough to block
        dyno = _make_full_dyno(own_memory=200, r14=False, r15=True)
        with _patch_index_backend({}):
            self.assertFalse(dyno.allow_downscale)

    def test_neither_r14_nor_r15_cool_memory_allows_downscale(self):
        dyno = _make_full_dyno(own_memory=200, r14=False, r15=False)
        with _patch_index_backend({}):
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
        with _patch_index_backend(self._store):
            self.assertFalse(dyno.allow_downscale_on_shutdown)

    def test_allows_shutdown_downscale_when_all_siblings_cool(self):
        dyno = _make_full_dyno("normal_worker.1", own_memory=200)
        self._set_mem("normal_worker.2", 300)
        with _patch_index_backend(self._store):
            self.assertTrue(dyno.allow_downscale_on_shutdown)

    def test_allows_shutdown_downscale_with_no_siblings(self):
        dyno = _make_full_dyno("normal_worker.1", own_memory=200)
        with _patch_index_backend(self._store):
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
        with _patch_index_backend({}):
            # Must not raise TypeError
            result = dyno.is_still_high_memory_usage_for_downscale
            self.assertIsInstance(result, bool)

    def test_zero_own_memory_allow_downscale_returns_bool(self):
        dyno = _make_full_dyno(own_memory=0)
        with _patch_index_backend({}):
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
        with patch_index_backend(dict(all_keys)):
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
        with _patch_index_backend(store):
            self.assertFalse(dyno.any_sibling_still_high_memory)

    def test_allow_downscale_false_at_base_size_due_to_threshold(self):
        dyno = _make_full_dyno(formation_size="standard-1x", own_memory=1)
        with _patch_index_backend({}):
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
        with _patch_index_backend(store):
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
        with _patch_index_backend(self._store):
            self.assertTrue(dyno.any_sibling_requires_upscale)

    def test_sibling_below_upscale_threshold_returns_false(self):
        # standard-2x: threshold 819.2 MB; sibling at 317 MB → False
        dyno = make_dyno("normal_worker.2", formation_size="standard-2x")
        self._set_mem("normal_worker.1", 317)
        with _patch_index_backend(self._store):
            self.assertFalse(dyno.any_sibling_requires_upscale)

    def test_excludes_self(self):
        dyno = make_dyno("normal_worker.1", formation_size="standard-2x")
        self._set_mem("normal_worker.1", 2183)
        with _patch_index_backend(self._store):
            self.assertFalse(dyno.any_sibling_requires_upscale)

    def test_no_siblings_returns_false(self):
        dyno = make_dyno("normal_worker.1", formation_size="standard-2x")
        with _patch_index_backend(self._store):
            self.assertFalse(dyno.any_sibling_requires_upscale)

    def test_formation_name_isolation(self):
        # normal_worker_extra.1 at 9999 MB must NOT match normal_worker
        dyno = make_dyno("normal_worker.2", formation_size="standard-2x")
        self._set_mem("normal_worker_extra.1", 9999)
        with _patch_index_backend(self._store):
            self.assertFalse(dyno.any_sibling_requires_upscale)

    def test_at_exact_threshold_returns_false(self):
        # Strict > (not >=): threshold = 1024 * 80% = 819.2; sibling at 819.2 → False
        dyno = make_dyno("normal_worker.2", formation_size="standard-2x")
        threshold = DYNO_SIZES["standard-2x"]["memory"] * django_settings.UPSCALE_PERCENTAGE_HIGH_MEM_USE / 100
        self._set_mem("normal_worker.1", threshold)
        with _patch_index_backend(self._store):
            self.assertFalse(dyno.any_sibling_requires_upscale)

    def test_none_memory_treated_as_absent(self):
        dyno = make_dyno("normal_worker.2", formation_size="standard-2x")
        key = "heroku:dyno_memory:normal_worker.1"
        cache.set(key, None, timeout=60)
        self._store[key] = None
        with _patch_index_backend(self._store):
            self.assertFalse(dyno.any_sibling_requires_upscale)


class TestQueueGating(unittest.TestCase):
    """allow_downscale respects downscale_on_non_empty_queue and no_tasks_in_queue."""

    def setUp(self):
        cache.clear()

    def tearDown(self):
        cache.clear()

    def test_tasks_in_queue_blocks_downscale_when_flag_false(self):
        dyno = _make_full_dyno(own_memory=200, no_tasks=False, downscale_on_non_empty=False)
        with _patch_index_backend({}):
            self.assertFalse(dyno.allow_downscale)

    def test_tasks_in_queue_allows_downscale_when_flag_true(self):
        dyno = _make_full_dyno(own_memory=200, no_tasks=False, downscale_on_non_empty=True)
        with _patch_index_backend({}):
            self.assertTrue(dyno.allow_downscale)

    def test_no_tasks_in_queue_allows_downscale_regardless_of_flag(self):
        dyno = _make_full_dyno(own_memory=200, no_tasks=True, downscale_on_non_empty=False)
        with _patch_index_backend({}):
            self.assertTrue(dyno.allow_downscale)



class TestIndexedDynoReadiness(unittest.TestCase):
    """Phase C: runtime readiness = the current dyno's own fresh index score."""

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
        client.get.return_value = None  # degraded marker absent unless the test sets it
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

    def test_not_ready_without_raw_client(self):
        # A LocMem-style backend exposes no raw client: readiness fails closed.
        dyno = self._dyno()
        self.assertFalse(dyno.index_ready)

    def test_not_ready_without_fresh_own_index_score(self):
        dyno = self._dyno()
        self._patch_backend(client_attrs={"zscore": MagicMock(return_value=None)})
        self.assertFalse(dyno.index_ready)

    def test_ready_from_fresh_own_index_score(self):
        dyno = self._dyno("normal_worker.1")
        _, client = self._patch_backend(client_attrs={
            "zscore": MagicMock(return_value=time.time()),
        })
        self.assertTrue(dyno.index_ready)
        # Phase C: readiness is the own score alone - no SCAN, no flag.
        client.scan.assert_not_called()

    def test_env_flag_no_longer_gates_readiness(self):
        # The rollout flag is gone: a fresh own score is ready even when the
        # retired flag is still set to false in the environment.
        dyno = self._dyno("normal_worker.1")
        os.environ["HEROKU_DYNO_INDEX_V1_READY"] = "false"
        self.addCleanup(os.environ.pop, "HEROKU_DYNO_INDEX_V1_READY", None)
        self._patch_backend(client_attrs={"zscore": MagicMock(return_value=time.time())})
        self.assertTrue(dyno.index_ready)

    @patch("heroku_manager.heroku.time.time", return_value=1_700_000_000.0)
    def test_not_ready_when_own_index_score_equals_cutoff(self, mock_time):
        # Score at/under the prune cutoff is stale: readiness must be false.
        from heroku_manager.heroku import _stale_cutoff
        dyno = self._dyno("normal_worker.1")
        self._patch_backend(client_attrs={"zscore": MagicMock(return_value=_stale_cutoff())})
        self.assertFalse(dyno.index_ready)

    def test_fresh_cutoff_is_exclusive_and_matches_prune_cutoff(self):
        # The sibling read's fresh range lower bound must equal the prune's
        # upper bound exactly (prune <= cutoff, fresh > cutoff).
        from heroku_manager.heroku import _stale_cutoff
        dyno = self._dyno("normal_worker.1")
        with patch("heroku_manager.heroku.time.time", return_value=1_700_000_000.0):
            _, client = self._patch_backend(client_attrs={
                "zscore": MagicMock(return_value=_stale_cutoff() + 10),
                "zrangebyscore": MagicMock(return_value=[]),
                "zremrangebyscore": MagicMock(return_value=0),
            })
            # Both paths run so both cutoffs are observed, never asserted
            # against a never-called mock.
            list(dyno._iter_sibling_values("memory"))
            dyno.prune_stale_index_members()
            # zremrangebyscore(key, min, max): the prune cutoff is the MAX bound.
            lower = client.zrangebyscore.call_args[0][1]
            prune = client.zremrangebyscore.call_args[0][2]
            self.assertEqual(lower, f'({prune}')


class TestIndexedSiblingValues(unittest.TestCase):
    """Phase C: _iter_sibling_values reads the app index only; never SCAN, never
    KEYS; exact formation prefix; own dyno excluded; missing values skipped;
    batched MGET; an uncertain member read yields nothing."""

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
        client.get.return_value = None  # degraded marker absent unless the test sets it
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

    def test_no_keys_or_scan_call_in_any_readiness_state(self):
        # Phase C: sibling reads are index-only - no KEYS and no SCAN.
        import pickle
        dyno = self._dyno("normal_worker.1")
        backend, client = self._patch_backend(client_attrs={
            "zscore": MagicMock(return_value=time.time()),
            "zrangebyscore": MagicMock(return_value=["normal_worker.1", "normal_worker.2"]),
            "mget": MagicMock(return_value=[pickle.dumps(300)]),
        })
        client.scan.side_effect = AssertionError("SCAN removed in Phase C")
        backend.keys = MagicMock(side_effect=AssertionError("KEYS forbidden"))
        backend.client.decode = _real_decoder()
        values = list(dyno._iter_sibling_values("memory"))
        self.assertEqual(values, [("normal_worker.2", 300)])
        backend.keys.assert_not_called()
        client.scan.assert_not_called()

    def test_uncertain_index_read_yields_no_siblings(self):
        # None from ZRANGEBYSCORE means the member list is unknown: yield
        # nothing rather than a partial fleet, and never fall back to SCAN.
        dyno = self._dyno("normal_worker.1")
        backend, client = self._patch_backend(client_attrs={
            "zscore": MagicMock(return_value=None),
            "zrangebyscore": MagicMock(return_value=None),
        })
        client.scan.side_effect = AssertionError("SCAN removed in Phase C")
        backend.get.return_value = 300
        self.assertEqual(list(dyno._iter_sibling_values("memory")), [])
        client.scan.assert_not_called()
        client.mget.assert_not_called()

    def test_exact_formation_prefix_filters_index_members(self):
        # normal_worker_extra.1 must not match normal_worker
        import pickle
        dyno = self._dyno("normal_worker.1")
        members = ["normal_worker.1", "normal_worker.2", "normal_worker_extra.1"]
        backend, _ = self._patch_backend(client_attrs={
            "zscore": MagicMock(return_value=time.time()),
            "zrangebyscore": MagicMock(return_value=members),
            "mget": MagicMock(return_value=[pickle.dumps(400), pickle.dumps(9999)]),
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
        backend, _ = self._patch_backend(client_attrs={
            "zscore": MagicMock(return_value=time.time()),
            "zrangebyscore": MagicMock(return_value=["normal_worker.1", "normal_worker.2"]),
            "mget": MagicMock(return_value=[pickle.dumps(300), pickle.dumps(9999)]),
        })
        backend.client.decode = _real_decoder()
        values = list(dyno._iter_sibling_values("memory"))
        self.assertEqual(values, [("normal_worker.1", 300)])

    def test_missing_metric_values_skipped(self):
        import pickle
        dyno = self._dyno("normal_worker.1")
        members = ["normal_worker.1", "normal_worker.2", "normal_worker.3"]
        backend, _ = self._patch_backend(client_attrs={
            "zscore": MagicMock(return_value=time.time()),
            "zrangebyscore": MagicMock(return_value=members),
            "mget": MagicMock(return_value=[None, pickle.dumps(300)]),
        })
        backend.client.decode = _real_decoder()
        values = list(dyno._iter_sibling_values("memory"))
        self.assertEqual(values, [("normal_worker.3", 300)])

    def test_batched_mget_used(self):
        import pickle
        dyno = self._dyno("normal_worker.1")
        members = ["normal_worker.1", "normal_worker.2", "normal_worker.3"]
        backend, client = self._patch_backend(client_attrs={
            "zscore": MagicMock(return_value=time.time()),
            "zrangebyscore": MagicMock(return_value=members),
            "mget": MagicMock(return_value=[pickle.dumps(300), pickle.dumps(400)]),
        })
        backend.client.decode = _real_decoder()
        list(dyno._iter_sibling_values("memory"))
        client.mget.assert_called_once()
        args = client.mget.call_args[0][0]
        self.assertIn("heroku:dyno_memory:normal_worker.2", args)
        self.assertIn("heroku:dyno_memory:normal_worker.3", args)


class TestIndexedFailClosed(unittest.TestCase):
    """Phase C: an unproven own index score must never authorize downscale,
    formation-idle downscale, or zombie restart; sibling reads stay index-only."""

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
        client.get.return_value = None  # degraded marker absent unless the test sets it
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

    def test_missing_own_score_blocks_downscale(self):
        # Destructive actions require a proven fresh own score; a missing score
        # must never authorize a downscale even when memory is cool.
        dyno = self._cool_full_dyno()
        _, client = self._patch_backend(client_attrs={"zscore": MagicMock(return_value=None)})
        self.assertFalse(dyno.allow_downscale)
        client.zscore.assert_called_once()
        client.scan.assert_not_called()

    def test_missing_own_score_blocks_shutdown_downscale(self):
        dyno = self._cool_full_dyno()
        _, client = self._patch_backend(client_attrs={"zscore": MagicMock(return_value=None)})
        self.assertFalse(dyno.allow_downscale_on_shutdown)
        client.scan.assert_not_called()

    def test_stale_own_score_blocks_formation_idle(self):
        from heroku_manager.heroku import _stale_cutoff
        dyno = self._cool_full_dyno()
        self._patch_backend(client_attrs={"zscore": MagicMock(return_value=_stale_cutoff())})
        with patch.object(type(dyno), "avg_load_1min",
                          new_callable=PropertyMock, return_value=0.4):
            self.assertFalse(dyno.is_formation_idle)

    def test_fresh_own_score_allows_shutdown_downscale_when_cool(self):
        dyno = self._cool_full_dyno()
        self._patch_backend(client_attrs={"zscore": MagicMock(return_value=time.time())})
        self.assertTrue(dyno.allow_downscale_on_shutdown)

    def test_fresh_own_score_allows_downscale(self):
        dyno = self._cool_full_dyno()
        self._patch_backend(client_attrs={"zscore": MagicMock(return_value=time.time())})
        self.assertTrue(dyno.allow_downscale)

    def test_missing_own_score_blocks_zombie_restart(self):
        # Zombie restart is destructive and requires a proven own score: without
        # one the check prunes once and never restarts, whatever the index holds.
        dyno = self._dyno("normal_worker.1")
        stale = timezone.now() - timezone.timedelta(seconds=django_settings.DYNO_ZOMBIE_THRESHOLD + 10)
        backend, client = self._patch_backend(client_attrs={
            "zscore": MagicMock(return_value=None),
            "zrangebyscore": MagicMock(return_value=["normal_worker.2"]),
            "zremrangebyscore": MagicMock(return_value=1),
        })
        # The alive read carries the stale timestamp; the readiness check's
        # degraded-marker read must stay empty for this test to reach ZSCORE.
        backend.get.side_effect = lambda key: stale if "dyno_alive" in key else None
        with patch.object(dyno, "restart_zombie_dyno") as mock_restart:
            dyno.check_for_sibling_zombie_dynos()
        mock_restart.assert_not_called()
        client.zremrangebyscore.assert_called_once()
        client.zscore.assert_called_once()
        client.mget.assert_not_called()
        client.scan.assert_not_called()

    def test_not_ready_still_prunes_stale_index_members_once(self):
        # Prune is a non-destructive cleanup: it runs once under the lock even
        # when the own score is missing, then the check returns.
        dyno = self._dyno("normal_worker.1")
        _, client = self._patch_backend(client_attrs={
            "zscore": MagicMock(return_value=None),
            "zremrangebyscore": MagicMock(return_value=3),
        })
        with patch.object(dyno, "restart_zombie_dyno") as mock_restart:
            dyno.check_for_sibling_zombie_dynos()
        client.zremrangebyscore.assert_called_once()
        mock_restart.assert_not_called()
        client.mget.assert_not_called()
        client.scan.assert_not_called()

    def test_ready_zombie_restart_uses_index_members(self):
        import pickle
        dyno = self._dyno("normal_worker.1")
        stale = timezone.now() - timezone.timedelta(seconds=django_settings.DYNO_ZOMBIE_THRESHOLD + 10)
        backend, _ = self._patch_backend(client_attrs={
            "zscore": MagicMock(return_value=time.time()),
            # Fresh index members only: the stale read returns nothing so this
            # zombie restart comes from the fresh-member evaluation.
            "zrangebyscore": MagicMock(side_effect=lambda key, mn, mx: [] if mn == "-inf" else ["normal_worker.2"]),
            "mget": MagicMock(return_value=[pickle.dumps(stale)]),
        })
        backend.client.decode = _real_decoder()
        with patch.object(dyno, "restart_zombie_dyno") as mock_restart:
            dyno.check_for_sibling_zombie_dynos()
        mock_restart.assert_called_once_with("normal_worker.2")

    def test_absent_index_member_never_authorizes_zombie_restart(self):
        dyno = self._dyno("normal_worker.1")
        # A dyno that is not in the app index is not a member: no restart.
        backend, _ = self._patch_backend(client_attrs={
            "zscore": MagicMock(return_value=time.time()),
            "zrangebyscore": MagicMock(return_value=[]),
        })
        backend.client.decode = _real_decoder()
        with patch.object(dyno, "restart_zombie_dyno") as mock_restart:
            dyno.check_for_sibling_zombie_dynos()
        mock_restart.assert_not_called()

    def test_empty_index_logs_no_misleading_restarting(self):
        # Ready, but the index lists no members: no restart may happen, so no
        # "Restarting..." log may be emitted either.
        dyno = self._dyno("normal_worker.1")
        stale = timezone.now() - timezone.timedelta(seconds=django_settings.DYNO_ZOMBIE_THRESHOLD + 10)
        backend, client = self._patch_backend(client_attrs={
            "zscore": MagicMock(return_value=time.time()),
            "zrangebyscore": MagicMock(return_value=[]),
        })
        backend.get.side_effect = lambda key: stale if "dyno_alive" in key else None
        with patch.object(logging.getLogger("heroku_manager.heroku"), "error") as mock_error:
            with patch.object(dyno, "restart_zombie_dyno") as mock_restart:
                dyno.check_for_sibling_zombie_dynos()
        mock_restart.assert_not_called()
        # Non-vacuous: the readiness predicate actually evaluated the own
        # index score, so the check had a real reason to skip the restart.
        client.zscore.assert_called_once()
        mock_error.assert_not_called()


class TestIndexedPrune(unittest.TestCase):
    """Phase C: one bounded ZREMRANGEBYSCORE under the existing zombie-check lock."""

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
        client.get.return_value = None  # degraded marker absent unless the test sets it
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
        _, client = self._patch_backend(client_attrs={
            "zscore": MagicMock(return_value=None),
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

    def test_uncertain_read_blocks_zombie_restart_without_keys(self):
        # An uncertain stale read (None) blocks the restart and the prune, and
        # nothing ever falls back to KEYS or SCAN.
        dyno = self._dyno("normal_worker.1")
        backend, client = self._patch_backend(client_attrs={
            "zscore": MagicMock(return_value=time.time()),
            "zrangebyscore": MagicMock(return_value=None),
            "zremrangebyscore": MagicMock(return_value=0),
        })
        backend.keys = MagicMock(side_effect=AssertionError("KEYS forbidden"))
        with patch.object(dyno, "restart_zombie_dyno") as mock_restart:
            dyno.check_for_sibling_zombie_dynos()
        mock_restart.assert_not_called()
        client.zremrangebyscore.assert_not_called()
        client.scan.assert_not_called()
        backend.keys.assert_not_called()

    def test_no_keys_route_when_raw_client_missing(self):
        # Missing raw client → readers return uncertain, never fall back to
        # Django cache.keys() (dynamic or lexical).  The predicate must
        # actually attempt the connection (get_client called) and fail closed.
        dyno = self._dyno("normal_worker.1")
        backend = MagicMock()
        backend.client.get_client.side_effect = AttributeError("no raw client")
        backend.make_key.side_effect = lambda key: key
        backend.get.return_value = None
        backend.keys = MagicMock(side_effect=AssertionError("KEYS forbidden"))
        with patch("heroku_manager.heroku.cache", backend):
            with patch.object(dyno, "restart_zombie_dyno") as mock_restart:
                dyno.check_for_sibling_zombie_dynos()
        backend.client.get_client.assert_called()
        mock_restart.assert_not_called()
        backend.keys.assert_not_called()


class TestIndexedPhysicalKeyContract(unittest.TestCase):
    """Phase C contract: every raw MGET key derives through cache.make_key
    physical prefix; raw bytes normalize safely; the fresh range is
    (cutoff, +inf); one readiness ZSCORE per autoscale cycle."""

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
        client.get.return_value = None  # degraded marker absent unless the test sets it
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
        _, client = self._patch_backend(client_attrs={
            "zrangebyscore": MagicMock(return_value=["normal_worker.1"]),
        })
        list(dyno._iter_sibling_values("memory"))
        args = client.zrangebyscore.call_args[0]
        self.assertEqual(args[0], "prefix:1:heroku:dynos:v1:floship")
        self.assertEqual(args[1], f'({_stale_cutoff()}')
        self.assertEqual(args[2], "+inf")

    def test_mget_uses_physical_make_key_keys(self):
        import pickle
        dyno = self._dyno("normal_worker.1")
        backend, client = self._patch_backend(client_attrs={
            "zscore": MagicMock(return_value=time.time()),
            "zrangebyscore": MagicMock(return_value=["normal_worker.1", "normal_worker.2"]),
            "mget": MagicMock(return_value=[pickle.dumps(300)]),
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
        backend, client = self._patch_backend(client_attrs={
            "zscore": MagicMock(return_value=time.time()),
            "zrangebyscore": MagicMock(return_value=[
                b"normal_worker.1", b"normal_worker.2", b"normal_worker_extra.1",
            ]),
            "mget": MagicMock(return_value=[pickle.dumps(300)]),
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
        backend, _ = self._patch_backend(client_attrs={
            "zscore": MagicMock(return_value=time.time()),
            "zrangebyscore": MagicMock(return_value=[
                "normal_worker.1", "normal_worker.2",
            ]),
            "mget": MagicMock(return_value=[raw, None]),
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
        _, client = self._patch_backend(client_attrs={
            "zscore": MagicMock(return_value=time.time()),
            "zrangebyscore": MagicMock(return_value=["normal_worker.2"]),
            "mget": MagicMock(return_value=[None]),
            "zremrangebyscore": MagicMock(return_value=1),
        })
        with patch.object(dyno, "restart_zombie_dyno") as mock_restart:
            dyno.check_for_sibling_zombie_dynos()
        # Stale member (score ≤ cutoff) is pruned once and never restarted.
        client.zremrangebyscore.assert_called_once()
        mock_restart.assert_not_called()

    def test_one_zscore_per_autoscale_cycle(self):
        # Readiness is a cached property: one ZSCORE serves the whole cycle,
        # however many sibling and zombie reads run inside it.
        dyno = self._dyno("normal_worker.1")
        _, client = self._patch_backend(client_attrs={
            "zscore": MagicMock(return_value=time.time()),
            "zrangebyscore": MagicMock(return_value=["normal_worker.1"]),
            "mget": MagicMock(return_value=[None]),
            "zremrangebyscore": MagicMock(return_value=0),
        })
        dyno.index_ready
        list(dyno._iter_sibling_values("memory"))
        list(dyno._iter_sibling_values("load"))
        with patch.object(dyno, "restart_zombie_dyno") as mock_restart:
            dyno.check_for_sibling_zombie_dynos()
        self.assertEqual(client.zscore.call_count, 1)
        client.scan.assert_not_called()
        mock_restart.assert_not_called()

    def test_index_only_reader_keeps_sibling_upscale_evidence(self):
        # Phase C: sibling reads come from the index whatever the own score
        # says, so safe upscale advocacy keeps its evidence.
        import pickle
        dyno = self._dyno("normal_worker.1", formation_size="standard-2x")
        backend, _ = self._patch_backend(client_attrs={
            "zscore": MagicMock(return_value=None),
            "zrangebyscore": MagicMock(return_value=["normal_worker.1", "normal_worker.2"]),
            "mget": MagicMock(return_value=[pickle.dumps(2183)]),
        })
        backend.client.decode = _real_decoder()
        self.assertTrue(dyno.any_sibling_requires_upscale)


class TestNoScanLeftInModule(unittest.TestCase):
    """Phase C source guard: the compatibility SCAN, its flag and its cap are gone."""

    def test_no_scan_identifiers_or_rollout_flag_in_source(self):
        import inspect
        from heroku_manager import heroku as mod
        source = inspect.getsource(mod)
        self.assertNotIn("scan_keys", source)
        self.assertNotIn("scan(", source)
        self.assertNotIn("HEROKU_DYNO_INDEX_V1_READY", source)
        self.assertNotIn("HEROKU_DYNO_INDEX_SCAN_CAP", source)
        self.assertFalse(hasattr(mod._IndexAdapter, "scan_keys"))


class TestRegistryDegradedMarker(unittest.TestCase):
    """Phase C: a failed index write closes the app's destructive gates."""

    def setUp(self):
        cache.clear()

    def tearDown(self):
        cache.clear()

    def test_failed_registry_write_publishes_the_marker(self):
        from heroku_manager.heroku import _registry_degraded_key
        # LocMem exposes no raw write client, so the write fails like a Redis
        # error and the dyno would be invisible to every reader.
        self.assertFalse(_update_dyno_registry("floship", "web.1", score=123))
        self.assertTrue(cache.get(_registry_degraded_key("floship")))

    def test_marker_marks_readiness_false_without_a_score_read(self):
        import pickle
        from heroku_manager.heroku import _registry_degraded_key
        dyno = make_dyno("normal_worker.1")
        _restore_real_index_ready(dyno)
        backend = MagicMock()
        client = MagicMock()
        client.get.return_value = None  # degraded marker absent unless the test sets it
        client.zscore.return_value = time.time()
        client.get.return_value = pickle.dumps(True)
        backend.client.get_client.return_value = client
        backend.client.decode = pickle.loads
        backend.make_key.side_effect = lambda key: key
        with patch("heroku_manager.heroku.cache", backend):
            self.assertFalse(dyno.index_ready)
        client.get.assert_called_once_with(_registry_degraded_key("floship"))
        client.zscore.assert_not_called()
        # The safety marker comes from the write authority, never a replica.
        backend.client.get_client.assert_called_once_with(write=True)

    def test_metric_reads_use_the_read_client(self):
        from heroku_manager.heroku import _IndexAdapter
        backend = MagicMock()
        client = MagicMock()
        backend.client.get_client.return_value = client
        backend.make_key.side_effect = lambda key: key
        with patch("heroku_manager.heroku.cache", backend):
            _IndexAdapter("floship").fresh_members(0)
        backend.client.get_client.assert_called_once_with(write=False)

    def test_marker_expires_after_two_autoscale_intervals(self):
        from heroku_manager.heroku import _mark_registry_degraded, _registry_degraded_key
        with patch("heroku_manager.heroku.cache") as mock_cache:
            _mark_registry_degraded("floship")
        mock_cache.set.assert_called_once_with(
            _registry_degraded_key("floship"), True, timeout=60,
        )


class TestIndexAppIsolation(unittest.TestCase):
    """Two apps sharing one raw client must use distinct physical ZSET keys.

    Legacy metric keys (``heroku:dyno_memory:{dyno}``, ...) remain unscoped
    by app, so apps still require separate Redis endpoints even though the
    v1 index keys are app-scoped.
    """

    def test_apps_never_share_index_members(self):
        from heroku_manager.heroku import _IndexAdapter
        backend = MagicMock()
        client = MagicMock()
        client.get.return_value = None  # degraded marker absent unless the test sets it
        zsets = {
            "p:heroku:dynos:v1:app-a": ["a_worker.1", "a_worker.2"],
            "p:heroku:dynos:v1:app-b": ["b_worker.1"],
        }
        client.zrangebyscore.side_effect = lambda key, lo, hi: zsets[key]
        backend.client.get_client.return_value = client
        backend.make_key.side_effect = lambda key: f"p:{key}"
        with patch("heroku_manager.heroku.cache", backend):
            members_a = _IndexAdapter("app-a").fresh_members(0)
            members_b = _IndexAdapter("app-b").fresh_members(0)
        self.assertEqual(members_a, ["a_worker.1", "a_worker.2"])
        self.assertEqual(members_b, ["b_worker.1"])
        self.assertEqual(
            client.zrangebyscore.call_args_list[0][0][0],
            "p:heroku:dynos:v1:app-a",
        )
        self.assertEqual(
            client.zrangebyscore.call_args_list[1][0][0],
            "p:heroku:dynos:v1:app-b",
        )
        self.assertNotIn("b_worker.1", members_a)
        self.assertNotIn("a_worker.1", members_b)


if __name__ == "__main__":
    unittest.main()
