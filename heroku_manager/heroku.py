import math
import hashlib
import json
import os
import re
import shlex
import requests
import threading
import time
import random
import uuid
from collections import namedtuple
from datetime import datetime, timedelta
from requests.exceptions import SSLError, ConnectionError, Timeout

from django.conf import settings
from django.core.cache import cache
from django.utils.functional import cached_property
from django.utils.http import parse_http_date_safe
from django.utils import timezone
from importlib import import_module

import logging
from .proxy import HerokuProxy, ProxyResponse, host_size_write_denial, legacy_size_owner

logger = logging.getLogger(__name__)


def _is_log_session_request(method, url):
    return method.upper() == "POST" and re.fullmatch(
        r"(?:https://api\.heroku\.com)?/apps/[^/?#]+/log-sessions", url
    ) is not None


def _log_session_retry_delay(value):
    value = value.strip()
    if re.fullmatch(r"[0-9]{1,6}", value):
        return max(1, min(86400, int(value)))
    retry_at = parse_http_date_safe(value)
    if retry_at is None:
        return 60
    return max(1, min(86400, math.ceil(retry_at - time.time())))


try:
    from redis.exceptions import WatchError as _RedisWatchError
except ImportError:
    class _RedisWatchError(Exception):
        pass


def _index_ready(instance):
    """Phase C runtime readiness for the indexed dyno registry (fail closed).

    The app ZSET is the only member list: the compatibility SCAN was removed
    after the migration window, so every writer runs this package version or
    later.  Readiness is the current dyno's own fresh index score - a dyno that
    cannot prove its writer works must not authorize destructive actions
    (downscale, formation-idle downscale, zombie restart).  Any uncertainty
    fails closed; ordinary check-ins and safe upscale behavior stay available.
    """
    if not instance.app_name or not instance.dyno_name:
        return False
    cutoff = _stale_cutoff()
    try:
        # Readiness authorizes destructive actions, so its ledger and its own
        # score come from the write authority: a lagging replica must never
        # hide a sibling's failed registry write.
        adapter = _IndexAdapter(instance.app_name, primary=True)
        client = adapter._connect()
        if client is None:
            return False  # no raw client → cannot prove the index → fail closed
        if _registry_degraded(instance):
            return False  # a live sibling is missing from the member list
        owner_key = cache.make_key(_dyno_owner_key(instance.app_name, instance.dyno_name))
        current_owner = client.get(owner_key)
        try:
            expected_owner = adapter.encode(instance._dyno_owner_token)
        except Exception:
            return False
        if current_owner != expected_owner:
            return False
        own_score = client.zscore(adapter._index_key, instance.dyno_name)
        return own_score is not None and own_score > cutoff
    except Exception:
        logger.warning(
            "Indexed dyno registry readiness check failed for app %s; failing "
            "closed (no destructive actions).", instance.app_name, exc_info=True,
        )
        return False


def _index_fail_closed(instance):
    """True when index readiness is uncertain: destructive actions (downscale,
    formation-idle authorization, zombie restart) are only authorized when the
    current dyno proves a fresh index score. Sibling reads that authorize
    downscale also require complete metrics from the primary.

    The failed-writer ledger is read here, uncached: ``index_ready`` lives for
    one autoscale cycle, and a sibling can report a failed registry write after
    that cache was filled.
    """
    if getattr(instance, '_dyno_owner_fenced', False) or _registry_degraded(instance):
        return True
    return not instance.index_ready


def _stale_cutoff():
    """Epoch-seconds cutoff for fresh index members, from DYNO_ZOMBIE_THRESHOLD."""
    threshold = int(getattr(settings, 'DYNO_ZOMBIE_THRESHOLD', 24 * 60 * 60))
    return time.time() - threshold


_DynoObservation = namedtuple(
    '_DynoObservation',
    ('check_in_epoch', 'memory_mb', 'stable', 'load_1m'),
)

_MetricReading = namedtuple('_MetricReading', ('value', 'source_epoch'))

_DownscaleDecision = namedtuple(
    '_DownscaleDecision', ('normal', 'stability', 'snapshot', 'target_size'),
)


def _observation_key(app_name, dyno_name):
    return f'heroku:dyno_observation:v1:{app_name}:{dyno_name}'


def _dyno_owner_key(app_name, dyno_name):
    """One current self-monitoring writer for an app-scoped dyno name."""
    return f'heroku:dyno_owner:v1:{app_name}:{dyno_name}'


def _dyno_contention_key(app_name, dyno_name):
    """A competing incarnation that strict readers must fail closed on."""
    return f'heroku:dyno_contention:v1:{app_name}:{dyno_name}'


def _dyno_memory_veto_key(app_name, dyno_name):
    """A coherent in-process memory-pressure veto for one dyno."""
    return f'heroku:dyno_memory_veto:v1:{app_name}:{dyno_name}'


def _dyno_owner_ttl():
    """Lease duration that covers one check-in including a slow log fetch."""
    interval = int(getattr(settings, 'DYNO_AUTOSCALE_INTERVAL', 30))
    return max(60, interval * 3)


def _dyno_alive_ttl(zombie_threshold):
    """Keep the last heartbeat through one scheduled scan after staleness."""
    return int(zombie_threshold) + _dyno_owner_ttl()


def _memory_veto_ttl():
    """Keep a pressure veto at least as long as its pre-nudge observation."""
    return max(60, int(getattr(settings, 'DYNO_ZOMBIE_THRESHOLD', 24 * 60 * 60)))


def _registry_degraded_grace():
    """Fail closed while a first registration may not have written a heartbeat."""
    return max(60, int(getattr(settings, 'DYNO_AUTOSCALE_INTERVAL', 30)) * 3)


_STRICT_LOGS_UNSET = object()
# Distinguishes "this caller did not bind a timer" from "expect no timer key".
_UPSCALE_TIMER_UNBOUND = object()


