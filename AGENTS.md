# 算子仓库 AI 工作约定

适用于本仓库全部目录。

1. **先确认任务和工作树。** 建分支前阅读[本仓分支规范](docs/BRANCHING.md)，常规功能从 `develop` 创建 `feature/<topic>`；固定 `release` 不带版本后缀。 查看 Git 状态及目标算子目录，保留用户已有修改。常规开发以本仓库的 [CONTRACT.md](CONTRACT.md) 为共同规范，先读[完整声明与字段索引](CONTRACT.md#算子目录与完整声明)。
2. **从需求确定契约。** 确认主要职责、数据来源、一次调用的范围、输入输出业务名、文件内容、失败或空结果语义及资源预算。无法从需求、现有声明或锁文件确定的值应明确列为待确认项。为下游需要的业务值和文件分别声明具名字段。文件数组在 `items` 的字符串上标记 `kind: ARTIFACT`，见[文件声明示例](CONTRACT.md#文件接入与传输边界)。命名和新增词汇按[命名与版本](CONTRACT.md#命名与版本)评审。
3. **按任务选参考。** 新算子从 [Python 模板](templates/python-operator/README.md) 起步。修改 MCAP 算子时先核对 [B01–B04 契约](docs/PRETRAINING_DATA_CLEANING.md)和[来源记录](docs/V4_SOURCE_PROVENANCE.json)。涉及构建或发布范围时读[开发与 CI](docs/DEVELOPMENT_AND_CI.md)。平台架构和 SDK 内部协议只在跨仓接口任务中进一步核对。
4. **按职责落盘。** 业务字段在算子目录的 `operator.yaml` 维护，平台注册 JSON 由工具生成。业务代码和专用 I/O 属于本算子，实际复用的能力进入独立共享包。改动 Dockerfile 时核对受控 Harbor 地址和精确版本，并以该版本依赖锁验证禁网构建。新增基础镜像时按[开发指引](docs/DEVELOPMENT_AND_CI.md#基础镜像的选择与开发)独立提交配方和 `VERSION`。通用 SDK 由独立 SDK 仓库维护，固定上游源码按来源记录核对。
5. **交付可复核的证据。** 对声明变更分别执行样例校验、注册转换和真实入口调用，核对业务输出、质检和错误行为。[验收命令](CONTRACT.md#注册与验收) 交付时列出修改文件、契约或版本影响、实际命令和结果。区分本地验证、容器验收与正式发布。所需 wheel、基础镜像或外部权限缺失时，记录受影响的验证范围。

6. **修改 CI 时按精确版本验证。** 文件影响规则集中在 `ci/plan/impact.py`。每个当前算子维护 `tests/source/`、`tests/wheel/`、`tests/container/`，通过 `ci.acceptance.versions` 在独立源码/wheel 环境执行；不以一个版本代表同编码的其他版本。`ci/release-versions.yaml` 可以列出同编码多个精确版本，兼容组合不会自动扩大其发布范围。依赖仅从 Nexus 读取，保留锁定摘要与构建隔离。

7. **CI 与业务测试分开。** 通用入口位于 `ci/plan/`、`ci/build/`、`ci/acceptance/`、`ci/publish/`；组合和业务断言属于 `tests/pipelines/<workflow>/`。计划只读 YAML/锁文件，不导入测试包。遵循 [CI 维护说明](ci/README.md)，保留各版本报告、父子流水线身份和真实失败验证。保护分支镜像交接使用现有 Harbor 临时标签及摘要，不上传镜像归档到 GitLab，不在发布作业重建。

Release 标题、说明与 Assets 遵循 [Release 编写规范](docs/RELEASE_NOTES.md)：标题含 Package/Operator、实际版本阶段及 `v` 前缀；Assets 只放下载文件。GitLab 原生 Evidence collection 不作为本仓库发布判断依据，不额外触发采集。

8. **共享包只留当前源码。** `packages/<project.name>/` 不建版本子目录或版本后缀目录；新共享包使用简短发行名与下划线导入名。已发布包变更须升版本，先构建依赖再更新消费方锁。旧算子锁定 wheel 从 Nexus 恢复，禁止批量改包名或重锁旧版本。共享包运行 `make package PACKAGE=<name>`；来源提交、源码和 wheel 摘要保存在 Nexus Data 的 `operators/package-releases/<name>/<version>/release.json` 中。包版本说明写入 `docs/releases/packages/<规范化发行名>/<版本>.md`；Nexus 回读验证后同步 `packages/<规范化发行名>/v<版本>` 标签与 Release。标签指向记录中的原提交，不能使用当前补登记提交；构建和发布判断仍读取 Nexus。算子正式 Git 标签为 `operators/<code>/v<version>`，不得移动已发布标签。见[共享包流程](docs/DEVELOPMENT_AND_CI.md#共享包与依赖边界)。

9. **算子只留当前源码。** 目录固定为 `operators/<code>/`，版本由声明和 pyproject 确定，逻辑键仍为 `<code>/v<version>`。禁止新增 `v*` 源码子目录。移除历史源码前，将仍参与发布/兼容测试的版本固定到 `ci/archived-versions.json` 的原镜像证据；当前源码、wheel 只验证当前版本，历史镜像继续参与精确兼容组合。不得重新计算旧声明或用新代码重建旧版本。develop/release/main 完成双仓交付、清单及回执后分别同步 Dev/RC/Stable 算子 Release；版本取实际交付镜像标签，复用候选时保留原标签。人工说明写入 `docs/releases/operators/<code>/<业务版本>.md`，使用“变更说明、兼容性、已知限制（按需）”，不手写自动来源和摘要。
