"""Heroku Platform API transport through the owner's permissioned proxy."""
import base64
import binascii
import hashlib
import hmac
import json
import os
import time
import uuid
from urllib.parse import unquote, urlsplit

import requests


class ProxyResponse:
    """Keep request credentials and transport internals out of application caches."""

    def __init__(self, status_code, text, headers=None, request_id=None):
        self.status_code = status_code
        self.content = text if isinstance(text, bytes) else text.encode("utf-8")
        self.headers = requests.structures.CaseInsensitiveDict(headers or {})
        self.request_id = request_id

    def __bool__(self):
        return self.status_code < 400

    @property
    def text(self):
        return self.content.decode("utf-8", errors="replace")

    def json(self):
        return json.loads(self.content)


class HerokuProxy:
    CREDENTIAL_PREFIX = "v2:"
    CAPSULE_PREFIX = "v1:"
    CAPSULE_INFO = b"heroku-control:credential-transport:v1"
    INNER_MIN_BYTES = 44
    INNER_MAX_BYTES = 4124
    SIGNED_HEADERS = ("accept", "cache-control", "content-type", "idempotency-key", "if-match", "if-none-match", "range")
    _HEX = frozenset("0123456789abcdef")
    _BASE64URL = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")

    def __init__(self, base_url=None, client_id=None, secret=None, key_id=None, credential=None, manager_id=None):
        raw_url = base_url if base_url is not None else os.environ.get("HEROKU_PROXY_URL", "")
        self._enabled = bool(raw_url)
        self.base_url = raw_url.rstrip("/")
        self.client_id = client_id if client_id is not None else os.environ.get("HEROKU_PROXY_CLIENT_ID", "")
        self.key_id = key_id if key_id is not None else os.environ.get("HEROKU_PROXY_KEY_ID", "v1")
        secret = secret if secret is not None else os.environ.get("HEROKU_PROXY_SECRET", "")
        credential = credential if credential is not None else os.environ.get("HEROKU_PROXY_CREDENTIAL", "")
        manager_id = manager_id if manager_id is not None else os.environ.get("HEROKU_PROXY_MANAGER_ID", "")
        self.config_error = None
        self._secret = b""
        self._credential = b""
        self._manager_id = ""
        # A bad configuration is recorded instead of raised: the proxy is built
        # from the environment while the dyno starts, and call_heroku_api must
        # stay able to fail closed without taking the whole app down.
        try:
            if self.enabled and secret:
                if not isinstance(secret, str) or len(secret) != 64 or any(ch not in self._HEX for ch in secret):
                    raise ValueError("HEROKU_PROXY_SECRET must be exactly 64 lowercase hexadecimal characters")
                self._secret = bytes.fromhex(secret)
            if self.enabled:
                target = urlsplit(self.base_url)
                # Accessing port validates numeric syntax and the 1..65535 range.
                try:
                    port = target.port
                except ValueError:
                    raise ValueError("HEROKU_PROXY_URL has an invalid port") from None
                if port == 0:
                    raise ValueError("HEROKU_PROXY_URL has an invalid port")
                if target.scheme != "https" or not target.hostname or target.username or target.password or target.query or target.fragment:
                    raise ValueError("HEROKU_PROXY_URL must be an HTTPS URL without credentials or a query")
                if target.path not in ("", "/v1/heroku"):
                    raise ValueError("HEROKU_PROXY_URL must use the /v1/heroku endpoint")
                self.base_url = "{}://{}{}".format(target.scheme, target.netloc, "/v1/heroku")
                self._manager_id = self._canonical_manager(manager_id)
                self._credential = self._decode_credential(credential)
                for value in (self.client_id, self.key_id):
                    if not isinstance(value, str) or not 1 <= len(value) <= 200 or value in (".", "..") or any(
                            not (ord(ch) < 128 and (ch.isalnum() or ch in "_-.@:")) for ch in value):
                        raise ValueError("Heroku proxy client and key identifiers are invalid")
                if self._secret:
                    try:
                        __import__("cryptography.hazmat.primitives.ciphers.aead", fromlist=["AESGCM"])
                    except ImportError:
                        raise ValueError("Install heroku_manager[proxy] to enable the encrypted proxy transport") from None
        except ValueError as exc:
            self.config_error = str(exc)
            self._secret = b""
            self._credential = b""
            self._manager_id = ""
        self._session = requests.Session()
        self._session.trust_env = False

    @classmethod
    def _canonical_manager(cls, value):
        error = "HEROKU_PROXY_MANAGER_ID must be a canonical UUID"
        if not isinstance(value, str) or len(value) != 36:
            raise ValueError(error)
        try:
            parsed = uuid.UUID(value)
        except (AttributeError, TypeError, ValueError):
            raise ValueError(error) from None
        if str(parsed) != value:
            raise ValueError(error)
        return value

    @classmethod
    def _decode_credential(cls, value):
        error = "HEROKU_PROXY_CREDENTIAL must be a canonical v2 credential capsule"
        if not isinstance(value, str) or not value.startswith(cls.CREDENTIAL_PREFIX):
            raise ValueError(error)
        encoded = value[len(cls.CREDENTIAL_PREFIX):]
        if not encoded or any(ch not in cls._BASE64URL for ch in encoded):
            raise ValueError(error)
        if not 62 <= len(value) <= 5502:
            raise ValueError("HEROKU_PROXY_CREDENTIAL has an invalid inner size")
        try:
            inner = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        except (binascii.Error, ValueError):
            raise ValueError(error) from None
        if base64.urlsafe_b64encode(inner).rstrip(b"=").decode("ascii") != encoded:
            raise ValueError(error)
        if not cls.INNER_MIN_BYTES <= len(inner) <= cls.INNER_MAX_BYTES:
            raise ValueError("HEROKU_PROXY_CREDENTIAL has an invalid inner size")
        return inner

    @property
    def enabled(self):
        """True when HEROKU_PROXY_URL selects the proxy transport."""
        return self._enabled

    @property
    def configured(self):
        return not self.config_error and bool(self.base_url and self.client_id and self._secret and self.key_id
                                             and self._manager_id and self._credential)

    def request(self, method, url, *, headers=None, data=None, request_id=None):
        if not self.configured:
            return None
        target = urlsplit(url)
        if target.scheme or target.netloc:
            if target.scheme != "https" or target.netloc != "api.heroku.com" or target.username or target.fragment:
                raise ValueError("Only Heroku Platform API URLs are accepted")
        elif not url.startswith("/") or url.startswith("//"):
            raise ValueError("A Heroku API path is required")
        if target.fragment or "\r" in url or "\n" in url or any(
                unquote(part) in (".", "..") or "/" in unquote(part) or "\\" in unquote(part)
                for part in target.path[1:].split("/")):
            raise ValueError("Invalid Heroku API path")
        method = method.upper()
        outgoing = {"accept": "application/vnd.heroku+json; version=3", "content-type": "application/json"}
        allowed = {"accept", "cache-control", "content-type", "range", "if-match", "if-none-match"}
        for name, value in (headers or {}).items():
            if name.lower() not in allowed:
                raise ValueError("Unsupported proxy request header")
            if not isinstance(value, str) or value.strip() != value or "\r" in value or "\n" in value:
                raise ValueError("Invalid proxy request header")
            outgoing[name.lower()] = value
        if method not in ("GET", "HEAD"):
            outgoing["idempotency-key"] = str(uuid.UUID(str(request_id))) if request_id else str(uuid.uuid4())
        path = target.path + ("?" + target.query if target.query else "")
        payload = json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode("utf-8") if data is not None else b""
        outgoing.update({"x-heroku-client": self.client_id, "x-heroku-key-id": self.key_id,
                         "x-heroku-timestamp": str(int(time.time())), "x-heroku-nonce": str(uuid.uuid4())})
        prepared = self._session.prepare_request(requests.Request(method, self.base_url + path,
                                                                  headers=outgoing, data=payload))
        lines = ["HEROKU-PROXY-HMAC-SHA256-SPLIT-1", self._manager_id, self.client_id, self.key_id,
                 outgoing["x-heroku-timestamp"], outgoing["x-heroku-nonce"], method,
                 prepared.path_url, hashlib.sha256(payload).hexdigest()]
        lines.extend("{}:{}".format(name, prepared.headers.get(name, "")) for name in self.SIGNED_HEADERS)
        signing_message = "\n".join(lines)
        capsule = self._build_capsule(signing_message, self._credential)
        prepared.headers["x-heroku-credential"] = capsule
        prepared.headers["x-heroku-signature"] = hmac.new(
            self._secret, (signing_message + "\nx-heroku-credential:" + capsule).encode("utf-8"), hashlib.sha256
        ).hexdigest()
        # One attempt. A timeout on a mutation requires reconciliation, never a new request ID and automatic redispatch.
        try:
            response = self._session.send(prepared, timeout=(3, 25), allow_redirects=False)
        except requests.RequestException:
            # Keep transport internals and signed headers out of logs and caches.
            if method not in ("GET", "HEAD"):
                request_id = outgoing["idempotency-key"]
                return ProxyResponse(502, json.dumps({"id": "proxy_outcome_unknown", "request_id": request_id}),
                                     request_id=request_id)
            return None
        safe_headers = {k: v for k, v in response.headers.items() if k.lower() in {
            "content-type", "next-range", "content-range", "retry-after", "ratelimit-remaining", "etag", "last-modified", "x-heroku-proxy-cache"
        }}
        if 300 <= response.status_code < 400:
            return ProxyResponse(502, '{"id":"proxy_redirect_denied"}', request_id=outgoing.get("idempotency-key"))
        return ProxyResponse(response.status_code, response.content, safe_headers, outgoing.get("idempotency-key"))

    def _build_capsule(self, signing_message, inner):
        """Wrap the fixed inner credential for one signed request."""
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        from cryptography.hazmat.primitives.kdf.hkdf import HKDF

        derived = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=self.CAPSULE_INFO).derive(self._secret)
        aad = self.CAPSULE_INFO + b"\n" + signing_message.encode("utf-8")
        outer_nonce = os.urandom(12)
        sealed = AESGCM(derived).encrypt(outer_nonce, inner, aad)
        return self.CAPSULE_PREFIX + base64.urlsafe_b64encode(outer_nonce + sealed).rstrip(b"=").decode("ascii")
