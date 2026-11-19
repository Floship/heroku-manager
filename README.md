# Heroku Manager

A Python package for managing Heroku dynos with autoscaling capabilities.

The base package keeps its `Requires-Python >=3.6` metadata. Release checks
import the base package on CPython 3.6 and test the optional proxy extra on the
configured 3.13 and 3.14 runtimes.

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
upscales remain allowed.

Since 0.2.17 every successful check-in also writes one score-bound sample at
`heroku:dyno_observation:v1:{app}:{dyno}`. The value is `(check_in_epoch,
memory_mb_or_none, stable, load_1m_or_none)`. The writer registers the dyno in
the ZSET before telemetry sampling and publishes the complete tuple with one
cache write after registration. A slow metric read therefore cannot hide a
joining worker. A dyno visible before the tuple exists fails closed. If the
publication is rejected before any write because the owner or score changed,
the previous legacy keys remain unchanged and strict readers stay closed until
a later check-in. An error after the write was attempted leaves the outcome
uncertain, so readers still require a later successful owner-bound check-in.
The tuple has the same
`DYNO_ZOMBIE_THRESHOLD` lifetime as the index entry. The observation key is
shared by app and dyno name. Its
`check_in_epoch` binds the value to the current registration, while the raw
telemetry keys carry the verified dyno incarnation.

One owner lease at `heroku:dyno_owner:v1:{app}:{dyno}` controls each app-scoped
dyno name. The lease lasts `max(60, 3 * DYNO_AUTOSCALE_INTERVAL)` seconds. A
process atomically claims or renews the lease and reserves its registry score
before sampling telemetry. Publication requires the exact owner token and
reserved score still to match. A delayed predecessor therefore cannot overwrite
its replacement, and a process that loses or replaces its lease stays fenced
until restart. Graceful cleanup uses the owner and score captured before
dispatch, while named cleanup re-reads both before its atomic delete. Strict
reads require a current owner for every formation member. A verified dyno UUID
is the self-monitoring token. A process fallback token remains stable for one
`HerokuDyno` object and changes after a fork changes the PID.

A second process that finds another owner writes a bounded marker at
`heroku:dyno_contention:v1:{app}:{dyno}`. Strict readers reject the formation
until that marker expires or the conflict clears. Remote monitoring cannot
claim a local dyno lease or remove its own registration. It keeps the legacy
diagnostic reads.

Immediate memory pressure uses the same owner boundary.
`record_memory_pressure()` writes a veto, the legacy memory value, and a legacy
unstable value in one owner-checked transaction. The veto lasts at least as long
as the observation it protects. Strict readers reject the formation while it is
present. The next valid memory sample from the current owner clears the veto
only when its source epoch is newer than the nudge. That first replacement
sample stays unstable, so a hot reading cannot inherit a plateau exemption.
Portal prefers this method and keeps the legacy write only for v0.2.16.
The veto write watches both the owner and the current veto. If two nudges finish
out of order, the older epoch is rejected and cannot replace the newer one.

The heartbeat at `heroku:dyno_alive:{dyno}` lasts `DYNO_ZOMBIE_THRESHOLD` plus
one owner-lease grace period. That keeps the stale timestamp available for the
next scheduled scan after the threshold. The observation tuple, memory, and
load keys keep the threshold TTL. Restart evaluation still uses the threshold,
while the registry prune waits through the heartbeat grace on both ready and
readiness-failure scans.

Automatic downscale reads the primary's scored formation roster and batches the
observation keys once. Every member needs an exact score match and a usable
memory value. The stability path also needs a usable load value for every
member from that same batch. A genuine zero load is usable. Missing or expired
values, old workers without observation tuples, an oversized value, a roster
change, or a new degradation marker blocks the resize. Strict metric reads keep
the source timestamp with the cached value and ignore the legacy scalar cache.
A log line older than `DYNO_GENERAL_CACHE_DURATION` cannot be stamped with a new
check-in. After validation, the reader checks the scored roster with a fresh
cutoff and checks degradation again before accepting.

The ordinary and stability paths share one roster, one observation batch, and
one watched primary read for the final roster, owners, contention markers, and
degradation state. A changed snapshot denies both paths for that check-in. The
near-expiry path cannot replace a rejected snapshot with a second stability
read in the same decision.

The legacy memory, stability, and load keys remain published for older readers
and rollback. Missing measurements are not replaced with the old zero-memory or
0.4-load fallbacks in the strict tuple. Advisory upscale keeps the legacy
best-effort reader and skips malformed samples individually.

Self-monitoring metric and history keys also include the verified
`HEROKU_DYNO_ID`. A numeric runtime sample must name this dyno and carry the
same UUID in `dyno=heroku.<scope>.<uuid>`. A local process memory reading can
still fill a missing memory sample. Remote monitoring keeps its legacy
diagnostic reads, while strict observations reject unverified log samples.
Memory history is deduplicated by source epoch, so one cached log sample cannot
count repeatedly toward a plateau. An out-of-order epoch is rejected and
publishes an unstable sample. A local memory reading is stamped when sampled.
Strict cache hits use the retained source epoch for freshness and skip the
legacy cache TTL read. The fallback process identity starts with fresh history.
Strict observations use a separate raw-log cache capped by the autoscale
interval. Memory and load share one log-session fetch during a check-in. A
failure is memoized for that check-in, so a missing memory reading cannot cause
a second request for load. A standalone strict read does not keep the failure
memoized after that request, so the next metric request can recover. The legacy
cache keeps its prior behavior.

