import asyncio

import httpx

from dcua.backends.fake import FakeBackend
from dcua.decisions import DecisionClient
from dcua.grounding import Grounder, parse_element_ref
from dcua.keys import is_risky_chord, parse_chord
from dcua.observe import IdRegistry, diff, render_outline

from sim import SimDecisions


def snap_of(backend, reg=None):
    reg = reg or IdRegistry()
    return reg.assign(backend.snapshot())


def test_ids_are_stable_across_snapshots():
    b = FakeBackend()
    reg = IdRegistry()
    s1 = snap_of(b, reg)
    save_id = next(e.id for e in s1.elements if e.name == "Save")
    b._fire("open_save")  # dialog appears, editor elements leave the foreground
    b._fire("cancel")
    s2 = snap_of(b, reg)
    assert next(e.id for e in s2.elements if e.name == "Save") == save_id


def test_single_round_grounding_picks_matching_element():
    sim = SimDecisions()
    b = FakeBackend()
    snap = snap_of(b)

    async def go():
        async with DecisionClient("k", transport=httpx.MockTransport(sim.handler)) as c:
            return await Grounder(c, max_options=20).ground("the Delete all button", "click", snap)

    g = asyncio.run(go())
    assert g.status == "ok" and g.element.name == "Delete all" and g.requests == 1
    q = sim.requests[0]["questions"]["target"]
    assert "none" in q["criteria"] and len(q["criteria"]) == len(snap.elements) + 1


def test_tournament_over_many_candidates():
    sim = SimDecisions()
    b = FakeBackend(extra_buttons=55)  # 60 elements -> 3 chunks of 20 + final
    snap = snap_of(b)

    async def go():
        async with DecisionClient("k", transport=httpx.MockTransport(sim.handler)) as c:
            return await Grounder(c, max_options=20).ground("Tool 42", "click", snap)

    g = asyncio.run(go())
    assert g.element is not None and g.element.name == "Tool 42"
    assert g.requests == 4 and len(sim.requests) == 4
    assert all(len(r["questions"]["target"]["criteria"]) <= 21 for r in sim.requests)


def test_direct_id_skips_model():
    snap = snap_of(FakeBackend())
    eid = snap.elements[0].id

    async def go():
        async with DecisionClient("k", transport=httpx.MockTransport(lambda r: 1 / 0)) as c:
            return await Grounder(c).ground(f"[{eid}]", "click", snap)

    g = asyncio.run(go())
    assert g.status == "direct" and g.element.id == eid
    assert parse_element_ref("E12") == "e12" and parse_element_ref("e12 button") is None


def test_outline_and_diff():
    b = FakeBackend()
    reg = IdRegistry()
    before = snap_of(b, reg)
    text = render_outline(before)
    assert 'Foreground window: "Untitled - Notepad"' in text and "[e" in text
    b._fire("open_save")
    after = snap_of(b, reg)
    d = diff(before, after)
    assert d["foreground_after"] == "Save As" and not d["nothing_changed"]


def test_key_chords():
    assert parse_chord("Ctrl+Shift+S") == ["ctrl", "shift", "s"]
    assert parse_chord("cmd+Return") == ["win", "enter"]
    assert parse_chord("ctrl++") == ["ctrl", "+"]
    assert is_risky_chord(parse_chord("alt+F4"))
    try:
        parse_chord("ctrl+hyperspace")
    except ValueError:
        pass
    else:
        raise AssertionError("unknown key accepted")
