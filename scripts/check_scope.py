#!/usr/bin/env python3
import subprocess,sys
from pathlib import Path
import yaml
ROOT=Path(__file__).resolve().parents[1]; S=ROOT/'.codex'/'state'; p=yaml.safe_load((S/'project.yaml').read_text()) or {}; t=yaml.safe_load((S/'tasks.yaml').read_text()) or {}
cur=p.get('control',{}).get('current_task'); task=next((x for x in t.get('task_tree',[]) if x.get('id')==cur),None)
if not task: print('FAIL current task not found'); sys.exit(1)
try:
 out=subprocess.check_output(['git','status','--porcelain=v1','--untracked-files=all'],cwd=ROOT,text=True)
except Exception:
 print('WARN no Git repository; scope check skipped'); sys.exit(0)
files=[]
for line in out.splitlines():
 if line.strip():
  s=line[3:]; files.append(s.split(' -> ',1)[-1])
allowed=task.get('allowed_paths',[])+['.codex/','scripts/']; forbidden=task.get('forbidden_paths',[]); bad=[]
for f in files:
 if any(f==x or f.startswith(x) for x in forbidden): bad.append(f+' [forbidden]')
 elif not any(f.startswith(x) for x in allowed): bad.append(f+' [outside allowed_paths]')
if bad:
 print('SCOPE FAIL'); [print(' -',x) for x in bad]; sys.exit(1)
print('SCOPE PASS:',len(files),'changed files within allowed paths')
