"""
TDD Red Phase: Phantom upscale_until key and instant-downscale race.

Production pattern observed via BetterStack logs on 2026-04-28:
  1. Memory spikes → sibling upscales formation to standard-2x
  2. ~2s later a DIFFERENT sibling with stale near-expired upscale_until
     key from a previous phantom cycle immediately downscales back to 1X
  3. Downscale clears original_formation_size; restarting siblings see
     no original → _downscale_memory_threshold returns inf → allow_downscale=False
  4. Bottom guard creates phantom upscale_until key even though formation
     is already on its original size
  5. All siblings loop: "Extending the time..." forever, never staying on 2X

Two bugs reproduced here:
  A. Phantom upscale_until key created when formation is already at original size
  B. The near-expiry extend path keeps extending even when on original size

Extended edge cases cover:
  C. Phantom clear falls through to R14 restart path
  D. High-TTL phantom keys (not near expiry) still detected and cleared
  E. Phantom on non-base tiers (standard-2x original=standard-2x)
  F. Multi-level upscale preserves timer (performance-m original=standard-1x)
  G. Post-phantom-clear → startup baseline recording → clean next cycle
  H. Bottom guard with allow_downscale=True proceeds to downscale
  I. Phantom clear + immediate R15 in same full autoscale cycle
  J. Multiple siblings phantom-clearing in sequence
  K. Bottom guard on genuinely upscaled formation with hot memory
"""

import os
import pickle
import time
import unittest
from contextlib import ExitStack
from datetime import timedelta
from unittest.mock import patch, MagicMock, PropertyMock, call

from django.core.cache import cache
from django.utils import timezone
from redis.exceptions import ResponseError

from tests.conftest import make_dyno, BaseLockTestCase, patch_index_backend
from heroku_manager.heroku import _IndexAdapter


def _mock_response(status=200):
    resp = MagicMock()
    resp.status_code = status
    resp.text = "ok"
    return resp


def _apply_dyno_patches(dyno, overrides, fn):
    """Helper to reduce nesting.  overrides is a dict of property_name→value."""
    defaults = {
        "current_memory_usage": 302.0,
        "current_memory_usage_percentage": 59.0,
        "detected_r14": False,
        "detected_r15": False,
        "no_tasks_in_queue": False,
        "tasks_in_queue": 51000,
        "avg_load_1min": 0.5,
    }
    defaults.update(overrides)

    patches = []
    for attr, val in defaults.items():
        patches.append(
            patch.object(type(dyno), attr, new_callable=PropertyMock, return_value=val)
        )

    cache.set(f"heroku:dyno_memory:{dyno.dyno_name}", defaults["current_memory_usage"], timeout=60)
    cache.set(f"heroku:dyno_load:{dyno.dyno_name}", defaults["avg_load_1min"], timeout=60)

    result = None
    ctx_managers = [p.__enter__() for p in patches]
    try:
        with patch_index_backend({}):
            result = fn()
    finally:
        for p in reversed(patches):
            p.__exit__(None, None, None)
    return result


# ─── Bug A: Phantom upscale_until key when on original formation ────────────

class TestPhantomUpscaleUntilOnOriginalFormation(BaseLockTestCase):
    """When the formation is already at its original size (standard-1x),
    the bottom guard in check_and_downscale_to_original_formation_size()
    should NOT create a phantom upscale_until key even if allow_downscale
    is False (e.g. because original_formation_size was just cleared)."""

    def test_no_phantom_key_when_on_original_size_no_original_recorded(self):
        """Formation on standard-1x, no original_formation_size set,
        no upscale_until key — should NOT create phantom upscale_until."""
        dyno = make_dyno(formation_size="standard-1x")

        _apply_dyno_patches(dyno, {}, dyno.check_and_downscale_to_original_formation_size)

        self.assertIsNone(cache.get(dyno.upscale_until_cache_key),
            "Must NOT create phantom upscale_until key when formation "
            "is already at standard-1x with no original recorded")

    def test_no_phantom_key_when_original_matches_current(self):
        """Formation on standard-1x, original_formation_size = standard-1x,
        no upscale_until key — should NOT create phantom upscale_until."""
        dyno = make_dyno(formation_size="standard-1x")
        cache.set(dyno.original_size_cache_key,
                  {"size": "standard-1x", "time": timezone.now()}, timeout=None)

        _apply_dyno_patches(dyno, {"tasks_in_queue": 74000},
                            dyno.check_and_downscale_to_original_formation_size)

        self.assertIsNone(cache.get(dyno.upscale_until_cache_key),
            "Must NOT create phantom upscale_until when already on original size")

    def test_no_phantom_on_standard_2x_with_original_2x(self):
        """Phantom on a non-base tier: formation on standard-2x,
        original_formation_size = standard-2x, no upscale_until key."""
        dyno = make_dyno(formation_size="standard-2x")
        cache.set(dyno.original_size_cache_key,
                  {"size": "standard-2x", "time": timezone.now()}, timeout=None)

        with patch_index_backend({}):
            _apply_dyno_patches(dyno, {"current_memory_usage": 500.0,
                                       "current_memory_usage_percentage": 48.8},
                                dyno.check_and_downscale_to_original_formation_size)

        self.assertIsNone(cache.get(dyno.upscale_until_cache_key),
            "No phantom when standard-2x matches original standard-2x")

    def test_no_phantom_when_below_original(self):
        """Formation on standard-1x, original = standard-2x (formation was
        scaled down past its original in some edge case). is_on_original_or_lower
        should be True → no phantom."""
        dyno = make_dyno(formation_size="standard-1x")
        cache.set(dyno.original_size_cache_key,
                  {"size": "standard-2x", "time": timezone.now()}, timeout=None)

        _apply_dyno_patches(dyno, {}, dyno.check_and_downscale_to_original_formation_size)

        self.assertIsNone(cache.get(dyno.upscale_until_cache_key),
            "No phantom when formation is below original size")


# ─── Bug B: Phantom extend loop when upscale_until exists on original size ──

