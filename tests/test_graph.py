from importlib import import_module
from urllib.parse import parse_qs, quote, urlsplit
import json
import traceback

import pytest
import requests


graph = import_module("mail_manager.graph")


class ChunkedResponse:
    def __init__(self, chunks, *, headers=None, status_code=200):
        self.chunks = list(chunks)
        self.headers = {} if headers is None else headers
        self.status_code = status_code
        self.chunk_size = None
        self.chunks_yielded = 0
        self.closed = False

    def iter_content(self, chunk_size):
        self.chunk_size = chunk_size
        for chunk in self.chunks:
            self.chunks_yielded += 1
            yield chunk

    def close(self):
        self.closed = True


class StaticSession:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return self.response

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return self.response


class InfiniteResponse(ChunkedResponse):
    def __init__(self, *, headers=None, status_code):
        super().__init__([], headers=headers, status_code=status_code)
        self.iterated = False

    def iter_content(self, chunk_size):
        self.chunk_size = chunk_size
        self.iterated = True
        while True:
            yield b"x" * chunk_size


class PagingSession:
    def __init__(self, *, page_size, page_count):
        self.page_size = page_size
        self.page_count = page_count
        self.calls = []

    def get(self, url, **kwargs):
        index = len(self.calls)
        self.calls.append((url, kwargs))
        payload = {"value": [{"id": f"message-{index}"}]}
        if index + 1 < self.page_count:
            payload["@odata.nextLink"] = (
                f"{graph.GRAPH_BASE}/me/messages?$skiptoken=page-{index + 1}"
            )
        body = json.dumps(payload, separators=(",", ":")).encode()
        body += b" " * (self.page_size - len(body))
        return ChunkedResponse(
            [body],
            headers={"Content-Length": str(len(body))},
        )


def test_graph_client_exposes_fixed_microsoft_endpoints():
    assert graph.TOKEN_URL == (
        "https://login.microsoftonline.com/consumers/oauth2/v2.0/token"
    )
    assert graph.GRAPH_BASE == "https://graph.microsoft.com/v1.0"
    assert graph.TOKEN_RESPONSE_MAX_BYTES == 64 * 1024
    assert graph.GRAPH_RESPONSE_MAX_BYTES == 2 * 1024 * 1024
    assert graph.GRAPH_TOTAL_MAX_BYTES == 8 * 1024 * 1024

    with pytest.raises(TypeError):
        graph.GraphClient(timeout=None)


def test_refresh_posts_only_the_fixed_consumer_oauth_form(requests_mock):
    session = requests.Session()
    requests_mock.post(
        graph.TOKEN_URL,
        json={"access_token": "access-new", "refresh_token": "refresh-new"},
    )

    result = graph.GraphClient(session=session).refresh("client-id", "refresh-old")

    assert result == ("access-new", "refresh-new")
    request = requests_mock.last_request
    assert request.timeout == (5, 15)
    assert request.stream is True
    assert parse_qs(request.text) == {
        "client_id": ["client-id"],
        "grant_type": ["refresh_token"],
        "refresh_token": ["refresh-old"],
        "scope": ["offline_access openid profile Mail.Read"],
    }


def test_refresh_falls_back_to_the_existing_refresh_token(requests_mock):
    requests_mock.post(graph.TOKEN_URL, json={"access_token": "access-new"})

    assert graph.GraphClient().refresh("client-id", "refresh-old") == (
        "access-new",
        "refresh-old",
    )


@pytest.mark.parametrize("access_token", ["", "   ", "\t\r\n", None, 42])
def test_refresh_rejects_access_tokens_that_are_not_nonempty_strings(
    requests_mock, access_token
):
    requests_mock.post(graph.TOKEN_URL, json={"access_token": access_token})

    with pytest.raises(graph.UpstreamProtocolError):
        graph.GraphClient().refresh("client-id", "refresh-old")


