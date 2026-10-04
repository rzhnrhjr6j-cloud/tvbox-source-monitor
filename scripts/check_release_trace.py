#!/usr/bin/env python3
import subprocess,sys
from pathlib import Path
import yaml
ROOT=Path(__file__).resolve().parents[1]; S=ROOT/'.codex'/'state'
def load(n): return yaml.safe_load((S/n).read_text(encoding='utf-8')) or {}
p=load('project.yaml'); req=load('requirements.yaml'); tasks=load('tasks.yaml'); rel=load('release.yaml'); ev=load('evidence.yaml'); changes=load('changes.yaml'); goals=load('goals.yaml')
errors=[]
cur=rel.get('current_release',{}); version=cur.get('product_version');
for r in req.get('requirements',[]):
    if r.get('release')==version and r.get('status') in {'ACTIVE','COMMITTED'}:
        tids=[t for t in tasks.get('task_tree',[]) if r.get('id') in t.get('requirement_ids',[]) and t.get('release')==version]
        if not tids: errors.append(f'{r.get("id")}: no task in release')
        elif any(t.get('status')!='DONE' for t in tids): errors.append(f'{r.get("id")}: not all covering tasks DONE')
        crit=set(r.get('acceptance_criteria',[])); covered=set()
        for e in ev.get('evidence',[]):
            if e.get('status')=='PASS' and any(r.get('id') in x.get('requirement_ids',[]) for x in tasks.get('task_tree',[]) if x.get('id')==e.get('task_id')):
                covered.update(e.get('criterion_ids',[]))
        miss=crit-covered
        if miss: errors.append(f'{r.get("id")}: acceptance evidence missing {sorted(miss)}')
# Current release cannot be released with unresolved changes targeted at this release.
for ch in changes.get('changes',[]):
    if ch.get('target_release')==version and ch.get('status') in {'DRAFT','ANALYZED','APPROVED_CURRENT'}:
        errors.append(f'{ch.get("id")}: change request not implemented/closed')
# Goal coverage
req_goal=set()
for r in req.get('requirements',[]):
    if r.get('release')==version and r.get('status') in {'ACTIVE','COMMITTED'}: req_goal.update(r.get('goal_refs',[]))
all_goals={g.get('id') for g in goals.get('goals',[])}
missing_goal=req_goal-all_goals
if missing_goal: errors.append('release references unknown goals: '+','.join(sorted(missing_goal)))
if errors:
    print('RELEASE TRACE FAIL'); [print(' -',x) for x in errors]; sys.exit(1)
print('RELEASE TRACE PASS')
