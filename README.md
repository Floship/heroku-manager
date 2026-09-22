# Heroku Manager

A Python package for managing Heroku dynos with autoscaling capabilities.

## Indexed dyno registry (FP-17968)

Since 0.2.12 every dyno check-in writes the full dyno name into the app-scoped
Redis sorted set `heroku:dynos:v1:{app}` (score = check-in epoch) and graceful
cleanup removes it. Since 0.2.13 the sibling-memory, formation-idle, and zombie
readers consume that index instead of Redis `KEYS`. The package never issues a
Redis `KEYS` command at runtime.

### Readiness and the indexed registry (Phase C)

Since 0.2.14 the app ZSET is the only member list: the compatibility `SCAN` and
its rollout flag are removed, and the package issues neither `SCAN` nor `KEYS`
at runtime.

Runtime readiness is the current dyno's own fresh index score: the dyno must
appear in the app ZSET with a score newer than `DYNO_ZOMBIE_THRESHOLD`. Every
uncertainty fails closed, so no downscale, no formation-idle downscale
authorization, and no zombie restart; check-ins and safe local/sibling
upscales remain allowed. Sibling readers are index-only as well: an uncertain
member read yields nothing rather than a partial fleet.

A registry write that raises publishes `heroku:dynos:v1:{app}:degraded` for
two autoscale intervals and readiness fails closed for the whole app until a
later check-in proves the writer works again. An evicted ZSET re-converges
within one autoscale interval, because every dyno re-registers at its own
check-in. Readiness reads the marker and its own score through the write
client, never a replica, and the destructive gates re-read the marker uncached
at the decision point, so a marker published after a readiness check still
stops the resize. The sibling metric reads keep the read client.
A registry write that raises records the dyno name in
`heroku:dynos:v1:{app}:degraded`, and readiness fails closed for the whole app
while that ledger names a live dyno. A check-in that finds the member list
missing - first deploy, or an eviction - marks
`heroku:dynos:v1:{app}:reconverging` for two autoscale intervals, because the
fleet only re-registers over the next interval. Readiness and the destructive
gates read both markers uncached through the write client, never a replica, so
a failure recorded after a readiness check still stops the resize. A ledger
entry clears only on proof: an index score newer than the recorded failure, or
a missing liveness key. The sibling metric reads keep the read client.

Deploy prerequisites, all of which held for every Floship app at release time:

1. v0.2.13 or later (index-aware writers) runs on every app for at least one
   full `DYNO_ZOMBIE_THRESHOLD` window, so no live dyno can be missing from
   the index.
2. The app ZSET exists and the fresh members contain every dyno name
   `heroku ps` reports.

Rollback is the previous tag: v0.2.13 keeps its bounded compatibility `SCAN` in
the not-ready window.

After deploying, verify per app:
- no `SCAN` and no `KEYS` entries in the Redis slowlog for heroku-manager;
- fresh v1 index members match the `heroku ps` expected dyno names (parity);
- a cool formation still downscales and a stale sibling is restarted once.

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
