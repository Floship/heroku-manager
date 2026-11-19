"""
Shared test setup: minimal Django config + make_dyno helper.
Import this in every test module via `from tests.conftest import *` or rely on
unittest discovery picking it up automatically.
"""
import base64
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

TEST_PROXY_MANAGER_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
# Transport-only tests mock the server; this is a public, correctly sized envelope.
TEST_PROXY_CREDENTIAL = "v2:" + base64.urlsafe_b64encode(bytes([3]) * 44).rstrip(b"=").decode()


def make_dyno(dyno_name="normal_worker.1", formation_size="standard-2x",
              index_ready_seed=True):
    """Minimal HerokuDyno with formation size pinned via instance cache."""
    dyno = HerokuDyno.__new__(HerokuDyno)
    dyno.app_name = "floship"
    dyno.dyno_name = dyno_name
    dyno.dyno_id = "abc123"
    dyno.formation_name = dyno_name.split(".")[0]
    dyno.heroku_api_key = None
    from heroku_manager.proxy import HerokuProxy
    dyno.proxy = HerokuProxy("https://manager.example", "test", "01" * 32,
                            credential=TEST_PROXY_CREDENTIAL, manager_id=TEST_PROXY_MANAGER_ID)
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


def patch_index_backend(memory_store, own_dyno="normal_worker.1"):
    """Patch the raw client so the index adapter reads ``memory_store``.

    LocMem has no raw client; the Phase B adapter requires one.  This helper
    installs a django-redis-style backend proxy on top of the real LocMemCache
    (delegating get/set/delete/lock) with ``make_key`` identity and a
    ZRANGEBYSCORE that lists the fixture store's dyno names.  There is no SCAN
    on the fake client, so a test that still tried to scan would fail loudly.
    """
    from django.core.cache import cache as real_cache

    class _FakePipeline:
        """Small WATCH/MULTI implementation for the task-owned test fixture."""

        def __init__(self, client):
            self._client = client
            self._backend = client._backend
            self._watched = {}
            self._commands = []

        def watch(self, *keys):
            self._watched.update({
                key: self._backend._versions.get(key, 0) for key in keys
            })
            return self

        def multi(self):
            return self

        def get(self, key):
            return self._client.get(key)

        def zscore(self, key, member):
            return self._client.zscore(key, member)

        def zrangebyscore(self, *args, **kwargs):
            return self._client.zrangebyscore(*args, **kwargs)

        def mget(self, keys):
            return self._client.mget(keys)

        def zrange(self, *args, **kwargs):
            return self._client.zrange(*args, **kwargs)

        def exists(self, *keys):
            return self._client.exists(*keys)

        def set(self, key, value, px=None):
            self._commands.append(("set", key, value))
            return self

        def delete(self, key):
            self._commands.append(("delete", key, None))
            return self

        def zadd(self, key, mapping):
            self._commands.append(("zadd", key, mapping))
            return self

        def zrem(self, key, member):
            self._commands.append(("zrem", key, member))
            return self

        def ping(self):
            self._commands.append(("ping", None, None))
            return self

        def execute(self):
            import pickle
            from redis.exceptions import WatchError
            for key, version in self._watched.items():
                if self._backend._versions.get(key, 0) != version:
                    self.reset()
                    raise WatchError("watched cache value changed")
            results = []
            for command, key, value in self._commands:
                if command == "set":
                    self._backend._store[key] = value
                    cached_value = value
                    if isinstance(value, bytes):
                        try:
                            cached_value = pickle.loads(value)
                        except Exception:
                            cached_value = value
                    real_cache.set(key, cached_value)
                    results.append(True)
                elif command == "delete":
                    self._backend._store.pop(key, None)
                    real_cache.delete(key)
                    results.append(1)
                elif command == "zadd":
                    self._backend._index_scores.update(value)
                    results.append(len(value))
                elif command == "zrem":
                    self._backend._index_scores.pop(value, None)
                    results.append(1)
                elif command == "ping":
                    results.append(True)
                self._backend._touch(key)
            self.reset()
            return results

        def reset(self):
            self._watched = {}
            self._commands = []
            return None

    class _FakeClient:
        """Raw redis-py client with a real decode for the adapter."""

        def __init__(self, backend):
            self._backend = backend

        def zremrangebyscore(self, key, min_score, max_score):
            return 0

        def zrangebyscore(self, key, min_score, max_score, withscores=False):
            # Derive fresh index members from the fixture store's metric keys
            # so legacy ready-path fixtures (seeded index_ready) actually see
            # their siblings through the index.
            self._backend._ensure_index()
            members = sorted(self._backend._index_scores)
            if withscores:
                return [(member, self._backend._index_scores[member]) for member in members]
            return members

        def zscore(self, key, member):
            return self._backend._index_scores.get(member)

        def get(self, key):
            import pickle
            value = self._backend._raw_lookup(key)
            if value is None:
                return None
            return value if isinstance(value, bytes) else pickle.dumps(value)

        def delete(self, key):
            existed = key in self._backend._store
            self._backend._store.pop(key, None)
            self._backend._touch(key)
            return 1 if existed else 0

        def zrange(self, key, start, end, withscores=False):
            # No failed-writer ledger in the fixture store: never degraded.
            return []

        def exists(self, *keys):
            count = 0
            for key in keys:
                if key in self._backend._store:
                    count += 1
                elif key.startswith("autoscaler") or "heroku:dynos:v1:" in key:
                    count += 1 if self._backend._index_scores else 0
                elif self._backend._lookup(key) is not None:
                    count += 1
            return count

        def mget(self, keys):
            import pickle
            self._backend._ensure_observations()
            # Dict fixtures carry Python values (mirroring cache.set); list
            # fixtures only name keys, so their values live in the real locmem
            # backend behind ``_ScanBackend.get``.  Either way raw MGET results
            # are serialized bytes, which the adapter must decode back into
            # Python numbers/datetimes before callers compare them.
            raw = []
            for key in keys:
                value = self._backend._raw_lookup(key)
                if value is None:
                    raw.append(None)
                elif isinstance(value, bytes):
                    raw.append(value)
                else:
                    raw.append(pickle.dumps(value))
            return raw

        def decode(self, value):
            import pickle
            return pickle.loads(value)

        def encode(self, value):
            import pickle
            return pickle.dumps(value)

        def zadd(self, *args, **kwargs):
            if len(args) >= 2 and isinstance(args[1], dict):
                self._backend._index_scores.update(args[1])
                self._backend._touch(args[0])
            return 1

        def zrem(self, *args, **kwargs):
            if len(args) >= 2:
                self._backend._index_scores.pop(args[1], None)
                self._backend._touch(args[0])
            return 1

        def expire(self, key, timeout):
            return 1

        def pipeline(self, transaction=True):
            return _FakePipeline(self)

    class _ScanBackend:
        """Proxy: raw-client seam for the index adapter, LocMem for the rest."""

        def __init__(self, store):
            if isinstance(store, list):
                # List fixtures (bare key lists) mirror the locmem cache: values
                # live in the real backend, reachable via get().
                self._store = {key: None for key in store}
            else:
                self._store = dict(store)
            dynos = {own_dyno}
            for key in self._store:
                parts = key.split(":")
                if len(parts) == 3 and parts[0] == "heroku":
                    dynos.add(parts[2])
            self._initial_dynos = dynos
            self._index_scores = {}
            self._versions = {}
            self._synthetic_owners = set()
            self._synthetic_observations = set()
            self._client = _FakeClient(self)
            self._raw = MagicMock()
            self._raw.get_client.return_value = self._client
            # The production adapter captures the decoder from cache.client
            # (the django-redis DefaultClient), so the fake backend must expose
            # the real decode there too.
            self._raw.decode = self._client.decode
            self._raw.encode = self._client.encode

        def _touch(self, key):
            self._versions[key] = self._versions.get(key, 0) + 1

        def _raw_lookup(self, key):
            if key in self._store:
                return self._store[key]
            return real_cache.get(key)

        def _lookup(self, key):
            import pickle
            value = self._raw_lookup(key)
            if isinstance(value, bytes):
                try:
                    return pickle.loads(value)
                except Exception:
                    return value
            return value

        def _ensure_index(self):
            dynos = set(self._initial_dynos)
            for key in self._store:
                parts = key.split(":")
                if len(parts) == 3 and parts[0] == "heroku":
                    dynos.add(parts[2])
            check_in_epoch = time.time()
            for dyno in dynos:
                self._index_scores.setdefault(dyno, check_in_epoch)

        def _metric_value(self, key, default):
            if key in self._store:
                return self._store[key]
            value = real_cache.get(key)
            return default if value is None and key not in self._store else value

        def _ensure_observations(self):
            self._ensure_index()
            self._ensure_owners()
            for dyno, check_in_epoch in self._index_scores.items():
                observation_key = f"heroku:dyno_observation:v1:floship:{dyno}"
                if observation_key in self._store and observation_key not in self._synthetic_observations:
                    continue
                memory = self._metric_value(f"heroku:dyno_memory:{dyno}", 100.0)
                stable = self._metric_value(f"heroku:dyno_memory_stable:{dyno}", False)
                load = self._metric_value(f"heroku:dyno_load:{dyno}", 0.0)
                self._store[observation_key] = (
                    check_in_epoch, memory, stable, load,
                )
                self._synthetic_observations.add(observation_key)

        def _ensure_owners(self):
            for dyno in self._index_scores:
                owner_key = f"heroku:dyno_owner:v1:floship:{dyno}"
                if owner_key not in self._store:
                    self._store[owner_key] = f"fixture-owner:{dyno}"
                    self._synthetic_owners.add(owner_key)

        def make_key(self, key, version=None):
            return key

        def get(self, key, default=None):
            value = self._lookup(key)
            return default if value is None else value

        def set(self, key, value, timeout=None, **kwargs):
            self._store[key] = value
            real_cache.set(key, value, timeout=timeout)
            self._touch(key)
            return True

        def delete(self, key, **kwargs):
            existed = key in self._store or real_cache.get(key) is not None
            self._store.pop(key, None)
            real_cache.delete(key)
            self._touch(key)
            return existed

        def ttl(self, key, default_timeout=None):
            if key in self._store:
                return 300
            return real_cache.ttl(key)

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