@pytest.mark.parametrize("new_refresh_token", [None, 42, "", "   ", "\t\r\n"])
def test_refresh_falls_back_to_a_stripped_existing_token(
    requests_mock, new_refresh_token
):
    requests_mock.post(
        graph.TOKEN_URL,
        json={
            "access_token": " access-new ",
            "refresh_token": new_refresh_token,
        },
    )

    assert graph.GraphClient().refresh("client-id", " refresh-old ") == (
        "access-new",
        "refresh-old",
    )


def test_refresh_strips_a_nonempty_rotated_refresh_token(requests_mock):
    requests_mock.post(
        graph.TOKEN_URL,
        json={
            "access_token": " access-new ",
            "refresh_token": " refresh-new ",
        },
    )

    assert graph.GraphClient().refresh("client-id", " refresh-old ") == (
        "access-new",
        "refresh-new",
    )


def test_refresh_stops_streaming_and_closes_when_chunks_exceed_the_limit():
    response_secret = b"response-secret"
    response = ChunkedResponse([b"x" * graph.TOKEN_RESPONSE_MAX_BYTES, response_secret])
    session = StaticSession(response)

    with pytest.raises(graph.UpstreamProtocolError) as captured:
        graph.GraphClient(session=session).refresh("client-id", "refresh-secret")

    assert response.chunk_size == 64 * 1024
    assert response.chunks_yielded == 2
    assert response.closed is True
    assert response_secret.decode() not in str(captured.value)
    assert session.calls[0][2]["timeout"] == (5, 15)
    assert session.calls[0][2]["stream"] is True
    assert session.calls[0][2]["allow_redirects"] is False


def test_refresh_rejects_an_oversized_content_length_without_reading_body():
    response = ChunkedResponse(
        [b"response-secret"],
        headers={"Content-Length": str(graph.TOKEN_RESPONSE_MAX_BYTES + 1)},
    )

    with pytest.raises(graph.UpstreamProtocolError):
        graph.GraphClient(session=StaticSession(response)).refresh(
            "client-id", "refresh-secret"
        )

    assert response.chunks_yielded == 0
    assert response.closed is True


def test_refresh_accepts_a_json_response_exactly_at_the_byte_limit():
    prefix = b'{"access_token":"'
    suffix = b'"}'
    token_length = graph.TOKEN_RESPONSE_MAX_BYTES - len(prefix) - len(suffix)
    body = prefix + (b"a" * token_length) + suffix
    response = ChunkedResponse([body], headers={"Content-Length": str(len(body))})

    access_token, refresh_token = graph.GraphClient(
        session=StaticSession(response)
    ).refresh("client-id", "refresh-old")

    assert len(access_token) == token_length
    assert refresh_token == "refresh-old"
    assert response.closed is True


def test_list_messages_rejects_a_graph_page_with_oversized_content_length():
    response = ChunkedResponse(
        [b"response-secret"],
        headers={"Content-Length": str(graph.GRAPH_RESPONSE_MAX_BYTES + 1)},
    )

    with pytest.raises(graph.UpstreamProtocolError):
        graph.GraphClient(session=StaticSession(response)).list_messages(
            "access-secret", "inbox", 10
        )

    assert response.chunks_yielded == 0
    assert response.closed is True


@pytest.mark.parametrize(
    ("client_id", "refresh_token"),
    [
        (None, "refresh-old"),
        ("", "refresh-old"),
        (42, "refresh-old"),
        ("client-id", None),
        ("client-id", ""),
        ("client-id", object()),
    ],
)
def test_refresh_rejects_missing_or_mistyped_credentials_locally(
    requests_mock, client_id, refresh_token
):
    requests_mock.post(graph.TOKEN_URL, json={"access_token": "unused"})

    with pytest.raises(ValueError) as captured:
        graph.GraphClient().refresh(client_id, refresh_token)

    assert requests_mock.call_count == 0
    assert "refresh-old" not in str(captured.value)


