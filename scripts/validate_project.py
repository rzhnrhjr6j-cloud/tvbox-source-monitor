#!/usr/bin/env python3
import sys
from pathlib import Path
import yaml
ROOT=Path(__file__).resolve().parents[1]; S=ROOT/'.codex'/'state'
REQ=['project.yaml','goals.yaml','requirements.yaml','tasks.yaml','changes.yaml','risks.yaml','decisions.yaml','release.yaml','session.yaml','evidence.yaml','issues.yaml','events.yaml','traceability.yaml','guardrails.yaml','alignment_reviews.yaml','impact_reviews.yaml']
E=[]; W=[]
def L(n): return yaml.safe_load((S/n).read_text(encoding='utf-8')) or {}
missing=[f for f in REQ if not (S/f).exists()]
E += ['missing '+f for f in missing]
p=L('project.yaml') if not missing else {}; g=L('goals.yaml') if not missing else {}; q=L('requirements.yaml') if not missing else {}; t=L('tasks.yaml') if not missing else {}; c=L('changes.yaml') if not missing else {}; r=L('release.yaml') if not missing else {}; e=L('evidence.yaml') if not missing else {}; i=L('issues.yaml') if not missing else {}; a=L('alignment_reviews.yaml') if not missing else {}
if not missing:
 C=p.get('control',{}); scope=p.get('scope',{}); mission=g.get('mission',{}).get('statement')
 if not p.get('project',{}).get('name') or str(p.get('project',{}).get('name')).startswith('REPLACE_ME'): E.append('project name is not initialized')
 if not mission or str(mission).startswith('REPLACE_ME'): E.append('mission statement is not initialized')
 if not scope.get('in_scope') or not scope.get('out_of_scope'): E.append('in_scope/out_of_scope must both be populated')
 T={x.get('id'):x for x in t.get('task_tree',[])}
 if len(T)!=len(t.get('task_tree',[])): E.append('duplicate task IDs')
 G={x.get('id') for x in g.get('goals',[])}
 R={x.get('id'):x for x in q.get('requirements',[])}
 current=C.get('current_product_version')
 for x in t.get('task_tree',[]):
  if x.get('status') not in {'BACKLOG','READY','IN_PROGRESS','VERIFYING','DONE','BLOCKED','DEFERRED'}: E.append(f'{x.get("id")}: invalid task status')
  if x.get('status') in {'READY','IN_PROGRESS','VERIFYING','DONE'}:
   if x.get('release')!=current and x.get('status') not in {'DONE','DEFERRED'}: E.append(f'{x.get("id")}: active task not in current release')
   if not x.get('goal_refs'): E.append(f'{x.get("id")}: missing goal_refs')
   if not x.get('requirement_ids'): E.append(f'{x.get("id")}: missing requirement_ids')
   if set(x.get('goal_refs',[]))-G: E.append(f'{x.get("id")}: references unknown goal')
  for d in x.get('dependencies',[]):
   if d not in T: E.append(f'{x.get("id")}: missing dependency {d}')
  for rid in x.get('requirement_ids',[]):
   if rid not in R: E.append(f'{x.get("id")}: missing requirement {rid}')
  if x.get('status')=='DONE':
   covered={ac for z in e.get('evidence',[]) if z.get('task_id')==x.get('id') and z.get('status')=='PASS' for ac in z.get('criterion_ids',[])}
   miss=set(x.get('acceptance_criteria',[]))-covered
   if miss: E.append(f'{x.get("id")}: DONE without evidence for {sorted(miss)}')
 wip=sum(x.get('status') in {'IN_PROGRESS','VERIFYING'} for x in t.get('task_tree',[]))
 if wip>int(C.get('wip_limit',2)): E.append(f'WIP {wip} exceeds limit {C.get("wip_limit")}')
 Rls=r.get('current_release',{})
 for aa,bb in [('product_version','current_product_version'),('requirement_baseline','current_requirement_baseline'),('code_version','current_code_version'),('database_version','current_database_version')]:
  if Rls.get(aa)!=C.get(bb): E.append(f'release/project mismatch: {aa}')
 for rr in q.get('requirements',[]):
  if rr.get('release')==current and rr.get('status') in {'ACTIVE','COMMITTED'}:
   if not rr.get('goal_refs'): E.append(f'{rr.get("id")}: missing goal_refs')
   if not rr.get('scope_tags'): E.append(f'{rr.get("id")}: missing scope_tags')
   if rr.get('non_goals') is None: E.append(f'{rr.get("id")}: missing non_goals')
 for ar in a.get('reviews',[]):
  if ar.get('status') not in {'PASS','PASS_WITH_WARNING','FAIL'}: E.append(f'{ar.get("task_id")}: invalid alignment review status')
 if any(ch.get('status')=='APPROVED_CURRENT' and ch.get('target_release')!=current for ch in c.get('changes',[])): E.append('approved current CR targets wrong release')
 for asm in L('risks.yaml').get('assumptions',[]):
  if asm.get('status')=='UNVERIFIED': W.append(f'{asm.get("id")}: unverified assumption')
if E:
 print('VALIDATION FAIL'); [print(' -',x) for x in E]
else: print('VALIDATION PASS')
if W: print('WARNINGS'); [print(' -',x) for x in W]
sys.exit(1 if E else 0)
