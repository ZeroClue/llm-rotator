"""Public-interface tests for the optimization pipeline.

Every test drives TokenOptimizer.optimize_context() with an explicit
OptimizationConfig — no private-method probes, no flag-global
monkeypatching. Stage behavior, purity, routing, gating, degradation,
and cache eviction are all observed through the contract.
"""

import copy

import pytest

from conftest import build_optimizer


def build(rotator, **overrides):
    return build_optimizer(rotator, **overrides)


def chat_payload(messages, **extra):
    return {"model": "gpt-4o", "messages": messages, **extra}


def test_routing_noop_for_non_chat_paths(rotator):
    opt = build(rotator)
    payload = chat_payload([{"role": "user", "content": "hi"}])
    result = opt.optimize_context(payload, path="v1/embeddings", is_streaming=False)
    assert result is payload


def test_gate_noop_when_disabled(rotator):
    opt = build(rotator, enabled=False)
    payload = chat_payload([{"role": "user", "content": "hi"}])
    result = opt.optimize_context(payload, path="v1/chat/completions", is_streaming=False)
    assert result is payload


def test_dedup_collapses_consecutive_duplicates_only(rotator):
    opt = build(rotator, enable_context_cache=False)
    msgs = [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "a"},
        {"role": "assistant", "content": "a"},  # consecutive duplicate: dropped
        {"role": "user", "content": "b"},       # different text: kept
    ]
    out = opt.optimize_context(chat_payload(msgs), path="v1/chat/completions")["messages"]
    # Dedup is content-based across roles: anything repeating the last kept
    # content collapses, whatever its role.
    assert [(m["role"], m["content"]) for m in out] == [
        ("user", "q"), ("assistant", "a"), ("user", "b"),
    ]


def test_purity_input_is_never_mutated(rotator):
    opt = build(rotator, enable_context_cache=False)
    msgs = [
        {"role": "system", "content": "Be  helpful.\n\n\n\nReally."},
        {"role": "user", "content": "q\n\n\n\nwith   spaces"},
        {"role": "user", "content": "q"},  # duplicate the stage will drop
    ]
    payload = chat_payload(msgs, max_tokens=12345)
    before = copy.deepcopy(payload)

    result = opt.optimize_context(payload, path="v1/chat/completions")

    assert payload == before
    assert result is not payload
    assert result["messages"] is not payload["messages"]
    # Whitespace was cleaned in the output but not the input.
    assert result["messages"][0]["content"] != payload["messages"][0]["content"]


def test_truncation_ladder_fits_budget_and_keeps_system_and_last_user(rotator):
    opt = build(rotator, enable_context_cache=False, remove_duplicates=False,
                strip_whitespace=False, max_context_tokens=600,
                reserved_response_tokens=0)
    filler = "word " * 60  # ~60+ tokens per message
    msgs = [{"role": "system", "content": "You are helpful."}] + [
        {"role": role, "content": f"{filler} #{i}"}
        for i, role in enumerate(["user" if i % 2 == 0 else "assistant" for i in range(30)])
    ] + [{"role": "user", "content": "final question"}]

    out = opt.optimize_context(chat_payload(msgs), path="v1/chat/completions")["messages"]

    assert out[0]["role"] == "system"
    assert out[-1] == {"role": "user", "content": "final question"}
    assert opt.count_message_tokens(out) <= 600


def test_aggressive_truncation_fallback_when_verification_fails(rotator, caplog):
    opt = build(rotator, enable_context_cache=False, remove_duplicates=False,
                strip_whitespace=False, max_context_tokens=200,
                reserved_response_tokens=0)
    huge_system = "You are helpful. " * 400  # cannot be dropped, busts any budget
    msgs = [{"role": "system", "content": huge_system},
            {"role": "user", "content": "q"}]

    with caplog.at_level("ERROR", logger="rotator"):
        out = opt.optimize_context(chat_payload(msgs), path="v1/chat/completions")

    assert any("verification FAILED" in r.getMessage() for r in caplog.records)
    assert out["messages"][0]["role"] == "system"


