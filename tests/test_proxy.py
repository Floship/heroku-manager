import base64
import hashlib
import hmac
import json
import os
import unittest
from unittest.mock import patch

import requests
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from heroku_manager.proxy import HerokuProxy, ProxyResponse

AUTH_HEX = "0f" * 32
MANAGER = "c3f1d8e4-1a2b-4c5d-8e9f-0123456789ab"
INFO = b"heroku-control:credential-transport:v1"


def b64url(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def credential(inner):
    return "v2:" + b64url(inner)


def derived_key(auth=AUTH_HEX):
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=INFO).derive(bytes.fromhex(auth))


def derived_key_with_zero_salt(auth=AUTH_HEX):
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=b"\x00" * 32, info=INFO).derive(bytes.fromhex(auth))


def split_capsule(capsule):
    encoded = capsule[len("v1:"):]
    raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
    return raw[:12], raw[12:]


def decrypt(capsule, signing_message, auth=AUTH_HEX):
    nonce, sealed = split_capsule(capsule)
    return AESGCM(derived_key(auth)).decrypt(nonce, sealed, INFO + b"\n" + signing_message.encode("utf-8"))


def signing_string(request, client, manager=MANAGER):
    body = request.body or b""
    headers = request.headers
    lines = ["HEROKU-PROXY-HMAC-SHA256-SPLIT-1", manager, client.client_id, client.key_id,
             headers["x-heroku-timestamp"], headers["x-heroku-nonce"], request.method, request.path_url,
             hashlib.sha256(body).hexdigest()]
    lines.extend("{}:{}".format(name, headers.get(name, "")) for name in client.SIGNED_HEADERS)
    return "\n".join(lines)


