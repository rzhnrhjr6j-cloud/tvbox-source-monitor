#!/usr/bin/env python3
import sys
from pathlib import Path
import yaml
ROOT = Path(__file__).resolve().parents[1]
S = ROOT/'.codex'/'state'

def load(name):
    p=S/name
    return yaml.safe_load(p.read_text(encoding='utf-8')) or {}

proj=load('project.yaml'); goals=load('goals.yaml'); reqs=load('requirements.yaml'); tasks=load('tasks.yaml'); ev=load('evidence.yaml'); rel=load('release.yaml'); tr=load('traceability.yaml')
errors=[]; warnings=[]
current=proj.get('control',{}).get('current_product_version')
goal_ids={g.get('id') for g in goals.get('goals',[]) if g.get('id')}
req_map={r.get('id'):r for r in reqs.get('requirements',[]) if r.get('id')}
task_map={t.get('id'):t for t in tasks.get('task_tree',[]) if t.get('id')}

if not goals.get('mission',{}).get('statement') or goals.get('mission',{}).get('statement')=='REPLACE_ME':
    errors.append('mission statement is not initialized')
if not goal_ids and req_map:
    errors.append('requirements exist but no product goals exist')

for rid,r in req_map.items():
    if r.get('status') in {'ACTIVE','COMMITTED'}:
        refs=[tid for tid,t in task_map.items() if rid in t.get('requirement_ids',[])]
        if not refs:
            errors.append(f'{rid}: no task coverage')
        if r.get('goal_refs') and not set(r['goal_refs']).issubset(goal_ids):
            errors.append(f'{rid}: references unknown goal IDs')
        if not r.get('acceptance_criteria'):
            errors.append(f'{rid}: missing acceptance_criteria')

for tid,t in task_map.items():
    if t.get('status') in {'READY','IN_PROGRESS','VERIFYING','DONE'}:
        if not t.get('requirement_ids'):
            errors.append(f'{tid}: active task has no requirement_ids')
        if not t.get('objective'):
            errors.append(f'{tid}: active task missing objective')
        if not t.get('goal_refs'):
            errors.append(f'{tid}: active task missing goal_refs')
        elif not set(t['goal_refs']).issubset(goal_ids):
            errors.append(f'{tid}: unknown goal_refs')
        if not t.get('scope_tags'):
            warnings.append(f'{tid}: no scope_tags; semantic drift detection weaker')
        if t.get('status')=='DONE':
            covered=set()
            for e in ev.get('evidence',[]):
                if e.get('task_id')==tid and e.get('status')=='PASS':
                    covered.update(e.get('criterion_ids',[]))
            missing=set(t.get('acceptance_criteria',[]))-covered
            if missing: errors.append(f'{tid}: traceability missing evidence for {sorted(missing)}')

# Requirement -> tasks -> evidence -> current release trace
for t in task_map.values():
    if t.get('release')==current and t.get('status') in {'READY','IN_PROGRESS','VERIFYING','DONE'}:
        for rid in t.get('requirement_ids',[]):
            if rid not in req_map: errors.append(f'{t.get("id")}: references missing requirement {rid}')

# mirror traceability.yaml: if populated, ensure it matches the state files
for rid, tids in (tr.get('requirements') or {}).items():
    actual=[t.get('id') for t in task_map.values() if rid in t.get('requirement_ids',[])]
    if sorted(tids or []) != sorted(actual):
        errors.append(f'traceability requirements[{rid}] disagrees with tasks.yaml')

if errors:
    print('TRACEABILITY FAIL')
    for x in errors: print(' -',x)
else:
    print('TRACEABILITY PASS')
if warnings:
    print('WARNINGS')
    for x in warnings: print(' -',x)
sys.exit(1 if errors else 0)
