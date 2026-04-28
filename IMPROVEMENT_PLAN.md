# Heroku Manager — Improvement Plan
> Generated: 2026-04-28 via 6-expert analysis (2× performance, 2× best-practice, 2× logic)

---

## Summary

| Priority | Count | Description |
|----------|-------|-------------|
| **P0** | 9 | Production bugs / security — fix immediately |
| **P1** | 16 | Significant logic errors, reliability gaps, quality issues |
| **P2** | 13 | Minor improvements, YAGNI, code clarity |

---

## P0 — Fix Immediately (Production Bugs / Security)

### P0-1 · `any_sibling_still_high_memory` glob pattern missing wildcard — guard is silently dead
**File**: `heroku_manager/heroku.py` — `any_sibling_still_high_memory` property  
**Type**: Logic Bug  
Pattern `f'heroku:dyno_memory:{self.formation_name}.'` has a trailing `.` but no `*`.  
`cache.keys()` glob matching never matches `worker.1`, `worker.2`, etc.  
**Impact**: Sibling memory guard always returns `False`. Any single low-memory dyno can
trigger a formation downscale while all other dynos are above threshold, immediately causing
R14/R15 storms across the formation.  
**Fix**: Change to `f'heroku:dyno_memory:{self.formation_name}.*'`

---

### P0-2 · Shared `_stop_event` silently kills both autoscale and file-cleaning threads
**File**: `stop_continuous_autoscale()`, `stop_continuous_file_cleaning()`, `start_continuous_autoscale()`  
**Type**: Logic Bug / Thread Safety  
A single `threading.Event` is shared between two independent threads. Calling either
`stop_*` method sets the event, terminating both loops. `start_continuous_autoscale()`
clears the event and restarts only the autoscale thread — file cleaning is silently dead
with no log entry and no recovery path.  
**Impact**: File cleaning permanently stops after any autoscale stop/restart cycle. On long-lived
dynos with many temp files, this causes disk/inode pressure.  
**Fix**: Replace with `_stop_autoscale_event` and `_stop_file_cleaning_event` (two events).

---

### P0-3 · `_acquire_rate_limit_token` blocks the autoscale thread up to 195 seconds
**File**: `_acquire_rate_limit_token()` → `call_heroku_api()`  
**Type**: Performance / Availability  
`time.sleep(wait)` runs directly on the autoscale thread (up to 65 s × 3 attempts = 195 s).
`call_heroku_api` adds another 120 s for 429 backoff and 30 s for transient retries on the same thread.  
**Impact**: Under API pressure, autoscaling is completely frozen for 3–5 minutes. Memory-related
upscales are blocked while the dyno is OOM. Total worst-case stall: ~325 s.  
**Fix**: Return `False` immediately (skip cycle) rather than sleeping inline. The caller
already handles `None` gracefully. Log the skip so the pattern is observable.

---

### P0-4 · `cache.keys()` full keyspace scans run on every autoscale tick
**File**: `check_for_sibling_zombie_dynos()`, `any_sibling_still_high_memory`  
**Type**: Performance  
Redis `KEYS` is O(N) and **blocks the Redis event loop** for the full scan. With two calls
per tick across every dyno in the fleet, this creates coordinated Redis stalls every ~30 s.  
**Impact**: Latency spikes for all Redis clients in the fleet (not just heroku-manager).  
**Fix**: Maintain an explicit Redis Set (`heroku:dyno_members:{formation_name}`) with
`SADD`/`SREM` to register/deregister dynos. Replace all `cache.keys()` calls with `SMEMBERS`.

---

### P0-5 · `_get_proc_class_by_formation_name` permanently caches `None` via `cached_property`
**File**: `_get_proc_class_by_formation_name` cached_property  
**Type**: Logic Bug  
A transient import error at startup (misconfigured `HIREFIRE_PROCS`, module not ready)
caches `None` for the process lifetime. `tasks_in_queue` then silently returns `0` forever.  
**Impact**: Autoscaler treats queue as empty permanently → formation downscales regardless
of actual workload. Requires full dyno restart to recover. No error is ever raised.  
**Fix**: Change `@cached_property` to `@property` so it retries on every access, or cache
only successful lookups explicitly.

---

