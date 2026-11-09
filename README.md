# Data Platform Operators · 算子开发指南

本仓库按算子编码维护当前版本的声明、实现与验收样例；历史版本通过精确镜像、Git 标签和发布记录追溯。开发者使用下面的流程。AI 从 [AGENTS.md](AGENTS.md) 开始。共同遵循[算子开发与注册规范](CONTRACT.md)。

按阶段开发时，先看[各阶段的特殊规范](CONTRACT.md#各阶段的特殊规范)。每个 `operators/<code>/` 只保留当前开发版本源码，已交付的多个版本仍可并存使用。[发布范围](ci/release-versions.yaml)列出允许进入 `release/main` 的精确版本，当前包括四个 `4.0.1` 和 B04 `5.0.0`；它不指定平台默认版本。分支流程见[分支管理](docs/BRANCHING.md)，交付格式与平台适配见[多版本发布清单](docs/RELEASE_MANIFEST.md)。实际发布及平台接入状态以相应回执为准。

## 第一次本地调用

需要 Python 3.12、uv 和公司 Nexus 的读取权限，或已缓存的锁定 SDK wheel 与第三方依赖。准备好环境后，从仓库根目录执行。[环境准备](docs/DEVELOPMENT_AND_CI.md#本地开发)

```bash
make dev OPERATOR=templates/python-operator
templates/python-operator/.venv/bin/python templates/python-operator/main.py \
  --local-request-file templates/python-operator/examples/local-request.json
```

请求中的 `record-001` 得分为 `0.8`，恰好达到默认阈值。成功后 `build/example-quality-gate/published/result.json` 中的 `decision.keep` 为 `true`，`quality_records.jsonl` 中有一条 `PASS` 明细。再次运行请使用新的工作目录。[可重复运行的模板示例](templates/python-operator/README.md#运行模板)

## 开发一个算子

| 步骤 | 操作 |
|---|---|
| 1. 确定身份 | 按业务职责选择编码、阶段和精确版本，创建 `operators/<code>/`。[命名词表](CONTRACT.md#命名与版本) · [阶段选择](CONTRACT.md#业务阶段) |
| 2. 复制模板 | 从模板复制受 Git 管理的文件，对齐目录、包名、镜像、入口和样例。[模板替换清单](templates/python-operator/README.md#用模板开发) |
| 3. 写声明 | 定义一次调用处理的数据、参数、输出和计算预算。业务值、单文件和文件数组按名称独立声明。文件仅在字符串上标记 `kind: ARTIFACT`。[业务数据声明](CONTRACT.md#业务数据声明) · [文件 I/O](CONTRACT.md#文件接入与传输边界) · [调用粒度](CONTRACT.md#调用粒度) · [资源与 GPU](CONTRACT.md#运行资源) |
| 4. 实现并验证 | 在算子目录的 `src/` 内实现计算与专用读写，运行真实入口，核对正常及异常场景，再生成注册 JSON。[镜像与执行](CONTRACT.md#镜像与执行) · [注册与验收](CONTRACT.md#注册与验收) |
| 5. 提交 MR | 列明算子编码、版本、I/O 变更和实际验证结果。在版本 Dockerfile 中选用已发布的精确基础镜像版本，新增独立算子还须接入容器验收。[选择或开发基础镜像](docs/DEVELOPMENT_AND_CI.md#基础镜像的选择与开发) · [版本级 CI](docs/DEVELOPMENT_AND_CI.md#按算子版本构建) · [基础镜像](images/README.md) |

## 算子与交接顺序

四个 MCAP 算子提供文件交接实例。当前源码为 B01–B03 `4.0.1`、B04 `5.0.0`。

| 顺序 | 声明 | 输出 |
|---|---|---|
| B01 | [MCAP 标准读取](operators/mcap_read_normalize/operator.yaml) | `normalized_batch_ref`（ZIP） |
| B02 | [MCAP 全局统计](operators/mcap_stats_global/operator.yaml) | `statistics_context`（ZIP） |
| B03 | [MCAP 质量检测](operators/mcap_quality_detect/operator.yaml) | `batch_quality_result`（ZIP） |
| B04 | [合格 MCAP 交付](operators/mcap_delivery_accepted/operator.yaml) | `archive`（ZIP） |

原始文件如何配对、每个 ZIP 包含什么，以及如何凭最终清单取回原件，见 [B01–B04 输入输出说明](docs/PRETRAINING_DATA_CLEANING.md#输入与输出)。

B04 5.0.0 根据质检结果下载通过项的原始 MCAP，输出含 `delivery_manifest.json` 的 ZIP。历史 B04 4.0.1 仍输出 JSON 清单，使用原镜像验收，不在当前树保留第二份源码。详见[B01–B04 版本差异](docs/PRETRAINING_DATA_CLEANING.md#输入与输出)与[历史版本管理](docs/DEVELOPMENT_AND_CI.md#算子源码与历史版本)。

CI 以 `<code>/v<version>` 为最小验证单元。普通源码 MR 执行源码与 wheel 检查，依赖或启动配置变化增加容器验收。版本隔离、Nexus 依赖与缓存说明见[开发与 CI](docs/DEVELOPMENT_AND_CI.md#构建与逐版本验证)。

每个当前算子维护 `tests/source/`、`tests/wheel/`、`tests/container/`。跨算子兼容组合在 `tests/pipelines/<workflow>/definition.yaml` 声明；独立算子无需加入预训练数据清洗流程。阶段、作业、报告与失败重试见 [CI 维护说明](ci/README.md)。

共享包在 `packages/<包名>/` 只维护当前源码，当前为 `pretrain-cleaning-common` 与 `pretrain-cleaning-engine`。独立测试后由 develop 发布到 Nexus，并同步 `packages/<规范化发行名>/v<版本>` 对应的 [GitLab Release](https://gitlab.striding.internal/stridingai/data/data-platform-operators/-/releases)。Release 提供版本说明、wheel、完整 Nexus 记录和“本包源码”下载入口；Nexus 保持发布权威，已有算子继续使用自己的锁定 wheel。[共享包开发、发布和历史追溯](docs/DEVELOPMENT_AND_CI.md#共享包与依赖边界)。

发布说明使用带类型、阶段和 `v` 版本的标题，例如 `[Package][Stable] pretrain-cleaning-common v0.4.3`。内容与下载资产按 [Release 编写规范](docs/RELEASE_NOTES.md) 维护。

算子在 develop、release、main 完成 Harbor/ACR 摘要核对、清单及交付回执后，分别同步 Dev、RC、Stable GitLab Release。标签为 `operators/<code>/v<实际镜像标签>`，例如 `operators/mcap_read_normalize/v4.0.1-dev.12345`；固定指向镜像原始构建提交。Release 提供版本说明、精确声明、单算子源码及版本发布清单。详见[Release 规范](docs/RELEASE_NOTES.md)。
