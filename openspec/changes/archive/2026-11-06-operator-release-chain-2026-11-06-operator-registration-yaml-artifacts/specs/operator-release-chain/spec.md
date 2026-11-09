# Operator Release Chain（算子发布物消费与租户注册）增量规范

## ADDED Requirements

### Requirement: 发布物 v4 携带 operator.yaml 工件引用

算子仓 CI 产出的发布物 MUST 升级为 schemaVersion 4：`components` 每行在既有五字段（code/buildVersion/digest/sourceCommit/repositories）之外新增第六字段 `operatorYaml: {repository, digest}`。`operatorYaml.repository` MUST 使用 `192.168.121.144/data/data-operators/definitions/{code}` 形态（Kargo 管理集群已实证可达的 registry 地址），`digest` MUST 钉扎该 yaml 工件的 sha256。tag 前缀与媒体类型 MUST 同步升为 `v4-` 与 `application/vnd.striding.data-platform.publication.v4+yaml`。无目录可引用的存量行（历史版本目录已删）MUST 原样保留且不带 `operatorYaml` 字段；compose MUST 对构建 commit 下仍存在目录的存量行补嵌引用。

#### Scenario: v4 行引用可拉取

- **WHEN** 发布物 v4 产出且 `mcap_stats_global` 最新行 buildVersion=4.0.1
- **THEN** 该行含 `operatorYaml{repository: 192.168.121.144/data/data-operators/definitions/mcap_stats_global, digest: sha256:…}`，按 repository@digest 拉取得到 operator.yaml 原文

#### Scenario: 目录已删的存量行不补引用

- **WHEN** compose 处理 buildVersion=2.0.0 的存量行且该版本目录在构建 commit 下不存在
- **THEN** 行原样复制，不含 `operatorYaml` 字段

### Requirement: definitions 独立仓托管 yaml 工件

算子仓 CI MUST 在每次算子版本构建时把该版本 `operator.yaml` 原文（不转 JSON）推送为 OCI 工件：仓库 `data/data-operators/definitions/{code}`、tag=该版本号、单 yaml 层、媒体类型 `application/vnd.striding.data-platform.operator-definition.v1+yaml`。镜像仓 `data/data-operators/{code}` MUST NOT 混入 yaml 工件。

#### Scenario: 每版本一个唯一路径

- **WHEN** `mcap_stats_global` 4.0.1 构建发布
- **THEN** definitions 仓出现 tag 4.0.1 的 yaml 工件，内容与 `operators/mcap_stats_global/v4.0.1/operator.yaml` 字节一致

### Requirement: 构建期版本一致性校验

算子仓 CI MUST 在构建时校验同一版本的四处版本声明一致：`operator.yaml` 的 `operatorVersion` == `pyproject.toml` 的 `project.version` == 版本目录名 `v{版本}` == 发布物行 `buildVersion`。不一致 MUST 使流水线失败（源头拦截）。

#### Scenario: yaml 版本写错构建即败

- **WHEN** 某版本 `operator.yaml` 写 `operatorVersion: 4.0.2` 而目录与 pyproject 均为 4.0.1
- **THEN** 算子仓 CI 流水线失败，发布物不产出

### Requirement: ops 链每码只注册最新版本（A1 模型）

ops 生成器 MUST 按 A1 模型展开注册步骤：每个事实源算子码取发布物 `filter(components, {#.code == 码})` 的最后一行（行数组按 (code, buildVersion) 升序，末行即最大 buildVersion）作为唯一注册对象。槽位展开（maxVersionsPerOperator）、版本探针（`GET /{code}/versions/{v}`）、版本已存在即跳过分支、算子仓 clone 步骤 MUST 全部移除；事实源 MUST 删除 `maxVersionsPerOperator` 与 `operatorsRepository` 字段（schema 与 loader 同步收口，算子仓 git secret 引用连带删除，存量配置含已删字段 MUST 拒收）。

#### Scenario: 同码多版本只注册最新

- **WHEN** 发布物含 `mcap_delivery_accepted` 2.0.0/4.0.0/4.0.1/5.0.0 四行
- **THEN** 注册链仅针对 5.0.0 行生成下载/解析/注册步骤

#### Scenario: 事实源遗留槽位配置被拒

- **WHEN** `operator-registration.yaml` 仍含 `maxVersionsPerOperator: 2`
- **THEN** 生成器校验失败并指明该字段已废除

### Requirement: yaml 引用经 oci-download + yaml-parse 消费

