# Changelog

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
