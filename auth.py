import logging
import os
import urllib.parse
from typing import Optional

from logging_config import setup_logger_with_stream_and_file


auth_logger = logging.getLogger("mcp.auth")
setup_logger_with_stream_and_file(auth_logger, "auth.log")


class TokenAuthMiddleware:
	"""
	Minimal ASGI auth middleware checking a shared token.

	- Reads expected token from AUTH_TOKEN env var
	- Accepts either Authorization: Bearer <token> header or ?token=<token> query param
	- If AUTH_TOKEN is empty or not set, auth is disabled (requests are allowed)
	"""

	def __init__(self, app) -> None:
		self.app = app
		self.expected_token = os.environ.get("AUTH_TOKEN", "").strip()
		# Log once if auth disabled
		if not self.expected_token:
			auth_logger.info("AUTH_TOKEN not set; authentication is DISABLED")
		else:
			auth_logger.info("AUTH_TOKEN configured; authentication is ENABLED")

	async def __call__(self, scope, receive, send):
		if scope.get("type") != "http":
			return await self.app(scope, receive, send)

		# Skip check if no token configured
		if not self.expected_token:
			return await self.app(scope, receive, send)

		# Extract token from Authorization header or query string
		token = None
		try:
			raw_headers = scope.get("headers", [])
			headers = {k.decode().lower(): v.decode() for k, v in raw_headers}
			auth_header = headers.get("authorization", "")
			if auth_header:
				parts = auth_header.split()
				if len(parts) == 2 and parts[0].lower() == "bearer":
					token = parts[1]
		except Exception:
			token = None

		if not token:
			try:
				raw_qs = scope.get("query_string", b"")
				if isinstance(raw_qs, (bytes, bytearray)):
					raw_qs = raw_qs.decode("utf-8", errors="ignore")
				qs = urllib.parse.parse_qs(raw_qs, keep_blank_values=True)
				token_candidates = qs.get("token") or []
				if token_candidates:
					token = token_candidates[0]
			except Exception:
				token = None

		if token == self.expected_token:
			return await self.app(scope, receive, send)

		# Unauthorized
		message = b'{"detail":"Unauthorized"}'
		headers = [
			[b"content-type", b"application/json"],
			[b"www-authenticate", b"Bearer"],
		]
		await send({
			"type": "http.response.start",
			"status": 401,
			"headers": headers,
		})
		await send({
			"type": "http.response.body",
			"body": message,
			"more_body": False,
		})
		return


