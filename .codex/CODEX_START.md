# Codex Start Contract V1.4 Core

## 1. Restore project state
Read:
1. `.codex/PROJECT_CONTROL.md`
2. `.codex/state/project.yaml`
3. `.codex/state/goals.yaml`
4. `.codex/state/requirements.yaml`
5. `.codex/state/tasks.yaml`
6. `.codex/state/session.yaml`
7. relevant risks/issues/decisions/evidence

Run:
```bash
./scripts/project-status.sh
./scripts/validate-project.sh
```

## 2. Before implementation
- Confirm current release, phase, task, dependencies and WIP.
- Confirm scope alignment and Acceptance Criteria.
- If the task is outside current scope/release, create or update a Change Request first.
- If the project is legacy and audit is pending, do not resume broad implementation.

## 3. During implementation
- Keep the task narrow.
- Do not silently create adjacent features.
- Record important decisions, risks and newly discovered requirements.
- If a conclusion changes, record why and what evidence changed it.

## 4. Before DONE
- Transition to VERIFYING.
- Run relevant tests.
- Record PASS evidence for every Acceptance Criterion.
- Record known risks/unverified items.
- Produce/update a Completion Report.
- Only then transition to DONE.

## 5. Before RELEASED
- All committed release tasks DONE.
- No open HIGH/CRITICAL blockers.
- Release/project versions match.
- Release traceability is complete.
- Git tag matches code version.
- Release Gate passes.
