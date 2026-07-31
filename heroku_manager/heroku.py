import math
import os
import re
import shlex
import requests
import threading
import time
import random
from datetime import datetime, timedelta
from requests.exceptions import SSLError, ConnectionError, Timeout

from django.conf import settings
from django.core.cache import cache
from django.utils.functional import cached_property
from django.utils import timezone
from importlib import import_module

import logging

logger = logging.getLogger(__name__)


def _update_dyno_registry(app_name, dyno_name, score=None):
    if not app_name or not dyno_name:
        return False

    try:
        client = cache.client.get_client(write=True)
        key = cache.make_key(f'heroku:dynos:v1:{app_name}')
        if score is None:
            client.zrem(key, dyno_name)
        else:
            client.zadd(key, {dyno_name: score})
        return True
    except Exception:
        logger.warning(
            "Failed to update indexed dyno registry for app %s, dyno %s.",
            app_name, dyno_name, exc_info=True,
        )
        return False

# Gracefully handle lock expiry.  Consumers may configure different lock backends —
# redis_lock.django_cache.RedisCache raises redis_lock.NotAcquired, while
# plain django-redis raises redis.exceptions.LockNotOwnedError.
# not a direct dep of heroku-manager.
try:
    from redis_lock import NotAcquired as _NotAcquired
    _LockExpiryErrors = (_NotAcquired,)
except ImportError:
    class _NotAcquired(Exception):
        pass
    _LockExpiryErrors = (_NotAcquired,)

try:
    from redis.exceptions import LockNotOwnedError
except ImportError:
    class LockNotOwnedError(Exception):
        pass
_LockExpiryErrors += (LockNotOwnedError,)


# Dyno size hierarchy with memory mapping
DYNO_SIZES = {
    "standard-1x": {
        "next": "standard-2x",
        "previous": None,
        "memory": 512,
        "threads_available": 4,
        "price_per_hour": 0.035,
        "max_price_per_month": 25.00
    },
    "standard-2x": {
        "next": "performance-m",
        "previous": "standard-1x",
        "memory": 1024,
        "threads_available": 8,
        "price_per_hour": 0.069,
        "max_price_per_month": 50.00
    },
    "performance-m": {
        "next": "performance-l-ram",  # Skips "performance-l" when scaling up
        "previous": "standard-2x",
        "memory": 2560,
        "threads_available": 12,
        "price_per_hour": 0.347,
        "max_price_per_month": 250.00
    },
    "performance-l": {
        "next": "performance-l-ram",
        "previous": "performance-m",  # Allows scaling down to "performance-m"
        "memory": 14336,
        "threads_available": 50,
        "price_per_hour": 0.694,
        "max_price_per_month": 500.00
    },
    "performance-l-ram": {
        "next": "performance-xl",
        "previous": "performance-m",
        "memory": 30720,
        "threads_available": 24,
        "price_per_hour": 0.694,
        "max_price_per_month": 500.00
    },
    "performance-xl": {
        "next": "performance-2xl",
        "previous": "performance-l-ram",
        "memory": 63488,
        "threads_available": 50,
        "price_per_hour": 1.04,
        "max_price_per_month": 750.00
    },
    "performance-2xl": {
        "next": None,
        "previous": "performance-xl",
        "memory": 126976,
        "threads_available": 100,
        "price_per_hour": 2.08,
        "max_price_per_month": 1500.00
    }
}

def get_dyno_settings(formation_size=None):
    dyno_memory = int(os.environ.get('DYNO_RAM', 512))
    if not formation_size:
        for dyno, size_info in DYNO_SIZES.items():
            if size_info["memory"] == dyno_memory:
                return dict(size_info)

        formation_size = 'standard-1x'
    return dict(DYNO_SIZES.get(formation_size, {}))

class _ApiResult:
    """Minimal result object returned by exec_connect, mirrors subprocess.run output."""
    __slots__ = ('stdout', 'stderr', 'returncode')

    def __init__(self, stdout: str, stderr: str, returncode: int) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


class HerokuManager:
    _instance = None
    _lock = threading.Lock()

    @staticmethod
    def get_autoscaler():
        with HerokuManager._lock:
            if HerokuManager._instance is None:
                HerokuManager._instance = HerokuDyno()
            return HerokuManager._instance

