<a href="https://claude.ai"><img src="made-with-claude.png" height="32" alt="Made with Claude"></a>

# tai

Use every model on your [Tiiny](https://www.tiiny.ai/) from the terminal, over Wi-Fi.

```
$ tai models
    Name                  Task   Type                NPU  Model ID
 ○  TRELLIS.2-4B          3d     Image-to-3D         6%   microsoft/TRELLIS.2-4B
 ○  Qwen3-ASR-1.7B        asr    ASR                 7%   Qwen/Qwen3-ASR-1.7B
 ●  Qwen3-Coder-Next      chat   Text Generation     94%  Qwen/Qwen3-Coder-Next
 ○  Qwen3.6-35B-A3B       chat   Image-Text-to-Text  47%  Qwen/Qwen3.6-35B-A3B
 ...
```

The device only runs *loaded* models, and they share an NPU budget. `tai` loads whatever a command needs. When it doesn't fit, it picks the idle models that free the least NPU (so big models stay loaded), shows them, and asks. Pass `-y` to skip the prompt.

## Install

```sh
make setup   # creates .venv and installs tai into it
make link    # optional: symlink tai into ~/.local/bin (PREFIX=... to change)
```

`tai` reads the device address and auth key from the official `tiiny` CLI's config (`~/.local/tiiny/config.json`), so run `tiiny connect <ip>` and `tiiny login` once. To override, set `TAI_DEVICE`, or set the token with `TIINY_API_KEY` or `TAI_TOKEN`. Put them in the environment or in a `.env` file in the current directory; real environment variables win.

## Usage

```sh
tai ask "explain RAII in two sentences"
git diff | tai ask -m coder "review this diff"
tai ask -f photo.jpg "what's in this picture?"   # vision model (Qwen3.6)
tai ask --think --show-thinking "hard puzzle…"   # toggle reasoning

tai chat                    # interactive; /help inside for commands
tai chat -c                 # continue the last session
tai history                 # list sessions; `tai history show 3`, `tai history rm 3`

tai image "a fox in the snow, watercolor" -o fox.png --size 1024x1024 --open
tai tts "Hello there" -i "female, british accent" --play
tai asr recording.wav
tai embed "first text" "second text"           # JSON lines
tai 3d toy.png --faces 200000 -o toy.glb       # ~15 min, image → textured mesh

tai agent                   # coding agent (full-screen) in the current folder
tai agent "fix the failing test"   # start with a task; -c continues here
                            # inside: /tiiny status|models|load|unload|restart|shutdown, /view
                            # images, audio, 3D and questions open in a local browser viewer
tai mcp                     # MCP servers the agent uses (.mcp.json, ~/.config/tai/mcp.json)
tai skills                  # skills it can use (.claude/skills, ~/.claude/skills, ~/.config/tai/skills)

tai status                  # NPU usage
tai load qwen3.6            # pre-load; tai unload --all
tai config set default.chat coder              # preferred model per task
tai config set think off                       # default reasoning mode
```

Models can be named by full id, display name, or any unique part of either (`coder`, `z-image`, `omni`).

## Development

```sh
make test    # unit tests (mocked, no device needed)
make check   # tests + code-health delta; run before committing
make help    # all targets
```

See `CLAUDE.md` and `cards/` for how the device API works and how swapping is decided.
