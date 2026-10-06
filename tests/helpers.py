"""Shared Telegram messages and clients for offline tests."""
from datetime import datetime, timezone
from telethon.tl.types import Channel, ChatPhotoEmpty, Message, PeerChannel


def dt(year=2026, day=1):
    return datetime(year, 1, day, tzinfo=timezone.utc)


def channel(cid=1):
    return Channel(id=cid, title=f"Channel {cid}", photo=ChatPhotoEmpty(),
                   date=dt(), broadcast=True)


def message(mid=1, text="TCMB politika faizini sabit tuttu", date=None, cid=1, edit=None):
    return Message(id=mid, peer_id=PeerChannel(cid), date=date or dt(),
                   message=text, edit_date=edit, views=1)


class FakeClient:
    def __init__(self, messages=()):
        self.messages = list(messages)
        self.fail_after = None

    async def get_messages(self, *args, **kwargs):
        return sorted(self.messages, key=lambda m: m.id)[:1]

    async def iter_messages(self, entity, **kwargs):
        rows = sorted(self.messages, key=lambda m: m.id, reverse=not kwargs.get("reverse", False))
        yielded = 0
        for msg in rows:
            if msg.id <= kwargs.get("min_id", 0):
                continue
            if kwargs.get("offset_id") and msg.id >= kwargs["offset_id"]:
                continue
            if self.fail_after is not None and yielded == self.fail_after:
                raise ConnectionError("simulated interruption")
            yielded += 1
            yield msg


