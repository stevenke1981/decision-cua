"""Pac-Man demo: game rules, text state, and the Decision-1 / heuristic policies."""
import asyncio
import json

import httpx

from dcua.decisions import DecisionClient
from dcua.games.pacman import GHOST_POINTS, PELLET_POINTS, PacmanGame
from dcua.games.pacman_agent import DecisionPolicy, HeuristicPolicy, play


def ghost(game, name):
    return next(g for g in game.ghosts if g.name == name)


def client_with(handler):
    return DecisionClient("k", transport=httpx.MockTransport(handler))


def choice_response(choice, probs):
    return httpx.Response(200, json={
        "model": "microsoft/microsoft-decision-1",
        "answers": {"move": {"type": "choice", "choice": choice, "probabilities": probs,
                             "confidence": probs[choice]}},
        "usage": {"cost": 0.00002},
    })


def test_start_position_and_legal_moves():
    game = PacmanGame()
    assert game.pac == (9, 15)
    assert game.legal_moves() == ["left", "right"]
    assert len(game.ghosts) == 4 and ghost(game, "blinky").dangerous
    assert all(g.in_house for g in game.ghosts if g.name != "blinky")


def test_eating_a_pellet_scores():
    game = PacmanGame()
    game.step("left")
    assert game.pac == (8, 15) and game.score == PELLET_POINTS and "pellet" in game.events


def test_tunnel_wraps_around():
    game = PacmanGame()
    game.pac, game.pac_dir = (0, 9), "left"
    game.step("left")
    assert game.pac == (18, 9)


def test_illegal_direction_keeps_heading():
    game = PacmanGame()
    game.step("up")  # wall above the start cell
    assert game.pac == (8, 15)  # kept the initial heading (left)


def test_dangerous_ghost_costs_a_life_and_resets():
    game = PacmanGame()
    ghost(game, "blinky").pos = (8, 15)
    game.step("left")
    assert "death" in game.events and game.lives == 2
    assert game.pac == (9, 15) and ghost(game, "blinky").pos == (9, 7)


def test_power_pellet_makes_ghosts_edible():
    game = PacmanGame()
    game.pac, game.pac_dir = (2, 15), "left"
    game.step("left")
    blinky = ghost(game, "blinky")
    assert "power" in game.events and blinky.edible
    blinky.pos = (1, 14)
    score = game.score
    game.step("up")
    assert "eat-ghost:blinky" in game.events and blinky.in_house
    assert game.score >= score + GHOST_POINTS


def test_text_state_lists_the_board_and_legal_moves():
    text = PacmanGame().to_text()
    assert "P" in text and "Legal moves: left, right." in text
    assert "blinky at x=9, y=7: dangerous" in text


def test_move_info_measures_ghost_distance():
    game = PacmanGame()
    ghost(game, "blinky").pos = (6, 15)
    left, right = game.move_info("left"), game.move_info("right")
    assert left.ghost_dist == 2 and right.ghost_dist > left.ghost_dist


def test_heuristic_avoids_an_adjacent_ghost():
    game = PacmanGame()
    ghost(game, "blinky").pos = (8, 15)
    d = asyncio.run(HeuristicPolicy().decide(game))
    assert d.move == "right" and abs(sum(d.probabilities.values()) - 1) < 1e-9
    assert d.probabilities["left"] < 0.01


def test_decision_policy_offers_only_legal_moves_and_keeps_probabilities():
    seen = {}

    def handler(request):
        body = json.loads(request.content)
        seen["criteria"] = body["questions"]["move"]["criteria"]
        seen["state"] = body["state"]
        return choice_response("right", {"left": 0.34, "right": 0.66})

    async def go():
        client = client_with(handler)
        try:
            return await DecisionPolicy(client).decide(PacmanGame())
        finally:
            await client.aclose()

    d = asyncio.run(go())
    assert set(seen["criteria"]) == {"left", "right"}
    assert "eats a pellet" in seen["criteria"]["left"] and "Legal moves" in seen["state"]
    assert d.move == "right" and d.source == "decision-1" and d.p == 0.66 and d.cost == 0.00002


def test_single_exit_is_forced_without_calling_the_model():
    def handler(request):
        raise AssertionError("the model must not be called for a forced move")

    async def go():
        client = client_with(handler)
        try:
            return await DecisionPolicy(client).decide(PacmanGame(maze=("#####", "#P.##", "#####")))
        finally:
            await client.aclose()

    d = asyncio.run(go())
    assert d.move == "right" and d.source == "forced"


def test_api_error_falls_back_to_heuristic():
    async def go():
        client = client_with(lambda request: httpx.Response(400, text="bad request"))
        try:
            return await DecisionPolicy(client).decide(PacmanGame())
        finally:
            await client.aclose()

    d = asyncio.run(go())
    assert d.source == "fallback" and "400" in d.error and d.move in {"left", "right"}


def test_headless_play_runs_and_counts_model_calls():
    calls = []

    def handler(request):
        criteria = json.loads(request.content)["questions"]["move"]["criteria"]
        calls.append(1)
        first = sorted(criteria)[0]
        return choice_response(first, {k: (0.7 if k == first else 0.3 / (len(criteria) - 1)) for k in criteria})

    async def go():
        client = client_with(handler)
        try:
            return await play(PacmanGame(seed=1), DecisionPolicy(client), max_steps=40)
        finally:
            await client.aclose()

    summary = asyncio.run(go())
    assert summary.steps <= 40 and summary.model_calls == len(calls) > 0
    assert abs(summary.cost - 0.00002 * len(calls)) < 1e-9


def test_heuristic_game_is_deterministic_for_a_seed():
    a = asyncio.run(play(PacmanGame(seed=3), HeuristicPolicy(), max_steps=150))
    b = asyncio.run(play(PacmanGame(seed=3), HeuristicPolicy(), max_steps=150))
    assert (a.score, a.steps, a.lives) == (b.score, b.steps, b.lives) and a.score > 0