def test_refresh_maps_only_exact_invalid_grant_errors(requests_mock):
    requests_mock.post(
        graph.TOKEN_URL,
        status_code=400,
        json={"error": "invalid_grant", "error_description": "response-secret"},
    )

    with pytest.raises(graph.InvalidGrant) as captured:
        graph.GraphClient().refresh("client-id", "refresh-secret")

    assert "response-secret" not in str(captured.value)
    assert "refresh-secret" not in repr(captured.value)


@pytest.mark.parametrize(
    "payload",
    [
        {"error": "invalid_client"},
        {"error": "INVALID_GRANT"},
        {},
    ],
)
def test_refresh_maps_other_400_errors_to_protocol_error(requests_mock, payload):
    requests_mock.post(graph.TOKEN_URL, status_code=400, json=payload)

    with pytest.raises(graph.UpstreamProtocolError):
        graph.GraphClient().refresh("client-id", "refresh-secret")


@pytest.mark.parametrize(
    ("retry_after_header", "expected"),
    [
        ("later", None),
        ("-1", None),
        ("999999999999999999999999999999999999", 3600),
        ("120", 120),
    ],
)
def test_refresh_parses_retry_after_with_a_safe_upper_bound(
    requests_mock, retry_after_header, expected
):
    requests_mock.post(
        graph.TOKEN_URL,
        status_code=429,
        headers={"Retry-After": retry_after_header},
        text="response-secret",
    )

    with pytest.raises(graph.UpstreamRateLimited) as captured:
        graph.GraphClient().refresh("client-id", "refresh-secret")

    assert captured.value.retry_after == expected
    assert "response-secret" not in repr(captured.value)


@pytest.mark.parametrize("status_code", [500, 503, 599])
def test_refresh_maps_server_errors_to_upstream_unavailable(requests_mock, status_code):
    requests_mock.post(graph.TOKEN_URL, status_code=status_code, text="response-secret")

    with pytest.raises(graph.UpstreamUnavailable):
        graph.GraphClient().refresh("client-id", "refresh-secret")


@pytest.mark.parametrize(
    ("status_code", "expected_error"),
    [
        (429, graph.UpstreamRateLimited),
        (503, graph.UpstreamUnavailable),
    ],
)
def test_refresh_classifies_error_status_without_reading_an_infinite_body(
    status_code, expected_error
):
    response = InfiniteResponse(
        status_code=status_code,
        headers={
            "Content-Length": str(graph.TOKEN_RESPONSE_MAX_BYTES + 1),
            "Retry-After": "17",
        },
    )

    with pytest.raises(expected_error) as captured:
        graph.GraphClient(session=StaticSession(response)).refresh(
            "client-id", "refresh-secret"
        )

    assert response.iterated is False
    assert response.closed is True
    if status_code == 429:
        assert captured.value.retry_after == 17


@pytest.mark.parametrize("network_error", [requests.Timeout, requests.ConnectionError])
def test_refresh_maps_network_failures_to_upstream_unavailable(
    requests_mock, network_error
):
    refresh_secret = "refresh-secret"
    requests_mock.post(graph.TOKEN_URL, exc=network_error("network-secret"))

    with pytest.raises(graph.UpstreamUnavailable) as captured:
        graph.GraphClient().refresh("client-id", refresh_secret)

    rendered = "".join(
        traceback.format_exception(
            type(captured.value), captured.value, captured.value.__traceback__
        )
    )
    assert "network-secret" not in rendered
    assert "refresh-secret" not in rendered


