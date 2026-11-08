"""`/tiiny` in the agent TUI: device status and model management.

``DeviceCommands.run(line)`` returns Markdown for the log. It asks through a
``LoaderUI`` (the TUI's modal) before anything disruptive: unloading models,
unloading the agent's own model, restarting or shutting down the device.
"""

from __future__ import annotations

from collections.abc import Callable

from tai.client import TiinyClient
from tai.loader import LoaderUI, ModelLoader
from tai.models import Model, build_catalog, resolve

GB = 1024 ** 3
MUTATING = {"load", "unload", "restart", "shutdown"}  # only while the agent is idle

HELP = """**/tiiny** — manage the device
- `/tiiny` or `/tiiny status` — NPU, loaded models, CPU / memory / disk
- `/tiiny models` — every installed model and whether it is loaded
- `/tiiny load <model>` — load a model (asks before unloading others)
- `/tiiny unload <model|all>` — unload model(s)
- `/tiiny restart` · `/tiiny shutdown` — reboot or power off the device (asks first)"""


def _pct(used: float, total: float) -> str:
    return f"{used / total:.0%}" if total else "?"


class DeviceCommands:
    def __init__(self, client: TiinyClient, ui: LoaderUI, agent_model: str | None = None):
        self.client, self.ui, self.agent_model = client, ui, agent_model
        self.commands: dict[str, Callable[[str], str]] = {
            "": self.status, "status": self.status, "models": self.models, "load": self.load,
            "unload": self.unload, "restart": self.restart, "shutdown": self.shutdown, "help": self.help,
        }

    @staticmethod
    def split(line: str) -> tuple[str, str]:
        sub, _, arg = line.strip().partition(" ")
        return sub.lower(), arg.strip()

    def run(self, line: str) -> str:
        sub, arg = self.split(line)
        handler = self.commands.get(sub)
        if handler is None:
            return f"Unknown `/tiiny {sub}`.\n\n{HELP}"
        return handler(arg)

    def catalog(self) -> list[Model]:
        return build_catalog(self.client.models(), self.client.running())

    def _mark(self, m: Model) -> str:
        return " *(agent)*" if m.id == self.agent_model else ""

    # -- read-only -------------------------------------------------------
    def help(self, _arg: str = "") -> str:
        return HELP

    def status(self, _arg: str = "") -> str:
        npu, sys = self.client.npu(), self.client.sys_status()
        info = self.client.device_info()
        loaded = [m for m in self.catalog() if m.loaded]
        mem, disk = sys.get("memory", {}), sys.get("disk", {})
        cores = sys.get("cpu", {}).get("per_core_percent") or [0]
        lines = [
            f"**{info.get('device_name', 'Tiiny')}** ({info.get('device_model_name', '')}, "
            f"TiinyOS {info.get('tiiny_os', '?')})",
            f"- NPU: {npu.get('npu_used')}% used, {npu.get('npu_available')}% free",
            f"- Loaded: {', '.join(f'{m.name} ({m.npu}%){self._mark(m)}' for m in loaded) or 'nothing'}",
            f"- CPU: {sum(cores) / len(cores):.0f}% avg · Memory: {_pct(mem.get('used_bytes', 0), mem.get('total_bytes', 0))}"
            f" of {mem.get('total_bytes', 0) / GB:.0f} GB · Disk: {disk.get('used_bytes', 0) / GB:.0f}"
            f" of {disk.get('total_bytes', 0) / GB:.0f} GB",
        ]
        return "\n".join(lines)

    def models(self, _arg: str = "") -> str:
        rows = ["| | Model | Task | NPU |", "|---|---|---|---|"]
        for m in sorted(self.catalog(), key=lambda m: (m.task, m.name)):
            rows.append(f"| {'●' if m.loaded else '○'} | {m.name}{self._mark(m)} | {m.task} | {m.npu}% |")
        return "\n".join(rows)

    # -- changes ---------------------------------------------------------
    def load(self, arg: str) -> str:
        if not arg:
            return "Usage: `/tiiny load <model>`"
        model = resolve(self.catalog(), arg)
        if model.loaded:
            return f"{model.name} is already loaded."
        ModelLoader(self.client, self.ui, protect=self.agent_model).ensure_loaded(model)
        return f"Loaded **{model.name}**."

    def unload(self, arg: str) -> str:
        if not arg:
            return "Usage: `/tiiny unload <model|all>`"
        targets = self._unload_targets(arg)
        if not targets:
            return "Nothing to unload."
        if not self.ui.confirm(self._unload_question(targets)):
            return "Cancelled."
        ModelLoader(self.client, self.ui).unload(targets)
        return f"Unloaded {', '.join(m.name for m in targets)}."

    def _unload_targets(self, arg: str) -> list[Model]:
        models = self.catalog()
        chosen = models if arg == "all" else [resolve(models, arg)]
        return [m for m in chosen if m.loaded]

    def _unload_question(self, targets: list[Model]) -> str:
        names = ", ".join(m.name + (" (the agent's model)" if m.id == self.agent_model else "") for m in targets)
        if any(m.id == self.agent_model for m in targets):
            return f"Unload {names}? The agent can't answer until its model is loaded again."
        return f"Unload {names}?"

    def restart(self, _arg: str = "") -> str:
        if not self.ui.confirm("Restart the Tiiny? All models are unloaded and the agent is offline "
                               "until the device is back (about a minute)."):
            return "Cancelled."
        self.client.reboot()
        return "Restarting the Tiiny…"

    def shutdown(self, _arg: str = "") -> str:
        if not self.ui.confirm("Shut down the Tiiny? It powers off; you'll need to switch it on by hand."):
            return "Cancelled."
        self.client.shutdown()
        return "Shutting down the Tiiny."