class ProxyTests(unittest.TestCase):
    def make_client(self, inner=None, base_url="https://manager.example", client_id="portal",
                    secret=AUTH_HEX, key_id="v1", manager_id=MANAGER):
        inner = os.urandom(44) if inner is None else inner
        client = HerokuProxy(base_url, client_id, secret, key_id, credential(inner), manager_id)
        return client, inner

    def response(self, status=200, text="{}", headers=None):
        response = requests.Response()
        response.status_code = status
        response._content = text.encode()
        response.headers.update(headers or {})
        return response

    @patch("heroku_manager.proxy.requests.Session.send")
    def test_signs_split_message_and_sends_only_a_fresh_capsule(self, send):
        send.return_value = self.response()
        client, inner = self.make_client()
        client.request("PATCH", "/apps/production/formation/worker", data={"size": "standard-2x"})
        request = send.call_args.args[0]
        self.assertEqual(request.url, "https://manager.example/v1/heroku/apps/production/formation/worker")
        headers = request.headers
        self.assertNotIn("Authorization", headers)
        self.assertNotIn(AUTH_HEX, str(dict(headers)))
        self.assertNotIn(b64url(inner), str(dict(headers)))
        self.assertNotIn("x-heroku-credential", client.SIGNED_HEADERS)
        self.assertEqual(json.loads(request.body), {"size": "standard-2x"})
        message = signing_string(request, client)
        capsule = headers["x-heroku-credential"]
        self.assertTrue(capsule.startswith("v1:"))
        self.assertEqual(headers["x-heroku-signature"], hmac.new(
            bytes.fromhex(AUTH_HEX), (message + "\nx-heroku-credential:" + capsule).encode("utf-8"),
            hashlib.sha256).hexdigest())
        nonce, _sealed = split_capsule(capsule)
        self.assertEqual(len(nonce), 12)
        self.assertEqual(decrypt(capsule, message), inner)
        # RFC 5869 default salt is HashLen zero bytes; prove the client used it.
        zero_nonce, zero_sealed = split_capsule(capsule)
        self.assertEqual(AESGCM(derived_key_with_zero_salt()).decrypt(
            zero_nonce, zero_sealed, INFO + b"\n" + message.encode("utf-8")), inner)
        self.assertFalse(client._session.trust_env)
        self.assertFalse(send.call_args.kwargs["allow_redirects"])

    @patch("heroku_manager.proxy.requests.Session.send")
    def test_each_request_gets_independent_nonce_capsule_and_signature(self, send):
        send.return_value = self.response()
        client, inner = self.make_client()
        with patch("heroku_manager.proxy.time.time", return_value=1000):
            client.request("GET", "/apps/production/dynos")
            client.request("GET", "/apps/production/dynos")
        first, second = (call.args[0] for call in send.call_args_list)
        self.assertNotEqual(first.headers["x-heroku-nonce"], second.headers["x-heroku-nonce"])
        self.assertNotEqual(first.headers["x-heroku-credential"], second.headers["x-heroku-credential"])
        self.assertNotEqual(first.headers["x-heroku-signature"], second.headers["x-heroku-signature"])
        self.assertEqual(decrypt(first.headers["x-heroku-credential"], signing_string(first, client)), inner)
        self.assertEqual(decrypt(second.headers["x-heroku-credential"], signing_string(second, client)), inner)

    @patch("heroku_manager.proxy.requests.Session.send")
    def test_capsule_and_signature_are_bound_to_the_exact_request(self, send):
        send.return_value = self.response()
        client, _ = self.make_client()
        client.request("PATCH", "/apps/production/formation/worker", data={"size": "standard-2x"})
        client.request("POST", "/apps/production/builds", data={"source_blob": {"url": "https://example.test"}})
        first, second = (call.args[0] for call in send.call_args_list)
        with self.assertRaises(InvalidTag):
            decrypt(first.headers["x-heroku-credential"], signing_string(second, client))
        message = signing_string(second, client)
        moved = hmac.new(bytes.fromhex(AUTH_HEX),
                         (message + "\nx-heroku-credential:" + first.headers["x-heroku-credential"]).encode("utf-8"),
                         hashlib.sha256).hexdigest()
        self.assertNotEqual(second.headers["x-heroku-signature"], moved)

    @patch("heroku_manager.proxy.requests.Session.send")
    def test_timeout_never_retries_or_returns_transport_exception(self, send):
        send.side_effect = requests.Timeout("sensitive transport")
        client, _ = self.make_client()
        result = client.request("POST", "/apps/production/builds", data={})
        self.assertEqual(result.status_code, 502)
        self.assertEqual(result.json()["request_id"], result.request_id)
        send.assert_called_once()

    @patch("heroku_manager.proxy.requests.Session.send")
    def test_heroku_key_cannot_activate_a_missing_proxy(self, send):
        with patch.dict(os.environ, {"HEROKU_API_KEY": "control-sentinel"}, clear=True):
            self.assertIsNone(HerokuProxy().request("GET", "/apps/production/dynos"))
        send.assert_not_called()

    def test_proxy_url_selects_the_mode_even_when_signing_settings_are_missing(self):
        client = HerokuProxy("https://manager.example", "", "")
        self.assertTrue(client.enabled)
        self.assertFalse(client.configured)
        self.assertIn("HEROKU_PROXY_MANAGER_ID", client.config_error)

    def test_invalid_settings_are_reported_instead_of_raised(self):
        client = HerokuProxy("http://manager.example", "portal", "zz")
        self.assertTrue(client.enabled)
        self.assertFalse(client.configured)
        self.assertIn("HEROKU_PROXY", client.config_error)

    def test_environment_defaults_load_the_split_credential(self):
        inner = os.urandom(44)
        env = {
            "HEROKU_PROXY_URL": "https://manager.example",
            "HEROKU_PROXY_CLIENT_ID": "portal",
            "HEROKU_PROXY_SECRET": AUTH_HEX,
            "HEROKU_PROXY_CREDENTIAL": credential(inner),
            "HEROKU_PROXY_MANAGER_ID": MANAGER,
        }
        with patch.dict(os.environ, env, clear=True):
            client = HerokuProxy()
        self.assertTrue(client.configured)
        self.assertEqual(client._credential, inner)

    def test_direct_mode_without_a_url_ignores_proxy_settings(self):
        client = HerokuProxy("", "portal", AUTH_HEX, "v1", "legacy-envelope", "bad-manager")
        self.assertFalse(client.enabled)
        self.assertFalse(client.configured)
        self.assertIsNone(client.config_error)

    @patch("heroku_manager.proxy.requests.Session.send")
    def test_redirects_and_arbitrary_urls_or_headers_are_rejected(self, send):
        client, _ = self.make_client()
        for url in ["https://evil.example/apps/x", "//evil.example/apps/x", "/apps/x/../config-vars"]:
            with self.assertRaises(ValueError):
                client.request("GET", url)
        with self.assertRaises(ValueError):
            client.request("GET", "/apps/x", headers={"Authorization": "Bearer anything"})
        with self.assertRaises(ValueError):
            client.request("GET", "/apps/x", headers={"x-heroku-credential": "v1:anything"})
        with self.assertRaises(ValueError):
            client.request("GET", "/apps/x", headers={"range": "id ..\r\nx"})
        with self.assertRaises(ValueError):
            client.request("GET", "/apps/x\r\n")
        send.assert_not_called()
        send.return_value = self.response(302, headers={"Location": "https://evil.example"})
        self.assertEqual(client.request("GET", "/apps/x").status_code, 502)

    @patch("heroku_manager.proxy.requests.Session.send")
    def test_response_drops_signed_request_and_sensitive_provider_headers(self, send):
        send.return_value = self.response(206, '[{"id":"a"}]', {"Next-Range": "id next..", "Set-Cookie": "secret"})
        client, _ = self.make_client()
        response = client.request("GET", "/apps", headers={"Range": "id ..; max=1"})
        self.assertEqual(response.headers["Next-Range"], "id next..")
        self.assertNotIn("Set-Cookie", response.headers)
        self.assertFalse(hasattr(response, "request"))
        self.assertTrue(response)
        self.assertEqual(response.json(), [{"id": "a"}])
        self.assertFalse(ProxyResponse(403, "{}"))