@pytest.mark.parametrize(
    ("response_kwargs", "sensitive_value"),
    [
        ({"text": "not-json-response-secret"}, "not-json-response-secret"),
        ({"json": {}}, "refresh-secret"),
        ({"json": {"access_token": None}}, "refresh-secret"),
    ],
)
def test_refresh_rejects_invalid_success_payload_without_leaking_secrets(
    requests_mock, response_kwargs, sensitive_value
):
    refresh_secret = "refresh-secret"
    requests_mock.post(graph.TOKEN_URL, **response_kwargs)

    with pytest.raises(graph.UpstreamProtocolError) as captured:
        graph.GraphClient().refresh("client-id", refresh_secret)

    rendered = "".join(
        traceback.format_exception(
            type(captured.value), captured.value, captured.value.__traceback__
        )
    )
    assert sensitive_value not in str(captured.value)
    assert sensitive_value not in repr(captured.value)
    assert sensitive_value not in rendered


def test_list_messages_uses_bounded_query_and_preserves_graph_dictionaries(
    requests_mock,
):
    message = {
        "id": "message-1",
        "subject": "Subject",
        "from": {"emailAddress": {"address": "sender@example.test"}},
        "receivedDateTime": "2026-07-13T00:00:00Z",
        "body": {"contentType": "html", "content": "<p>Body</p>"},
        "bodyPreview": "Body",
        "futureGraphField": {"preserved": True},
    }
    requests_mock.get(
        f"{graph.GRAPH_BASE}/me/mailFolders/inbox/messages",
        json={"value": [message]},
    )

    result = graph.GraphClient().list_messages("access-secret", "inbox", 30)

    assert result == [message]
    request = requests_mock.last_request
    assert request.timeout == (5, 15)
    assert request.stream is True
    assert request.headers["Authorization"] == "Bearer access-secret"
    assert parse_qs(urlsplit(request.url).query) == {
        "$top": ["30"],
        "$orderby": ["receivedDateTime desc"],
        "$select": [
            "id,subject,from,receivedDateTime,bodyPreview",
        ],
    }


def test_list_messages_follows_two_pages_and_truncates_at_limit(requests_mock):
    initial_url = f"{graph.GRAPH_BASE}/me/mailFolders/junkemail/messages"
    next_link = (
        f"{graph.GRAPH_BASE}/me/mailFolders/junkemail/messages?$skiptoken=opaque"
    )
    first_page = [{"id": f"message-{index}"} for index in range(50)]
    second_page = [{"id": f"message-{index}"} for index in range(50, 80)]
    requests_mock.get(
        initial_url,
        json={"value": first_page, "@odata.nextLink": next_link},
    )
    requests_mock.get(next_link, json={"value": second_page})

    result = graph.GraphClient().list_messages("access-secret", "junkemail", 60)

    assert [message["id"] for message in result] == [
        f"message-{index}" for index in range(60)
    ]
    assert requests_mock.call_count == 2
    assert parse_qs(urlsplit(requests_mock.request_history[0].url).query)["$top"] == [
        "50"
    ]
    assert parse_qs(urlsplit(requests_mock.request_history[1].url).query) == {
        "$skiptoken": ["opaque"]
    }


def test_list_messages_accepts_explicit_https_port_443_next_link(requests_mock):
    initial_url = f"{graph.GRAPH_BASE}/me/mailFolders/inbox/messages"
    next_link = (
        "https://graph.microsoft.com:443/v1.0/me/mailFolders/inbox/messages"
        "?$skiptoken=opaque"
    )
    requests_mock.get(
        initial_url,
        json={"value": [{"id": "first"}], "@odata.nextLink": next_link},
    )
    requests_mock.get(next_link, json={"value": [{"id": "second"}]})

    assert graph.GraphClient().list_messages("access-secret", "inbox", 2) == [
        {"id": "first"},
        {"id": "second"},
    ]


