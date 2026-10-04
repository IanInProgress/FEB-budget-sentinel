import app
from app import _send_easter_egg_if_triggered


class FakeClient:
    def __init__(self):
        self.messages = []

    def files_upload_v2(self, **upload):
        self.messages.append(upload)


def _map(monkeypatch):
    monkeypatch.setattr(app, "EASTER_EGG_IMAGES", {"UALICE": "image.png", "UBOB": "images/bob.png"})


def test_posts_member_specific_image(monkeypatch):
    _map(monkeypatch)
    client = FakeClient()
    assert _send_easter_egg_if_triggered(
        {"channel": "C123", "ts": "111.222", "text": "Hey <@UBOB>"}, client
    )
    upload = client.messages[0]
    assert upload["channel"] == "C123"
    assert upload["thread_ts"] == "111.222"
    assert upload["file"].endswith("/images/bob.png")
    assert upload["filename"] == "bob.png"
    assert upload["title"] == "Bob"


def test_one_image_per_distinct_mapped_member(monkeypatch):
    _map(monkeypatch)
    client = FakeClient()
    _send_easter_egg_if_triggered(
        {"channel": "C1", "ts": "1.2", "text": "<@UALICE> <@UBOB> <@UALICE> <@UOTHER>"}, client
    )
    assert [m["filename"] for m in client.messages] == ["image.png", "bob.png"]
    assert [m["title"] for m in client.messages] == ["Image", "Bob"]


def test_unmapped_or_group_mention_does_nothing(monkeypatch):
    _map(monkeypatch)
    client = FakeClient()
    assert not _send_easter_egg_if_triggered({"channel": "C123", "ts": "1.2", "text": "<@UOTHER>"}, client)
    assert not _send_easter_egg_if_triggered({"channel": "C123", "ts": "1.2", "text": "<!subteam^S1>"}, client)
    assert client.messages == []


def test_keeps_thread_context(monkeypatch):
    _map(monkeypatch)
    client = FakeClient()
    _send_easter_egg_if_triggered(
        {"channel": "C123", "thread_ts": "123.456", "ts": "123.789", "text": "<@UALICE>"}, client
    )
    assert client.messages[0]["thread_ts"] == "123.456"