Both formation resize methods read the primary formation tier before taking a
lock, writing a resize flag, or sending a PATCH. A process whose `DYNO_RAM`
tier no longer matches the primary tier defers the resize. This covers the
normal autoscale loop and direct shutdown calls. A failed primary read also
defers the mutation. A primary value that is invalid, undecodable, or decodes
to `None` also defers. A successful cache miss keeps the previous instance-tier
fallback, and remote monitoring or an unknown local tier keeps its prior
behavior.

A downscale flag now stores a unique attempt token. A definite rejection clears
only the token owned by that attempt, so late cleanup cannot remove a newer
attempt's flag. Proxy responses that require reconciliation retain the flag and
pending intent. Direct transport timeouts, server errors, and other ambiguous
responses also retain it. A later direct 4xx does not clear the flag after an
earlier timeout because the first request may have reached Heroku.

`downscale_formation_if_allowed()` evaluates one decision and passes its
snapshot into the resize. The managed path reserves the flag under the same
primary Redis WATCH that validates the roster, owners, contention markers,
memory vetoes, degradation state, formation tier, captured downscale target,
and existing flag. The target captured for the memory decision also supplies
the PATCH size. If the baseline target changes before reservation, no PATCH is
sent. The carried target and snapshot are validated before an at-or-below
cleanup, so a mismatch preserves the baseline and upscale timer. A roster join,
owner handoff, the first WATCH abort, or an unreadable state creates no flag.
The local reservation uses one WATCH/MULTI/EXEC transaction, with no retry after
an abort. The target fallback applies only to a genuinely absent baseline key.
A present value that
is invalid, undecodable, or decodes to `None` is unavailable. Upscale reserves
its flag only for the current owner. Managed local downscales always require
the guarded decision. Remote monitoring keeps the prior explicit manual
downscale entry point. Consumers that do not have the method continue to use
the v0.2.16 Boolean and direct-call path.

Scheduled phantom cleanup and the no-decision mutation path use one primary
compare-and-delete. `clear_formation_if_at_or_below()` uses type-strict `GET`s
for the raw primary tier, baseline, and upscale timer. The no-decision and
remote paths capture that tuple before acquiring the mutation lock and carry it
into the transaction. A present upscale timer must decode to a `datetime`;
malformed, wrong-type, or serialized-`None` timers are unavailable. A baseline
rewrite with a newer timestamp, a changed or invalid timer, a tier change, an
invalid value, a read failure, or a WATCH abort leaves the new state intact. A
genuine primary-tier miss keeps the known instance-tier fallback and requires
the tier key to remain absent through the transaction.

The zombie reader batches liveness values from the registry and before each
dispatch re-reads the stale sibling's owner, score, liveness, and contention
marker on primary Redis. A fresh check-in or live contention cancels the
restart. A successful restart uses the owner and score captured by that
revalidation for cleanup, so a delayed response cannot remove a replacement. A
contested singleton remains in the registry for the next scheduled retry.
The scan logs stale heartbeat evidence as detection. The restart message is
emitted only after Heroku accepts DELETE.

An expired owner lease does not erase a stale heartbeat. The bounded alive
value and registry score remain valid zombie evidence, so a dead worker can
still be restarted after its 90-second lease expires while the heartbeat key is
inside its threshold-plus-grace retention window. If the owner is absent,
restart dispatch remains allowed but cleanup is skipped because there is no
owner and score pair to delete conditionally.

Upscale and downscale recheck managed ownership after acquiring their mutation
locks. Zombie restart rechecks stale liveness after acquiring its restart lock.
If a replacement appears while a caller waits for a lock, the caller sends no
PATCH or DELETE and reserves no new mutation marker. Upscale samples and formats
its memory diagnostic before the final ownership check and flag reservation.

A registry write that raises records the dyno name in
`heroku:dynos:v1:{app}:degraded`, and readiness fails closed for the whole app
while that ledger names a live dyno. A check-in that finds the member list
missing - first deploy, or an eviction - marks
`heroku:dynos:v1:{app}:reconverging` for two autoscale intervals, because the
fleet only re-registers over the next interval. Readiness and the destructive
gates read both markers uncached through the write client, never a replica, so
a failure recorded after a readiness check still stops the resize. A ledger
entry clears only when the index score is newer than the recorded failure or
the liveness key is absent.

