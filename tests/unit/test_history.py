"""Forced MAM resync for one chat."""

from types import SimpleNamespace

from ward.core.history import NS_MAM, HistoryService, Page


class FakeSession:
    def __init__(self):
        self.account = SimpleNamespace(id=1)
        self.jid = "alice@example.org"
        self.account_features = {NS_MAM}


class FakeE2E:
    def __init__(self):
        self.states = []

    def history_sync(self, _session, on):
        self.states.append(on)


class FakeCore:
    def __init__(self):
        self.e2e = FakeE2E()


def test_resync_chat_walks_backwards_from_newest(run):
    async def scenario():
        service = HistoryService(FakeCore())
        session = FakeSession()
        chat = SimpleNamespace(type="chat", jid="bob@example.org")
        calls = []
        pages = [Page(False, "first1", "last1", 100), Page(True, "", "", 1)]

        async def page(_session, archive, mode, **kwargs):
            calls.append((archive, mode, kwargs))
            return pages.pop(0)

        service._page = page
        assert await service.resync_chat(session, chat) == 2
        assert calls[0][0] is None and calls[0][1] == "scrollback"
        assert calls[0][2]["with_"] == "bob@example.org"
        assert calls[0][2]["before"] == ""
        assert calls[1][2]["before"] == "first1"
        assert service.core.e2e.states == [True, False]

    run(scenario(), timeout=5)