class CredentialConfigTests(unittest.TestCase):
    def test_exact_inner_size_bounds_are_accepted(self):
        for size in (HerokuProxy.INNER_MIN_BYTES, HerokuProxy.INNER_MAX_BYTES):
            with self.subTest(size=size):
                client = HerokuProxy("https://manager.example", "portal", AUTH_HEX, "v1",
                                     credential(os.urandom(size)), MANAGER)
                self.assertTrue(client.configured)
                self.assertIsNone(client.config_error)

    def test_inner_sizes_outside_the_protocol_are_rejected(self):
        for size in (HerokuProxy.INNER_MIN_BYTES - 1, HerokuProxy.INNER_MAX_BYTES + 1):
            with self.subTest(size=size):
                inner = os.urandom(size)
                client = HerokuProxy("https://manager.example", "portal", AUTH_HEX, "v1",
                                     credential(inner), MANAGER)
                self.assertFalse(client.configured)
                self.assertIn("inner size", client.config_error)

    def test_old_and_noncanonical_credential_formats_fail_closed(self):
        inner = os.urandom(44)
        variants = [
            "v1:" + b64url(inner),
            "v2:" + b64url(inner) + "==",
            "v2:" + b64url(inner)[:-1],
            "v2:" + base64.b64encode(inner).decode("ascii"),
            "v2:",
            "",
        ]
        for value in variants:
            with self.subTest(value=value[:12]):
                client = HerokuProxy("https://manager.example", "portal", AUTH_HEX, "v1", value, MANAGER)
                self.assertFalse(client.configured)
                self.assertIn("HEROKU_PROXY_CREDENTIAL", client.config_error)

    def test_manager_must_be_a_canonical_uuid(self):
        inner = os.urandom(44)
        for value in (MANAGER.upper(), "{" + MANAGER + "}", MANAGER.replace("-", ""), "urn:uuid:" + MANAGER, "nope"):
            with self.subTest(value=value):
                client = HerokuProxy("https://manager.example", "portal", AUTH_HEX, "v1", credential(inner), value)
                self.assertFalse(client.configured)
                self.assertIn("HEROKU_PROXY_MANAGER_ID", client.config_error)

    def test_auth_secret_must_be_exactly_32_lowercase_hex_bytes(self):
        inner = os.urandom(44)
        for value in (AUTH_HEX[:-2], AUTH_HEX + "00", AUTH_HEX.upper(), "zz" * 32, ("01" * 64)):
            with self.subTest(length=len(value)):
                client = HerokuProxy("https://manager.example", "portal", value, "v1", credential(inner), MANAGER)
                self.assertFalse(client.configured)
                self.assertIn("HEROKU_PROXY_SECRET", client.config_error)

    @patch("heroku_manager.proxy.requests.Session.send")
    def test_missing_credential_or_manager_fails_closed_before_dispatch(self, send):
        for credential_value, manager_value in (("", MANAGER), (credential(os.urandom(44)), ""), ("", "")):
            with self.subTest(credential=bool(credential_value), manager=bool(manager_value)):
                client = HerokuProxy("https://manager.example", "portal", AUTH_HEX, "v1",
                                     credential_value, manager_value)
                self.assertFalse(client.configured)
                self.assertIsNone(client.request("GET", "/apps/production/dynos"))
        send.assert_not_called()

    def test_invalid_values_never_appear_in_config_errors(self):
        bad_secret = "UPPER" * 12 + "ABCD"
        client = HerokuProxy("https://manager.example", "portal", bad_secret, "v1", credential(os.urandom(44)), MANAGER)
        self.assertNotIn(bad_secret, client.config_error)
        bad_credential = "v2:not+a+capsule"
        client = HerokuProxy("https://manager.example", "portal", AUTH_HEX, "v1", bad_credential, MANAGER)
        self.assertNotIn(bad_credential, client.config_error)

    def test_invalid_port_is_reported(self):
        client = HerokuProxy("https://manager.example:99999", "portal", AUTH_HEX, "v1",
                             credential(os.urandom(44)), MANAGER)
        self.assertFalse(client.configured)
        self.assertIn("invalid port", client.config_error)

    def test_maximum_inner_capsule_stays_within_the_wire_bound(self):
        client = HerokuProxy("https://manager.example", "portal", AUTH_HEX, "v1",
                             credential(os.urandom(HerokuProxy.INNER_MAX_BYTES)), MANAGER)
        capsule = client._build_capsule("any-signing-message", client._credential)
        self.assertLessEqual(len(capsule), 5539)

    @patch("heroku_manager.proxy.requests.Session.send")
    def test_missing_optional_crypto_dependency_fails_closed(self, send):
        original_import = __import__

        def without_crypto(name, *args, **kwargs):
            if name.startswith("cryptography"):
                raise ImportError("private dependency detail")
            return original_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=without_crypto):
            client = HerokuProxy("https://manager.example", "portal", AUTH_HEX, "v1",
                                 credential(bytes([1]) * 44), MANAGER)
        self.assertFalse(client.configured)
        self.assertIn("heroku_manager[proxy]", client.config_error)
        self.assertNotIn("private dependency detail", client.config_error)
        self.assertIsNone(client.request("GET", "/apps/production/dynos"))
        send.assert_not_called()

    def test_client_and_key_identifiers_must_be_bounded_ascii(self):
        for client_id, key_id in (("portal\nother", "v1"), ("portal", "v1\r"),
                                  ("x" * 201, "v1"), ("portal", "."), ("portál", "v1")):
            with self.subTest(client_id=client_id, key_id=key_id):
                client = HerokuProxy("https://manager.example", client_id, AUTH_HEX, key_id,
                                     credential(bytes([1]) * 44), MANAGER)
                self.assertFalse(client.configured)
                self.assertIsNotNone(client.config_error)


if __name__ == "__main__":
    unittest.main()
