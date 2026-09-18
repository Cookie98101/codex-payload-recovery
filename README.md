# Codex Payload Recovery

A Codex Skill and local Responses API proxy for two related problems:

- recovering an existing task damaged by HTTP 413 or orphaned tool outputs;
- preventing future image-heavy tasks from resending oversized history.

## Contents

- `skill/` — installable Codex Skill (`SKILL.md` plus protocol reference)
- `proxy/` — local FastAPI/httpx proxy with image pruning, orphan-output cleanup, streaming pass-through, and tests

The proxy is intended to bind to localhost only. Configure the upstream origin and run it behind a local Codex provider whose `base_url` ends in `/v1`.

The proxy does not log request bodies or authorization headers. Review and adapt its local paths, service manager, and provider-specific limits before deployment.
