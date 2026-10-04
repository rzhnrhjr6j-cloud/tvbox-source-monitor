# Evidence-Based Completion

禁止把“代码写完”“本地能跑”“没有发现问题”直接等同于 DONE。

DONE 前必须：
1. task status = VERIFYING
2. acceptance criteria 全部有 PASS evidence
3. relevant tests 已执行
4. known issues / unverified items 已记录
5. completion report 已更新
6. checkpoint / code version 已记录

结论只能达到证据实际支持的等级：
ASSUMPTION < STATIC < UNIT < INTEGRATION < SCENARIO < LONG_RUNNING < PRODUCTION