### P0-6 · `stop_continuous_autoscale()` called on successful scale permanently kills remote monitoring
**File**: `upscale_formation_to_next_level()`, `downscale_formation_to_original_size()`  
**Type**: Logic Bug  
For `remote_monitoring=True` dynos, the monitoring dyno is NOT restarted by the scale
operation. `_supervised_run` exits on `_stop_event.set()` (not on exception), so it also
terminates. The formation is left unmonitored permanently after every remote scale event.  
**Impact**: Silent monitoring outage after every remote formation scale — no upscale/downscale
decisions until next dyno restart.  
**Fix**: Guard `stop_continuous_autoscale()` calls with `if not self.remote_monitoring:`.

---

### P0-7 · Shell injection in `exec_connect` / `clean_old_files`
**File**: `exec_connect()`, `clean_old_files()`  
**Type**: Security (OWASP A03 — Injection)  
`payload = {'command': f'bash -c "{command}"'}` — if `command` contains `"`, `;`, `$(`, `` ` ``,
or `\`, an attacker with control over `command` can break out of the quotes and execute
arbitrary code on the one-off dyno. `directory` in the `find` f-string is also unsanitized.  
**Impact**: Remote code execution on Heroku infrastructure.  
**Fix**: Use `shlex.quote()` on all user-supplied arguments before interpolating into shell
strings. Validate `directory` against an allowlist. For `clean_old_files`, build the command
as a list and avoid shell=True entirely where possible.

---

### P0-8 · `increment_dyno_counter` returns mixed types (`int` or `'restarted'`)
**File**: `increment_dyno_counter()`  
**Type**: Logic Bug  
Returns `'restarted'` (string) when the threshold is reached, `int` otherwise. The docstring
declares `Returns: int`. Any caller doing `if result >= threshold` receives a `TypeError`.  
**Fix**: Return a sentinel int (e.g. `0` or `-1`) on restart, or raise a typed exception.
Update the docstring to match.

---

### P0-9 · `_downscale_memory_threshold` returns `0` when no original/previous size — dyno never downscales
**File**: `_downscale_memory_threshold` property  
**Type**: Logic Bug  
If both `original_formation_size` and `previous_formation_size` are `None` (Redis flush,
key expiry, standard-1x formation), `target_memory = 0` and `threshold = 0`.
`is_still_high_memory_usage_for_downscale = current_memory >= 0` is always `True`.
`allow_downscale` is permanently `False`.  
**Impact**: Formation is permanently stuck in upscaled state, accruing unnecessary cost until
next manual intervention.  
**Fix**: When `target_size` is `None`, fall back to `formation_size` directly, or return a
threshold of `float('inf')` to indicate "unknown — block downscale" with an explicit log warning.

---

## P1 — Fix Soon (Significant Reliability / Quality Issues)

### P1-1 · `set_upscaling()` flag expires while API call is in-flight → duplicate upscale
**File**: `upscale_formation_to_next_level()`  
Lock releases before `call_heroku_api()`. If the API call (with retries) takes longer than
`DYNO_TIME_BETWEEN_SCALES`, `is_upscaling` expires. A second dyno passes the lock, issues
a second PATCH, and formation jumps two size levels. Downscale only restores to
`original_formation_size` (one level), leaving formation permanently over-provisioned.  
**Fix**: Extend `set_upscaling()` TTL to `DYNO_TIME_BETWEEN_SCALES + max_api_call_duration`
(e.g. add 300 s buffer), or re-check `is_upscaling` after the API call completes.

---

### P1-2 · `is_upscaling` flag stays `True` after API failure, blocking retries
**File**: `upscale_formation_to_next_level()`  
`set_upscaling()` is called before the API call. On non-200 response, no `clear_upscaling()`
is called. Retry attempts are blocked for `DYNO_TIME_BETWEEN_SCALES` even though no scale occurred.  
**Fix**: Add a `clear_upscaling()` method and call it in the error path (non-200 and `None` responses).

---

### P1-3 · `_acquire_rate_limit_token` TOCTOU — `cache.incr()` return value discarded
**File**: `_acquire_rate_limit_token()`  
Multiple dynos can all pass `if current < rate_limit` before any increments. The post-`incr`
value is never inspected, allowing true counter to reach `N × rate_limit`.  
**Fix**: Use the return value of `cache.incr()` and reject if it exceeds `rate_limit`. Consider
using Redis `INCR` + `EXPIRE` atomically via a Lua script or pipeline.

---

### P1-4 · `is_on_original_formation_size_or_lower` resets baseline on Redis key expiry
**File**: `is_on_original_formation_size_or_lower` property  
If `original_formation_size` key expires and the formation was manually resized down, calling
this property sets the current (smaller) size as the new baseline via `set_original_formation_size()`.
All subsequent upscales use the wrong target, and the dyno never restores to its true original size.  
**Fix**: Remove the side effect from the property. Callers should ensure `set_original_formation_size()`
is called at appropriate times (startup), not inside a property getter.

---

### P1-5 · `check_and_downscale` early return misses safe mid-cycle downscale opportunities
**File**: `check_and_downscale_to_original_formation_size()`  
When `upscaled_until` is set but `current_ttl >= DYNO_DOWNSCALE_CHECK_INTERVAL`, the method
returns immediately without inspecting memory. Memory could be safe for hours but downscale
waits for TTL to approach zero, inflating cost unnecessarily.  
**Fix**: Evaluate `allow_downscale` even when TTL is not near expiry; downscale early if all
conditions are satisfied.

---

### P1-6 · `increment_dyno_counter` always deletes counter even when restart fails
**File**: `increment_dyno_counter()`  
`cache.delete(cache_key)` runs unconditionally after `restart_dyno()`. If restart fails
(rate-limited, API error), counter resets to 0 and the threshold is never re-triggered within
the TTL window. A malfunctioning dyno becomes invisible to the watchdog.  
**Fix**: Only delete counter when `restart_dyno()` confirms success (202 response).

---

### P1-7 · `check_in_dyno` skips writing memory key when usage is `0` (falsy)
**File**: `check_in_dyno()`  
`if mem:` treats `0` as "no data". A temporarily valid 0 MB reading (or failed read) causes
no memory key to be written. `any_sibling_still_high_memory` then treats the dyno as safe
and may allow downscale when memory state is actually unknown.  
**Fix**: Change to `if mem is not None:`.

---

### P1-8 · "Stuck True" bug in `extract_latest_metric` for `bool` result type (R14/R15 never clears)
**File**: `extract_latest_metric()`, `get_r14_from_logs()`, `get_r15_from_logs()`  
When no match is found, the method returns `cached_value` (fallback). If `cached_value` is
`True` (from a prior R14/R15 match), it is returned forever — detection permanently latched.
`allow_downscale` remains `False` indefinitely even after the error clears.  
**Fix**: When no match is found in logs (within the timeout window), explicitly return `False`
for `bool` result type rather than falling back to cached value.

---

### P1-9 · `get_heroku_logs` logplex URL fetch bypasses rate limiting and error handling
**File**: `get_heroku_logs()`  
`log_response = requests.get(log_url)` is a raw call with no retry, no SSL error handling,
no timeout, and no rate limiting. Network errors here raise unhandled exceptions within the
`cache.lock` block.  
**Fix**: Wrap logplex fetch with `try/except (SSLError, ConnectionError, Timeout)` and add a
reasonable `timeout=` argument.

---

### P1-10 · Inconsistent settings access (`hasattr` vs `getattr`) throughout the file
**File**: Throughout `heroku_manager/heroku.py`  
10+ `hasattr(settings, X) and settings.X` patterns are mixed with `getattr(settings, X, default)`.
The `hasattr`+direct-access pattern is verbose, TOCTOU-prone, and violates DRY.  
**Fix**: Standardize on `getattr(settings, X, default)` everywhere. Document required (no default)
vs optional (with default) settings in a module-level docstring or `REQUIRED_SETTINGS` list.

---

### P1-11 · Mutable default argument in `call_heroku_api`
**File**: `call_heroku_api(self, method, url, custom_headers={}, data=None)`  
Classic Python footgun — the dict is shared across all calls using the default. Any future
mutation (e.g. `headers.update(custom_headers)` is read-only, but callers may extend behavior)
produces cross-call contamination.  
**Fix**: Change default to `None`; add `custom_headers = custom_headers or {}` inside the body.

---

### P1-12 · Fragile manual `cached_property` invalidation in `_update_formation_size`
**File**: `_update_formation_size()`  
Six `cached_property` names are hard-coded in a list for manual `__dict__.pop()`. Adding a
new dependent `cached_property` silently breaks correctness unless the pop list is updated.  
**Fix**: Extract to a `_invalidate_formation_dependent_cache()` method with a docstring listing
the dependency chain, or use a `__reset_cache_on_resize__` class-level tuple convention.

---

### P1-13 · `settings` loop variable shadows Django `settings` import in `get_dyno_settings`
**File**: `get_dyno_settings()` module function  
`for dyno, settings in DYNO_SIZES.items()` shadows the `from django.conf import settings`
import. Any future access to `settings.ANYTHING` inside that loop silently reads a dict value.  
**Fix**: Rename loop variable to `dyno_cfg` or `size_info`.

---

### P1-14 · `extract_latest_metric` cache-age logic always misses under non-Redis backends
**File**: `extract_latest_metric()`  
`cache.ttl()` returns `0` for non-existent keys on `LocMemCache` (and any non-Redis backend).
`cache_age = timeout - 0 = timeout ≥ cache_refresh_interval` unconditionally — logs are
always re-fetched. This masks cache-miss bugs in CI and causes unexpected API calls in
non-Redis deployments.  
**Fix**: Treat `ttl == 0` as "key missing" (force refresh) vs "key present but about to expire"
(allow stale use). Use a separate sentinel key or store a timestamp alongside the cached value.

---

### P1-15 · No startup validation of required settings
**File**: `HerokuDyno.__init__()` / module level  
~30 settings accesses mix bare attribute access (`settings.HIGH_MEM_USE_MB`), `getattr` with
defaults, and `hasattr` guards. Missing required settings throw `AttributeError` mid-operation,
caught by the autoscale `try/except`, and logged as errors — the scale is silently skipped.  
**Fix**: Add a `validate_settings()` call at startup (e.g. in `__init__` or `start_continuous_autoscale`)
that checks all required settings exist and raises with a clear message early.

---

### P1-16 · Regex patterns recompiled on every `extract_latest_metric` call
**File**: `extract_latest_metric()`  
`re.compile(pattern)` is called inside the method for every invocation across 5 metric callers
per autoscale tick. Python's `re` module has an internal LRU cache but it is sized for 512 patterns —
with dynamically constructed patterns this works, but explicit compilation at class/module level
is clearer and avoids the LRU-miss overhead.  
**Fix**: Pre-compile patterns in callers (`get_memory_usage_from_logs`, etc.) and pass compiled
`re.Pattern` objects, or cache via `functools.lru_cache` on `re.compile`.

---

## P2 — Improve When Convenient (Code Quality / Minor Issues)

### P2-1 · `formation_size` instance cache TTL equals autoscale interval — Redis hit every tick
Instance cache expires at exactly `DYNO_AUTOSCALE_INTERVAL` (same as loop sleep), so it
provides zero actual caching. Either increase TTL (e.g. 5× interval) or eliminate the Redis
lookup by only refreshing on explicit scale events (`_update_formation_size`).

### P2-2 · Duplicate `check_and_clean_old_files()` in both `_run_continuous` and `_run_continuous_file_cleaning`
Both loops call the same method at the same interval. The internal timestamp guard makes it
idempotent, but having a dedicated file-cleaning thread is pointless if the work is also
done in the main loop. Remove the call from `_run_continuous` or remove the dedicated thread.

### P2-3 · `set_threads_used` caches thread count for 1 hour — stale for autoscale decisions
`threading.enumerate()` count is meaningful only for the current moment. A 1-hour TTL means
autoscale uses counts that are up to 3,600 ticks stale. Either use a much shorter TTL (e.g.
2× autoscale interval) or remove threads from scaling criteria entirely.

### P2-4 · Unused imports: `import subprocess`, `from subprocess import run`
Neither is used anywhere in the file. Remove to reduce reader confusion and avoid misleading
about the module's capabilities.

### P2-5 · Inner class `ApiResult` defined inside `exec_connect` — recreated on every call
Move to module-level `dataclasses.dataclass` or `typing.NamedTuple`. Also clarifies the
public shape of the return value.

### P2-6 · `restart_zombie_dyno` is a pointless single-line delegate
Adds a call frame with no additional logic, logging, or contract. Either add zombie-specific
behavior or inline the call directly in `check_for_sibling_zombie_dynos`.

### P2-7 · `is_on_original_formation_size_or_lower` builds unnecessary list comprehension
Building a list of all sizes ≤ original memory and then checking membership is O(n) when a
direct comparison suffices: `DYNO_SIZES[self.formation_size]["memory"] <= DYNO_SIZES[original]["memory"]`.

### P2-8 · `allow_downscale_on_shutdown` is dead code — never called anywhere
Remove or wire up in `stop_continuous_autoscale()`. Also missing a queue-check guard compared
to `allow_downscale` (would downscale a busy formation on shutdown if ever used).

### P2-9 · Lock key `heroku:dyno_alive` is a prefix of the `heroku:dyno_alive:*` scan pattern
On some Redis Django backends, `cache.lock` prefixes the key with additional namespace segments
that could match the `heroku:dyno_alive:*` glob, causing the lock entry to appear in zombie-detection
scans and producing a `TypeError` crash. Use a distinct lock key: `heroku:lock:dyno_alive_check`.

### P2-10 · `get_dyno_settings` returns a live reference to `DYNO_SIZES` entries (mutation risk)
Both return paths return the dict object directly from `DYNO_SIZES`. Any caller mutating the
returned dict corrupts the module-level constant. Return `dict(DYNO_SIZES[formation_size])` (shallow copy).

### P2-11 · Double-colon typo in `threads_used_cache_key`: `heroku:threads_used::`
`f'heroku:threads_used::{self.app_name}:{self.dyno_name}'` — the double colon is inconsistent
with every other key in the file. Fix to single colon. Key is consistent between write/read
(both use the same property), but it will confuse observability tooling and tests.

### P2-12 · No central cache key registry — 14+ hard-coded key patterns scattered throughout
Cache keys are duplicated in `heroku.py` and across test files. A single typo in one location
silently creates a key mismatch. Centralize in a `CacheKeys` dataclass or frozen `NamedTuple`
at the top of the file with all key templates.

### P2-13 · God class: `HerokuDyno` holds ~14 distinct responsibilities
Testability and maintainability suffer. Natural seams for extraction:
- `HerokuApiClient` — rate limiter + HTTP + retry
- `HerokuLogParser` — log fetch, parse, metric extraction
- `FormationAutoscaler` — scale decisions, cache state, thresholds
- `DynoMonitor` — threading lifecycle, zombie detection, check-in

---

## Recommended Implementation Order

### Sprint 1 — P0 (immediate, blocking bugs)
1. P0-1: Fix `any_sibling_still_high_memory` wildcard pattern (`.*`)
2. P0-2: Split `_stop_event` into two events
3. P0-7: Fix shell injection in `exec_connect`/`clean_old_files`
4. P0-5: Fix `_get_proc_class_by_formation_name` → change to `@property`
5. P0-9: Fix `_downscale_memory_threshold` zero-threshold case
6. P0-8: Fix `increment_dyno_counter` return type
7. P0-3: Make `_acquire_rate_limit_token` non-blocking (skip cycle vs sleep)
8. P0-4: Replace `cache.keys()` with Redis Set membership
9. P0-6: Guard `stop_continuous_autoscale()` with `remote_monitoring` check

### Sprint 2 — P1 high-impact
1. P1-4: Remove side effect from `is_on_original_formation_size_or_lower`
2. P1-7: Fix `if mem:` → `if mem is not None:` in `check_in_dyno`
3. P1-8: Fix "stuck True" R14/R15 detection
4. P1-1 + P1-2: Fix `set_upscaling` TTL and add `clear_upscaling` on failure
5. P1-3: Fix TOCTOU in `_acquire_rate_limit_token`
6. P1-6: Fix counter deletion on failed restart
7. P1-15: Add startup settings validation

### Sprint 3 — P1 quality
1. P1-10: Standardize `getattr(settings, X, default)` throughout
2. P1-11: Fix mutable default argument
3. P1-13: Fix `settings` shadow in `get_dyno_settings`
4. P1-14: Fix cache-age logic for non-Redis backends
5. P1-9: Add error handling to logplex URL fetch
6. P1-12: Extract `_invalidate_formation_dependent_cache()`

### Sprint 4 — P2 + architecture
1. P2-1 through P2-12: Apply in a single cleanup PR
2. P2-13: Begin extracting `HerokuApiClient` as first seam (lowest coupling)
