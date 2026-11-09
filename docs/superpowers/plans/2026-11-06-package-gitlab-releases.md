# 共享包 GitLab Release 同步

依据：用户确认的 Nexus 权威记录与 GitLab Release 展示方案。只修改算子仓库，不修改 wheel、依赖锁或业务源码。

## 设计约束

- Nexus `pypi-internal` 保存 wheel，Data 保存完整发布记录；构建、安装及发布计划仍以 Nexus 为准。
- 每包每版本对应 `packages/<规范化发行名>/v<版本>` 带注释标签与 Release，标签固定指向 Nexus 原 `source_commit`。
- 现有 protected develop `publish-packages` 在 Nexus 发布、回读成功后同步 Release；不新增阶段或作业。
- 同步内容：安装命令、源提交和包目录、精确 wheel 与摘要、Nexus 记录、原构建及验收流水线。版本说明单独维护，不从全仓提交猜测包的功能变化。
- 默认源码归档是全仓；增加使用原提交和 `source_path` 限定的“本包源码”资产。
- 同步幂等；错误标签或冲突元数据报错，不移动标签、不覆盖 Nexus 内容。失败可重试原作业或从 Nexus 单独补登记；历史补登记不构建。
- 包标签不触发流水线。自动化仅管理自己的说明区块和三个下载链接，保留人工说明及其他链接。

### Task 1: Release 同步与回归测试

接口：有效 Nexus 记录 → GitLab Release；`publish_packages` 使用 `store.create()` 返回的原记录。手动补登记入口只读取 Nexus，验证 wheel 后调用同一同步逻辑。

1. 写 HTTP 边界和发布集成测试并观察失败：原提交、包路径、幂等、缺失补齐、冲突、权限和重试。
2. 实现有界、禁止重定向的 GitLab 写入客户端，CI 使用 `CI_JOB_TOKEN`；本地补登记可显式指定 Token 文件。
3. 验证标签、Release 和下载链接回读；Nexus 发布成功后才写 GitLab。
4. 运行全部 CI 测试及现有仓库测试入口。

Expected: 新行为测试先失败后通过；源码、wheel、依赖锁未变，Nexus planning 不依赖 GitLab Release。

### Task 2: 文档与独立评审

接口：代码的同步和恢复行为 → 开发、分支、CI 文档。

更新 README、AGENTS、CONTRACT、开发指引、CI 说明和模板；移除“共享包不创建标签”的现行规定。说明 notes 维护、认证、权限、默认全仓与包源码的区别、失败恢复、历史记录缺少独立测试报告时的边界。完成一次独立整分支评审并修复重要问题。

Expected: 文档与实际入口一致，当前规范无相互冲突的共享包标签或 Harbor 来源记录规则。

### Task 3: GitLab 实际验收与合入

接口：本地通过的功能分支 → develop → 固定 release → main。

创建 MR、验证真实 CI_JOB_TOKEN 同步权限，补登记现有五个 Nexus 版本，核对标签原提交、三个下载链接、包限定源码内容及重复同步无额外写入。保持算子标签及 Nexus 内容不变。按分支规范合入；main 正式镜像发布手动作业不执行。

Expected: 相关流水线通过；五个包 Release 可访问，包标签无新流水线，原 wheel/发布记录摘要不变。

## Review Focus

检查原记录与当前重试身份混淆、旧路径源码链接、Release 资产冲突、部分写入恢复、错误标签不可变、CI_JOB_TOKEN 实际权限、手动 notes 保留及 Nexus 权威边界。不要将历史补登记时间误报为原包发布时间。

### Task 4: 标题、说明和 Assets 规范（用户追加）

在最终推进 release/main 前完成：标题统一 `[Package|Operator][阶段] <名称> v<版本>`，阶段按实际版本识别 Dev/RC/Stable，Python alpha/beta 单独标识。已知旧标题无损升级，源提交、标签、资产、原生 evidence 不变。共享包说明按变更/兼容性分段，自动区块维持 Nexus 事实。明确 Package 的三个资产，以及 Operator 的声明、注册 JSON 和已有发布清单；镜像地址、命令、网页链接放正文。

核验 GitLab 原生 Evidence collection 的自动采集行为。本仓不主动追加采集，不以其取代 Nexus 或镜像交付记录；不虚构 released_at 隐藏原生区块。先观察标题/迁移测试失败再实现，运行完整 CI 测试、增量 MR、更新现有九个 Release 展示并回读；已有快照和下载内容不得改写。

Expected: 类型、版本阶段及 v 前缀明确；当前五包、四算子标题一致；只改发布展示与文档，不改实际版本、依赖锁或业务代码。
