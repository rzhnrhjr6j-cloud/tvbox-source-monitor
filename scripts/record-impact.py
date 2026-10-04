#!/usr/bin/env python3
import argparse, datetime as dt
from pathlib import Path
import yaml
ROOT=Path(__file__).resolve().parents[1]; S=ROOT/'.codex'/'state'
def load(n): return yaml.safe_load((S/n).read_text(encoding='utf-8')) or {}
def save(n,d): (S/n).write_text(yaml.safe_dump(d,allow_unicode=True,sort_keys=False),encoding='utf-8')
a=argparse.ArgumentParser(); a.add_argument('cr_id'); a.add_argument('--status',choices=['PASS','FAIL'],required=True); a.add_argument('--summary',required=True); a.add_argument('--risk',default='MEDIUM'); a.add_argument('--reason',default=''); args=a.parse_args()
changes=load('changes.yaml'); cr=next((x for x in changes.get('changes',[]) if x.get('id')==args.cr_id),None)
if not cr: raise SystemExit('ERROR CR not found')
d=load('impact_reviews.yaml'); reviews=d.setdefault('reviews',[])
record={'change_request_id':args.cr_id,'status':args.status,'risk':args.risk,'summary':args.summary,'reason':args.reason,'timestamp':dt.datetime.now().astimezone().isoformat(timespec='seconds'),'affected_tasks':cr.get('affected_tasks',[]),'requirement_ids':cr.get('requirement_ids',[]),'target_release':cr.get('target_release')}
reviews[:]=[r for r in reviews if r.get('change_request_id')!=args.cr_id]; reviews.append(record); save('impact_reviews.yaml',d)
print('RECORDED IMPACT',args.cr_id,args.status)
