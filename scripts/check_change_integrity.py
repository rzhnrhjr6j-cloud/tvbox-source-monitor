#!/usr/bin/env python3
import subprocess,sys
from pathlib import Path
import yaml
ROOT=Path(__file__).resolve().parents[1]; S=ROOT/'.codex'/'state'
def load(n): return yaml.safe_load((S/n).read_text(encoding='utf-8')) or {}
p=load('project.yaml'); t=load('tasks.yaml'); c=load('changes.yaml');
cur=p.get('control',{}).get('current_task'); task=next((x for x in t.get('task_tree',[]) if x.get('id')==cur),None)
if not task: print('CHANGE INTEGRITY FAIL - current task missing'); sys.exit(1)
try:
    diff=subprocess.check_output(['git','diff','--name-status'],cwd=ROOT,text=True)
    untracked=subprocess.check_output(['git','ls-files','--others','--exclude-standard'],cwd=ROOT,text=True)
except Exception:
    print('CHANGE INTEGRITY WARN - no Git repository; skipped changed-file attribution')
    sys.exit(0)
files=[]
for line in diff.splitlines():
    if not line.strip(): continue
    parts=line.split('\t')
    path=parts[-1]
    files.append(path)
files += [x for x in untracked.splitlines() if x]
allowed=task.get('allowed_paths',[])+['.codex/','scripts/']
viol=[]
for f in sorted(set(files)):
    if not any(f==a or f.startswith(a) for a in allowed): viol.append(f)
# current change request linkage: if current task explicitly declares CRs, ensure they exist and are approved.
cr_map={x.get('id'):x for x in c.get('changes',[])}
for cid in task.get('change_request_ids',[]):
    cr=cr_map.get(cid)
    if not cr: viol.append(f'{cid} [missing change request]')
    elif cr.get('status') not in {'APPROVED_CURRENT','IMPLEMENTED'}: viol.append(f'{cid} [not approved for current release]')
if viol:
    print('CHANGE INTEGRITY FAIL'); [print(' -',x) for x in viol]; sys.exit(1)
print('CHANGE INTEGRITY PASS:',len(set(files)),'changed/untracked files attributed to current task')