class TestPhantomExtendLoopOnOriginalFormation(BaseLockTestCase):
    """When the formation is at its original size but a phantom upscale_until
    key exists, the code should clear the phantom state rather than extend it."""

    def test_clears_phantom_upscale_until_when_back_on_original(self):
        """Formation on standard-1x, original = standard-1x, phantom
        upscale_until exists → should clear the phantom key."""
        dyno = make_dyno(formation_size="standard-1x")
        cache.set(dyno.original_size_cache_key,
                  {"size": "standard-1x", "time": timezone.now()}, timeout=None)
        until = timezone.now() + timedelta(seconds=100)
        cache.set(dyno.upscale_until_cache_key, until, timeout=300)

        with patch.object(cache, "ttl", return_value=50):
            _apply_dyno_patches(dyno, {}, dyno.check_and_downscale_to_original_formation_size)

        self.assertIsNone(cache.get(dyno.upscale_until_cache_key),
            "Phantom upscale_until must be cleared when formation is on original size")

    def test_clears_phantom_original_formation_size(self):
        """After clearing phantom, second call must not recreate it."""
        dyno = make_dyno(formation_size="standard-1x")
        cache.set(dyno.original_size_cache_key,
                  {"size": "standard-1x", "time": timezone.now()}, timeout=None)
        cache.set(dyno.upscale_until_cache_key,
                  timezone.now() + timedelta(seconds=100), timeout=300)

        with patch.object(cache, "ttl", return_value=50):
            _apply_dyno_patches(dyno, {"tasks_in_queue": 60000},
                                dyno.check_and_downscale_to_original_formation_size)

        # Second call — should not recreate phantom
        _apply_dyno_patches(dyno, {"tasks_in_queue": 60000},
                            dyno.check_and_downscale_to_original_formation_size)

        self.assertIsNone(cache.get(dyno.upscale_until_cache_key),
            "Second call must also NOT create phantom key")

    def test_clears_high_ttl_phantom(self):
        """Phantom key with high TTL (not near-expiry) is still cleared
        on original formation — the is_on_original check runs before TTL check."""
        dyno = make_dyno(formation_size="standard-1x")
        cache.set(dyno.original_size_cache_key,
                  {"size": "standard-1x", "time": timezone.now()}, timeout=None)
        # Phantom with TTL=600 — far from near-expiry
        cache.set(dyno.upscale_until_cache_key,
                  timezone.now() + timedelta(seconds=600), timeout=600)

        # TTL is high (600) — doesn't matter, should still clear
        with patch.object(cache, "ttl", return_value=600):
            _apply_dyno_patches(dyno, {}, dyno.check_and_downscale_to_original_formation_size)

        self.assertIsNone(cache.get(dyno.upscale_until_cache_key),
            "High-TTL phantom key must still be cleared on original formation")

    def test_clears_phantom_on_non_base_tier(self):
        """Phantom on standard-2x with original=standard-2x should be cleared."""
        dyno = make_dyno(formation_size="standard-2x")
        cache.set(dyno.original_size_cache_key,
                  {"size": "standard-2x", "time": timezone.now()}, timeout=None)
        cache.set(dyno.upscale_until_cache_key,
                  timezone.now() + timedelta(seconds=200), timeout=200)

        with patch_index_backend({}):
            with patch.object(cache, "ttl", return_value=50):
                _apply_dyno_patches(dyno, {"current_memory_usage": 400.0,
                                           "current_memory_usage_percentage": 39.1},
                                    dyno.check_and_downscale_to_original_formation_size)

        self.assertIsNone(cache.get(dyno.upscale_until_cache_key),
            "Phantom on standard-2x==original standard-2x must be cleared")

    def test_clears_original_size_cache_key_on_phantom_clear(self):
        """clear_original_formation_size() should delete both Redis keys."""
        dyno = make_dyno(formation_size="standard-1x")
        cache.set(dyno.original_size_cache_key,
                  {"size": "standard-1x", "time": timezone.now()}, timeout=None)
        cache.set(dyno.upscale_until_cache_key,
                  timezone.now() + timedelta(seconds=100), timeout=300)

        with patch.object(cache, "ttl", return_value=50):
            _apply_dyno_patches(dyno, {}, dyno.check_and_downscale_to_original_formation_size)

        self.assertIsNone(cache.get(dyno.original_size_cache_key),
            "original_size_cache_key must be deleted when phantom is cleared")
        self.assertIsNone(cache.get(dyno.upscale_until_cache_key),
            "upscale_until must be deleted when phantom is cleared")


# ─── C: Phantom clear falls through to R14 restart ─────────────────────────

class TestPhantomClearFallsToR14Restart(BaseLockTestCase):
    """After phantom state is cleared, the method should fall through to the
    R14 restart check. If R14 + high memory + empty queue → restart."""

    def test_phantom_clear_then_r14_restart(self):
        """Phantom cleared → falls through → R14 at 106% + empty queue → restart."""
        dyno = make_dyno(formation_size="standard-1x")
        cache.set(dyno.original_size_cache_key,
                  {"size": "standard-1x", "time": timezone.now()}, timeout=None)
        cache.set(dyno.upscale_until_cache_key,
                  timezone.now() + timedelta(seconds=100), timeout=300)

        with patch.object(cache, "ttl", return_value=50):
            with patch.object(dyno, "restart_dyno") as mock_restart:
                _apply_dyno_patches(dyno, {
                    "current_memory_usage": 545.0,
                    "current_memory_usage_percentage": 106.4,
                    "detected_r14": True,
                    "no_tasks_in_queue": True,
                    "tasks_in_queue": 0,
                }, dyno.check_and_downscale_to_original_formation_size)

        mock_restart.assert_called_once()

    def test_phantom_clear_then_r14_with_queue_no_restart(self):
        """Phantom cleared → R14 at 106% but queue NOT empty → no restart."""
        dyno = make_dyno(formation_size="standard-1x")
        cache.set(dyno.original_size_cache_key,
                  {"size": "standard-1x", "time": timezone.now()}, timeout=None)
        cache.set(dyno.upscale_until_cache_key,
                  timezone.now() + timedelta(seconds=100), timeout=300)

        with patch.object(cache, "ttl", return_value=50):
            with patch.object(dyno, "restart_dyno") as mock_restart:
                _apply_dyno_patches(dyno, {
                    "current_memory_usage": 545.0,
                    "current_memory_usage_percentage": 106.4,
                    "detected_r14": True,
                    "no_tasks_in_queue": False,
                    "tasks_in_queue": 1000,
                }, dyno.check_and_downscale_to_original_formation_size)

        mock_restart.assert_not_called()

    def test_phantom_clear_then_r15_does_not_restart(self):
        """Phantom cleared → R15 active → the R14-only restart check should
        NOT fire (R15 is for upscale, not restart)."""
        dyno = make_dyno(formation_size="standard-1x")
        cache.set(dyno.original_size_cache_key,
                  {"size": "standard-1x", "time": timezone.now()}, timeout=None)
        cache.set(dyno.upscale_until_cache_key,
                  timezone.now() + timedelta(seconds=100), timeout=300)

        with patch.object(cache, "ttl", return_value=50):
            with patch.object(dyno, "restart_dyno") as mock_restart:
                _apply_dyno_patches(dyno, {
                    "current_memory_usage": 950.0,
                    "current_memory_usage_percentage": 185.0,
                    "detected_r14": True,
                    "detected_r15": True,
                    "no_tasks_in_queue": True,
                    "tasks_in_queue": 0,
                }, dyno.check_and_downscale_to_original_formation_size)

        mock_restart.assert_not_called()

    def test_phantom_clear_then_low_memory_no_restart(self):
        """Phantom cleared → memory at 59% with R14 → no restart
        (below DOWNSCALE_PERCENTAGE_HIGH_MEM_USE=105)."""
        dyno = make_dyno(formation_size="standard-1x")
        cache.set(dyno.original_size_cache_key,
                  {"size": "standard-1x", "time": timezone.now()}, timeout=None)
        cache.set(dyno.upscale_until_cache_key,
                  timezone.now() + timedelta(seconds=100), timeout=300)

        with patch.object(cache, "ttl", return_value=50):
            with patch.object(dyno, "restart_dyno") as mock_restart:
                _apply_dyno_patches(dyno, {
                    "current_memory_usage": 302.0,
                    "current_memory_usage_percentage": 59.0,
                    "detected_r14": True,
                    "no_tasks_in_queue": True,
                    "tasks_in_queue": 0,
                }, dyno.check_and_downscale_to_original_formation_size)

        mock_restart.assert_not_called()


# ─── D: Legitimate upscale state preserved when actually upscaled ───────────

