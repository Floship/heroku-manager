# Changelog

## 0.1.4 - 2025-12-21
- Reduce autoscaler and housekeeping logs to debug to cut info noise.
- Improve Heroku log session error handling and messaging (FP-16794).
- Enforce max dyno size caps during upscale and guard downscale timing.
- Make /tmp cleanup safer with directory exclusions and shorter error timeouts.
- Add retry handling for UrlFieldFileOpenError during notify_three_pl.
- Minor fixes and stability improvements.
