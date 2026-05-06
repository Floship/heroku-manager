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
import time
import unittest
from unittest.mock import MagicMock, patch, PropertyMock
import django
from django.conf import settings as django_settings

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
from heroku_manager.heroku import HerokuDyno, DYNO_SIZES


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
    return dyno


def _isolate_dyno_type(dyno):
    isolated_type = type(f"IsolatedHerokuDyno_{id(dyno)}", (type(dyno),), {})
    dyno.__class__ = isolated_type
    return dyno


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
        return make_dyno(dyno_name=dyno_name, formation_size=formation_size)

    def _set_sibling_memory(self, dyno_name, memory_mb):
        key = f"heroku:dyno_memory:{dyno_name}"
        cache.set(key, memory_mb, timeout=60)
        self._memory_store[key] = memory_mb

    def _patch_keys(self, pattern_to_keys=None):
        """Patch cache.keys() (Redis-only in production) onto the locmem backend."""
        def _keys(pattern):
            prefix = pattern.replace(".*", ".")
            return [k for k in self._memory_store if k.startswith(prefix)]
        return patch.object(cache, "keys", side_effect=_keys, create=True)

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
        """Patch cache.keys() (Redis-only in production) onto the locmem backend."""
        def _keys(pattern):
            prefix = pattern.replace(".*", ".")
            return [k for k in self._memory_store if k.startswith(prefix)]
        return patch.object(cache, "keys", side_effect=_keys, create=True)

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
    """Patch cache.keys() to return from an in-memory dict (locmem has no keys())."""
    def _keys(pattern):
        # Treat trailing dot as literal prefix (not glob wildcard after dot)
        prefix = pattern.replace(".*", ".") if ".*" in pattern else pattern
        return [k for k in memory_store if k.startswith(prefix)]
    return patch.object(cache, "keys", side_effect=_keys, create=True)


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

        def _keys(pattern):
            prefix = pattern.replace(".*", ".") if ".*" in pattern else pattern
            return [k for k in all_keys if k.startswith(prefix)]

        with patch.object(cache, "keys", side_effect=_keys, create=True):
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


if __name__ == "__main__":
    unittest.main()
