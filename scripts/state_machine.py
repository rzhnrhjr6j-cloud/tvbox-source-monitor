#!/usr/bin/env python3
import argparse, datetime as dt, subprocess, sys
from pathlib import Path
import yaml
ROOT=Path(__file__).resolve().parents[1]; S=ROOT/'.codex'/'state'
TASK={'BACKLOG':{'READY','DEFERRED'},'READY':{'IN_PROGRESS','DEFERRED'},'IN_PROGRESS':{'VERIFYING','BLOCKED','BACKLOG'},'BLOCKED':{'IN_PROGRESS','BACKLOG'},'VERIFYING':{'DONE','IN_PROGRESS','BLOCKED'},'DONE':set(),'DEFERRED':{'READY','BACKLOG'}}
CR={'DRAFT':{'ANALYZED'},'ANALYZED':{'APPROVED_CURRENT','APPROVED_FUTURE','BACKLOG','REJECTED'},'APPROVED_CURRENT':{'IMPLEMENTED'},'APPROVED_FUTURE':{'BACKLOG'},'BACKLOG':{'ANALYZED','REJECTED'},'REJECTED':set(),'IMPLEMENTED':set()}
REL={'PLANNED':{'DEVELOPMENT'},'DEVELOPMENT':{'VERIFYING'},'VERIFYING':{'RELEASED','DEVELOPMENT'},'RELEASED':{'RETIRED'},'RETIRED':set()}
def load(n): return yaml.safe_load((S/n).read_text()) or {}
def save(n,d): (S/n).write_text(yaml.safe_dump(d,allow_unicode=True,sort_keys=False),encoding='utf-8')
def log(k,i,a,b,r):
 d=load('events.yaml'); d.setdefault('events',[]).append({'timestamp':dt.datetime.now().astimezone().isoformat(timespec='seconds'),'kind':k,'target_id':i,'from':a,'to':b,'reason':r}); save('events.yaml',d)
def tasks(): return load('tasks.yaml').get('task_tree',[])
def evidence_for(tid): return [e for e in load('evidence.yaml').get('evidence',[]) if e.get('task_id')==tid and e.get('status')=='PASS']
def alignment_for(tid):
 d=load('alignment_reviews.yaml'); rel=load('project.yaml').get('control',{}).get('current_product_version'); return next((x for x in d.get('reviews',[]) if x.get('task_id')==tid and x.get('release')==rel),None)
def wip(): return sum(t.get('status') in {'IN_PROGRESS','VERIFYING'} for t in tasks())
def task(tid,new,reason):
 d=load('tasks.yaml'); t=next((x for x in d.get('task_tree',[]) if x.get('id')==tid),None)
 if not t: raise SystemExit('ERROR task not found')
 old=t.get('status'); new=new.upper()
 if new not in TASK.get(old,set()): raise SystemExit(f'ERROR illegal task transition {old} -> {new}')
 p=load('project.yaml'); limit=int(p.get('control',{}).get('wip_limit',2))
 if new=='IN_PROGRESS':
  if wip()>=limit: raise SystemExit('ERROR WIP limit exceeded')
  if t.get('release')!=p.get('control',{}).get('current_product_version'): raise SystemExit('ERROR task is not in current release')
  ar=alignment_for(tid)
  if not ar or ar.get('status') not in {'PASS','PASS_WITH_WARNING'}: raise SystemExit('ERROR task requires a passing alignment review before IN_PROGRESS')
  tm={x['id']:x for x in tasks()}; bad=[x for x in t.get('dependencies',[]) if tm.get(x,{}).get('status')!='DONE']
  if bad: raise SystemExit('ERROR dependencies not DONE: '+','.join(bad))
 if new=='DONE':
  required=set(t.get('acceptance_criteria',[])); covered={c for e in evidence_for(tid) for c in e.get('criterion_ids',[])}
  if required-covered: raise SystemExit('ERROR missing passing evidence: '+','.join(sorted(required-covered)))
 t['status']=new; save('tasks.yaml',d); log('TASK',tid,old,new,reason); print(f'PASS {tid}: {old} -> {new}')
