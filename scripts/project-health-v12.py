#!/usr/bin/env python3
import subprocess,sys
from pathlib import Path
R=Path(__file__).resolve().parents[1]
checks=[
 ('VALIDATE','./scripts/validate-project.sh'),
 ('TRACEABILITY','python3 scripts/check_traceability.py'),
 ('DRIFT_GUARD','python3 scripts/check_drift.py'),
 ('CHANGE_INTEGRITY','python3 scripts/check_change_integrity.py'),
 ('RELEASE_TRACE','python3 scripts/check_release_trace.py'),
 ('SCOPE','./scripts/check-scope.sh'),
 ('EVIDENCE','./scripts/check-tests.sh'),
]
failed=0
for label,cmd in checks:
    print('\n===',label,'===')
    rc=subprocess.run(cmd,cwd=R,shell=True).returncode
    failed += int(rc!=0)
print('\nV1.2 HEALTH:', 'FAIL' if failed else 'PASS')
sys.exit(1 if failed else 0)
