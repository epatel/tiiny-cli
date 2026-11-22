"""Music: instrument loops (Foundation-1) and songs (YuE2, AceStep, SongGeneration).

Routes and quirks come from Tiiny Studio's code (rust/studio-core/src/media/music.rs, read
2026-11-22); not yet run on our device. See the music section of cards/device-api.md.

- Loops: ``POST /v1/music/generate/wav {model, prompt, config}`` answers with the WAV itself.
- YuE2 and AceStep: ``POST /v1/music/generate {model, config}`` composes the whole song at
  ``seconds_total`` and answers ``{success, audio_data (base64 WAV), session_id}``.
- SongGeneration: the gateway's plain ``/generate`` fails ("Session not found"), so
  ``/generate/preview`` with ``preview_seconds`` = the length; from 240 s an 8-second opening,
  then ``/generate/resume {session_id}`` renders the full arrangement.
- A song takes many minutes and a quiet connection can drop before the answer comes; the
  finished song still shows up in ``GET /v1/music/sessions`` and downloads as MP3.
"""

from __future__ import annotations

# Works on the device's wire format: JSON objects and string ids.
# aihealth: disable=primitive_obsession

import base64
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from tai.client import TiinyClient, TiinyError
from tai.options import merge

LOOP_MODELS = ("foundation",)  # by id; every other music model makes songs
FULL_SONG_SECONDS = 240  # from here SongGeneration renders its full-length arrangement
OPENING_SECONDS = 8
DEFAULT_SONG_SECONDS = 60  # SongGeneration's preview needs a length
WATCH_EVERY = 30  # seconds between looks at the device's sessions after a dropped answer
WATCH_FOR = 3600
FAILED = ("failed", "error", "cancelled")


def is_loop(model_id: str) -> bool:
    return any(name in model_id.lower() for name in LOOP_MODELS)


def is_staged(model_id: str, info: dict) -> bool:
    """SongGeneration: composed through preview (+ resume) rather than one plain /generate."""
    service = str(info.get("service_name") or "")
    return service.startswith("SongGeneration") if service else "songgeneration" in model_id.lower()


def _present(fields: dict) -> dict:
    return {k: v for k, v in fields.items() if v not in (None, "")}


@dataclass(frozen=True)
class MusicOptions:
    lyrics: str = ""
    seconds: float | None = None
    instrumental: bool = False
    steps: int | None = None
    seed: int | None = None
    style: Path | None = None  # music whose feel a song borrows (SongGeneration, AceStep)
    extra: dict = field(default_factory=dict)  # -x fields, over the ones above

    def song_config(self, caption: str) -> dict:
        style = base64.b64encode(self.style.read_bytes()).decode() if self.style else None
        fields = {"caption": caption, "lyrics": self.lyrics.strip(),
                  "generation_type": "bgm" if self.instrumental else "mixed",
                  "seconds_total": self.seconds, "steps": self.steps, "seed": self.seed,
                  "reference_audio_base64": style}
        return merge(_present(fields), self.extra)

    def loop_config(self) -> dict:
        return merge(_present({"seconds_total": self.seconds, "steps": self.steps, "seed": self.seed}), self.extra)

    def problems(self, model_id: str) -> list[str]:
        """What the model would reject or ignore."""
        if is_loop(model_id):
            return [f"--{flag}: this model makes instrument loops, not songs"
                    for flag, given in (("lyrics", self.lyrics.strip()), ("instrumental", self.instrumental),
                                        ("style", self.style)) if given]
        if self.instrumental and "yue" in model_id.lower():
            return ["--instrumental: YuE2 only makes the full mix (vocals and backing); "
                    "pick another song model for an instrumental"]
        if not self.instrumental and not self.lyrics.strip():
            return ["a song with vocals needs --lyrics (or --instrumental)"]
        return []


@dataclass(frozen=True)
class Song:
    audio: bytes
    format: str  # "wav" or "mp3"
    session: str = ""  # the device's music session, when it said


class MusicJob:
    def __init__(self, client: TiinyClient, notice: Callable[[str], None] = lambda s: None,
                 sleep: Callable[[float], None] | None = None, clock: Callable[[], float] | None = None):
        self.client, self.notice = client, notice
        self.sleep, self.clock = sleep or time.sleep, clock or time.time

    def make(self, model_id: str, prompt: str, opts: MusicOptions, fmt: str = "wav") -> Song:
        """A loop or a song from ``prompt``, as ``fmt`` ("mp3" or "wav") when the device can give it."""
        if is_loop(model_id):
            return Song(self.client.music_loop(model_id, prompt, opts.loop_config()), "wav")
        song = self.song(model_id, opts.song_config(prompt), is_staged(model_id, self._info(model_id)))
        return self._as_mp3(song) if fmt == "mp3" else song

    def song(self, model_id: str, config: dict, staged: bool) -> Song:
        started = self.clock()
        try:
            res = self._compose(model_id, config, staged)
        except httpx.TransportError:
            self.notice("The connection dropped; waiting for the song to finish on the device…")
            return self._recover(model_id, started)
        if not res.get("success", True) or not res.get("audio_data"):
            raise TiinyError(f"no song came back: {res.get('error') or res.get('detail') or res}")
        return Song(base64.b64decode(res["audio_data"]), "wav", str(res.get("session_id") or ""))

    def _info(self, model_id: str) -> dict:
        try:
            return self.client.music_info(model_id)
        except TiinyError:  # older firmware has no /v1/music/info
            return {}

    def _compose(self, model_id: str, config: dict, staged: bool) -> dict:
        if not staged:
            return self.client.music_song(model_id, config)
        config = dict(config)
        seconds = config.pop("seconds_total", None) or DEFAULT_SONG_SECONDS
        if seconds < FULL_SONG_SECONDS:
            return self.client.music_song(model_id, {**config, "preview_seconds": seconds}, "/preview")
        opening = self.client.music_song(model_id, {**config, "preview_seconds": OPENING_SECONDS}, "/preview")
        session = opening.get("session_id")
        return self.client.music_song(model_id, config, "/resume", session) if session else opening

    def _as_mp3(self, song: Song) -> Song:
        if song.format == "mp3" or not song.session:
            return song
        return Song(self.client.music_download(song.session), "mp3", song.session)

    def _recover(self, model_id: str, since: float) -> Song:
        """The song this job started, from the device's sessions once it is finished."""
        deadline = self.clock() + WATCH_FOR
        while self.clock() < deadline:
            done = self._finished_session(model_id, since)
            if done:
                return Song(self.client.music_download(done), "mp3", done)
            self.sleep(WATCH_EVERY)
        raise TiinyError("the connection dropped and no finished song showed up on the device within an hour "
                         "(see GET /v1/music/sessions)")

    def _finished_session(self, model_id: str, since: float) -> str:
        try:
            sessions = self.client.music_sessions()
        except (httpx.TransportError, TiinyError):
            return ""
        for s in (s for s in sessions if _started_by(s, model_id, since)):
            if s.get("status") in FAILED:
                raise TiinyError(f"the song failed on the device: {s.get('error') or s.get('status')}")
            if s.get("status") == "completed":
                return str(s["session_id"])
        return ""


def _started_by(session: dict, model_id: str, since: float) -> bool:
    """A session of ``model_id`` started after ``since`` (the device composes one song at a time)."""
    started = float(session.get("created_at") or 0) >= since - 5
    return session.get("model_id") == model_id and started
