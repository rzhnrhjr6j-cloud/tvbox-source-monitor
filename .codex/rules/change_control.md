# Change Control

需求变化是允许的；未经记录和影响评估的变化是不允许的。

Change Request lifecycle:
DRAFT → ANALYZED → APPROVED_CURRENT / APPROVED_FUTURE / BACKLOG / REJECTED

APPROVED_CURRENT 必须具备：
- requirement_ids
- affected_tasks
- target_release == 当前 Release
- PASS Impact Review

任何发现的新功能、新业务规则、跨模块依赖、架构调整建议，在实施前必须先归类为：
1. 当前 Release
2. 下一 Release
3. Backlog
4. Rejected / Out of Scope
