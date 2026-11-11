import pytest

from tai.models import ResolveError, build_catalog, plan_swap, resolve


def api(id, type, npu, status="downloaded", input="Text", **kw):
    return {"id": id, "display_name": id.split("/")[-1], "type": type, "npu_usage": npu,
            "status": status, "input": input, **kw}


MODELS = [
    api("Qwen/Qwen3-Coder-Next", "Text Generation", 94),
    api("Qwen/Qwen3.6-35B-A3B", "Image-Text-to-Text", 47, "running", "Text,Image,Video"),
    api("Tongyi-MAI/Z-Image-Turbo", "Text-to-Image", 23, "running"),
    api("k2-fsa/OmniVoice", "Text-to-Speech", 2, "running"),
    api("Qwen/Qwen3-ASR-1.7B", "ASR", 7),
    api("Qwen/Qwen3-Embedding-0.6B", "Text Embedding", 1, "running"),
]
RUNNING = {"instances": {"running": [
    {"model_id": "Qwen/Qwen3.6-35B-A3B", "status": "running", "use_time": 300, "active_request_count": 0},
    {"model_id": "Tongyi-MAI/Z-Image-Turbo", "status": "running", "use_time": 100, "active_request_count": 0},
    {"model_id": "k2-fsa/OmniVoice", "status": "running", "use_time": 200, "active_request_count": 0},
    {"model_id": "Qwen/Qwen3-Embedding-0.6B", "status": "running", "use_time": 50, "active_request_count": 0},
]}}


@pytest.fixture
def cat():
    return build_catalog(MODELS, RUNNING)


def test_resolve_by_id_name_and_substring(cat):
    assert resolve(cat, "Qwen/Qwen3-Coder-Next").name == "Qwen3-Coder-Next"
    assert resolve(cat, "omnivoice").id == "k2-fsa/OmniVoice"
    assert resolve(cat, "z-image").id == "Tongyi-MAI/Z-Image-Turbo"


def test_resolve_ambiguous_and_unknown(cat):
    with pytest.raises(ResolveError, match="ambiguous"):
        resolve(cat, "qwen3")
    with pytest.raises(ResolveError, match="No model matches"):
        resolve(cat, "llama")


def test_resolve_rejects_wrong_task(cat):
    with pytest.raises(ResolveError, match="not usable for 'image'"):
        resolve(cat, "coder", task="image")


def test_default_prefers_loaded_then_cheapest(cat):
    assert resolve(cat, None, "chat").id == "Qwen/Qwen3.6-35B-A3B"  # loaded
    assert resolve(cat, None, "asr").id == "Qwen/Qwen3-ASR-1.7B"  # only option
    assert resolve(cat, None, "chat", default="Qwen/Qwen3-Coder-Next").name == "Qwen3-Coder-Next"


def test_vision_flag(cat):
    assert resolve(cat, "qwen3.6").vision
    assert not resolve(cat, "coder").vision


def test_no_swap_when_it_fits(cat):
    plan = plan_swap(cat, resolve(cat, "asr"))  # 73 used, 7 needed
    assert plan.unload == [] and plan.free_before == 27


def test_swap_unloads_the_least_npu_that_makes_room(cat):
    plan = plan_swap(cat, resolve(cat, "coder"))  # needs 94, 27 free -> free 67 more
    # Options: qwen3.6+z-image = 70, all four = 73. Keep the small ones loaded.
    assert {m.name for m in plan.unload} == {"Qwen3.6-35B-A3B", "Z-Image-Turbo"}


def loaded(*specs):
    """specs: (name, npu, last_used); plus an unloaded 'Target' needing the given npu."""
    models = [api(f"a/{n}", "ASR", npu, "running") for n, npu, _ in specs]
    running = {"instances": {"running": [
        {"model_id": f"a/{n}", "status": "running", "use_time": used} for n, _, used in specs]}}
    return build_catalog(models, running)


def target(npu):
    return build_catalog([api("a/Target", "ASR", npu)])[0]


def test_small_model_is_unloaded_instead_of_a_big_older_one():
    # The real case: 5% free, TRELLIS needs 6. The coder (94) is older, but the embedding (1) suffices.
    cat = loaded(("Coder", 94, 1), ("Embed", 1, 2))
    assert [m.name for m in plan_swap(cat + [target(6)], target(6)).unload] == ["Embed"]


def test_one_medium_model_beats_many_small_ones():
    cat = loaded(("A", 10, 1), ("B", 10, 2), ("C", 10, 3), ("M", 25, 4))  # 45 free, need 70 -> free 25
    assert [m.name for m in plan_swap(cat + [target(70)], target(70)).unload] == ["M"]


def test_tie_prefers_fewer_models_then_least_recently_used():
    cat = loaded(("Old", 20, 1), ("New", 20, 2), ("X", 10, 3), ("Y", 10, 4))  # 40 free, need 60
    assert [m.name for m in plan_swap(cat + [target(60)], target(60)).unload] == ["Old"]


def test_swap_takes_everything_when_needed():
    cat = loaded(("Small", 10, 1), ("Big", 80, 2))  # 10 free, need 94
    assert {m.name for m in plan_swap(cat + [target(94)], target(94)).unload} == {"Small", "Big"}


def test_swap_never_unloads_busy_models(cat):
    for m in cat:
        if m.name == "Qwen3.6-35B-A3B":
            m.active = 1
    with pytest.raises(ResolveError, match="busy models: Qwen3.6-35B-A3B"):
        plan_swap(cat, resolve(cat, "coder"))


def test_greedy_fallback_for_many_models(monkeypatch):
    import tai.models
    monkeypatch.setattr(tai.models, "MAX_EXACT", 1)
    cat = loaded(("A", 30, 1), ("B", 5, 2), ("C", 10, 3))  # 55 free, need 70 -> smallest first: B, C
    assert [m.name for m in plan_swap(cat + [target(70)], target(70)).unload] == ["B", "C"]


def test_short_tokens():
    from tai.models import short_tokens
    assert [short_tokens(n) for n in (900, 1234, 8192, 131072)] == ["900", "1.2K", "8.2K", "131K"]