def test_get_message_encodes_id_and_requests_full_body(requests_mock):
    message_id = "AAMk+/= id"
    message = {
        "id": message_id,
        "subject": "Subject",
        "from": {"emailAddress": {"address": "sender@example.test"}},
        "receivedDateTime": "2026-07-13T00:00:00Z",
        "body": {"contentType": "html", "content": "<p>Body</p>"},
        "bodyPreview": "Body",
    }
    requests_mock.get(
        f"{graph.GRAPH_BASE}/me/mailFolders/inbox/messages/{quote(message_id, safe='')}",
        json=message,
    )

    assert graph.GraphClient().get_message("access-secret", "inbox", message_id) == message
    request = requests_mock.last_request
    assert parse_qs(urlsplit(request.url).query) == {
        "$select": ["id,subject,from,receivedDateTime,body,bodyPreview"]
    }


@pytest.mark.parametrize("message_id", [None, "", 42, "x" * 1025])
def test_get_message_rejects_invalid_ids_without_network(requests_mock, message_id):
    with pytest.raises(ValueError):
        graph.GraphClient().get_message("access-secret", "inbox", message_id)
    assert requests_mock.call_count == 0


@pytest.mark.parametrize(
    ("status_code", "expected_error"),
    [
        (401, graph.GraphAuthorizationRejected),
        (403, graph.GraphPermissionDenied),
    ],
)
def test_graph_mail_authorization_errors_are_classified_and_sanitized(
    requests_mock, status_code, expected_error
):
    requests_mock.get(
        f"{graph.GRAPH_BASE}/me/mailFolders/inbox/messages",
        status_code=status_code,
        text="response-secret",
    )

    access_secret = "access-" + "secret"
    with pytest.raises(expected_error) as captured:
        graph.GraphClient().list_messages(access_secret, "inbox", 10)

    assert access_secret not in str(captured.value)
    assert access_secret not in repr(captured.value)
    assert "response-secret" not in str(captured.value)
    assert "response-secret" not in repr(captured.value)


@pytest.mark.parametrize("folder", [None, "", "Inbox", "sentitems", 42])
def test_list_messages_rejects_folders_outside_the_allowlist(requests_mock, folder):
    with pytest.raises(ValueError):
        graph.GraphClient().list_messages("access-secret", folder, 10)

    assert requests_mock.call_count == 0


@pytest.mark.parametrize("limit", [None, 0, 101, True, 10.5, "10"])
def test_list_messages_rejects_limits_outside_one_to_one_hundred(requests_mock, limit):
    with pytest.raises(ValueError):
        graph.GraphClient().list_messages("access-secret", "inbox", limit)

    assert requests_mock.call_count == 0


@pytest.mark.parametrize("access_token", [None, "", 42])
def test_list_messages_rejects_invalid_access_tokens_locally(
    requests_mock, access_token
):
    with pytest.raises(ValueError):
        graph.GraphClient().list_messages(access_token, "inbox", 10)

    assert requests_mock.call_count == 0


@pytest.mark.parametrize("value", [None, {}, "messages"])
def test_list_messages_requires_a_list_value_on_every_page(requests_mock, value):
    requests_mock.get(
        f"{graph.GRAPH_BASE}/me/mailFolders/inbox/messages",
        json={"value": value},
    )

    with pytest.raises(graph.UpstreamProtocolError):
        graph.GraphClient().list_messages("access-secret", "inbox", 10)


@pytest.mark.parametrize(
    "next_link",
    [
        "http://graph.microsoft.com/v1.0/me/messages",
        "//graph.microsoft.com/v1.0/me/messages",
        "https://evil.test/v1.0/me/messages",
        "https://graph.microsoft.com.evil.test/v1.0/me/messages",
        "https://graph.microsoft.com:444/v1.0/me/messages",
        "https://graph.microsoft.com:notaport/v1.0/me/messages",
        "https://user:password@graph.microsoft.com/v1.0/me/messages",
        "https://graph.microsoft.com/beta/me/messages",
        "https://graph.microsoft.com/v1.0evil/me/messages",
        "https://graph.microsoft.com/v1.0/../beta/messages",
        "https://graph.microsoft.com/v1.0/%2e%2e/beta/messages",
        "https://graph.microsoft.com/v1.0/%252e%252e/beta/messages",
        "https://graph.microsoft.com/v1.0/me\\beta/messages",
        "https://graph.microsoft.com/v1.0/me%5cbeta/messages",
        "https://graph.microsoft.com/v1.0//me/messages",
        "https://graph.microsoft.com/v1.0/me/messages#fragment",
        42,
    ],
)
def test_list_messages_rejects_untrusted_next_links(requests_mock, next_link):
    requests_mock.get(
        f"{graph.GRAPH_BASE}/me/mailFolders/inbox/messages",
        json={"value": [{"id": "first"}], "@odata.nextLink": next_link},
    )

    with pytest.raises(graph.UpstreamProtocolError):
        graph.GraphClient().list_messages("access-secret", "inbox", 2)

    assert requests_mock.call_count == 1


