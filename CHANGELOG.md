# Changelog

## 0.2.14 - 2026-09-22
- **Improvement: Phase C index-only dyno readers (FP-17968).** The app-scoped sorted set `heroku:dynos:v1:{app}` is now the only member list. The compatibility `SCAN` and the `HEROKU_DYNO_INDEX_V1_READY` rollout flag are removed, so runtime issues neither `SCAN` nor `KEYS`.
- **Readiness is the own-member ZSCORE.** A dyno is ready when its own index score is present and fresher than `DYNO_ZOMBIE_THRESHOLD`; readiness fails closed on a missing raw client, a missing or stale score, or any read error. Downscale, formation-idle authorization, and zombie restart still require readiness; check-ins and safe upscales remain allowed.
- **Sibling readers are index-only.** `_iter_sibling_values` reads fresh members with one bounded `ZRANGEBYSCORE` and batches values with one `MGET`; an uncertain member read yields nothing rather than a partial fleet. The zombie check keeps one bounded `ZREMRANGEBYSCORE` prune under the existing lock.
- **Failed-writer ledger for a failed index write.** When a registry write raises, the dyno name goes into `heroku:dynos:v1:{app}:degraded` (score = failure time; the key expires two zombie windows after the last failure). Readiness and every destructive gate read that ledger uncached through the write client, so neither a lagging replica nor a readiness cache filled earlier in the cycle can hide the failure; a failed ledger read counts as degraded. Entries clear themselves on the read: a dyno that registers again (fresh index score) or that is gone (its `heroku:dyno_alive:` key expired) drops out, so the gate stays closed exactly while a live dyno can be missing from the only member list. An evicted ZSET needs no ledger entry: every dyno re-registers at its own check-in, so the member list re-converges within one autoscale interval.
- **Removed:** `_IndexAdapter.scan_keys()`, the `HEROKU_DYNO_INDEX_SCAN_CAP` control, and the SCAN reconciliation inside the readiness check.
- **Deploy prerequisite:** every app must already run an index-aware writer (v0.2.13 or later) for at least one full `DYNO_ZOMBIE_THRESHOLD` window; otherwise a live dyno that never wrote to the index is invisible to readers. Rollback is the v0.2.13 tag.
- **Tests:** readiness from the own score, the retired flag proven inert, no `SCAN`/`KEYS` on any read, index-only sibling filtering, batched decode, fail-closed destructive actions, one `ZSCORE` per cycle, one bounded prune, and a source guard that the module has no `scan_keys`, no `scan(`, and no flag or cap names.

## 0.2.13 - 2026-08-03
- **Improvement: Phase B indexed dyno readers (FP-17968).** The sibling-memory/upscale, formation-idle, and zombie readers now consume the app-scoped `heroku:dynos:v1:{app}` sorted set introduced in 0.2.12 instead of Redis `KEYS`. Runtime no longer issues `KEYS`: the ready path uses bounded `ZRANGEBYSCORE` + batched `MGET`; the mixed-version window uses a bounded, exactly-prefix-filtered `SCAN` (never `KEYS`).
- **New flag: `HEROKU_DYNO_INDEX_V1_READY` (default false).** Readiness requires the per-app flag, a fresh v1 index score for the current dyno, and every compatibility-SCAN member represented in the index. When the flag is enabled but readiness is uncertain/incomplete/error, downscale, formation-idle authorization, and zombie restart fail closed; check-in and safe local/sibling upscale remain allowed. Stale index members are pruned once per cycle with one bounded `ZREMRANGEBYSCORE` under the existing zombie-check lock.
- **New control: `HEROKU_DYNO_INDEX_SCAN_CAP` (default 500).** Compatibility scans above the cap log a per-app warning and return no results: readiness fails closed and sibling-upscale evidence degrades for that cycle — never a `KEYS` fallback. Post-enable observability: no `KEYS` slowlog entries, expected-member parity, downscale/zombie behavior, and flag-clear rollback.
- **Destructive actions are gated on readiness, not on the flag.** With the flag false or absent, downscale, formation-idle authorization, and zombie restart fail closed exactly as they do when the flag is true but readiness is uncertain; the compatibility SCAN remains available only to non-destructive sibling readers and safe upscale.
- **Rollout:** enable the flag per app only after the two-cycle expected-member comparison against Heroku Platform API/`heroku ps` passes; clearing the flag is the immediate rollback. Compatibility scan removal is a later Phase C release.
- **Tests: new indexed-reader coverage** exercises flag default false, readiness reconciliation, physical-key SCAN/MGET contract with real django-redis wire formats, fresh-range semantics, bounded SCAN cap, exact formation prefix, own-dyno exclusion, missing metrics, batched MGET, fail-closed destructive actions, single bounded prune, and one-readiness-SCAN-per-cycle reuse.

