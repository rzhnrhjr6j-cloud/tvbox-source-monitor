# Traceability V1.2

目标是让任何结论都可以沿链路回溯：

Goal → Requirement → Task → Evidence → Release

如果有变化：

Goal → Requirement → Change Request → Impact Review → Task → Evidence → Release

禁止：
- 需求直接变成代码但没有 Task
- Task 直接 DONE 但没有 Acceptance Evidence
- 当前 Release 有 Requirement 却没有 Task
- Task 引用不存在的 Requirement/Goal
- CR 未经 Impact Review 就批准进入当前 Release
