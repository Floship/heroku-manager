import os
import re
import requests
import threading
import time
from requests.exceptions import SSLError, ConnectionError, Timeout
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from django.conf import settings
from django.core.cache import cache
from django.utils.functional import cached_property
from django.utils import timezone
from importlib import import_module

import logging

logger = logging.getLogger(__name__)


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
        for dyno, settings in DYNO_SIZES.items():
            if settings["memory"] == dyno_memory:
                formation_size = dyno
                return settings

        formation_size = 'standard-1x'
    return DYNO_SIZES.get(formation_size, {})

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
        self._stop_event = threading.Event()
        self._autoscale_thread = None
        self._thread_lock = threading.Lock()

    @cached_property
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
        settings = {}
        settings.update(get_dyno_settings(self.formation_size))
        settings.update(settings.WORKER_SETTINGS_MAP.get(self.formation_name, {}))
        return settings

    @cached_property
    def downscale_on_non_empty_queue(self):
        return self.settings.get("downscale_on_non_empty_queue", True)

    @cached_property
    def threads_avaiable(self):
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

    @cached_property
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
        # Determine lower or equal formation sizes
        lower_size_formations = [
            dyno for dyno, details in DYNO_SIZES.items()
            if details["memory"] <= DYNO_SIZES[self.original_formation_size]["memory"]
        ]

        # Check if current size is within the lower or equal list
        return self.formation_size in lower_size_formations

    @property
    def is_memory_usage_high(self):
        return self.current_memory_usage > settings.HIGH_MEM_USE_MB

    @property
    def is_out_of_memory(self):
        return self.current_memory_usage > self.available_memory

    @property
    def requires_upscale(self):
        return self.current_memory_usage_percentage > settings.UPSCALE_PERCENTAGE_HIGH_MEM_USE or self.detected_r15

    @property
    def allow_downscale(self):
        return not self.requires_upscale and \
            not self.is_still_high_memory_usage_for_downscale and \
            not self.detected_r14 and \
            (self.downscale_on_non_empty_queue or self.no_tasks_in_queue)

    @property
    def detected_r15(self):
        return self.get_r15_from_logs()

    @property
    def detected_r14(self):
        return self.get_r14_from_logs()

    @property
    def avg_load_1min(self):
        return self.get_load_1min_avg()

    @property
    def is_still_high_memory_usage_for_downscale(self):
        previous_formation_memory = DYNO_SIZES.get(self.previous_formation_size, {}).get("memory", 0)
        return self.current_memory_usage >= (previous_formation_memory * settings.DOWNSCALE_PERCENTAGE_HIGH_MEM_USE / 100)

    @retry(stop=stop_after_attempt(5), wait=wait_exponential(multiplier=1, min=4, max=10), retry=retry_if_exception_type((SSLError, ConnectionError, Timeout)))
    def call_heroku_api(self, method, url, custom_headers={}, data=None):
        headers = {
            "Accept": "application/vnd.heroku+json; version=3",
            "Authorization": f"Bearer {self.heroku_api_key}"
        }
        headers.update(custom_headers)

        if method == "GET":
            request_hash = hash(f"{method}{url}")
            response = cache.get(f'heroku:api_response:{request_hash}')
            if response:
                return response

        response = requests.request(method, url, headers=headers, json=data)

        if method == "GET" and response.status_code == 200:
            cache.set(f'heroku:api_response:{request_hash}', response, timeout=5)

        if response.status_code != 200:
            logger.error(f"Failed to call Heroku API. Response: {response.status_code} - {response.text}, URL: {url}")
        return response

    @cached_property
    def formation_size(self):
        default_formation_size = 'standard-1x'

        if not self.remote_monitoring and hasattr(settings, 'DYNO_RAM') and settings.DYNO_RAM:
            for dyno, details in DYNO_SIZES.items():
                if details["memory"] == settings.DYNO_RAM:
                    return dyno

        # Fallback to Heroku API if not set in settings

        if not self.heroku_api_key:
            logger.error("HEROKU_API_KEY is not set. Cannot perform operations.")
            return default_formation_size

        if not self.app_name or not self.formation_name:
            logger.error("DYNO or HEROKU_APP_NAME is not set. Cannot perform operations.")
            return default_formation_size

        url = f'https://api.heroku.com/apps/{self.app_name}/formation/{self.formation_name}'
        response = self.call_heroku_api("GET", url)

        if response.status_code != 200:
            logger.error(f"Failed to retrieve current dyno size from Heroku API. Response: {response.status_code} - {response.text}, URL: {url}")
            logger.info("Attempting to retrieve dyno size from logs...")

            available_memory = self.get_total_available_memory_from_logs()
            if available_memory:
                for dyno, details in DYNO_SIZES.items():
                    if details["memory"] == available_memory:
                        return dyno
            return default_formation_size

        return response.json().get('size').lower()

    @cached_property
    def next_formation_size(self):
        if self.formation_size in DYNO_SIZES:
            return DYNO_SIZES.get(self.formation_size, {}).get("next")

    @cached_property
    def upscale_until_cache_key(self):
        return f'heroku:keep_upscaled_formation_until:{self.formation_name}'

    @cached_property
    def original_size_cache_key(self):
        return f'heroku:original_formation_size:{self.formation_name}'

    def set_original_formation_size(self):
        if not cache.get(self.original_size_cache_key):
            cache.set(self.original_size_cache_key, {"size": self.formation_size, "time": timezone.now()}, timeout=None)

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

    @cached_property
    def previous_formation_size(self):
        if self.formation_size in DYNO_SIZES:
            return DYNO_SIZES[self.formation_size].get("previous")

    @cached_property
    def upscaling_cache_key(self):
        return f'heroku:upscaling_formation:{self.formation_name}'

    @property
    def is_upscaling(self):
        cache.get(self.upscaling_cache_key)

    def set_upscaling(self):
        cache.set(self.upscaling_cache_key, True, timeout=settings.DYNO_TIME_BETWEEN_SCALES)

    @cached_property
    def downscale_cache_key(self):
        return f'heroku:downscale_formation:{self.formation_name}'

    @property
    def is_downscaling(self):
        cache.get(self.downscale_cache_key)

    def set_downscaling(self):
        cache.set(self.downscale_cache_key, True, timeout=settings.DYNO_TIME_BETWEEN_SCALES)

    @cached_property
    def threads_used_cache_key(self):
        return f'heroku:threads_used::{self.app_name}:{self.dyno_name}'

    def get_threads_used(self):
        if self.remote_monitoring:
            return cache.get(self.threads_used_cache_key)

        return self.set_threads_used()

    def set_threads_used(self):
        threads_used = len(threading.enumerate())
        if threads_used:
            cache.set(self.threads_used_cache_key, threads_used, timeout=1 * 60 * 60)
        return threads_used

    def autoscale(self, continuous=True):
        """
        Autoscale the dyno based on current requirements.

        Args:
            continuous (bool): Whether to run autoscaling continuously.
        """

        # If continuous mode, return stats
        if continuous:
            logger.info(f"Dyno {self.dyno_name} stats: "
                        f"Formation Size: {self.formation_size}, "
                        f"Memory Usage: {self.current_memory_usage} / {self.available_memory} MB, "
                        f"Load Avg (1min): {self.avg_load_1min}, "
                        f"Tasks in Queue: {self.tasks_in_queue}, "
                        f"Threads Used: {self.threads_used}, "
                        f"Detected R14: {self.detected_r14}, "
                        f"Detected R15: {self.detected_r15}, "
                    )

        # ignore beatworker if settings.DYNO_AUTOSCALE_ENABLED_FOR_BEATWORKER is False
        if 'beatworker' == self.formation_name and hasattr(settings, 'DYNO_AUTOSCALE_ENABLED_FOR_BEATWORKER') and not settings.DYNO_AUTOSCALE_ENABLED_FOR_BEATWORKER:
            return

        try:
            if hasattr(settings, 'DYNO_LOG_THREADS_USED') and settings.DYNO_LOG_THREADS_USED:
                self.set_threads_used()

            if self.requires_upscale:
                self.upscale_formation_to_next_level()
            else:
                self.check_and_downscale_to_original_formation_size()
        except Exception as e:
            logger.error(f"Failed to autoscale dyno {self.dyno_name}. Error: {e}", exc_info=True)

    def _run_continuous(self):
        while not self._stop_event.is_set():
            self.check_in_dyno()
            self.check_for_sibling_zombie_dynos()
            self.autoscale(continuous=True)
            time.sleep(settings.DYNO_AUTOSCALE_INTERVAL)

    def start_continuous_autoscale(self):
        """
        Start continuous autoscaling on a separate thread.
        """
        with self._thread_lock:
            if self._autoscale_thread and self._autoscale_thread.is_alive():
                logger.warning(f"Autoscaling thread for {self.dyno_name} is already running.")
                return

            self._stop_event.clear()
            self._autoscale_thread = threading.Thread(target=self._supervised_run, args=(), daemon=True)
            self._autoscale_thread.start()
            logger.info(f"Continuous autoscaling thread started for {self.dyno_name} with interval {settings.DYNO_AUTOSCALE_INTERVAL} seconds.")
            return self._autoscale_thread

    def stop_continuous_autoscale(self):
        """Stop the continuous autoscaling thread."""
        with self._thread_lock:
            self.remove_dyno_from_alive_cache()

            if not self._autoscale_thread or not self._autoscale_thread.is_alive():
                # logger.warning(f"No running autoscaling thread to stop for {self.dyno_name}.")
                return

            self._stop_event.set()

            # Ensure we are not calling join on the current thread
            if threading.current_thread() != self._autoscale_thread:
                self._autoscale_thread.join()

            self._autoscale_thread = None
            logger.info(f"Stopped continuous autoscaling for {self.dyno_name}.")

    def _supervised_run(self):
        """Supervised loop to restart the thread if it exits."""
        while not self._stop_event.is_set():
            try:
                self._run_continuous()
            except Exception as e:
                logger.error(f"Autoscaler thread for {self.dyno_name} crashed. Restarting... Error: {e}", exc_info=True)
                time.sleep(15)  # Short delay before restarting

    # Registers dyno as alive in redis cache table so other workers can check if it's alive and restart it if it doesn't respond for a while
    def check_in_dyno(self):
        now = timezone.now()
        cache.set(f'heroku:dyno_alive:{self.dyno_name}', now, timeout=24 * 60 * 60)

    def remove_dyno_from_alive_cache(self, dyno_name=None):
        dyno_name = dyno_name or self.dyno_name
        cache.delete(f'heroku:dyno_alive:{dyno_name}')

    def check_for_sibling_zombie_dynos(self):
        """
        Check if any sibling dynos are marked as alive in the cache and restart them if they
        have not checked in within the DYNO_ZOMBIE_THRESHOLD seconds.
        """
        # Make sure only one dyno is checking for zombie dynos at a time
        with cache.lock('heroku:dyno_alive', expire=30):
            siblings = [dyno for dyno in cache.keys('heroku:dyno_alive:*')]
            for sibling in siblings:
                last_checkin = cache.get(sibling)
                if last_checkin:
                    last_checkin_seconds_ago = (timezone.now() - last_checkin).total_seconds()
                    # logger.info(f"Sibling dyno: {sibling}. Last check-in: {last_checkin} ({last_checkin_seconds_ago:.2f} seconds ago).")
                    if hasattr(settings, 'DYNO_ZOMBIE_THRESHOLD') and last_checkin_seconds_ago > settings.DYNO_ZOMBIE_THRESHOLD:
                        last_checkin_minutes_ago = last_checkin_seconds_ago // 60
                        dyno_name = sibling.split(':')[-1]
                        logger.error(f"Zombie dyno detected: {dyno_name}. Last check-in: {last_checkin_minutes_ago:.0f} minutes ago. Restarting...")
                        self.restart_zombie_dyno(dyno_name)

    def restart_zombie_dyno(self, dyno_name):
        self.restart_dyno(dyno_name)


    def upscale_formation_to_next_level(self):
        '''
        Upscale the dyno to the next level in the hierarchy for a period of X hours
        '''
        # Ensure upscale is only executed once every settings.DYNO_TIME_BETWEEN_SCALES seconds for this dyno type

        with cache.lock(self.upscaling_cache_key, expire=30):
            if self.is_upscaling:
                logger.info(f"Upscaling formation {self.formation_name} is already in progress.")
                return

            self.set_upscaling()

        if not self.remote_monitoring:
            logger.warning(f"Memory usage is greater than {self.current_memory_usage_percentage:.2f}% of available RAM ({self.available_memory}MB). "
                            f"Current memory usage: {self.current_memory_usage:.2f}MB. Triggered by dyno {self.dyno_name}")

        next_level = self.next_formation_size
        if not next_level:
            logger.info("Formation is already at the highest level or unrecognized size.")
            return self.formation_size

        # Store original dyno size in Redis cache
        self.set_original_formation_size()

        # Update dyno to upscale
        url = f'https://api.heroku.com/apps/{self.app_name}/formation/{self.formation_name}'
        headers = {
            'Accept': 'application/vnd.heroku+json; version=3',
            'Authorization': f'Bearer {self.heroku_api_key}',
        }

        response = requests.patch(url, headers=headers, json={"size": next_level})
        if response.status_code == 200:
            logger.info(f"Upscaled formation {self.formation_name} from {self.formation_size} to {next_level} with {DYNO_SIZES[next_level]['memory']} MB memory.")

            # Set cache key that expires in X hour to downscale back to original size
            if hasattr(settings, 'DYNO_DOWNSCALE_CHECK_INTERVAL') and hasattr(settings, 'DYNO_MIN_UPSCALE_DURATION') and hasattr(settings, 'DYNO_AUTOSCALE_INTERVAL'):
                delta = settings.DYNO_DOWNSCALE_CHECK_INTERVAL + settings.DYNO_MIN_UPSCALE_DURATION + settings.DYNO_AUTOSCALE_INTERVAL
                until = timezone.now() + timezone.timedelta(seconds=delta)
                cache.set(self.upscale_until_cache_key, until, timeout=delta)

            self.stop_continuous_autoscale()
        else:
            logger.error(f"Failed to upscale formation {self.formation_name}. Response: {response.status_code} - {response.text}")

    def check_and_downscale_to_original_formation_size(self):
        """
        Checks if formation should be downscaled by checking memory usage and downscale if necessary
        """
        # Check if formation is upscaled and if it should be downscaled
        upscaled_until = cache.get(self.upscale_until_cache_key)
        if upscaled_until:
            # if ttl of cach is about to expire (5 min before) - check if memory usage is still high and extend the scaled time
            current_ttl = cache.ttl(self.upscale_until_cache_key)
            if hasattr(settings, 'DYNO_DOWNSCALE_CHECK_INTERVAL') and current_ttl < settings.DYNO_DOWNSCALE_CHECK_INTERVAL:
                if self.allow_downscale:
                    self.downscale_formation_to_original_size()
                elif ((self.detected_r14 and not self.detected_r15) or self.is_still_high_memory_usage_for_downscale) and self.no_tasks_in_queue:
                    self.restart_dyno()
                else:
                    load_avg = f'{self.avg_load_1min:.2f}' if self.avg_load_1min else 'unknown'
                    memory_usage = f'{self.current_memory_usage_percentage}% ({self.current_memory_usage:.2f}MB / {self.available_memory}MB)' \
                        if self.current_memory_usage and self.available_memory and self.current_memory_usage_percentage else 'unknown'
                    logger.warning(f"Extending the time for {self.formation_name} to stay upscaled by {settings.DYNO_DOWNSCALE_CHECK_INTERVAL} seconds. "
                                    f"Current Memory Usage: {memory_usage}. Current Load Avg: {load_avg}. Tasks in Queue: {self.tasks_in_queue}.")
                    new_until = upscaled_until + timezone.timedelta(seconds=settings.DYNO_DOWNSCALE_CHECK_INTERVAL)
                    new_timeout = current_ttl + settings.DYNO_DOWNSCALE_CHECK_INTERVAL
                    cache.set(self.upscale_until_cache_key, new_until, timeout=new_timeout)
            return

        # if on original formation size and memory usage is high, restart the dyno
        if hasattr(settings, 'DOWNSCALE_PERCENTAGE_HIGH_MEM_USE') and self.current_memory_usage_percentage > settings.DOWNSCALE_PERCENTAGE_HIGH_MEM_USE and self.detected_r14 and not self.detected_r15 and self.no_tasks_in_queue:
            self.restart_dyno()
            return

        # Downscale formation to original size as there is no need to keep it upscaled
        # logger.info(f"Downscaling formation {self.formation_name} back to original size because cache wasn't extended. Current Memory Usage: {self.current_memory_usage_percentage}%")
        self.downscale_formation_to_original_size()

    def downscale_formation_to_original_size(self):
        if self.previous_formation_size is None:
            return

        original_formation_size = self.original_formation_size

        # If original size is not set, no need to downscale
        if not original_formation_size:
            return

        with cache.lock(self.downscale_cache_key, expire=30):
            if original_formation_size == self.formation_size:
                logger.info("Formation is already at the original size.")
                self.clear_original_formation_size()
                return

            # Check if formation is on lower size than original size and skip downscale
            if self.is_on_original_formation_size_or_lower:
                logger.info(f"Formation {self.formation_name} is already at original or lower size than the original size.")
                self.clear_original_formation_size()
                return

            # Ensure downscale is only executed once every settings.DYNO_TIME_BETWEEN_SCALES seconds for this formation type
            if self.is_downscaling:
                # logger.info(f"Downscaling formation {self.formation_name} is already in progress.")
                return

            self.set_downscaling()

        # Scale formation back to original size
        url = f'https://api.heroku.com/apps/{self.app_name}/formation/{self.formation_name}'
        headers = {
            'Accept': 'application/vnd.heroku+json; version=3',
            'Authorization': f'Bearer {self.heroku_api_key}',
        }

        response = requests.patch(url, headers=headers, json={"size": original_formation_size})
        if response.status_code == 200:
            logger.info(f"Downscaled formation {self.formation_name} back to {original_formation_size} with {DYNO_SIZES[original_formation_size]['memory']} MB memory.")
            self.clear_original_formation_size()
            self.stop_continuous_autoscale()
        else:
            logger.error(f"Failed to downscale formation {self.formation_name}. Response: {response.status_code} - {response.text}")

    def restart_dyno(self, dyno_name=None):
        if not self.app_name or (not self.dyno_name and not dyno_name):
            return

        dyno_name = dyno_name or self.dyno_name

        # Ensure restart is only executed once every settings.DYNO_TIME_BETWEEN_RESTARTS seconds for this dyno
        cache_key = f'heroku:restart_dyno:{dyno_name}'
        with cache.lock(cache_key, expire=30):
            if cache.get(cache_key):
                logger.info(f"Restarting dyno {dyno_name} is already in progress.")
                return

            if hasattr(settings, 'DYNO_TIME_BETWEEN_RESTARTS'):
                cache.set(cache_key, True, timeout=settings.DYNO_TIME_BETWEEN_RESTARTS)
            else:
                cache.set(cache_key, True, timeout=300)  # Default to 5 minutes


        # Restart the dyno via Heroku API
        url = f'https://api.heroku.com/apps/{self.app_name}/dynos/{dyno_name}'
        headers = {
            'Accept': 'application/vnd.heroku+json; version=3',
            'Authorization': f'Bearer {self.heroku_api_key}',
        }

        response = requests.delete(url, headers=headers)
        if response.status_code == 202:
            logger.info(f"Restarting dyno {dyno_name}...")

            if dyno_name == self.dyno_name:
                self.stop_continuous_autoscale()
            else:
                self.remove_dyno_from_alive_cache(dyno_name)
        else:
            logger.error(f"Failed to restart dyno {dyno_name}. Response: {response.status_code} - {response.text}")
            return

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
            logger.info(f"Initialized counter for dyno {dyno_name} with value 1 and TTL {ttl} seconds")
        else:
            # Increment the counter without changing the TTL
            counter = cache.incr(cache_key)
            logger.info(f"Incremented counter for dyno {dyno_name} to {counter}")

        # Get the threshold from settings or default to 15
        threshold = getattr(settings, 'DYNO_RESTART_THRESHOLD', 15)

        # If counter exceeds threshold, restart the dyno
        if counter >= threshold:
            logger.warning(f"Counter for dyno {dyno_name} reached threshold {threshold}. Restarting dyno...")
            self.restart_dyno(dyno_name)
            # Reset the counter after restart
            cache.delete(cache_key)
            return 0

        return counter

    @retry(stop=stop_after_attempt(5), wait=wait_exponential(multiplier=1, min=4, max=10), retry=retry_if_exception_type((SSLError, ConnectionError, Timeout)))
    def get_heroku_logs(self, source="heroku", date_from=None):
        if not self.heroku_api_key:
            return None

        cache_key = f'heroku:logs:{self.app_name}:{self.dyno_name}'
        logs = cache.get(cache_key)
        if not logs:
            with cache.lock(cache_key, expire=30):
                logs = cache.get(cache_key)
                if not logs:
                    # Step 1: Set up API request to retrieve logs
                    url = f"https://api.heroku.com/apps/{self.app_name}/log-sessions"
                    headers = {
                        "Accept": "application/vnd.heroku+json; version=3",
                        "Authorization": f"Bearer {self.heroku_api_key}",
                    }
                    payload = {
                        "dyno": self.dyno_name,
                        "tail": False,
                        "source": source,
                        "lines": 200  # Adjusting this almost does nothing
                    }

                    try:
                        # Step 2: Start a log session and retrieve logs
                        response = requests.post(url, headers=headers, json=payload)
                        response.raise_for_status()
                    except SSLError as e:
                        logger.error(f"SSLEOFError occurred while retrieving log session: {e}")
                        return None
                    except requests.exceptions.RequestException as e:
                        logger.error(f"Failed to retrieve log session. Status code: {response.status_code} - {response.text}")
                        return None

                    log_url = response.json().get("logplex_url")
                    if not log_url:
                        logger.info("Log URL not found in the response.")
                        return None

                    log_response = requests.get(log_url)
                    if log_response.status_code != 200:
                        logger.info(f"Failed to retrieve logs. Status code: {log_response.status_code} - {log_response.text}")
                        return None

                    logs = log_response.text or "\n" # Ensure logs is not empty to not overload API
                    if hasattr(settings, 'DYNO_LOGS_CACHE_DURATION'):
                        cache.set(cache_key, logs, timeout=settings.DYNO_LOGS_CACHE_DURATION)
                    else:
                        cache.set(cache_key, logs, timeout=60)  # Default to 1 minute

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
            cache_refresh_interval = getattr(settings, 'DYNO_GENERAL_CACHE_DURATION', 300)  # Default to 5 minutes
        else:
            timeout = timeout
            cache_refresh_interval = timeout

        cache_key = f'{cache_key}:{self.app_name}:{self.dyno_name}'

        # Fetch from cache
        cached_value = cache.get(cache_key)
        ttl = cache.ttl(cache_key) or 0
        cache_age = timeout - ttl  # Calculate cache age in seconds

        # If cache is fresh, return cached value
        if cache_age < cache_refresh_interval and cached_value is not None:
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
            for pattern in compiled_patterns:
                match = pattern.search(message)
                if match:
                    # Extract the matched value
                    extracted_value = match.group(1)
                    latest_value = result_type(extracted_value) if extracted_value.replace('.', '', 1).isdigit() else extracted_value
                    break
            if latest_value is not None:
                break

        if result_type == bool:
            latest_value = bool(latest_value)

        # Update cache with the latest value
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
        timeout = getattr(settings, 'DYNO_ERRORS_TIMEOUT_DURATION', 3600)  # Default to 1 hour
        return self.extract_latest_metric(patterns, cache_key="heroku:r15_error", timeout=timeout, result_type=bool)

    def get_r14_from_logs(self):
        """
        Retrieves the most recent R14 error from Heroku logs, indicating memory quota exceeded.

        Returns:
            bool: The R14 error message if found, or None if not found.
        """
        patterns = [r"Error R14 \((.*?)\)"]
        timeout = getattr(settings, 'DYNO_ERRORS_TIMEOUT_DURATION', 3600)  # Default to 1 hour
        return self.extract_latest_metric(patterns, cache_key="heroku:r14_error", timeout=timeout, result_type=bool)