## 0.2.12 - 2026-07-31
- **Improvement: Phase A indexed dyno registry dual writer (FP-17968).** Every dyno check-in adds the full dyno name to the app-scoped `heroku:dynos:v1:{app}` Redis sorted set with the aware check-in timestamp as its score; graceful cleanup removes the member. The logical key is transformed with the configured cache backend's `make_key()` before raw Redis commands run.
- **Compatibility: existing metric keys and readers are unchanged.** Registry write failures are logged without breaking the established check-in or cleanup lifecycle, allowing this writer release to reach the whole fleet before indexed readers are enabled.
- **Tests: 5 new registry tests** cover physical-key handling, add/remove commands, missing identity/backend failure, and both lifecycle seams. 334 total.

## 0.2.11 - 2026-05-20
- **Fix: `NotAcquired` crash on lock expiry (FLOSHIP-SHIPPING-1FE).** `get_heroku_logs()` held a Redis lock with `expire=30` but made 2 HTTP calls inside (`call_heroku_api` with no timeout + `requests.get(timeout=30)`). Lock expired before `__exit__` released it, crashing the autoscaler thread with `NotAcquired`.
- **Fix: `timeout=30` added to `requests.request()` in `call_heroku_api`.** Was missing entirely, could hang indefinitely.
- **Fix: lock expire increased for `get_heroku_logs` from 30s to 90s.** Covers 2 HTTP timeouts + margin.
- **Fix: `NotAcquired` caught at all 5 `cache.lock` sites** -- logs warning instead of crashing (zombie check, upscale, downscale, restart, log fetch). Import `redis_lock.NotAcquired` with fallback class for envs without `redis_lock`.
- **Fix: abandoned `return` inside lock body.** Python abandons `return` statements when `__exit__` raises -- replaced with flag variables so guard conditions (is_upscaling, is_downscaling, restart in-progress) are respected even when `NotAcquired` fires. Added `if not logs: return None` guard in `get_heroku_logs` to prevent `AttributeError` on `logs.split()`.
- **Tests: 9 new lock expiry tests** covering both happy path (guard=False, lock expires) and guard-hit scenarios (guard=True, lock expires, API call must NOT proceed). 329 total.

