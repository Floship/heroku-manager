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

import unittest
from datetime import timedelta
from unittest.mock import patch, MagicMock, PropertyMock, call

from django.core.cache import cache
from django.utils import timezone

from tests.conftest import make_dyno, BaseLockTestCase, patch_cache_keys


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

    result = None
    ctx_managers = [p.__enter__() for p in patches]
    try:
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

        with patch_cache_keys({}):
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

        with patch_cache_keys({}):
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

        with patch_cache_keys({}):
            with patch.object(cache, "ttl", return_value=50):
                _apply_dyno_patches(dyno, {"current_memory_usage": 350.0,
                                           "current_memory_usage_percentage": 34.2},
                                    dyno.check_and_downscale_to_original_formation_size)

        extended = cache.get(dyno.upscale_until_cache_key)
        self.assertIsNotNone(extended,
            "Legitimate upscale timer must be extended, not cleared")

    def test_multi_level_upscale_timer_preserved(self):
        """On performance-m (2560MB), original=standard-1x (512MB) —
        genuinely 2 levels up. Timer must be preserved."""
        dyno = make_dyno(formation_size="performance-m")
        cache.set(dyno.original_size_cache_key,
                  {"size": "standard-1x", "time": timezone.now()}, timeout=None)
        until = timezone.now() + timedelta(seconds=200)
        cache.set(dyno.upscale_until_cache_key, until, timeout=300)

        with patch_cache_keys({}):
            with patch.object(cache, "ttl", return_value=50):
                _apply_dyno_patches(dyno, {"current_memory_usage": 900.0,
                                           "current_memory_usage_percentage": 35.2},
                                    dyno.check_and_downscale_to_original_formation_size)

        extended = cache.get(dyno.upscale_until_cache_key)
        self.assertIsNotNone(extended,
            "Multi-level upscale timer must be preserved")

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

        with patch_cache_keys({}):
            with patch.object(cache, "ttl", return_value=50):
                with patch.object(dyno, "downscale_formation_to_original_size") as mock_ds:
                    _apply_dyno_patches(dyno, {
                        "current_memory_usage": 300.0,
                        "current_memory_usage_percentage": 29.3,
                    }, dyno.check_and_downscale_to_original_formation_size)

        mock_ds.assert_called_once()

    def test_near_expiry_r14_hot_empty_queue_restarts(self):
        """Upscaled, near expiry, R14 active + hot memory + empty queue → restart."""
        dyno = make_dyno(formation_size="standard-2x")
        cache.set(dyno.original_size_cache_key,
                  {"size": "standard-1x", "time": timezone.now()}, timeout=None)
        cache.set(dyno.upscale_until_cache_key,
                  timezone.now() + timedelta(seconds=50), timeout=50)

        with patch_cache_keys({}):
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

        with patch_cache_keys({}):
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

        with patch_cache_keys({}):
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

        with patch_cache_keys({}):
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

        with patch_cache_keys({}):
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

        with patch_cache_keys({}):
            _apply_dyno_patches(dyno, {}, dyno.check_and_downscale_to_original_formation_size)

        self.assertIsNone(cache.get(dyno.upscale_until_cache_key))


if __name__ == "__main__":
    unittest.main()
