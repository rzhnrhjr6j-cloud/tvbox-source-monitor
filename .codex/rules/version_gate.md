# Version Gates

每个 Release 都有自己的需求基线、任务集合、验收标准和代码版本。

规则：
- 当前 Release 的新增功能必须走 Change Request。
- Bug 修复可以在冻结 Release 内进行，但必须保留回归证据。
- Release 进入 VERIFYING 后，默认不再加入新功能。
- RELEASED 必须有匹配 Git tag。
- Stable Baseline 与 Development Baseline 必须明确区分。
