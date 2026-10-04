# Codex Project Control — V1.4 Core

This repository uses Codex Project Control Framework V1.4 Core.

## Source of truth
- `.codex/PROJECT_CONTROL.md` — human-readable control summary
- `.codex/state/*.yaml` — machine-readable project state
- `.codex/PLANS.md` — living execution plan for complex/multi-session work
- `scripts/` — control and validation tooling

## Mandatory startup
Before substantial implementation:
1. Read `.codex/PROJECT_CONTROL.md` and `.codex/state/session.yaml`.
2. Read the current release, goals, requirements, current task, risks and relevant acceptance criteria.
3. Run `./scripts/project-status.sh` and `./scripts/validate-project.sh`.
4. If legacy adoption audit is pending, complete the audit and establish a baseline before broad implementation.

## Mandatory task flow
`BACKLOG → READY → IN_PROGRESS → VERIFYING → DONE`

- IN_PROGRESS requires a passing Alignment Review.
- DONE requires PASS acceptance evidence for every acceptance criterion.
- Release changes require Change Request + Impact Review.
- Current-release changes require approval and must be linked to requirements/tasks.

## Mandatory change flow
`DRAFT → ANALYZED → APPROVED_CURRENT / APPROVED_FUTURE / BACKLOG / REJECTED`

Never silently turn a discovered idea into current work.

## Completion rule
A task is not DONE because the code exists or a happy-path test passes. The completion report must identify evidence, remaining unknowns and the checkpoint/version.

## Release rule
A release cannot be marked RELEASED until its task/evidence trace is complete, blocking issues are cleared, and the release is backed by a Git tag.
