# Heroku Manager

A Python package for managing Heroku dynos with autoscaling capabilities.

## Indexed dyno registry (FP-17968)

Since 0.2.12 every dyno check-in writes the full dyno name into the app-scoped
Redis sorted set `heroku:dynos:v1:{app}` (score = check-in epoch) and graceful
cleanup removes it. Since 0.2.13 the sibling-memory, formation-idle, and zombie
readers consume that index instead of Redis `KEYS`. The package never issues a
Redis `KEYS` command at runtime.

### Readiness flag (default false)

Set `HEROKU_DYNO_INDEX_V1_READY=true` in an app's environment only after the
staged rollout proof passes for that app:

1. Deploy 0.2.13 with the flag unset (writers are unchanged; the compatibility
   reader uses a bounded `SCAN` with exact-prefix filtering).
2. Let at least two autoscaler check-in cycles run, obtain the expected active
   autoscaled dyno names from the Heroku Platform API/`heroku ps`, and confirm
   the fresh v1 index members contain every expected dyno in both cycles.
3. Set the flag. Runtime readiness additionally requires the current dyno to
   have a fresh v1 score and every compatibility-SCAN member to be represented
   in the index.

The flag defaults to false, and destructive actions are never authorized
without runtime readiness. When the flag is false/absent, or when it is set
but readiness is uncertain, incomplete, or errored, the package fails closed:
no downscale, no formation-idle downscale authorization, and no zombie
restart. Check-ins and safe local/sibling upscales remain allowed, and the
bounded compatibility SCAN stays available only to those non-destructive
readers. Clearing the flag is the immediate rollback; the compatibility scan
remains until a later Phase C release removes it.

## Features

- Automatic scaling of Heroku dynos based on memory usage and load
- Dyno health monitoring (R14/R15 detection, load averages, memory quota)
- Automatic restart of unresponsive or zombie dynos
- Max dyno size guardrails with timed upscale/downscale windows
- Dyno restart counters with threshold-based protection
- Integration with Django for caching and configuration
- Automatic cleaning of old files in specified directories via Heroku exec (with safe exclusions)

## Installation

```bash
pip install heroku-manager
```

Enable Heroku runtime metrics (required for memory/load signals):

```bash
heroku labs:enable log-runtime-metrics -a <app_name>
```

## Usage

### Basic Autoscaling

```python
from heroku_manager import HerokuManager

# Get the autoscaler instance
autoscaler = HerokuManager.get_autoscaler()

# Start continuous autoscaling
autoscaler.start_continuous_autoscale()

# Manual scaling operations
if autoscaler.requires_upscale:
    autoscaler.upscale_formation_to_next_level()
elif autoscaler.allow_downscale:
    autoscaler.downscale_formation_to_original_size()

# Stop autoscaling
autoscaler.stop_continuous_autoscale()
```

### Restart Counter Safeguard

Automatically restart a dyno after N events (e.g., failures) while respecting a TTL:

```python
from heroku_manager import HerokuManager

autoscaler = HerokuManager.get_autoscaler()

# Increment counter; if threshold is reached the dyno is restarted automatically
autoscaler.increment_dyno_counter()
```

### File Cleaning

The package can automatically clean old files in specified directories on Heroku dynos. This is useful for preventing disk space issues.

To enable automatic file cleaning, set the following in your Django settings:

```python
# Enable file cleaning
DYNO_FILE_CLEANING_ENABLED = True

# Clean files older than 48 hours (default)
DYNO_FILE_AGE_THRESHOLD = 48

# Clean files every 24 hours (default)
DYNO_FILE_CLEANING_INTERVAL = 24 * 60 * 60

# Directory to clean (default: /tmp)
DYNO_FILE_CLEANING_DIRECTORY = '/tmp'
```

You can also manually clean files:

```python
from heroku_manager import HerokuManager

# Get the autoscaler instance
autoscaler = HerokuManager.get_autoscaler()

# Clean files in /tmp older than 48 hours
autoscaler.clean_old_files(directory='/tmp', hours=48)
```

### One-off Exec Helper

Run a command on a one-off dyno using the Heroku API (uses the current formation size):

```python
result = autoscaler.exec_connect(command="echo 'hello' && env")
```

## Configuration

Environment variables:
- `HEROKU_API_KEY` (required): Heroku API key.
- `HEROKU_APP_NAME` (required): Heroku app name.
- `DYNO` (auto-set on Heroku): Current dyno name, used for formation detection.
- `DYNO_RAM` (optional): Force the formation size via memory mapping when API calls are unavailable.

