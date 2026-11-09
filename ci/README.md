# CI 维护说明

CI 面向所有算子和基础镜像；业务流程只在 `tests/pipelines/<workflow>/` 声明。开发命令见[开发与 CI](../docs/DEVELOPMENT_AND_CI.md)，算子开发规范见 [CONTRACT.md](../CONTRACT.md)。

## 文件归属

```text
.gitlab-ci.yml                # 分支与父流水线入口
ci/
  gitlab/                    # common、plan、build、test、publish 模板
  plan/                      # 发现、影响规则、证据冻结和子流水线生成
  build/                     # SDK/wheel 准备、构建上下文、镜像构建
  acceptance/                # 精确版本环境、容器启动、计数与报告
  publish/                   # Harbor 发布、ACR 同步、外部 v4 清单
  registry/                  # GitLab 只读接口、Harbor OCI 记录及镜像操作
  base_images/               # 配方发现与基础镜像构建/验收/发布
  scripts/                   # Runner 环境和固定入口
  tooling/                   # 通用测试依赖锁
  release-versions.yaml       # 允许正式发布的精确版本列表
  run.py                     # 子作业入口
  runtime.py                 # 计划身份、实际作业尝试与报告消费
images/                      # runtime / builder 配方及各自测试
operators/<code>/tests/{source,wheel,container}/
packages/<package>/tests/source/
tests/ci/                    # 通用 CI 的回归测试
tests/pipelines/<workflow>/  # 兼容组合和业务断言
tools/                       # 开发者使用的声明检查和注册导出
```

新增 CI 文件须登记 `plan/impact.py`；归属、检查选择和构建摘要使用同一规则。未知构建/执行路径明确失败。共享包以规范化发行名和版本唯一定位，每个消费闭包只选取一个兼容版本。

