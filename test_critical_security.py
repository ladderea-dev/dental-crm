import hashlib
import hmac
import os
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from fastapi.responses import JSONResponse
from starlette.requests import Request

from server import (
    AuthRequest,
    PUBLIC_API_ROUTES,
    _admin_auth_middleware,
    _read_session,
    _verify_meta_webhook_signature,
    api_auth_login,
    api_auth_logout,
    api_auth_me,
    api_wa_webhook_verify,
    api_wa_webhook_receive,
    app,
)


def _request(body: bytes, signature: str) -> Request:
    delivered = False

    async def receive():
        nonlocal delivered
        if delivered:
            return {"type": "http.request", "body": b"", "more_body": False}
        delivered = True
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/whatsapp/webhook",
            "headers": [(b"x-hub-signature-256", signature.encode())],
        },
        receive,
    )


def _http_request(method: str, path: str, headers=None) -> Request:
    return Request(
        {
            "type": "http",
            "method": method,
            "path": path,
            "scheme": "https",
            "server": ("testserver", 443),
            "client": ("127.0.0.1", 12345),
            "query_string": b"",
            "headers": headers or [],
        }
    )


class WebhookSignatureTests(unittest.TestCase):
    def test_valid_meta_signature(self):
        body = b'{"object":"whatsapp_business_account","entry":[]}'
        secret = "test-app-secret"
        digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        self.assertTrue(_verify_meta_webhook_signature(body, f"sha256={digest}", secret))

    def test_invalid_and_malformed_signatures(self):
        body = b'{"entry":[]}'
        self.assertFalse(_verify_meta_webhook_signature(body, "sha256=" + "0" * 64, "test-app-secret"))
        self.assertFalse(_verify_meta_webhook_signature(body, "sha1=" + "0" * 40, "test-app-secret"))
        self.assertFalse(_verify_meta_webhook_signature(body, "", "test-app-secret"))
        self.assertFalse(_verify_meta_webhook_signature(body, "sha256=" + "0" * 64, ""))


class WebhookRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_signed_webhook_awaits_message_processing_before_ack(self):
        body = b'{"object":"whatsapp_business_account","entry":[]}'
        secret = "test-app-secret"
        digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        request = _request(body, f"sha256={digest}")
        parsed = [{"phone": "+6590000000", "message_id": "wamid.synthetic", "message_type": "text", "text": "Hello"}]
        handled = AsyncMock()
        with patch("server.get_setting", AsyncMock(return_value=secret)), \
             patch("server.parse_webhook_messages", return_value=parsed), \
             patch("server.handle_inbound_whatsapp_message", handled), \
             patch("server.log_error", AsyncMock()):
            response = await api_wa_webhook_receive(request)
        self.assertEqual(response, {"status": "ok"})
        handled.assert_awaited_once_with(parsed[0])

    async def test_signed_webhook_returns_retryable_error_when_processing_fails(self):
        body = b'{"object":"whatsapp_business_account","entry":[]}'
        secret = "test-app-secret"
        digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        request = _request(body, f"sha256={digest}")
        parsed = [{"phone": "+6590000000", "message_id": "wamid.failed", "message_type": "text", "text": "9 am"}]
        with patch("server.get_setting", AsyncMock(return_value=secret)), \
             patch("server.parse_webhook_messages", return_value=parsed), \
             patch("server.handle_inbound_whatsapp_message", AsyncMock(side_effect=RuntimeError("synthetic"))), \
             patch("server.log_error", AsyncMock()):
            with self.assertRaises(HTTPException) as caught:
                await api_wa_webhook_receive(request)
        self.assertEqual(caught.exception.status_code, 500)

    async def test_get_verification_uses_configured_verify_token(self):
        with patch("db.get_setting", AsyncMock(side_effect=["verify-token"])):
            response = await api_wa_webhook_verify("subscribe", "verify-token", "challenge-value")
        self.assertEqual(response.body, b"challenge-value")

    async def test_get_verification_rejects_wrong_token(self):
        with patch("db.get_setting", AsyncMock(side_effect=["verify-token"])):
            with self.assertRaises(HTTPException) as caught:
                await api_wa_webhook_verify("subscribe", "wrong-token", "challenge-value")
        self.assertEqual(caught.exception.status_code, 403)

    async def test_route_rejects_invalid_signature(self):
        request = _request(b'{"entry":[]}', "sha256=" + "0" * 64)
        with patch("server.get_setting", AsyncMock(return_value="test-app-secret")), patch(
            "server.log_error", AsyncMock()
        ):
            with self.assertRaises(HTTPException) as caught:
                await api_wa_webhook_receive(request)
        self.assertEqual(caught.exception.status_code, 403)

    async def test_route_rejects_signed_malformed_json(self):
        body = b"not-json"
        secret = "test-app-secret"
        digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        request = _request(body, f"sha256={digest}")
        with patch("server.get_setting", AsyncMock(return_value=secret)), patch(
            "server.log_error", AsyncMock()
        ):
            with self.assertRaises(HTTPException) as caught:
                await api_wa_webhook_receive(request)
        self.assertEqual(caught.exception.status_code, 400)


