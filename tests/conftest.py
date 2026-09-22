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

from unittest.mock import MagicMock, patch
from heroku_manager.heroku import HerokuDyno, DYNO_SIZES


def make_dyno(dyno_name="normal_worker.1", formation_size="standard-2x",
              index_ready_seed=True):
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
    if index_ready_seed:
        # Legacy unit fixtures exercise sibling/queue/stability logic and opt
        # into a seeded ready state so destructive-gate tests are not
        # weakened.  Dedicated readiness tests pass index_ready_seed=False and
        # drive the real predicate.  This is a test fixture, never a runtime
        # branch.  Seeding is per-instance (via instance dict) so the shared
        # production class descriptor is never mutated.
        dyno.__dict__["index_ready"] = True
    return dyno


def patch_index_backend(memory_store):
    """Patch the raw client so the index adapter reads ``memory_store``.

    LocMem has no raw client; the Phase B adapter requires one.  This helper
    installs a django-redis-style backend proxy on top of the real LocMemCache
    (delegating get/set/delete/lock) with ``make_key`` identity and a
    ZRANGEBYSCORE that lists the fixture store's dyno names.  There is no SCAN
    on the fake client, so a test that still tried to scan would fail loudly.
    """
    from django.core.cache import cache as real_cache

    class _FakeClient:
        """Raw redis-py client with a real decode for the adapter."""

        def __init__(self, backend):
            self._backend = backend

        def zremrangebyscore(self, key, min_score, max_score):
            return 0

        def zrangebyscore(self, key, min_score, max_score):
            # Derive fresh index members from the fixture store's metric keys
            # so legacy ready-path fixtures (seeded index_ready) actually see
            # their siblings through the index.
            dynos = set()
            for k in self._backend._store:
                parts = k.split(":")
                if len(parts) == 3 and parts[0] == "heroku":
                    dynos.add(parts[2])
            return sorted(dynos)

        def zscore(self, key, member):
            # No index ZSET in the fixture store: readiness is not derivable,
            # so the real predicate fails closed.  Legacy destructive-gate
            # tests seed index_ready directly and never hit this.
            return None

        def get(self, key):
            import pickle
            value = self._backend._store.get(key, None)
            if value is None:
                value = self._backend.get(key)
            return pickle.dumps(value) if value is not None else None

        def zrange(self, key, start, end):
            # No failed-writer ledger in the fixture store: never degraded.
            return []

        def mget(self, keys):
            import pickle
            # Dict fixtures carry Python values (mirroring cache.set); list
            # fixtures only name keys, so their values live in the real locmem
            # backend behind ``_ScanBackend.get``.  Either way raw MGET results
            # are serialized bytes, which the adapter must decode back into
            # Python numbers/datetimes before callers compare them.
            raw = []
            for key in keys:
                value = self._backend._store.get(key, None)
                if value is None:
                    value = self._backend.get(key)
                raw.append(pickle.dumps(value) if value is not None else None)
            return raw

        def decode(self, value):
            import pickle
            return pickle.loads(value)

        def zadd(self, *args, **kwargs):
            return 1

        def zrem(self, *args, **kwargs):
            return 1

    class _ScanBackend:
        """Proxy: raw-client seam for the index adapter, LocMem for the rest."""

        def __init__(self, store):
            if isinstance(store, list):
                # List fixtures (bare key lists) mirror the locmem cache: values
                # live in the real backend, reachable via get().
                self._store = {key: None for key in store}
            else:
                self._store = dict(store)
            self._client = _FakeClient(self)
            self._raw = MagicMock()
            self._raw.get_client.return_value = self._client
            # The production adapter captures the decoder from cache.client
            # (the django-redis DefaultClient), so the fake backend must expose
            # the real decode there too.
            self._raw.decode = self._client.decode

        def make_key(self, key, version=None):
            return key

        @property
        def client(self):
            return self._raw

        def __getattr__(self, name):
            return getattr(real_cache, name)

    return patch("heroku_manager.heroku.cache", _ScanBackend(memory_store))




def real_decoder():
    """Real django-redis DefaultClient.decode callable for wire-format tests.

    pickle.dumps(300) is not int()-parseable, so DefaultClient.decode falls
    through to the serializer and returns 300 — proving raw MGET bytes are
    decoded into Python values before callers compare them.  Construct inside
    a ``patch.object(DefaultClient, 'decode', wraps=DefaultClient.decode)``
    block to observe the decode path through the django-redis class method.
    """
    import pickle
    from django_redis.client.default import DefaultClient
    decoder = DefaultClient.__new__(DefaultClient)
    decoder._serializer = type(
        "S", (),
        {"loads": staticmethod(pickle.loads), "dumps": staticmethod(pickle.dumps)},
    )()
    decoder._compressor = type(
        "C", (),
        {"decompress": staticmethod(lambda v: v), "compress": staticmethod(lambda v: v)},
    )()
    # Return a callable that always routes through DefaultClient.decode with a
    # REAL instance as self.  When the class method is wrapped (patch.object
    # with wraps=), the wrap still sees the real instance and the real
    # serializer/compressor, so the decode path is observable and functional.
    def _decode(raw):
        return DefaultClient.decode(decoder, raw)
    return _decode


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
