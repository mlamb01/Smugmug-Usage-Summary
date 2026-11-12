"""Unit tests for smugmug_usage.py.

No test touches the network: SmugMug responses are faked with small
stub objects, and time.sleep is patched out so retry tests run instantly.
"""

import csv
import json
from email.utils import format_datetime
from datetime import datetime, timedelta, timezone

import pytest
import requests

import smugmug_usage as su


# ------------------------------------------------------------ test doubles
class FakeResponse:
    def __init__(self, status=200, payload=None, headers=None, text=""):
        self.status_code = status
        self._payload = payload
        self.headers = headers or {}
        self.text = text or json.dumps(payload or {})

    def json(self):
        if self._payload is None:
            raise ValueError("no JSON")
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


class FakeSession:
    """Returns queued responses in order and records every URL requested."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append((url, params))
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def ok(response_body):
    return FakeResponse(200, {"Response": response_body})


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(su.time, "sleep", lambda s: None)


# ------------------------------------------------------------ human()
@pytest.mark.parametrize("n, expected", [
    (0, "0.00 B"),
    (1023, "1,023.00 B"),
    (1024, "1.00 KB"),
    (1536, "1.50 KB"),
    (1024 ** 2, "1.00 MB"),
    (5 * 1024 ** 3, "5.00 GB"),
    (2 * 1024 ** 4, "2.00 TB"),
    (3 * 1024 ** 5, "3,072.00 TB"),   # TB is the largest unit
])
def test_human_formats_sizes(n, expected):
    assert su.human(n) == expected


# ------------------------------------------------------------ credentials
def test_credentials_from_environment():
    env = {"SMUGMUG_API_KEY": ' "abc" ', "SMUGMUG_API_SECRET": "xyz"}
    assert su.get_credentials(env) == ("abc", "xyz")


@pytest.mark.parametrize("env", [
    {},
    {"SMUGMUG_API_KEY": "abc"},
    {"SMUGMUG_API_KEY": "abc", "SMUGMUG_API_SECRET": "   "},
])
def test_missing_credentials_return_none(env):
    assert su.get_credentials(env) is None


def test_no_credentials_are_hardcoded():
    assert not hasattr(su, "API_KEY")
    assert not hasattr(su, "API_SECRET")


# ------------------------------------------------------------ retry wait
def test_retry_after_seconds_is_used():
    assert su._retry_wait(FakeResponse(429, headers={"Retry-After": "7"}), 0) == 7.0


def test_retry_after_is_capped_at_120():
    assert su._retry_wait(FakeResponse(429, headers={"Retry-After": "999"}), 0) == 120.0


def test_retry_after_http_date():
    when = datetime.now(timezone.utc) + timedelta(seconds=30)
    resp = FakeResponse(503, headers={"Retry-After": format_datetime(when, usegmt=True)})
    assert 25 <= su._retry_wait(resp, 0) <= 30


@pytest.mark.parametrize("attempt, expected", [(0, 1), (1, 2), (3, 8), (10, 60)])
def test_exponential_backoff_without_header(attempt, expected):
    assert su._retry_wait(None, attempt) == expected


# ------------------------------------------------------------ api_get()
def test_api_get_returns_response_object():
    sess = FakeSession(ok({"User": {"NickName": "mike"}}))
    assert su.api_get(sess, su.API_BASE + "!authuser") == {"User": {"NickName": "mike"}}


def test_api_get_prefixes_relative_urls():
    sess = FakeSession(ok({}))
    su.api_get(sess, "/api/v2/album/abc")
    assert sess.calls[0][0] == "https://api.smugmug.com/api/v2/album/abc"


def test_api_get_retries_rate_limit_and_server_errors():
    sess = FakeSession(FakeResponse(429), FakeResponse(503), ok({"done": True}))
    assert su.api_get(sess, "/x") == {"done": True}
    assert len(sess.calls) == 3


def test_api_get_retries_network_errors():
    sess = FakeSession(requests.ConnectionError("down"), ok({"done": True}))
    assert su.api_get(sess, "/x") == {"done": True}


def test_api_get_raises_auth_error_on_401():
    with pytest.raises(su.AuthError):
        su.api_get(FakeSession(FakeResponse(401)), "/x")


def test_api_get_raises_http_error_on_404_without_retrying():
    sess = FakeSession(FakeResponse(404))
    with pytest.raises(requests.HTTPError):
        su.api_get(sess, "/x")
    assert len(sess.calls) == 1


def test_api_get_gives_up_after_max_retries():
    sess = FakeSession(*[FakeResponse(500) for _ in range(su.MAX_RETRIES)])
    with pytest.raises(RuntimeError, match="Gave up"):
        su.api_get(sess, "/x")
    assert len(sess.calls) == su.MAX_RETRIES


def test_api_get_rejects_unexpected_json():
    with pytest.raises(RuntimeError, match="Unexpected response"):
        su.api_get(FakeSession(FakeResponse(200, {"NotResponse": 1})), "/x")


# ------------------------------------------------------------ paged()
def test_paged_follows_next_page():
    sess = FakeSession(
        ok({"Album": [{"n": 1}, {"n": 2}], "Pages": {"NextPage": "/p2"}}),
        ok({"Album": [{"n": 3}], "Pages": {}}),
    )
    items = list(su.paged(sess, "/p1", "Album", {"_filter": "Name"}))
    assert [i["n"] for i in items] == [1, 2, 3]
    assert sess.calls[0][1] == {"_filter": "Name", "start": 1, "count": su.PAGE_SIZE}
    assert sess.calls[1] == ("https://api.smugmug.com/p2", None)


def test_paged_stops_when_a_page_repeats():
    sess = FakeSession(
        ok({"Album": [{"n": 1}], "Pages": {"NextPage": "/p2"}}),
        ok({"Album": [{"n": 2}], "Pages": {"NextPage": "/p2"}}),
    )
    assert len(list(su.paged(sess, "/p1", "Album", {}))) == 2
    assert len(sess.calls) == 2


def test_paged_handles_empty_album():
    sess = FakeSession(ok({"Pages": {}}))
    assert list(su.paged(sess, "/p1", "AlbumImage", {})) == []


# ------------------------------------------------------------ video_size()
def test_video_size_uses_largest_video_uri():
    sess = FakeSession(ok({"LargestVideo": {"Size": 5000}}))
    img = {"Uris": {"LargestVideo": {"Uri": "/api/v2/image/K-0!largestvideo"}}}
    assert su.video_size(sess, img) == 5000


def test_video_size_builds_uri_from_image_key():
    sess = FakeSession(ok({"LargestVideo": {"Size": 10}}))
    su.video_size(sess, {"ImageKey": "abc", "Serial": 2})
    assert sess.calls[0][0].endswith("/api/v2/image/abc-2!largestvideo")


@pytest.mark.parametrize("body", [{"LargestVideo": {}}, {"LargestVideo": {"Size": 0}}, {}])
def test_video_size_none_when_not_reported(body):
    assert su.video_size(FakeSession(ok(body)), {"ImageKey": "k"}) is None


def test_video_size_none_on_http_error():
    assert su.video_size(FakeSession(FakeResponse(404)), {"ImageKey": "k"}) is None


# ------------------------------------------------------------ tally_album()
def test_tally_counts_photos_and_videos():
    totals = su.Totals()
    images = [
        {"ImageKey": "a", "ArchivedSize": 100},
        {"ImageKey": "b", "ArchivedSize": 200},
        {"ImageKey": "v", "IsVideo": True, "ArchivedSize": 5},  # poster JPG, ignored
    ]
    result = su.tally_album(images, "Album", totals, lambda img: 1000)
    assert result == (1300, 3)
    assert (totals.photos, totals.photo_bytes) == (2, 300)
    assert (totals.videos, totals.video_bytes) == (1, 1000)


def test_tally_skips_images_already_counted_in_another_album():
    totals = su.Totals()
    su.tally_album([{"ImageKey": "a", "ArchivedSize": 100}], "One", totals, None)
    result = su.tally_album([{"ImageKey": "a", "ArchivedSize": 100}], "Two", totals, None)
    assert result == (0, 0)
    assert totals.dupes == 1
    assert totals.photo_bytes == 100


@pytest.mark.parametrize("size", [None, 0, -1, "big"])
def test_tally_counts_missing_photo_size_as_unknown(size):
    totals = su.Totals()
    assert su.tally_album([{"ImageKey": "a", "ArchivedSize": size}], "A", totals, None) == (0, 0)
    assert totals.unknown == 1


def test_tally_records_videos_with_no_size():
    totals = su.Totals()
    su.tally_album([{"ImageKey": "v", "IsVideo": True, "FileName": "clip.mpg"}],
                   "Recital", totals, lambda img: None)
    assert totals.unknown == 1
    assert totals.no_size_videos == ["Recital/clip.mpg"]


# ------------------------------------------------------------ write_csv()
def test_write_csv_writes_utf8_bom_with_header(tmp_path):
    out = tmp_path / "usage.csv"
    path = su.write_csv([["/Café", 2, 2, 300, "300.00 B", ""]], base=str(out))
    assert path == str(out)
    raw = out.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")
    rows = list(csv.reader(out.read_text(encoding="utf-8-sig").splitlines()))
    assert rows[0] == ["Album", "Files counted", "Album ImageCount", "Bytes", "Size", "Note"]
    assert rows[1][0] == "/Café"


def test_write_csv_falls_back_when_file_is_locked(tmp_path, monkeypatch):
    out = str(tmp_path / "usage.csv")
    real_open = open

    def locked_open(path, *args, **kwargs):
        if path == out:
            raise PermissionError("open in Excel")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", locked_open)
    path = su.write_csv([], base=out)
    assert path != out
    assert path.startswith(str(tmp_path / "usage_"))


# ------------------------------------------------------------ saved tokens
def test_load_saved_tokens_missing_file(tmp_path, monkeypatch):
    monkeypatch.setattr(su, "TOKEN_FILE", tmp_path / "none.json")
    assert su.load_saved_tokens() is None


def test_load_saved_tokens_reads_file(tmp_path, monkeypatch):
    f = tmp_path / "tokens.json"
    f.write_text(json.dumps({"token": "t", "secret": "s"}), encoding="utf-8")
    monkeypatch.setattr(su, "TOKEN_FILE", f)
    assert su.load_saved_tokens() == ("t", "s")


@pytest.mark.parametrize("content", ["not json", "{}", "[]"])
def test_load_saved_tokens_unreadable_file(tmp_path, monkeypatch, content):
    f = tmp_path / "tokens.json"
    f.write_text(content, encoding="utf-8")
    monkeypatch.setattr(su, "TOKEN_FILE", f)
    assert su.load_saved_tokens() is None