def test_list_messages_maps_invalid_grant_body_to_protocol_error(requests_mock):
    requests_mock.get(
        f"{graph.GRAPH_BASE}/me/mailFolders/inbox/messages",
        status_code=400,
        json={"error": "invalid_grant"},
    )

    with pytest.raises(graph.UpstreamProtocolError) as captured:
        graph.GraphClient().list_messages("access-secret", "inbox", 10)

    assert not isinstance(captured.value, graph.InvalidGrant)


def test_list_messages_does_not_read_a_400_response_body():
    response = InfiniteResponse(status_code=400)

    with pytest.raises(graph.UpstreamProtocolError):
        graph.GraphClient(session=StaticSession(response)).list_messages(
            "access-secret", "inbox", 10
        )

    assert response.iterated is False
    assert response.closed is True


def test_list_messages_sanitizes_authorization_and_response_failures(requests_mock):
    access_secret = "access-secret"
    response_secret = "response-secret"
    requests_mock.get(
        f"{graph.GRAPH_BASE}/me/mailFolders/inbox/messages",
        status_code=503,
        text=response_secret,
    )

    with pytest.raises(graph.UpstreamUnavailable) as captured:
        graph.GraphClient().list_messages(access_secret, "inbox", 10)

    rendered = "".join(
        traceback.format_exception(
            type(captured.value), captured.value, captured.value.__traceback__
        )
    )
    for sensitive_value in (access_secret, response_secret):
        assert sensitive_value not in str(captured.value)
        assert sensitive_value not in repr(captured.value)
        assert sensitive_value not in rendered


def test_list_messages_maps_network_timeout_without_leaking_authorization(
    requests_mock,
):
    access_secret = "access-secret"
    requests_mock.get(
        f"{graph.GRAPH_BASE}/me/mailFolders/inbox/messages",
        exc=requests.Timeout("network-secret"),
    )

    with pytest.raises(graph.UpstreamUnavailable) as captured:
        graph.GraphClient().list_messages(access_secret, "inbox", 10)

    rendered = "".join(
        traceback.format_exception(
            type(captured.value), captured.value, captured.value.__traceback__
        )
    )
    assert access_secret not in rendered
    assert "network-secret" not in rendered


def test_list_messages_stops_on_an_empty_page_even_with_a_next_link(requests_mock):
    initial_url = f"{graph.GRAPH_BASE}/me/mailFolders/inbox/messages"
    requests_mock.get(
        initial_url,
        json={
            "value": [],
            "@odata.nextLink": f"{initial_url}?$skiptoken=unused",
        },
    )

    assert graph.GraphClient().list_messages("access-secret", "inbox", 10) == []
    assert requests_mock.call_count == 1


def test_list_messages_rejects_a_self_loop_without_requesting_it_again(requests_mock):
    initial_url = f"{graph.GRAPH_BASE}/me/mailFolders/inbox/messages"
    requests_mock.get(
        initial_url,
        json={"value": [{"id": "first"}], "@odata.nextLink": initial_url},
    )

    with pytest.raises(graph.UpstreamProtocolError):
        graph.GraphClient().list_messages("access-secret", "inbox", 10)

    assert requests_mock.call_count == 1


