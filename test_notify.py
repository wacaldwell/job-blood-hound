import notify


class _Resp:
    def __init__(self, code): self.status_code = code


def test_post_discord_sends_content():
    sent = {}

    def fake_post(url, json, timeout):
        sent["url"] = url; sent["json"] = json
        return _Resp(204)

    ok = notify.post_discord("http://hook", "hello", post_fn=fake_post)
    assert ok is True
    assert sent["url"] == "http://hook"
    assert sent["json"]["content"] == "hello"
    # Link previews are suppressed so the digest is not a wall of embed cards.
    assert sent["json"]["flags"] == notify.SUPPRESS_EMBEDS


def test_post_discord_truncates_long_text():
    sent = {}

    def fake_post(url, json, timeout):
        sent["json"] = json
        return _Resp(204)

    notify.post_discord("http://hook", "x" * 5000, post_fn=fake_post)
    assert len(sent["json"]["content"]) <= 1900


def test_post_discord_no_webhook_is_noop():
    assert notify.post_discord("", "hello", post_fn=None) is False
