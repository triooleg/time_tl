# Project agent team

Executable Codex agent profiles are stored in `.codex/agents/*.toml`. This directory contains the human-readable routing registry, task packet, and review templates. Root `AGENTS.md` contains mandatory policy for every agent.

The primary orchestrator may run at most three workers concurrently. Agents do not self-approve safety-sensitive changes and do not perform live messaging or deployment without explicit approval.
