"""`tai import`: bring a Hugging Face model onto the device (``/api/v1/models/import*``).

Not in the device's OpenAPI; read from the TiinyOS app 1.0.3 (2026-11-21, see cards/tiinyos-app.md).
The app does it in three steps, and so does tai:

1. ``POST import/inspect {hf_url}`` → ``data.model`` (``fullname``, ``params``, ``size_bytes``, …),
   ``data.import_support`` (``supported``, ``status``, ``reason_code``, ``toolkit_id``, ``base_model_id``,
   ``required_base_model``), ``data.hf.base_model_ids``, ``data.disk_space``, ``data.error``.
2. The request (``import_request``) is filled from that: the device picks the toolkit (the converter
   for the model's architecture) and the base model; the user gives only a name, a description and
   whether reasoning is on.
3. ``POST import`` streams SSE progress (``status``, ``stage``/``stage_name``, ``progress``,
   ``success``, ``error``/``error_code``) until ``done`` with status completed.

The app's own requirements: ``.safetensors`` files, a public license that allows direct download.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

HF = "https://huggingface.co/"

# The TiinyOS app's messages for reason codes (inspect and import).
REASONS = {
    "MODEL_FILE_FORMAT_UNSUPPORTED": "Unsupported file format: the model must be in .safetensors files.",
    "CONVERTER_BASE_MODEL_MISSING": "Install the required base model first, then import again.",
    "CONVERTER_BASE_MODEL_VERSION_MISMATCH": "The installed base model is incompatible with this import: "
                                             "update the base model and try again.",
    "MODEL_NOT_SUPPORTED": "This model cannot be imported.",
    "MODEL_ARCHITECTURE_UNSUPPORTED": "This model's architecture is not supported yet.",
    "IMPORT_INSUFFICIENT_DISK_SPACE": "Not enough disk space on the device.",
    "MODEL_ID_CONFLICT": "A model with this name is already on the device: pick another --name.",
    "HF_AUTH_FAILED": "Hugging Face refused access: the model must be public with a license that allows download.",
    "HF_MODEL_NOT_FOUND": "No such model on Hugging Face.",
    "HF_NETWORK_UNAVAILABLE": "The device can't reach Hugging Face: check its internet connection.",
    "HF_METADATA_UNAVAILABLE": "The device couldn't read the model's details from Hugging Face.",
    "HF_DOWNLOAD_FAILED": "Downloading from Hugging Face failed.",
    "TOOLKIT_NOT_FOUND": "No converter on the device fits this model.",
    "TOOLKIT_AMBIGUOUS": "More than one converter fits this model.",
    "TOOLKIT_DOWNLOAD_FAILED": "Downloading the converter failed.",
    "TOOLKIT_RUNTIME_DOWNLOAD_FAILED": "Downloading the converter failed.",
    "TOOLKIT_EXECUTION_FAILED": "Converting the model failed.",
    "INVALID_HF_URL": "Not a Hugging Face model URL.",
    "INVALID_MODEL_ID": "Invalid model name.",
}
_FAILED = {"failed", "error", "cancelled", "cancel_request"}


@dataclass(frozen=True)
class HfModel:
    """A Hugging Face model, given as ``org/name`` or its URL."""
    url: str

    @classmethod
    def parse(cls, text: str) -> HfModel:
        text = text.strip().rstrip("/")
        if re.match(r"https?://", text):
            return cls(text)
        if text.count("/") != 1:
            raise ValueError(f"give a Hugging Face model as org/name or its URL, not {text!r}")
        return cls(HF + text)

    @property
    def name(self) -> str:
        """The model's own name: https://huggingface.co/org/Name-7B → Name-7B."""
        return self.url.split("/")[-1]


@dataclass
class ImportOptions:
    """``tai import``'s options: the name on the device (None: the repository's), a description, reasoning."""
    name: str | None = None
    description: str = ""
    reasoning: bool = False


@dataclass
class ImportChoice:
    """What the user decides: the model and the options."""
    model: HfModel
    opts: ImportOptions

    @property
    def name(self) -> str:
        """The name on the device: --name, else the repository's name."""
        return self.opts.name or self.model.name


def first(*values) -> str:
    """The first value that isn't empty (the app's way of choosing between fields)."""
    return next((str(v).strip() for v in values if v is not None and str(v).strip()), "")