class HerokuDyno:
    def __init__(self, dyno_name=None):
        self.app_name = os.environ.get('HEROKU_APP_NAME')
        self.dyno_name = dyno_name or os.environ.get('DYNO', None)
        self.dyno_id = os.environ.get('HEROKU_DYNO_ID', None)
        # self.dyno_name = 'webhooks_worker.1'
        self.formation_name = self.dyno_name.split('.')[0] if self.dyno_name else None
        self.heroku_api_key = os.environ.get('HEROKU_API_KEY')
        self._stop_autoscale_event = threading.Event()
        self._stop_file_cleaning_event = threading.Event()
        self._autoscale_thread = None
        self._file_cleaning_thread = None
        self._thread_lock = threading.Lock()
        self._last_file_cleaning = None
        self._formation_size_cached = None  # (size, timestamp) tuple for instance-level caching

    # Per-dyno cache key prefixes — published in check_in_dyno, cleaned in
    # remove_dyno_from_alive_cache.  Add new metrics here to publish/clean
    # them automatically.
    _DYNO_CACHE_PREFIXES = ('alive', 'memory', 'memory_stable', 'load')

    @property
    def _get_proc_class_by_formation_name(self):
        """
        Dynamically find and return the proc class corresponding to the formation_name
        from settings.HIREFIRE_PROCS.
        """
        try:
            for proc_path in settings.HIREFIRE_PROCS:
                module_path, class_name = proc_path.rsplit('.', 1)
                proc_class = getattr(import_module(module_path), class_name)
                if proc_class.name == self.formation_name:
                    return proc_class
        except Exception as e:
            logger.error(f"Failed to find proc class for formation {self.formation_name}. Error: {e}", exc_info=True)

    @property
    def tasks_in_queue(self):
        """
        Get the quantity of tasks in the proc queue.
        """
        proc_class = self._get_proc_class_by_formation_name
        if (proc_class):
            return proc_class().quantity({})
        return 0

    @property
    def no_tasks_in_queue(self):
        return self.tasks_in_queue == 0

    @cached_property
    def settings(self):
        dyno_settings = {}
        dyno_settings.update(get_dyno_settings(self.formation_size))
        dyno_settings.update(settings.WORKER_SETTINGS_MAP.get(self.formation_name, {}))
        
        return dyno_settings

    @cached_property
    def downscale_on_non_empty_queue(self):
        return self.settings.get("downscale_on_non_empty_queue", True)
    
    @cached_property
    def max_dyno_size(self):
        return self.settings.get("max_dyno_size", "performance-2xl")

    @cached_property
    def threads_available(self):
        return self.settings.get("threads_available", 1)

    @property
    def threads_used(self):
        return self.get_threads_used()

    @cached_property
    def price_per_hour(self):
        return self.settings.get("price_per_hour", 0.035)

    @cached_property
    def max_price_per_month(self):
        return self.settings.get("max_price_per_month", 25.00)

    @property
    def available_memory(self):
        return DYNO_SIZES.get(self.formation_size, {}).get("memory", 512)

    @property
    def current_memory_usage(self):
        return self.exact_memory_usage

    @property
    def remote_monitoring(self):
        return self.dyno_name != os.environ.get('DYNO', None)

    @property
    def exact_memory_usage(self):
        exact_memory = self.get_memory_usage_from_logs()
        # Import here to avoid circular import
        try:
            from utils.process import get_total_memory_usage
            return get_total_memory_usage() if not exact_memory else exact_memory
        except ImportError:
            return exact_memory or 0

    @property
    def current_memory_usage_percentage(self):
        cur_mem = self.current_memory_usage
        if cur_mem and self.available_memory:
            return round(cur_mem / self.available_memory * 100, 2)
        return 0.0  # Return 0 if memory data is not available

    @property
    def is_on_original_formation_size_or_lower(self):
        original = self.original_formation_size
        if not original or original not in DYNO_SIZES or self.formation_size not in DYNO_SIZES:
            return False
        return DYNO_SIZES[self.formation_size]["memory"] <= DYNO_SIZES[original]["memory"]

    @property
    def is_memory_usage_high(self):
        return self.current_memory_usage > settings.HIGH_MEM_USE_MB

    @property
    def is_out_of_memory(self):
        return self.current_memory_usage > self.available_memory

    @property
    def requires_upscale(self):
        return bool(
            self.current_memory_usage_percentage > settings.UPSCALE_PERCENTAGE_HIGH_MEM_USE
            or self.detected_r15
        )

    @property
    def allow_downscale(self):
        queue_ok = self.downscale_on_non_empty_queue or self.no_tasks_in_queue

        # Normal path: memory is below the downscale threshold for all dynos.
        if not self.requires_upscale and \
                not self.is_still_high_memory_usage_for_downscale and \
                not self.any_sibling_still_high_memory and \
                queue_ok:
            return True

        # Stability override: RSS plateau + idle formation = work is done,
        # only a restart will reclaim memory.
        if self._is_stability_eligible and queue_ok:
            logger.info(
                f"[{self.formation_name}] Memory stabilized at "
                f"{self.current_memory_usage:.0f}MB and load settled — allowing downscale "
                f"(RSS plateau detected; restart will reclaim memory)."
            )
            return True

        return False
            
    @property
    def allow_downscale_on_shutdown(self):
        """
        Allow downscale on shutdown if neither this dyno nor any sibling is in a
        high-memory state.  Siblings must be checked because a graceful shutdown
        removes this dyno's memory key before the formation resize, which would
        otherwise leave hot siblings vulnerable to an unexpected downscale.
        """
        if not self.requires_upscale and \
                not self.is_still_high_memory_usage_for_downscale and \
                not self.any_sibling_still_high_memory:
            return True

        # Stability override.
        if self._is_stability_eligible:
            return True

        return False

    @property
    def detected_r15(self):
        return bool(self.get_r15_from_logs())

    @property
    def detected_r14(self):
        return bool(self.get_r14_from_logs())

    @property
    def avg_load_1min(self):
        return self.get_load_1min_avg()

    @property
    def _downscale_memory_threshold(self):
        """Memory (MB) above which a dyno is considered too hot to downscale.

        Uses the *original* (target) formation size so that the guard reflects
        the quota the formation will actually run on after downscaling.  Using
        the intermediate ``previous_formation_size`` produced a threshold that
        was too high (e.g. Standard-2X = 1075 MB instead of Standard-1X = 537 MB
        when going from Performance-M → Standard-1X), allowing a downscale while
        memory was still 1034 MB and causing immediate R14 errors.
        """
        target_size = self.original_formation_size or self.previous_formation_size
        if not target_size:
            logger.warning(
                f"[{self.formation_name}] Cannot compute downscale threshold: "
                f"no original or previous formation size known. Blocking downscale."
            )
            return float('inf')
        target_memory = DYNO_SIZES.get(target_size, {}).get("memory", 0)
        if not target_memory:
            logger.warning(
                f"[{self.formation_name}] Target size {target_size} has 0 memory. "
                f"Blocking downscale."
            )
            return float('inf')
        return target_memory * settings.DOWNSCALE_PERCENTAGE_HIGH_MEM_USE / 100

    @property
    def is_still_high_memory_usage_for_downscale(self):
        threshold = self._downscale_memory_threshold
        return math.isinf(threshold) or self.current_memory_usage >= threshold

    @property
    def _is_stability_eligible(self):
        """True when the formation qualifies for stability-based forced downscale.

        Combines all preconditions that must hold before the RSS-plateau override
        can bypass the normal memory-threshold check:
        - Not actively under memory pressure (no R15, memory % < upscale threshold)
        - Actually upscaled above the original tier
        - Memory has plateaued (±tolerance over the stability window)
        - All dynos in the formation are idle (load avg < threshold)
        - No sibling is still actively hot (unless also stabilized)
        """
        return (
            not self.requires_upscale
            and not self.is_on_original_formation_size_or_lower
            and self.is_memory_stabilized
            and self.is_formation_idle
            and not self.any_sibling_still_high_memory
        )

    def record_memory_reading(self, mem=None):
        """Record current memory usage for RSS plateau detection."""
        if mem is None:
            mem = self.current_memory_usage
        if not mem:
            return
        now = time.time()
        window = getattr(settings, 'DYNO_MEMORY_STABILITY_WINDOW', 300)
        history = cache.get(self.memory_history_cache_key) or []
        history = [(t, m) for t, m in history if now - t <= window]
        history.append((now, mem))
        cache.set(self.memory_history_cache_key, history, timeout=window + 120)

    @property
    def is_memory_stabilized(self):
        """True when memory has flattened out (±tolerance over the stability window).

        Python's allocator holds RSS without releasing it back to the OS.  Once
        heavy tasks finish, memory plateaus.  Detecting this plateau lets the
        autoscaler force a downscale + restart to reclaim memory.

        Tolerance is the greater of a fixed floor (``DYNO_MEMORY_STABILITY_TOLERANCE_MB``,
        default 50 MB) and a percentage of the average reading
        (``DYNO_MEMORY_STABILITY_TOLERANCE_PCT``, default 1%).  This scales
        gracefully: at 15 GB RSS the effective tolerance is ~150 MB (normal GC
        jitter won't prevent detection), while at 500 MB it stays at 50 MB.

        Readings must also span at least half the stability window to prevent
        a burst of readings in a few seconds from being mistaken for a plateau.
        """
        history = cache.get(self.memory_history_cache_key) or []
        min_readings = getattr(settings, 'DYNO_MEMORY_STABILITY_MIN_READINGS', 6)
        if len(history) < min_readings:
            return False
        # Ensure readings span a meaningful period, not just a short burst
        window = getattr(settings, 'DYNO_MEMORY_STABILITY_WINDOW', 300)
        time_span = history[-1][0] - history[0][0]
        if time_span < window / 2:
            return False
        readings = [m for _, m in history]
        avg = sum(readings) / len(readings)
        tolerance_mb = getattr(settings, 'DYNO_MEMORY_STABILITY_TOLERANCE_MB', 50)
        tolerance_pct = getattr(settings, 'DYNO_MEMORY_STABILITY_TOLERANCE_PCT', 1)
        tolerance = max(tolerance_mb, avg * tolerance_pct / 100)
        return all(abs(m - avg) <= tolerance for m in readings)

    def _acquire_rate_limit_token(self):
        """
        Acquire a token from the shared Redis-based rate limiter.
        Coordinates across all dynos to stay within Heroku's 4,500 calls/hour limit.
        Blocks (with back-off) if the per-minute budget is exhausted; returns False
        after 3 consecutive waits.
        """
        rate_limit = int(getattr(settings, 'HEROKU_API_RATE_LIMIT_PER_MINUTE', 50))
        cache_key = f'heroku:api_rate:{self.app_name}'

        current = cache.get(cache_key)
        if current is None:
            cache.set(cache_key, 1, timeout=60)
            return True
        if current < rate_limit:
            try:
                new_count = cache.incr(cache_key)
            except ValueError:
                # Key expired between get and incr
                cache.set(cache_key, 1, timeout=60)
                return True
            if new_count > rate_limit:
                # Lost the TOCTOU race — another dyno incremented past the limit
                logger.warning(
                    f"[{self.formation_name}] Heroku API rate limit exceeded "
                    f"({new_count}/{rate_limit}/min). Skipping cycle."
                )
                return False
            return True

        # Budget exhausted — skip this cycle instead of blocking the autoscale thread
        logger.warning(
            f"[{self.formation_name}] Heroku API rate limit reached "
            f"({current}/{rate_limit}/min). Skipping API call this cycle."
        )
        return False

    def call_heroku_api(self, method, url, custom_headers=None, data=None):
        """
        Central method for ALL Heroku Platform API calls.

        Features:
        - Shared Redis rate limiter (coordinates across all dynos)
        - Automatic 429 back-off using Retry-After header
        - Retry on transient network errors (SSL, connection, timeout)
        - GET response caching (5 s)
        """
        custom_headers = custom_headers or {}
        headers = {
            "Accept": "application/vnd.heroku+json; version=3",
            "Authorization": f"Bearer {self.heroku_api_key}"
        }
        headers.update(custom_headers)

        # Return cached GET responses before consuming a rate-limit token
        request_hash = None
        if method == "GET":
            request_hash = hash(f"{method}{url}")
            cached_response = cache.get(f'heroku:api_response:{request_hash}')
            if cached_response:
                return cached_response

        max_attempts = 5
        for attempt in range(1, max_attempts + 1):
            # ---- shared rate limit ----
            if not self._acquire_rate_limit_token():
                return None

            try:
                response = requests.request(method, url, headers=headers, json=data, timeout=30)
            except (SSLError, ConnectionError, Timeout) as exc:
                if attempt < max_attempts:
                    wait = min(4 * (2 ** (attempt - 1)), 30)
                    logger.warning(
                        f"[{self.formation_name}] Heroku API {method} {url} "
                        f"transient error (attempt {attempt}/{max_attempts}): {exc}. "
                        f"Retrying in {wait}s..."
                    )
                    time.sleep(wait)
                    continue
                logger.error(
                    f"[{self.formation_name}] Heroku API {method} {url} "
                    f"failed after {max_attempts} attempts: {exc}",
                    exc_info=True,
                )
                return None

            # ---- 429 back-off ----
            if response.status_code == 429:
                retry_after = int(response.headers.get('Retry-After', 30))
                backoff = min(retry_after + random.uniform(1, 5), 120)
                logger.warning(
                    f"[{self.formation_name}] Heroku API 429 on {method} {url}. "
                    f"Backing off {backoff:.0f}s (attempt {attempt}/{max_attempts})."
                )
                if attempt < max_attempts:
                    time.sleep(backoff)
                    continue
                return response

            # ---- cache successful GETs ----
            if method == "GET" and response.status_code == 200 and request_hash is not None:
                cache.set(f'heroku:api_response:{request_hash}', response, timeout=5)

            # ---- log unexpected errors ----
            if response.status_code >= 400:
                logger.error(
                    f"[{self.formation_name}] Heroku API {method} {url} "
                    f"returned {response.status_code}: {response.text}"
                )

            return response

        return None

    @property
    def formation_size(self):
        """
        Get current formation size from Redis cache (set by scale operations)
        or derive from DYNO_RAM env var.  Does NOT call the Heroku API,
        keeping API budget for operations that actually need it.
        """
        # 1. Instance-level cache (avoids Redis round-trip every access)
        now = time.time()
        cache_duration = getattr(settings, 'DYNO_AUTOSCALE_INTERVAL', 30) * 5
        if self._formation_size_cached:
            cached_size, cached_time = self._formation_size_cached
            if now - cached_time < cache_duration:
                return cached_size

        # 2. Redis cache (updated by _update_formation_size after scale ops)
        redis_key = f'heroku:formation_size:{self.app_name}:{self.formation_name}'
        cached_size = cache.get(redis_key)
        if cached_size and cached_size in DYNO_SIZES:
            self._formation_size_cached = (cached_size, now)
            return cached_size

        # 3. Derive from DYNO_RAM env var (set by Heroku at boot)
        dyno_memory = int(os.environ.get('DYNO_RAM', 512))
        for dyno, details in DYNO_SIZES.items():
            if details['memory'] == dyno_memory:
                self._formation_size_cached = (dyno, now)
                # Persist to Redis so other lookups are consistent
                cache.set(redis_key, dyno, timeout=None)
                return dyno

        default = 'standard-1x'
        self._formation_size_cached = (default, now)
        return default

    # cached_properties that depend on formation_size and must be cleared after a resize.
    _FORMATION_DEPENDENT_CACHE = (
        'settings', 'downscale_on_non_empty_queue', 'max_dyno_size',
        'threads_available', 'price_per_hour', 'max_price_per_month',
    )

    def _invalidate_formation_dependent_cache(self):
        for attr in self._FORMATION_DEPENDENT_CACHE:
            self.__dict__.pop(attr, None)

    def _update_formation_size(self, new_size):
        """
        Record the new formation size in Redis + instance cache after a
        successful scale operation.  Also invalidates dependent cached_property
        values so they are recomputed from the new size.
        """
        self._formation_size_cached = (new_size, time.time())
        redis_key = f'heroku:formation_size:{self.app_name}:{self.formation_name}'
        cache.set(redis_key, new_size, timeout=None)
        self._invalidate_formation_dependent_cache()

    @property
    def next_formation_size(self):
        if self.formation_size in DYNO_SIZES:
            return DYNO_SIZES.get(self.formation_size, {}).get("next")

    @cached_property
    def upscale_until_cache_key(self):
        return f'heroku:keep_upscaled_formation_until:{self.formation_name}'

    @cached_property
    def original_size_cache_key(self):
        return f'heroku:original_formation_size:{self.formation_name}'

    def set_original_formation_size(self, value=None):
        if not cache.get(self.original_size_cache_key) or value:
            size = value if isinstance(value, str) else self.formation_size
            cache.set(self.original_size_cache_key, {"size": size, "time": timezone.now()}, timeout=None)

    def clear_original_formation_size(self):
        cache.delete(self.original_size_cache_key)
        cache.delete(self.upscale_until_cache_key)

    @property
    def original_formation_size(self):
        original_size = cache.get(self.original_size_cache_key)
        return original_size.get("size") if original_size else None

    @property
    def original_formation_size_time(self):
        original_size = cache.get(self.original_size_cache_key)
        return original_size.get("time") if original_size else None

    @property
    def previous_formation_size(self):
        if self.formation_size in DYNO_SIZES:
            return DYNO_SIZES[self.formation_size].get("previous")

    @cached_property
    def upscaling_cache_key(self):
        return f'heroku:upscaling_formation:{self.formation_name}'

    @property
    def is_upscaling(self):
        return cache.get(self.upscaling_cache_key)

    def set_upscaling(self):
        # Extend TTL to cover the full scale cool-down plus worst-case API round-trip time
        # (up to 5 retries × 30 s backoff ≈ 150 s) so the flag never expires mid-call.
        ttl = settings.DYNO_TIME_BETWEEN_SCALES + 150
        cache.set(self.upscaling_cache_key, True, timeout=ttl)

    def clear_upscaling(self):
        cache.delete(self.upscaling_cache_key)

    @cached_property
    def memory_history_cache_key(self):
        return f'heroku:memory_history:{self.app_name}:{self.dyno_name}'

    @cached_property
    def downscale_cache_key(self):
        return f'heroku:downscale_formation:{self.formation_name}'

    @property
    def is_downscaling(self):
        return cache.get(self.downscale_cache_key)

    def set_downscaling(self):
        cache.set(self.downscale_cache_key, True, timeout=settings.DYNO_TIME_BETWEEN_SCALES)

    @cached_property
    def threads_used_cache_key(self):
        return f'heroku:threads_used:{self.app_name}:{self.dyno_name}'

    def get_threads_used(self):
        if self.remote_monitoring:
            return cache.get(self.threads_used_cache_key)

        return self.set_threads_used()

    def set_threads_used(self):
        threads_used = len(threading.enumerate())
        if threads_used:
            ttl = getattr(settings, 'DYNO_AUTOSCALE_INTERVAL', 30) * 2
            cache.set(self.threads_used_cache_key, threads_used, timeout=ttl)
        return threads_used

    def autoscale(self, continuous=True):
        """
        Autoscale the dyno based on current requirements.

        Args:
            continuous (bool): Whether to run autoscaling continuously.
        """

        # If continuous mode, return stats
        if continuous:
            logger.debug(f"Dyno {self.dyno_name} stats: "
                         f"Formation Size: {self.formation_size}, "
                         f"Memory Usage: {self.current_memory_usage} / {self.available_memory} MB, "
                         f"Load Avg (1min): {self.avg_load_1min}, "
                         f"Tasks in Queue: {self.tasks_in_queue}, "
                         f"Threads Used: {self.threads_used}, "
                         f"Detected R14: {self.detected_r14}, "
                         f"Detected R15: {self.detected_r15}, "
                    )

        # ignore beatworker if settings.DYNO_AUTOSCALE_ENABLED_FOR_BEATWORKER is False
        if 'beatworker' == self.formation_name and not getattr(settings, 'DYNO_AUTOSCALE_ENABLED_FOR_BEATWORKER', True):
            return

        try:
            if getattr(settings, 'DYNO_LOG_THREADS_USED', False):
                self.set_threads_used()

            if self.requires_upscale or self.any_sibling_requires_upscale:
                self.upscale_formation_to_next_level()
            else:
                self.check_and_downscale_to_original_formation_size()
        except Exception as e:
            logger.error(f"Failed to autoscale dyno {self.dyno_name}. Error: {e}", exc_info=True)

    def _check_formation_on_startup(self):
        """
        Safety check on startup: detect if formation is stuck in an upscaled state
        (e.g., due to Redis key loss or missed downscale) and restore the downscale timer.
        Also ensures original_formation_size is always recorded as a baseline.
        """
        try:
            current_size = self.formation_size
            original_size = self.original_formation_size
            upscale_until = cache.get(self.upscale_until_cache_key)

            if original_size and original_size in DYNO_SIZES and current_size in DYNO_SIZES:
                current_memory = DYNO_SIZES[current_size]["memory"]
                original_memory = DYNO_SIZES[original_size]["memory"]

                if current_memory > original_memory and not upscale_until:
                    # Formation is upscaled but there's no upscale_until key —
                    # the downscale timer was lost. Restore it so downscale can proceed.
                    logger.warning(
                        f"Startup safety check: {self.formation_name} is on {current_size} "
                        f"but original size is {original_size} with no upscale_until key. "
                        f"Restoring downscale timer."
                    )
                    delta = getattr(settings, 'DYNO_DOWNSCALE_CHECK_INTERVAL', 300) + \
                            getattr(settings, 'DYNO_AUTOSCALE_INTERVAL', 30)
                    until = timezone.now() + timedelta(seconds=delta)
                    cache.set(self.upscale_until_cache_key, until, timeout=delta)

            # Always set original formation size on startup if not already set
            if not original_size:
                previous = self.previous_formation_size
                if previous and previous in DYNO_SIZES:
                    # Formation is above the lowest tier with no recorded original —
                    # assume it was upscaled and the original was lost (e.g. Redis
                    # eviction, phantom clear).  Record the previous tier so the
                    # downscale path can recover instead of staying stuck.
                    self.set_original_formation_size(value=previous)
                    logger.warning(
                        f"Startup safety check: {self.formation_name} is on {current_size} "
                        f"with no original size recorded. Assuming original was {previous} "
                        f"and restoring downscale timer."
                    )
                    delta = getattr(settings, 'DYNO_DOWNSCALE_CHECK_INTERVAL', 300) + \
                            getattr(settings, 'DYNO_AUTOSCALE_INTERVAL', 30)
                    until = timezone.now() + timedelta(seconds=delta)
                    cache.set(self.upscale_until_cache_key, until, timeout=delta)
                else:
                    self.set_original_formation_size()
                    logger.info(
                        f"Recorded baseline formation size for {self.formation_name}: {current_size}"
                    )
        except Exception as e:
            logger.error(f"Startup formation check failed: {e}", exc_info=True)

    def _run_continuous(self):
        # On startup, check for stuck upscaled state and record baseline
        self._check_formation_on_startup()

        while not self._stop_autoscale_event.is_set():
            self.check_in_dyno()
            self.check_for_sibling_zombie_dynos()
            self.autoscale(continuous=True)

            # Jitter ±20 % to prevent thundering-herd across dynos
            base = settings.DYNO_AUTOSCALE_INTERVAL
            jitter = base * random.uniform(-0.2, 0.2)
            time.sleep(base + jitter)

    def _run_continuous_file_cleaning(self):
        while not self._stop_file_cleaning_event.is_set():
            self.check_and_clean_old_files()
            time.sleep(settings.DYNO_AUTOSCALE_INTERVAL)

    def check_and_clean_old_files(self):
        """
        Check if it's time to clean old files and perform the cleaning if necessary.
        """
        # Get the file cleaning interval from settings or use default (1 hour)
        file_cleaning_interval = int(getattr(settings, 'DYNO_FILE_CLEANING_INTERVAL', 60 * 60))

        # Get the file age threshold from settings or use default (48 hours)
        file_age_hours = getattr(settings, 'DYNO_FILE_AGE_THRESHOLD', 48)

        # Get the directory to clean from settings or use default (/tmp)
        directory = getattr(settings, 'DYNO_FILE_CLEANING_DIRECTORY', '/tmp')

        # Check if file cleaning is enabled
        if not getattr(settings, 'DYNO_FILE_CLEANING_ENABLED', False):
            return

        # Check if it's time to clean files
        now = timezone.now()
        if (self._last_file_cleaning is None or 
            (now - self._last_file_cleaning).total_seconds() >= file_cleaning_interval):

            logger.debug(f"Starting to clean files in {directory} older than {file_age_hours} hours on dyno {self.dyno_name}...")

            # Clean old files
            success = self.clean_old_files(directory=directory, hours=file_age_hours)

            if success:
                # Update the last file cleaning timestamp
                self._last_file_cleaning = now
                logger.debug(f"Successfully cleaned old files in {directory} on dyno {self.dyno_name}. Next cleaning in {file_cleaning_interval/3600:.1f} hours.")
            else:
                # If cleaning failed, try again after a shorter interval
                self._last_file_cleaning = now - timedelta(seconds=file_cleaning_interval * 0.9)
                logger.warning(f"Failed to clean old files in {directory} on dyno {self.dyno_name}. Will try again soon.")

    def start_continuous_autoscale(self):
        """
        Start continuous autoscaling on a separate thread.
        """
        with self._thread_lock:
            if self._autoscale_thread and self._autoscale_thread.is_alive():
                logger.warning(f"Autoscaling thread for {self.dyno_name} is already running.")
                return

            self._stop_autoscale_event.clear()
            self._autoscale_thread = threading.Thread(target=self._supervised_run, args=(), daemon=True)
            self._autoscale_thread.start()
            logger.debug(f"Continuous autoscaling thread started for {self.dyno_name} with interval {settings.DYNO_AUTOSCALE_INTERVAL} seconds.")
            return self._autoscale_thread

    def start_continuous_file_cleaning(self):
        """
        Start continuous file cleaning on a separate thread.
        """
        with self._thread_lock:
            if self._file_cleaning_thread and self._file_cleaning_thread.is_alive():
                logger.warning(f"File cleaning thread for {self.dyno_name} is already running.")
                return

            self._stop_file_cleaning_event.clear()
            self._file_cleaning_thread = threading.Thread(target=self._supervised_run_file_cleaning, args=(), daemon=True)
            self._file_cleaning_thread.start()
            logger.debug(f"Continuous file cleaning thread started for {self.dyno_name} with interval {settings.DYNO_AUTOSCALE_INTERVAL} seconds.")
            return self._file_cleaning_thread

    def stop_continuous_autoscale(self):
        """Stop the continuous autoscaling thread."""
        with self._thread_lock:
            self.remove_dyno_from_alive_cache()

            if not self._autoscale_thread or not self._autoscale_thread.is_alive():
                return

            self._stop_autoscale_event.set()

            # Ensure we are not calling join on the current thread
            if threading.current_thread() != self._autoscale_thread:
                self._autoscale_thread.join()

            self._autoscale_thread = None
            logger.debug(f"Stopped continuous autoscaling for {self.dyno_name}.")

    def stop_continuous_file_cleaning(self):
        """Stop the continuous file cleaning thread."""
        with self._thread_lock:
            if not self._file_cleaning_thread or not self._file_cleaning_thread.is_alive():
                return

            self._stop_file_cleaning_event.set()

            # Ensure we are not calling join on the current thread
            if threading.current_thread() != self._file_cleaning_thread:
                self._file_cleaning_thread.join()

            self._file_cleaning_thread = None
            logger.debug(f"Stopped continuous file cleaning for {self.dyno_name}.")

    def _supervised_run(self):
        """Supervised loop to restart the thread if it exits."""
        while not self._stop_autoscale_event.is_set():
            try:
                self._run_continuous()
            except Exception as e:
                logger.error(f"Autoscaler thread for {self.dyno_name} crashed. Restarting... Error: {e}", exc_info=True)
                time.sleep(15)  # Short delay before restarting

    def _supervised_run_file_cleaning(self):
        """Supervised loop to restart the file cleaning thread if it exits."""
        while not self._stop_file_cleaning_event.is_set():
            try:
                self._run_continuous_file_cleaning()
            except Exception as e:
                logger.error(f"File cleaning thread for {self.dyno_name} crashed. Restarting... Error: {e}", exc_info=True)
                time.sleep(15)  # Short delay before restarting

    # Registers dyno as alive in redis cache table so other workers can check if it's alive and restart it if it doesn't respond for a while
    def check_in_dyno(self):
        now = timezone.now()
        # Use DYNO_ZOMBIE_THRESHOLD as the TTL so crashed dynos auto-expire before
        # the zombie detector fires — prevents stale memory keys from blocking downscale.
        ttl = int(getattr(settings, 'DYNO_ZOMBIE_THRESHOLD', 24 * 60 * 60))
        cache.set(f'heroku:dyno_alive:{self.dyno_name}', now, timeout=ttl)
        _update_dyno_registry(self.app_name, self.dyno_name, now.timestamp())

        # Publish per-dyno metrics so siblings can gate formation-wide decisions.
        mem = self.current_memory_usage
        if mem is not None:
            cache.set(f'heroku:dyno_memory:{self.dyno_name}', mem, timeout=ttl)
            self.record_memory_reading(mem)
            cache.set(
                f'heroku:dyno_memory_stable:{self.dyno_name}',
                self.is_memory_stabilized, timeout=ttl,
            )
        load = self.avg_load_1min
        if load is not None:
            cache.set(f'heroku:dyno_load:{self.dyno_name}', load, timeout=ttl)

    def remove_dyno_from_alive_cache(self, dyno_name=None):
        dyno_name = dyno_name or self.dyno_name
        for prefix in self._DYNO_CACHE_PREFIXES:
            cache.delete(f'heroku:dyno_{prefix}:{dyno_name}')
        _update_dyno_registry(self.app_name, dyno_name)

    def _iter_sibling_values(self, metric):
        """Yield ``(dyno_name, value)`` for each sibling's published metric.

        ``metric`` is the cache key infix, e.g. ``'memory'`` or ``'load'``.
        Only siblings of the same formation are returned (own dyno is skipped).
        """
        own_key = f'heroku:dyno_{metric}:{self.dyno_name}'
        for key in cache.keys(f'heroku:dyno_{metric}:{self.formation_name}.*'):
            if key == own_key:
                continue
            value = cache.get(key)
            if value is not None:
                yield key.split(':')[-1], value

    @property
    def any_sibling_still_high_memory(self):
        """
        Return True if any *other* dyno of the same formation has memory at or above
        the downscale threshold.  Prevents a low-memory dyno from downscaling the
        whole formation while a sibling is still running hot.
        """
        threshold = self._downscale_memory_threshold
        if not threshold:
            return False
        for sibling_name, sibling_mem in self._iter_sibling_values('memory'):
            if sibling_mem >= threshold:
                # If the sibling's memory has also stabilized (RSS plateau),
                # it won't drop further — only a restart will reclaim it.
                # Don't block formation downscale in that case.
                sibling_stable = cache.get(f'heroku:dyno_memory_stable:{sibling_name}')
                if sibling_stable:
                    logger.debug(
                        f"Sibling {sibling_name} at {sibling_mem:.0f}MB >= "
                        f"{threshold:.0f}MB but memory stabilized; not blocking downscale."
                    )
                    continue
                logger.debug(
                    f"Sibling {sibling_name} at {sibling_mem:.0f}MB >= "
                    f"{threshold:.0f}MB threshold; blocking {self.formation_name} downscale."
                )
                return True
        return False

    @property
    def any_sibling_requires_upscale(self):
        """Return True if any sibling's memory exceeds the upscale threshold
        for the current tier.  Allows a cool dyno to trigger chain upscale
        on behalf of a hot sibling whose autoscale thread may have stalled."""
        upscale_pct = getattr(settings, 'UPSCALE_PERCENTAGE_HIGH_MEM_USE', 80)
        threshold_mb = self.available_memory * upscale_pct / 100
        for sibling_name, sibling_mem in self._iter_sibling_values('memory'):
            if sibling_mem > threshold_mb:
                logger.info(
                    f"Sibling {sibling_name} at {sibling_mem:.0f}MB > "
                    f"{threshold_mb:.0f}MB upscale threshold; advocating upscale "
                    f"for {self.formation_name}."
                )
                return True
        return False

    @property
    def is_formation_idle(self):
        """True when *all* dynos in the formation (self + siblings) have a load
        average below ``DYNO_STABILITY_LOAD_THRESHOLD`` (default 1.0).

        A high load average indicates a CPU-bound task is still executing.  Even
        if RSS has plateaued, downscaling during active work would kill that task
        (formation resize restarts all dynos).  Gate the stability-based
        downscale on this check to let heavy tasks finish first.
        """
        threshold = getattr(settings, 'DYNO_STABILITY_LOAD_THRESHOLD', 1.0)
        # Check own load
        own_load = self.avg_load_1min
        if own_load is not None and own_load >= threshold:
            logger.debug(
                f"[{self.formation_name}] Own load {own_load:.2f} >= "
                f"{threshold:.1f}; formation not idle."
            )
            return False
        # Check sibling loads
        for sibling_name, sibling_load in self._iter_sibling_values('load'):
            if sibling_load >= threshold:
                logger.debug(
                    f"[{self.formation_name}] Sibling {sibling_name} load "
                    f"{sibling_load:.2f} >= {threshold:.1f}; formation not idle."
                )
                return False
        return True

    def check_for_sibling_zombie_dynos(self):
        """
        Check if any sibling dynos are marked as alive in the cache and restart them if they
        have not checked in within the DYNO_ZOMBIE_THRESHOLD seconds.
        """
        # Make sure only one dyno is checking for zombie dynos at a time
        zombie_threshold = getattr(settings, 'DYNO_ZOMBIE_THRESHOLD', 24 * 60 * 60)
        try:
            with cache.lock('heroku:lock:dyno_alive_check', expire=30):
                siblings = [dyno for dyno in cache.keys('heroku:dyno_alive:*')]
                for sibling in siblings:
                    last_checkin = cache.get(sibling)
                    if last_checkin:
                        last_checkin_seconds_ago = (timezone.now() - last_checkin).total_seconds()
                        if last_checkin_seconds_ago > zombie_threshold:
                            last_checkin_minutes_ago = last_checkin_seconds_ago // 60
                            dyno_name = sibling.split(':')[-1]
                            logger.error(f"Zombie dyno detected: {dyno_name}. Last check-in: {last_checkin_minutes_ago:.0f} minutes ago. Restarting...")
                            self.restart_zombie_dyno(dyno_name)
        except _LockExpiryErrors:
            logger.warning("Lock 'heroku:lock:dyno_alive_check' expired before release.")

    def restart_zombie_dyno(self, dyno_name):
        self.restart_dyno(dyno_name)


    def upscale_formation_to_next_level(self):
        '''
        Upscale the dyno to the next level in the hierarchy for a period of X hours
        '''
        # Check if not already at max_dyno_size
        if self.formation_size == self.max_dyno_size:
            logger.debug(f"Formation {self.formation_name} is already at max size {self.max_dyno_size}.")
            return

        # Once a formation has already been upscaled, keep it at the current size
        # until the downscale window is reached instead of chaining another upscale
        # just because R15 is still present after the first resize.
        # However, if memory is genuinely above the upscale threshold on the
        # CURRENT tier (not stale R15 from the pre-upscale tier), allow chaining
        # to the next tier — otherwise the formation gets stuck at an intermediate
        # size while workers hit R14/R15.
        if cache.get(self.upscale_until_cache_key):
            upscale_threshold = getattr(settings, 'UPSCALE_PERCENTAGE_HIGH_MEM_USE', 80)
            self_hot = self.current_memory_usage_percentage > upscale_threshold
            sibling_hot = self.any_sibling_requires_upscale
            if not self_hot and not sibling_hot:
                logger.debug(
                    f"Formation {self.formation_name} is already within the keep-upscaled window; "
                    f"skipping repeat upscale."
                )
                return
            logger.warning(
                f"Formation {self.formation_name} is within the keep-upscaled window but "
                f"{'self' if self_hot else 'sibling'} memory is still high "
                f"(self={self.current_memory_usage_percentage:.1f}%) — "
                f"allowing chain upscale to next tier."
            )

        # Ensure upscale is only executed once every settings.DYNO_TIME_BETWEEN_SCALES seconds for this dyno type
        _lock_proceed = True
        try:
            with cache.lock(self.upscaling_cache_key, expire=30):
                if self.is_upscaling:
                    logger.debug(f"Upscaling formation {self.formation_name} is already in progress.")
                    _lock_proceed = False
                else:
                    self.set_upscaling()
        except _LockExpiryErrors:
            logger.warning(f"Lock '{self.upscaling_cache_key}' expired before release.")
        if not _lock_proceed:
            return

        if not self.remote_monitoring:
            logger.warning(f"Memory usage is greater than {self.current_memory_usage_percentage:.2f}% of available RAM ({self.available_memory}MB). "
                            f"Current memory usage: {self.current_memory_usage:.2f}MB. Triggered by dyno {self.dyno_name}")

        next_level = self.next_formation_size
        if not next_level:
            self.clear_upscaling()
            logger.debug("Formation is already at the highest level or unrecognized size.")
            return self.formation_size

        # Store original dyno size in Redis cache
        self.set_original_formation_size()

        # Update dyno to upscale (routed through call_heroku_api for rate limiting)
        url = f'https://api.heroku.com/apps/{self.app_name}/formation/{self.formation_name}'
        response = self.call_heroku_api("PATCH", url, data={"size": next_level})
        if response and response.status_code == 200:
            logger.info(f"Upscaled formation {self.formation_name} from {self.formation_size} to {next_level} with {DYNO_SIZES[next_level]['memory']} MB memory.")
            self._update_formation_size(next_level)
            cache.delete(self.memory_history_cache_key)

            # Set cache key that expires after min upscale duration to gate downscale checks
            delta = (
                getattr(settings, 'DYNO_DOWNSCALE_CHECK_INTERVAL', 300) +
                getattr(settings, 'DYNO_MIN_UPSCALE_DURATION', 300) +
                getattr(settings, 'DYNO_AUTOSCALE_INTERVAL', 30)
            )
            until = timezone.now() + timedelta(seconds=delta)
            cache.set(self.upscale_until_cache_key, until, timeout=delta)

            if not self.remote_monitoring:
                self.stop_continuous_autoscale()
        elif response:
            self.clear_upscaling()
            logger.error(f"Failed to upscale formation {self.formation_name}. Response: {response.status_code} - {response.text}")
        else:
            self.clear_upscaling()
            logger.error(f"Failed to upscale formation {self.formation_name}. API call was rate-limited or failed.")

    def check_and_downscale_to_original_formation_size(self):
        """
        Checks if formation should be downscaled by checking memory usage and downscale if necessary
        """
        # Check if formation is upscaled and if it should be downscaled
        check_interval = getattr(settings, 'DYNO_DOWNSCALE_CHECK_INTERVAL', 300)
        upscaled_until = cache.get(self.upscale_until_cache_key)
        if upscaled_until:
            # If the formation is already back at (or below) its original size,
            # any upscale_until key is a phantom artifact — e.g. from a sibling
            # that downscaled in the gap between an upscale API call and its
            # cache.set().  Clear it so the autoscaler can resume normal
            # R14/R15 detection instead of looping in the "Extending..." path.
            if self.is_on_original_formation_size_or_lower:
                logger.info(
                    f"Cleared phantom upscale state for {self.formation_name}: "
                    f"formation is already at original size {self.formation_size}."
                )
                self.clear_original_formation_size()
                # Fall through to the R14 restart check below
            else:
                current_ttl = cache.ttl(self.upscale_until_cache_key)
                # Only allow downscale once we are within the final check_interval window.
                # Before that, keep the formation upscaled regardless of current memory —
                # this prevents an immediate downscale after an R15 restart clears memory.
                if current_ttl < check_interval:
                    if self.allow_downscale:
                        self.downscale_formation_to_original_size()
                        return
                    # Near expiry and not safe to downscale.
                    # If stability-eligible, downscale directly — a restart alone
                    # won't free RSS; the formation resize triggers a fresh
                    # restart on the smaller tier.
                    if self._is_stability_eligible:
                        logger.info(
                            f"[{self.formation_name}] Memory stabilized at "
                            f"{self.current_memory_usage:.0f}MB near timer expiry — "
                            f"forcing downscale (restart alone won't reclaim RSS)."
                        )
                        self.downscale_formation_to_original_size()
                        return
                    # Otherwise restart if hot, or extend the timer
                    if ((self.detected_r14 and not self.detected_r15) or self.is_still_high_memory_usage_for_downscale) and self.no_tasks_in_queue:
                        self.restart_dyno()
                    else:
                        load_avg = f'{self.avg_load_1min:.2f}' if self.avg_load_1min else 'unknown'
                        memory_usage = f'{self.current_memory_usage_percentage}% ({self.current_memory_usage:.2f}MB / {self.available_memory}MB)' \
                            if self.current_memory_usage and self.available_memory and self.current_memory_usage_percentage else 'unknown'
                        stable = self.is_memory_stabilized
                        history = cache.get(self.memory_history_cache_key) or []
                        logger.warning(f"Extending the time for {self.formation_name} to stay upscaled by {check_interval} seconds. "
                                        f"Current Memory Usage: {memory_usage}. Current Load Avg: {load_avg}. "
                                        f"Tasks in Queue: {self.tasks_in_queue}. "
                                        f"Memory Stabilized: {stable} ({len(history)} readings).")
                        new_until = upscaled_until + timedelta(seconds=check_interval)
                        new_timeout = current_ttl + check_interval
                        cache.set(self.upscale_until_cache_key, new_until, timeout=new_timeout)
                return

        # if on original formation size and memory usage is high, restart the dyno
        if self.current_memory_usage_percentage > getattr(settings, 'DOWNSCALE_PERCENTAGE_HIGH_MEM_USE', 105) and self.detected_r14 and not self.detected_r15 and self.no_tasks_in_queue:
            self.restart_dyno()
            return

        # Guard: the upscale_until key may have expired while memory is still
        # elevated (e.g. R14 storm, slow GC).  Unconditionally downscaling here
        # would move the formation back to a smaller size that cannot handle the
        # current memory load, causing immediate R14 errors.  Re-check allow_downscale
        # and restore the upscale timer if the formation is not yet safe to shrink.
        # Only do this when the formation is actually upscaled above its original
        # size — otherwise we'd create a phantom upscale_until key on a formation
        # that was already downscaled, trapping siblings in an infinite extend loop.
        # Also skip when at the lowest tier with no original recorded (post-downscale
        # clear) — there's nothing to downscale from.
        can_be_upscaled = (
            not self.is_on_original_formation_size_or_lower
            and not (self.previous_formation_size is None and not self.original_formation_size)
        )
        if can_be_upscaled and not self.allow_downscale:
            delta = getattr(settings, 'DYNO_DOWNSCALE_CHECK_INTERVAL', 300)
            new_until = timezone.now() + timedelta(seconds=delta)
            cache.set(self.upscale_until_cache_key, new_until, timeout=delta)
            logger.warning(
                f"upscale_until key expired for {self.formation_name} but allow_downscale "
                f"is False (memory still high or R14 active). Restoring upscale timer for "
                f"{delta}s. Memory: {self.current_memory_usage:.0f}MB."
            )
            return

        # Downscale formation to original size as there is no need to keep it upscaled
        self.downscale_formation_to_original_size()

    def downscale_formation_to_original_size(self):
        if self.previous_formation_size is None:
            return

        original_formation_size = self.original_formation_size

        # If original size is not set, fall back to previous tier so the
        # formation can still recover from a lost baseline (e.g. Redis eviction).
        if not original_formation_size:
            original_formation_size = self.previous_formation_size
            if not original_formation_size:
                return
            logger.info(
                f"No original formation size for {self.formation_name}, "
                f"using previous tier {original_formation_size} as downscale target."
            )

        _lock_proceed = True
        try:
            with cache.lock(self.downscale_cache_key, expire=30):
                # Check if formation is on lower size than original size and skip downscale
                if self.is_on_original_formation_size_or_lower:
                    logger.debug(f"Formation {self.formation_name} is already at original or lower size than the original size.")
                    self.clear_original_formation_size()
                    _lock_proceed = False
                elif self.is_downscaling:
                    # logger.info(f"Downscaling formation {self.formation_name} is already in progress.")
                    _lock_proceed = False
                else:
                    self.set_downscaling()
        except _LockExpiryErrors:
            logger.warning(f"Lock '{self.downscale_cache_key}' expired before release.")
        if not _lock_proceed:
            return

        # Scale formation back to original size (routed through call_heroku_api for rate limiting)
        url = f'https://api.heroku.com/apps/{self.app_name}/formation/{self.formation_name}'
        response = self.call_heroku_api("PATCH", url, data={"size": original_formation_size})
        if response and response.status_code == 200:
            logger.info(f"Downscaled formation {self.formation_name} back to {original_formation_size} with {DYNO_SIZES[original_formation_size]['memory']} MB memory.")
            self._update_formation_size(original_formation_size)
            self.clear_upscaling()
            self.clear_original_formation_size()
            cache.delete(self.memory_history_cache_key)
            if not self.remote_monitoring:
                self.stop_continuous_autoscale()
        elif response:
            logger.error(f"Failed to downscale formation {self.formation_name}. Response: {response.status_code} - {response.text}")
        else:
            logger.error(f"Failed to downscale formation {self.formation_name}. API call was rate-limited or failed.")

    def restart_dyno(self, dyno_name=None):
        if not self.app_name or (not self.dyno_name and not dyno_name):
            return

        dyno_name = dyno_name or self.dyno_name

        # Ensure restart is only executed once every DYNO_TIME_BETWEEN_RESTARTS seconds for this dyno
        restart_cache_key = f'heroku:restart_dyno:{dyno_name}'
        _lock_proceed = True
        try:
            with cache.lock(restart_cache_key, expire=30):
                if cache.get(restart_cache_key):
                    logger.debug(f"Restarting dyno {dyno_name} is already in progress.")
                    _lock_proceed = False
                else:
                    cache.set(restart_cache_key, True, timeout=getattr(settings, 'DYNO_TIME_BETWEEN_RESTARTS', 300))
        except _LockExpiryErrors:
            logger.warning(f"Lock '{restart_cache_key}' expired before release.")
        if not _lock_proceed:
            return False

        # Restart the dyno via Heroku API (routed through call_heroku_api for rate limiting)
        url = f'https://api.heroku.com/apps/{self.app_name}/dynos/{dyno_name}'
        response = self.call_heroku_api("DELETE", url)
        if response and response.status_code == 202:
            logger.info(f"Restarting dyno {dyno_name}...")

            if dyno_name == self.dyno_name:
                self.stop_continuous_autoscale()
            else:
                self.remove_dyno_from_alive_cache(dyno_name)
            return True
        elif response:
            logger.error(f"Failed to restart dyno {dyno_name}. Response: {response.status_code} - {response.text}")
        else:
            logger.error(f"Failed to restart dyno {dyno_name}. API call was rate-limited or failed.")
        return False

    def increment_dyno_counter(self, dyno_name=None):
        """
        Increments a counter in cache for the specified dyno_name.
        If the counter exceeds the threshold (from settings), restarts the dyno.
        The TTL is set only during initialization and never updated.

        Args:
            dyno_name (str, optional): The name of the dyno. Defaults to self.dyno_name.

        Returns:
            int: The current counter value after increment
        """
        dyno_name = dyno_name or self.dyno_name
        if not dyno_name:
            return 0

        cache_key = f'heroku:dyno_counter:{dyno_name}'

        # Get the current counter value
        counter = cache.get(cache_key)

        # If counter doesn't exist, initialize it with TTL
        if counter is None:
            counter = 1
            # Set TTL from settings or default to 1 hour
            ttl = getattr(settings, 'DYNO_COUNTER_TTL', 60 * 60)  # Default to 1 hour
            cache.set(cache_key, counter, timeout=ttl)
            logger.debug(f"Initialized counter for dyno {dyno_name} with value 1 and TTL {ttl} seconds")
        else:
            # Increment the counter without changing the TTL
            counter = cache.incr(cache_key)
            logger.debug(f"Incremented counter for dyno {dyno_name} to {counter}")

        # Get the threshold from settings or default to 15
        threshold = getattr(settings, 'DYNO_RESTART_THRESHOLD', 15)

        # If counter exceeds threshold, restart the dyno
        if counter >= threshold:
            logger.warning(f"Counter for dyno {dyno_name} reached threshold {threshold}. Restarting dyno...")
            restarted = self.restart_dyno(dyno_name)
            if restarted:
                cache.delete(cache_key)
            return 0

        return counter

    def get_heroku_logs(self, source="heroku", date_from=None):
        if not self.heroku_api_key:
            return None

        cache_key = f'heroku:logs:{self.app_name}:{self.dyno_name}'
        logs = cache.get(cache_key)
        if not logs:
            try:
                with cache.lock(cache_key, expire=90):
                    logs = cache.get(cache_key)
                    if not logs:
                        # Step 1: Set up API request to retrieve logs
                        url = f"https://api.heroku.com/apps/{self.app_name}/log-sessions"
                        payload = {
                            "dyno": self.dyno_name,
                            "tail": False,
                            "source": source,
                            "lines": 200  # Adjusting this almost does nothing
                        }

                        # Step 2: Start a log session via call_heroku_api (rate-limited + retry)
                        response = self.call_heroku_api("POST", url, data=payload)
                        if not response:
                            logger.warning("Failed to retrieve log session (rate-limited or network error).")
                            return None
                        if response.status_code >= 400:
                            logger.warning(f"Failed to retrieve log session. Status code: {response.status_code} - {response.text}")
                            return None

                        log_url = response.json().get("logplex_url")
                        if not log_url:
                            logger.info("Log URL not found in the response.")
                            return None

                        try:
                            log_response = requests.get(log_url, timeout=30)
                        except (SSLError, ConnectionError, Timeout) as exc:
                            logger.warning(f"Failed to fetch logplex URL: {exc}")
                            return None
                        if log_response.status_code != 200:
                            logger.info(f"Failed to retrieve logs. Status code: {log_response.status_code} - {log_response.text}")
                            return None

                        logs = log_response.text or "\n" # Ensure logs is not empty to not overload API
                        log_cache_ttl = getattr(settings, 'DYNO_LOGS_CACHE_DURATION', 90)
                        cache.set(cache_key, logs, timeout=log_cache_ttl)
            except _LockExpiryErrors:
                logger.warning(f"Lock '{cache_key}' expired before release. Logs may already be cached.")

        if not logs:
            return None

        logs_parsed = []

        # Define the regular expression pattern to match the timestamp, dyno source, and message.
        # This pattern captures the timestamp, dyno name, and log message content.
        # 2024-11-14T12:39:00.845449+00:00 heroku[normal_worker.1]: source=normal_worker.1 dyno=heroku.36787764.0acfd07d-c858-4ce8-9143-9c6fe97d8ba0 sample#load_avg_1m=0.11 sample#load_avg_5m=0.18 sample#load_avg_15m=0.13
        pattern = re.compile(
            r'(?P<timestamp>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}\+\d{2}:\d{2})\s+heroku\[(?P<dyno_name>[\w\.]+)\]:\s+(?P<message>.+)'
        )

        for line in logs.split("\n"):
            match = pattern.match(line)
            if match:
                timestamp_str = match.group("timestamp")
                try:
                    timestamp = timezone.datetime.strptime(timestamp_str, "%Y-%m-%dT%H:%M:%S.%f%z")
                except ValueError as e:
                    logger.error(f"Timestamp parsing failed: {e}")
                    continue

                logs_parsed.append({
                    "timestamp": timestamp,
                    "dyno_name": match.group("dyno_name"),
                    "message": match.group("message")
                })

        return logs_parsed

    def extract_latest_metric(self, patterns, cache_key, timeout=None, result_type=float):
        """
        Generic helper to extract the latest occurrence of a specified metric from Heroku logs.
        Handles multiple patterns and ensures there is some value at all times.

        Parameters:
            patterns (list): A list of regular expression patterns to search for the metric.
            cache_key (str): The cache key to store the latest value.
            timeout (int): Cache timeout in seconds. Defaults to 24 hours.
            result_type (type): The desired return type for the metric value (e.g., float, str).

        Returns:
            float or str: The extracted metric value, converted to the specified result_type.
                        Returns None if no patterns are found.
        """
        if timeout is None:
            timeout = 24 * 60 * 60  # Default to 24 hours
            cache_refresh_interval = getattr(settings, 'DYNO_GENERAL_CACHE_DURATION', 300)
        else:
            cache_refresh_interval = timeout

        cache_key = f'{cache_key}:{self.app_name}:{self.dyno_name}'

        # Fetch from cache; ttl > 0 means the key exists and has time remaining
        cached_value = cache.get(cache_key)
        ttl = cache.ttl(cache_key)
        cache_is_fresh = ttl is not None and ttl > 0 and (timeout - ttl) < cache_refresh_interval

        if cache_is_fresh and cached_value is not None:
            return cached_value

        # Fetch and parse logs
        logs_parsed = self.get_heroku_logs()
        if not logs_parsed:
            return cached_value

        # Compile all patterns
        compiled_patterns = [re.compile(pattern) for pattern in patterns]
        latest_value = None

        # Search for matches in reverse order to find the most recent occurrence
        for log_entry in reversed(logs_parsed):
            message = log_entry.get("message", "")
            timestamp = log_entry.get("timestamp")
            seconds_ago = (timezone.now() - timestamp).total_seconds() if timestamp else None
            if seconds_ago is not None and seconds_ago > timeout:
                continue
            for pattern in compiled_patterns:
                match = pattern.search(message)
                if match:
                    extracted_value = match.group(1)
                    latest_value = result_type(extracted_value) if extracted_value.replace('.', '', 1).isdigit() else extracted_value
                    break
            if latest_value is not None:
                break

        if result_type == bool:
            # Explicit False when no recent match found — prevents cached True from latching forever
            latest_value = bool(latest_value)
            cache.set(cache_key, latest_value, timeout=timeout)
            return latest_value

        if latest_value is not None:
            cache.set(cache_key, latest_value, timeout=timeout)

        return latest_value if latest_value is not None else cached_value

    def get_memory_usage_from_logs(self):
        """
        Retrieves the most recent total memory usage of a specified dyno by parsing Heroku logs.

        Returns:
            float: The total memory usage in MB, or None if not found.
        """
        patterns = [
            r"sample#memory_total=(\d+\.\d+)MB",          # Pattern for memory_total in MB
            r"Process running mem=(\d+)M\(\d+\.\d+%\)"    # Pattern for memory in megabytes
        ]
        return self.extract_latest_metric(patterns, cache_key="heroku:memory_total", result_type=float)

    def get_total_available_memory_from_logs(self):
        """
        Retrieves the most recent total memory quota from Heroku logs.

        Returns:
            float: The total memory quota in MB, or None if not found.
        """
        patterns = [r"sample#memory_quota=(\d+\.\d+)MB"]
        return self.extract_latest_metric(patterns, cache_key="heroku:memory_quota", result_type=float)

    def get_load_1min_avg(self):
        """
        Retrieves the most recent 1-minute load average from Heroku logs.

        Returns:
            float: The 1-minute load average, or None if not found.
        """
        patterns = [r"sample#load_avg_1m=(\d+\.\d+)"]
        load_1m_value = self.extract_latest_metric(patterns, cache_key="heroku:load_avg_1m", result_type=float)
        return load_1m_value if load_1m_value is not None else 0.4  # Default to 0.4 if not found

    def get_r15_from_logs(self):
        """
        Retrieves the most recent R15 error from Heroku logs, indicating memory quota exceeded.

        Returns:
            bool: The R15 error message if found, or None if not found.
        """
        patterns = [r"Error R15 \((.*?)\)"]
        timeout = getattr(settings, 'DYNO_ERRORS_TIMEOUT_DURATION', 60)  # Default to 60 seconds
        return self.extract_latest_metric(patterns, cache_key="heroku:r15_error", timeout=timeout, result_type=bool)

    def get_r14_from_logs(self):
        """
        Retrieves the most recent R14 error from Heroku logs, indicating memory quota exceeded.

        Returns:
            bool: The R14 error message if found, or None if not found.
        """
        patterns = [r"Error R14 \((.*?)\)"]
        timeout = getattr(settings, 'DYNO_ERRORS_TIMEOUT_DURATION', 60)  # Default to 60 seconds
        return self.extract_latest_metric(patterns, cache_key="heroku:r14_error", timeout=timeout, result_type=bool)

    def exec_connect(self, dyno_name=None, command=None):
        """
        Execute a command on a dyno using the Heroku API.

        This method creates a new one-off dyno to run the command.

        Args:
            dyno_name (str, optional): The name of the target dyno. Defaults to self.dyno_name.
                Note: This is only used for logging purposes as the API creates a new dyno.
            command (str, optional): The command to execute on the dyno. If not provided, just connects to the dyno.

        Returns:
            object: A result object with stdout, stderr, and returncode attributes, or None if the operation failed.
        """
        dyno_name = dyno_name or self.dyno_name
        if not self.app_name or not dyno_name:
            logger.error("Cannot connect to dyno: app_name or dyno_name is not set.")
            return None

        if not self.heroku_api_key:
            logger.error("Cannot execute command: HEROKU_API_KEY is not set.")
            return None

        try:
            # Use the Heroku API to run the command on a one-off dyno
            url = f'https://api.heroku.com/apps/{self.app_name}/dynos'

            # Quote the command to prevent shell injection
            safe_command = shlex.quote(command) if command else None
            payload = {
                'command': f'bash -c {safe_command}' if safe_command else 'bash',
                'attach': True,
                'size': self.formation_size,
                'type': 'run'
            }

            logger.info(f"Executing command on a one-off dyno via Heroku API (target dyno: {dyno_name})...")
            response = self.call_heroku_api("POST", url, custom_headers={'Content-Type': 'application/json'}, data=payload)

            if response and response.status_code == 201:  # 201 Created
                dyno_data = response.json()
                logger.info(f"Command execution started on one-off dyno {dyno_data.get('name')}.")
                return _ApiResult(stdout=str(dyno_data), stderr="", returncode=0)
            elif response:
                logger.error(f"Failed to execute command via Heroku API. Response: {response.status_code} - {response.text}")
                return None
            else:
                logger.error(f"Failed to execute command via Heroku API. API call was rate-limited or failed.")
                return None
        except Exception as e:
            logger.error(f"Failed to execute command via Heroku API. Error: {e}", exc_info=True)
            return None


    def clean_old_files(self, dyno_name=None, directory="/tmp", hours=48):
        """
        Clean files in the specified directory that are older than the specified number of hours.

        Args:
            dyno_name (str, optional): The name of the dyno to connect to. Defaults to self.dyno_name.
            directory (str, optional): The directory to clean. Defaults to "/tmp".
            hours (int, optional): The age threshold in hours. Defaults to 48.

        Returns:
            bool: True if the operation was successful, False otherwise.
        """
        dyno_name = dyno_name or self.dyno_name
        if not self.app_name or not dyno_name:
            logger.error("Cannot clean old files: app_name or dyno_name is not set.")
            return False

        try:
            # Create the find command to delete files older than the specified hours
            # Exclude env.d and mask directories
            find_cmd = f"find {directory} -type f -mtime +{hours/24} -not -path '{directory}/env.d*' -not -path '{directory}/mask*' -delete"
            logger.info(f"Cleaning files in {directory} older than {hours} hours on dyno {dyno_name}, excluding env.d and mask...")

            # Execute the command on the dyno
            result = self.exec_connect(dyno_name, command=find_cmd)

            if not result:
                logger.error(f"Failed to connect to dyno {dyno_name}.")
                return False

            if result.returncode != 0:
                logger.error(f"Failed to clean old files on dyno {dyno_name}. Error: {result.stderr}")
                return False

            logger.info(f"Successfully cleaned old files in {directory} on dyno {dyno_name}.")
            return True
        except Exception as e:
            logger.error(f"Failed to clean old files on dyno {dyno_name}. Error: {e}", exc_info=True)
            return False