class TestLegitimateUpscaleNotCleared(BaseLockTestCase):
    """When the formation IS genuinely upscaled (e.g. on standard-2x with
    original=standard-1x), the upscale_until key must NOT be cleared."""

    def test_upscale_timer_preserved_when_actually_upscaled(self):
        """On standard-2x, original=standard-1x, tasks in queue →
        should extend timer, NOT clear it."""
        dyno = make_dyno(formation_size="standard-2x")
        cache.set(dyno.original_size_cache_key,
                  {"size": "standard-1x", "time": timezone.now()}, timeout=None)
        until = timezone.now() + timedelta(seconds=100)
        cache.set(dyno.upscale_until_cache_key, until, timeout=300)
        cache.set(
            f"heroku:dyno_memory_stable:{dyno.dyno_name}", False, timeout=60,
        )

        with patch.object(cache, "ttl", return_value=50), \
                patch.object(
                    type(dyno),
                    "downscale_on_non_empty_queue",
                    new_callable=PropertyMock,
                    return_value=False,
                ), \
                patch.object(dyno, "call_heroku_api") as api:
            _apply_dyno_patches(dyno, {
                "current_memory_usage": 350.0,
                "current_memory_usage_percentage": 34.2,
                "no_tasks_in_queue": False,
                "tasks_in_queue": 1,
            }, dyno.check_and_downscale_to_original_formation_size)

        extended = cache.get(dyno.upscale_until_cache_key)
        api.assert_not_called()
        self.assertIsNone(cache.get(dyno.downscale_cache_key))
        self.assertIsNotNone(extended,
            "Legitimate upscale timer must be extended, not cleared")
        self.assertGreater(extended, until)

    def test_multi_level_upscale_timer_preserved(self):
        """On performance-m (2560MB), original=standard-1x (512MB) —
        genuinely 2 levels up. Timer must be preserved."""
        dyno = make_dyno(formation_size="performance-m")
        cache.set(dyno.original_size_cache_key,
                  {"size": "standard-1x", "time": timezone.now()}, timeout=None)
        until = timezone.now() + timedelta(seconds=200)
        cache.set(dyno.upscale_until_cache_key, until, timeout=300)
        cache.set(
            f"heroku:dyno_memory_stable:{dyno.dyno_name}", False, timeout=60,
        )

        with patch.object(cache, "ttl", return_value=50), \
                patch.object(
                    type(dyno),
                    "downscale_on_non_empty_queue",
                    new_callable=PropertyMock,
                    return_value=False,
                ), \
                patch.object(dyno, "call_heroku_api") as api:
            _apply_dyno_patches(dyno, {
                "current_memory_usage": 900.0,
                "current_memory_usage_percentage": 35.2,
                "no_tasks_in_queue": False,
                "tasks_in_queue": 1,
            }, dyno.check_and_downscale_to_original_formation_size)

        extended = cache.get(dyno.upscale_until_cache_key)
        api.assert_not_called()
        self.assertIsNone(cache.get(dyno.downscale_cache_key))
        self.assertIsNotNone(extended,
            "Multi-level upscale timer must be preserved")
        self.assertGreater(extended, until)

    def test_upscale_timer_not_near_expiry_returns_early(self):
        """TTL still high (500) — should return early without extending or clearing."""
        dyno = make_dyno(formation_size="standard-2x")
        cache.set(dyno.original_size_cache_key,
                  {"size": "standard-1x", "time": timezone.now()}, timeout=None)
        original_until = timezone.now() + timedelta(seconds=500)
        cache.set(dyno.upscale_until_cache_key, original_until, timeout=500)

        with patch.object(cache, "ttl", return_value=500):
            _apply_dyno_patches(dyno, {}, dyno.check_and_downscale_to_original_formation_size)

        # Key still intact, value unchanged
        current = cache.get(dyno.upscale_until_cache_key)
        self.assertEqual(current, original_until,
            "High-TTL legitimate upscale key should not be touched")


# ─── E: Near-expiry legitimate upscale with allow_downscale=True ────────────

class TestNearExpiryLegitimateDownscale(BaseLockTestCase):
    """When genuinely upscaled, near expiry, and allow_downscale is True,
    should call downscale_formation_to_original_size."""

    def test_near_expiry_allow_downscale_calls_downscale(self):
        dyno = make_dyno(formation_size="standard-2x")
        cache.set(dyno.original_size_cache_key,
                  {"size": "standard-1x", "time": timezone.now()}, timeout=None)
        cache.set(dyno.upscale_until_cache_key,
                  timezone.now() + timedelta(seconds=50), timeout=50)

        with patch_index_backend({}):
            with patch.object(cache, "ttl", return_value=50):
                with patch.object(dyno, "downscale_formation_to_original_size") as mock_ds:
                    _apply_dyno_patches(dyno, {
                        "current_memory_usage": 300.0,
                        "current_memory_usage_percentage": 29.3,
                    }, dyno.check_and_downscale_to_original_formation_size)

        mock_ds.assert_called_once()

    def test_missing_sibling_load_blocks_near_expiry_plateau_downscale(self):
        dyno = make_dyno(formation_size="standard-2x")
        cache.set(dyno.original_size_cache_key, {"size": "standard-1x", "time": timezone.now()}, timeout=None)
        cache.set(dyno.upscale_until_cache_key, timezone.now() + timedelta(seconds=50), timeout=50)
        store = {"heroku:dyno_memory:normal_worker.2": 300}
        overrides = {
            "current_memory_usage": 600.0,
            "current_memory_usage_percentage": 58.6,
            "is_memory_stabilized": True,
            "avg_load_1min": 0.2,
            "no_tasks_in_queue": False,
            "tasks_in_queue": 1,
        }
        with (
            patch_index_backend(store),
            patch.object(cache, "ttl", return_value=50),
            patch.object(dyno, "downscale_formation_to_original_size") as downscale,
            patch.object(dyno, "restart_dyno") as restart,
        ):
            _apply_dyno_patches(dyno, overrides, dyno.check_and_downscale_to_original_formation_size)

        downscale.assert_not_called()
        restart.assert_not_called()

    def test_near_expiry_r14_hot_empty_queue_restarts(self):
        """Upscaled, near expiry, R14 active + hot memory + empty queue → restart."""
        dyno = make_dyno(formation_size="standard-2x")
        cache.set(dyno.original_size_cache_key,
                  {"size": "standard-1x", "time": timezone.now()}, timeout=None)
        cache.set(dyno.upscale_until_cache_key,
                  timezone.now() + timedelta(seconds=50), timeout=50)

        with patch_index_backend({}):
            with patch.object(cache, "ttl", return_value=50):
                with patch.object(dyno, "restart_dyno") as mock_restart:
                    _apply_dyno_patches(dyno, {
                        "current_memory_usage": 600.0,
                        "current_memory_usage_percentage": 58.6,
                        "detected_r14": True,
                        "no_tasks_in_queue": True,
                        "tasks_in_queue": 0,
                    }, dyno.check_and_downscale_to_original_formation_size)

        mock_restart.assert_called_once()


# ─── F: Bottom guard for genuinely upscaled with expired key ────────────────

class TestBottomGuardGenuinelyUpscaled(BaseLockTestCase):
    """The bottom guard restores upscale_until when the formation is genuinely
    above its original size and allow_downscale is False."""

    def test_bottom_guard_restores_timer_when_upscaled_above_original(self):
        """Standard-2x, original=standard-1x, no upscale_until, high memory →
        should restore upscale_until."""
        dyno = make_dyno(formation_size="standard-2x")
        cache.set(dyno.original_size_cache_key,
                  {"size": "standard-1x", "time": timezone.now()}, timeout=None)
        # No upscale_until key (expired)

        with patch_index_backend({}):
            _apply_dyno_patches(dyno, {
                "current_memory_usage": 600.0,
                "current_memory_usage_percentage": 58.6,
            }, dyno.check_and_downscale_to_original_formation_size)

        restored = cache.get(dyno.upscale_until_cache_key)
        self.assertIsNotNone(restored,
            "Bottom guard should restore upscale_until when genuinely upscaled")

    def test_bottom_guard_allow_downscale_true_calls_downscale(self):
        """Standard-2x, original=standard-1x, no upscale_until,
        allow_downscale=True → should downscale."""
        dyno = make_dyno(formation_size="standard-2x")
        cache.set(dyno.original_size_cache_key,
                  {"size": "standard-1x", "time": timezone.now()}, timeout=None)

        with patch_index_backend({}):
            with patch.object(dyno, "downscale_formation_to_original_size") as mock_ds:
                _apply_dyno_patches(dyno, {
                    "current_memory_usage": 300.0,
                    "current_memory_usage_percentage": 29.3,
                }, dyno.check_and_downscale_to_original_formation_size)

        mock_ds.assert_called_once()

    def test_bottom_guard_performance_m_hot_restores_timer(self):
        """Performance-M (2560MB), original=standard-1x, hot memory →
        should restore timer, not create phantom."""
        dyno = make_dyno(formation_size="performance-m")
        cache.set(dyno.original_size_cache_key,
                  {"size": "standard-1x", "time": timezone.now()}, timeout=None)

        with patch_index_backend({}):
            _apply_dyno_patches(dyno, {
                "current_memory_usage": 1000.0,
                "current_memory_usage_percentage": 39.1,
            }, dyno.check_and_downscale_to_original_formation_size)

        restored = cache.get(dyno.upscale_until_cache_key)
        self.assertIsNotNone(restored,
            "Bottom guard should restore timer for genuinely upscaled performance-m")


