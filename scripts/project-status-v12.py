#!/usr/bin/env python3
from pathlib import Path
import yaml
S=Path(__file__).resolve().parents[1]/'.codex'/'state'
def load(n): return yaml.safe_load((S/n).read_text(encoding='utf-8')) or {}
p=load('project.yaml'); t=load('tasks.yaml'); q=load('requirements.yaml'); g=load('goals.yaml'); r=load('release.yaml'); c=load('changes.yaml'); risk=load('risks.yaml')
C=p.get('control',{}); ts=t.get('task_tree',[]); cur=C.get('current_product_version')
counts={};
for x in ts: counts[x.get('status')]=counts.get(x.get('status'),0)+1
active_req=sum(1 for x in q.get('requirements',[]) if x.get('release')==cur and x.get('status') in {'ACTIVE','COMMITTED'})
open_risks=sum(1 for x in risk.get('risks',[]) if x.get('status') not in {None,'CLOSED'})
changes_pending=sum(1 for x in c.get('changes',[]) if x.get('status') in {'DRAFT','ANALYZED','APPROVED_CURRENT','APPROVED_FUTURE'})
print('Project:',p.get('project',{}).get('name'))
print('Mission:',g.get('mission',{}).get('statement'))
print('Release:',cur,'Code:',C.get('current_code_version'),'DB:',C.get('current_database_version'))
print('Phase:',C.get('current_phase'),'Current task:',C.get('current_task'))
print('Goals:',len(g.get('goals',[])),'Active requirements:',active_req)
print('Tasks:',counts)
print('WIP:',sum(x.get('status') in {'IN_PROGRESS','VERIFYING'} for x in ts),'/',C.get('wip_limit'))
print('Pending change requests:',changes_pending,'Open risks:',open_risks)
print('Stable baseline:',C.get('stable_baseline'),'Development baseline:',C.get('development_baseline'))
