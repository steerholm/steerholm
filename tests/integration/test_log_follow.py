"""`holm log`, `log --follow` and `log set`, as the daemon answers them."""
import asyncio

import pytest

from steerholm import gateway as gateway_mod
from steerholm.events import DecisionEvent, now_iso
from tests.conftest import make_gateway


def _event(tool, agent="a", decision="allowed", agent_id=None):
    return DecisionEvent(ts=now_iso(), agent=agent, agent_id=agent_id, tool=tool,
                         decision=decision)


async def _next_event(stream):
    """The next decision on a follow stream, past any keep-alives."""
    while True:
        result = await asyncio.wait_for(stream.__anext__(), 5)
        if result is not None:
            return result


@pytest.fixture
def gw(config_manager):
    gateway = make_gateway(config_manager)
    gateway.event_log = gateway_mod.EventLog()      # in the test's config dir
    return gateway


@pytest.mark.asyncio
async def test_follow_gives_the_history_then_each_new_decision_once(gw):
    gw._record(_event("before1"))
    gw._record(_event("before2"))
    stream = gw._admin_log_follow("uid:0", {"number": 50})

    history = await asyncio.wait_for(stream.__anext__(), 5)
    assert [e["tool"] for e in history["events"]] == ["before1", "before2"]

    gw._record(_event("after"))
    assert (await _next_event(stream))["tool"] == "after"
    await stream.aclose()


@pytest.mark.asyncio
async def test_a_decision_recorded_while_the_history_is_read_is_not_in_both(gw):
    gw._record(_event("old"))
    stream = gw._admin_log_follow("uid:0", {"number": 50})
    first = asyncio.ensure_future(stream.__anext__())
    await asyncio.sleep(0)            # subscribed and marked; the history is being read
    gw._record(_event("during"))

    history = await asyncio.wait_for(first, 5)
    assert [e["tool"] for e in history["events"]] == ["old"]
    assert (await _next_event(stream))["tool"] == "during"
    await stream.aclose()


@pytest.mark.asyncio
async def test_follow_applies_the_filters_to_new_decisions(gw):
    stream = gw._admin_log_follow("uid:0", {"number": 50, "status": "denied"})
    await asyncio.wait_for(stream.__anext__(), 5)
    gw._record(_event("ok"))
    gw._record(_event("no", decision="denied"))
    assert (await _next_event(stream))["tool"] == "no"
    await stream.aclose()


@pytest.mark.asyncio
async def test_an_idle_follow_sends_keep_alives(gw, monkeypatch):
    monkeypatch.setattr(gateway_mod, "_KEEP_ALIVE", 0.01)
    stream = gw._admin_log_follow("uid:0", {})
    await asyncio.wait_for(stream.__anext__(), 5)
    assert await asyncio.wait_for(stream.__anext__(), 5) is None
    await stream.aclose()


@pytest.mark.asyncio
async def test_a_follower_that_falls_behind_is_ended(gw, monkeypatch):
    monkeypatch.setattr(gateway_mod, "_FOLLOW_BACKLOG", 2)
    stream = gw._admin_log_follow("uid:0", {})
    await asyncio.wait_for(stream.__anext__(), 5)
    for tool in ("one", "two", "three"):        # the third does not fit
        gw._record(_event(tool))

    assert (await _next_event(stream))["tool"] == "one"
    assert (await _next_event(stream))["tool"] == "two"
    with pytest.raises(ValueError, match="faster than they were read"):
        await _next_event(stream)


@pytest.mark.asyncio
async def test_a_closed_follow_stops_receiving(gw):
    stream = gw._admin_log_follow("uid:0", {})
    await asyncio.wait_for(stream.__anext__(), 5)
    assert len(gw._log_followers) == 1
    await stream.aclose()
    assert gw._log_followers == set()
    gw._record(_event("nobody listening"))        # no error, nothing held


@pytest.mark.asyncio
async def test_an_unknown_status_is_refused(gw):
    with pytest.raises(ValueError, match="--status must be one of"):
        await gw._admin_log("uid:0", {"status": "maybe"})
    stream = gw._admin_log_follow("uid:0", {"status": "maybe"})
    with pytest.raises(ValueError, match="--status must be one of"):
        await stream.__anext__()


@pytest.mark.asyncio
async def test_log_returns_the_last_n_and_the_ids_behind_a_name(gw):
    gw._record(_event("t1", agent="coder", agent_id="agt_1"))
    gw._record(_event("t2", agent="coder", agent_id="agt_2"))
    gw._record(_event("t3", agent="other", agent_id="agt_3"))
    history = await gw._admin_log("uid:0", {"number": 1, "agent": "coder"})
    assert [e["tool"] for e in history["events"]] == ["t2"]
    assert history["agent_ids"] == ["agt_1", "agt_2"]      # over all of it, not just the last 1


@pytest.mark.asyncio
async def test_log_set_applies_to_the_running_log(gw):
    answer = await gw._admin_log_set("uid:0", {"max_files": 3, "max_age_days": 30})
    assert answer == {"max_files": 3, "max_age_days": 30}
    assert (gw.event_log._max_files, gw.event_log._max_age_days) == (3, 30)
