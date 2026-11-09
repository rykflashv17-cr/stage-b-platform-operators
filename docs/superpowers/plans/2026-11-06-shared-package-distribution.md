# 共享包单份源码与 Nexus 发布实施计划

> 标签追溯部分已由 [算子标签统一与共享包追溯迁移](2026-11-06-operator-release-tags.md) 取代：共享包不再使用 Git 标签，按发布记录中的提交和路径追溯。以下保留原实施计划。

**Goal:** `packages/` 每个发行包只维护当前开发源码；旧算子从 Nexus 获取原名、原版本、原摘要的 wheel；新共享包使用 `pretrain-cleaning-common` 和 `pretrain-cleaning-engine`。

**Architecture:** 精确依赖和 wheel 摘要仍由消费方 `pyproject.toml`、`uv.lock` 维护。当前源码匹配锁定版本时可构建；其他版本必须从 Nexus 获取并验摘要。包测试独立于算子，保护分支发布已测试 wheel 并创建 `packages/<project.name>/v<project.version>` 标签；重试不得覆盖不同内容。

**Tech Stack:** Python 3.12、uv、pytest、GitLab 动态子流水线、Nexus PyPI hosted。

**Spec:** 本次用户批准的单份当前源码、简化包名、包级历史标签、保留旧算子依赖及仅修改算子仓库的要求。

## Global Constraints

- 已有算子版本目录、业务契约、依赖身份和锁定摘要保持不变；旧包先归档至 Nexus，再移除重复源码。
- 新发行名及导入名分别为 `pretrain-cleaning-common` / `pretrain_cleaning_common`、`pretrain-cleaning-engine` / `pretrain_cleaning_engine`。新发行身份沿用当前数值版本 0.4.3、0.1.0，不覆盖旧发行名。
- `_vendor/` 上游文件内容不变，只移动所属模块目录。
- Nexus 是唯一依赖入口；凭据不进入源码、命令行或日志。发布失败可重试，相同版本不同字节必须失败。
- MR 只构建和测试，不发布包、不创建标签；保护分支 develop 发布经过隔离验证的包。release/main 检查同版本内容，不覆盖、不重建已发布包。
- 标签指向构建来源提交，注释包含源码目录、wheel 文件名、摘要和 Nexus 地址。标签已存在时核对内容，不移动。
- 旧算子无需引用新包名；新算子或新算子版本显式选择新包及版本。

## Review Focus

历史依赖从源码转为制品后是否漏装；源码/wheel 环境是否串用模块；没有算子消费者的共享包能否被 CI 独立验证；发布重试是否使用旧报告或未验证 wheel；同名旧版本是否误选当前源码；标签和 Nexus 字节是否一致；第三方源码是否被误改。

### Task 1: 锁定制品读取与历史归档

Files: `ci/build/dependency_wheels.py`、`ci/publish/packages.py`、`tests/ci/test_shared_packages.py`、历史发布记录。

Interfaces: 从锁文件读取路径 wheel 身份与摘要；统一下载/验摘要；Nexus 幂等发布与包标签。

1. RED：缺失制品、摘要冲突、错误包元数据、历史版本选择、发布重试回归。
2. GREEN：实现读取与验证；验证旧 wheel 与原源码及历史构建回执一致。
3. 将旧 common 0.4.2/0.4.3、engine 0.1.0 归档到 Nexus，读取验证并创建来源标签。

Expected: 旧依赖可仅凭锁文件从 Nexus 获取，不依赖旧源码目录。

### Task 2: 当前包整理与隔离验收

Files: `packages/`、`ci/plan/projects.py`、`ci/build/{environment,packages,context}.py`、`ci/acceptance/`、相关测试与来源路径记录。

Interfaces: 当前源码仅一个版本；未匹配版本按精确锁从 Nexus 读取；包本身有独立 source/wheel 测试入口。

1. RED：当前源码唯一性、历史算子依赖集合、无消费者包验收与导入隔离。
2. GREEN：迁移并改名当前 common/engine，旧算子依赖保持原样；构建流程下载历史 wheel。
3. 构建新包，锁定新依赖；验证新包及所有历史算子版本源码/wheel 和完整流程。

Expected: `packages/` 仅两个当前源码目录；旧模块来自原 wheel，新模块来自独立包。

### Task 3: CI 包发布与追溯

Files: `ci/plan/{checks,pipeline,impact}.py`、`ci/{run,runtime}.py`、`ci/gitlab/`、`ci/publish/packages.py`、CI 测试。

Interfaces: 检查计划包含共享包；构建一次，测试后向 Nexus 发布同一 wheel；标签关联精确提交。

1. RED：包变更选择、文档跳过、MR 禁止发布、保护分支发布、失败/冲突重试。
2. GREEN：增加共享包独立测试和发布作业，复用既有构建与报告机制。
3. 实际验证 Nexus 新包及标签；核对 GitLab CI 配置、权限及执行结果。

Expected: 修改未被旧算子消费的新共享包，不重建旧算子镜像；失败不影响已发布历史依赖。

### Task 4: 开发说明与最终验收

Files: `README.md`、`AGENTS.md`、`CONTRACT.md`、`docs/DEVELOPMENT_AND_CI.md`、`ci/README.md`、模板及相关开发说明。

1. 说明新包开发、版本更新、Nexus 消费、历史源码查找和旧依赖保留规则。
2. 运行 CI 回归、全部版本隔离验证、兼容组合及容器验证。
3. 独立审查全部变更，修复实质问题，记录 Nexus、Git 标签和实际验收结果。

Expected: 文档与可执行入口一致，交付包含旧依赖兼容和新包发布证据。
