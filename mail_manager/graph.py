from collections.abc import Mapping
import json
import posixpath
from urllib.parse import quote, unquote, urlsplit, urlunsplit

import requests

from mail_manager.models import MailManagerError


TOKEN_URL = "https://login.microsoftonline.com/consumers/oauth2/v2.0/token"
GRAPH_BASE = "https://graph.microsoft.com/v1.0"
OAUTH_SCOPE = "offline_access openid profile Mail.Read"
MAX_RETRY_AFTER_SECONDS = 3600
TOKEN_RESPONSE_MAX_BYTES = 64 * 1024
GRAPH_RESPONSE_MAX_BYTES = 2 * 1024 * 1024
GRAPH_MESSAGE_MAX_BYTES = 4 * 1024 * 1024
GRAPH_TOTAL_MAX_BYTES = 8 * 1024 * 1024
MAX_MESSAGE_ID_LENGTH = 1024
REQUEST_TIMEOUT = (5, 15)
RESPONSE_CHUNK_SIZE = 64 * 1024


class GraphClientError(MailManagerError):
    """Base class for sanitized Microsoft Graph failures."""


class InvalidGrant(GraphClientError):
    pass


class UpstreamRateLimited(GraphClientError):
    def __init__(self, retry_after=None):
        self.retry_after = retry_after
        super().__init__("Microsoft Graph rate limited the request")


class UpstreamUnavailable(GraphClientError):
    pass


class UpstreamProtocolError(GraphClientError):
    pass


class GraphAuthorizationRejected(GraphClientError):
    pass


class GraphPermissionDenied(GraphClientError):
    pass


def _retry_after_seconds(value):
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or not value.isascii() or not value.isdigit():
        return None
    significant_digits = value.lstrip("0") or "0"
    if len(significant_digits) > len(str(MAX_RETRY_AFTER_SECONDS)):
        return MAX_RETRY_AFTER_SECONDS
    return min(int(significant_digits), MAX_RETRY_AFTER_SECONDS)


def _json_mapping(body):
    try:
        payload = json.loads(body)
    except (TypeError, ValueError):
        raise UpstreamProtocolError("Microsoft Graph returned invalid JSON") from None
    if not isinstance(payload, Mapping):
        raise UpstreamProtocolError(
            "Microsoft Graph returned an invalid response"
        ) from None
    return payload


def _bounded_response_body(response, max_bytes):
    try:
        content_length = response.headers.get("Content-Length")
        if content_length is not None:
            try:
                declared_length = int(content_length, 10)
            except (TypeError, ValueError):
                raise UpstreamProtocolError(
                    "Microsoft Graph returned an invalid content length"
                ) from None
            if declared_length < 0:
                raise UpstreamProtocolError(
                    "Microsoft Graph returned an invalid content length"
                ) from None
            if declared_length > max_bytes:
                raise UpstreamProtocolError(
                    "Microsoft Graph response exceeded the size limit"
                ) from None

        chunks = []
        total_bytes = 0
        for chunk in response.iter_content(chunk_size=RESPONSE_CHUNK_SIZE):
            if not chunk:
                continue
            if not isinstance(chunk, (bytes, bytearray)):
                raise UpstreamProtocolError(
                    "Microsoft Graph returned an invalid response body"
                ) from None
            total_bytes += len(chunk)
            if total_bytes > max_bytes:
                raise UpstreamProtocolError(
                    "Microsoft Graph response exceeded the size limit"
                ) from None
            chunks.append(bytes(chunk))
        return b"".join(chunks), total_bytes
    except requests.RequestException:
        raise UpstreamUnavailable("Microsoft Graph is unavailable") from None
    finally:
        try:
            response.close()
        except Exception:
            pass


def _close_response(response):
    try:
        response.close()
    except Exception:
        pass


