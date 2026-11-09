# Security policy

## Supported versions

There are no releases yet: fixes go into `main`, and only `main` is supported.

## Reporting a vulnerability

Please report it privately, not in a public issue:
[open a private security advisory](https://github.com/epatel/tiiny-cli/security/advisories/new)
(GitHub's private vulnerability reporting).

Include the version (`tai --version`), the command you ran (`tai serve`, `tai agent`, the
browser viewer, an MCP server, ...), and the steps or input that trigger it. You should get
an answer within a week. Once a fix is in, the advisory is published with credit to you,
unless you prefer otherwise.

## Scope

tai talks to a Tiiny device on your local network with the auth key from the official
`tiiny` CLI's config. Things worth reporting include:

- `tai serve` (binds 127.0.0.1, one API key) forwarding requests without the key, leaking
  the device key, or reaching anything other than the device
- the `tai agent` browser viewer (token + origin check) accepting requests from other sites
  or without its token
- `tai agent` running a write, edit, bash or MCP tool call without asking, when it should ask
- tai sending the device key, prompts or files anywhere other than the configured device

Known vulnerabilities in dependencies are checked daily by the Audit workflow
(`.github/workflows/audit.yml`); reports about those are welcome too.

What the device itself stores is outside tai's control: its knowledge-base vault records
chats sent to it, including those sent through tai.