# ─── G: Post-phantom interaction with _check_formation_on_startup ───────────

class TestPhantomAndStartupInteraction(BaseLockTestCase):
    """After phantom clear deletes all keys, _check_formation_on_startup
    should record baseline → next autoscale cycle behaves normally."""

    def test_startup_records_baseline_after_phantom_clear(self):
        """Phantom cleared (keys deleted) → startup → records baseline."""
        dyno = make_dyno(formation_size="standard-1x")
        # Simulate post-phantom-clear: no keys
        cache.delete(dyno.original_size_cache_key)
        cache.delete(dyno.upscale_until_cache_key)

        dyno._check_formation_on_startup()

        original = cache.get(dyno.original_size_cache_key)
        self.assertIsNotNone(original, "Startup should record baseline")
        self.assertEqual(original["size"], "standard-1x")

    def test_cycle_after_startup_baseline_no_phantom(self):
        """After startup records baseline, next autoscale cycle on original
        size should NOT create phantom keys."""
        dyno = make_dyno(formation_size="standard-1x")
        cache.delete(dyno.original_size_cache_key)
        cache.delete(dyno.upscale_until_cache_key)

        # Startup records baseline
        dyno._check_formation_on_startup()

        # Now run autoscale downscale check — should be clean
        _apply_dyno_patches(dyno, {}, dyno.check_and_downscale_to_original_formation_size)

        self.assertIsNone(cache.get(dyno.upscale_until_cache_key),
            "No phantom after startup baseline + autoscale on original size")

    def test_startup_with_phantom_key_present_at_original(self):
        """If startup runs while a phantom key already exists (and we're at
        original size), startup doesn't interact with phantom key. The next
        autoscale cycle should clear it."""
        dyno = make_dyno(formation_size="standard-1x")
        cache.set(dyno.original_size_cache_key,
                  {"size": "standard-1x", "time": timezone.now()}, timeout=None)
        # Phantom key present
        cache.set(dyno.upscale_until_cache_key,
                  timezone.now() + timedelta(seconds=300), timeout=300)

        # Startup: current==original, upscale_until exists —
        # startup won't touch it (it only acts when current > original AND no key)
        dyno._check_formation_on_startup()

        # Phantom key should still be there (startup doesn't clear it)
        self.assertIsNotNone(cache.get(dyno.upscale_until_cache_key))

        # But next autoscale cycle should clear it
        with patch.object(cache, "ttl", return_value=50):
            _apply_dyno_patches(dyno, {}, dyno.check_and_downscale_to_original_formation_size)

        self.assertIsNone(cache.get(dyno.upscale_until_cache_key),
            "Autoscale cycle after startup must clear phantom")


# ─── H: Full autoscale() integration — phantom clear + immediate R15 ────────

class TestPhantomClearThenR15Upscale(BaseLockTestCase):
    """After phantom clear in check_and_downscale, if the NEXT autoscale()
    call sees R15, it should upscale via requires_upscale → upscale_formation."""

    def test_phantom_clear_then_autoscale_with_r15(self):
        """Full autoscale() cycle: phantom cleared → requires_upscale=True (R15)
        → upscale_formation_to_next_level called."""
        dyno = make_dyno(formation_size="standard-1x")
        cache.set(dyno.original_size_cache_key,
                  {"size": "standard-1x", "time": timezone.now()}, timeout=None)
        cache.set(dyno.upscale_until_cache_key,
                  timezone.now() + timedelta(seconds=100), timeout=300)

        # First: check_and_downscale clears phantom (no R15 yet)
        with patch.object(cache, "ttl", return_value=50):
            _apply_dyno_patches(dyno, {}, dyno.check_and_downscale_to_original_formation_size)

        self.assertIsNone(cache.get(dyno.upscale_until_cache_key))

        # Now: full autoscale() with R15 → should go to upscale path
        with patch.object(dyno, "set_threads_used"):
            with patch.object(dyno, "upscale_formation_to_next_level") as mock_upscale:
                _apply_dyno_patches(dyno, {
                    "detected_r15": True,
                    "current_memory_usage": 950.0,
                    "current_memory_usage_percentage": 185.0,
                }, lambda: dyno.autoscale(continuous=False))

        mock_upscale.assert_called_once()


class TestPhantomDoesNotBlockLegitimateUpscale(BaseLockTestCase):
    """After phantom cleanup, a legitimate R15 should still trigger upscale."""

    def test_upscale_works_after_phantom_cleanup(self):
        """Phantom cleared → next cycle with R15 → should upscale normally."""
        dyno = make_dyno(formation_size="standard-1x")
        cache.set(dyno.original_size_cache_key,
                  {"size": "standard-1x", "time": timezone.now()}, timeout=None)
        cache.set(dyno.upscale_until_cache_key,
                  timezone.now() + timedelta(seconds=100), timeout=300)

        with patch.object(cache, "ttl", return_value=50):
            _apply_dyno_patches(dyno, {}, dyno.check_and_downscale_to_original_formation_size)

        self.assertIsNone(cache.get(dyno.upscale_until_cache_key))

        with patch.object(type(dyno), "detected_r15",
                          new_callable=PropertyMock, return_value=True):
            with patch.object(type(dyno), "current_memory_usage",
                              new_callable=PropertyMock, return_value=950.0):
                with patch.object(type(dyno), "current_memory_usage_percentage",
                                  new_callable=PropertyMock, return_value=185.0):
                    self.assertTrue(dyno.requires_upscale,
                        "R15 after phantom cleanup must trigger requires_upscale")


# ─── I: Multiple siblings clearing phantom in sequence ──────────────────────

class TestMultipleSiblingsPhantomClear(BaseLockTestCase):
    """Multiple siblings all running autoscale at roughly the same time
    on original formation size — none should create phantom keys."""

    def test_three_siblings_clear_phantom_no_new_phantom(self):
        """Siblings .1, .3, .6 all on standard-1x with phantom key.
        Each should clear it (or find it already cleared)."""
        for dyno_num in [1, 3, 6]:
            # Fresh dyno instance for each sibling
            sib = make_dyno(dyno_name=f"normal_worker.{dyno_num}",
                            formation_size="standard-1x")
            # They all share the same formation_name → same cache keys
            cache.set(sib.original_size_cache_key,
                      {"size": "standard-1x", "time": timezone.now()}, timeout=None)
            cache.set(sib.upscale_until_cache_key,
                      timezone.now() + timedelta(seconds=200), timeout=200)

            with patch.object(cache, "ttl", return_value=50):
                _apply_dyno_patches(sib, {}, sib.check_and_downscale_to_original_formation_size)

        # All phantom keys should be cleared
        # (using the last sibling's key names, which are the same for all)
        self.assertIsNone(cache.get(sib.upscale_until_cache_key),
            "All siblings on original size must clear phantom")
        self.assertIsNone(cache.get(sib.original_size_cache_key),
            "All siblings must clean up original_size key")

    def test_sibling_after_clear_with_no_phantom_stays_clean(self):
        """After first sibling clears phantom, subsequent siblings find
        no upscale_until key → should not create new phantom."""
        sib1 = make_dyno(dyno_name="normal_worker.1", formation_size="standard-1x")
        cache.set(sib1.original_size_cache_key,
                  {"size": "standard-1x", "time": timezone.now()}, timeout=None)
        cache.set(sib1.upscale_until_cache_key,
                  timezone.now() + timedelta(seconds=200), timeout=200)

        # Sibling 1 clears phantom
        with patch.object(cache, "ttl", return_value=50):
            _apply_dyno_patches(sib1, {}, sib1.check_and_downscale_to_original_formation_size)

        # Sibling 3 — phantom already cleared, keys gone
        sib3 = make_dyno(dyno_name="normal_worker.3", formation_size="standard-1x")
        _apply_dyno_patches(sib3, {}, sib3.check_and_downscale_to_original_formation_size)

        # Should still be clean
        self.assertIsNone(cache.get(sib3.upscale_until_cache_key),
            "Sibling after phantom clear must stay clean")


