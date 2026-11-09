# 算子 Git 标签统一与共享包追溯迁移

> 共享包记录的 Harbor 存储已由 [Nexus Data 迁移方案](2026-11-06-nexus-package-records.md) 替代；本文保留首次标签迁移的历史设计。

> 使用 executing-plans 实施；只修改算子仓库及其已授权的发布引用。

**Goal:** 补齐四个 `operators/<code>/v2.0.0` 标签及发布说明，删除原 `v2.0.0` 和全部五个共享包 Git 标签，确保 CI 和历史依赖继续可用。

**Architecture:** 算子正式标签指向原始构建提交。共享 wheel 继续存放 Nexus，包来源记录复用现有 Harbor CI record 存储，由包名与版本定位；共享包 CI 不再读写 Git 标签。先迁移记录并验证，再切换 CI，最后删除旧 Git 引用。

**Tech Stack:** Python、pytest、GitLab、Nexus、现有 Harbor/ORAS CI record。

**Spec:** 本次用户明确要求补齐四个算子标签、删除原全局及共享包标签、更新命名规范且只修改算子仓库。

## Global Constraints

- 四个算子 2.0.0 标签均指向 `6178584085290adb7b1b8b7836ee02bee6e316d1`，原镜像摘要和验收证据不变。
- 删除五个共享包标签不删除 Nexus wheel、不修改算子源码和依赖锁。
- 包记录保存源码提交、路径、源码摘要、wheel 摘要及原作业身份；同版本不同内容拒绝覆盖。
- MR 不发布，受保护 develop 发布包及记录；release/main 只复用已发布记录。
- 保留现有 GitLab 发布说明和资产链接，备份后才删除旧 Release/标签。
- 未运行的旧 main 手动发布不启动；本轮不正式发布算子镜像。

## Review Focus

记录缺失与权限失败不得混淆；旧标签删除后 CI 不应重建包或重建标签；包重试不得覆盖不同 wheel/源码；MR 包变更须具备记录读取路径；迁移不得改变历史源码、镜像或 wheel。

### Task 1: 无共享包 Git 标签的 CI 发布记录

Files: `ci/publish/package_records.py`、`ci/plan/{entry,packages,impact}.py`、`ci/publish/packages.py`、`ci/scripts/job.sh`、相关 CI 测试。

Interfaces: `PackageRecords(registry).read(name, version)` 返回已验证记录或 None；`create(record)` 幂等写入；`validate_record(record, expected)` 校验精确身份与不可变输入。

- [ ] RED：记录读写、冲突、重试、畸形记录、release/main 复用、MR 包读取和发布作业认证回归。
- [ ] GREEN：替换 `PackageTags`，沿用 Harbor 记录格式和认证，不增加项目或保存 wheel 副本。
- [ ] 验证：`pytest -q tests/ci` 通过；源码及锁文件差异为空。

### Task 2: 历史发布迁移与命名规范

Files: README、AGENTS、CONTRACT、分支及开发/CI 指引、历史发布记录；管理脚本只保存于本次 build 目录。

Interfaces: 保存六个旧 Git 标签和三个 GitLab Releases；新算子标签带各自镜像摘要、声明路径、原发布来源；共享包记录由 Task 1 的格式写入并回读。

- [ ] 导出并备份原标签/Release，核对五份 wheel、四份历史声明及镜像摘要来源。
- [ ] 创建四个精确标签和 Release；迁移五份包记录到现有 Harbor，核对回读。
- [ ] 文档统一：Git 标签仅使用 `operators/<code>/v<version>`；包以 Nexus 版本和来源记录定位源码。
- [ ] 独立审查与真实 GitLab MR 验收；按 develop → release → main 推进必要 CI 修复。
- [ ] 删除原 Release 和六个旧 Git 标签，复核只剩四个算子标签；以不依赖旧标签的计划验证和真实主线检查收尾。

Expected: Git 标签整洁且 CI 无回归；历史发布及依赖仍可按提交和摘要追溯。
