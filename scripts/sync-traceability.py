#!/usr/bin/env python3
from pathlib import Path
import yaml
S=Path(__file__).resolve().parents[1]/'.codex'/'state'
def load(n): return yaml.safe_load((S/n).read_text(encoding='utf-8')) or {}
def save(n,d): (S/n).write_text(yaml.safe_dump(d,allow_unicode=True,sort_keys=False),encoding='utf-8')
q=load('requirements.yaml'); t=load('tasks.yaml'); e=load('evidence.yaml'); r=load('release.yaml')
out={'requirements':{},'tasks':{},'evidence':{},'releases':{}}
for req in q.get('requirements',[]):
    rid=req.get('id'); out['requirements'][rid]=[x.get('id') for x in t.get('task_tree',[]) if rid in x.get('requirement_ids',[])]
for task in t.get('task_tree',[]):
    tid=task.get('id'); out['tasks'][tid]={'requirements':task.get('requirement_ids',[]),'evidence':[x.get('id') for x in e.get('evidence',[]) if x.get('task_id')==tid]}
for ev in e.get('evidence',[]): out['evidence'][ev.get('id')]={'task_id':ev.get('task_id'),'criterion_ids':ev.get('criterion_ids',[]),'status':ev.get('status')}
for release in r.get('releases',[]): out['releases'][str(release.get('product_version'))]=[x.get('id') for x in t.get('task_tree',[]) if x.get('release')==release.get('product_version')]
save('traceability.yaml',out); print('TRACEABILITY SYNC PASS')
