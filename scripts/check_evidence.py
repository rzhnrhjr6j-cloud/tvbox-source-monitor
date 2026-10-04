#!/usr/bin/env python3
import sys
from pathlib import Path
import yaml
ROOT=Path(__file__).resolve().parents[1]; S=ROOT/'.codex'/'state'; T=yaml.safe_load((S/'tasks.yaml').read_text()) or {}; E=yaml.safe_load((S/'evidence.yaml').read_text()) or {}
bad=[]
for t in T.get('task_tree',[]):
 if t.get('status')=='DONE':
  p=[e for e in E.get('evidence',[]) if e.get('task_id')==t.get('id') and e.get('status')=='PASS']
  covered={c for e in p for c in e.get('criterion_ids',[])}; miss=set(t.get('acceptance_criteria',[]))-covered
  if miss: bad.append(f'{t.get("id")}: missing {sorted(miss)}')
  if any(e.get('level')=='ASSUMPTION' for e in p): bad.append(f'{t.get("id")}: ASSUMPTION cannot support DONE')
for e in E.get('evidence',[]):
 if e.get('status')=='PASS' and not e.get('task_id'): bad.append(f'{e.get("id")}: PASS evidence missing task_id')
if bad:
 print('EVIDENCE FAIL'); [print(' -',x) for x in bad]; sys.exit(1)
print('EVIDENCE PASS')