def _raise_for_upstream_status(response, *, oauth):
    if response.status_code == 429:
        retry_after = _retry_after_seconds(response.headers.get("Retry-After"))
        _close_response(response)
        raise UpstreamRateLimited(retry_after) from None
    if 500 <= response.status_code < 600:
        _close_response(response)
        raise UpstreamUnavailable("Microsoft Graph is unavailable") from None
    if 400 <= response.status_code < 500:
        if oauth and response.status_code == 400:
            return
        status_code = response.status_code
        _close_response(response)
        if not oauth and status_code == 401:
            raise GraphAuthorizationRejected(
                "Microsoft Graph rejected mail authorization"
            ) from None
        if not oauth and status_code == 403:
            raise GraphPermissionDenied(
                "Microsoft Graph denied mail permission"
            ) from None
        raise UpstreamProtocolError("Microsoft Graph rejected the request") from None
    if not 200 <= response.status_code < 300:
        _close_response(response)
        raise UpstreamProtocolError(
            "Microsoft Graph returned an unexpected status"
        ) from None


def _response_json(response, max_bytes, *, oauth):
    _raise_for_upstream_status(response, oauth=oauth)
    body, bytes_read = _bounded_response_body(response, max_bytes)
    payload = _json_mapping(body)
    if oauth and response.status_code == 400:
        if payload.get("error") == "invalid_grant":
            raise InvalidGrant("Microsoft refresh token is invalid") from None
        raise UpstreamProtocolError("Microsoft Graph rejected the request") from None
    return payload, bytes_read


class GraphClient:
    def __init__(self, session=None):
        self.session = session if session is not None else requests.Session()

    def refresh(self, client_id, refresh_token):
        client_id = self._require_secret(client_id, "client_id")
        refresh_token = self._require_secret(refresh_token, "refresh_token")
        try:
            response = self.session.post(
                TOKEN_URL,
                data={
                    "client_id": client_id,
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "scope": OAUTH_SCOPE,
                },
                timeout=REQUEST_TIMEOUT,
                allow_redirects=False,
                stream=True,
            )
        except requests.RequestException:
            raise UpstreamUnavailable("Microsoft Graph is unavailable") from None

        payload, _ = _response_json(response, TOKEN_RESPONSE_MAX_BYTES, oauth=True)
        access_token = payload.get("access_token")
        if not isinstance(access_token, str) or not access_token.strip():
            raise UpstreamProtocolError(
                "Microsoft Graph omitted the access token"
            ) from None
        access_token = access_token.strip()
        new_refresh_token = payload.get("refresh_token")
        if not isinstance(new_refresh_token, str) or not new_refresh_token.strip():
            new_refresh_token = refresh_token
        else:
            new_refresh_token = new_refresh_token.strip()
        return access_token, new_refresh_token

    def list_messages(self, access_token, folder, limit):
        access_token = self._require_secret(access_token, "access_token")
        if folder not in {"inbox", "junkemail"}:
            raise ValueError("folder must be inbox or junkemail")
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 100
        ):
            raise ValueError("limit must be an integer from 1 to 100")

        url = f"{GRAPH_BASE}/me/mailFolders/{folder}/messages"
        params = {
            "$top": min(50, limit),
            "$orderby": "receivedDateTime desc",
            "$select": "id,subject,from,receivedDateTime,bodyPreview",
        }
        messages = []
        remaining_total_bytes = GRAPH_TOTAL_MAX_BYTES
        _, initial_url_key = _trusted_next_link(url)
        seen_urls = {initial_url_key}
        page_count = 0
        while page_count < limit:
            page_count += 1
            try:
                response = self.session.get(
                    url,
                    headers={"Authorization": f"Bearer {access_token}"},
                    params=params,
                    timeout=REQUEST_TIMEOUT,
                    allow_redirects=False,
                    stream=True,
                )
            except requests.RequestException:
                raise UpstreamUnavailable("Microsoft Graph is unavailable") from None

            page_max_bytes = min(GRAPH_RESPONSE_MAX_BYTES, remaining_total_bytes)
            payload, page_bytes = _response_json(response, page_max_bytes, oauth=False)
            remaining_total_bytes -= page_bytes
            page_messages = payload.get("value")
            if not isinstance(page_messages, list):
                raise UpstreamProtocolError(
                    "Microsoft Graph returned an invalid message page"
                ) from None
            if not page_messages:
                return messages

            previous_count = len(messages)
            remaining = limit - len(messages)
            messages.extend(page_messages[:remaining])
            if len(messages) >= limit:
                return messages
            if len(messages) == previous_count:
                return messages

            next_link = payload.get("@odata.nextLink")
            if next_link is None:
                return messages
            if remaining_total_bytes <= 0:
                raise UpstreamProtocolError(
                    "Microsoft Graph pagination exceeded the total size limit"
                ) from None
            trusted_next_link, next_link_key = _trusted_next_link(next_link)
            if next_link_key in seen_urls:
                raise UpstreamProtocolError(
                    "Microsoft Graph returned a repeated next link"
                ) from None
            seen_urls.add(next_link_key)
            url = trusted_next_link
            params = None
        return messages

    def get_message(self, access_token, folder, message_id):
        access_token = self._require_secret(access_token, "access_token")
        if folder not in {"inbox", "junkemail"}:
            raise ValueError("folder must be inbox or junkemail")
        message_id = self._require_secret(message_id, "message_id")
        if len(message_id) > MAX_MESSAGE_ID_LENGTH:
            raise ValueError("message_id is too long")
        encoded_id = quote(message_id, safe="")
        url = f"{GRAPH_BASE}/me/mailFolders/{folder}/messages/{encoded_id}"
        try:
            response = self.session.get(
                url,
                headers={"Authorization": f"Bearer {access_token}"},
                params={
                    "$select": "id,subject,from,receivedDateTime,body,bodyPreview"
                },
                timeout=REQUEST_TIMEOUT,
                allow_redirects=False,
                stream=True,
            )
        except requests.RequestException:
            raise UpstreamUnavailable("Microsoft Graph is unavailable") from None
        payload, _ = _response_json(
            response, GRAPH_MESSAGE_MAX_BYTES, oauth=False
        )
        return payload

    @staticmethod
    def _require_secret(value, field_name):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{field_name} must be a non-empty string")
        return value.strip()