注册链 MUST 用与发布物自身相同的两步消化 `operatorYaml` 引用：`oci-download`（imageRef=行内 repository@digest，落 `./scratch/definitions/{code}.yaml`）→ `yaml-parse`（结构化输出）。注册 body 的内容字段 MUST 取自解析产物：`operatorVersion`/`name`/`description`/`ioDefinition`（inputs/outputs/parameters）来自 yaml；`type`/`functionType`/`executionScope` 来自 ops 事实源；`imageRef` = 镜像行 `repositories['local-harbor'].repository + "@" + 行.digest`（digest 钉扎，不用 yaml 的 tag 引用）；`upgradeDescription` = 行 `sourceCommit`；`executionConfig` = `{"cpu":{"numCpus": int(yaml.resources.requests.cpu)}}`。工件 cpu 为带引号字符串（K8s 资源惯用形制 `'2'`），平台 `executionConfig.cpu.numCpus` 仅收 JSON 数字（isNumber 校验），MUST 经 expr-lang 内置 `int()` 统一转换（数字/字符串双形制均成立，非法值表达式报错 fail-closed）。yaml 中 `stage`/`command` 平台无对应字段 MUST NOT 组装。

#### Scenario: body 组装字段源正确

- **WHEN** 注册某算子且 yaml `resources.requests.cpu` 为带引号字符串 `'2'`
- **THEN** 请求体 executionConfig 为 `{"cpu":{"numCpus":2}}`（int 转数字），imageRef 以 `@sha256:` 结尾

### Requirement: 注册按算子编码探针分派 create/update

每码 MUST 先执行唯一探针 `GET /{code}`（带 X-Tenant-Id 与登录 token）：HTTP 400 且业务码 10061001 判不存在 → 执行 `POST /operator/operators`（create，含 nodeKind=OPERATOR/artifactType=IMAGE/operatorCode）；HTTP 200 判存在 → 执行 `PUT /operator/operators/{code}`（update）。create 与 update 步骤互斥门控且互不引用对方 outputs。

#### Scenario: 新租户首跑全量 create

- **WHEN** 租户下无 `mcap_stats_global` 且探针返回 400+10061001
- **THEN** 仅 create 步骤执行，body 携 operatorCode=mcap_stats_global

### Requirement: 版本冲突由平台原生裁决且 Kargo 可见

注册链 MUST NOT 在 ops 侧做版本序比较或冲突预判；冲突语义完全委托平台：同版本同内容（hasVersionChanges 全等且 upgradeDescription 一致）→ 幂等 200，保障 Kargo 晋级重试安全；同版本异内容 → 平台 409 + 业务码 10061016 → 注册 http 步骤失败 → 晋级 Failed（Kargo UI 可见；2026-11-06 dev 实测四码冲突全 409 非 400）。create 撞已存在编码 → 409 + 10061019 同样晋级失败。旧版本插入平台不拒绝（合法历史行为），A1 模型 MUST NOT 产生旧版本注册。

#### Scenario: 同版本重跑幂等

- **WHEN** 晋级因后续步骤失败重试，已注册的 5.0.0 再次 PUT 且内容全同
- **THEN** 平台返回 200，注册步骤通过，晋级继续

#### Scenario: 同版本镜像变更冲突可见

- **WHEN** 算子 5.0.0 镜像重构建 digest 变化，链以新 imageRef PUT 同版本
- **THEN** 平台返回 409+10061016，注册步骤失败，晋级在 Kargo 显示 Failed

### Requirement: 注册前置 guard 集

注册步骤前 MUST 施加 guard，任一不满足即晋级失败：发布物 v4 行形状（含 `operatorYaml` 封闭字段集）；算子码双向闭包（发布物行码 ⊆ 事实源清单 且 事实源码 ⊆ 发布物行码）；最新行 MUST 含 `operatorYaml`（无引用=该码不可注册，fail-closed）；`yaml.code == 行.code`；`yaml.operatorVersion == 行.buildVersion`；`operatorYaml.repository` 前缀钉扎 `192.168.121.144/data/data-operators/definitions/{code}`。

#### Scenario: 最新行缺引用即失败

- **WHEN** 某码最新行因 CI 异常未带 `operatorYaml`
- **THEN** guard 步骤失败、晋级 Failed，不发起任何平台调用

### Requirement: enabled=false 零注册步骤与 v4 切换静默

事实源 `enabled: false` 时生成器 MUST 产出零注册步骤且 v4 pin（媒体类型/tag 正则/schemaVersion/行字段集）对主链生效。v3→v4 两侧合流顺序 MUST 任意安全：仅一侧升级时链不报错、仅静默无新 Freight，双侧就位后自动恢复晋级。

#### Scenario: CI 先行升级不致败

- **WHEN** 算子仓 CI 已产 v4 tag 而 ops 仍 pin v3
- **THEN** Warehouse 不发现新 Freight，既有链路无失败；ops MR 合入后恢复发现
