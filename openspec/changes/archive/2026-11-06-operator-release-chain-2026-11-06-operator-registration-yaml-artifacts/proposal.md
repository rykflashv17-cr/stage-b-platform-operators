# operator-registration-yaml-artifacts 需求提案

## 1. 业务背景

发布物 v3（累计多版本行）已上线，但注册信息（名称、输入输出定义、参数、资源配置）只存在于算子仓 `operators/{code}/v{版本}/operator.yaml`，发布物行不含。当前 ops 注册链为拿到 yaml 必须 clone 算子仓，带来三个实证问题：

1. **目录删除漂移**：发布物是累计镜像库存（行只增不减），仓库只保留当前源码目录。真实现状：最新发布物 12 行中 7 行的版本目录已删除，无 yaml 可组装。
2. **槽位模型失效**：刚合并的 `maxVersionsPerOperator: 2` 面对真实 12 行（delivery_accepted 已 4 版）直接 fail-closed，且随累计增长无限超限。
3. **启用成本**：clone 依赖算子仓 git secret（cred-type=git 标签）与管理集群到 GitLab 的连通性，是启用清单里最重的两项。

用户拍板：operator.yaml 作为**独立 OCI 工件**按版本唯一路径发布，发布物行放**引用**（不内嵌内容）；注册按**算子编码唯一**判断——不存在则 create、存在则 update（每码只注册最新版本）；同版本异内容=版本冲突=直接异常，必须在 Kargo 上可见。

## 2. 需求范围

**做：**

- **算子仓 CI（data-platform-operators）**：发布物升 v4——每行新增 `operatorYaml: {repository, digest}` 引用；构建时把 operator.yaml 原文推成 OCI 工件到独立 definitions 仓（`data/data-operators/definitions/{code}`，tag=版本）；构建期校验 `yaml.operatorVersion == pyproject.version == 目录名`；compose 对现存目录的存量行补嵌引用。
- **ops 链（data-platform-ops）**：v4 pin（schemaVersion/媒体类型/tag 正则）；注册模型改 A1——每码取 `filter(code)` 最后一行（最大 buildVersion），oci-download+yaml-parse 消费引用（与发布物自身消化同轨）；删除槽位展开、版本探针（`GET /versions/{v}`）、skip 分支、clone 步骤；body 组装改 yaml 字段源 + `executionConfig` 换算（`resources.requests.cpu → cpu.numCpus`）；guard 集（yaml.code==行.code、yaml.operatorVersion==行.buildVersion、引用仓库前缀钉扎）。
- **冲突语义**：完全由平台原生裁决——同版本同内容→幂等 200（Kargo 重试安全）；同版本异内容→400+10061016 → http 步骤失败→晋级 Failed（Kargo 可见）。旧版本插入平台不拒绝（累计模型设计行为），A1 模型天然不触发。

**不做：**

- 平台（data-platform Java）零改动。
- dev 存量手工注册行不清理（首跑可能逐码撞冲突，用户拍板"失败就失败"）。
- 不注册非最新版本（历史版本只是镜像库存史）。

## 3. 影响范围

- 受影响的 Maven 模块：**无**（本变更不动 Java；平台冲突幂等语义已存在于 develop `OperatorManagementService.update()`，10_061_016/10_061_019）
- 受影响的包/类：
  - `data-platform-operators`：`ci/plan/compose.py`（行组装+补嵌）、`ci/plan/pipeline.py`/构建发布物处（v4 字段+definitions 工件推送）、版本一致性校验
  - `data-platform-ops`：`bootstrap/kargo_operators_bootstrap.py`（注册步骤重构）、`delivery/kargo/operator-registration.yaml`（删 maxVersionsPerOperator）、`bootstrap/schemas/kargo-operator-registration.schema.json`、`tests/test_kargo_operators_bootstrap.py`、`delivery/kargo/desired/prod-gitops-01/*`（重渲染）
- 受影响的对外接口：算子注册 API（POST/PUT /operator/operators）调用方式变化，契约本身不变
- 兼容性说明：
  - 发布物 v3→v4 升版：行字段集封闭校验，加字段必须升 schemaVersion/tag 前缀/媒体类型；旧 v3 tag 留在 registry 作历史
  - 切换顺序任意安全：CI 先出 v4 → v3 盯梢的 Warehouse 静默（不失败）；ops 先合 v4 → 找不到 v4 tag 同样静默；两侧合流后自动恢复晋级
  - definitions 仓引用使用 `192.168.121.144/...` 形态（与 publicationRepositories 同源，Kargo 管理集群已实证可达）

## 4. 复杂度评估

- 复杂度：中等（两仓协同 + Kargo expr 静态步骤重构，但全部沿用已实证机制：oci-download/yaml-parse/filter/map）
- 是否新增数据表：否

## 5. 全局强制约束

1. 本变更无数据库改动；ops 侧维持"事实源 MR 即变更"与 desired 文件可重渲染幂等
2. 凭据不落仓：登录凭据仍走 SealedSecret（不变）；definitions 工件走 Harbor 匿名拉取（与发布物同策略，待实证 data-operators 项目匿名策略）
3. Kargo 硬约束：步骤静态无循环（≤128 步/Stage）、expr ASCII、skipped 步骤无 outputs；`filter(...)[len(...)-1]` 动态索引与 filter/map 语义须 27 探针实证后才可 enabled=true
4. 本地提交不推送；推送/MR 用户自操作

## 6. 验收标准

1. 算子仓 CI 产 v4 发布物：行含 `operatorYaml{repository,digest}`，definitions 仓出现对应版本 yaml 工件，构建期版本不一致即流水线失败
2. ops 生成器（enabled=false）渲染 desired 零注册步骤、v4 pin 生效；单测全绿（含 v4 行形状守卫、A1 行选择、guard 表达式、maxVersionsPerOperator 拒收存量配置）
3. enabled=true 时生成的步骤序列：每码 = 下载 yaml→解析→双 guard→探针→create|update，无 clone/槽位/版本探针/skip
4. 幂等与冲突（dev 实测）：同版本同内容重跑→200；同版本异内容→步骤失败、晋级 Failed 在 Kargo UI 可见、错误码 10_061_016
5. 27 探针 Stage 实证 `filter/map` 与动态索引 `[len(...)-1]` 语义