# ─── J: Full production scenario simulation ─────────────────────────────────

class TestProductionPhantomCycle(BaseLockTestCase):
    """Simulate the full production failure loop observed on 2026-04-28:
    upscale → instant downscale → phantom loop → R14/R15 ignored."""

    def test_full_cycle_phantom_does_not_block_next_upscale(self):
        """After a downscale clears original_formation_size and upscale_until,
        the next autoscale cycle on a sibling at standard-1x should NOT
        create a phantom upscale_until key that blocks future upscaling."""
        sibling = make_dyno(dyno_name="normal_worker.6", formation_size="standard-1x")
        cache.delete(sibling.original_size_cache_key)
        cache.delete(sibling.upscale_until_cache_key)

        _apply_dyno_patches(sibling, {}, sibling.check_and_downscale_to_original_formation_size)

        self.assertIsNone(cache.get(sibling.upscale_until_cache_key),
            "Sibling on standard-1x after downscale must NOT create phantom key. "
            "This causes the production infinite-extend loop.")

    def test_full_production_sequence_upscale_downscale_phantom_cycle(self):
        """Simulate: upscale → instant sibling downscale → clear → phantom
        check → should NOT create phantom → R15 → should upscale."""
        # Step 1: Sibling .2 upscales (would call API, we skip that)
        upscaler = make_dyno(dyno_name="normal_worker.2", formation_size="standard-1x")
        # Simulate post-upscale state: formation is now 2x, keys set
        upscaler._formation_size_cached = ("standard-2x", __import__("time").time())
        cache.set(upscaler.original_size_cache_key,
                  {"size": "standard-1x", "time": timezone.now()}, timeout=None)
        until = timezone.now() + timedelta(seconds=630)
        cache.set(upscaler.upscale_until_cache_key, until, timeout=630)

        # Step 2: Sibling .6 immediately downscales (race condition)
        # This clears both keys
        cache.delete(upscaler.original_size_cache_key)
        cache.delete(upscaler.upscale_until_cache_key)
        # Formation goes back to 1x
        racer = make_dyno(dyno_name="normal_worker.6", formation_size="standard-1x")

        # Step 3: Sibling .3 runs its autoscale cycle — should NOT create phantom
        checker = make_dyno(dyno_name="normal_worker.3", formation_size="standard-1x")
        _apply_dyno_patches(checker, {
            "current_memory_usage": 260.0,
            "current_memory_usage_percentage": 50.8,
        }, checker.check_and_downscale_to_original_formation_size)

        self.assertIsNone(cache.get(checker.upscale_until_cache_key),
            "Post-race sibling must not create phantom")

        # Step 4: Memory spikes again with R15 → requires_upscale should work
        with patch.object(type(checker), "detected_r15",
                          new_callable=PropertyMock, return_value=True):
            with patch.object(type(checker), "current_memory_usage_percentage",
                              new_callable=PropertyMock, return_value=186.0):
                self.assertTrue(checker.requires_upscale,
                    "requires_upscale must work after the full race cycle")

    def test_repeated_phantom_cycles_converge(self):
        """Run 5 consecutive check_and_downscale calls on a sibling at
        original size with no keys — none should create phantom keys.
        This verifies the fix is stable across multiple cycles."""
        dyno = make_dyno(formation_size="standard-1x")

        for i in range(5):
            _apply_dyno_patches(dyno, {
                "current_memory_usage": 300.0 + i * 10,
                "current_memory_usage_percentage": 58.6 + i * 2,
            }, dyno.check_and_downscale_to_original_formation_size)

            self.assertIsNone(cache.get(dyno.upscale_until_cache_key),
                f"Cycle {i+1}: must not create phantom key")


# ─── K: Edge cases for is_on_original_formation_size_or_lower ───────────────

class TestIsOnOriginalFormationSizeOrLower(BaseLockTestCase):
    """Targeted tests for the gating property used by the phantom detection."""

    def test_returns_false_when_no_original(self):
        """No original_formation_size → returns False (can't determine)."""
        dyno = make_dyno(formation_size="standard-1x")
        self.assertFalse(dyno.is_on_original_formation_size_or_lower)

    def test_returns_true_when_at_original(self):
        dyno = make_dyno(formation_size="standard-1x")
        cache.set(dyno.original_size_cache_key,
                  {"size": "standard-1x", "time": timezone.now()}, timeout=None)
        self.assertTrue(dyno.is_on_original_formation_size_or_lower)

    def test_returns_true_when_below_original(self):
        dyno = make_dyno(formation_size="standard-1x")
        cache.set(dyno.original_size_cache_key,
                  {"size": "standard-2x", "time": timezone.now()}, timeout=None)
        self.assertTrue(dyno.is_on_original_formation_size_or_lower)

    def test_returns_false_when_above_original(self):
        dyno = make_dyno(formation_size="standard-2x")
        cache.set(dyno.original_size_cache_key,
                  {"size": "standard-1x", "time": timezone.now()}, timeout=None)
        self.assertFalse(dyno.is_on_original_formation_size_or_lower)

    def test_returns_false_when_original_is_unknown_size(self):
        dyno = make_dyno(formation_size="standard-1x")
        cache.set(dyno.original_size_cache_key,
                  {"size": "nonexistent-tier", "time": timezone.now()}, timeout=None)
        self.assertFalse(dyno.is_on_original_formation_size_or_lower)

    def test_returns_false_when_current_is_unknown_size(self):
        dyno = make_dyno(formation_size="unknown-size")
        cache.set(dyno.original_size_cache_key,
                  {"size": "standard-1x", "time": timezone.now()}, timeout=None)
        self.assertFalse(dyno.is_on_original_formation_size_or_lower)


# ─── L: can_be_upscaled compound condition edge cases ──────────────────────

class TestCanBeUpscaledGuard(BaseLockTestCase):
    """The bottom guard uses a compound 'can_be_upscaled' condition.
    Test the exact combinations that matter."""

    def test_standard_1x_no_original_no_previous_no_phantom(self):
        """Standard-1x, previous=None (lowest tier), no original → can_be_upscaled=False
        → should NOT create phantom key."""
        dyno = make_dyno(formation_size="standard-1x")
        _apply_dyno_patches(dyno, {}, dyno.check_and_downscale_to_original_formation_size)
        self.assertIsNone(cache.get(dyno.upscale_until_cache_key))

    def test_standard_2x_no_original_with_previous_creates_timer(self):
        """Standard-2x, previous=standard-1x, no original, high memory →
        can_be_upscaled=True → should create upscale_until (legitimate guard)."""
        dyno = make_dyno(formation_size="standard-2x")
        # No original_formation_size, but previous=standard-1x (from DYNO_SIZES)

        with patch_index_backend({}):
            _apply_dyno_patches(dyno, {
                "current_memory_usage": 600.0,
                "current_memory_usage_percentage": 58.6,
            }, dyno.check_and_downscale_to_original_formation_size)

        restored = cache.get(dyno.upscale_until_cache_key)
        self.assertIsNotNone(restored,
            "Standard-2x with no original but with previous should create guard timer")

    def test_standard_2x_with_original_2x_no_phantom(self):
        """Standard-2x, original=standard-2x → on original → no phantom."""
        dyno = make_dyno(formation_size="standard-2x")
        cache.set(dyno.original_size_cache_key,
                  {"size": "standard-2x", "time": timezone.now()}, timeout=None)

        with patch_index_backend({}):
            _apply_dyno_patches(dyno, {}, dyno.check_and_downscale_to_original_formation_size)

        self.assertIsNone(cache.get(dyno.upscale_until_cache_key))


