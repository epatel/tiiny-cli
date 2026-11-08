"""`/tiiny` commands against a fake device with scripted confirmations."""

from contextlib import contextmanager

import pytest

from tai.agent.device import DeviceCommands
from tai.loader import LoadCancelled
from tai.models import ResolveError


def model(id, npu, **fields):
    """fields: status (default downloaded), type (default ASR), used (last-use time)."""
    return {"id": id, "display_name": id.split("/")[-1], "type": "ASR", "npu_usage": npu,
            "status": "downloaded", "used": 0, **fields}


class FakeClient:
    def __init__(self, models):
        self._models = {m["id"]: m for m in models}
        self.log: list[tuple] = []

    def models(self):
        return list(self._models.values())

    def running(self):
        return {"instances": {"running": [{"model_id": m["id"], "status": "running", "use_time": m["used"]}
                                          for m in self._models.values() if m["status"] == "running"]}}

    def npu(self):
        used = sum(m["npu_usage"] for m in self._models.values() if m["status"] == "running")
        return {"npu_total": 100, "npu_used": used, "npu_available": 100 - used}

    def sys_status(self):
        return {"cpu": {"per_core_percent": [10, 30]}, "memory": {"used_bytes": 8 * 1024**3, "total_bytes": 32 * 1024**3},
                "disk": {"used_bytes": 100 * 1024**3, "total_bytes": 900 * 1024**3}}

    def device_info(self):
        return {"device_name": "My Tiiny", "device_model_name": "Tiiny z01", "tiiny_os": "0.1.39"}

    def stop(self, model_id):
        self.log.append(("stop", model_id))
        self._models[model_id]["status"] = "downloaded"

    def launch(self, model_id):
        self.log.append(("launch", model_id))
        self._models[model_id]["status"] = "running"
        yield {"_event": "done", "progress": 100, "success": True}

    def reboot(self):
        self.log.append(("reboot",))

    def shutdown(self):
        self.log.append(("shutdown",))


class FakeUI:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.questions: list[str] = []

    def confirm(self, question):
        self.questions.append(question)
        return self.answers.pop(0)

    def info(self, message):
        pass

    @contextmanager
    def progress(self, name):
        yield lambda pct, msg: None


CODER = model("q/Coder", 94, status="running", type="Text Generation", used=5)
EMBED = model("q/Embed", 1, status="running", type="Text Embedding", used=1)
ASR = model("q/ASR", 7)


def cmds(*answers, models=None):
    client = FakeClient(models or [dict(CODER), dict(EMBED), dict(ASR)])
    ui = FakeUI(*answers)
    return DeviceCommands(client, ui, agent_model="q/Coder"), client, ui


def test_status_and_models():
    c, _, _ = cmds()
    status = c.run("")
    assert "**My Tiiny** (Tiiny z01, TiinyOS 0.1.39)" in status and "NPU: 95% used, 5% free" in status
    assert "Coder (94%) *(agent)*" in status and "CPU: 20% avg" in status and "Memory: 25% of 32 GB" in status
    table = c.run("models")
    assert "| ● | Coder *(agent)* | chat | 94% |" in table and "| ○ | ASR | asr | 7% |" in table


def test_load_that_fits_asks_nothing():
    c, client, ui = cmds(models=[dict(EMBED), dict(ASR)])
    assert c.run("load asr") == "Loaded **ASR**."
    assert client.log == [("launch", "q/ASR")] and ui.questions == []


def test_load_that_needs_a_swap_names_the_agent_model():
    c, client, ui = cmds(True)
    assert c.run("load asr") == "Loaded **ASR**."
    assert "Coder (94%) — the agent's model" in ui.questions[0]
    assert client.log == [("stop", "q/Coder"), ("launch", "q/ASR")]


def test_declined_swap_raises_cancelled_and_changes_nothing():
    c, client, _ = cmds(False)
    with pytest.raises(LoadCancelled):
        c.run("load asr")
    assert client.log == []


def test_already_loaded_and_usage():
    c, _, _ = cmds()
    assert c.run("load embed") == "Embed is already loaded."
    assert c.run("load").startswith("Usage") and c.run("unload").startswith("Usage")


def test_unload_agent_model_warns():
    c, client, ui = cmds(True)
    assert c.run("unload coder") == "Unloaded Coder."
    assert "Coder (the agent's model)" in ui.questions[0] and "can't answer" in ui.questions[0]
    assert client.log == [("stop", "q/Coder")]


def test_unload_all_and_cancel():
    c, client, _ = cmds(False)
    assert c.run("unload all") == "Cancelled." and client.log == []
    c, client, ui = cmds(True)
    assert c.run("unload all") == "Unloaded Coder, Embed."
    c, client, _ = cmds(models=[dict(ASR)])
    assert c.run("unload all") == "Nothing to unload."


def test_restart_and_shutdown_need_confirmation():
    c, client, _ = cmds(False, True, True)
    assert c.run("restart") == "Cancelled." and client.log == []
    assert c.run("restart") == "Restarting the Tiiny…" and client.log == [("reboot",)]
    assert c.run("shutdown") == "Shutting down the Tiiny." and client.log[-1] == ("shutdown",)


def test_unknown_and_bad_model():
    c, _, _ = cmds()
    assert c.run("dance").startswith("Unknown `/tiiny dance`") and "/tiiny models" in c.run("help")
    with pytest.raises(ResolveError):
        c.run("load llama")
