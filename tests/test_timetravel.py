"""Track 3a tests: conversation time-travel — note/rewind/branch/diff,
persistence round-trip. Uses tmp paths; never touches the real ~/.jarvis.
"""
import json

import pytest

from core.timetravel import ConversationTree


@pytest.fixture()
def tree(tmp_path):
    return ConversationTree("test-session", path=tmp_path / "tree.json")


def test_note_and_chain(tree):
    tree.note_turn("user", "hello")
    tree.note_turn("assistant", "hi")
    chain = tree.current_chain()
    assert [n["role"] for n in chain] == ["user", "assistant"]
    assert chain[1]["parent"] == chain[0]["id"]


def test_rewind_is_non_destructive(tree):
    tree.note_turn("user", "q1")
    tree.note_turn("assistant", "a1")
    tree.note_turn("user", "q2")
    tree.note_turn("assistant", "a2")
    r = tree.rewind(1)
    assert r["ok"] is True
    # rewound one user-turn back to q2; old nodes still exist
    assert len(tree.nodes) == 4
    assert tree.current_chain()[-1]["text"] == "q2"
    # new turns diverge from the rewound point
    tree.note_turn("assistant", "a2-retry")
    assert len(tree.nodes) == 5


def test_rewind_past_start_fails(tree):
    tree.note_turn("user", "only")
    assert tree.rewind(5)["ok"] is False


def test_branch_and_switch(tree):
    tree.note_turn("user", "base")
    r = tree.branch("alt")
    assert r["ok"] is True and r["branch"] == "alt"
    tree.note_turn("user", "alt-question")
    # main still has just the base turn
    tree.branch("main")
    assert len(tree.current_chain()) == 1
    names = {b["name"] for b in tree.list_branches()}
    assert names == {"main", "alt"}


def test_diff_shows_divergence(tree):
    tree.note_turn("user", "shared")
    tree.note_turn("assistant", "shared-answer")
    tree.branch("b")
    tree.note_turn("user", "b-only")
    d = tree.diff("main", "b")
    assert d["ok"] is True
    assert d["common_turns"] == 2
    assert len(d["only_in_a"]) == 0
    assert len(d["only_in_b"]) == 1
    assert d["only_in_b"][0]["text"] == "b-only"


def test_diff_unknown_branch(tree):
    assert tree.diff("main", "nope")["ok"] is False


def test_persistence_round_trip(tmp_path):
    p = tmp_path / "tree.json"
    t1 = ConversationTree("s", path=p)
    t1.note_turn("user", "persist me")
    t1.branch("side")
    t2 = ConversationTree("s", path=p)
    assert len(t2.current_chain()) == 1
    assert {b["name"] for b in t2.list_branches()} == {"main", "side"}
    assert t2.current_branch == "side"


def test_recent_context(tree):
    tree.note_turn("user", "context q")
    tree.note_turn("assistant", "context a")
    ctx = tree.recent_context(10)
    assert "You: context q" in ctx and "Jade: context a" in ctx