本仓模块边界由包元数据发现，不维护业务包白名单。固定上游包的额外导入目录按[共享包与依赖边界](../docs/DEVELOPMENT_AND_CI.md#共享包与依赖边界)声明；普通算子或共享包无需增加此配置。

## 阶段与作业

| 阶段 | 作业 | 边界 |
|---|---|---|
| plan | `plan-changes` | 冻结计划、基础回执、已接受镜像及测试定义；生成 `child.yml`。纯文档 MR 不安装依赖。 |
| build | `run-jobs` | 父流水线触发并等待唯一子流水线，使用 `strategy: mirror`。 |
| build | `build-packages` | 一次准备所需版本、当前共享包、模板和组合环境的 wheel；历史共享 wheel 从 Nexus 下载并验摘要。 |
| build | `build-operator-images:<code>/v<version>`、`build-base-images:<配方>/<版本>` | 保护分支每个构建输入只构建一次；暂存现有 Harbor 仓库。 |
| test | `test-packages:<name>` | 当前共享包独立源码/wheel 环境验收，包括无算子消费者的包。 |
| publish-packages | `publish-packages` | 仅 protected develop：校验最新构建和测试报告，发布并核验 Nexus wheel/来源记录，再同步包 GitLab 标签和 Release；不使用 Harbor 保存包记录。 |
| test | `test-operators:<code>/v<version>` | 合并环境准备；源码与 wheel 仍分别运行、分别报告。源码阶段使用本版本 SDK 校验声明及样例。 |
| test | `test-operator-images:<版本>`、`test-pipelines:<组合>` | 拉取精确摘要；具备相同环境且明确许可时可合并。 |
| test | `check-images:<组>`、`check-base-images:<配方>/<版本>` | 风险 MR 的本地构建与验收，不写 Harbor/ACR；失败时重试整个组。 |
| test | `check-code`、`test-base-images:<配方>/<版本>` | 必要工具/模板检查、基础镜像运行验收。 |
| publish-images | `publish-operator-images`、`publish-base-images:<配方>/<版本>` | 仅发布已通过验收的内容，不构建。main 算子发布为阻塞式手动作业。 |
| mirror-images | `mirror-operator-images` | 按摘要同步至 ACR 公网，核对 Harbor 与 ACR 公网回读。 |
| publish-manifest | `publish-operator-manifest` | 更新外部 v4 清单及逐版本交付回执，再同步对应 Dev/RC/Stable GitLab Release。 |

没有工作的计划只生成 `check-plan`。作业依赖使用 `needs`；不要通过复制分支专属发布作业扩展流程。

`develop` 按全部版本的交付状态规划候选；固定 `release` 和 `main` 按 `ci/release-versions.yaml` 规划发布，并展开显式兼容组合。组合中的其他版本仅辅助测试，不自动创建 rc/正式标签或加入发布清单。各版本分别计算构建及验收依据；已满足要求的版本复用，不因范围包含多个版本而全部重建。

## 交接与复用

镜像不作为 GitLab 归档上传。保护分支构建先推送 `<版本>-ci.<父流水线ID>.<构建作业ID>`，后续按 `repository@sha256:…` 拉取。所有标签均位于现有 Harbor `data` 项目；正式/候选标签只有测试通过后才创建。MR 在同一作业内保留构建镜像供验收。

| 产物 | GitLab 路径 |
|---|---|
| 冻结计划与子配置 | `build/plan.json`、`build/child.yml` |
| wheel 与包清单 | `build/wheels/`、`build/packages/<job_id>/packages.json` |
| 版本构建报告 | `build/operators/<code>/<version>/build/<job_id>/build.json` |
| 版本检查 | `build/operators/<code>/<version>/test/<job_id>/<source或wheel或container>/acceptance.json` |
| 组合检查 | `build/pipelines/<workflow>/<组合>/test/<job_id>/acceptance.json` |
| 基础镜像报告 | `build/base-images/<配方>/<版本>/<build或test或publish>/<job_id>/` |
| Harbor、ACR、交付报告 | `build/release/<job_id>/{harbor,acr,release}.json` |
| 对外清单与回执 | `build/release/{manifest.yaml,receipt.json,delivery.json}` |

报告关联计划摘要、父子流水线、逻辑作业和实际尝试 ID。GitLab 产物保留 30 天；镜像的长期构建、验收及交付依据作为小型 OCI 制品存入现有 Harbor 仓库。仅复用受保护分支且源码/依赖/基础摘要、测试定义和镜像摘要完全匹配的记录。权限、网络和摘要异常不视为正常未命中。

共享包的来源记录使用 `https://nexus.striding.internal/repository/Data/operators/package-releases/<name>/<version>/release.json`，由 `ci/publish/package_records.py` 通过 HTTPS 读取和写入。包发布作业沿用 Nexus 认证及串行控制，不准备 ORAS/Harbor；普通包 MR 仅查询 Nexus。先确认 wheel 摘要再保存记录；记录缺失可恢复，同版本不同源码或 wheel 拒绝覆盖，重试保留原来源。`ci/publish/package_releases.py` 随后用 `CI_JOB_TOKEN` 创建或补齐包 Release，发布报告的 `gitlab_releases` 保存同步入口；标签始终绑定 Nexus 原提交。GitLab 同步失败使作业失败，重试不回滚 Nexus；仅缺 Release 时可使用独立补登记命令，不改变 Nexus planning。详见[同步与恢复](../docs/DEVELOPMENT_AND_CI.md#gitlab-release-与补登记)。

缓存不是证据。`build-packages` 写入工具 uv 缓存；逐版本检查使用各版本的锁定缓存；镜像构建使用校验过的 wheel 下载缓存。禁止把 `.venv` 或裸标签作为跨作业交接依据。

## 兼容组合与测试归属

版本容器检查自带固定输入。共享包回归在实际消费版本的源码环境内运行，不混装同名包的多个版本。流程声明只有数据，规划时不导入测试、SDK 或业务模块。

```yaml
requirements:
  platform: linux/amd64
  runner_tags:
    - software
  environment: tests/pipelines/example/uv.lock
combinations:
  current:
    versions:
      read: example_read_data/v1.0.0
      write: example_write_data/v2.0.0
    tests:
      - test_pipeline.py
    parameters: {}
container_checks: {}
```

组合可用 `environment` 指定本流程目录内另一份 `uv.lock`；本仓依赖须在相邻 `pyproject.toml` 以精确 wheel 路径声明。计划根据该锁准备依赖，报告绑定本次 wheel 生产作业。共享测试数据在顶层 `fixtures` 列出并参与测试摘要。新增独立算子不必创建组合；已有流程的新版本必须声明兼容组合，不能自动替换后假定兼容。

## 失败与重试

- 子测试失败或取消，父流水线不会成功，发布不能启动；保留该尝试的日志和分项报告。
- 测试、发布、同步和清单失败可单独重试，读取最新成功的生产作业并校验摘要，不调用构建。
- 同一消费尝试不得混用生产作业重试前后的报告。上游构建重试后，相关测试也须重新通过。
- 重试触发作业产生的新子流水线不执行既有计划；启动新父流水线重新规划。计划/产物过期、镜像或冻结记录缺失、交付基线已推进时同样重新规划。
- `develop` 任一版本的 ACR 同步失败仍保持未完成状态；下次父流水线复用已验收 Harbor 摘要并补同步，不要求该版本先进入正式发布范围。
- main 的手动发布未执行时，同步及清单保持阻塞；不会因已有历史通过记录自动正式发布。

CI 机制变更除本地回归和 CI Lint 外，须执行真实 GitLab 的失败、取消、手动门禁、生产作业重试、触发器重建和仅同步/清单重试验证。演练不得用测试替身发布正式镜像。验收方法与历史接口实证见 [CI 调整方案](../docs/superpowers/plans/2026-11-04-operator-ci-adjustment.md)。

## 多版本发布

Release 标题按资源类型和实际版本阶段生成，采用 `[Package|Operator][Dev|RC|Stable] <名称> v<版本>`；Python alpha/beta 使用 Alpha/Beta。它与镜像标签、分支名分别管理。Release notes 和 Assets 遵循[编写规范](../docs/RELEASE_NOTES.md)，不主动采集 GitLab Release evidence。

固定分支流程为 `develop → release → main`，算子版本独立于分支名。发布范围、累计快照、首次 v2 历史恢复和平台消费规则见[发布清单规范](../docs/RELEASE_MANIFEST.md)。主流程仅写 v4；历史 v2/v3 字节及旧 `release/v*` 记录用于读取核验，不作为新写入入口。

共享包发布与历史源码：`packages/` 每个规范化发行名仅一个当前版本。发布输入包含源码路径及摘要，Nexus 同版本不同 wheel 拒绝覆盖。来源记录在 Nexus Data，通过包名和版本定位；GitLab Release 是展示与下载入口，不作为发布判断依据。release/main 核对 Nexus 记录，不重复构建已发布包。未完成的 develop 包发布可重试对应作业；后续 develop 计划会补齐缺失的 Nexus 记录，仅缺 Release 时使用补登记入口。详见[开发指引](../docs/DEVELOPMENT_AND_CI.md#共享包与依赖边界)。

## 当前源码与历史镜像

项目发现仅接受 `operators/<code>/pyproject.toml`，并核对同目录声明编码及版本；历史 `v*` 源码子目录会报错。当前源码的构建、独立环境和报告仍以 `<code>/v<version>` 区分。

`ci/archived-versions.json` 固定已移除源码但仍参与交付或组合的历史构建/测试引用；`ci.plan.history` 校验原 GitLab 来源、镜像与原始声明。历史节点不生成 source/wheel/build 作业，仍参加显式兼容组合。引用或校验代码变化触发历史容器/组合检查，缺记录不回退当前源码。

`ci.publish.operator_sources` 从镜像原始提交读取声明，支持原版本目录和当前平铺目录；CI 将原始字节发布为内容摘要标记的 OCI 制品，清单 `operatorYaml` 按 digest 引用。不得用当前分支重建旧声明。

`ci.publish.gitlab_releases` 复用包/算子 Release 的幂等 API 处理；`ci.publish.operator_releases` 在 develop/release/main 已完成清单交付后调用，分别创建 Dev/RC/Stable Release；使用计划中的实际交付标签，不重新推导候选流水线编号。Release 失败不回滚已有产物；重试沿用冻结记录及原提交。原作业失效后，`python -m ci.publish.operator_releases` 通过 `--branch develop|release|main` 按指定分支快照与固定交付回执补登记，仅写 GitLab。详见[开发与恢复](../docs/DEVELOPMENT_AND_CI.md#算子-release-同步与恢复)。