def _trusted_next_link(value):
    if (
        not isinstance(value, str)
        or value != value.strip()
        or "#" in value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise UpstreamProtocolError("Microsoft Graph returned an unsafe next link")
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        port = parsed.port
    except (UnicodeError, ValueError):
        raise UpstreamProtocolError(
            "Microsoft Graph returned an unsafe next link"
        ) from None
    if (
        parsed.scheme != "https"
        or hostname != "graph.microsoft.com"
        or port not in {None, 443}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise UpstreamProtocolError(
            "Microsoft Graph returned an unsafe next link"
        ) from None

    decoded_path = parsed.path
    try:
        for _ in range(2):
            next_path = unquote(decoded_path, errors="strict")
            if next_path == decoded_path:
                break
            decoded_path = next_path
        else:
            if unquote(decoded_path, errors="strict") != decoded_path:
                raise UpstreamProtocolError(
                    "Microsoft Graph returned an unsafe next link"
                ) from None
    except (UnicodeError, ValueError):
        raise UpstreamProtocolError(
            "Microsoft Graph returned an unsafe next link"
        ) from None

    path_segments = decoded_path.split("/")
    if (
        "\\" in decoded_path
        or any(
            ord(character) < 32 or ord(character) == 127 for character in decoded_path
        )
        or any(segment in {".", ".."} for segment in path_segments)
        or posixpath.normpath(decoded_path) != decoded_path
        or not decoded_path.startswith("/v1.0/")
    ):
        raise UpstreamProtocolError(
            "Microsoft Graph returned an unsafe next link"
        ) from None

    canonical_path = quote(decoded_path, safe="/!$&'()*+,-.:;=@_~")
    canonical_url = urlunsplit(
        ("https", "graph.microsoft.com", canonical_path, parsed.query, "")
    )
    return value, canonical_url