## 0.2.10 - 2026-05-06
- **Fix: forced downscale via RSS plateau detection.** Python's memory allocator never releases RSS back to the OS, so after heavy tasks finish, an upscaled worker stays at high memory (e.g. 15 GB on Performance-L-RAM) indefinitely — `is_still_high_memory_usage_for_downscale` always returns True and the autoscaler keeps extending the upscale timer forever. New `is_memory_stabilized` property tracks memory readings over a sliding window (`DYNO_MEMORY_STABILITY_WINDOW`, default 300s) and detects when RSS has flattened out (±`DYNO_MEMORY_STABILITY_TOLERANCE_MB`, default 50 MB, across at least `DYNO_MEMORY_STABILITY_MIN_READINGS`, default 6 readings). When the plateau is detected and the worker is not actively under pressure (no R15, not at/below original size), `allow_downscale` and `allow_downscale_on_shutdown` bypass the memory threshold check and allow the formation resize — which triggers a Heroku restart, resetting RSS. Sibling awareness is also updated: `any_sibling_still_high_memory` now checks the sibling's stability status (`heroku:dyno_memory_stable:` cache key) and does not block downscale when the sibling's memory has also stabilized. Memory history is cleared on both successful downscale and upscale (upscale triggers restart, invalidating old readings). Production impact: normal_worker stuck on Performance-L-RAM (30,720 MB quota) at 15,520 MB RSS for 24+ hours; with this fix it will downscale within ~5 minutes of memory stabilizing.
- **Improvement: proportional tolerance for plateau detection.** Tolerance is now `max(TOLERANCE_MB, avg_reading * TOLERANCE_PCT / 100)` — at 15 GB RSS the effective tolerance is ~150 MB (normal GC jitter won't prevent detection), while at 500 MB it stays at the 50 MB floor.
- **Improvement: minimum time span guard.** Readings must span at least half the stability window (default 150s) to prevent a burst of readings in a few seconds from being mistaken for a plateau.
- **Improvement: stability info in extend log.** The "Extending the time to stay upscaled" warning now includes `Memory Stabilized: True/False (N readings)` so operators can see plateau detection progress.
- **New settings:** `DYNO_MEMORY_STABILITY_WINDOW` (seconds, default 300), `DYNO_MEMORY_STABILITY_MIN_READINGS` (int, default 6), `DYNO_MEMORY_STABILITY_TOLERANCE_MB` (MB, default 50), `DYNO_MEMORY_STABILITY_TOLERANCE_PCT` (%, default 1 — used when larger than TOLERANCE_MB), `DYNO_STABILITY_LOAD_THRESHOLD` (float, default 1.0 — formation-wide load avg below which formation is considered idle).
- **Improvement: formation-wide load gate for stability-based downscale.** `is_formation_idle` checks that *all* dynos in the formation (self + siblings via published `heroku:dyno_load:` cache keys) have a 1-minute load average below `DYNO_STABILITY_LOAD_THRESHOLD` (default 1.0). The stability override in `allow_downscale`, `allow_downscale_on_shutdown`, and the near-expiry forced-downscale path all require `is_formation_idle` to be True. This prevents downsizing the formation while a CPU-heavy task is still executing — even if RSS has already plateaued, the task could still be mid-computation. Each `check_in_dyno()` cycle publishes the dyno's load to Redis alongside memory, and `remove_dyno_from_alive_cache()` cleans the load key along with memory and stable keys.
- **Fix: near-expiry restart bypasses downscale when memory is stable.** When `allow_downscale` returned False only because the queue was non-empty (and `downscale_on_non_empty_queue=False`), the near-expiry path would restart the dyno — which resets RSS on the *same expensive tier* but never downsizes. Now detects the stability plateau independently and forces a downscale (resize = restart on smaller tier) instead of a pointless same-tier restart.
- **Fix: stale `dyno_memory_stable` key after dyno death.** `remove_dyno_from_alive_cache()` now also deletes the `heroku:dyno_memory_stable:` key, preventing a dead dyno's stale `stable=True` from incorrectly unblocking sibling downscale checks.
- **Fix: `_downscale_memory_threshold` edge case.** When the target size has `memory=0` (data corruption), the threshold now returns `inf` (blocks downscale) instead of `0` (which would pass the `>= threshold` check for any positive memory reading).
- **Refactor: DRY stability-override logic.** Extracted `_is_stability_eligible` property combining all preconditions for the RSS-plateau override (not requires_upscale, not at original size, is_memory_stabilized, is_formation_idle, no hot siblings). Used in `allow_downscale`, `allow_downscale_on_shutdown`, and the near-expiry forced-downscale path — eliminating the triplication of the same 5-clause condition. Added `_iter_sibling_values(metric)` generator to eliminate repeated cache.keys() + own-key-skip + cache.get() boilerplate from `any_sibling_still_high_memory`, `any_sibling_requires_upscale`, and `is_formation_idle`. Centralized per-dyno cache key prefixes into `_DYNO_CACHE_PREFIXES` tuple so `remove_dyno_from_alive_cache()` auto-cleans all metrics via a single loop — adding a new metric now requires only appending to the tuple.
- **Tests: 31 new tests** for memory stability detection (plateau/rising/no-data/burst/proportional tolerance), stability-based downscale override, formation idle gate (own load/sibling load/boundary), near-expiry forced downscale, R15 safety guard, sibling stability awareness, memory history recording/pruning, upscale+downscale history cleanup, alive cache cleanup, threshold edge case, and shutdown override. 320 total.

## 0.2.8 - 2026-05-01
- **Fix: formation stuck at upscaled tier when original_formation_size is lost.** When the baseline size is lost (Redis eviction, phantom clear), `_check_formation_on_startup()` was recording the current (already upscaled) size as baseline — e.g. standard-2x as "original". The phantom detector then saw formation=original, cleared everything, and `downscale_formation_to_original_size()` silently exited because `original_formation_size` was None. Formation stayed stuck at standard-2x indefinitely with zero log output. Fix: startup now records the *previous tier* as original when the formation is above the lowest tier (e.g. standard-2x → records standard-1x) and restores the downscale timer. `downscale_formation_to_original_size()` also falls back to `previous_formation_size` when original is missing. `set_original_formation_size(value=)` now correctly stores explicit string values. Production impact: floship-naf normal_worker stuck on standard-2x for 8+ hours at 41% memory (425 MB on 1024 MB quota) instead of downscaling to standard-1x.
- **Tests: 12 new tests** (5 startup recovery, 3 downscale fallback, 2 set_original_formation_size, 2 end-to-end phantom→recovery). 290 total.

## 0.2.7 - 2026-04-29
- **Fix: sibling-triggered chain upscale.** When a hot dyno's autoscale thread stalls under memory pressure, cool siblings now detect the hot sibling via `any_sibling_requires_upscale` (checks sibling memory keys in Redis against the upscale threshold for the current tier) and trigger chain upscale on its behalf. `autoscale()` now checks `requires_upscale or any_sibling_requires_upscale`. The chain guard in `upscale_formation_to_next_level()` also checks sibling memory, so a cool dyno with `upscale_until` set can advocate for a hot sibling.
- **Tests: 12 new tests** (7 for `any_sibling_requires_upscale` property, 5 for sibling-triggered autoscale + chain guard). 277 total.

## 0.2.6 - 2026-04-29
- **Fix: allow chain upscale when memory is genuinely above threshold on current tier.** `upscale_formation_to_next_level()` unconditionally blocked chaining (standard-2x → performance-m) when `upscale_until` key existed — even when a dyno was at 213% memory (2183 MB on 1024 MB quota). The guard now checks `current_memory_usage_percentage > UPSCALE_PERCENTAGE_HIGH_MEM_USE` before blocking; if memory is above threshold, the chain upscale proceeds. Production impact: normal_worker stuck at standard-2x for hours with continuous R14 (230 events/day) instead of scaling to performance-m.
- **Tests: 6 chain upscale tests** covering threshold boundary, stale R15 guard, R15+hot memory, production scenario, and regression for normal upscale path.

## 0.2.5 - 2026-04-28
- **Fix: phantom upscale_until key trapping formation in infinite extend loop.** When a sibling downscaled the formation back to its original size (standard-1x) between an upscale API call and its `cache.set()`, the `upscale_until` key from the previous phantom cycle survived. Other siblings then kept "Extending the time to stay upscaled" even though the formation was already at its original size — blocking legitimate R15→upscale responses. Production impact: all 6 normal_workers cycling through R14/R15 every ~10 minutes with 1200+ error events/day. Fix adds `is_on_original_formation_size_or_lower` checks in both the `upscale_until` handler and the expired-key guard to detect and clear phantom upscale state.
- **Tests: 40 regression tests for phantom upscale state.** Covers phantom detection on original size (4 scenarios incl. non-base tier, below-original), phantom clear loop (5 scenarios incl. high-TTL, both-keys-deleted), phantom-clear→R14 restart fallthrough (4 scenarios), legitimate upscale preservation (3 scenarios incl. multi-level perf-m), near-expiry downscale/restart (2), bottom guard (3), startup interaction (3), phantom-clear→R15 upscale (2), multi-sibling clearing (2), full production race sequence (3), `is_on_original_formation_size_or_lower` property (6), and `can_be_upscaled` compound guard (3).

## 0.2.4 - 2026-04-28
- **Fix: treat `R15` as the hard no-downscale signal.** `requires_upscale` no longer escalates solely on `R14`; only memory above the configured upscale threshold or an active `R15` now forces an upscale decision.
- **Fix: ignore `R14` in downscale guards when memory is otherwise safe.** `allow_downscale` and `allow_downscale_on_shutdown` no longer block a resize just because an `R14` marker was seen.
- **Fix: preserve the keep-upscaled window until the final check interval.** When a formation has recently upscaled, low post-restart memory no longer causes an immediate downscale; the formation stays upscaled until the TTL is near expiry, and an active `R15` still prevents downscale in that final window.
- **Tests: add regression coverage for `R14` being ignored below threshold and for `R15` blocking downscale near TTL expiry.**

## 0.2.3 - 2026-04-28
- **Security: shell injection fix in `exec_connect`.** Command argument is now quoted via `shlex.quote()` before being passed to `bash -c`, preventing injection via untrusted formation names or commands.
- **Fix: P0-1 — sibling memory glob wildcard.** `cache.keys()` pattern was `heroku:dyno_memory:{name}.` (no wildcard), so it never matched any keys. Fixed to `{name}.*`.
- **Fix: P0-2 — split shared `_stop_event`.** A single threading event was shared between the autoscale and file-cleaning threads; stopping one would stop both. Replaced with `_stop_autoscale_event` and `_stop_file_cleaning_event`.
- **Fix: P0-3 — non-blocking rate limiter.** `_acquire_rate_limit_token()` no longer sleeps 195 s per attempt when the budget is exhausted. It logs a warning and returns `False`, letting the caller skip one cycle instead of blocking the thread.
- **Fix: P0-5 — `_get_proc_class_by_formation_name` `@cached_property` caches `None`.** Changed to `@property` so a `None` result from a race-condition first call is not permanently cached.
- **Fix: P0-6 — `stop_continuous_autoscale()` guarded in scale methods.** `upscale_formation_to_next_level` and `downscale_formation_to_original_size` were unconditionally stopping the autoscale thread, killing remote-monitoring-only deployments. Both now guard with `if not self.remote_monitoring:`.
- **Fix: P0-8 — `increment_dyno_counter` mixed return type.** Now always returns `int` (0 on restart, incremented counter otherwise). Was returning the string `'restarted'` on the restart path.
- **Fix: P0-9 — zero downscale threshold permits unconditional downscale.** When no original or previous formation size is known, `_downscale_memory_threshold` returns `float('inf')` and `is_still_high_memory_usage_for_downscale` uses `math.isinf()` to block the downscale safely.
- **Fix: P1-1/P1-2 — `set_upscaling()` TTL extended; `clear_upscaling()` added.** TTL is now `DYNO_TIME_BETWEEN_SCALES + 150` s to cover the full lock window. Error paths in `upscale_formation_to_next_level` now call `clear_upscaling()` instead of leaving a dangling lock.
- **Fix: P1-3 — TOCTOU in rate limiter.** `cache.incr()` return value is now checked against the rate limit (instead of a separate `cache.get()`), closing a race window where the limit could be exceeded between get and incr.
- **Fix: P1-4 — `is_on_original_formation_size_or_lower` side effect removed.** Now uses a direct size comparison instead of updating the previous-size cache as a side effect.
- **Fix: P1-5 — `allow_downscale` checked eagerly in `check_and_downscale`.** Was only checked near TTL expiry; hot-memory dynos could slip through the guard on the initial TTL-not-yet-expired path.
- **Fix: P1-6 — counter not deleted on failed restart.** `increment_dyno_counter` now only deletes the counter key if `restart_dyno()` returns `True`.
- **Fix: P1-7 — `check_in_dyno` zero-memory guard.** Changed `if mem:` to `if mem is not None:` so a valid reading of 0 MB is stored, not silently dropped.
- **Fix: P1-8 — stuck-True R14/R15 detection.** `extract_latest_metric` now always writes cache for `bool` results (both `True` and `False`), preventing a latched `True` when the error condition clears.
- **Fix: P1-9 — logplex fetch error handling.** `get_heroku_logs` now wraps the logplex URL fetch in `try/except (SSLError, ConnectionError, Timeout)` with `timeout=30`.
- **Fix: P1-10 — `hasattr` replaced with `getattr(..., default)`.** All settings presence checks now use `getattr` with safe defaults throughout `autoscale()`, `check_and_downscale`, `restart_dyno`, and zombie check.
- **Fix: P1-11 — mutable default `custom_headers={}`.** Changed to `custom_headers=None`, resolved to `{}` inside the function body.
- **Fix: P1-12 — extracted `_FORMATION_DEPENDENT_CACHE` + `_invalidate_formation_dependent_cache()`.** Centralises the set of keys that must be cleared after a scale operation (DRY).
- **Fix: P1-13 — `settings` variable shadowed in `get_dyno_settings` loop.** Loop variable renamed to `size_info`; method now returns `dict(size_info)` copy.
- **Fix: P1-14 — cache-age guard uses `ttl > 0` sentinel.** Replaced the ambiguous `ttl or 0` pattern which treated a TTL of `0` as missing.
- **Fix: P2-1 — `formation_size` instance cache TTL.** Changed from `DYNO_AUTOSCALE_INTERVAL * 1` to `* 5` to reduce redundant API calls.
- **Fix: P2-2 — `check_and_clean_old_files` removed from autoscale loop.** Was duplicated in both `_run_continuous` and `_run_continuous_file_cleaning`; now only in the file-cleaning thread.
- **Fix: P2-3 — `set_threads_used` cache TTL.** Reduced from 1 hour to `DYNO_AUTOSCALE_INTERVAL * 2` so the value reflects recent thread counts.
- **Fix: P2-4 — removed unused `import subprocess` / `from subprocess import run`.**
- **Fix: P2-5 — `_ApiResult` promoted to module level.** Was an inner class inside `exec_connect`; promoted to a module-level dataclass.
- **Fix: P2-9 — lock key collision in zombie check.** Changed from `heroku:dyno_alive` (conflicts with check-in keys) to `heroku:lock:dyno_alive_check`.
- **Fix: P2-10 — `get_dyno_settings` returns a copy.** Prevents callers from mutating the live settings dict.
- **Fix: P2-11 — double-colon typo in `threads_used_cache_key`.**
- **Fix: P2-B — `timezone.timedelta` → `timedelta`.** Added `from datetime import timedelta` and replaced all `timezone.timedelta(...)` calls.

## 0.2.2 - 2026-04-28
- **Fix: block downscale after `upscale_until` expiry when the formation is still hot.** If the keep-upscaled timer had already expired, `check_and_downscale_to_original_formation_size()` could fall through to an unconditional downscale even while `allow_downscale` was still false. The timer is now restored instead of shrinking the formation while memory or R14 pressure is still active.
- **Fix: downscale threshold now uses the original target size, not the intermediate previous size.** Performance-M workers returning to Standard-1X were previously compared against a Standard-2X threshold, which allowed a premature downscale around ~1 GB usage and immediately triggered R14 on the smaller formation.
- **Tests: add regression coverage for both failure modes.** New tests reproduce the expired-TTL downscale path and the wrong-threshold calculation so the bug cannot regress silently.

## 0.2.1 - 2026-04-28
- **Fix: sibling-memory guard prevents premature formation downscale.** A low-memory dyno could trigger a formation downscale while sibling dynos were still running above the downscale threshold. Each dyno now publishes its own memory to Redis (`heroku:dyno_memory:{dyno_name}`) on every check-in; `allow_downscale` and `allow_downscale_on_shutdown` both gate on `any_sibling_still_high_memory` before resizing the formation.
- **Fix: stale crashed-dyno memory keys.** Memory keys now use `DYNO_ZOMBIE_THRESHOLD` as TTL (instead of a hardcoded 24 h) so a crashed dyno's stale key expires at the same point the zombie detector fires — preventing an indefinite downscale block.
- **Fix: `allow_downscale_on_shutdown` now checks sibling memory.** A graceful shutdown removes the dying dyno's memory key before the formation resize; the sibling guard prevents that resize from executing while hot siblings are still alive.
- **Fix: `cache.keys()` formation pattern uses literal dot prefix.** `heroku:dyno_memory:normal_worker.` (dot suffix) instead of `normal_worker.*` (wildcard) prevents `normal_worker_extra.1` from being matched as a sibling of `normal_worker`.
- **Refactor: extracted `_downscale_memory_threshold` property (DRY).** Both `is_still_high_memory_usage_for_downscale` and `any_sibling_still_high_memory` share one threshold calculation.
- **Tests: 48 unit tests** covering threshold math, R14/R15 interactions, sibling guard, shutdown guard, TTL correctness, formation name isolation, null-memory safety, base-size edge cases, threshold boundary semantics, and queue gating.

## 0.2.0 - 2025-02-23
- **Major: Shared Redis API rate limiter.** All Heroku Platform API calls now go through `call_heroku_api()` with a per-app sliding-window rate limiter (default 50 req/min, configurable via `HEROKU_API_RATE_LIMIT_PER_MINUTE`). Prevents 429 errors when many dynos poll concurrently.
- **429 back-off.** `call_heroku_api()` now reads the `Retry-After` header on 429 responses and backs off automatically before retrying (up to 5 attempts).
- **Route all API calls through central method.** `upscale_formation_to_next_level`, `downscale_formation_to_original_size`, `restart_dyno`, `exec_connect`, and `get_heroku_logs` now use `call_heroku_api()` instead of raw `requests.*` calls. This ensures every call is rate-limited, retried, and logged consistently.
- **Roll back API-based formation size detection.** `formation_size` no longer calls the Heroku API every cycle. Instead it uses DYNO_RAM env var (set by Heroku at boot) with Redis cache updates on scale operations. Saves ~1 API call per dyno per cycle.
- **Jitter on autoscale loop.** `_run_continuous()` now adds ±20% jitter to the sleep interval to prevent thundering-herd API bursts across dynos.
- **Increase default log cache TTL** from 60 s to 90 s. Ensures logs are not re-fetched every cycle even if `DYNO_LOGS_CACHE_DURATION` is not explicitly configured.
- **Remove tenacity dependency** from `get_heroku_logs` (retries are now handled by the central `call_heroku_api` method).
- **New setting:** `HEROKU_API_RATE_LIMIT_PER_MINUTE` (default 50) — shared budget across all dynos for a given Heroku app.

## 0.1.6 - 2025-06-28
- **Critical fix**: `formation_size` now queries Heroku API as source of truth instead of relying on `DYNO_RAM` env var. Prevents workers from getting permanently stuck on upscaled sizes after restart.
- Convert `next_formation_size`, `previous_formation_size`, `available_memory` from `@cached_property` to `@property` so they reflect actual formation size changes.
- Fix `is_upscaling` and `is_downscaling` properties that were missing `return` statements (guards against rapid scaling were silently broken).
- Add startup safety check to detect workers stuck in upscaled state (e.g., due to Redis key loss) and automatically restore the downscale timer.
- Add `_invalidate_formation_cache()` to clear stale cached values after successful scale operations.

## 0.1.5 - 2026-02-17
- Retry on 5xx Heroku API errors in `get_heroku_logs` with exponential backoff (up to 5 attempts).
- Catch `HTTPError` separately: re-raise 5xx for retry, log 4xx as error.
- Downgrade `RequestException` log level from error to warning for transient network issues.

## 0.1.4 - 2025-12-21
- Reduce autoscaler and housekeeping logs to debug to cut info noise.
- Improve Heroku log session error handling and messaging (FP-16794).
- Add retry handling for UrlFieldFileOpenError during notify_three_pl (FP-16366).
- Enforce max dyno size caps during upscale and guard downscale timing; restart protection when cache expires.
- Harden /tmp cleaning with exclusions, safer commands, and retries; shorten dyno error timeout window.
- Fix original formation size handling, shutdown behavior, and assorted stability fixes.

## 0.1.3 - 2025-07-01
- Introduce automatic /tmp file cleaning for Heroku dynos.
- Update settings defaults and README guidance for the cleaner.

## 0.1.2 - 2025-06-04
- Add dyno counter with restart-on-threshold safeguard and TTL handling.

## 0.1.1 - 2025-06-02
- Adjust worker settings mapping.
- Initial dyno counter with restart hook.

## 0.1.0 - 2025-05-27
- Initial release of heroku_manager with autoscaling foundation.