class TestPhantomCleanupRace(BaseLockTestCase):

    def test_newer_baseline_and_timer_after_capture_survive(self):
        dyno = make_dyno(formation_size="standard-1x")
        initial_time = timezone.now()
        cache.set(
            dyno.original_size_cache_key,
            {"size": "standard-1x", "time": initial_time},
            timeout=None,
        )
        cache.set(
            dyno.upscale_until_cache_key,
            initial_time + timedelta(seconds=30),
            timeout=60,
        )

        with patch_index_backend({}) as backend:
            original_get = backend._client.get
            raced = []
            captured_reads = 0
            new_time = initial_time + timedelta(seconds=5)
            new_timer = initial_time + timedelta(seconds=90)

            def racing_get(key):
                nonlocal captured_reads
                captured = original_get(key)
                captured_reads += 1
                if captured_reads == 3 and not raced:
                    raced.append(True)
                    cache.set(
                        dyno.original_size_cache_key,
                        {"size": "standard-1x", "time": new_time},
                        timeout=None,
                    )
                    cache.set(
                        dyno.upscale_until_cache_key,
                        new_timer,
                        timeout=120,
                    )
                return captured

            backend._client.get = racing_get
            with patch.object(
                        type(dyno),
                        "current_memory_usage_percentage",
                        new_callable=PropertyMock,
                        return_value=0.0,
                    ), \
                    patch.object(dyno, "call_heroku_api") as api:
                dyno.check_and_downscale_to_original_formation_size()

            self.assertEqual(
                cache.get(dyno.original_size_cache_key)["time"],
                new_time,
            )
            self.assertEqual(
                cache.get(dyno.upscale_until_cache_key),
                new_timer,
            )
            self.assertIsNone(cache.get(dyno.downscale_cache_key))
            api.assert_not_called()


class TestScheduledTimerState(BaseLockTestCase):
    """The scheduled path must decide timer state from the primary raw value."""

    def _prime_upscaled(self, dyno, baseline="standard-1x"):
        cache.set(
            dyno.original_size_cache_key,
            {"size": baseline, "time": timezone.now()},
            timeout=None,
        )
        cache.set(
            dyno.upscale_until_cache_key,
            timezone.now() + timedelta(seconds=50),
            timeout=50,
        )

    def _run_scheduled(self, dyno):
        with patch.object(
                    type(dyno),
                    "current_memory_usage_percentage",
                    new_callable=PropertyMock,
                    return_value=10.0,
                ), \
                patch.object(
                    type(dyno),
                    "detected_r14",
                    new_callable=PropertyMock,
                    return_value=False,
                ), \
                patch.object(
                    type(dyno),
                    "detected_r15",
                    new_callable=PropertyMock,
                    return_value=False,
                ), \
                patch.object(dyno, "downscale_formation_to_original_size") as downscale, \
                patch.object(dyno, "restart_dyno") as restart:
            dyno.check_and_downscale_to_original_formation_size()
        return downscale, restart

    def test_serialized_none_timer_defers_without_resize(self):
        dyno = make_dyno(formation_size="performance-m")
        self._prime_upscaled(dyno)
        poisoned = pickle.dumps(None)
        with patch_index_backend({}) as backend:
            backend._store[dyno.upscale_until_cache_key] = poisoned
            downscale, restart = self._run_scheduled(dyno)

            self.assertEqual(
                backend._store[dyno.upscale_until_cache_key],
                poisoned,
            )
        downscale.assert_not_called()
        restart.assert_not_called()
        self.assertEqual(
            cache.get(dyno.original_size_cache_key)["size"],
            "standard-1x",
        )

    def test_undecodable_timer_defers_without_resize(self):
        dyno = make_dyno(formation_size="performance-m")
        self._prime_upscaled(dyno)
        poisoned = b"\x80not-a-pickle"
        with patch_index_backend({}) as backend:
            backend._store[dyno.upscale_until_cache_key] = poisoned
            downscale, restart = self._run_scheduled(dyno)

            self.assertEqual(
                backend._store[dyno.upscale_until_cache_key],
                poisoned,
            )
        downscale.assert_not_called()
        restart.assert_not_called()
        self.assertEqual(
            cache.get(dyno.original_size_cache_key)["size"],
            "standard-1x",
        )

    def test_wrong_type_timer_defers_without_resize(self):
        dyno = make_dyno(formation_size="performance-m")
        self._prime_upscaled(dyno)
        with patch_index_backend({}) as backend:
            real_get = backend._client.get

            def wrong_type_get(key):
                if key == dyno.upscale_until_cache_key:
                    raise ResponseError(
                        "WRONGTYPE Operation against a key holding the wrong "
                        "kind of value",
                    )
                return real_get(key)

            backend._client.get = wrong_type_get
            downscale, restart = self._run_scheduled(dyno)

        downscale.assert_not_called()
        restart.assert_not_called()
        self.assertEqual(
            cache.get(dyno.original_size_cache_key)["size"],
            "standard-1x",
        )
        self.assertIsNotNone(cache.get(dyno.upscale_until_cache_key))

    def test_raw_timer_miss_keeps_expiry_path(self):
        dyno = make_dyno(formation_size="performance-m")
        cache.set(
            dyno.original_size_cache_key,
            {"size": "standard-1x", "time": timezone.now()},
            timeout=None,
        )

        with patch_index_backend({}):
            downscale, restart = self._run_scheduled(dyno)

        downscale.assert_called_once()
        restart.assert_not_called()

    def test_live_timer_without_baseline_uses_previous_tier(self):
        dyno = make_dyno(formation_size="performance-m")
        timer = timezone.now() + timedelta(seconds=50)
        cache.set(dyno.upscale_until_cache_key, timer, timeout=50)

        with patch_index_backend({}), patch.object(cache, "ttl", return_value=50):
            downscale, restart = self._run_scheduled(dyno)

        downscale.assert_called_once()
        restart.assert_not_called()
        self.assertEqual(cache.get(dyno.upscale_until_cache_key), timer)
        self.assertIsNone(cache.get(dyno.original_size_cache_key))

    def _prime_live_timer_without_baseline(self, dyno):
        formation_key = (
            f"heroku:formation_size:{dyno.app_name}:{dyno.formation_name}"
        )
        cache.set(formation_key, dyno.formation_size, timeout=None)
        cache.delete(dyno.original_size_cache_key)
        timer = timezone.now() + timedelta(seconds=45)
        cache.set(dyno.upscale_until_cache_key, timer, timeout=50)
        return formation_key, timer

    def _run_reservation_case(self, *, remote, replace_timer):
        cache.clear()
        dyno = make_dyno(formation_size="performance-m")
        formation_key, timer = self._prime_live_timer_without_baseline(dyno)
        replacement = timer + timedelta(seconds=600)
        env = {
            "DYNO": "other_worker.9" if remote else dyno.dyno_name,
            "DYNO_RAM": "2560",
        }
        response = _mock_response(200)

        with patch.dict(os.environ, env), \
                patch_index_backend({}) as backend:
            backend._store[
                f"heroku:dyno_owner:v1:{dyno.app_name}:{dyno.dyno_name}"
            ] = dyno._dyno_owner_token
            real_pipeline = backend._client.pipeline
            # The fake raw client uses identity keys, unlike LocMemCache.
            timer_key = dyno.upscale_until_cache_key
            state = {"fired": False}

            class HookPipeline:
                def __init__(self, pipeline):
                    self._pipeline = pipeline

                def watch(self, *keys):
                    if replace_timer and not state["fired"] and timer_key in keys:
                        state["fired"] = True
                        cache.set(
                            dyno.upscale_until_cache_key,
                            replacement,
                            timeout=600,
                        )
                    return self._pipeline.watch(*keys)

                def __getattr__(self, name):
                    return getattr(self._pipeline, name)

            def hooked_pipeline(transaction=True):
                return HookPipeline(real_pipeline(transaction=transaction))

            backend._client.pipeline = hooked_pipeline
            with patch.object(
                        type(dyno),
                        "current_memory_usage_percentage",
                        new_callable=PropertyMock,
                        return_value=10.0,
                    ), \
                    patch.object(
                        type(dyno),
                        "detected_r14",
                        new_callable=PropertyMock,
                        return_value=False,
                    ), \
                    patch.object(
                        type(dyno),
                        "detected_r15",
                        new_callable=PropertyMock,
                        return_value=False,
                    ), \
                    patch.object(
                        dyno,
                        "_defer_resize_for_retiring_dyno",
                        return_value=False,
                    ), \
                    patch.object(
                        dyno, "call_heroku_api", return_value=response,
                    ) as api:
                dyno.check_and_downscale_to_original_formation_size()

            if replace_timer:
                self.assertTrue(state["fired"])
                api.assert_not_called()
                self.assertIsNone(cache.get(dyno.downscale_cache_key))
                self.assertEqual(
                    cache.get(dyno.upscale_until_cache_key),
                    replacement,
                )
                self.assertEqual(cache.get(formation_key), "performance-m")
                return

            self.assertFalse(state["fired"])
            api.assert_called_once()
            self.assertEqual(api.call_args[0][0], "PATCH")
            self.assertEqual(api.call_args[1]["data"], {"size": "standard-2x"})
            self.assertEqual(cache.get(formation_key), "standard-2x")

    def test_unchanged_timer_before_reservation_downscales(self):
        for remote in (False, True):
            with self.subTest(remote=remote):
                self._run_reservation_case(remote=remote, replace_timer=False)

    def test_timer_replaced_before_watch_defers(self):
        for remote in (False, True):
            with self.subTest(remote=remote):
                self._run_reservation_case(remote=remote, replace_timer=True)


