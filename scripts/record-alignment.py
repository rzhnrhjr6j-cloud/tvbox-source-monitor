#!/usr/bin/env python3
import argparse, datetime as dt, sys
from pathlib import Path
import yaml
ROOT=Path(__file__).resolve().parents[1]; S=ROOT/'.codex'/'state'
def load(n): return yaml.safe_load((S/n).read_text(encoding='utf-8')) or {}
def save(n,d): (S/n).write_text(yaml.safe_dump(d,allow_unicode=True,sort_keys=False),encoding='utf-8')

a=argparse.ArgumentParser(); a.add_argument('task_id'); a.add_argument('--status',choices=['PASS','PASS_WITH_WARNING','FAIL'],required=True); a.add_argument('--reviewer',default='codex'); a.add_argument('--summary',required=True); a.add_argument('--reason',default=''); args=a.parse_args()
p=load('project.yaml'); tdoc=load('tasks.yaml');
t=next((x for x in tdoc.get('task_tree',[]) if x.get('id')==args.task_id),None)
if not t: raise SystemExit('ERROR task not found')
current=p.get('control',{}).get('current_product_version')
d=load('alignment_reviews.yaml'); reviews=d.setdefault('reviews',[])
record={'task_id':args.task_id,'release':current,'status':args.status,'reviewer':args.reviewer,'timestamp':dt.datetime.now().astimezone().isoformat(timespec='seconds'),'summary':args.summary,'reason':args.reason,'requirement_ids':t.get('requirement_ids',[]),'goal_refs':t.get('goal_refs',[]),'scope_tags':t.get('scope_tags',[]),'non_goal_tags':t.get('non_goal_tags',[])}
reviews[:]=[r for r in reviews if not (r.get('task_id')==args.task_id and r.get('release')==current)]
reviews.append(record); save('alignment_reviews.yaml',d)
print('RECORDED ALIGNMENT',args.task_id,args.status)
