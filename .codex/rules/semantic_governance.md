# Semantic Governance V1.2

本框架不声称脚本能够像人一样理解产品意图。V1.2采用“显式合同 + 机器校验 + 对齐审查”的三层方法。

## 必须存在的链路

Mission/Goal → Requirement → Change Request（如有）→ Task → Code/Paths → Evidence → Release。

任何一段缺失，项目状态不能升级。

## 防止目标漂移

每个当前版本的 Requirement 必须声明：
- goal_refs
- scope_tags
- non_goals
- acceptance_criteria

每个当前版本的 Task 必须声明：
- goal_refs
- requirement_ids
- scope_tags
- non_goal_tags
- objective

每个进入 IN_PROGRESS 的 Task 都必须有当前 Release 的 PASS/PASS_WITH_WARNING alignment review。

Alignment Review 必须说明：
1. 为什么这个任务服务于目标；
2. 它实现哪些需求；
3. 它明确不实现什么；
4. 是否发现范围扩张；
5. 是否需要新增 CR。

## 机器能够判断的“漂移”

- 任务没有目标/需求引用
- 引用不存在的目标/需求
- 使用被禁止的 scope tag/term
- task scope 与自身 non-goal 冲突
- 当前 Release 任务没有 alignment review
- 当前 Release 要求没有任务覆盖
- Release 没有完整的任务/证据链
- 新增变更没有经过 Change Request / Impact Review

机器不能独立证明“业务语义一定正确”，因此保留人工/Codex Alignment Review 作为语义闸门。