def test_list_messages_rejects_a_two_url_loop_without_leaking_the_query(
    requests_mock,
):
    access_secret = "access-secret"
    query_secret = "opaque-query-secret"
    initial_url = f"{graph.GRAPH_BASE}/me/mailFolders/inbox/messages"
    first_link = f"{initial_url}?$skiptoken={query_secret}-one"
    second_link = f"{initial_url}?$skiptoken={query_secret}-two"
    requests_mock.get(
        initial_url,
        json={"value": [{"id": "initial"}], "@odata.nextLink": first_link},
    )
    requests_mock.get(
        first_link,
        json={"value": [{"id": "first"}], "@odata.nextLink": second_link},
    )
    requests_mock.get(
        second_link,
        json={"value": [{"id": "second"}], "@odata.nextLink": first_link},
    )

    with pytest.raises(graph.UpstreamProtocolError) as captured:
        graph.GraphClient().list_messages(access_secret, "inbox", 10)

    rendered = "".join(
        traceback.format_exception(
            type(captured.value), captured.value, captured.value.__traceback__
        )
    )
    assert requests_mock.call_count == 3
    assert query_secret not in str(captured.value)
    assert query_secret not in repr(captured.value)
    assert query_secret not in rendered
    assert access_secret not in rendered


def test_list_messages_detects_equivalent_urls_using_a_canonical_key(requests_mock):
    initial_url = f"{graph.GRAPH_BASE}/me/mailFolders/inbox/messages"
    first_link = f"{graph.GRAPH_BASE}/me/messages?$skiptoken=opaque"
    equivalent_link = (
        "https://GRAPH.microsoft.com:443/v1.0/%6de/messages?$skiptoken=opaque"
    )
    requests_mock.get(
        initial_url,
        json={"value": [{"id": "initial"}], "@odata.nextLink": first_link},
    )
    requests_mock.get(
        first_link,
        json={"value": [{"id": "first"}], "@odata.nextLink": equivalent_link},
    )

    with pytest.raises(graph.UpstreamProtocolError):
        graph.GraphClient().list_messages("access-secret", "inbox", 10)

    assert requests_mock.call_count == 2


def test_list_messages_allows_one_message_per_page_up_to_the_limit(requests_mock):
    initial_url = f"{graph.GRAPH_BASE}/me/mailFolders/inbox/messages"
    for index in range(100):
        request_url = (
            initial_url if index == 0 else f"{initial_url}?$skiptoken=page-{index}"
        )
        payload = {"value": [{"id": f"message-{index}"}]}
        if index < 99:
            payload["@odata.nextLink"] = f"{initial_url}?$skiptoken=page-{index + 1}"
        requests_mock.get(request_url, json=payload)

    result = graph.GraphClient().list_messages("access-secret", "inbox", 100)

    assert [message["id"] for message in result] == [
        f"message-{index}" for index in range(100)
    ]
    assert requests_mock.call_count == 100


def test_list_messages_stops_before_following_pages_beyond_total_byte_budget():
    session = PagingSession(
        page_size=graph.GRAPH_RESPONSE_MAX_BYTES,
        page_count=100,
    )

    with pytest.raises(graph.UpstreamProtocolError) as captured:
        graph.GraphClient(session=session).list_messages("access-secret", "inbox", 100)

    assert len(session.calls) == 4
    assert "skiptoken" not in str(captured.value)
    assert all(call[1]["stream"] is True for call in session.calls)


def test_list_messages_accepts_pages_equal_to_the_total_byte_budget():
    session = PagingSession(
        page_size=graph.GRAPH_RESPONSE_MAX_BYTES,
        page_count=4,
    )

    result = graph.GraphClient(session=session).list_messages(
        "access-secret", "inbox", 100
    )

    assert [message["id"] for message in result] == [
        f"message-{index}" for index in range(4)
    ]
    assert len(session.calls) == 4
