# Changelog

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