class TestScheduledRemoteReservation(BaseLockTestCase):
    """Remote scheduled decisions keep their timer and baseline expectations."""

    def _prime(self, dyno, *, baseline="standard-1x"):
        formation_key = (
            f"heroku:formation_size:{dyno.app_name}:{dyno.formation_name}"
        )
        cache.set(formation_key, dyno.formation_size, timeout=None)
        cache.delete(dyno.original_size_cache_key)
        if baseline is not None:
            cache.set(
                dyno.original_size_cache_key,
                {"size": baseline, "time": timezone.now()},
                timeout=None,
            )
        timer = timezone.now() + timedelta(seconds=45)
        cache.set(dyno.upscale_until_cache_key, timer, timeout=50)
        return formation_key, timer

    def _enter_common(self, stack, dyno, api):
        stack.enter_context(patch.object(
            type(dyno),
            "current_memory_usage_percentage",
            new_callable=PropertyMock,
            return_value=10.0,
        ))
        stack.enter_context(patch.object(
            type(dyno),
            "detected_r14",
            new_callable=PropertyMock,
            return_value=False,
        ))
        stack.enter_context(patch.object(
            type(dyno),
            "detected_r15",
            new_callable=PropertyMock,
            return_value=False,
        ))
        return stack.enter_context(patch.object(dyno, "call_heroku_api", **api))

    def _patch_downscale_race(self, stack, dyno, race):
        state = {"fired": False}
        real_downscale = dyno.downscale_formation_to_original_size

        def run_race_then_downscale(*args, **kwargs):
            if not state["fired"]:
                state["fired"] = True
                race()
            return real_downscale(*args, **kwargs)

        stack.enter_context(patch.object(
            dyno,
            "downscale_formation_to_original_size",
            side_effect=run_race_then_downscale,
        ))
        return state

    def test_remote_scheduled_uses_recorded_baseline(self):
        dyno = make_dyno(formation_size="performance-m")
        formation_key, _ = self._prime(dyno)
        response = _mock_response(200)

        with patch.dict(
                    os.environ,
                    {"DYNO": "other_worker.9", "DYNO_RAM": "2560"},
                ), \
                patch_index_backend({}), \
                ExitStack() as stack:
            api = self._enter_common(
                stack, dyno, {"return_value": response},
            )
            dyno.check_and_downscale_to_original_formation_size()

        api.assert_called_once()
        self.assertEqual(api.call_args[1]["data"], {"size": "standard-1x"})
        self.assertEqual(cache.get(formation_key), "standard-1x")

    def test_remote_scheduled_cleanup_preserves_replacement_timer(self):
        dyno = make_dyno(formation_size="performance-m")
        formation_key, timer = self._prime(dyno)
        replacement = timer + timedelta(seconds=600)
        original_evaluate = dyno._evaluate_downscale_snapshot

        def evaluate_then_race(**kwargs):
            decision = original_evaluate(**kwargs)
            if decision.normal or decision.stability:
                cache.set(formation_key, "standard-1x", timeout=None)
                cache.set(
                    dyno.upscale_until_cache_key,
                    replacement,
                    timeout=600,
                )
            return decision

        with patch.dict(
                    os.environ,
                    {"DYNO": "other_worker.9", "DYNO_RAM": "2560"},
                ), \
                patch_index_backend({}), \
                patch.object(
                    dyno,
                    "_evaluate_downscale_snapshot",
                    side_effect=evaluate_then_race,
                ), \
                ExitStack() as stack:
            api = self._enter_common(stack, dyno, {})
            dyno.check_and_downscale_to_original_formation_size()

        api.assert_not_called()
        self.assertEqual(cache.get(dyno.upscale_until_cache_key), replacement)
        self.assertEqual(
            cache.get(dyno.original_size_cache_key)["size"],
            "standard-1x",
        )
        self.assertEqual(cache.get(formation_key), "standard-1x")

    def test_remote_manual_ignores_timer_change(self):
        dyno = make_dyno(formation_size="performance-m")
        formation_key, timer = self._prime(dyno)
        replacement = timer + timedelta(seconds=600)
        response = _mock_response(200)
        flag_key = f"heroku:downscale_formation:{dyno.formation_name}"

        with patch.dict(
                    os.environ,
                    {"DYNO": "other_worker.9", "DYNO_RAM": "2560"},
                ), \
                patch_index_backend({}) as backend, \
                ExitStack() as stack:
            real_pipeline = backend._client.pipeline
            state = {"fired": False}

            class HookPipeline:
                def __init__(self, pipeline):
                    self._pipeline = pipeline

                def watch(self, *keys):
                    result = self._pipeline.watch(*keys)
                    if not state["fired"] and flag_key in keys:
                        state["fired"] = True
                        from heroku_manager import heroku as heroku_module
                        heroku_module.cache.set(
                            dyno.upscale_until_cache_key,
                            replacement,
                            timeout=600,
                        )
                    return result

                def __getattr__(self, name):
                    return getattr(self._pipeline, name)

            def hooked_pipeline(transaction=True):
                return HookPipeline(real_pipeline(transaction=transaction))

            backend._client.pipeline = hooked_pipeline
            api = self._enter_common(
                stack, dyno, {"return_value": response},
            )
            dyno.downscale_formation_to_original_size()

        self.assertTrue(state["fired"])
        api.assert_called_once()
        self.assertEqual(api.call_args[1]["data"], {"size": "standard-1x"})
        self.assertEqual(cache.get(formation_key), "standard-1x")

    def test_remote_scheduled_cleanup_preserves_late_baseline_change(self):
        dyno = make_dyno(formation_size="performance-m")
        formation_key = (
            f"heroku:formation_size:{dyno.app_name}:{dyno.formation_name}"
        )
        cache.set(formation_key, "performance-m", timeout=None)
        cache.set(
            dyno.original_size_cache_key,
            {"size": "standard-1x", "time": timezone.now()},
            timeout=None,
        )
        cache.delete(dyno.upscale_until_cache_key)
        new_baseline = {"size": "standard-2x", "time": timezone.now()}
        original_read = _IndexAdapter.read_formation_size_raw
        state = {"fired": False}

        def read_then_change(adapter, formation_name, fallback_size):
            cache.set(formation_key, "standard-1x", timeout=None)
            result = original_read(adapter, formation_name, fallback_size)
            if not state["fired"]:
                state["fired"] = True
                cache.set(
                    dyno.original_size_cache_key,
                    new_baseline,
                    timeout=None,
                )
            return result

        with patch.dict(
                    os.environ,
                    {"DYNO": "other_worker.9", "DYNO_RAM": "1024"},
                ), \
                patch_index_backend({}), \
                patch.object(
                    _IndexAdapter,
                    "read_formation_size_raw",
                    new=read_then_change,
                ), \
                ExitStack() as stack:
            api = self._enter_common(stack, dyno, {})
            dyno.check_and_downscale_to_original_formation_size()

        self.assertTrue(state["fired"])
        api.assert_not_called()
        preserved_baseline = cache.get(dyno.original_size_cache_key)
        self.assertIsNotNone(preserved_baseline)
        self.assertEqual(preserved_baseline["size"], "standard-2x")
        self.assertEqual(cache.get(formation_key), "standard-1x")

    def test_remote_scheduled_miss_cleanup_preserves_new_timer(self):
        dyno = make_dyno(formation_size="standard-2x")
        formation_key = (
            f"heroku:formation_size:{dyno.app_name}:{dyno.formation_name}"
        )
        cache.set(formation_key, "standard-2x", timeout=None)
        cache.set(
            dyno.original_size_cache_key,
            {"size": "standard-2x", "time": timezone.now()},
            timeout=None,
        )
        cache.delete(dyno.upscale_until_cache_key)
        inserted = timezone.now() + timedelta(seconds=300)
        original_capture = _IndexAdapter.capture_formation_cleanup_state
        state = {"fired": False}

        def capture_then_insert(adapter, formation_name):
            result = original_capture(adapter, formation_name)
            if not state["fired"]:
                state["fired"] = True
                cache.set(
                    dyno.upscale_until_cache_key,
                    inserted,
                    timeout=300,
                )
            return result

        with patch.dict(
                    os.environ,
                    {"DYNO": "other_worker.9", "DYNO_RAM": "1024"},
                ), \
                patch_index_backend({}), \
                patch.object(
                    _IndexAdapter,
                    "capture_formation_cleanup_state",
                    new=capture_then_insert,
                ), \
                ExitStack() as stack:
            api = self._enter_common(stack, dyno, {})
            dyno.check_and_downscale_to_original_formation_size()

        self.assertTrue(state["fired"])
        api.assert_not_called()
        self.assertEqual(cache.get(dyno.upscale_until_cache_key), inserted)
        self.assertEqual(
            cache.get(dyno.original_size_cache_key)["size"],
            "standard-2x",
        )

    def _assert_timer_expiry_unchanged(self, dyno, expected, observed):
        self.assertEqual(cache.get(dyno.upscale_until_cache_key), expected)
        elapsed = time.monotonic() - observed["started"]
        remaining = cache.ttl(dyno.upscale_until_cache_key)
        self.assertGreaterEqual(remaining, observed["ttl"] - elapsed - 2)
        self.assertLessEqual(remaining, observed["ttl"] + 1)

    def test_remote_scheduled_tier_drop_after_capture_defers_cleanup(self):
        dyno = make_dyno(formation_size="performance-m")
        formation_key, timer = self._prime(
            dyno, baseline="standard-2x",
        )
        baseline = cache.get(dyno.original_size_cache_key)
        observed = {
            "started": time.monotonic(),
            "ttl": cache.ttl(dyno.upscale_until_cache_key),
        }

        def race():
            cache.set(formation_key, "standard-1x", timeout=None)

        with patch.dict(
                    os.environ,
                    {"DYNO": "other_worker.9", "DYNO_RAM": "2560"},
                ), \
                patch_index_backend({}), \
                ExitStack() as stack:
            state = self._patch_downscale_race(stack, dyno, race)
            api = self._enter_common(
                stack, dyno, {"return_value": _mock_response(200)},
            )
            dyno.check_and_downscale_to_original_formation_size()

        self.assertTrue(state["fired"])
        api.assert_not_called()
        self.assertIsNone(cache.get(dyno.downscale_cache_key))
        self.assertEqual(cache.get(formation_key), "standard-1x")
        self.assertEqual(cache.get(dyno.original_size_cache_key), baseline)
        self._assert_timer_expiry_unchanged(dyno, timer, observed)

    def test_remote_scheduled_tier_rise_after_timer_miss_is_cleanup_only(self):
        dyno = make_dyno(formation_size="standard-2x")
        formation_key = (
            f"heroku:formation_size:{dyno.app_name}:{dyno.formation_name}"
        )
        cache.set(formation_key, "standard-2x", timeout=None)
        baseline = {"size": "standard-2x", "time": timezone.now()}
        cache.set(dyno.original_size_cache_key, baseline, timeout=None)
        cache.delete(dyno.upscale_until_cache_key)
        inserted = timezone.now() + timedelta(seconds=300)
        observed = {}

        def race():
            cache.set(formation_key, "performance-m", timeout=None)
            cache.set(
                dyno.upscale_until_cache_key,
                inserted,
                timeout=300,
            )
            observed["started"] = time.monotonic()
            observed["ttl"] = cache.ttl(dyno.upscale_until_cache_key)

        with patch.dict(
                    os.environ,
                    {"DYNO": "other_worker.9", "DYNO_RAM": "1024"},
                ), \
                patch_index_backend({}), \
                ExitStack() as stack:
            stack.enter_context(patch.object(
                dyno,
                "_defer_resize_for_retiring_dyno",
                return_value=False,
            ))
            state = self._patch_downscale_race(stack, dyno, race)
            api = self._enter_common(
                stack, dyno, {"return_value": _mock_response(200)},
            )
            dyno.check_and_downscale_to_original_formation_size()

        self.assertTrue(state["fired"])
        api.assert_not_called()
        self.assertIsNone(cache.get(dyno.downscale_cache_key))
        self.assertEqual(cache.get(formation_key), "performance-m")
        self.assertEqual(cache.get(dyno.original_size_cache_key)["size"], "standard-2x")
        self._assert_timer_expiry_unchanged(dyno, inserted, observed)

    def test_local_scheduled_fallback_change_is_cleanup_only(self):
        dyno = make_dyno(formation_size="standard-2x")
        formation_key = (
            f"heroku:formation_size:{dyno.app_name}:{dyno.formation_name}"
        )
        cache.set(formation_key, "standard-2x", timeout=None)
        cache.set(
            dyno.original_size_cache_key,
            {"size": "standard-2x", "time": timezone.now()},
            timeout=None,
        )
        cache.delete(dyno.upscale_until_cache_key)
        replacement_baseline = {
            "size": "standard-1x",
            "time": timezone.now(),
        }
        inserted = timezone.now() + timedelta(seconds=300)
        observed = {}

        def race():
            cache.set(
                dyno.original_size_cache_key,
                replacement_baseline,
                timeout=None,
            )
            cache.set(
                dyno.upscale_until_cache_key,
                inserted,
                timeout=300,
            )
            observed["started"] = time.monotonic()
            observed["ttl"] = cache.ttl(dyno.upscale_until_cache_key)

        with patch.dict(
                    os.environ,
                    {"DYNO": dyno.dyno_name, "DYNO_RAM": "1024"},
                ), \
                patch_index_backend({}) as backend, \
                ExitStack() as stack:
            backend._store[
                f"heroku:dyno_owner:v1:{dyno.app_name}:{dyno.dyno_name}"
            ] = dyno._dyno_owner_token
            stack.enter_context(patch.object(
                dyno,
                "_defer_resize_for_retiring_dyno",
                return_value=False,
            ))
            state = self._patch_downscale_race(stack, dyno, race)
            api = self._enter_common(
                stack, dyno, {"return_value": _mock_response(200)},
            )
            dyno.check_and_downscale_to_original_formation_size()

        self.assertTrue(state["fired"])
        api.assert_not_called()
        self.assertIsNone(cache.get(dyno.downscale_cache_key))
        self.assertEqual(cache.get(formation_key), "standard-2x")
        self.assertEqual(
            cache.get(dyno.original_size_cache_key),
            replacement_baseline,
        )
        self._assert_timer_expiry_unchanged(dyno, inserted, observed)


if __name__ == "__main__":
    unittest.main()