def test_streaming_fastpath_skips_cache_and_expensive_stages(rotator, caplog):
    opt = build(rotator, enable_importance_scoring=True)
    msgs = [
        {"role": "user", "content": "hello\n\n\n\nworld"},
        {"role": "user", "content": "hello\n\n\n\nworld"},  # duplicate
    ]

    with caplog.at_level("INFO", logger="rotator"):
        out = opt.optimize_context(chat_payload(msgs), path="v1/chat/completions",
                                   is_streaming=True)["messages"]
        assert [m["content"] for m in out] == ["hello\n\nworld"]  # deduped + stripped
        assert not any("Cache hit" in r.getMessage() for r in caplog.records)

        # Identical buffered request hits the cache; the streamed one did not populate it.
        opt.optimize_context(chat_payload(msgs), path="v1/chat/completions")
        opt.optimize_context(chat_payload(msgs), path="v1/chat/completions")
        assert sum(1 for r in caplog.records if "Cache hit" in r.getMessage()) == 1


def test_error_degradation_returns_input_unoptimized(rotator, caplog, monkeypatch):
    opt = build(rotator)
    payload = chat_payload([{"role": "user", "content": "precious"}])

    def boom(*args, **kwargs):
        raise RuntimeError("cache exploded")

    monkeypatch.setattr(opt, "_hash_content", boom)
    with caplog.at_level("ERROR", logger="rotator"):
        result = opt.optimize_context(payload, path="v1/chat/completions")

    assert result is payload  # untouched, request survives
    assert any("forwarding payload unoptimized" in r.getMessage() for r in caplog.records)


def test_lru_eviction_observed_through_behavior(rotator, caplog):
    opt = build(rotator, context_cache_size=1)

    def ask(i):
        with caplog.at_level("INFO", logger="rotator"):
            opt.optimize_context(
                chat_payload([{"role": "user", "content": f"unique-{i}"}]),
                path="v1/chat/completions",
            )

    ask("a")
    ask("b")            # evicts a
    ask("b")            # Cache hit
    hits_after_b = sum(1 for r in caplog.records if "Cache hit" in r.getMessage())
    ask("a")            # was evicted: no hit possible
    hits_total = sum(1 for r in caplog.records if "Cache hit" in r.getMessage())
    assert hits_after_b == 1
    assert hits_total == 1  # re-asking evicted "a" must not register a hit


def test_max_tokens_clamped_to_fit_budget(rotator, caplog):
    opt = build(rotator, enable_context_cache=False, max_context_tokens=1000,
                reserved_response_tokens=0)

    greedy = opt.optimize_context(
        chat_payload([{"role": "user", "content": "hi"}], max_tokens=999_999),
        path="v1/chat/completions",
    )
    fitting = opt.optimize_context(
        chat_payload([{"role": "user", "content": "hi"}], max_tokens=50),
        path="v1/chat/completions",
    )

    assert 1 <= greedy["max_tokens"] < 999_999
    assert fitting["max_tokens"] == 50
    assert any("Clamped max_tokens" in r.getMessage() for r in caplog.records)


def test_importance_filter_preserves_system_and_alternation(rotator):
    opt = build(rotator, enable_context_cache=False,
                enable_importance_scoring=True, min_message_importance=0.25)
    msgs = (
        [{"role": "system", "content": "Be helpful."}]
        + [{"role": "user", "content": "q1"},
           {"role": "assistant", "content": "a1"}, {"role": "user", "content": "q2"},
           {"role": "assistant", "content": "a2"}, {"role": "user", "content": "q3"},
           {"role": "assistant", "content": "a3"}, {"role": "user", "content": "q4"},
           {"role": "assistant", "content": "a4"}]
        + [{"role": "user", "content": "final question"}]
    )
    out = opt.optimize_context(chat_payload(msgs), path="v1/chat/completions")["messages"]
    roles = [m["role"] for m in out]
    assert roles[0] == "system"
    assert roles[-1] == "user"
    assert all(a != b for a, b in zip(roles, roles[1:]))
