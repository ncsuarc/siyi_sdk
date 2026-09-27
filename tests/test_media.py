"""Local media API fixtures for the SDK's HTTP control companion."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
from unittest.mock import patch

import pytest

from siyi_sdk.exceptions import TransportError
from siyi_sdk.media import MediaClient
from siyi_sdk.models import MediaType


class Response:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def __enter__(self) -> Response:
        return self

    def __exit__(self, *args: object) -> None:
        pass

    def read(self) -> bytes:
        return json.dumps(self.payload).encode()


async def test_media_list_and_pagination_use_encoded_local_requests() -> None:
    requests: list[tuple[str, dict[str, str], float]] = []

    def urlopen(request, timeout):
        parsed = urllib.parse.urlsplit(request.full_url)
        params = dict(urllib.parse.parse_qsl(parsed.query))
        requests.append((parsed.path, params, timeout))
        assert request.get_header("Accept") == "application/json"
        if parsed.path.endswith("getdirectories"):
            data = {"directories": [{"name": "2026", "path": "SD Card/2026"}]}
        elif parsed.path.endswith("getmediacount"):
            data = {"count": 3}
        elif parsed.path.endswith("getmedialist"):
            start = int(params["start"])
            names = ["one.jpg", "two.jpg", "three.jpg"][start : start + int(params["count"])]
            data = {"list": [{"name": name, "url": f"http://camera/{name}"} for name in names]}
        else:
            raise AssertionError(parsed.path)
        return Response({"success": True, "data": data})

    with patch("siyi_sdk.media.urllib.request.urlopen", side_effect=urlopen):
        async with MediaClient("127.0.0.1", port=82, timeout=1.25) as media:
            directories = await media.list_directories()
            assert [(entry.name, entry.path) for entry in directories] == [("2026", "SD Card/2026")]
            assert await media.get_media_count(MediaType.IMAGES, directories[0].path) == 3
            files = await media.list_all_files(MediaType.IMAGES, directories[0].path, page_size=2)
            assert [entry.name for entry in files] == ["one.jpg", "two.jpg", "three.jpg"]
    assert len(requests) == 5
    assert requests[-1][1]["start"] == "2"
    assert all(timeout == 1.25 for _, _, timeout in requests)
    assert all(params.get("path", "SD Card/2026") == "SD Card/2026" for _, params, _ in requests)


@pytest.mark.parametrize(
    "failure,expected",
    [
        (urllib.error.HTTPError("http://camera", 503, "unavailable", None, None), "HTTP 503"),
        (OSError("unreachable"), "unreachable"),
    ],
)
async def test_media_http_failures_become_transport_error(failure, expected) -> None:
    with (
        patch("siyi_sdk.media.urllib.request.urlopen", side_effect=failure),
        pytest.raises(TransportError, match=expected),
    ):
        await MediaClient().list_directories()


async def test_media_api_failure_and_empty_page_stop_pagination() -> None:
    responses = iter(
        [
            Response({"success": False, "message": "card absent"}),
            Response({"success": True, "data": {"count": 3}}),
            Response({"success": True, "data": {"list": []}}),
        ]
    )
    with patch(
        "siyi_sdk.media.urllib.request.urlopen", side_effect=lambda *args, **kwargs: next(responses)
    ):
        media = MediaClient()
        with pytest.raises(TransportError, match="card absent"):
            await media.list_directories()
        assert await media.list_all_files(page_size=2) == []
