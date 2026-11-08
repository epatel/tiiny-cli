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


def test_swap_unloads_least_recently_used_first(cat):
    plan = plan_swap(cat, resolve(cat, "coder"))  # needs 94, 27 free
    # LRU order: embedding(50), z-image(100), omnivoice(200), qwen3.6(300)
    assert [m.name for m in plan.unload] == [
        "Qwen3-Embedding-0.6B", "Z-Image-Turbo", "OmniVoice", "Qwen3.6-35B-A3B"]


def test_swap_stops_as_soon_as_it_fits():
    models = MODELS[:1] + [api("a/Big", "Text-to-Image", 80, "running"), api("a/Small", "ASR", 10, "running")]
    running = {"instances": {"running": [
        {"model_id": "a/Small", "status": "running", "use_time": 1},
        {"model_id": "a/Big", "status": "running", "use_time": 2}]}}
    cat = build_catalog(models, running)
    plan = plan_swap(cat, resolve(cat, "coder"))  # 10 free; small frees 20, big frees 100
    assert [m.name for m in plan.unload] == ["Small", "Big"]


def test_swap_never_unloads_busy_models(cat):
    for m in cat:
        if m.name == "Qwen3.6-35B-A3B":
            m.active = 1
    with pytest.raises(ResolveError, match="busy models: Qwen3.6-35B-A3B"):
        plan_swap(cat, resolve(cat, "coder"))
