from types import SimpleNamespace

from app import _USERGROUP_MEMBERS_CACHE, _send_easter_egg_if_triggered


class FakeClient:
    def __init__(self, members):
        self.members = members
        self.messages = []

    def usergroups_users_list(self, *, usergroup):
        return {"users": self.members}

    def files_upload_v2(self, **upload):
        self.messages.append(upload)


def test_easter_egg_posts_inline_image_for_individual_group_member_mention():
    _USERGROUP_MEMBERS_CACHE.clear()
    client = FakeClient(["UMEMBER"])
    settings = SimpleNamespace(
        easter_egg_usergroup_id="S_GROUP",
    )

    sent = _send_easter_egg_if_triggered(
        {"channel": "C123", "text": "Hey <@UMEMBER>"}, client, settings
    )

    assert sent is True
    assert len(client.messages) == 1
    upload = client.messages[0]
    assert upload["channel"] == "C123"
    assert upload["file"].endswith("/image.png")
    assert upload["filename"] == "image.png"
    assert upload["alt_txt"]


def test_easter_egg_does_not_trigger_for_group_mention_or_nonmember():
    _USERGROUP_MEMBERS_CACHE.clear()
    client = FakeClient(["UMEMBER"])
    settings = SimpleNamespace(
        easter_egg_usergroup_id="S_GROUP",
    )

    assert not _send_easter_egg_if_triggered(
        {"channel": "C123", "text": "<!subteam^S_GROUP>"}, client, settings
    )
    assert not _send_easter_egg_if_triggered(
        {"channel": "C123", "text": "Hey <@U_OTHER>"}, client, settings
    )
    assert client.messages == []


def test_easter_egg_keeps_thread_context():
    _USERGROUP_MEMBERS_CACHE.clear()
    client = FakeClient(["UMEMBER"])
    settings = SimpleNamespace(
        easter_egg_usergroup_id="S_GROUP",
    )

    _send_easter_egg_if_triggered(
        {"channel": "C123", "thread_ts": "123.456", "text": "<@UMEMBER>"},
        client,
        settings,
    )

    assert client.messages[0]["thread_ts"] == "123.456"