# Changelog

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
