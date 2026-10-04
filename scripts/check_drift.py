#!/usr/bin/env python3
"""Deterministic semantic-drift guardrails based on explicit project contracts.
This does not claim to understand product semantics. It detects missing/contradictory
contracts and explicit out-of-scope term leakage, then requires a human/Codex alignment
review for ambiguous cases.
"""
import re, sys
from pathlib import Path
import yaml
ROOT=Path(__file__).resolve().parents[1]; S=ROOT/'.codex'/'state'

def load(name): return yaml.safe_load((S/name).read_text(encoding='utf-8')) or {}
proj=load('project.yaml'); goals=load('goals.yaml'); reqs=load('requirements.yaml'); tasks=load('tasks.yaml'); guards=load('guardrails.yaml'); reviews=load('alignment_reviews.yaml')
errors=[]; warnings=[]
current=proj.get('control',{}).get('current_product_version')
req_map={r.get('id'):r for r in reqs.get('requirements',[])}
review_map={(r.get('task_id'), r.get('release')):r for r in reviews.get('reviews',[])}
for t in tasks.get('task_tree',[]):
    if t.get('release')!=current or t.get('status') not in {'READY','IN_PROGRESS','VERIFYING','DONE'}: continue
    tid=t.get('id')
    if not t.get('goal_refs'): errors.append(f'{tid}: no goal_refs; cannot establish goal alignment')
    if not t.get('requirement_ids'): errors.append(f'{tid}: no requirement_ids; cannot establish requirement alignment')
    required_tags=set()
    for rid in t.get('requirement_ids',[]):
        r=req_map.get(rid,{}); required_tags.update(r.get('scope_tags',[]))
        if r and r.get('release')!=current and t.get('status') not in {'DONE','DEFERRED'}:
            errors.append(f'{tid}: active task references requirement {rid} outside current release')
    task_tags=set(t.get('scope_tags',[]))
    allowed=set(guards.get('semantic',{}).get('allowed_scope_tags',[]))
    forbidden=set(guards.get('semantic',{}).get('forbidden_scope_tags',[]))
    if forbidden & task_tags:
        errors.append(f'{tid}: forbidden scope tags present: {sorted(forbidden & task_tags)}')
    if allowed and not task_tags.issubset(allowed):
        errors.append(f'{tid}: scope_tags outside guardrail allowlist: {sorted(task_tags-allowed)}')
    text=' '.join(str(x or '') for x in [t.get('title'),t.get('objective'),' '.join(t.get('acceptance_criteria',[]))]).lower()
    for term in guards.get('semantic',{}).get('forbidden_terms',[]):
        if term and re.search(r'\b'+re.escape(str(term).lower())+r'\b', text):
            errors.append(f'{tid}: task text contains forbidden term "{term}"')
    # explicit non-goal leakage
    nong=set(t.get('non_goal_tags',[]))
    if nong & task_tags:
        errors.append(f'{tid}: scope_tags overlap its own non_goal_tags: {sorted(nong & task_tags)}')
    review=review_map.get((tid,current))
    if not review:
        if t.get('status') in {'IN_PROGRESS','VERIFYING','DONE'}:
            errors.append(f'{tid}: missing alignment review for current release')
        else:
            warnings.append(f'{tid}: alignment review not yet recorded; required before IN_PROGRESS')
    else:
        if review.get('status') not in {'PASS','PASS_WITH_WARNING'}:
            errors.append(f'{tid}: alignment review status is {review.get("status")}')
        if review.get('requirement_ids') != t.get('requirement_ids'):
            warnings.append(f'{tid}: alignment review requirement_ids differ from task')
        if review.get('scope_tags') != t.get('scope_tags'):
            warnings.append(f'{tid}: alignment review scope_tags differ from task')

# Requirements must declare scope and non-goals so Codex has a contract to align against.
for r in reqs.get('requirements',[]):
    if r.get('release')==current and r.get('status') in {'ACTIVE','COMMITTED'}:
        if not r.get('scope_tags'): errors.append(f'{r.get("id")}: missing scope_tags')
        if r.get('non_goals') is None: errors.append(f'{r.get("id")}: missing non_goals')

if errors:
    print('DRIFT GUARD FAIL')
    for x in errors: print(' -',x)
else:
    print('DRIFT GUARD PASS')
if warnings:
    print('WARNINGS')
    for x in warnings: print(' -',x)
sys.exit(1 if errors else 0)