def cr(cid,new,reason):
 d=load('changes.yaml'); c=next((x for x in d.get('changes',[]) if x.get('id')==cid),None)
 if not c: raise SystemExit('ERROR CR not found')
 old=c.get('status'); new=new.upper()
 if new not in CR.get(old,set()): raise SystemExit(f'ERROR illegal CR transition {old} -> {new}')
 if new=='APPROVED_CURRENT':
  if not c.get('requirement_ids') or not c.get('affected_tasks'): raise SystemExit('ERROR current-release CR needs requirement_ids and affected_tasks')
  if c.get('target_release')!=load('project.yaml').get('control',{}).get('current_product_version'): raise SystemExit('ERROR CR target release mismatch')
  impacts=load('impact_reviews.yaml').get('reviews',[])
  ir=next((x for x in impacts if x.get('change_request_id')==cid),None)
  if not ir or ir.get('status')!='PASS': raise SystemExit('ERROR current-release CR requires passing impact review')
 c['status']=new; save('changes.yaml',d); log('CR',cid,old,new,reason); print(f'PASS {cid}: {old} -> {new}')
def release(new,reason):
 d=load('release.yaml'); r=d.get('current_release',{}); old=r.get('state'); new=new.upper()
 if new not in REL.get(old,set()): raise SystemExit(f'ERROR illegal release transition {old} -> {new}')
 if new=='RELEASED':
  tags=set(subprocess.check_output(['git','tag'],cwd=ROOT,text=True).splitlines()) if (ROOT/'.git').exists() else set()
  if r.get('code_version') not in tags: raise SystemExit('ERROR release requires matching Git tag')
  pending=[t['id'] for t in tasks() if t.get('release')==r.get('product_version') and t.get('status')!='DONE']
  if pending: raise SystemExit('ERROR pending tasks: '+','.join(pending))
  blocked=[i['id'] for i in load('issues.yaml').get('issues',[]) if i.get('status') in {'OPEN','IN_PROGRESS'} and i.get('severity') in {'HIGH','CRITICAL'}]
  if blocked: raise SystemExit('ERROR blocking issues: '+','.join(blocked))
 r['state']=new; d['current_release']=r
 for x in d.get('releases',[]):
  if x.get('product_version')==r.get('product_version'): x['state']=new
 save('release.yaml',d); log('RELEASE',r.get('product_version'),old,new,reason); print(f'PASS release: {old} -> {new}')
def status():
 p=load('project.yaml'); c=p.get('control',{}); ts=tasks(); counts={}
 for t in ts: counts[t.get('status')]=counts.get(t.get('status'),0)+1
 print('Project:',p.get('project',{}).get('name')); print('Release:',c.get('current_product_version'),c.get('current_code_version')); print('Phase:',c.get('current_phase'),'Current Task:',c.get('current_task')); print('Tasks:',counts); print('WIP:',wip(),'/',c.get('wip_limit')); print('Stable:',c.get('stable_baseline')); print('Dev:',c.get('development_baseline'))
ap=argparse.ArgumentParser(); sp=ap.add_subparsers(dest='cmd',required=True)
sp.add_parser('status')
a=sp.add_parser('transition-task'); a.add_argument('id'); a.add_argument('state'); a.add_argument('--reason',required=True)
a=sp.add_parser('transition-cr'); a.add_argument('id'); a.add_argument('state'); a.add_argument('--reason',required=True)
a=sp.add_parser('transition-release'); a.add_argument('state'); a.add_argument('--reason',required=True)
a=ap.parse_args()
if a.cmd=='status': status()
elif a.cmd=='transition-task': task(a.id,a.state,a.reason)
elif a.cmd=='transition-cr': cr(a.id,a.state,a.reason)
elif a.cmd=='transition-release': release(a.state,a.reason)