An exhausted registration WATCH is recorded in the same ledger. The joining
dyno's heartbeat is written inside the transaction that failed, so the reader
cannot require it. The ledger keeps a fresh failure closed for
`max(60, 3 * DYNO_AUTOSCALE_INTERVAL)` seconds. A newer score proves recovery.
After the grace, an absent heartbeat permits cleanup. The compare-and-delete
removes only the failure timestamp that was inspected, so a newer concurrent
failure survives. The ledger key expires after
`max(2 * DYNO_ZOMBIE_THRESHOLD, registration_grace + 1)` seconds, so Redis
cannot discard the failure before the reader's grace ends.

Deploy prerequisites, all of which held for every Floship app at release time:

1. v0.2.13 or later (index-aware writers) runs on every app for at least one
   full `DYNO_ZOMBIE_THRESHOLD` window, so no live dyno can be missing from
   the index.
2. The app ZSET exists and the fresh members contain every dyno name
   `heroku ps` reports.

Rollback for this release is v0.2.16. It does not use owner leases or strict
observation tuples. Pause destructive scaling and drain the 0.2.17 writers
before rolling back. The legacy memory, stability, load, and registry keys
remain compatible with that version.

After deploying, verify per app:
- no `SCAN` and no `KEYS` entries in the Redis slowlog for heroku-manager;
- fresh v1 index members match the `heroku ps` expected dyno names (parity);
- each fresh member has a current owner lease and a score-bound observation;
- a cool formation still downscales and a stale sibling is restarted once.

Before activating or rolling back 0.2.17, pause destructive scaling and drain
every old autoscaler-capable process and one-off. Include shutdown callers and
Portal's startup formation PATCH in that check. Wait a full 60 seconds after the
last possible write to `heroku:api_rate:{app}` before claiming corrected rate
accounting. Verify owner, registry, and observation coverage before resuming.
The previous version can reset the shared counter after a new caller initializes
it, so the old and new algorithms are not mixed safely.

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
else:
    autoscaler.downscale_formation_if_allowed()

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

Install `heroku-manager[proxy]` to use the encrypted proxy transport. Direct mode keeps the original dependencies.
The proxy extra requires Python 3.8 or later through [cryptography](https://pypi.org/project/cryptography/46.0.0/).
For local tests, install `python -m pip install -e '.[proxy]'`, then run `python -m unittest discover -s tests -t .`.

Environment variables:
- `HEROKU_PROXY_URL` (optional): HTTPS manager URL ending in `/v1/heroku`. Setting it selects the signed proxy transport.
- `HEROKU_PROXY_CLIENT_ID` (required with a proxy URL): The manager's public client identifier.
- `HEROKU_PROXY_KEY_ID` (optional, default `v1`): The configured signing-key version.
- `HEROKU_PROXY_SECRET` (required with a proxy URL): 32 random bytes in lowercase hexadecimal, shared with the manager for authentication.
- `HEROKU_PROXY_MANAGER_ID` (required with a proxy URL): The manager's canonical app UUID.
- `HEROKU_PROXY_CREDENTIAL` (required with a proxy URL): The provisioned `v2:` encrypted Heroku credential. Its unwrap key stays on the manager.
- `HEROKU_API_KEY` (used without a proxy URL): Heroku API key for the direct Platform API transport.
- `HEROKU_APP_NAME` (required): Heroku app name.
- `DYNO` (auto-set on Heroku): Current dyno name, used for formation detection.
- `HEROKU_DYNO_ID` (auto-set on Heroku): Current dyno incarnation, used to isolate self-monitoring metric and history caches.
- `DYNO_RAM` (optional): Force the formation size via memory mapping when API calls are unavailable.

Operational notes:
- `HEROKU_PROXY_URL` decides the transport. Without it, calls use `HEROKU_API_KEY` directly as before.
- A proxy URL with missing or invalid signing settings fails closed. It never falls back to `HEROKU_API_KEY`.
- Proxy requests carry a fresh encrypted credential capsule, a time-bound HMAC signature, and a unique nonce. The signing secret and fixed envelope are never sent plainly.
- The server's policy controls which operations the client may request, including one-off commands.
- Direct mode keeps the shared rate limiter, five-second GET cache, transient-error retries and 429 back-off.
- Uncertain mutations retain their request ID in shared Django cache and block automatic redispatch until reconciled.
- The manager reports the original uncertain response and includes the status, any recognized error code, and reconciliation handles.
- A later attempt for the same pending intent can alert after the grace period. The cache limits repeat alerts to once per hour when available. The original response always reports its own outcome.
- Log-session creation is a repeatable read and can recover from a failed request on the next interval.
- An expected proxy cooldown response with HTTP 429, `error=cooldown`, and a `Retry-After` from 1 through 86,400 seconds produces one warning and no error-level Sentry event. The parser trims only ASCII space and tab, rejects non-ASCII whitespace or digits, and sends malformed or oversized values through normal failure cleanup. Actual rate limits and unknown 429 responses keep their error handling.
- After verifying the provider outcome, use `reconcile_proxy_request(method, url, data, request_id)` to release that intent.
- If the original payload is unavailable, use `reconcile_proxy_request(request_id=..., pending_key=...)` with both logged handles after verifying the provider outcome.
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
