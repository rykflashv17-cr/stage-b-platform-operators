# 共享包发布记录迁移到 Nexus Data

**Goal:** 将共享包记录迁到用户指定的 `https://nexus.striding.internal/repository/Data/operators/package-releases/<name>/<version>/release.json`，保留 wheel、版本、依赖锁和原始来源信息，按分支规范合入 main 后清理 Harbor 旧记录。

**Spec:** 本轮用户批准的迁移方案及路径修正。仅修改算子仓库；不新增仓库、CI 阶段或作业，不执行 main 的正式镜像发布。

**Architecture:** 共享包记录使用 Nexus HTTPS GET/PUT；只读与发布分别复用 Nexus 账号。保持每个版本一份 JSON，冲突拒绝覆盖，原始构建来源不因重试变化。仅 404 表示不存在，错误响应和畸形内容必须报错。沿用现有发布作业串行控制，不修改整个 Data 仓库策略。

## Global Constraints

- 原五份记录的包字段、Git 来源和 wheel 摘要不变；算子及共享包源码、所有依赖锁不变。
- MR 与非写入环节仅使用只读 Nexus 凭据；只有受保护 develop 发布记录。
- 保留所有镜像相关 Harbor 功能；只移除共享包记录的 Harbor 依赖。
- 先备份、迁移并回读，再切换三个长期分支；最后清理精确匹配的旧 Harbor 记录。
- 若旧 CI 仍可能写入记录，先完成或取消对应未启动的手动旧发布入口，再清理；不删除镜像、镜像回执或其他仓库内容。

## Review Focus

确认认证错误不会导致重新发布，网络失败重试不会覆盖冲突，记录读回校验及大小限制有效；确认包作业不再需要 Harbor/ORAS，镜像作业保持原行为；检查迁移的 wheel 和锁不变。Nexus 读写与 Harbor 清理由主执行者实际验收。

### Task 1: Nexus 发布记录实现与回归

Files: `ci/publish/package_records.py`、`ci/publish/packages.py`、`ci/plan/entry.py`、`ci/scripts/job.sh`、`tests/ci/`。

Interfaces: `PackageRecords(writable=False).read(name, version)` 返回原记录或 None；`create(record)` 仅写入端可用；`location(name, version)` 返回规范化的 Nexus URL。

- 编写真实本地 HTTP 边界测试，覆盖路径、只读/发布凭据、错误/超时/损坏/超大响应、重试、冲突及原来源保留。
- 替换 Harbor 存储并取消包专用的 ORAS 初始化；保留包版本计划和发布校验。
- 验证：`pytest -q tests/ci`；执行 CI 壳脚本边界验证；源码和锁差异为空。

Expected: 新测试先失败后通过；全部 CI 回归通过。

### Task 2: 记录迁移、文档、真实 CI 和清理

Files: README/AGENTS/CONTRACT、CI 和开发文档、`docs/PACKAGE_RELEASES.json`；本次操作证据保存在 `build/nexus-package-records/`。

Interfaces: 从现有五份 Harbor 记录恢复原始 JSON；Nexus URL 和 JSON 摘要写入历史快照；旧记录按 repository + digest 精确删除。

- 使用现有 CI 账号验证 Data 读取与发布，迁移五份记录并核对 Nexus wheel。
- 同步文档，独立评审一次；feature → develop → release → main 逐次验收。
- 确认旧运行任务不依赖 Harbor 包记录后删除五个旧制品，核对 Nexus 回读及 wheel 摘要，并跑删除后的主线自动检查。

Expected: main 自动检查通过、正式发布仍为手动；Harbor 包记录清理且所有现有依赖保持可用。
