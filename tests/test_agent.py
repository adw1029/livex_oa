"""Agent loop + confirmation gate, with a scripted LLM and the real MCP server over a fake cal.com."""

from __future__ import annotations

import pytest
from mcp import Client

from app import sessions
from app.agent import Agent, Session
from calcom_mcp import timeutil as tu
from calcom_mcp.server import create_server
from tests.fakes import FakeCal, ScriptedLLM, call, next_weekday, reply

pytestmark = pytest.mark.anyio

TZ = "America/Los_Angeles"


@pytest.fixture
def cal() -> FakeCal:
    return FakeCal(next_weekday(3))


@pytest.fixture
async def mcp(cal):
    async with Client(create_server(cal)) as client:
        yield client


async def make_agent(mcp, *script):
    llm = ScriptedLLM(*script)
    agent = Agent(llm, mcp)
    await agent.start()
    return agent, llm


def book_call(cal: FakeCal, hh: int = 14):
    return call(
        "create_booking",
        {"event_type_id": 1, "start": tu.to_utc_z(cal.at(hh)), "attendee_name": "Jordan Lee",
         "attendee_email": "jordan@example.com", "attendee_timezone": TZ},
        text="Here's the booking — confirm below.",
    )


def tool_results(message) -> dict[str, dict]:
    return {b["tool_use_id"]: b for b in message["content"] if isinstance(b, dict) and b.get("type") == "tool_result"}


async def test_gate_set_comes_from_annotations(mcp):
    agent, _ = await make_agent(mcp)
    assert agent.gated == {"create_booking", "reschedule_booking", "cancel_booking"}


async def test_read_only_tools_run_immediately_and_choices_become_chips(mcp, cal):
    cal.seed("Intro with Sam", cal.at(15))
    agent, llm = await make_agent(
        mcp,
        call("list_bookings", {"timezone": TZ, "date_from": cal.day.isoformat()}),
        reply("You have **Intro with Sam** at 3:00 PM.\n\n[[Move it | Cancel it]]"),
    )
    turn = await agent.chat(Session("s"), "what's on thursday?", TZ)
    assert turn.reply == "You have **Intro with Sam** at 3:00 PM."
    assert turn.suggestions == ["Move it", "Cancel it"]
    assert turn.pending_action is None
    assert "Intro with Sam" in tool_results(llm.seen[1][-1])["tu_list_bookings"]["content"]


async def test_write_waits_for_confirm_then_executes(mcp, cal):
    agent, llm = await make_agent(mcp, book_call(cal))
    session = Session("s", timezone=TZ)
    turn = await agent.chat(session, "book jordan thursday 2pm, jordan@example.com", TZ)

    assert turn.pending_action and turn.pending_action["items"][0]["action"] == "Book"
    assert "Jordan Lee" in turn.pending_action["items"][0]["detail"]
    assert not [c for c in cal.calls if c[0] == "create_booking"], "nothing may be written before Confirm"

    llm.push(reply("Booked — Jordan has the invite."))
    done = await agent.confirm(session, turn.pending_action["id"], approve=True)
    assert done.calendar_changed and done.reply.startswith("Booked")
    assert len([c for c in cal.calls if c[0] == "create_booking"]) == 1
    result = tool_results(llm.seen[-1][-1])["tu_create_booking"]
    assert not result["is_error"]
    assert agent.audit == [{"session": "s", "tool": "create_booking", "input": book_call(cal).content[1].input, "ok": True}]


async def test_pending_action_survives_save_and_load(mcp, cal):
    """On Lambda the Confirm click can reach a different instance, so the session goes through storage."""
    agent, llm = await make_agent(mcp, call("list_event_types", {}), book_call(cal))
    session = Session("s", timezone=TZ)
    turn = await agent.chat(session, "book jordan thursday 2pm", TZ)

    restored = sessions.load(sessions.dump(session))
    assert restored.event_types.keys() == session.event_types.keys()
    llm.push(reply("Booked."))
    done = await agent.confirm(restored, turn.pending_action["id"], approve=True)
    assert done.calendar_changed
    assert len([c for c in cal.calls if c[0] == "create_booking"]) == 1


async def test_decline_never_writes(mcp, cal):
    agent, llm = await make_agent(mcp, book_call(cal), reply("No problem, left it alone."))
    session = Session("s", timezone=TZ)
    turn = await agent.chat(session, "book it", TZ)
    done = await agent.confirm(session, turn.pending_action["id"], approve=False)
    assert not done.calendar_changed
    assert not [c for c in cal.calls if c[0] == "create_booking"]
    result = tool_results(llm.seen[-1][-1])["tu_create_booking"]
    assert result["is_error"] and "Cancel" in result["content"]


async def test_typed_yes_counts_as_confirm(mcp, cal):
    agent, _ = await make_agent(mcp, book_call(cal), reply("Done!"))
    session = Session("s", timezone=TZ)
    await agent.chat(session, "book it", TZ)
    done = await agent.chat(session, "yes", TZ)
    assert done.calendar_changed
    assert len([c for c in cal.calls if c[0] == "create_booking"]) == 1


async def test_changing_the_subject_closes_pending_without_writing(mcp, cal):
    agent, llm = await make_agent(mcp, book_call(cal), reply("Sure — what time instead?"))
    session = Session("s", timezone=TZ)
    await agent.chat(session, "book it", TZ)
    await agent.chat(session, "actually make it 4pm", TZ)
    assert session.pending is None
    assert not [c for c in cal.calls if c[0] == "create_booking"]
    last_user = llm.seen[-1][-1]
    assert tool_results(last_user)["tu_create_booking"]["is_error"]
    assert last_user["content"][-1] == {"type": "text", "text": "actually make it 4pm"}


async def test_stale_confirm_is_harmless(mcp, cal):
    agent, _ = await make_agent(mcp)
    turn = await agent.confirm(Session("s"), "old-id", approve=True)
    assert "expired" in turn.reply and not turn.calendar_changed


async def test_reschedule_card_names_the_meeting(mcp, cal):
    uid = cal.seed("Intro with Sam", cal.at(15))
    agent, _ = await make_agent(
        mcp,
        call("list_bookings", {"timezone": TZ, "date_from": cal.day.isoformat()}),
        call("reschedule_booking", {"booking_uid": uid, "new_start": tu.to_utc_z(cal.at(16)), "timezone": TZ}),
    )
    turn = await agent.chat(Session("s", timezone=TZ), "move my 3pm to 4", TZ)
    (item,) = turn.pending_action["items"]
    assert item["action"] == "Reschedule" and item["title"] == "Intro with Sam"
    assert "4:00 PM" in item["when"] and "3:00" in item["detail"]


async def test_every_tool_use_gets_a_result(mcp, cal):
    """The API rejects histories with an unanswered tool_use — check the transcript stays well-formed."""
    agent, llm = await make_agent(mcp, book_call(cal), reply("ok"), reply("ok"))
    session = Session("s", timezone=TZ)
    await agent.chat(session, "book it", TZ)
    await agent.chat(session, "never mind", TZ)
    await agent.chat(session, "thanks", TZ)
    msgs = session.messages
    for i, m in enumerate(msgs):
        if m["role"] == "assistant":
            ids = [b.id for b in m["content"] if b.type == "tool_use"]
            if ids:
                assert set(ids) <= set(tool_results(msgs[i + 1]))
