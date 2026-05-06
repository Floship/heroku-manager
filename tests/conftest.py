"""
Shared test setup: minimal Django config + make_dyno helper.
Import this in every test module via `from tests.conftest import *` or rely on
unittest discovery picking it up automatically.
"""
import os
import time
import threading
import django
from django.conf import settings as django_settings

if not django_settings.configured:
    django_settings.configure(
        CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}},
        WORKER_SETTINGS_MAP={},
        DYNO_AUTOSCALE_INTERVAL=30,
        HIGH_MEM_USE_MB=400,
        UPSCALE_PERCENTAGE_HIGH_MEM_USE=80,
        DOWNSCALE_PERCENTAGE_HIGH_MEM_USE=105,
        DYNO_TIME_BETWEEN_SCALES=300,
        DYNO_TIME_BETWEEN_RESTARTS=300,
        DYNO_ZOMBIE_THRESHOLD=300,
        DYNO_DOWNSCALE_CHECK_INTERVAL=60,
        DYNO_MIN_UPSCALE_DURATION=300,
        DYNO_ERRORS_TIMEOUT_DURATION=60,
        DYNO_GENERAL_CACHE_DURATION=300,
        DYNO_LOGS_CACHE_DURATION=90,
        DYNO_AUTOSCALE_ENABLED_FOR_BEATWORKER=True,
        DYNO_MEMORY_STABILITY_WINDOW=300,
        DYNO_MEMORY_STABILITY_MIN_READINGS=6,
        DYNO_MEMORY_STABILITY_TOLERANCE_MB=50,
        DYNO_MEMORY_STABILITY_TOLERANCE_PCT=1,
        DYNO_STABILITY_LOAD_THRESHOLD=1.0,
        USE_TZ=True,
        TIME_ZONE="UTC",
    )
    django.setup()

from unittest.mock import MagicMock, PropertyMock
from heroku_manager.heroku import HerokuDyno, DYNO_SIZES


def make_dyno(dyno_name="normal_worker.1", formation_size="standard-2x"):
    """Minimal HerokuDyno with formation size pinned via instance cache."""
    dyno = HerokuDyno.__new__(HerokuDyno)
    dyno.app_name = "floship"
    dyno.dyno_name = dyno_name
    dyno.dyno_id = "abc123"
    dyno.formation_name = dyno_name.split(".")[0]
    dyno.heroku_api_key = "fake-key"
    dyno._stop_autoscale_event = threading.Event()
    dyno._stop_file_cleaning_event = threading.Event()
    dyno._autoscale_thread = None
    dyno._file_cleaning_thread = None
    dyno._thread_lock = threading.Lock()
    dyno._last_file_cleaning = None
    dyno._formation_size_cached = (formation_size, time.time())
    return dyno


def patch_cache_keys(memory_store):
    """Patch cache.keys() (locmem has none) to return from a dict."""
    from unittest.mock import patch
    from django.core.cache import cache

    def _keys(pattern):
        prefix = pattern.replace(".*", ".") if ".*" in pattern else pattern
        return [k for k in memory_store if k.startswith(prefix)]

    return patch.object(cache, "keys", side_effect=_keys, create=True)


import unittest
from unittest.mock import patch
from django.core.cache import cache as _cache


def _make_lock_cm():
    m = MagicMock()
    m.__enter__ = MagicMock(return_value=None)
    m.__exit__ = MagicMock(return_value=False)
    return m


# Patch LocMemCache class directly — avoids ConnectionProxy delattr issues
from django.core.cache.backends.locmem import LocMemCache as _LocMemCache
if not hasattr(_LocMemCache, "lock"):
    _LocMemCache.lock = lambda self, *a, **kw: _make_lock_cm()
if not hasattr(_LocMemCache, "ttl"):
    _LocMemCache.ttl = lambda self, key: 0  # 0 = stale; tests that need fresh TTL mock this


class BaseLockTestCase(unittest.TestCase):
    """TestCase that clears cache before/after each test.
    lock() and ttl() are patched at the LocMemCache class level above."""

    def setUp(self):
        _cache.clear()

    def tearDown(self):
        _cache.clear()