Operational notes:
- Enable Heroku runtime metrics (see Installation) so memory/load signals are available.
- Autoscaling and cleaning rely on Django cache; ensure your cache backend is configured and shared across dynos.

### Indexed Dyno Registry Rollout

Version 0.2.12 starts the staged registry rollout by dual-writing each dyno's
full name and latest check-in timestamp to the app-scoped Redis sorted set
`heroku:dynos:v1:{HEROKU_APP_NAME}`. Graceful dyno removal also removes that
member. The key is passed through the configured cache backend's `make_key()`
before raw Redis commands are used.

This release does not change existing metric keys or readers, including their
compatibility scans. Registry write failures are logged but do not interrupt
the existing check-in or cleanup path. Deploy this writer across every app and
wait two full `DYNO_ZOMBIE_THRESHOLD` cycles before a later release enables
indexed readers or marks the registry ready.

## Django Settings

When used with Django, the following settings are available:

### Autoscaling Settings
- `DYNO_CONTINUOS_AUTOSCALE_ENABLED`: Enable continuous autoscaling
- `DYNO_AUTOSCALE_INTERVAL`: Interval between autoscale checks (in seconds)
- `DYNO_TIME_BETWEEN_SCALES`: Minimum time between scaling operations (in seconds)
- `DYNO_MIN_UPSCALE_DURATION`: Minimum duration to keep a dyno upscaled (in seconds)
- `DYNO_DOWNSCALE_CHECK_INTERVAL`: Interval for checking if a dyno can be downscaled (in seconds)
- `DYNO_ZOMBIE_THRESHOLD`: Time threshold for considering a dyno as unresponsive (in seconds)
- `DYNO_LOG_THREADS_USED`: Whether to log the number of threads used
- `DYNO_LOGS_CACHE_DURATION`: Duration to cache dyno logs (in seconds)
- `DYNO_ERRORS_TIMEOUT_DURATION`: Duration to cache error information (in seconds)
- `DYNO_GENERAL_CACHE_DURATION`: General cache duration (in seconds)
- `DYNO_TIME_BETWEEN_RESTARTS`: Minimum time between dyno restarts (in seconds)
- `DYNO_AUTOSCALE_ENABLED_FOR_BEATWORKER`: Whether to enable autoscaling for beat workers
- `UPSCALE_PERCENTAGE_HIGH_MEM_USE`: Memory usage percentage threshold for upscaling
- `DOWNSCALE_PERCENTAGE_HIGH_MEM_USE`: Memory usage percentage threshold for downscaling
- `HIGH_MEM_USE_MB`: High memory usage threshold in MB
- `DYNO_RAM`: Memory size (MB) for mapping to formation when API access is limited
- `WORKER_SETTINGS_MAP`: Per-formation overrides (e.g., max_dyno_size, downscale_on_non_empty_queue)

### File Cleaning Settings
- `DYNO_FILE_CLEANING_ENABLED`: Enable automatic cleaning of old files (default: False)
- `DYNO_FILE_CLEANING_INTERVAL`: Interval between file cleaning operations in seconds (default: 24 hours)
- `DYNO_FILE_AGE_THRESHOLD`: Age threshold for files to be deleted in hours (default: 48 hours)
- `DYNO_FILE_CLEANING_DIRECTORY`: Directory to clean (default: /tmp)

### Restart / Counter Settings
- `DYNO_RESTART_THRESHOLD`: Counter threshold that triggers a restart (default: 15)
- `DYNO_COUNTER_TTL`: TTL (seconds) for the counter key (default: 3600)
- `DYNO_TIME_BETWEEN_RESTARTS`: Minimum time between restarts for a dyno (default: 300)

## Dyno Size Mapping

The package ships with a memory-based formation map (`DYNO_SIZES`) including standard/performance tiers (1x, 2x, m, l, l-ram, xl, 2xl) and their RAM, thread hints, and pricing metadata. You can override per-formation behavior via `WORKER_SETTINGS_MAP`.

## Logging

- Routine autoscaler/file-cleaning stats now log at debug to reduce noise. Raise your log level to debug when troubleshooting.
- Errors and warnings remain at higher levels for visibility.

## Changelog

See [CHANGELOG.md](CHANGELOG.md) for release history.
