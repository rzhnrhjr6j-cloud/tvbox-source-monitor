#!/usr/bin/env python3
from pathlib import Path
import yaml
S=Path(__file__).resolve().parents[1]/'.codex'/'state'; p=yaml.safe_load((S/'project.yaml').read_text()) or {}; t=yaml.safe_load((S/'tasks.yaml').read_text()) or {}
C=p.get('control',{}); counts={}
for x in t.get('task_tree',[]): counts[x.get('status')]=counts.get(x.get('status'),0)+1
print('Project:',p.get('project',{}).get('name')); print('Release:',C.get('current_product_version'),'Code:',C.get('current_code_version')); print('Current task:',C.get('current_task'),'Phase:',C.get('current_phase')); print('Tasks:',counts); print('WIP:',sum(x.get('status') in {'IN_PROGRESS','VERIFYING'} for x in t.get('task_tree',[])),'/',C.get('wip_limit')); print('Stable:',C.get('stable_baseline')); print('Development:',C.get('development_baseline'))