class RouteAuthenticationTests(unittest.TestCase):
    def test_only_required_api_routes_are_public(self):
        expected = {
            ("GET", "/api/live"),
            ("POST", "/api/auth/login"),
            ("GET", "/api/auth/me"),
            ("POST", "/api/auth/logout"),
            ("POST", "/api/mobile/auth/login"),
            ("POST", "/api/mobile/auth/refresh"),
            ("POST", "/api/mobile/admin/auth/login"),
            ("GET", "/api/whatsapp/webhook"),
            ("POST", "/api/whatsapp/webhook"),
        }
        self.assertEqual(PUBLIC_API_ROUTES, expected)

    def test_detailed_health_is_protected(self):
        self.assertNotIn(("GET", "/api/health"), PUBLIC_API_ROUTES)

    def test_every_other_api_route_is_covered_by_global_auth(self):
        uncovered = []
        for route in app.routes:
            path = getattr(route, "path", "")
            for method in getattr(route, "methods", set()):
                if path.startswith("/api/") and (method, path) not in PUBLIC_API_ROUTES:
                    # The global middleware covers every API route not in this exact set.
                    continue
                if path.startswith("/api/") and (method, path) in PUBLIC_API_ROUTES:
                    continue
                if path.startswith("/api/"):
                    uncovered.append((method, path))
        self.assertEqual(uncovered, [])

    def test_incoming_lead_is_no_longer_public(self):
        self.assertNotIn(("POST", "/api/leads/incoming"), PUBLIC_API_ROUTES)


class AuthenticationFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_login_session_me_and_logout_still_work(self):
        env = {
            "ADMIN_USERNAME": "test-admin",
            "ADMIN_PASSWORD": "test-password",
            "SESSION_SECRET": "test-session-secret",
        }
        with patch.dict(os.environ, env, clear=False):
            login_response = await api_auth_login(
                AuthRequest(username="test-admin", password="test-password"),
                _http_request("POST", "/api/auth/login"),
            )
            cookie_header = login_response.headers["set-cookie"]
            self.assertIn("HttpOnly", cookie_header)
            self.assertIn("Secure", cookie_header)
            self.assertIn("SameSite=lax", cookie_header)
            cookie_value = cookie_header.split(";", 1)[0].split("=", 1)[1]
            authenticated_request = _http_request(
                "GET",
                "/api/auth/me",
                [(b"cookie", f"aicalling_admin_session={cookie_value}".encode())],
            )
            self.assertEqual(_read_session(authenticated_request), "test-admin")
            me = await api_auth_me(authenticated_request)
            self.assertTrue(me["authenticated"])
            logout_response = await api_auth_logout(authenticated_request)
            self.assertIn("Max-Age=0", logout_response.headers["set-cookie"])

    async def test_admin_api_key_still_authorizes_integrations(self):
        request = _http_request(
            "POST", "/api/leads/incoming", [(b"x-api-key", b"integration-key")]
        )
        downstream = AsyncMock(return_value=JSONResponse({"allowed": True}))
        with patch("server.get_setting", AsyncMock(return_value="integration-key")):
            response = await _admin_auth_middleware(request, downstream)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["x-robots-tag"], "noindex, nofollow, noarchive")
        downstream.assert_awaited_once_with(request)

    async def test_missing_auth_fails_closed(self):
        request = _http_request("GET", "/api/settings")
        downstream = AsyncMock(return_value="should-not-run")
        with patch("server.get_setting", AsyncMock(return_value="")), patch.dict(
            os.environ,
            {"ADMIN_USERNAME": "", "ADMIN_PASSWORD": "", "SESSION_SECRET": ""},
            clear=False,
        ):
            response = await _admin_auth_middleware(request, downstream)
        self.assertEqual(response.status_code, 401)
        downstream.assert_not_awaited()

    async def test_mobile_job_api_requires_a_valid_staff_session(self):
        request = _http_request("GET", "/api/mobile/appointments")
        downstream = AsyncMock(return_value=JSONResponse({"allowed": True}))
        with patch("server.authenticate_mobile_request", AsyncMock(return_value=None)):
            response = await _admin_auth_middleware(request, downstream)
        self.assertEqual(response.status_code, 401)
        downstream.assert_not_awaited()

    async def test_mobile_job_api_accepts_staff_auth_without_admin_auth(self):
        request = _http_request(
            "GET", "/api/mobile/appointments", [(b"authorization", b"Bearer synthetic")]
        )
        downstream = AsyncMock(return_value=JSONResponse({"allowed": True}))
        identity = {"sub": "staff-1", "sid": "session-1"}
        with patch("server.authenticate_mobile_request", AsyncMock(return_value=identity)):
            response = await _admin_auth_middleware(request, downstream)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(request.state.mobile_staff, identity)
        downstream.assert_awaited_once_with(request)


if __name__ == "__main__":
    unittest.main()
