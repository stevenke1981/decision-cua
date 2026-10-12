import asyncio

import httpx
import pytest

from dcua.decisions import Choice, DecisionClient, DecisionError, Noul, Score

LIVE_SHAPE = {  # shape captured from OpenRouter docs (multimodal decisions guide)
    "model": "microsoft/microsoft-decision-1",
    "answers": {
        "team": {"type": "choice", "choice": "billing",
                 "probabilities": {"billing": 0.97, "engineering": 0.02, "support": 0.01}, "confidence": 0.95},
        "refund": {"type": "noul", "noul": 0.9461},
        "severity": {"type": "score", "score": 1.38,
                     "probabilities": {"0": 0.02, "1": 0.58, "2": 0.4}, "confidence": 0.58},
    },
    "usage": {"input_tokens": 421, "output_tokens": 0, "cost": 0.0000177},
    "id": "gen-dec-1", "provider": "Azure",
}
QS = {
    "team": Choice("Which team?", {"billing": "money", "engineering": "bugs", "support": None}),
    "refund": Noul("Asks for money back?", true="asks", false="does not"),
    "severity": Score("How severe?", ("low", "mid", "high")),
}


def client(handler):
    return DecisionClient("k", transport=httpx.MockTransport(handler), max_retries=2)


def test_parse_all_three_primitives_and_wire_format():
    sent = {}

    def h(req):
        sent.update(__import__("json").loads(req.content))
        assert req.headers["authorization"] == "Bearer k"
        assert req.url.path == "/api/alpha/decisions"
        return httpx.Response(200, json=LIVE_SHAPE)

    async def go():
        async with client(h) as c:
            return await c.ask("I was charged twice", QS)

    r = asyncio.run(go())
    assert sent["model"] == "microsoft/microsoft-decision-1"
    assert sent["questions"]["severity"] == {"type": "score", "instructions": "How severe?",
                                             "criteria": ["low", "mid", "high"]}
    assert sent["questions"]["refund"]["criteria"] == {"true": "asks", "false": "does not"}
    assert r.choice("team").choice == "billing" and r.choice("team").p == 0.97
    assert r.noul("refund").yes and r.noul("refund").p == pytest.approx(0.9461)
    assert r.score("severity").probabilities == [0.02, 0.58, 0.4]
    assert r.cost == pytest.approx(0.0000177)


def test_rejects_option_outside_criteria():
    bad = {**LIVE_SHAPE, "answers": {**LIVE_SHAPE["answers"],
                                     "team": {"type": "choice", "choice": "sales", "probabilities": {}}}}

    async def go():
        async with client(lambda r: httpx.Response(200, json=bad)) as c:
            await c.ask("x", QS)

    with pytest.raises(DecisionError, match="unsupported option"):
        asyncio.run(go())


def test_retries_on_429_then_succeeds():
    calls = {"n": 0}

    def h(req):
        calls["n"] += 1
        return httpx.Response(429, text="slow down") if calls["n"] == 1 else httpx.Response(200, json=LIVE_SHAPE)

    async def go():
        async with client(h) as c:
            return await c.ask("x", QS)

    asyncio.run(go())
    assert calls["n"] == 2


def test_400_is_not_retried():
    calls = {"n": 0}

    def h(req):
        calls["n"] += 1
        return httpx.Response(400, json={"error": "bad question"})

    async def go():
        async with client(h) as c:
            await c.ask("x", QS)

    with pytest.raises(DecisionError, match="HTTP 400"):
        asyncio.run(go())
    assert calls["n"] == 1


def test_missing_key_fails_only_when_used():
    c = DecisionClient("")
    with pytest.raises(DecisionError, match="OPENROUTER_API_KEY"):
        asyncio.run(c.ask("x", QS))