def _is_finite_number(value, *, positive=False):
    """Return whether value is a real, finite number in the accepted range."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        finite = math.isfinite(value)
    except (OverflowError, TypeError):
        return False
    return finite and (value > 0 if positive else value >= 0)


def _canonical_uuid(value):
    """Return a canonical lowercase UUID or None for malformed input."""
    if not isinstance(value, str):
        return None
    try:
        return str(uuid.UUID(value))
    except (AttributeError, TypeError, ValueError):
        return None


_LOG_DYNO_SOURCE_RE = re.compile(
    r"\bsource=(?P<source_dyno_name>[^\s]+)\s+"
    r"dyno=heroku\.(?P<source_dyno_scope>[^\s.]+)\.(?P<source_dyno_id>[^\s]+)",
)


def _log_dyno_identity(message):
    """Extract the source dyno name and UUID from one runtime metric line."""
    match = _LOG_DYNO_SOURCE_RE.search(message or "")
    if not match:
        return None, None
    return (
        match.group("source_dyno_name"),
        _canonical_uuid(match.group("source_dyno_id")),
    )


class _IndexAdapter:
    """Private adapter around the django-redis raw client for the dyno index.

    Centralizes physical-key derivation (``cache.make_key``) and keeps all
    Redis access inside one seam.  Every read here is bounded: sorted-set
    range operations and batched MGET; no SCAN, no KEYS.
    """

    def __init__(self, app_name, primary=False):
        self.app_name = app_name
        self._client = None
        self._index_key = None
        self._decoder = None
        self._encoder = None
        # Readiness and downscale reads use the primary. Advisory upscale
        # reads may use a replica when the cache configures one.
        self._primary = primary

    def _connect(self):
        if self._client is None:
            try:
                backend = cache.client
                self._client = backend.get_client(write=self._primary)
                # The raw redis-py client has no decode(); the decoder lives on
                # the django-redis DefaultClient itself and turns serialized
                # bytes back into Python values.
                decoder = getattr(cache.client, "decode", None)
                if callable(decoder):
                    self._decoder = decoder
                encoder = getattr(cache.client, "encode", None)
                if callable(encoder):
                    self._encoder = encoder
            except AttributeError:
                # LocMem/unit-test backends expose no raw client; reads return
                # uncertain (None) and destructive actions fail closed. There
                # is no keys() fallback: KEYS is banned in production.
                self._client = None
                self._decoder = None
                self._encoder = None
                return None
            self._index_key = cache.make_key(f'heroku:dynos:v1:{self.app_name}')
        return self._client

    def decode(self, raw):
        """Decode one raw redis value through the django-redis client decoder.

        django-redis ``DefaultClient.decode`` turns serialized bytes back into
        Python ints/floats/datetimes; raw ints pass through unchanged.  MGET
        results must be decoded before callers compare them, never compared as
        bytes.
        """
        if raw is None:
            return None
        if self._decoder is None:
            # No real decoder available (unit-test backends or a raw client
            # without decode): return raw as-is.
            return raw
        try:
            return self._decoder(raw)
        except Exception:
            logger.warning(
                "Failed to decode raw redis value for app %s; treating as "
                "missing.", self.app_name, exc_info=True,
            )
            return None

    def encode(self, value):
        """Encode a high-level cache value for an atomic raw transaction."""
        if self._encoder is None:
            raise RuntimeError("cache encoder unavailable")
        return self._encoder(value)

    def _zrange(self, min_score, max_score):
        """Bounded ZRANGEBYSCORE, normalized to dyno-name strings."""
        client = self._connect()
        if client is None:
            return None
        members = client.zrangebyscore(self._index_key, min_score, max_score)
        if members is None:
            return None
        # redis-py returns ZSET members as bytes; normalize to dyno-name str.
        return [
            m.decode('utf-8', errors='replace') if isinstance(m, bytes) else m
            for m in members
        ]

    def fresh_members(self, cutoff):
        """Bounded ZRANGEBYSCORE(cutoff, +inf); fresh members only."""
        return self._zrange(f'({cutoff}', '+inf')

    def fresh_scored_members(self, cutoff):
        """Bounded fresh members with their check-in scores."""
        client = self._connect()
        if client is None:
            return None
        members = client.zrangebyscore(
            self._index_key, f'({cutoff}', '+inf', withscores=True,
        )
        if members is None:
            return None
        scored = []
        for member, score in members:
            name = member.decode('utf-8', errors='replace') if isinstance(member, bytes) else member
            scored.append((name, score))
        return scored

    def stale_members(self, cutoff):
        """Bounded ZRANGEBYSCORE(-inf, cutoff); stale members (score <= cutoff)."""
        return self._zrange('-inf', cutoff)

    def get_raw_scalar(self, key):
        """Return one raw cache value, preserving a genuine Redis miss as None."""
        client = self._connect()
        if client is None:
            raise RuntimeError("cache client unavailable")
        return client.get(cache.make_key(key))

    def decode_required(self, raw):
        """Decode a present value without turning corruption into a miss."""
        if raw is None:
            raise ValueError("cache value is absent")
        if self._decoder is None:
            raise RuntimeError("cache decoder unavailable")
        try:
            value = self._decoder(raw)
        except Exception as exc:
            raise ValueError("cache value could not be decoded") from exc
        if value is None:
            raise ValueError("cache value decoded to None")
        return value

    def _decode_watched_owner_batch(self, pipe, owner_keys, raw_owners):
        """Decode a watched owner batch without hiding wrong-type entries.

        ``MGET`` returns nil both for an absent key and for a key whose Redis
        type is not a string. A normal owner read may identify a genuine miss,
        but every nil slot needs a type-independent presence check before the
        batch can be treated as an absent or invalid owner set.
        """
        if raw_owners is None or len(raw_owners) != len(owner_keys):
            raise ValueError("owner batch is unavailable")
        nil_keys = [
            key
            for key, raw_owner in zip(owner_keys, raw_owners)
            if raw_owner is None
        ]
        if nil_keys:
            present = pipe.exists(*nil_keys)
            if (
                not isinstance(present, int)
                or isinstance(present, bool)
                or present < 0
                or present > len(nil_keys)
            ):
                raise ValueError("owner presence count is invalid")
            if present:
                raise ValueError("owner key has an invalid Redis type")
        owners = tuple(self.decode(raw) for raw in raw_owners)
        if any(
            raw is not None
            and (not isinstance(decoded, str) or not decoded)
            for raw, decoded in zip(raw_owners, owners)
        ):
            raise ValueError("owner value is invalid")
        return owners

    def get_scalar(self, key):
        """Read one decoded scalar through the selected Redis client.

        ``None`` is a successful cache miss. A missing client, unavailable
        decoder, or failed decode raises so callers never confuse uncertainty
        with an empty value.
        """
        raw = self.get_raw_scalar(key)
        if raw is None:
            return None
        return self.decode_required(raw)

    def decode_original_formation_size(self, raw):
        """Decode one present baseline record, rejecting every invalid shape."""
        original = self.decode_required(raw)
        if not isinstance(original, dict):
            raise ValueError("original formation size is not a record")
        size = original.get('size')
        if not isinstance(size, str) or size not in DYNO_SIZES:
            raise ValueError("original formation size is invalid")
        return original

    def decode_formation_size(self, raw, fallback_size):
        """Decode one formation tier; only a raw miss uses the known tier."""
        if raw is None:
            return fallback_size
        value = self.decode_required(raw)
        if not isinstance(value, str) or value not in DYNO_SIZES:
            raise ValueError("formation size is invalid")
        return value

    def decode_upscale_timer(self, raw):
        """Decode a present upscale timer, which must be a datetime."""
        if raw is None:
            return None
        value = self.decode_required(raw)
        if not isinstance(value, datetime):
            raise ValueError("upscale timer is invalid")
        return value

    def resolve_downscale_target(self, raw_original, formation_size):
        """Resolve a baseline target with the shared strict raw-miss rule."""
        if raw_original is None:
            return DYNO_SIZES.get(formation_size, {}).get('previous')
        return self.decode_original_formation_size(raw_original)['size']

    def read_downscale_target(self, formation_name, fallback_formation_size):
        """Read the current target and retain the raw value for later WATCH."""
        try:
            raw_original = self.get_raw_scalar(
                f'heroku:original_formation_size:{formation_name}',
            )
            target_size = self.resolve_downscale_target(
                raw_original, fallback_formation_size,
            )
        except Exception:
            return 'unavailable', None, None
        if target_size is None:
            return 'missing', None, raw_original
        return (
            'miss' if raw_original is None else 'found',
            target_size,
            raw_original,
        )

    def read_formation_size_raw(self, formation_name, fallback_size):
        """Read a primary tier and retain the raw value for exact WATCH checks."""
        try:
            raw_formation = self.get_raw_scalar(
                f'heroku:formation_size:{self.app_name}:{formation_name}',
            )
            formation_size = self.decode_formation_size(
                raw_formation, fallback_size,
            )
        except Exception:
            return 'unavailable', None, None
        return (
            'miss' if raw_formation is None else 'found',
            formation_size,
            raw_formation,
        )

    def get_owner_and_score(self, dyno_name):
        """Capture one dyno's current owner and reserved registry score.

        WATCH plus a no-op transaction rejects an interleaving where a
        replacement registers between the owner GET and the score ZSCORE.
        """
        client = self._connect()
        if client is None:
            return 'unavailable', None, None
        owner_key = cache.make_key(_dyno_owner_key(self.app_name, dyno_name))
        raw_owner = None
        score = None
        pipe = None
        for attempt in range(2):
            try:
                pipe = client.pipeline(transaction=True)
                pipe.watch(owner_key, self._index_key)
                raw_owner = pipe.get(owner_key)
                score = pipe.zscore(self._index_key, dyno_name)
                pipe.multi()
                pipe.ping()
                pipe.execute()
                break
            except _RedisWatchError:
                if attempt == 0:
                    continue
                return 'unavailable', None, None
            except Exception:
                return 'unavailable', None, None
            finally:
                if pipe is not None:
                    pipe.reset()
        if raw_owner is None or score is None:
            return 'missing', None, None
        if self._decoder is None:
            return 'unavailable', None, None
        try:
            owner = self._decoder(raw_owner)
        except Exception:
            return 'unavailable', None, None
        if not isinstance(owner, str) or not owner:
            return 'unavailable', None, None
        return 'found', owner, score

    def reserve_owned_flag(
        self, flag_key, dyno_name, owner_token, value, timeout,
        *, formation_name, expected_formation_size,
    ):
        """Set one resize flag only for the current owner at the expected tier."""
        client = self._connect()
        if client is None:
            return 'unavailable'
        owner_key = cache.make_key(_dyno_owner_key(self.app_name, dyno_name))
        physical_flag = cache.make_key(flag_key)
        formation_key = cache.make_key(
            f'heroku:formation_size:{self.app_name}:{formation_name}'
        )
        pipe = None
        for attempt in range(2):
            try:
                pipe = client.pipeline(transaction=True)
                pipe.watch(owner_key, physical_flag, formation_key)
                current_owner = pipe.get(owner_key)
                current_flag = pipe.get(physical_flag)
                raw_formation_size = pipe.get(formation_key)
                formation_size = self.decode(raw_formation_size)
                if current_owner != self.encode(owner_token):
                    return 'owner_mismatch'
                if current_flag is not None:
                    return 'flag_present'
                if (
                    raw_formation_size is not None
                    and formation_size != expected_formation_size
                ):
                    return 'tier_changed'
                pipe.multi()
                pipe.set(
                    physical_flag,
                    self.encode(value),
                    px=int(timeout * 1000),
                )
                pipe.execute()
                return 'reserved'
            except _RedisWatchError:
                if attempt == 0:
                    continue
                return 'contended'
            except Exception:
                return 'unavailable'
            finally:
                if pipe is not None:
                    pipe.reset()
        return 'contended'

    def publish_memory_pressure(
        self, dyno_name, owner_token, source_epoch, memory_mb, timeout,
    ):
        """Write a memory-pressure veto and its legacy readers under one owner."""
        if not _is_finite_number(source_epoch):
            raise ValueError('memory-pressure epoch must be finite')
        if not _is_finite_number(memory_mb, positive=True):
            raise ValueError('memory pressure must be a positive number')
        client = self._connect()
        if client is None:
            return 'unavailable'
        owner_key = cache.make_key(_dyno_owner_key(self.app_name, dyno_name))
        veto_key = cache.make_key(_dyno_memory_veto_key(self.app_name, dyno_name))
        memory_key = cache.make_key(f'heroku:dyno_memory:{dyno_name}')
        stable_key = cache.make_key(f'heroku:dyno_memory_stable:{dyno_name}')
        for attempt in range(2):
            pipe = None
            try:
                pipe = client.pipeline(transaction=True)
                pipe.watch(owner_key, veto_key)
                current_owner = pipe.get(owner_key)
                if current_owner != self.encode(owner_token):
                    return 'owner_mismatch'
                raw_veto = pipe.get(veto_key)
                if raw_veto is not None:
                    current_veto = self.decode(raw_veto)
                    if not (
                        isinstance(current_veto, (tuple, list))
                        and len(current_veto) == 3
                        and isinstance(current_veto[0], str)
                        and current_veto[0]
                        and _is_finite_number(current_veto[1])
                        and _is_finite_number(current_veto[2], positive=True)
                    ):
                        return 'unavailable'
                    if current_veto[1] >= source_epoch:
                        return 'superseded'
                timeout_ms = int(timeout * 1000)
                pipe.multi()
                pipe.set(
                    veto_key,
                    self.encode((owner_token, source_epoch, memory_mb)),
                    px=timeout_ms,
                )
                pipe.set(memory_key, self.encode(memory_mb), px=timeout_ms)
                pipe.set(stable_key, self.encode(False), px=timeout_ms)
                pipe.execute()
                return 'published'
            except _RedisWatchError:
                if attempt == 0:
                    continue
                return 'contended'
            except Exception:
                return 'unavailable'
            finally:
                if pipe is not None:
                    pipe.reset()
        return 'contended'

    def mark_owner_contention(self, dyno_name, contender_token):
        """Leave a bounded primary marker while another owner is active."""
        client = self._connect()
        if client is None:
            return False
        key = cache.make_key(_dyno_contention_key(self.app_name, dyno_name))
        try:
            client.set(
                key,
                self.encode(contender_token),
                px=_dyno_owner_ttl() * 1000,
            )
            return True
        except Exception:
            return False

    def read_owned_liveness(self, dyno_name):
        """Read one owner, score, and liveness value from the primary.

        WATCH plus a no-op transaction keeps the values coherent. A
        concurrent registration or cleanup fails the read instead of authorizing
        a restart from mixed evidence. A missing owner returns owner_expired
        with the score and alive value so the shorter write lease cannot erase
        stale zombie evidence. A live contention marker blocks restart dispatch.
        """
        client = self._connect()
        if client is None:
            return 'unavailable', None, None, None
        owner_key = cache.make_key(_dyno_owner_key(self.app_name, dyno_name))
        alive_key = cache.make_key(f'heroku:dyno_alive:{dyno_name}')
        contention_key = cache.make_key(_dyno_contention_key(self.app_name, dyno_name))
        pipe = None
        try:
            pipe = client.pipeline(transaction=True)
            pipe.watch(owner_key, self._index_key, alive_key, contention_key)
            raw_owner = pipe.get(owner_key)
            score = pipe.zscore(self._index_key, dyno_name)
            raw_alive = pipe.get(alive_key)
            raw_contention = pipe.get(contention_key)
            pipe.multi()
            pipe.ping()
            pipe.execute()
        except _RedisWatchError:
            return 'contended', None, None, None
        except Exception:
            return 'unavailable', None, None, None
        finally:
            if pipe is not None:
                pipe.reset()
        if raw_contention is not None:
            return 'contended', None, None, None
        if score is None or raw_alive is None:
            return 'missing', None, None, None
        owner = None
        try:
            if raw_owner is not None:
                owner = self.decode(raw_owner)
            alive = self.decode(raw_alive)
        except Exception:
            return 'unavailable', None, None, None
        if raw_owner is not None and (not isinstance(owner, str) or not owner):
            return 'unavailable', None, None, None
        if alive is None:
            return 'unavailable', None, None, None
        return ('found' if owner is not None else 'owner_expired', owner, score, alive)

    def read_formation_validation_state(
        self, prefix, cutoff, expected_roster,
    ):
        """Read a final roster, owner, contention, and degradation snapshot.

        The index, owner, contention, reconvergence, and failed-writer keys are
        watched before their values are read. A no-op transaction verifies none
        changed during the read. The caller compares the exact roster and
        decoded owner values with the observation batch already evaluated.
        """
        client = self._connect()
        if client is None:
            return 'unavailable', None, None, True
        reconverging_key = cache.make_key(
            _registry_reconverging_key(self.app_name),
        )
        degraded_key = cache.make_key(_registry_degraded_key(self.app_name))
        for attempt in range(2):
            pipe = None
            try:
                pipe = client.pipeline(transaction=True)
                pipe.watch(self._index_key, reconverging_key, degraded_key)
                current = pipe.zrangebyscore(
                    self._index_key, f'({cutoff}', '+inf', withscores=True,
                )
                if current is None:
                    return 'unavailable', None, None, True
                current_roster = tuple(
                    (member.decode('utf-8', errors='replace')
                     if isinstance(member, bytes) else member, float(score))
                    for member, score in current
                    if (member.decode('utf-8', errors='replace')
                        if isinstance(member, bytes) else member).startswith(prefix)
                )
                if current_roster != expected_roster:
                    return 'changed', None, None, False
                owner_keys = [
                    cache.make_key(_dyno_owner_key(self.app_name, member))
                    for member, _ in current_roster
                ]
                contention_keys = [
                    cache.make_key(_dyno_contention_key(self.app_name, member))
                    for member, _ in current_roster
                ]
                veto_keys = [
                    cache.make_key(_dyno_memory_veto_key(self.app_name, member))
                    for member, _ in current_roster
                ]
                pipe.watch(*owner_keys, *contention_keys, *veto_keys)
                raw_owners = pipe.mget(owner_keys)
                contention_count = pipe.exists(*contention_keys)
                veto_count = pipe.exists(*veto_keys)
                reconverging = pipe.get(reconverging_key)
                degraded_entries = pipe.zrange(degraded_key, 0, 0)
                if (
                    raw_owners is None
                    or not isinstance(contention_count, int)
                    or isinstance(contention_count, bool)
                    or contention_count < 0
                    or not isinstance(veto_count, int)
                    or isinstance(veto_count, bool)
                    or veto_count < 0
                    or degraded_entries is None
                ):
                    return 'unavailable', None, None, True
                try:
                    owners = self._decode_watched_owner_batch(
                        pipe, owner_keys, raw_owners,
                    )
                except Exception:
                    return 'unavailable', None, None, True
                pipe.multi()
                pipe.ping()
                pipe.execute()
                if veto_count:
                    return 'memory_pressure', None, None, True
                if contention_count:
                    return 'contended', None, None, True
                degraded = bool(reconverging is not None or degraded_entries)
                if degraded:
                    return 'degraded', owners, tuple(
                        None for _ in current_roster
                    ), True
                return 'ok', owners, tuple(None for _ in current_roster), degraded
            except _RedisWatchError:
                if attempt == 0:
                    continue
                return 'contended', None, None, True
            except Exception:
                return 'unavailable', None, None, True
            finally:
                if pipe is not None:
                    pipe.reset()
        return 'contended', None, None, True

    def reserve_downscale_if_current(
        self, expected_snapshot, dyno_name, owner_token, flag_key, flag_value,
        timeout, *, formation_name, expected_formation_size,
        expected_target_size, cleanup_if_at_or_below=False,
        expected_upscale_timer_raw=_UPSCALE_TIMER_UNBOUND,
        expected_cleanup_capture=None,
    ):
        """Atomically revalidate a formation and reserve its downscale attempt."""
        client = self._connect()
        if client is None:
            return 'unavailable'
        roster, _, _, expected_owners = expected_snapshot
        prefix = f'{formation_name}.'
        physical_flag = cache.make_key(flag_key)
        formation_key = cache.make_key(
            f'heroku:formation_size:{self.app_name}:{formation_name}'
        )
        original_size_key = cache.make_key(
            f'heroku:original_formation_size:{formation_name}'
        )
        upscale_until_key = cache.make_key(
            f'heroku:keep_upscaled_formation_until:{formation_name}'
        )
        reconverging_key = cache.make_key(
            _registry_reconverging_key(self.app_name),
        )
        degraded_key = cache.make_key(_registry_degraded_key(self.app_name))
        owner_keys = [
            cache.make_key(_dyno_owner_key(self.app_name, member))
            for member, _ in roster
        ]
        contention_keys = [
            cache.make_key(_dyno_contention_key(self.app_name, member))
            for member, _ in roster
        ]
        veto_keys = [
            cache.make_key(_dyno_memory_veto_key(self.app_name, member))
            for member, _ in roster
        ]
        pipe = None
        try:
            pipe = client.pipeline(transaction=True)
            pipe.watch(
                self._index_key,
                physical_flag,
                formation_key,
                original_size_key,
                upscale_until_key,
                reconverging_key,
                degraded_key,
                *owner_keys,
                *contention_keys,
                *veto_keys,
            )
            fresh_cutoff = _stale_cutoff()
            current = pipe.zrangebyscore(
                self._index_key, f'({fresh_cutoff}', '+inf', withscores=True,
            )
            if current is None:
                return 'unavailable'
            current_roster = tuple(
                (
                    member.decode('utf-8', errors='replace')
                    if isinstance(member, bytes) else member,
                    float(score),
                )
                for member, score in current
                if (
                    member.decode('utf-8', errors='replace')
                    if isinstance(member, bytes) else member
                ).startswith(prefix)
            )
            if current_roster != roster:
                return 'changed'

            raw_owners = pipe.mget(owner_keys)
            contention_count = pipe.exists(*contention_keys)
            veto_count = pipe.exists(*veto_keys)
            current_flag = pipe.get(physical_flag)
            raw_formation_size = pipe.get(formation_key)
            raw_original_size = pipe.get(original_size_key)
            raw_upscale_until = pipe.get(upscale_until_key)
            reconverging = pipe.get(reconverging_key)
            degraded_entries = pipe.zrange(degraded_key, 0, 0)
            if (
                raw_owners is None
                or not isinstance(contention_count, int)
                or isinstance(contention_count, bool)
                or contention_count < 0
                or not isinstance(veto_count, int)
                or isinstance(veto_count, bool)
                or veto_count < 0
                or degraded_entries is None
            ):
                return 'unavailable'
            try:
                owners = self._decode_watched_owner_batch(
                    pipe, owner_keys, raw_owners,
                )
            except Exception:
                return 'unavailable'
            if current_flag is not None:
                return 'flag_present'
            if expected_cleanup_capture is not None:
                expected_cleanup_capture = tuple(expected_cleanup_capture)
                if len(expected_cleanup_capture) != 3:
                    return 'unavailable'
                current_cleanup_capture = (
                    raw_formation_size,
                    raw_original_size,
                    raw_upscale_until,
                )
                if current_cleanup_capture != expected_cleanup_capture:
                    return 'changed'
            if veto_count:
                return 'memory_pressure'
            if contention_count:
                return 'contended'
            if reconverging is not None or degraded_entries:
                return 'degraded'

            # A caller that evaluated the keep-upscaled timer binds the exact
            # raw value it saw. WATCH then rejects a renewal, removal, or
            # corruption written before this transaction started.
            if expected_upscale_timer_raw is not _UPSCALE_TIMER_UNBOUND:
                if raw_upscale_until != expected_upscale_timer_raw:
                    return 'timer_changed'
                try:
                    self.decode_upscale_timer(raw_upscale_until)
                except Exception:
                    return 'unavailable'

            if owners != tuple(
                expected_owners.get(member) for member, _ in roster
            ):
                return 'owner_changed'
            owners_by_member = {
                member: owner for (member, _), owner in zip(roster, owners)
            }
            current_owner = owners_by_member.get(dyno_name)
            if current_owner != owner_token:
                return 'owner_changed'
            try:
                current_formation_size = self.decode_formation_size(
                    raw_formation_size, expected_formation_size,
                )
                current_target_size = self.resolve_downscale_target(
                    raw_original_size, current_formation_size,
                )
            except Exception:
                return 'unavailable'
            if current_formation_size != expected_formation_size:
                return 'tier_changed'
            if current_target_size is None:
                return 'unavailable'
            if current_target_size != expected_target_size:
                return 'target_changed'

            current_target_memory = DYNO_SIZES.get(
                current_formation_size, {},
            ).get('memory', 0)
            expected_target_memory = DYNO_SIZES.get(
                expected_target_size, {},
            ).get('memory', 0)
            if (
                cleanup_if_at_or_below
                and current_target_memory
                and expected_target_memory
                and current_target_memory <= expected_target_memory
            ):
                try:
                    self.decode_upscale_timer(raw_upscale_until)
                except Exception:
                    return 'unavailable'
                pipe.multi()
                pipe.delete(original_size_key)
                pipe.delete(upscale_until_key)
                pipe.execute()
                return 'already_at_target'

            # The validation reads above and this flag write are covered by
            # the same WATCH window. The first abort ends this invocation.
            pipe.multi()
            pipe.set(
                physical_flag,
                self.encode(flag_value),
                px=int(timeout * 1000),
            )
            pipe.execute()
            return 'reserved'
        except _RedisWatchError:
            return 'contended'
        except Exception:
            return 'unavailable'
        finally:
            if pipe is not None:
                pipe.reset()

    def reserve_remote_downscale_if_current(
        self, flag_key, flag_value, timeout, *, formation_name,
        expected_formation_raw, expected_formation_size,
        expected_original_raw, expected_target_size,
        expected_upscale_timer_raw=_UPSCALE_TIMER_UNBOUND,
        expected_cleanup_capture=None,
    ):
        """Reserve a manual downscale only if its raw target state is stable."""
        client = self._connect()
        if client is None:
            return 'unavailable'
        physical_flag = cache.make_key(flag_key)
        formation_key = cache.make_key(
            f'heroku:formation_size:{self.app_name}:{formation_name}',
        )
        original_key = cache.make_key(
            f'heroku:original_formation_size:{formation_name}',
        )
        upscale_until_key = cache.make_key(
            f'heroku:keep_upscaled_formation_until:{formation_name}',
        )
        pipe = None
        try:
            pipe = client.pipeline(transaction=True)
            watch_keys = [physical_flag, formation_key, original_key]
            timer_bound = (
                expected_upscale_timer_raw is not _UPSCALE_TIMER_UNBOUND
                or expected_cleanup_capture is not None
            )
            if timer_bound:
                watch_keys.append(upscale_until_key)
            pipe.watch(*watch_keys)
            current_flag = pipe.get(physical_flag)
            current_formation_raw = pipe.get(formation_key)
            current_original_raw = pipe.get(original_key)
            current_upscale_raw = None
            if timer_bound:
                current_upscale_raw = pipe.get(upscale_until_key)
            if current_flag is not None:
                return 'flag_present'
            if expected_cleanup_capture is not None:
                expected_cleanup_capture = tuple(expected_cleanup_capture)
                if len(expected_cleanup_capture) != 3:
                    return 'unavailable'
                current_cleanup_capture = (
                    current_formation_raw,
                    current_original_raw,
                    current_upscale_raw,
                )
                if current_cleanup_capture != expected_cleanup_capture:
                    return 'changed'
            if current_formation_raw != expected_formation_raw:
                return 'tier_changed'
            if current_original_raw != expected_original_raw:
                return 'target_changed'
            if timer_bound:
                if current_upscale_raw != expected_upscale_timer_raw:
                    return 'timer_changed'
                try:
                    self.decode_upscale_timer(current_upscale_raw)
                except Exception:
                    return 'unavailable'
            try:
                current_formation_size = self.decode_formation_size(
                    current_formation_raw, expected_formation_size,
                )
                current_target_size = self.resolve_downscale_target(
                    current_original_raw, current_formation_size,
                )
            except Exception:
                return 'unavailable'
            if current_formation_size != expected_formation_size:
                return 'tier_changed'
            if current_target_size is None:
                return 'unavailable'
            if current_target_size != expected_target_size:
                return 'target_changed'
            pipe.multi()
            pipe.set(
                physical_flag,
                self.encode(flag_value),
                px=int(timeout * 1000),
            )
            pipe.execute()
            return 'reserved'
        except _RedisWatchError:
            return 'contended'
        except Exception:
            return 'unavailable'
        finally:
            if pipe is not None:
                pipe.reset()

    def claim_and_register(
        self, dyno_name, owner_token, score, alive_value, alive_ttl,
        *, allow_new_claim=True,
    ):
        """Atomically claim this dyno name and publish its registry score."""
        client = self._connect()
        if client is None:
            return 'unavailable'
        owner_key = cache.make_key(_dyno_owner_key(self.app_name, dyno_name))
        owner_value = self.encode(owner_token)
        reconverging_key = cache.make_key(_registry_reconverging_key(self.app_name))
        reconverging_ttl = max(60, int(getattr(settings, 'DYNO_AUTOSCALE_INTERVAL', 30)) * 2)
        for attempt in range(2):
            pipe = None
            try:
                pipe = client.pipeline(transaction=True)
                pipe.watch(owner_key, self._index_key)
                current_owner = pipe.get(owner_key)
                if current_owner is None and not allow_new_claim:
                    return 'owner_mismatch'
                if current_owner is not None and current_owner != owner_value:
                    return 'owner_mismatch'
                registry_exists = pipe.exists(self._index_key)
                pipe.multi()
                pipe.set(owner_key, owner_value, px=_dyno_owner_ttl() * 1000)
                if not registry_exists:
                    pipe.set(reconverging_key, self.encode(True), px=reconverging_ttl * 1000)
                pipe.zadd(self._index_key, {dyno_name: score})
                pipe.set(
                    cache.make_key(f'heroku:dyno_alive:{dyno_name}'),
                    self.encode(alive_value),
                    px=alive_ttl * 1000,
                )
                pipe.execute()
                return 'claimed'
            except _RedisWatchError:
                if attempt == 0:
                    continue
                _mark_registry_degraded(self.app_name, dyno_name)
                return 'contended'
            except Exception:
                _mark_registry_degraded(self.app_name, dyno_name)
                return 'unavailable'
            finally:
                if pipe is not None:
                    pipe.reset()
        _mark_registry_degraded(self.app_name, dyno_name)
        return 'contended'

    def publish_owned_values(
        self, dyno_name, owner_token, score, values, *,
        observation_memory=None, observation_epoch=None,
    ):
        """Publish one sample only while its owner and reserved score still match."""
        client = self._connect()
        if client is None:
            return 'unavailable'
        owner_key = cache.make_key(_dyno_owner_key(self.app_name, dyno_name))
        veto_key = cache.make_key(_dyno_memory_veto_key(self.app_name, dyno_name))
        pipe = None
        try:
            pipe = client.pipeline(transaction=True)
            pipe.watch(owner_key, self._index_key, veto_key)
            current_owner = pipe.get(owner_key)
            current_score = pipe.zscore(self._index_key, dyno_name)
            if current_owner != self.encode(owner_token) or current_score != score:
                return 'owner_mismatch'
            raw_veto = pipe.get(veto_key)
            clear_veto = False
            if raw_veto is not None:
                veto = self.decode(raw_veto)
                if not (
                    isinstance(veto, (tuple, list))
                    and len(veto) == 3
                    and isinstance(veto[0], str)
                    and veto[0]
                    and _is_finite_number(veto[1])
                    and _is_finite_number(veto[2], positive=True)
                ):
                    return 'unavailable'
                if (
                    not _is_finite_number(observation_memory, positive=True)
                    or not _is_finite_number(observation_epoch)
                    or observation_epoch <= veto[1]
                ):
                    return 'memory_pressure'
                clear_veto = True
                adjusted_values = []
                for key, value, timeout in values:
                    if key == _observation_key(self.app_name, dyno_name):
                        if isinstance(value, _DynoObservation):
                            value = value._replace(stable=False)
                    elif key == f'heroku:dyno_memory_stable:{dyno_name}':
                        value = False
                    adjusted_values.append((key, value, timeout))
                values = adjusted_values
            pipe.multi()
            pipe.set(owner_key, self.encode(owner_token), px=_dyno_owner_ttl() * 1000)
            for key, value, timeout in values:
                physical_key = cache.make_key(key)
                if value is None:
                    pipe.delete(physical_key)
                    continue
                timeout_ms = None if timeout is None else int(timeout * 1000)
                pipe.set(physical_key, self.encode(value), px=timeout_ms)
            if clear_veto:
                pipe.delete(veto_key)
            pipe.execute()
            return 'published'
        except _RedisWatchError:
            return 'contended'
        except Exception:
            return 'unavailable'
        finally:
            if pipe is not None:
                pipe.reset()

    def delete_owned_dyno(self, dyno_name, expected_owner, expected_score, keys):
        """Delete one dyno only when its captured owner and score are unchanged."""
        client = self._connect()
        if client is None:
            return 'unavailable'
        owner_key = cache.make_key(_dyno_owner_key(self.app_name, dyno_name))
        pipe = None
        try:
            pipe = client.pipeline(transaction=True)
            pipe.watch(owner_key, self._index_key)
            current_owner = pipe.get(owner_key)
            current_score = pipe.zscore(self._index_key, dyno_name)
            if current_owner != self.encode(expected_owner) or current_score != expected_score:
                return 'owner_mismatch'
            pipe.multi()
            for key in keys:
                pipe.delete(cache.make_key(key))
            pipe.zrem(self._index_key, dyno_name)
            pipe.delete(owner_key)
            pipe.execute()
            return 'deleted'
        except _RedisWatchError:
            return 'contended'
        except Exception:
            return 'unavailable'
        finally:
            if pipe is not None:
                pipe.reset()

    def compare_and_delete_scalar(self, key, expected_value):
        """Delete one cache value only if an attempt token still owns it."""
        client = self._connect()
        if client is None:
            return 'unavailable'
        physical_key = cache.make_key(key)
        pipe = None
        try:
            pipe = client.pipeline(transaction=True)
            pipe.watch(physical_key)
            if pipe.get(physical_key) != self.encode(expected_value):
                return 'owner_mismatch'
            pipe.multi()
            pipe.delete(physical_key)
            pipe.execute()
            return 'deleted'
        except _RedisWatchError:
            return 'contended'
        except Exception:
            return 'unavailable'
        finally:
            if pipe is not None:
                pipe.reset()

    def _formation_cleanup_keys(self, formation_name):
        """Return the physical keys cleared for an at-or-below baseline."""
        return (
            cache.make_key(
                f'heroku:formation_size:{self.app_name}:{formation_name}',
            ),
            cache.make_key(
                f'heroku:original_formation_size:{formation_name}',
            ),
            cache.make_key(
                f'heroku:keep_upscaled_formation_until:{formation_name}',
            ),
        )

    def capture_formation_cleanup_state(self, formation_name):
        """Capture exact raw cleanup state before taking a writer lock.

        Individual primary GETs keep a wrong-type key fail closed. Redis MGET
        returns nil for a wrong-type member, which would turn an invalid tier
        into a normal miss.
        """
        client = self._connect()
        if client is None:
            return None
        try:
            return tuple(
                client.get(key)
                for key in self._formation_cleanup_keys(formation_name)
            )
        except Exception:
            return None

    def clear_formation_if_at_or_below(
        self, formation_name, fallback_formation_size, *, captured=None,
        expected_upscale_timer_raw=_UPSCALE_TIMER_UNBOUND,
    ):
        """Delete one at-or-below baseline only if its full state is unchanged.

        ``captured`` is the raw tier, baseline, and upscale timer obtained
        before an outer mutation lock.  Callers without that lock capture the
        same tuple here.  The watched transaction uses type-strict GETs to
        compare those exact values before deleting either key, so a wrong-type
        value or a writer that replaces the baseline or timer wins the race.

        ``no_baseline`` means the primary holds no original baseline.  That
        is a genuine miss, not corrupt state, so the scheduler can still use
        its previous-tier fallback.  Nothing is deleted for that result.
        """
        client = self._connect()
        if client is None:
            return 'unavailable'
        keys = self._formation_cleanup_keys(formation_name)
        if captured is None:
            captured = self.capture_formation_cleanup_state(formation_name)
        if captured is None:
            return 'unavailable'
        captured = tuple(captured)
        if len(captured) != len(keys):
            return 'unavailable'

        pipe = None
        try:
            pipe = client.pipeline(transaction=True)
            pipe.watch(*keys)
            current = tuple(pipe.get(key) for key in keys)
            if current != captured:
                return 'changed'
            raw_formation, raw_original, raw_upscale_until = current
            try:
                self.decode_upscale_timer(raw_upscale_until)
                current_size = self.decode_formation_size(
                    raw_formation, fallback_formation_size,
                )
            except Exception:
                return 'unavailable'
            if raw_original is None:
                # EXEC closes the WATCH window before the scheduler acts on
                # the genuine baseline miss.
                pipe.multi()
                pipe.execute()
                return 'no_baseline'
            try:
                original_size = self.decode_original_formation_size(
                    raw_original,
                )['size']
            except Exception:
                return 'unavailable'
            current_memory = DYNO_SIZES.get(current_size, {}).get('memory', 0)
            original_memory = DYNO_SIZES.get(original_size, {}).get('memory', 0)
            if not current_memory or not original_memory:
                return 'unavailable'
            if current_memory > original_memory:
                # A watched no-op keeps the caller's continuation bound to the
                # exact tier, baseline, and timer it just compared.
                pipe.multi()
                pipe.execute()
                return 'not_at_or_below'
            if (
                expected_upscale_timer_raw is not _UPSCALE_TIMER_UNBOUND
                and current[2] != expected_upscale_timer_raw
            ):
                return 'timer_changed'
            pipe.multi()
            pipe.delete(keys[1])
            pipe.delete(keys[2])
            pipe.execute()
            return 'cleared'
        except _RedisWatchError:
            return 'contended'
        except Exception:
            return 'unavailable'
        finally:
            if pipe is not None:
                pipe.reset()

    def clear_degraded_if_score(self, dyno_name, expected_failure_time):
        """Remove one resolved failure without deleting a newer concurrent one."""
        client = self._connect()
        if client is None:
            return 'unavailable'
        ledger = cache.make_key(_registry_degraded_key(self.app_name))
        pipe = None
        try:
            pipe = client.pipeline(transaction=True)
            pipe.watch(ledger)
            current = pipe.zscore(ledger, dyno_name)
            if current != expected_failure_time:
                return 'changed'
            pipe.multi()
            pipe.zrem(ledger, dyno_name)
            pipe.execute()
            return 'cleared'
        except _RedisWatchError:
            return 'contended'
        except Exception:
            return 'unavailable'
        finally:
            if pipe is not None:
                pipe.reset()

    def mget(self, keys):
        """Batched metric reads via physical keys; never one round trip per dyno."""
        client = self._connect()
        if client is None:
            return None
        physical = [cache.make_key(key) for key in keys]
        raw_values = client.mget(physical)
        if raw_values is None:
            return None
        return [self.decode(value) for value in raw_values]

    def exists(self, keys):
        """Count present physical keys without decoding their values."""
        client = self._connect()
        if client is None:
            return None
        physical = [cache.make_key(key) for key in keys]
        try:
            count = client.exists(*physical)
        except Exception:
            return None
        if (
            not isinstance(count, int)
            or isinstance(count, bool)
            or count < 0
        ):
            return None
        return count

    def zremrangebyscore(self, min_score, max_score):
        """Prune through a write client; never write via a read replica."""
        if self._index_key is None:
            self._index_key = cache.make_key(f'heroku:dynos:v1:{self.app_name}')
        try:
            client = cache.client.get_client(write=True)
        except AttributeError:
            return None
        return client.zremrangebyscore(self._index_key, min_score, max_score)


#: Most failed-writer names probed in one degraded read.  The ledger is per app
#: and normally holds one entry or none.
_DEGRADED_LEDGER_LIMIT = 20


def _registry_degraded_key(app_name):
    """Ledger of dyno names whose registry write failed (score = failure time)."""
    return f'heroku:dynos:v1:{app_name}:degraded'


def _registry_reconverging_key(app_name):
    """Marker for a re-created member list whose fleet is still re-registering."""
    return f'heroku:dynos:v1:{app_name}:reconverging'


def _mark_registry_reconverging(app_name):
    """Give a re-created member list one check-in interval to fill in.

    A missing app ZSET means a first deploy or an eviction, and the fleet only
    re-registers over the next autoscale interval.  Until then a one-member
    list must not authorize a resize, so the writer publishes this marker for
    two intervals (best effort).
    """
    timeout = max(60, int(getattr(settings, 'DYNO_AUTOSCALE_INTERVAL', 30)) * 2)
    try:
        cache.set(_registry_reconverging_key(app_name), True, timeout=timeout)
    except Exception:
        logger.warning(
            "Failed to publish the dyno registry reconvergence marker for app %s.",
            app_name, exc_info=True,
        )


def _registry_degraded_ttl():
    """Ledger lifetime that covers the full registration grace."""
    zombie_windows = 2 * int(
        getattr(settings, 'DYNO_ZOMBIE_THRESHOLD', 24 * 60 * 60),
    )
    return max(zombie_windows, _registry_degraded_grace() + 1)


def _mark_registry_degraded(app_name, dyno_name):
    """Record one failed registry write in the app's ledger (best effort).

    The entry lives until that dyno registers again, proves it is gone, or a
    bounded first-registration grace expires. The ledger key lives for at least
    the registration grace and two zombie windows after the last failure.
    """
    try:
        client = cache.client.get_client(write=True)
        key = cache.make_key(_registry_degraded_key(app_name))
        client.zadd(key, {dyno_name: time.time()})
        client.expire(key, _registry_degraded_ttl())
    except Exception:
        logger.warning(
            "Failed to record the failed registry write for app %s, dyno %s.",
            app_name, dyno_name, exc_info=True,
        )


def _registry_degraded(instance):
    """Uncached read of the app's failure ledger and reconvergence marker.

    True when the ledger names a dyno that is alive but not registered since
    its failed write, or when the member list was re-created and the fleet is
    still re-registering; either way a sibling workload can be invisible to
    every destructive gate.  A ledger entry clears only on proof: a score
    newer than the recorded failure means that dyno registered again, and a
    gone liveness key means the dyno is no longer live.  Any read failure
    counts as degraded, because the callers authorize a resize; a missing raw
    client is left to ``index_ready``, which already fails closed for it.
    """
    if not instance.app_name:
        return True
    try:
        adapter = _IndexAdapter(instance.app_name, primary=True)
        client = adapter._connect()
        if client is None:
            return False
        if client.get(cache.make_key(_registry_reconverging_key(instance.app_name))) is not None:
            return True  # re-created member list: the fleet is re-registering
        ledger = cache.make_key(_registry_degraded_key(instance.app_name))
        entries = client.zrange(ledger, 0, _DEGRADED_LEDGER_LIMIT - 1, withscores=True) or []
        if not entries:
            return False
        for raw_member, failure_time in entries:
            member = raw_member.decode('utf-8', errors='replace') if isinstance(raw_member, bytes) else raw_member
            score = client.zscore(adapter._index_key, member)
            if score is not None and score > failure_time:
                adapter.clear_degraded_if_score(member, failure_time)
                continue
            if client.get(cache.make_key(f'heroku:dyno_alive:{member}')) is not None:
                return True  # alive, but not registered since that failure
            if time.time() - failure_time <= _registry_degraded_grace():
                return True  # first registration may have failed before its heartbeat
            adapter.clear_degraded_if_score(member, failure_time)
        if len(entries) >= _DEGRADED_LEDGER_LIMIT:
            # A full batch cannot prove that no later entry names a live writer.
            return True
        return False
    except Exception:
        logger.warning(
            "Dyno registry degraded-ledger read failed for app %s; failing "
            "closed (no destructive actions).", instance.app_name, exc_info=True,
        )
        return True


def _update_dyno_registry(app_name, dyno_name, score=None):
    if not app_name or not dyno_name:
        return False

    try:
        client = cache.client.get_client(write=True)
        key = cache.make_key(f'heroku:dynos:v1:{app_name}')
        if score is None:
            client.zrem(key, dyno_name)
        else:
            if not client.exists(key):
                # First deploy or an eviction: the list is not a fleet yet.
                _mark_registry_reconverging(app_name)
            client.zadd(key, {dyno_name: score})
        return True
    except Exception:
        logger.warning(
            "Failed to update indexed dyno registry for app %s, dyno %s.",
            app_name, dyno_name, exc_info=True,
        )
        _mark_registry_degraded(app_name, dyno_name)
        return False

# Gracefully handle lock expiry.  Consumers may configure different lock backends —
# redis_lock.django_cache.RedisCache raises redis_lock.NotAcquired, while
# plain django-redis raises redis.exceptions.LockNotOwnedError.
# not a direct dep of heroku-manager.
try:
    from redis_lock import NotAcquired as _NotAcquired
    _LockExpiryErrors = (_NotAcquired,)
except ImportError:
    class _NotAcquired(Exception):
        pass
    _LockExpiryErrors = (_NotAcquired,)

try:
    from redis.exceptions import LockNotOwnedError
except ImportError:
    class LockNotOwnedError(Exception):
        pass
_LockExpiryErrors += (LockNotOwnedError,)


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
        for dyno, size_info in DYNO_SIZES.items():
            if size_info["memory"] == dyno_memory:
                return dict(size_info)

        formation_size = 'standard-1x'
    return dict(DYNO_SIZES.get(formation_size, {}))

class _ApiResult:
    """Minimal result object returned by exec_connect, mirrors subprocess.run output."""
    __slots__ = ('stdout', 'stderr', 'returncode')

    def __init__(self, stdout: str, stderr: str, returncode: int) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


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
        self.proxy = HerokuProxy()
        self.heroku_api_key = None if self.proxy.enabled else os.environ.get('HEROKU_API_KEY')
        self._stop_autoscale_event = threading.Event()
        self._stop_file_cleaning_event = threading.Event()
        self._autoscale_thread = None
        self._file_cleaning_thread = None
        self._thread_lock = threading.Lock()
        self._last_file_cleaning = None
        self._formation_size_cached = None  # (size, timestamp) tuple for instance-level caching
        self._dyno_owner_claimed = False
        self._dyno_owner_fenced = False
        self._last_registered_score = None

    @property
    def heroku_api_configured(self):
        """True when the selected transport can dispatch Platform API requests.

        HEROKU_PROXY_URL selects the signed proxy and never falls back to a
        local API key.  Without it the legacy direct transport uses the key.
        """
        if self.proxy.enabled:
            return self.proxy.configured
        return bool(self.heroku_api_key)

    @property
    def _verified_self_dyno_id(self):
        """Return the current process UUID only when it identifies this dyno."""
        if self.remote_monitoring:
            return None
        if self.dyno_name != os.environ.get('DYNO'):
            return None
        return _canonical_uuid(self.dyno_id)

    @property
    def _dyno_owner_token(self):
        """Stable ownership token for this process incarnation."""
        override = self.__dict__.get('_dyno_owner_token_override')
        if override:
            return override
        verified_id = self._verified_self_dyno_id
        if verified_id:
            return f'dyno:{verified_id}'
        pid = os.getpid()
        process_token = self.__dict__.get('_process_owner_token')
        if process_token and process_token[0] == pid:
            return process_token[1]
        if process_token is not None:
            self._dyno_owner_claimed = False
            self._dyno_owner_fenced = False
            self._last_registered_score = None
        token = f'process:{uuid.uuid4()}:{pid}'
        self.__dict__['_process_owner_token'] = (pid, token)
        return token

    @property
    def local_dyno_size(self):
        """Return the size this process booted with, from Heroku's DYNO_RAM."""
        try:
            dyno_memory = int(os.environ.get('DYNO_RAM', 0))
        except (TypeError, ValueError):
            return None
        for size_name, details in DYNO_SIZES.items():
            if details.get('memory') == dyno_memory:
                return size_name
        return None

    def _read_primary_formation_size(self, adapter=None):
        """Return primary tier state without confusing a miss with an error."""
        adapter = adapter or _IndexAdapter(self.app_name, primary=True)
        try:
            value = adapter.get_scalar(
                f'heroku:formation_size:{self.app_name}:{self.formation_name}'
            )
        except Exception:
            logger.warning(
                "Could not read the primary formation size for %s/%s.",
                self.app_name, self.formation_name, exc_info=True,
            )
            return 'unavailable', None
        if isinstance(value, str) and value in DYNO_SIZES:
            return 'found', value
        if value is None:
            return 'miss', None
        logger.warning(
            "Primary formation size for %s/%s is invalid; deferring resize.",
            self.app_name, self.formation_name,
        )
        return 'unavailable', None

    def _defer_resize_for_retiring_dyno(self, operation):
        """Block a resize unless this process owns the current dyno lease."""
        if self.remote_monitoring:
            return False

        # Accessing the token first resets inherited ownership state after a
        # fork, including a fence copied from the parent process.
        owner_token = self._dyno_owner_token
        adapter = None
        if getattr(self, '_dyno_owner_fenced', False):
            detail = 'fenced ownership'
        else:
            adapter = _IndexAdapter(self.app_name, primary=True)
            status, current_owner, _ = adapter.get_owner_and_score(self.dyno_name)
            if status != 'found' or current_owner != owner_token:
                detail = 'current owner unavailable or replaced'
            else:
                detail = None

        local_size = self.local_dyno_size
        if local_size is None:
            if detail is None:
                return False
            logger.info(
                "Skipping %s for %s/%s: local dyno size unknown, %s.",
                operation, self.app_name, self.dyno_name, detail,
            )
            return True

        if detail is None:
            state, primary_size = self._read_primary_formation_size(adapter)
            if state == 'unavailable':
                detail = 'primary formation read unavailable'
            else:
                current_size = primary_size if state == 'found' else self.formation_size
                if current_size == local_size:
                    # Primary state is authoritative even when the instance cache
                    # still holds the predecessor tier after a restart overlap.
                    previous_size = (
                        self._formation_size_cached[0]
                        if self._formation_size_cached else None
                    )
                    self._formation_size_cached = (current_size, time.time())
                    if previous_size != current_size:
                        self._invalidate_formation_dependent_cache()
                    return False
                detail = f'primary formation size {current_size}'

        logger.info(
            "Skipping %s for retiring %s/%s: local dyno size %s, %s.",
            operation, self.app_name, self.dyno_name, local_size, detail,
        )
        return True

    # Per-dyno cache key prefixes — published in check_in_dyno, cleaned in
    # remove_dyno_from_alive_cache.  Add new metrics here to publish/clean
    # them automatically.
    _DYNO_CACHE_PREFIXES = ('alive', 'memory', 'memory_stable', 'load')

    @property
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
        dyno_settings = {}
        dyno_settings.update(get_dyno_settings(self.formation_size))
        dyno_settings.update(settings.WORKER_SETTINGS_MAP.get(self.formation_name, {}))
        
        return dyno_settings

    @cached_property
    def downscale_on_non_empty_queue(self):
        return self.settings.get("downscale_on_non_empty_queue", True)
    
    @cached_property
    def max_dyno_size(self):
        return self.settings.get("max_dyno_size", "performance-2xl")

    @cached_property
    def threads_available(self):
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

    @property
    def available_memory(self):
        return DYNO_SIZES.get(self.formation_size, {}).get("memory", 512)

    @property
    def current_memory_usage(self):
        return self.exact_memory_usage

    @property
    def remote_monitoring(self):
        return self.dyno_name != os.environ.get('DYNO', None)

    @cached_property
    def index_ready(self):
        """Phase C runtime readiness predicate for this dyno (fail closed).

        Cached for one autoscale cycle: ``check_in_dyno`` resets it, so the
        own-member ZSCORE runs at most once per cycle (readiness is reused by
        the sibling-memory, load, and zombie readers).
        """
        return _index_ready(self)

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
    def memory_reading_for_observation(self):
        """Return a validated memory reading with its source epoch."""
        patterns = [
            r"sample#memory_total=(\d+\.\d+)MB",
            r"Process running mem=(\d+)M\(\d+\.\d+%\)",
        ]
        reading = self._extract_latest_metric_reading(
            patterns,
            cache_key="heroku:memory_total",
            result_type=float,
            require_fresh_source=True,
        )
        if reading is not None and _is_finite_number(reading.value, positive=True):
            return reading
        if self.remote_monitoring:
            return None
        try:
            from utils.process import get_total_memory_usage
            value = get_total_memory_usage()
        except ImportError:
            return None
        if _is_finite_number(value, positive=True):
            return _MetricReading(value, time.time())
        return None

    @property
    def memory_mb_for_observation(self):
        """Return a real memory sample, preserving an unavailable reading as None."""
        reading = self.memory_reading_for_observation
        return reading.value if reading is not None else None

    @property
    def current_memory_usage_percentage(self):
        cur_mem = self.current_memory_usage
        if cur_mem and self.available_memory:
            return round(cur_mem / self.available_memory * 100, 2)
        return 0.0  # Return 0 if memory data is not available

    @property
    def is_on_original_formation_size_or_lower(self):
        original = self.original_formation_size
        if not original or original not in DYNO_SIZES or self.formation_size not in DYNO_SIZES:
            return False
        return DYNO_SIZES[self.formation_size]["memory"] <= DYNO_SIZES[original]["memory"]

    @property
    def is_memory_usage_high(self):
        return self.current_memory_usage > settings.HIGH_MEM_USE_MB

    @property
    def is_out_of_memory(self):
        return self.current_memory_usage > self.available_memory

    @property
    def requires_upscale(self):
        return bool(
            self.current_memory_usage_percentage > settings.UPSCALE_PERCENTAGE_HIGH_MEM_USE
            or self.detected_r15
        )

    def _evaluate_downscale_snapshot(self, *, queue_allowed, stability_ignores_queue=False):
        """Evaluate both memory paths from one roster and one observation batch."""
        if _index_fail_closed(self) or self.requires_upscale:
            return _DownscaleDecision(False, False, None, None)
        if not queue_allowed and not stability_ignores_queue:
            return _DownscaleDecision(False, False, None, None)
        snapshot = self._formation_observation_snapshot()
        if snapshot is None:
            return _DownscaleDecision(False, False, None, None)
        target_size = self._downscale_target_size
        threshold = self._downscale_threshold_for_target(target_size)
        normal = queue_allowed and self._normal_memory_snapshot_allows_downscale(
            snapshot, threshold,
        )
        stability = (
            (queue_allowed or stability_ignores_queue)
            and self._stability_snapshot_allows_downscale(snapshot, threshold)
        )
        if not normal and not stability:
            return _DownscaleDecision(False, False, snapshot, target_size)
        if not self._formation_observation_snapshot_current(snapshot):
            return _DownscaleDecision(False, False, snapshot, target_size)
        return _DownscaleDecision(normal, stability, snapshot, target_size)

    @property
    def allow_downscale(self):
        # Phase B: downscale is destructive; it requires the indexed registry
        # to be runtime-ready. Flag false/absent or uncertain readiness blocks
        # the resize because a partial index can undercount hot siblings.
        if _index_fail_closed(self) or self.requires_upscale:
            return False
        queue_allowed = self.downscale_on_non_empty_queue or self.no_tasks_in_queue
        decision = self._evaluate_downscale_snapshot(queue_allowed=queue_allowed)
        if decision.stability and not decision.normal:
            logger.info(
                f"[{self.formation_name}] Memory stabilized at "
                f"{self.current_memory_usage:.0f}MB and load settled — allowing downscale "
                f"(RSS plateau detected; restart will reclaim memory)."
            )
        return decision.normal or decision.stability
            
    @property
    def allow_downscale_on_shutdown(self):
        """
        Allow downscale on shutdown if neither this dyno nor any sibling is in a
        high-memory state.  Siblings must be checked because a graceful shutdown
        removes this dyno's memory key before the formation resize, which would
        otherwise leave hot siblings vulnerable to an unexpected downscale.
        """
        decision = self._evaluate_downscale_snapshot(queue_allowed=True)
        return decision.normal or decision.stability

    @property
    def detected_r15(self):
        return bool(self.get_r15_from_logs())

    @property
    def detected_r14(self):
        return bool(self.get_r14_from_logs())

    @property
    def avg_load_1min(self):
        return self.get_load_1min_avg()

    @property
    def load_1min_for_observation(self):
        """Return a real load sample, preserving an unavailable reading as None."""
        patterns = [r"sample#load_avg_1m=(\d+\.\d+)"]
        reading = self._extract_latest_metric_reading(
            patterns, cache_key="heroku:load_avg_1m", result_type=float,
            require_fresh_source=True,
        )
        if reading is None:
            return None
        if '_strict_observation_logs' in self.__dict__:
            self._strict_load_source_epoch = reading.source_epoch
        return reading.value

    @property
    def _downscale_target_size(self):
        """Return the effective baseline target for this decision."""
        adapter = _IndexAdapter(self.app_name, primary=True)
        status, target_size, _ = adapter.read_downscale_target(
            self.formation_name, self.formation_size,
        )
        if status in ('found', 'miss'):
            return target_size
        if status == 'unavailable':
            logger.warning(
                "Could not read a usable downscale baseline for %s/%s; "
                "blocking the decision.",
                self.app_name, self.formation_name,
            )
        return None

    def _downscale_threshold_for_target(self, target_size):
        """Memory threshold for one captured downscale target."""
        if not target_size:
            logger.warning(
                f"[{self.formation_name}] Cannot compute downscale threshold: "
                f"no original or previous formation size known. Blocking downscale."
            )
            return float('inf')
        target_memory = DYNO_SIZES.get(target_size, {}).get("memory", 0)
        if not target_memory:
            logger.warning(
                f"[{self.formation_name}] Target size {target_size} has 0 memory. "
                f"Blocking downscale."
            )
            return float('inf')
        return target_memory * settings.DOWNSCALE_PERCENTAGE_HIGH_MEM_USE / 100

    @property
    def _downscale_memory_threshold(self):
        """Memory (MB) above which a dyno is too hot for the current target."""
        return self._downscale_threshold_for_target(
            self._downscale_target_size,
        )

    @property
    def is_still_high_memory_usage_for_downscale(self):
        threshold = self._downscale_memory_threshold
        return math.isinf(threshold) or self.current_memory_usage >= threshold

    @property
    def _is_stability_eligible(self):
        """True when the formation qualifies for stability-based forced downscale.

        Combines all preconditions that must hold before the RSS-plateau override
        can bypass the normal memory-threshold check:
        - Not actively under memory pressure (no R15, memory % < upscale threshold)
        - Actually upscaled above the original tier
        - Memory has plateaued (±tolerance over the stability window)
        - All dynos in the formation are idle (load avg < threshold)
        - No sibling is still actively hot (unless also stabilized)
        """
        decision = self._evaluate_downscale_snapshot(
            queue_allowed=False,
            stability_ignores_queue=True,
        )
        return decision.stability

    def _normalized_memory_history(self, history, now):
        """Prune, validate, and deduplicate history by source epoch."""
        window = getattr(settings, 'DYNO_MEMORY_STABILITY_WINDOW', 300)
        by_epoch = {}
        for item in history or []:
            if not isinstance(item, (tuple, list)) or len(item) != 2:
                continue
            source_epoch, memory_mb = item
            if not _is_finite_number(source_epoch) or not _is_finite_number(memory_mb, positive=True):
                continue
            if source_epoch > now + 1 or now - source_epoch > window:
                continue
            by_epoch[float(source_epoch)] = float(memory_mb)
        return sorted(by_epoch.items())

    def record_memory_reading(self, mem=None, source_epoch=None):
        """Record one source reading and return stability for that exact history."""
        if mem is None:
            reading = self.memory_reading_for_observation
            if reading is not None:
                mem = reading.value
                source_epoch = reading.source_epoch
        elif source_epoch is None:
            source_epoch = time.time()
        if not _is_finite_number(mem, positive=True):
            return False

        now = time.time()
        if not _is_finite_number(source_epoch) or source_epoch > now + 1:
            return False
        history = self._normalized_memory_history(
            cache.get(self.memory_history_cache_key) or [], now,
        )
        latest_epoch = history[-1][0] if history else None
        window = getattr(settings, 'DYNO_MEMORY_STABILITY_WINDOW', 300)
        if (
            latest_epoch is not None
            and source_epoch < latest_epoch
        ) or now - source_epoch > window:
            cache.set(
                self.memory_history_cache_key,
                history,
                timeout=window + 120,
            )
            return False

        by_epoch = dict(history)
        by_epoch[float(source_epoch)] = float(mem)
        history = sorted(by_epoch.items())
        cache.set(self.memory_history_cache_key, history, timeout=window + 120)
        return self._history_is_stabilized(history)

    @property
    def is_memory_stabilized(self):
        """True when memory has flattened out (±tolerance over the stability window).

        Python's allocator holds RSS without releasing it back to the OS.  Once
        heavy tasks finish, memory plateaus.  Detecting this plateau lets the
        autoscaler force a downscale + restart to reclaim memory.

        Tolerance is the greater of a fixed floor (``DYNO_MEMORY_STABILITY_TOLERANCE_MB``,
        default 50 MB) and a percentage of the average reading
        (``DYNO_MEMORY_STABILITY_TOLERANCE_PCT``, default 1%).  This scales
        gracefully: at 15 GB RSS the effective tolerance is ~150 MB (normal GC
        jitter won't prevent detection), while at 500 MB it stays at 50 MB.

        Readings must also span at least half the stability window to prevent
        a burst of readings in a few seconds from being mistaken for a plateau.
        """
        history = self._normalized_memory_history(
            cache.get(self.memory_history_cache_key) or [], time.time(),
        )
        return self._history_is_stabilized(history)

    def _history_is_stabilized(self, history):
        min_readings = getattr(settings, 'DYNO_MEMORY_STABILITY_MIN_READINGS', 6)
        if len(history) < min_readings:
            return False
        # Ensure readings span a meaningful period, not just a short burst
        window = getattr(settings, 'DYNO_MEMORY_STABILITY_WINDOW', 300)
        time_span = history[-1][0] - history[0][0]
        if time_span < window / 2:
            return False
        readings = [m for _, m in history]
        avg = sum(readings) / len(readings)
        tolerance_mb = getattr(settings, 'DYNO_MEMORY_STABILITY_TOLERANCE_MB', 50)
        tolerance_pct = getattr(settings, 'DYNO_MEMORY_STABILITY_TOLERANCE_PCT', 1)
        tolerance = max(tolerance_mb, avg * tolerance_pct / 100)
        return all(abs(m - avg) <= tolerance for m in readings)

    def _acquire_rate_limit_token(self):
        """Reserve one request in the app's shared 60-second window."""
        rate_limit = int(getattr(settings, 'HEROKU_API_RATE_LIMIT_PER_MINUTE', 50))
        if rate_limit <= 0:
            return False
        cache_key = f'heroku:api_rate:{self.app_name}'
        for _ in range(2):
            try:
                count = cache.incr(cache_key)
            except ValueError:
                if cache.add(cache_key, 1, timeout=60):
                    return True
                # Another caller opened the window. Count against its reservation.
                continue
            if count <= rate_limit:
                return True
            logger.warning("[%s] Heroku API rate limit reached (%s/min). Skipping API call.",
                           self.formation_name, rate_limit)
            return False
        logger.warning("[%s] Heroku API rate limit counter unavailable. Skipping API call.",
                       self.formation_name)
        return False

    def _proxy_pending_key(self, method, url, data):
        identity = json.dumps([self.proxy.base_url, self.proxy.client_id, self.app_name, method, url, data],
                              sort_keys=True, separators=(",", ":"))
        return "heroku:proxy_pending:v1:" + hashlib.sha256(identity.encode()).hexdigest()

    def reconcile_proxy_request(self, method=None, url=None, data=None, request_id=None, *, pending_key=None):
        """Clear one uncertain intent only after the operator verifies its provider outcome."""
        if not request_id:
            return False
        if pending_key is not None:
            if not isinstance(pending_key, str) or not re.fullmatch(r"heroku:proxy_pending:v1:[0-9a-f]{64}", pending_key):
                return False
            key = pending_key
        elif isinstance(method, str) and isinstance(url, str):
            key = self._proxy_pending_key(method.upper(), url, data)
        else:
            return False
        return self._clear_proxy_pending(key, request_id)

    def _clear_proxy_pending(self, key, request_id):
        with cache.lock(key + ":reconcile", expire=10):
            if cache.get(key) == str(request_id):
                cache.delete(key)
                return True
        return False

    def call_heroku_api(self, method, url, custom_headers=None, data=None):
        """Central method for ALL Heroku Platform API calls.

        HEROKU_PROXY_URL selects the signed proxy transport; otherwise the
        direct Platform API transport runs unchanged.
        """
        if self.proxy.enabled:
            return self._call_heroku_api_via_proxy(method, url, custom_headers, data)
        return self._call_heroku_api_direct(method, url, custom_headers, data)

    def _call_heroku_api_via_proxy(self, method, url, custom_headers, data):
        """Use the permissioned HTTP proxy. Never use a local Heroku credential."""
        denial = host_size_write_denial(method, url, data, headers=custom_headers)
        if denial is not None:
            return denial
        if not self.proxy.configured:
            logger.error(
                "Heroku proxy is selected but not fully configured (%s); refusing to call Heroku directly.",
                self.proxy.config_error or "missing proxy settings",
            )
            return None
        if not self._acquire_rate_limit_token():
            return None
        pending_key = None
        request_id = None
        # Creating a log session is a repeatable read, despite Heroku's POST API.
        if method.upper() not in ("GET", "HEAD") and not _is_log_session_request(method, url):
            pending_key = self._proxy_pending_key(method.upper(), url, data)
            previous = cache.get(pending_key)
            reserved = False
            if previous is None:
                request_id = str(uuid.uuid4())
                # Publish the grace period before another worker can see the reservation.
                # It outlasts the proxy deadline and expires if this worker exits.
                try:
                    cache.set(f"{pending_key}:active:{request_id}", True, timeout=90)
                except Exception:
                    logger.debug("Could not record the Heroku proxy alert grace period.")
                reserved = cache.add(pending_key, request_id, timeout=None)
                if not reserved:
                    previous = cache.get(pending_key)
            if not reserved:
                response = ProxyResponse(409, json.dumps({"id": "proxy_reconciliation_required", "request_id": previous}),
                                         request_id=previous)
                return self._report_proxy_reconciliation(pending_key, response)
        try:
            response = self.proxy.request(method, url, headers=custom_headers, data=data, request_id=request_id)
        except (ValueError, TypeError):
            # Local request validation failed before dispatch. A corrected call may proceed.
            if pending_key:
                self._clear_proxy_pending(pending_key, request_id)
            logger.error("Invalid Heroku proxy request; nothing was dispatched.")
            return ProxyResponse(400, '{"id":"proxy_request_invalid"}', request_id=request_id)
        # Keep uncertain outcomes reserved across ticks and worker restarts. A definite rejection can be corrected and resubmitted.
        if pending_key and response is not None and (200 <= response.status_code < 300
                or response.status_code in (400, 401, 403, 404, 405, 413, 422, 429)):
            self._clear_proxy_pending(pending_key, request_id)
        elif pending_key:
            if response is None:
                response = ProxyResponse(502, '{"id":"proxy_outcome_unknown"}', request_id=request_id)
            return self._report_proxy_reconciliation(pending_key, response, original_attempt=True)
        if response is not None and response.status_code >= 400:
            cooldown_retry = self._proxy_cooldown_retry_after(response)
            if cooldown_retry is not None:
                logger.warning(
                    "Heroku proxy cooldown for %s %s, status=429, "
                    "error=cooldown, retry_after=%ss",
                    method, self.formation_name, cooldown_retry,
                )
            else:
                logger.warning("Heroku proxy rejected %s for formation %s, status %s",
                               method, self.formation_name, response.status_code)
        return response

    def _report_proxy_reconciliation(self, pending_key, response, *, original_attempt=False):
        response.reconciliation_required = True
        request_id = response.request_id
        if not request_id:
            return response
        # Alert bookkeeping must preserve the actual response and pending intent.
        try:
            if not original_attempt and cache.get(f"{pending_key}:active:{request_id}"):
                return response
            first_alert = cache.add(f"{pending_key}:alert:{request_id}", True, timeout=3600)
        except Exception:
            first_alert = False
        if original_attempt or first_alert:
            logger.error(
                "Heroku proxy operation pending for %s, request_id=%s pending_key=%s status=%s error=%s",
                self.formation_name, request_id, pending_key, response.status_code, response.error_id,
            )
        return response

    @staticmethod
    def _proxy_cooldown_retry_after(response):
        """Return a valid cooldown Retry-After, or None for every other response."""
        if (
            not isinstance(response, ProxyResponse)
            or response.status_code != 429
            or response.error_id != "cooldown"
        ):
            return None
        value = response.headers.get("retry-after")
        if not isinstance(value, str):
            return None
        stripped = value.strip(" \t")
        if not re.fullmatch(r"[0-9]+", stripped):
            return None
        # Bound the meaningful digits before int(). Python rejects extreme lengths.
        digits = stripped.lstrip("0") or "0"
        if len(digits) > 5:
            return None
        retry_after = int(digits)
        return retry_after if 0 < retry_after <= 86400 else None

    @staticmethod
    def _log_api_failure(operation, response):
        if response is None:
            logger.error("%s. API call was rate-limited or failed.", operation)
        elif isinstance(response, ProxyResponse):
            if not response.reconciliation_required:
                if HerokuDyno._proxy_cooldown_retry_after(response) is not None:
                    return
                logger.error("%s. HTTP %s, error=%s.", operation, response.status_code, response.error_id)
        else:
            logger.error("%s. HTTP %s.", operation, response.status_code)

    def _definite_resize_rejection(self, response):
        """Return True only when the provider definitely did not apply a mutation."""
        if response is None:
            # The pinned proxy returns None before dispatch. Direct transport
            # cannot distinguish pre-dispatch exhaustion from an ambiguous send.
            return self.proxy.enabled
        if isinstance(response, ProxyResponse):
            if response.reconciliation_required:
                return False
            return 400 <= response.status_code < 500
        # Direct transport may retry a timed-out request. A later 4xx does not
        # prove the earlier attempts were rejected before reaching Heroku.
        return False

    def _call_heroku_api_direct(self, method, url, custom_headers=None, data=None):
        """
        Direct Heroku Platform API transport, used when HEROKU_PROXY_URL is absent.

        Features:
        - Shared Redis rate limiter (coordinates across all dynos)
        - Automatic 429 back-off using Retry-After header
        - Retry on transient network errors (SSL, connection, timeout)
        - GET response caching (5 s)
        """
        denial = host_size_write_denial(method, url, data, headers=custom_headers)
        if denial is not None:
            return denial
        custom_headers = custom_headers or {}
        headers = {
            "Accept": "application/vnd.heroku+json; version=3",
            "Authorization": f"Bearer {self.heroku_api_key}"
        }
        headers.update(custom_headers)

        # Return cached GET responses before consuming a rate-limit token
        request_hash = None
        if method == "GET":
            request_hash = hash(f"{method}{url}")
            cached_response = cache.get(f'heroku:api_response:{request_hash}')
            if cached_response:
                return cached_response

        max_attempts = 5
        for attempt in range(1, max_attempts + 1):
            # ---- shared rate limit ----
            if not self._acquire_rate_limit_token():
                return None

            # Recheck before each legacy retry, after the quota acquisition.
            denial = host_size_write_denial(method, url, data, headers=custom_headers)
            if denial is not None:
                # An earlier attempt may already have reached the provider.
                return denial if attempt == 1 else None
            try:
                response = requests.request(method, url, headers=headers, json=data, timeout=30)
            except (SSLError, ConnectionError, Timeout) as exc:
                if attempt < max_attempts:
                    wait = min(4 * (2 ** (attempt - 1)), 30)
                    logger.warning(
                        f"[{self.formation_name}] Heroku API {method} {url} "
                        f"transient error (attempt {attempt}/{max_attempts}): {exc}. "
                        f"Retrying in {wait}s..."
                    )
                    time.sleep(wait)
                    continue
                logger.warning(
                    f"[{self.formation_name}] Heroku API {method} {url} "
                    f"failed after {max_attempts} attempts: {exc}",
                    exc_info=True,
                )
                return None

            # ---- 429 back-off ----
            if response.status_code == 429:
                if _is_log_session_request(method, url):
                    return response
                retry_after = int(response.headers.get('Retry-After', 30))
                backoff = min(retry_after + random.uniform(1, 5), 120)
                logger.warning(
                    f"[{self.formation_name}] Heroku API 429 on {method} {url}. "
                    f"Backing off {backoff:.0f}s (attempt {attempt}/{max_attempts})."
                )
                if attempt < max_attempts:
                    time.sleep(backoff)
                    continue
                return response

            # ---- cache successful GETs ----
            if method == "GET" and response.status_code == 200 and request_hash is not None:
                cache.set(f'heroku:api_response:{request_hash}', response, timeout=5)

            # ---- log unexpected errors ----
            if response.status_code >= 400:
                logger.error(
                    f"[{self.formation_name}] Heroku API {method} {url} "
                    f"returned {response.status_code}: {response.text}"
                )

            return response

        return None

    @property
    def formation_size(self):
        """
        Get current formation size from Redis cache (set by scale operations)
        or derive from DYNO_RAM env var.  Does NOT call the Heroku API,
        keeping API budget for operations that actually need it.
        """
        # 1. Instance-level cache (avoids Redis round-trip every access)
        now = time.time()
        cache_duration = getattr(settings, 'DYNO_AUTOSCALE_INTERVAL', 30) * 5
        if self._formation_size_cached:
            cached_size, cached_time = self._formation_size_cached
            if now - cached_time < cache_duration:
                return cached_size

        # 2. Redis cache (updated by _update_formation_size after scale ops)
        redis_key = f'heroku:formation_size:{self.app_name}:{self.formation_name}'
        cached_size = cache.get(redis_key)
        if cached_size and cached_size in DYNO_SIZES:
            self._formation_size_cached = (cached_size, now)
            return cached_size

        # 3. Derive from DYNO_RAM env var (set by Heroku at boot)
        dyno_memory = int(os.environ.get('DYNO_RAM', 512))
        for dyno, details in DYNO_SIZES.items():
            if details['memory'] == dyno_memory:
                self._formation_size_cached = (dyno, now)
                # Persist to Redis so other lookups are consistent
                cache.set(redis_key, dyno, timeout=None)
                return dyno

        default = 'standard-1x'
        self._formation_size_cached = (default, now)
        return default

    # cached_properties that depend on formation_size and must be cleared after a resize.
    _FORMATION_DEPENDENT_CACHE = (
        'settings', 'downscale_on_non_empty_queue', 'max_dyno_size',
        'threads_available', 'price_per_hour', 'max_price_per_month',
    )

    def _invalidate_formation_dependent_cache(self):
        for attr in self._FORMATION_DEPENDENT_CACHE:
            self.__dict__.pop(attr, None)

    def _update_formation_size(self, new_size):
        """
        Record the new formation size in Redis + instance cache after a
        successful scale operation.  Also invalidates dependent cached_property
        values so they are recomputed from the new size.
        """
        self._formation_size_cached = (new_size, time.time())
        redis_key = f'heroku:formation_size:{self.app_name}:{self.formation_name}'
        cache.set(redis_key, new_size, timeout=None)
        self._invalidate_formation_dependent_cache()

    @property
    def next_formation_size(self):
        if self.formation_size in DYNO_SIZES:
            return DYNO_SIZES.get(self.formation_size, {}).get("next")

    @cached_property
    def upscale_until_cache_key(self):
        return f'heroku:keep_upscaled_formation_until:{self.formation_name}'

    @cached_property
    def original_size_cache_key(self):
        return f'heroku:original_formation_size:{self.formation_name}'

    def set_original_formation_size(self, value=None):
        if not cache.get(self.original_size_cache_key) or value:
            size = value if isinstance(value, str) else self.formation_size
            cache.set(self.original_size_cache_key, {"size": size, "time": timezone.now()}, timeout=None)

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

    @property
    def previous_formation_size(self):
        if self.formation_size in DYNO_SIZES:
            return DYNO_SIZES[self.formation_size].get("previous")

    @cached_property
    def upscaling_cache_key(self):
        return f'heroku:upscaling_formation:{self.formation_name}'

    @property
    def is_upscaling(self):
        return cache.get(self.upscaling_cache_key)

    def set_upscaling(self):
        # Extend TTL to cover the full scale cool-down plus worst-case API round-trip time
        # (up to 5 retries × 30 s backoff ≈ 150 s) so the flag never expires mid-call.
        ttl = settings.DYNO_TIME_BETWEEN_SCALES + 150
        cache.set(self.upscaling_cache_key, True, timeout=ttl)

    def clear_upscaling(self):
        cache.delete(self.upscaling_cache_key)

    @property
    def memory_history_cache_key(self):
        key = f'heroku:memory_history:{self.app_name}:{self.dyno_name}'
        identity = self._verified_self_dyno_id or self._dyno_owner_token
        return f'{key}:{identity}'

    @cached_property
    def downscale_cache_key(self):
        return f'heroku:downscale_formation:{self.formation_name}'

    @property
    def is_downscaling(self):
        return bool(cache.get(self.downscale_cache_key))

    def set_downscaling(self):
        attempt_token = str(uuid.uuid4())
        cache.set(
            self.downscale_cache_key,
            attempt_token,
            timeout=settings.DYNO_TIME_BETWEEN_SCALES,
        )
        return attempt_token

    def clear_downscaling(self, attempt_token):
        """Clear only the downscale flag still owned by this attempt."""
        if not attempt_token:
            return False
        status = _IndexAdapter(self.app_name, primary=True).compare_and_delete_scalar(
            self.downscale_cache_key,
            attempt_token,
        )
        if status == 'unavailable':
            logger.warning(
                "Could not clear the failed downscale flag for %s.",
                self.formation_name,
            )
        return status == 'deleted'

    @cached_property
    def threads_used_cache_key(self):
        return f'heroku:threads_used:{self.app_name}:{self.dyno_name}'

    def get_threads_used(self):
        if self.remote_monitoring:
            return cache.get(self.threads_used_cache_key)

        return self.set_threads_used()

    def set_threads_used(self):
        threads_used = len(threading.enumerate())
        if threads_used:
            ttl = getattr(settings, 'DYNO_AUTOSCALE_INTERVAL', 30) * 2
            cache.set(self.threads_used_cache_key, threads_used, timeout=ttl)
        return threads_used

    def autoscale(self, continuous=True, *, r15_only=False):
        """
        Autoscale the dyno based on current requirements.

        Args:
            continuous (bool): Whether to run autoscaling continuously.
            r15_only (bool): Limit this tick to an emergency upscale.
        """

        if 'beatworker' == self.formation_name and not getattr(settings, 'DYNO_AUTOSCALE_ENABLED_FOR_BEATWORKER', True):
            return

        if r15_only and not legacy_size_owner():
            return
        if self._defer_resize_for_retiring_dyno('autoscale'):
            return

        try:
            if r15_only:
                if self.detected_r15:
                    self.upscale_formation_to_next_level()
                return

            thread_count = None
            if getattr(settings, 'DYNO_LOG_THREADS_USED', False) and not self.remote_monitoring:
                thread_count = self.set_threads_used()
            if not legacy_size_owner():
                self._restart_for_r14_pressure(require_no_upscale=True)
                return

            if continuous and logger.isEnabledFor(logging.DEBUG):
                self._log_autoscale_stats(thread_count)

            if self.requires_upscale or self.any_sibling_requires_upscale:
                self.upscale_formation_to_next_level()
            else:
                self.check_and_downscale_to_original_formation_size()
        except Exception as e:
            logger.error(f"Failed to autoscale dyno {self.dyno_name}. Error: {e}", exc_info=True)

    def _log_autoscale_stats(self, thread_count):
        try:
            if self.remote_monitoring:
                thread_count = cache.get(self.threads_used_cache_key)
            elif thread_count is None:
                thread_count = threading.active_count()
            logger.debug(
                "Dyno %s stats: Formation Size: %s, Memory Usage: %s / %s MB, "
                "Load Avg (1min): %s, Tasks in Queue: %s, Threads Used: %s, "
                "Detected R14: %s, Detected R15: %s",
                self.dyno_name, self.formation_size, self.current_memory_usage, self.available_memory,
                self.avg_load_1min, self.tasks_in_queue, thread_count, self.detected_r14, self.detected_r15,
            )
        except Exception:
            logger.debug("Could not read autoscale diagnostics for %s.", self.dyno_name, exc_info=True)

    def _check_formation_on_startup(self):
        """
        Safety check on startup: detect if formation is stuck in an upscaled state
        (e.g., due to Redis key loss or missed downscale) and restore the downscale timer.
        Also ensures original_formation_size is always recorded as a baseline.
        """
        if not legacy_size_owner():
            return
        try:
            current_size = self.formation_size
            original_size = self.original_formation_size
            upscale_until = cache.get(self.upscale_until_cache_key)

            if original_size and original_size in DYNO_SIZES and current_size in DYNO_SIZES:
                current_memory = DYNO_SIZES[current_size]["memory"]
                original_memory = DYNO_SIZES[original_size]["memory"]

                if current_memory > original_memory and not upscale_until:
                    # Formation is upscaled but there's no upscale_until key —
                    # the downscale timer was lost. Restore it so downscale can proceed.
                    logger.warning(
                        f"Startup safety check: {self.formation_name} is on {current_size} "
                        f"but original size is {original_size} with no upscale_until key. "
                        f"Restoring downscale timer."
                    )
                    delta = getattr(settings, 'DYNO_DOWNSCALE_CHECK_INTERVAL', 300) + \
                            getattr(settings, 'DYNO_AUTOSCALE_INTERVAL', 30)
                    until = timezone.now() + timedelta(seconds=delta)
                    cache.set(self.upscale_until_cache_key, until, timeout=delta)

            # Always set original formation size on startup if not already set
            if not original_size:
                previous = self.previous_formation_size
                if previous and previous in DYNO_SIZES:
                    # Formation is above the lowest tier with no recorded original —
                    # assume it was upscaled and the original was lost (e.g. Redis
                    # eviction, phantom clear).  Record the previous tier so the
                    # downscale path can recover instead of staying stuck.
                    self.set_original_formation_size(value=previous)
                    logger.warning(
                        f"Startup safety check: {self.formation_name} is on {current_size} "
                        f"with no original size recorded. Assuming original was {previous} "
                        f"and restoring downscale timer."
                    )
                    delta = getattr(settings, 'DYNO_DOWNSCALE_CHECK_INTERVAL', 300) + \
                            getattr(settings, 'DYNO_AUTOSCALE_INTERVAL', 30)
                    until = timezone.now() + timedelta(seconds=delta)
                    cache.set(self.upscale_until_cache_key, until, timeout=delta)
                else:
                    self.set_original_formation_size()
                    logger.info(
                        f"Recorded baseline formation size for {self.formation_name}: {current_size}"
                    )
        except Exception as e:
            logger.error(f"Startup formation check failed: {e}", exc_info=True)

    def _run_continuous(self):
        # Legacy size recovery is independent of observation and zombie checks.
        self._check_formation_on_startup()

        while not self._stop_autoscale_event.is_set():
            status = self._check_in_dyno_status()
            if status == 'published':
                self.check_for_sibling_zombie_dynos()
                self.autoscale(continuous=True)
            elif status == 'memory_pressure':
                self.autoscale(continuous=True, r15_only=True)

            # Jitter ±20 % to prevent thundering-herd across dynos
            base = settings.DYNO_AUTOSCALE_INTERVAL
            jitter = base * random.uniform(-0.2, 0.2)
            time.sleep(base + jitter)

    def _run_continuous_file_cleaning(self):
        while not self._stop_file_cleaning_event.is_set():
            self.check_and_clean_old_files()
            time.sleep(settings.DYNO_AUTOSCALE_INTERVAL)

    def check_and_clean_old_files(self):
        """
        Check if it's time to clean old files and perform the cleaning if necessary.
        """
        # Get the file cleaning interval from settings or use default (1 hour)
        file_cleaning_interval = int(getattr(settings, 'DYNO_FILE_CLEANING_INTERVAL', 60 * 60))

        # Get the file age threshold from settings or use default (48 hours)
        file_age_hours = getattr(settings, 'DYNO_FILE_AGE_THRESHOLD', 48)

        # Get the directory to clean from settings or use default (/tmp)
        directory = getattr(settings, 'DYNO_FILE_CLEANING_DIRECTORY', '/tmp')

        # Check if file cleaning is enabled
        if not getattr(settings, 'DYNO_FILE_CLEANING_ENABLED', False):
            return

        # Check if it's time to clean files
        now = timezone.now()
        if (self._last_file_cleaning is None or 
            (now - self._last_file_cleaning).total_seconds() >= file_cleaning_interval):

            logger.debug(f"Starting to clean files in {directory} older than {file_age_hours} hours on dyno {self.dyno_name}...")

            # Clean old files
            success = self.clean_old_files(directory=directory, hours=file_age_hours)

            if success:
                # Update the last file cleaning timestamp
                self._last_file_cleaning = now
                logger.debug(f"Successfully cleaned old files in {directory} on dyno {self.dyno_name}. Next cleaning in {file_cleaning_interval/3600:.1f} hours.")
            else:
                # If cleaning failed, try again after a shorter interval
                self._last_file_cleaning = now - timedelta(seconds=file_cleaning_interval * 0.9)
                logger.warning(f"Failed to clean old files in {directory} on dyno {self.dyno_name}. Will try again soon.")

    def start_continuous_autoscale(self):
        """
        Start continuous autoscaling on a separate thread.
        """
        with self._thread_lock:
            if self._autoscale_thread and self._autoscale_thread.is_alive():
                logger.warning(f"Autoscaling thread for {self.dyno_name} is already running.")
                return

            self._stop_autoscale_event.clear()
            self._autoscale_thread = threading.Thread(target=self._supervised_run, args=(), daemon=True)
            self._autoscale_thread.start()
            logger.debug(f"Continuous autoscaling thread started for {self.dyno_name} with interval {settings.DYNO_AUTOSCALE_INTERVAL} seconds.")
            return self._autoscale_thread

    def start_continuous_file_cleaning(self):
        """
        Start continuous file cleaning on a separate thread.
        """
        with self._thread_lock:
            if self._file_cleaning_thread and self._file_cleaning_thread.is_alive():
                logger.warning(f"File cleaning thread for {self.dyno_name} is already running.")
                return

            self._stop_file_cleaning_event.clear()
            self._file_cleaning_thread = threading.Thread(target=self._supervised_run_file_cleaning, args=(), daemon=True)
            self._file_cleaning_thread.start()
            logger.debug(f"Continuous file cleaning thread started for {self.dyno_name} with interval {settings.DYNO_AUTOSCALE_INTERVAL} seconds.")
            return self._file_cleaning_thread

    def stop_continuous_autoscale(self):
        """Stop the continuous autoscaling thread."""
        with self._thread_lock:
            self.remove_dyno_from_alive_cache()

            if not self._autoscale_thread or not self._autoscale_thread.is_alive():
                return

            self._stop_autoscale_event.set()

            # Ensure we are not calling join on the current thread
            if threading.current_thread() != self._autoscale_thread:
                self._autoscale_thread.join()

            self._autoscale_thread = None
            logger.debug(f"Stopped continuous autoscaling for {self.dyno_name}.")

    def stop_continuous_file_cleaning(self):
        """Stop the continuous file cleaning thread."""
        with self._thread_lock:
            if not self._file_cleaning_thread or not self._file_cleaning_thread.is_alive():
                return

            self._stop_file_cleaning_event.set()

            # Ensure we are not calling join on the current thread
            if threading.current_thread() != self._file_cleaning_thread:
                self._file_cleaning_thread.join()

            self._file_cleaning_thread = None
            logger.debug(f"Stopped continuous file cleaning for {self.dyno_name}.")

    def _supervised_run(self):
        """Supervised loop to restart the thread if it exits."""
        while not self._stop_autoscale_event.is_set():
            try:
                self._run_continuous()
            except Exception as e:
                logger.error(f"Autoscaler thread for {self.dyno_name} crashed. Restarting... Error: {e}", exc_info=True)
                time.sleep(15)  # Short delay before restarting

    def _supervised_run_file_cleaning(self):
        """Supervised loop to restart the file cleaning thread if it exits."""
        while not self._stop_file_cleaning_event.is_set():
            try:
                self._run_continuous_file_cleaning()
            except Exception as e:
                logger.error(f"File cleaning thread for {self.dyno_name} crashed. Restarting... Error: {e}", exc_info=True)
                time.sleep(15)  # Short delay before restarting

    def _claim_current_dyno(self, check_in_epoch, alive_ttl):
        """Claim or renew this incarnation before any telemetry read."""
        if self.remote_monitoring:
            logger.debug(
                "Remote monitoring does not claim dyno ownership for %s/%s.",
                self.app_name, self.dyno_name,
            )
            return False, None
        owner_token = self._dyno_owner_token
        if getattr(self, '_dyno_owner_fenced', False):
            return False, None
        if not self.app_name or not self.dyno_name:
            return False, None
        adapter = _IndexAdapter(self.app_name, primary=True)
        status = adapter.claim_and_register(
            self.dyno_name,
            owner_token,
            check_in_epoch,
            timezone.now(),
            alive_ttl,
            allow_new_claim=not getattr(self, '_dyno_owner_claimed', False),
        )
        if status == 'claimed':
            self._dyno_owner_claimed = True
            self._last_registered_score = check_in_epoch
            return True, check_in_epoch
        if status in ('owner_mismatch', 'contended'):
            owner_status, current_owner, _ = adapter.get_owner_and_score(self.dyno_name)
            lost_or_replaced = (
                owner_status == 'missing'
                or (owner_status == 'found' and current_owner != owner_token)
            )
            if lost_or_replaced:
                adapter.mark_owner_contention(self.dyno_name, owner_token)
            if lost_or_replaced and getattr(self, '_dyno_owner_claimed', False):
                self._dyno_owner_fenced = True
                logger.warning(
                    "Dyno ownership for %s/%s was lost or replaced; this process is fenced until restart.",
                    self.app_name, self.dyno_name,
                )
            else:
                logger.debug(
                    "Dyno ownership for %s/%s is held by another process; automatic mutations are paused.",
                    self.app_name, self.dyno_name,
                )
            return False, None
        logger.warning(
            "Dyno ownership for %s/%s is unavailable; automatic mutations are paused for this tick.",
            self.app_name, self.dyno_name,
        )
        return False, None

    def record_memory_pressure(self, mem_mb):
        """Nudge this dyno into a fail-closed state during an immediate memory spike."""
        if not _is_finite_number(mem_mb, positive=True):
            raise ValueError('memory pressure must be a positive number')
        source_epoch = time.time()
        self.record_memory_reading(mem_mb, source_epoch)
        if self.remote_monitoring or not self.app_name or not self.dyno_name:
            return False
        status = _IndexAdapter(
            self.app_name, primary=True,
        ).publish_memory_pressure(
            self.dyno_name,
            self._dyno_owner_token,
            source_epoch,
            float(mem_mb),
            _memory_veto_ttl(),
        )
        if status != 'published':
            logger.warning(
                "Failed to publish memory-pressure veto for %s/%s: %s.",
                self.app_name, self.dyno_name, status,
            )
            return False
        return True

    def _current_memory_pressure_veto(self):
        """Return one validated nudge tuple, or fail closed on corrupt state."""
        value = _IndexAdapter(self.app_name, primary=True).get_scalar(
            _dyno_memory_veto_key(self.app_name, self.dyno_name),
        )
        if value is None:
            return None
        if not (
            isinstance(value, (tuple, list))
            and len(value) == 3
            and isinstance(value[0], str)
            and value[0]
            and _is_finite_number(value[1])
            and _is_finite_number(value[2], positive=True)
        ):
            raise ValueError('memory-pressure veto is malformed')
        return value

    def check_in_dyno(self):
        """Return whether the current process published a new observation."""
        return self._check_in_dyno_status() == 'published'

    def _observe_runtime_shadow(self, memory_mb, memory_source_epoch, load_1m, load_source_epoch):
        observer_v2 = getattr(settings, 'DYNO_RUNTIME_SHADOW_OBSERVER_V2', None)
        observer = observer_v2 if observer_v2 is not None else getattr(settings, 'DYNO_RUNTIME_SHADOW_OBSERVER', None)
        if observer is None:
            return
        dyno_id = self._verified_self_dyno_id
        if not dyno_id:
            return
        try:
            observation = dict(
                app_name=self.app_name,
                dyno_name=self.dyno_name,
                dyno_id=dyno_id,
                observed_at=time.time(),
                memory_mb=memory_mb,
                memory_source_epoch=memory_source_epoch,
                load_1m=load_1m,
            )
            if observer_v2 is not None:
                observation["load_source_epoch"] = load_source_epoch
            observer(**observation)
        except Exception as error:
            logger.warning(
                "Dyno shadow observation failed for %s/%s: %s",
                self.app_name, self.dyno_name, type(error).__name__,
            )

    def _check_in_dyno_status(self):
        """Claim this dyno, sample it, and publish one owner-bound observation."""
        now = timezone.now()
        # Reset per-cycle readiness so the own-member ZSCORE is recomputed once
        # per autoscale cycle, not per reader.
        self.__dict__.pop('index_ready', None)
        ttl = int(getattr(settings, 'DYNO_ZOMBIE_THRESHOLD', 24 * 60 * 60))
        alive_ttl = _dyno_alive_ttl(ttl)
        check_in_epoch = now.timestamp()
        claimed, score = self._claim_current_dyno(check_in_epoch, alive_ttl)
        if not claimed:
            return 'claim_failed'

        self.__dict__['_strict_observation_logs'] = _STRICT_LOGS_UNSET
        memory_reading = None
        load = None
        load_source_epoch = None
        try:
            try:
                memory_reading = self.memory_reading_for_observation
            except Exception:
                logger.warning(
                    "Failed to read memory telemetry for %s/%s; leaving memory unknown.",
                    self.app_name, self.dyno_name, exc_info=True,
                )
            mem = memory_reading.value if memory_reading is not None else None
            if not _is_finite_number(mem, positive=True):
                mem = None
            stable = False
            if mem is not None:
                try:
                    stable = self.record_memory_reading(
                        mem=mem,
                        source_epoch=memory_reading.source_epoch,
                    )
                except Exception:
                    logger.warning(
                        "Failed to update memory history for %s/%s; publishing an unstable sample.",
                        self.app_name, self.dyno_name, exc_info=True,
                    )

            try:
                load = self.load_1min_for_observation
                load_source_epoch = self.__dict__.get('_strict_load_source_epoch')
            except Exception:
                logger.warning(
                    "Failed to read load telemetry for %s/%s; leaving load unknown.",
                    self.app_name, self.dyno_name, exc_info=True,
                )
            if not _is_finite_number(load):
                load = None
        finally:
            self.__dict__.pop('_strict_observation_logs', None)
            self.__dict__.pop('_strict_load_source_epoch', None)

        try:
            memory_veto = self._current_memory_pressure_veto()
        except Exception:
            memory_veto = True
            logger.warning(
                "Could not read the memory-pressure veto for %s/%s; publishing unstable.",
                self.app_name, self.dyno_name, exc_info=True,
            )
        if memory_veto is not None:
            stable = False

        values = [
            (_observation_key(self.app_name, self.dyno_name), _DynoObservation(
                check_in_epoch, mem, stable, load,
            ), ttl),
            (f'heroku:dyno_alive:{self.dyno_name}', now, alive_ttl),
            (f'heroku:dyno_memory:{self.dyno_name}', mem, ttl),
            (f'heroku:dyno_memory_stable:{self.dyno_name}', stable if mem is not None else None, ttl),
            (f'heroku:dyno_load:{self.dyno_name}', load, ttl),
        ]
        adapter = _IndexAdapter(self.app_name, primary=True)
        memory_source_epoch = memory_reading.source_epoch if memory_reading is not None else None
        status = adapter.publish_owned_values(
            self.dyno_name,
            self._dyno_owner_token,
            score,
            values,
            observation_memory=mem,
            observation_epoch=memory_source_epoch,
        )
        if status == 'published':
            self._observe_runtime_shadow(mem, memory_source_epoch, load, load_source_epoch)
            return status
        if status in ('owner_mismatch', 'contended'):
            owner_status, current_owner, _ = adapter.get_owner_and_score(self.dyno_name)
            if (
                owner_status == 'missing'
                or (owner_status == 'found' and current_owner != self._dyno_owner_token)
            ):
                self._dyno_owner_fenced = True
                logger.warning(
                    "Dyno ownership for %s/%s changed during sampling; this process is fenced until restart.",
                    self.app_name, self.dyno_name,
                )
            else:
                logger.warning(
                    "Observation publication for %s/%s did not win its ownership check; strict readers remain closed.",
                    self.app_name, self.dyno_name,
                )
        elif status == 'memory_pressure':
            logger.warning(
                "Memory-pressure veto remains for %s/%s; the pre-nudge observation was not replaced.",
                self.app_name, self.dyno_name,
            )
        else:
            logger.warning(
                "Failed to publish the observation for %s/%s; strict readers remain closed.",
                self.app_name, self.dyno_name,
            )
        return status

    def remove_dyno_from_alive_cache(self, dyno_name=None, expected_owner=None, expected_score=None):
        """Remove only the dyno incarnation that still matches the captured owner."""
        dyno_name = dyno_name or self.dyno_name
        if not self.app_name or not dyno_name:
            return False
        if self.remote_monitoring and dyno_name == self.dyno_name:
            return False
        if dyno_name == self.dyno_name:
            owner = expected_owner or self._dyno_owner_token
            score = expected_score if expected_score is not None else getattr(self, '_last_registered_score', None)
        else:
            owner = expected_owner
            score = expected_score
            if owner is None or score is None:
                status, owner, score = _IndexAdapter(
                    self.app_name, primary=True,
                ).get_owner_and_score(dyno_name)
                if status != 'found':
                    return False
        if owner is None or score is None:
            return False
        keys = [_observation_key(self.app_name, dyno_name)]
        keys.extend(f'heroku:dyno_{prefix}:{dyno_name}' for prefix in self._DYNO_CACHE_PREFIXES)
        keys.append(_dyno_memory_veto_key(self.app_name, dyno_name))
        status = _IndexAdapter(self.app_name, primary=True).delete_owned_dyno(
            dyno_name, owner, score, keys,
        )
        if status == 'deleted':
            return True
        if status == 'unavailable':
            logger.warning(
                "Could not remove the owned dyno record for %s/%s.",
                self.app_name, dyno_name, exc_info=True,
            )
        return False

    def _sibling_values(self, metric, *, primary=False):
        """Read a formation's sibling metrics. None means the read is uncertain."""
        adapter = _IndexAdapter(self.app_name, primary=primary)
        try:
            members = adapter.fresh_members(_stale_cutoff())
            if members is None or (primary and self.dyno_name not in members):
                return None
            prefix = f'{self.formation_name}.'
            siblings = [member for member in members if member.startswith(prefix) and member != self.dyno_name]
            if not siblings:
                return []
            values = adapter.mget([f'heroku:dyno_{metric}:{member}' for member in siblings])
            if values is None or len(values) != len(siblings):
                return None
            readings = []
            for member, value in zip(siblings, values):
                if _is_finite_number(value):
                    readings.append((member, value))
                elif primary:
                    return None
            return readings
        except Exception:
            logger.warning("Could not read sibling %s metrics for %s.", metric, self.formation_name, exc_info=True)
            return None

    def _formation_observation_snapshot(self):
        """Read one scored formation roster and one consistent observation batch."""
        adapter = _IndexAdapter(self.app_name, primary=True)
        cutoff = _stale_cutoff()
        try:
            scored_members = adapter.fresh_scored_members(cutoff)
            if scored_members is None:
                return None
            prefix = f'{self.formation_name}.'
            roster = tuple(
                (member, float(score))
                for member, score in scored_members
                if member.startswith(prefix)
            )
            if not roster or self.dyno_name not in {member for member, _ in roster}:
                return None
            owner_keys = []
            observation_keys = []
            contention_keys = []
            veto_keys = []
            for member, _ in roster:
                owner_keys.append(_dyno_owner_key(self.app_name, member))
                observation_keys.append(
                    _observation_key(self.app_name, member),
                )
                contention_keys.append(
                    _dyno_contention_key(self.app_name, member),
                )
                veto_keys.append(_dyno_memory_veto_key(self.app_name, member))
            keys = []
            for owner_key, observation_key in zip(
                owner_keys, observation_keys,
            ):
                keys.extend([owner_key, observation_key])
            values = adapter.mget(keys)
            if values is None or len(values) != len(keys):
                return None
            contention_count = adapter.exists(contention_keys)
            veto_count = adapter.exists(veto_keys)
            if (
                contention_count is None
                or veto_count is None
                or contention_count
                or veto_count
            ):
                return None
            observations = {}
            owners = {}
            for index, (member, score) in enumerate(roster):
                owner = values[index * 2]
                value = values[index * 2 + 1]
                if not isinstance(owner, str) or not owner:
                    return None
                if not isinstance(value, (tuple, list)) or len(value) != 4:
                    return None
                epoch, memory_mb, stable, load_1m = value
                if (
                    not _is_finite_number(epoch)
                    or epoch != score
                    or not isinstance(stable, bool)
                ):
                    return None
                observations[member] = _DynoObservation(
                    epoch,
                    memory_mb if _is_finite_number(memory_mb, positive=True) else None,
                    stable,
                    load_1m if _is_finite_number(load_1m) else None,
                )
                owners[member] = owner
            return roster, observations, cutoff, owners
        except Exception:
            logger.warning(
                "Could not read scored formation observations for %s.",
                self.formation_name, exc_info=True,
            )
            return None

    def _formation_observation_snapshot_current(self, snapshot):
        """Re-read membership and degradation state before a destructive resize."""
        roster, _, cutoff, expected_owners = snapshot
        if _registry_degraded(self):
            return False
        adapter = _IndexAdapter(self.app_name, primary=True)
        try:
            status, current_owners, contentions, degraded = (
                adapter.read_formation_validation_state(
                    f'{self.formation_name}.', _stale_cutoff(), roster,
                )
            )
            if status != 'ok' or degraded:
                return False
            return all(
                isinstance(owner, str)
                and owner
                and owner == expected_owners.get(member)
                and contention is None
                for (member, _), owner, contention in zip(
                    roster, current_owners, contentions,
                )
            )
        except Exception:
            logger.warning(
                "Could not re-read scored formation observations for %s.",
                self.formation_name, exc_info=True,
            )
            return False

    def _normal_memory_snapshot_allows_downscale(self, snapshot, threshold):
        if not threshold or math.isinf(threshold):
            return False
        _, observations, _, _ = snapshot
        own = observations[self.dyno_name]
        if own.memory_mb is None or own.memory_mb >= threshold:
            return False
        for member, observation in observations.items():
            if observation.memory_mb is None:
                return False
            if (member != self.dyno_name and observation.memory_mb >= threshold
                    and not observation.stable):
                return False
        return True

    def _stability_snapshot_allows_downscale(self, snapshot, threshold):
        if (
            self.is_on_original_formation_size_or_lower
            or not threshold
            or math.isinf(threshold)
        ):
            return False
        _, observations, _, _ = snapshot
        own = observations[self.dyno_name]
        if own.memory_mb is None or own.load_1m is None or not own.stable:
            return False
        load_threshold = getattr(settings, 'DYNO_STABILITY_LOAD_THRESHOLD', 1.0)
        for member, observation in observations.items():
            if observation.memory_mb is None or observation.load_1m is None:
                return False
            if observation.load_1m >= load_threshold:
                return False
            if (member != self.dyno_name and observation.memory_mb >= threshold
                    and not observation.stable):
                return False
        return True

    @property
    def any_sibling_still_high_memory(self):
        """
        Return True if any *other* dyno of the same formation has memory at or above
        the downscale threshold.  Prevents a low-memory dyno from downscaling the
        whole formation while a sibling is still running hot.
        """
        threshold = self._downscale_memory_threshold
        if not threshold or math.isinf(threshold):
            return False
        snapshot = self._formation_observation_snapshot()
        if snapshot is None:
            return True
        _, observations, _, _ = snapshot
        if any(observation.memory_mb is None for observation in observations.values()):
            return True
        hot_siblings = [
            observation for member, observation in observations.items()
            if member != self.dyno_name and observation.memory_mb >= threshold
        ]
        if any(not observation.stable for observation in hot_siblings):
            return True
        return not self._formation_observation_snapshot_current(snapshot)

    @property
    def any_sibling_requires_upscale(self):
        """Return True if any sibling's memory exceeds the upscale threshold
        for the current tier.  Allows a cool dyno to trigger chain upscale
        on behalf of a hot sibling whose autoscale thread may have stalled."""
        upscale_pct = getattr(settings, 'UPSCALE_PERCENTAGE_HIGH_MEM_USE', 80)
        threshold_mb = self.available_memory * upscale_pct / 100
        for sibling_name, sibling_mem in self._sibling_values('memory') or ():
            if sibling_mem > threshold_mb:
                logger.info(
                    f"Sibling {sibling_name} at {sibling_mem:.0f}MB > "
                    f"{threshold_mb:.0f}MB upscale threshold; advocating upscale "
                    f"for {self.formation_name}."
                )
                return True
        return False

    @property
    def is_formation_idle(self):
        """True when *all* dynos in the formation (self + siblings) have a load
        average below ``DYNO_STABILITY_LOAD_THRESHOLD`` (default 1.0).

        A high load average indicates a CPU-bound task is still executing.  Even
        if RSS has plateaued, downscaling during active work would kill that task
        (formation resize restarts all dynos).  Gate the stability-based
        downscale on this check to let heavy tasks finish first.
        """
        if _index_fail_closed(self):
            return False
        snapshot = self._formation_observation_snapshot()
        if snapshot is None:
            return False
        threshold = getattr(settings, 'DYNO_STABILITY_LOAD_THRESHOLD', 1.0)
        _, observations, _, _ = snapshot
        for member, observation in observations.items():
            if observation.load_1m is None:
                return False
            if observation.load_1m >= threshold:
                logger.debug(
                    f"[{self.formation_name}] {member} load "
                    f"{observation.load_1m:.2f} >= {threshold:.1f}; formation not idle."
                )
                return False
        return self._formation_observation_snapshot_current(snapshot)

    def check_for_sibling_zombie_dynos(self):
        """
        Check if any sibling dynos are marked as alive in the cache and restart them if they
        have not checked in within the DYNO_ZOMBIE_THRESHOLD seconds.

        Phase C: the app ZSET is the authoritative member list and the only
        one; no compatibility SCAN remains.  Stale members are evaluated for
        zombie restart BEFORE the one
        bounded ZREMRANGEBYSCORE prune, which runs only when that evaluation
        completes; uncertain stale reads skip the prune. Restart evaluation
        uses the zombie threshold, while pruning waits for the longer heartbeat
        grace so a contested restart survives for the next scan.
        Gracefully removed dynos stay absent (ZREM); missing alive values are
        skipped.  Destructive restart requires readiness; otherwise the check
        prunes once and returns.
        """
        # Make sure only one dyno is checking for zombie dynos at a time
        zombie_threshold = int(
            getattr(settings, 'DYNO_ZOMBIE_THRESHOLD', 24 * 60 * 60)
        )
        try:
            with cache.lock('heroku:lock:dyno_alive_check', expire=30):
                now_epoch = time.time()
                cutoff = now_epoch - zombie_threshold
                prune_cutoff = now_epoch - _dyno_alive_ttl(zombie_threshold)
                if _index_fail_closed(self):
                    self.prune_stale_index_members(prune_cutoff)
                    return
                adapter = self._zombie_adapter()
                if not self._evaluate_zombies(
                    adapter, adapter.stale_members(cutoff), zombie_threshold,
                ):
                    return  # uncertain stale read: no restart, no prune
                self.prune_stale_index_members(prune_cutoff)
                self._evaluate_zombies(adapter, adapter.fresh_members(cutoff), zombie_threshold)
        except _LockExpiryErrors:
            logger.warning("Lock 'heroku:lock:dyno_alive_check' expired before release.")

    def _zombie_adapter(self):
        """Single adapter for the zombie-check read (stale + fresh members)."""
        return _IndexAdapter(self.app_name)

    def _evaluate_zombies(self, adapter, members, zombie_threshold):
        """Restart sibling zombies from batched alive timestamps; missing skipped.

        False only on uncertain reads (members or alive-value batch is None);
        True when evaluation completed, including empty members and missing
        individual alive values.
        """
        if members is None:
            return False
        if not members:
            return True
        values = adapter.mget([f'heroku:dyno_alive:{m}' for m in members])
        if values is None:
            return False
        for member, last_checkin in zip(members, values):
            if member == self.dyno_name or not last_checkin:
                continue
            last_checkin_seconds_ago = (timezone.now() - last_checkin).total_seconds()
            if last_checkin_seconds_ago > zombie_threshold:
                logger.warning(
                    f"Zombie dyno detected: {member}. Last check-in: "
                    f"{last_checkin_seconds_ago // 60:.0f} minutes ago."
                )
                self.restart_zombie_dyno(member, zombie_threshold)
        return True

    def prune_stale_index_members(self, cutoff=None):
        """Prune stale index members with one bounded ZREMRANGEBYSCORE.

        Must run under the zombie-check lock (callers hold it).  Never prunes
        member-by-member; a single range removal keeps cleanup O(log N).
        ``cutoff`` defaults to a fresh ``_stale_cutoff()`` when not supplied,
        preserving all other callers; the zombie check passes the longer
        heartbeat cutoff so a contested member survives the next retry scan.
        """
        try:
            _IndexAdapter(self.app_name).zremrangebyscore('-inf', cutoff if cutoff is not None else _stale_cutoff())
        except Exception:
            logger.warning(
                "Failed to prune stale indexed dyno registry members for app %s.",
                self.app_name, exc_info=True,
            )

    def restart_zombie_dyno(self, dyno_name, zombie_threshold=None):
        """Revalidate stale liveness on the primary before restart dispatch."""
        threshold = (
            getattr(settings, 'DYNO_ZOMBIE_THRESHOLD', 24 * 60 * 60)
            if zombie_threshold is None else zombie_threshold
        )
        if self._stale_zombie_evidence(dyno_name, threshold) is None:
            return False
        return self.restart_dyno(
            dyno_name,
            zombie_threshold=threshold,
        )

    def _stale_zombie_evidence(self, dyno_name, threshold):
        """Return owner and score when bounded stale-heartbeat evidence remains."""
        status, owner, score, alive = _IndexAdapter(
            self.app_name, primary=True,
        ).read_owned_liveness(dyno_name)
        if status not in ('found', 'owner_expired') or not isinstance(alive, datetime):
            return None
        seconds_ago = (timezone.now() - alive).total_seconds()
        if seconds_ago <= threshold:
            return None
        return owner, score


    def upscale_formation_to_next_level(self):
        '''
        Upscale the dyno to the next level in the hierarchy for a period of X hours
        '''
        if not legacy_size_owner() or self._defer_resize_for_retiring_dyno('upscale'):
            return

        # Check if not already at max_dyno_size
        if self.formation_size == self.max_dyno_size:
            logger.debug(f"Formation {self.formation_name} is already at max size {self.max_dyno_size}.")
            return

        # Once a formation has already been upscaled, keep it at the current size
        # until the downscale window is reached instead of chaining another upscale
        # just because R15 is still present after the first resize.
        # However, if memory is genuinely above the upscale threshold on the
        # CURRENT tier (not stale R15 from the pre-upscale tier), allow chaining
        # to the next tier — otherwise the formation gets stuck at an intermediate
        # size while workers hit R14/R15.
        if cache.get(self.upscale_until_cache_key):
            upscale_threshold = getattr(settings, 'UPSCALE_PERCENTAGE_HIGH_MEM_USE', 80)
            self_hot = self.current_memory_usage_percentage > upscale_threshold
            sibling_hot = self.any_sibling_requires_upscale
            if not self_hot and not sibling_hot:
                logger.debug(
                    f"Formation {self.formation_name} is already within the keep-upscaled window; "
                    f"skipping repeat upscale."
                )
                return
            logger.warning(
                f"Formation {self.formation_name} is within the keep-upscaled window but "
                f"{'self' if self_hot else 'sibling'} memory is still high "
                f"(self={self.current_memory_usage_percentage:.1f}%) — "
                f"allowing chain upscale to next tier."
            )

        diagnostic_message = None
        if not self.remote_monitoring:
            memory_usage = self.current_memory_usage
            available_memory = self.available_memory
            memory_percentage = (
                round(memory_usage / available_memory * 100, 2)
                if memory_usage and available_memory else 0.0
            )
            diagnostic_message = (
                f"Memory usage is greater than {memory_percentage:.2f}% of "
                f"available RAM ({available_memory}MB). Current memory usage: "
                f"{memory_usage:.2f}MB. Triggered by dyno {self.dyno_name}"
            )

        # Ensure upscale is only executed once every settings.DYNO_TIME_BETWEEN_SCALES seconds for this dyno type
        _lock_proceed = True
        try:
            with cache.lock(self.upscaling_cache_key, expire=30):
                if self._defer_resize_for_retiring_dyno('upscale'):
                    _lock_proceed = False
                elif self.is_upscaling:
                    logger.debug(f"Upscaling formation {self.formation_name} is already in progress.")
                    _lock_proceed = False
                else:
                    if self.remote_monitoring:
                        self.set_upscaling()
                    else:
                        status = _IndexAdapter(
                            self.app_name, primary=True,
                        ).reserve_owned_flag(
                            self.upscaling_cache_key,
                            self.dyno_name,
                            self._dyno_owner_token,
                            True,
                            settings.DYNO_TIME_BETWEEN_SCALES + 150,
                            formation_name=self.formation_name,
                            expected_formation_size=self.formation_size,
                        )
                        if status != 'reserved':
                            logger.info(
                                "Skipping upscale for %s/%s without a current owner reservation: %s.",
                                self.app_name, self.dyno_name, status,
                            )
                            _lock_proceed = False
        except _LockExpiryErrors:
            logger.warning(f"Lock '{self.upscaling_cache_key}' expired before release.")
            _lock_proceed = False
        if not _lock_proceed:
            return

        if diagnostic_message is not None:
            logger.warning(diagnostic_message)

        next_level = self.next_formation_size
        if not next_level:
            self.clear_upscaling()
            logger.debug("Formation is already at the highest level or unrecognized size.")
            return self.formation_size

        # Store original dyno size in Redis cache
        self.set_original_formation_size()

        # Update dyno to upscale (routed through call_heroku_api for rate limiting)
        url = f'https://api.heroku.com/apps/{self.app_name}/formation/{self.formation_name}'
        response = self.call_heroku_api("PATCH", url, data={"size": next_level})
        if response and response.status_code == 200:
            logger.info(f"Upscaled formation {self.formation_name} from {self.formation_size} to {next_level} with {DYNO_SIZES[next_level]['memory']} MB memory.")
            self._update_formation_size(next_level)
            cache.delete(self.memory_history_cache_key)

            # Set cache key that expires after min upscale duration to gate downscale checks
            delta = (
                getattr(settings, 'DYNO_DOWNSCALE_CHECK_INTERVAL', 300) +
                getattr(settings, 'DYNO_MIN_UPSCALE_DURATION', 300) +
                getattr(settings, 'DYNO_AUTOSCALE_INTERVAL', 30)
            )
            until = timezone.now() + timedelta(seconds=delta)
            cache.set(self.upscale_until_cache_key, until, timeout=delta)

            if not self.remote_monitoring:
                self.stop_continuous_autoscale()
        else:
            self.clear_upscaling()
            self._log_api_failure(f"Failed to upscale formation {self.formation_name}", response)

    def downscale_formation_if_allowed(self, *, on_shutdown=False):
        """Evaluate one policy decision and carry its snapshot into the mutation."""
        if not legacy_size_owner() or _index_fail_closed(self) or self.requires_upscale:
            return False
        queue_allowed = (
            True if on_shutdown
            else (self.downscale_on_non_empty_queue or self.no_tasks_in_queue)
        )
        decision = self._evaluate_downscale_snapshot(queue_allowed=queue_allowed)
        if decision.snapshot is None or not (decision.normal or decision.stability):
            return False
        self.downscale_formation_to_original_size(decision=decision)
        return True

    def _restart_for_r14_pressure(self, *, require_no_upscale=False):
        if not (
            self.current_memory_usage_percentage > getattr(settings, 'DOWNSCALE_PERCENTAGE_HIGH_MEM_USE', 105)
            and self.detected_r14
            and not self.detected_r15
        ):
            return False
        if require_no_upscale and (self.requires_upscale or self.any_sibling_requires_upscale):
            return False
        if not self.no_tasks_in_queue:
            return False
        self.restart_dyno()
        return True

    def check_and_downscale_to_original_formation_size(self):
        """
        Checks if formation should be downscaled by checking memory usage and downscale if necessary
        """
        if not legacy_size_owner():
            return
        # Check if formation is upscaled and if it should be downscaled
        check_interval = getattr(settings, 'DYNO_DOWNSCALE_CHECK_INTERVAL', 300)
        adapter = _IndexAdapter(self.app_name, primary=True)
        captured = adapter.capture_formation_cleanup_state(self.formation_name)
        if captured is None:
            logger.warning(
                "Skipping downscale check for %s/%s without a readable "
                "primary formation state.",
                self.app_name, self.formation_name,
            )
            return
        try:
            upscaled_until = adapter.decode_upscale_timer(captured[2])
        except Exception:
            logger.warning(
                "Skipping downscale check for %s/%s with an invalid upscale "
                "timer.",
                self.app_name, self.formation_name,
            )
            return
        if captured[2] is not None:
            # If the formation is already back at (or below) its original size,
            # any upscale_until key is a phantom artifact — e.g. from a sibling
            # that downscaled in the gap between an upscale API call and its
            # cache.set().  Clear it so the autoscaler can resume normal
            # R14/R15 detection instead of looping in the "Extending..." path.
            cleanup_status = adapter.clear_formation_if_at_or_below(
                self.formation_name,
                self.formation_size,
                captured=captured,
            )
            if cleanup_status == 'cleared':
                logger.info(
                    f"Cleared phantom upscale state for {self.formation_name}: "
                    f"formation is already at original size {self.formation_size}."
                )
                # Fall through to the R14 restart check below
            elif cleanup_status in ('not_at_or_below', 'no_baseline'):
                if cleanup_status == 'no_baseline':
                    logger.info(
                        "No recorded baseline for %s/%s; keeping the live "
                        "upscale timer and using the previous tier.",
                        self.app_name, self.formation_name,
                    )
                current_ttl = cache.ttl(self.upscale_until_cache_key)
                # Only allow downscale once we are within the final check_interval window.
                # Before that, keep the formation upscaled regardless of current memory —
                # this prevents an immediate downscale after an R15 restart clears memory.
                if current_ttl < check_interval:
                    if _index_fail_closed(self) or self.requires_upscale:
                        decision = _DownscaleDecision(False, False, None, None)
                    else:
                        queue_allowed = self.downscale_on_non_empty_queue or self.no_tasks_in_queue
                        decision = self._evaluate_downscale_snapshot(
                            queue_allowed=queue_allowed,
                            stability_ignores_queue=True,
                        )
                    if decision.normal or decision.stability:
                        if decision.stability and not decision.normal:
                            logger.info(
                                f"[{self.formation_name}] Memory stabilized at "
                                f"{self.current_memory_usage:.0f}MB near timer expiry — "
                                f"forcing downscale (restart alone won't reclaim RSS)."
                            )
                        self.downscale_formation_to_original_size(
                            decision=decision,
                            expected_upscale_timer_raw=captured[2],
                            expected_cleanup_capture=captured,
                        )
                        return
                    # Otherwise restart if hot, or extend the timer
                    if ((self.detected_r14 and not self.detected_r15) or self.is_still_high_memory_usage_for_downscale) and self.no_tasks_in_queue:
                        self.restart_dyno()
                    else:
                        load_avg = f'{self.avg_load_1min:.2f}' if self.avg_load_1min else 'unknown'
                        memory_usage = f'{self.current_memory_usage_percentage}% ({self.current_memory_usage:.2f}MB / {self.available_memory}MB)' \
                            if self.current_memory_usage and self.available_memory and self.current_memory_usage_percentage else 'unknown'
                        stable = self.is_memory_stabilized
                        history = cache.get(self.memory_history_cache_key) or []
                        logger.warning(f"Extending the time for {self.formation_name} to stay upscaled by {check_interval} seconds. "
                                        f"Current Memory Usage: {memory_usage}. Current Load Avg: {load_avg}. "
                                        f"Tasks in Queue: {self.tasks_in_queue}. "
                                        f"Memory Stabilized: {stable} ({len(history)} readings).")
                        new_until = upscaled_until + timedelta(seconds=check_interval)
                        new_timeout = current_ttl + check_interval
                        cache.set(self.upscale_until_cache_key, new_until, timeout=new_timeout)
                return
            else:
                logger.warning(
                    "Skipping phantom cleanup for %s/%s without a stable "
                    "primary state: %s.",
                    self.app_name, self.formation_name, cleanup_status,
                )
                return

        # if on original formation size and memory usage is high, restart the dyno
        if self._restart_for_r14_pressure():
            return

        # Guard: the upscale_until key may have expired while memory is still
        # elevated (e.g. R14 storm, slow GC).  Unconditionally downscaling here
        # would move the formation back to a smaller size that cannot handle the
        # current memory load, causing immediate R14 errors.  Re-check allow_downscale
        # and restore the upscale timer if the formation is not yet safe to shrink.
        # Only do this when the formation is actually upscaled above its original
        # size — otherwise we'd create a phantom upscale_until key on a formation
        # that was already downscaled, trapping siblings in an infinite extend loop.
        # Also skip when at the lowest tier with no original recorded (post-downscale
        # clear) — there's nothing to downscale from.
        can_be_upscaled = (
            not self.is_on_original_formation_size_or_lower
            and not (self.previous_formation_size is None and not self.original_formation_size)
        )
        if can_be_upscaled:
            queue_allowed = self.downscale_on_non_empty_queue or self.no_tasks_in_queue
            decision = self._evaluate_downscale_snapshot(queue_allowed=queue_allowed)
            if not (decision.normal or decision.stability):
                delta = getattr(settings, 'DYNO_DOWNSCALE_CHECK_INTERVAL', 300)
                new_until = timezone.now() + timedelta(seconds=delta)
                cache.set(self.upscale_until_cache_key, new_until, timeout=delta)
                logger.warning(
                    f"upscale_until key expired for {self.formation_name} but allow_downscale "
                    f"is False (memory still high or R14 active). Restoring upscale timer for "
                    f"{delta}s. Memory: {self.current_memory_usage:.0f}MB."
                )
                return
            self.downscale_formation_to_original_size(
                decision=decision,
                expected_upscale_timer_raw=captured[2],
                expected_cleanup_capture=captured,
            )
            return

        # Preserve the existing no-op cleanup when the formation is already at
        # or below its recorded original size.
        self.downscale_formation_to_original_size(
            expected_cleanup_capture=captured,
        )

    def downscale_formation_to_original_size(
        self, *, decision=None,
        expected_upscale_timer_raw=_UPSCALE_TIMER_UNBOUND,
        expected_cleanup_capture=None,
    ):
        if not legacy_size_owner() or self._defer_resize_for_retiring_dyno('downscale'):
            return

        if self.previous_formation_size is None:
            return

        adapter = _IndexAdapter(self.app_name, primary=True)
        if decision is None and expected_cleanup_capture is not None:
            cleanup_capture = tuple(expected_cleanup_capture)
            if len(cleanup_capture) != 3:
                logger.warning(
                    "Skipping scheduled cleanup for %s/%s without a "
                    "complete primary capture.",
                    self.app_name, self.formation_name,
                )
                return
            try:
                with cache.lock(self.downscale_cache_key, expire=30):
                    if self._defer_resize_for_retiring_dyno('downscale'):
                        return
                    if self.is_downscaling:
                        logger.debug(
                            "Downscale for %s is already reserved.",
                            self.formation_name,
                        )
                        return
                    cleanup_status = adapter.clear_formation_if_at_or_below(
                        self.formation_name,
                        self.formation_size,
                        captured=cleanup_capture,
                    )
                    if cleanup_status == 'cleared':
                        logger.debug(
                            "Formation %s is already at or below its "
                            "original size.", self.formation_name,
                        )
                    elif cleanup_status == 'no_baseline':
                        logger.info(
                            "Skipping scheduled cleanup for %s/%s after a "
                            "genuine baseline miss.",
                            self.app_name, self.formation_name,
                        )
                    else:
                        logger.warning(
                            "Skipping scheduled cleanup for %s/%s without "
                            "an unchanged primary state: %s.",
                            self.app_name, self.formation_name, cleanup_status,
                        )
            except _LockExpiryErrors:
                logger.warning(
                    f"Lock '{self.downscale_cache_key}' expired before release."
                )
            return

        raw_target = None
        target_from_decision = (
            decision is not None and decision.target_size is not None
        )
        if target_from_decision:
            original_formation_size = decision.target_size
        else:
            target_status, original_formation_size, raw_target = (
                adapter.read_downscale_target(
                    self.formation_name, self.formation_size,
                )
            )
            if target_status not in ('found', 'miss'):
                logger.warning(
                    "Skipping downscale for %s/%s without a usable baseline: %s.",
                    self.app_name, self.formation_name, target_status,
                )
                return
            if target_status == 'miss':
                logger.info(
                    f"No original formation size for {self.formation_name}, "
                    f"using previous tier {original_formation_size} as downscale target."
                )

        if target_from_decision and self.remote_monitoring:
            # The remote reservation compares the raw baseline. A carried
            # decision must re-read it and prove it still resolves to the
            # target the decision used.
            baseline_status, baseline_target, raw_target = (
                adapter.read_downscale_target(
                    self.formation_name, self.formation_size,
                )
            )
            if (
                baseline_status not in ('found', 'miss')
                or baseline_target != original_formation_size
            ):
                logger.warning(
                    "Skipping remote downscale for %s/%s without a current "
                    "baseline target: %s.",
                    self.app_name, self.formation_name,
                    baseline_status,
                )
                return

        remote_formation_raw = None
        effective_current_size = self.formation_size
        if self.remote_monitoring:
            tier_status, effective_current_size, remote_formation_raw = (
                adapter.read_formation_size_raw(
                    self.formation_name, self.formation_size,
                )
            )
            if tier_status == 'unavailable':
                logger.warning(
                    "Skipping remote downscale for %s/%s without a usable "
                    "primary formation tier.",
                    self.app_name, self.formation_name,
                )
                return

        current_memory = DYNO_SIZES.get(effective_current_size, {}).get(
            'memory', 0,
        )
        target_memory = DYNO_SIZES.get(original_formation_size, {}).get(
            'memory', 0,
        )
        at_or_below_target = bool(
            current_memory and target_memory and current_memory <= target_memory
        )

        # Managed local direct calls still get a fresh conservative decision.
        # A remote operator keeps the prior explicit downscale entry point.
        if (
            decision is None
            and not self.remote_monitoring
            and not at_or_below_target
        ):
            queue_allowed = self.downscale_on_non_empty_queue or self.no_tasks_in_queue
            decision = self._evaluate_downscale_snapshot(queue_allowed=queue_allowed)
            if decision.snapshot is None or not (decision.normal or decision.stability):
                return
        if decision is not None and decision.target_size is not None:
            original_formation_size = decision.target_size

        requires_cleanup_capture = (
            expected_cleanup_capture is not None
            or (not self.remote_monitoring and decision is None)
            or (self.remote_monitoring and at_or_below_target)
        )
        cleanup_capture = None
        if requires_cleanup_capture:
            if expected_cleanup_capture is not None:
                cleanup_capture = tuple(expected_cleanup_capture)
            else:
                cleanup_capture = adapter.capture_formation_cleanup_state(
                    self.formation_name,
                )
            if cleanup_capture is None:
                logger.warning(
                    "Skipping cleanup for %s/%s without a readable primary "
                    "formation state.",
                    self.app_name, self.formation_name,
                )
                return
            if len(cleanup_capture) != 3:
                logger.warning(
                    "Skipping cleanup for %s/%s without a complete primary "
                    "formation state.",
                    self.app_name, self.formation_name,
                )
                return
            if self.remote_monitoring and at_or_below_target:
                if (
                    cleanup_capture[0] != remote_formation_raw
                    or cleanup_capture[1] != raw_target
                ):
                    logger.warning(
                        "Skipping remote cleanup for %s/%s after the tier or "
                        "baseline changed before capture.",
                        self.app_name, self.formation_name,
                    )
                    return

        _lock_proceed = True
        attempt_token = None
        try:
            with cache.lock(self.downscale_cache_key, expire=30):
                if self._defer_resize_for_retiring_dyno('downscale'):
                    _lock_proceed = False
                elif self.is_downscaling:
                    logger.debug(
                        "Downscale for %s is already reserved.", self.formation_name,
                    )
                    _lock_proceed = False
                elif self.remote_monitoring:
                    cleanup_status = 'not_at_or_below'
                    if at_or_below_target:
                        cleanup_status = adapter.clear_formation_if_at_or_below(
                            self.formation_name,
                            self.formation_size,
                            captured=cleanup_capture,
                            expected_upscale_timer_raw=(
                                expected_upscale_timer_raw
                            ),
                        )
                    if at_or_below_target:
                        if cleanup_status == 'cleared':
                            logger.debug(
                                "Formation %s is already at or below its "
                                "original size.", self.formation_name,
                            )
                        elif cleanup_status == 'no_baseline':
                            logger.info(
                                "Skipping remote scheduled downscale for "
                                "%s/%s after a genuine baseline miss.",
                                self.app_name, self.formation_name,
                            )
                        else:
                            logger.warning(
                                "Skipping remote scheduled downscale for "
                                "%s/%s without an unchanged primary state: "
                                "%s.", self.app_name, self.formation_name,
                                cleanup_status,
                            )
                        _lock_proceed = False
                    elif cleanup_status == 'not_at_or_below':
                        attempt_token = str(uuid.uuid4())
                        reservation_status = (
                            adapter.reserve_remote_downscale_if_current(
                                self.downscale_cache_key,
                                attempt_token,
                                settings.DYNO_TIME_BETWEEN_SCALES,
                                formation_name=self.formation_name,
                                expected_formation_raw=remote_formation_raw,
                                expected_formation_size=effective_current_size,
                                expected_original_raw=raw_target,
                                expected_target_size=original_formation_size,
                                expected_upscale_timer_raw=(
                                    expected_upscale_timer_raw
                                ),
                                expected_cleanup_capture=(
                                    expected_cleanup_capture
                                ),
                            )
                        )
                        if reservation_status != 'reserved':
                            logger.info(
                                "Skipping remote downscale for %s/%s without "
                                "a current authorization: %s.",
                                self.app_name, self.dyno_name,
                                reservation_status,
                            )
                            _lock_proceed = False
                    else:
                        if cleanup_status == 'cleared':
                            logger.debug(
                                "Formation %s is already at or below its "
                                "original size.", self.formation_name,
                            )
                        elif cleanup_status != 'not_at_or_below':
                            logger.warning(
                                "Skipping remote downscale for %s/%s without "
                                "a stable at-or-below state: %s.",
                                self.app_name, self.formation_name,
                                cleanup_status,
                            )
                        _lock_proceed = False
                elif decision is None:
                    cleanup_status = adapter.clear_formation_if_at_or_below(
                        self.formation_name,
                        self.formation_size,
                        captured=cleanup_capture,
                    )
                    if cleanup_status == 'cleared':
                        logger.debug(
                            "Formation %s is already at or below its original "
                            "size.", self.formation_name,
                        )
                    elif cleanup_status != 'not_at_or_below':
                        logger.warning(
                            "Skipping cleanup for %s/%s without a stable "
                            "at-or-below state: %s.",
                            self.app_name, self.formation_name, cleanup_status,
                        )
                    _lock_proceed = False
                elif decision.snapshot is None:
                    _lock_proceed = False
                else:
                    attempt_token = str(uuid.uuid4())
                    status = adapter.reserve_downscale_if_current(
                        decision.snapshot,
                        self.dyno_name,
                        self._dyno_owner_token,
                        self.downscale_cache_key,
                        attempt_token,
                        settings.DYNO_TIME_BETWEEN_SCALES,
                        formation_name=self.formation_name,
                        expected_formation_size=self.formation_size,
                        expected_target_size=original_formation_size,
                        cleanup_if_at_or_below=True,
                        expected_upscale_timer_raw=(
                            expected_upscale_timer_raw
                        ),
                        expected_cleanup_capture=expected_cleanup_capture,
                    )
                    if status == 'already_at_target':
                        _lock_proceed = False
                    elif status != 'reserved':
                        logger.info(
                            "Skipping downscale for %s/%s without a current authorization: %s.",
                            self.app_name, self.dyno_name, status,
                        )
                        _lock_proceed = False
        except _LockExpiryErrors:
            logger.warning(f"Lock '{self.downscale_cache_key}' expired before release.")
            _lock_proceed = False
        if not _lock_proceed:
            return

        # Scale formation back to original size (routed through call_heroku_api for rate limiting)
        url = f'https://api.heroku.com/apps/{self.app_name}/formation/{self.formation_name}'
        response = self.call_heroku_api("PATCH", url, data={"size": original_formation_size})
        if response and response.status_code == 200:
            logger.info(f"Downscaled formation {self.formation_name} back to {original_formation_size} with {DYNO_SIZES[original_formation_size]['memory']} MB memory.")
            self._update_formation_size(original_formation_size)
            self.clear_upscaling()
            self.clear_original_formation_size()
            cache.delete(self.memory_history_cache_key)
            if not self.remote_monitoring:
                self.stop_continuous_autoscale()
        else:
            self._log_api_failure(f"Failed to downscale formation {self.formation_name}", response)
            if self._definite_resize_rejection(response):
                self.clear_downscaling(attempt_token)

    def restart_dyno(
        self, dyno_name=None, *, expected_owner=None, expected_score=None,
        zombie_threshold=None,
    ):
        if not self.app_name or (not self.dyno_name and not dyno_name):
            return

        dyno_name = dyno_name or self.dyno_name

        # Ensure restart is only executed once every DYNO_TIME_BETWEEN_RESTARTS seconds for this dyno
        restart_cache_key = f'heroku:restart_dyno:{dyno_name}'
        _lock_proceed = True
        try:
            with cache.lock(restart_cache_key, expire=30):
                if cache.get(restart_cache_key):
                    logger.debug(f"Restarting dyno {dyno_name} is already in progress.")
                    _lock_proceed = False
                else:
                    if zombie_threshold is not None and dyno_name != self.dyno_name:
                        evidence = self._stale_zombie_evidence(
                            dyno_name, zombie_threshold,
                        )
                        if evidence is None:
                            _lock_proceed = False
                        else:
                            expected_owner, expected_score = evidence
                    elif dyno_name != self.dyno_name and (
                        expected_owner is None or expected_score is None
                    ):
                        status, expected_owner, expected_score = _IndexAdapter(
                            self.app_name, primary=True,
                        ).get_owner_and_score(dyno_name)
                        if status != 'found':
                            expected_owner = None
                            expected_score = None
                    if _lock_proceed:
                        cache.set(
                            restart_cache_key,
                            True,
                            timeout=getattr(settings, 'DYNO_TIME_BETWEEN_RESTARTS', 300),
                        )
        except _LockExpiryErrors:
            logger.warning(f"Lock '{restart_cache_key}' expired before release.")
        if not _lock_proceed:
            return False

        # Restart the dyno via Heroku API (routed through call_heroku_api for rate limiting)
        url = f'https://api.heroku.com/apps/{self.app_name}/dynos/{dyno_name}'
        response = self.call_heroku_api("DELETE", url)
        if response and response.status_code == 202:
            logger.info(f"Restarting dyno {dyno_name}...")

            if dyno_name == self.dyno_name:
                self.stop_continuous_autoscale()
            elif expected_owner is not None and expected_score is not None:
                self.remove_dyno_from_alive_cache(
                    dyno_name,
                    expected_owner=expected_owner,
                    expected_score=expected_score,
                )
            return True
        self._log_api_failure(f"Failed to restart dyno {dyno_name}", response)
        return False

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
            logger.debug(f"Initialized counter for dyno {dyno_name} with value 1 and TTL {ttl} seconds")
        else:
            # Increment the counter without changing the TTL
            counter = cache.incr(cache_key)
            logger.debug(f"Incremented counter for dyno {dyno_name} to {counter}")

        # Get the threshold from settings or default to 15
        threshold = getattr(settings, 'DYNO_RESTART_THRESHOLD', 15)

        # If counter exceeds threshold, restart the dyno
        if counter >= threshold:
            logger.warning(f"Counter for dyno {dyno_name} reached threshold {threshold}. Restarting dyno...")
            restarted = self.restart_dyno(dyno_name)
            if restarted:
                cache.delete(cache_key)
            return 0

        return counter

    def get_heroku_logs(
        self, source="heroku", date_from=None, *, strict_observation=False,
    ):
        if not self.heroku_api_configured:
            if self.proxy.enabled:
                logger.error("Cannot retrieve Heroku logs: proxy configuration is invalid (%s).",
                             self.proxy.config_error or "missing proxy settings")
            return None

        cache_key = f'heroku:logs:{self.app_name}:{self.dyno_name}'
        dyno_id = self._verified_self_dyno_id
        if dyno_id:
            cache_key = f'{cache_key}:{dyno_id}'
        if source != "heroku":
            cache_key = f'{cache_key}:source:{source}'
        strict_key = f'{cache_key}:strict:v2'
        read_key = strict_key if strict_observation else cache_key
        lock_key = cache_key if dyno_id else read_key
        backoff_key = f'heroku:log_session_backoff:{self.app_name}'

        def cached_logs():
            logs = cache.get(read_key)
            if not logs and dyno_id and not strict_observation:
                return cache.get(strict_key)
            return logs

        logs = cached_logs()
        if not logs:
            if (cache.get(backoff_key) or 0) > time.time():
                return None
            try:
                with cache.lock(lock_key, expire=90):
                    logs = cached_logs()
                    if not logs:
                        if (cache.get(backoff_key) or 0) > time.time():
                            return None
                        # Step 1: Set up API request to retrieve logs
                        url = f"https://api.heroku.com/apps/{self.app_name}/log-sessions"
                        payload = {
                            "dyno": self.dyno_name,
                            "tail": False,
                            "source": source,
                            "lines": 200  # Adjusting this almost does nothing
                        }

                        response = self.call_heroku_api("POST", url, data=payload)
                        if response is not None and response.status_code == 429:
                            # Share the provider window across dynos and log sources in this app.
                            until = time.time() + _log_session_retry_delay(response.headers.get("Retry-After", ""))
                            with cache.lock(backoff_key + ":lock", expire=10):
                                now = time.time()
                                previous = cache.get(backoff_key) or 0
                                delay = max(1, math.ceil(max(until, previous) - now))
                                cache.set(backoff_key, max(until, previous), timeout=delay)
                            if previous <= now:
                                logger.warning("Pausing new autoscaler log sessions for %s for %ss", self.app_name, delay)
                            return None
                        if not response:
                            logger.warning("Failed to retrieve log session (rate-limited or network error).")
                            return None
                        if response.status_code >= 400:
                            logger.warning(f"Failed to retrieve log session. Status code: {response.status_code} - {response.text}")
                            return None

                        log_url = response.json().get("logplex_url")
                        if not log_url:
                            logger.info("Log URL not found in the response.")
                            return None

                        try:
                            log_response = requests.get(log_url, timeout=30)
                        except (SSLError, ConnectionError, Timeout) as exc:
                            logger.warning(f"Failed to fetch logplex URL: {exc}")
                            return None
                        if log_response.status_code != 200:
                            logger.info(f"Failed to retrieve logs. Status code: {log_response.status_code} - {log_response.text}")
                            return None

                        logs = log_response.text or "\n" # Ensure logs is not empty to not overload API
                        log_cache_ttl = getattr(settings, 'DYNO_LOGS_CACHE_DURATION', 90)
                        if strict_observation:
                            log_cache_ttl = min(
                                int(log_cache_ttl),
                                max(1, int(getattr(
                                    settings, 'DYNO_GENERAL_CACHE_DURATION', 300,
                                ))),
                                max(1, int(getattr(
                                    settings, 'DYNO_AUTOSCALE_INTERVAL', 30,
                                ))),
                            )
                        cache.set(read_key, logs, timeout=log_cache_ttl)
            except _LockExpiryErrors:
                logger.warning(f"Lock '{lock_key}' expired before release. Logs may already be cached.")

        if not logs:
            return None

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

                message = match.group("message")
                source_dyno_name, source_dyno_id = _log_dyno_identity(message)
                logs_parsed.append({
                    "timestamp": timestamp,
                    "dyno_name": match.group("dyno_name"),
                    "message": message,
                    "source_dyno_name": source_dyno_name,
                    "source_dyno_id": source_dyno_id,
                })

        return logs_parsed

    def extract_latest_metric(
        self, patterns, cache_key, timeout=None, result_type=float,
        require_fresh_source=False,
    ):
        """Return the latest metric value while preserving the scalar API."""
        reading = self._extract_latest_metric_reading(
            patterns, cache_key, timeout=timeout, result_type=result_type,
            require_fresh_source=require_fresh_source,
        )
        return reading.value if reading is not None else None

    def _extract_latest_metric_reading(
        self, patterns, cache_key, timeout=None, result_type=float,
        require_fresh_source=False,
    ):
        """
        Generic helper to extract the latest occurrence of a specified metric from Heroku logs.
        Handles multiple patterns and ensures there is some value at all times.

        Parameters:
            patterns (list): A list of regular expression patterns to search for the metric.
            cache_key (str): The cache key to store the latest value.
            timeout (int): Cache timeout in seconds. Defaults to 24 hours.
            result_type (type): The desired return type for the metric value (e.g., float, str).
            require_fresh_source (bool): Use a separate timestamped cache and
                reject old log entries or cached values. Strict observation
                readers use this mode.

        Returns:
            float or str: The extracted metric value, converted to the specified result_type.
                        Returns None if no patterns are found.
        """
        if timeout is None:
            timeout = 24 * 60 * 60  # Default to 24 hours
            cache_refresh_interval = getattr(settings, 'DYNO_GENERAL_CACHE_DURATION', 300)
        else:
            cache_refresh_interval = timeout

        scoped_cache_key = f'{cache_key}:{self.app_name}:{self.dyno_name}'
        verified_dyno_id = self._verified_self_dyno_id
        if verified_dyno_id:
            scoped_cache_key = f'{scoped_cache_key}:{verified_dyno_id}'
        if require_fresh_source:
            max_source_age = getattr(settings, 'DYNO_GENERAL_CACHE_DURATION', 300)
            cache_refresh_interval = min(
                max_source_age,
                max(1, int(getattr(settings, 'DYNO_AUTOSCALE_INTERVAL', 30))),
            )
            timeout = max_source_age
            cache_key = f'{scoped_cache_key}:source-timestamped:v1'
        else:
            max_source_age = None
            cache_key = scoped_cache_key

        # Fetch from cache. Strict observations derive freshness from the
        # source timestamp, so only the legacy path needs a separate TTL read.
        cached_entry = cache.get(cache_key)
        if require_fresh_source:
            cache_is_fresh = False
        else:
            ttl = cache.ttl(cache_key)
            cache_is_fresh = ttl is not None and ttl > 0 and (timeout - ttl) < cache_refresh_interval
        cached_value = cached_entry
        cached_source_epoch = None
        if require_fresh_source:
            cached_value = None
            if isinstance(cached_entry, (tuple, list)) and len(cached_entry) == 2:
                value, source_epoch = cached_entry
                if _is_finite_number(source_epoch):
                    cached_value = value
                    cached_source_epoch = source_epoch
                    now_epoch = time.time()
                    cache_is_fresh = (
                        source_epoch <= now_epoch
                        and now_epoch - source_epoch <= max_source_age
                        and now_epoch - source_epoch < cache_refresh_interval
                    )
            if cached_source_epoch is None:
                cache_is_fresh = False

        if cache_is_fresh and cached_value is not None:
            return _MetricReading(cached_value, cached_source_epoch)

        # Fetch and parse logs. One check-in memoizes the strict fetch, including
        # failure, so memory and load cannot issue duplicate log-session calls.
        if require_fresh_source:
            check_in_active = '_strict_observation_logs' in self.__dict__
            if check_in_active:
                logs_parsed = self.__dict__['_strict_observation_logs']
            else:
                logs_parsed = _STRICT_LOGS_UNSET
            if logs_parsed is _STRICT_LOGS_UNSET:
                try:
                    logs_parsed = self.get_heroku_logs(strict_observation=True)
                except Exception:
                    logs_parsed = None
                if check_in_active:
                    self.__dict__['_strict_observation_logs'] = logs_parsed
        else:
            logs_parsed = self.get_heroku_logs()
        if not logs_parsed:
            if require_fresh_source or cached_value is None:
                return None
            return _MetricReading(cached_value, None)

        # Compile all patterns
        compiled_patterns = [re.compile(pattern) for pattern in patterns]
        latest_value = None
        sample_epoch = None

        # Search for matches in reverse order to find the most recent occurrence
        for log_entry in reversed(logs_parsed):
            message = log_entry.get("message", "")
            timestamp = log_entry.get("timestamp")
            seconds_ago = (timezone.now() - timestamp).total_seconds() if timestamp else None
            if result_type is not bool and (require_fresh_source or verified_dyno_id):
                if (
                    verified_dyno_id is None
                    or log_entry.get("source_dyno_name") != self.dyno_name
                    or log_entry.get("source_dyno_id") != verified_dyno_id
                ):
                    continue
            if require_fresh_source:
                if (
                    timestamp is None
                    or seconds_ago is None
                    or seconds_ago < 0
                    or seconds_ago > max_source_age
                ):
                    continue
                sample_epoch = timestamp.timestamp()
            elif seconds_ago is not None and seconds_ago > timeout:
                continue
            for pattern in compiled_patterns:
                match = pattern.search(message)
                if match:
                    extracted_value = match.group(1)
                    latest_value = result_type(extracted_value) if extracted_value.replace('.', '', 1).isdigit() else extracted_value
                    break
            if latest_value is not None:
                break

        if result_type == bool:
            # Explicit False when no recent match found — prevents cached True from latching forever
            latest_value = bool(latest_value)
            cache.set(cache_key, latest_value, timeout=timeout)
            return _MetricReading(latest_value, None)

        if latest_value is not None:
            cached_result = (
                (latest_value, sample_epoch)
                if require_fresh_source
                else latest_value
            )
            cache.set(cache_key, cached_result, timeout=timeout)

        if latest_value is not None:
            return _MetricReading(latest_value, sample_epoch if require_fresh_source else None)
        if require_fresh_source or cached_value is None:
            return None
        return _MetricReading(cached_value, None)

    def get_memory_usage_from_logs(self, require_fresh=False):
        """
        Retrieves the most recent total memory usage of a specified dyno by parsing Heroku logs.

        Returns:
            float: The total memory usage in MB, or None if not found.
        """
        patterns = [
            r"sample#memory_total=(\d+\.\d+)MB",          # Pattern for memory_total in MB
            r"Process running mem=(\d+)M\(\d+\.\d+%\)"    # Pattern for memory in megabytes
        ]
        return self.extract_latest_metric(
            patterns, cache_key="heroku:memory_total", result_type=float,
            require_fresh_source=require_fresh,
        )

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
        timeout = getattr(settings, 'DYNO_ERRORS_TIMEOUT_DURATION', 60)  # Default to 60 seconds
        return self.extract_latest_metric(patterns, cache_key="heroku:r15_error", timeout=timeout, result_type=bool)

    def get_r14_from_logs(self):
        """
        Retrieves the most recent R14 error from Heroku logs, indicating memory quota exceeded.

        Returns:
            bool: The R14 error message if found, or None if not found.
        """
        patterns = [r"Error R14 \((.*?)\)"]
        timeout = getattr(settings, 'DYNO_ERRORS_TIMEOUT_DURATION', 60)  # Default to 60 seconds
        return self.extract_latest_metric(patterns, cache_key="heroku:r14_error", timeout=timeout, result_type=bool)

    def exec_connect(self, dyno_name=None, command=None):
        """
        Execute a command on a dyno using the Heroku API.

        This method creates a new one-off dyno to run the command.

        Args:
            dyno_name (str, optional): The name of the target dyno. Defaults to self.dyno_name.
                Note: This is only used for logging purposes as the API creates a new dyno.
            command (str, optional): The command to execute on the dyno. If not provided, just connects to the dyno.

        Returns:
            object: A result object with stdout, stderr, and returncode attributes, or None if the operation failed.
        """
        dyno_name = dyno_name or self.dyno_name
        if not self.app_name or not dyno_name:
            logger.error("Cannot connect to dyno: app_name or dyno_name is not set.")
            return None

        if not self.heroku_api_configured:
            logger.error("Cannot execute command: no Heroku API transport is configured.")
            return None

        try:
            # Use the Heroku API to run the command on a one-off dyno
            url = f'https://api.heroku.com/apps/{self.app_name}/dynos'

            # Quote the command to prevent shell injection
            safe_command = shlex.quote(command) if command else None
            payload = {
                'command': f'bash -c {safe_command}' if safe_command else 'bash',
                'attach': True,
                'size': self.formation_size,
                'type': 'run'
            }

            logger.info(f"Executing command on a one-off dyno via Heroku API (target dyno: {dyno_name})...")
            response = self.call_heroku_api("POST", url, custom_headers={'Content-Type': 'application/json'}, data=payload)

            if response and response.status_code == 201:  # 201 Created
                dyno_data = response.json()
                logger.info(f"Command execution started on one-off dyno {dyno_data.get('name')}.")
                return _ApiResult(stdout=str(dyno_data), stderr="", returncode=0)
            self._log_api_failure("Failed to execute command via Heroku API", response)
            return None
        except Exception as e:
            logger.error(f"Failed to execute command via Heroku API. Error: {e}", exc_info=True)
            return None


    def clean_old_files(self, dyno_name=None, directory="/tmp", hours=48):
        """
        Clean files in the specified directory that are older than the specified number of hours.

        Args:
            dyno_name (str, optional): The name of the dyno to connect to. Defaults to self.dyno_name.
            directory (str, optional): The directory to clean. Defaults to "/tmp".
            hours (int, optional): The age threshold in hours. Defaults to 48.

        Returns:
            bool: True if the operation was successful, False otherwise.
        """
        dyno_name = dyno_name or self.dyno_name
        if not self.app_name or not dyno_name:
            logger.error("Cannot clean old files: app_name or dyno_name is not set.")
            return False

        try:
            # Create the find command to delete files older than the specified hours
            # Exclude env.d and mask directories
            find_cmd = f"find {directory} -type f -mtime +{hours/24} -not -path '{directory}/env.d*' -not -path '{directory}/mask*' -delete"
            logger.info(f"Cleaning files in {directory} older than {hours} hours on dyno {dyno_name}, excluding env.d and mask...")

            # Execute the command on the dyno
            result = self.exec_connect(dyno_name, command=find_cmd)

            if not result:
                logger.error(f"Failed to connect to dyno {dyno_name}.")
                return False

            if result.returncode != 0:
                logger.error(f"Failed to clean old files on dyno {dyno_name}. Error: {result.stderr}")
                return False

            logger.info(f"Successfully cleaned old files in {directory} on dyno {dyno_name}.")
            return True
        except Exception as e:
            logger.error(f"Failed to clean old files on dyno {dyno_name}. Error: {e}", exc_info=True)
            return False