@dataclass
class Inspection:
    """What the device says about a Hugging Face model (``data`` of ``import/inspect``)."""
    raw: dict

    @classmethod
    def of(cls, response: dict) -> Inspection:
        return cls(response.get("data") or response)

    @property
    def model(self) -> dict:
        return self.raw.get("model") or {}

    @property
    def support(self) -> dict:
        return self.raw.get("import_support") or {}

    @property
    def problem(self) -> dict | None:
        """The error the device reports, or an import_support that says no."""
        error = self.raw.get("error")
        if error:
            return error if isinstance(error, dict) else {"message": str(error)}
        disk = self.raw.get("disk_space") or {}
        if disk.get("sufficient") is False:
            return disk
        return self.support if self.support.get("supported") is False else None

    @property
    def needs_base_model(self) -> dict | None:
        """``required_base_model`` ({model_id, display_name}) when a store model must be installed first."""
        p = self.problem or self.support
        if p.get("status") != "action_required":
            return None
        missing = p.get("reason_code") == "CONVERTER_BASE_MODEL_MISSING"
        return (p.get("required_base_model") or {}) if missing or p.get("resolution") == "install_base_model" else None

    @property
    def supported(self) -> bool:
        status = first(self.raw.get("status")).lower()
        if status:
            return status == "supported"
        return self.problem is None and self.support.get("supported") is True

    @property
    def size(self) -> int:
        for v in (self.raw.get("inspection_info", {}).get("size"), self.model.get("size_bytes"),
                  (self.raw.get("download_sizes") or {}).get("model")):
            if v and float(v) > 0:
                return int(v)
        return 0

    @property
    def task(self) -> str:
        t = self.raw.get("model_task") or {}
        return first(t.get("label"), t.get("type"), self.model.get("type"))

    def why_not(self) -> str:
        """Why it can't be imported, in words."""
        p = self.problem or self.support
        code = first(p.get("reason_code"), p.get("code"))
        return REASONS.get(code) or first(p.get("message"), p.get("msg"), code, "the device says it can't import this model")

    def request(self, choice: ImportChoice) -> dict:
        """The body of ``POST import``, filled the way the app fills it."""
        m, s = self.model, self.support
        base_ids = (self.raw.get("hf") or {}).get("base_model_ids") or [None]
        body = {
            "base_model_id": first(s.get("base_model_id"), base_ids[0], m.get("base_model_id")),
            "hf_url": choice.model.url,
            "hf_revision": first(m.get("hf_revision"), s.get("hf_revision"), m.get("revision"), s.get("revision")),
            "toolkit_id": first(s.get("toolkit_id"), m.get("toolkit_id")),
            "toolkit_revision": first(s.get("toolkit_revision"), m.get("toolkit_revision")),
            "model_name": choice.name,
            "display_name": choice.name,
            "model_desc": choice.opts.description,
            "reasoning_enabled": choice.opts.reasoning,
        }
        return {k: v for k, v in body.items() if v != "" or k in ("model_desc",)}


def error_text(detail) -> str:
    """A failed inspect/import response's ``detail`` (or body) in words."""
    if isinstance(detail, dict):
        inner = detail.get("detail") if isinstance(detail.get("detail"), dict) else detail
        code = first(inner.get("reason_code"), inner.get("code"))
        return REASONS.get(code) or first(inner.get("message"), inner.get("msg"), inner.get("error"), code, str(detail))
    return str(detail)


def event_state(event: str, ev: dict) -> str:
    """``done``, ``error`` or ``progress`` for an import stream event (the app's rules)."""
    if event == "error" or _failed(ev):
        return "error"
    return "done" if event == "done" and _completed(ev) else "progress"


def _failed(ev: dict) -> bool:
    signs = (ev.get("success") is False, first(ev.get("status")).lower() in _FAILED, ev.get("error"), ev.get("error_code"))
    return any(signs)


def _completed(ev: dict) -> bool:
    stage = first(ev.get("stage_name"), ev.get("stage")).lower()
    return ev.get("success") is True and first(ev.get("status")).lower() == "completed" and stage == "complete"


def event_note(ev: dict) -> str:
    """A short progress note, e.g. 'downloading model' or 'converting'."""
    return first(ev.get("stage_name"), ev.get("stage"), ev.get("status"), ev.get("message"))
