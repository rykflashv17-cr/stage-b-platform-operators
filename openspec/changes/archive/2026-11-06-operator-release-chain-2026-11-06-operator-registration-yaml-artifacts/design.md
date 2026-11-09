# operator-registration-yaml-artifacts 技术设计

## 一、数据库设计

无（平台零改动；冲突/幂等语义已存在于 develop `OperatorManagementService.update()`：同版本同内容→幂等 200，同版本异内容→`OPERATOR_VERSION_CONFLICT`(10_061_016)；create 撞码→`OPERATOR_CODE_CONFLICT`(10_061_019)；查无→400+`OPERATOR_NOT_FOUND`(10061001)。比较面=`hasVersionChanges`：executionScope/executionMode/type/functionType/imageRef/functionConfig/executionConfig/ioDefinition，后三 JSON 语义比较；幂等条件另含 upgradeDescription 相等）。

## 二、改动文件结构（非 Java；两仓协同）

```
data-platform-operators（算子仓 CI，产者侧）
├─ ci/plan/compose.py        行组装：v4 字段集 + operatorYaml 引用；存量行有目录则补嵌
├─ ci/plan/pipeline.py       构建发布物：schemaVersion 4 / tag ^v4- / 媒体类型 v4+yaml
├─ ci/plan/images.py(或同层) definitions 工件推送（yaml 原文 OCI 工件）
└─ ci/plan/checks.py(或同层) 构建期版本一致性校验（yaml==pyproject==目录==buildVersion）

data-platform-ops（Kargo 链，消费者侧）
├─ bootstrap/kargo_operators_bootstrap.py   注册段重构（本文档主体）
├─ delivery/kargo/operator-registration.yaml 删 maxVersionsPerOperator + 注释更新
├─ bootstrap/schemas/kargo-operator-registration.schema.json 删字段+重算 sha256 钉扎
├─ tests/test_kargo_operators_bootstrap.py   槽位用例改 A1 + 新 guard 断言
└─ delivery/kargo/desired/prod-gitops-01/*   重渲染（三线 warehouse/steps 有 diff，属预期）
```

## 三、链路设计

### 3.1 发布物 v4 与 definitions 工件

```yaml
# 发布物（schemaVersion: 4，tag v4-{line}-p[0-9]{20}-[0-9a-f]{12}，
# 媒体类型 application/vnd.striding.data-platform.publication.v4+yaml）
components:
- code: mcap_stats_global
  buildVersion: 4.0.1
  digest: sha256:2cdad11a…              # 镜像
  sourceCommit: 9fb2195…
  repositories: {local-harbor: {repository, role: canonical, verifiedAt}}
  operatorYaml:                          # 新增第六字段（存量无目录行不带）
    repository: 192.168.121.144/data/data-operators/definitions/mcap_stats_global
    digest: sha256:<yaml 工件 digest>    # oci-download imageRef = repository@digest
```

definitions 工件：仓库 `data/data-operators/definitions/{code}`，tag=版本号，单层 operator.yaml 原文，媒体类型 `application/vnd.striding.data-platform.operator-definition.v1+yaml`。**不转 JSON**——链上用 yaml-parse 消化（与发布物同轨）。仓库地址用 `192.168.121.144/…` 形态（与 publicationRepositories 同源；管理集群对该地址的 oci-download 已实证）。

### 3.2 A1 行选择（每码最新）

```
rows   = filter(publication.components, {#.code == CODE})     # (code,buildVersion) 升序
latest = rows[len(rows) - 1]                                  # 末行=最大 buildVersion
```

- 生产者排序保证末行即最新；`len`/`filter` 已在 27 探针清单，**动态索引 `[len(…)-1]` 为新增探针项**。
- 槽位/版本探针/skip/clone 全删；事实源删 `maxVersionsPerOperator`（loader 拒收存量字段，防误配）。

### 3.3 每码步骤序列（enabled=true；每码 5 步 + 每租户 1 登录）

```
login（每租户一次，secret() 读凭据 → accessToken 输出）
└─ 逐码：
   1 oci-download   imageRef=行.operatorYaml.repository@digest
                    → ./scratch/definitions/{code}.yaml
   2 yaml-parse     → 输出 definition_{code}
   3 guard(合并为一条 fail 步骤，表达式取反触发)：
       行形状：type(operatorYaml)=="object" 且封闭字段集 {repository,digest}
       引用钉扎：repository == "192.168.121.144/data/data-operators/definitions/"+code
       内容一致：definition.code == 行.code
                 definition.operatorVersion == 行.buildVersion
   4 probe   GET /operator/operators/{code}（X-Tenant-Id、Bearer token）
             成功判定 (200 || (400 && body.code==10061001))，输出 exists
   5 create  POST /operator/operators        门控 !exists（仅此一支）
   6 update  PUT /operator/operators/{code}  门控 exists
            create/update 互斥、互不引用对方 outputs（无 absent-output 风险）
```

步骤量：4 码 × 5 步 + 登录 ≈ 21 步/租户，远低于 128 上限。

### 3.4 注册 body 组装（字段源唯一化）

| 请求字段 | 来源 |
|---|---|
| operatorVersion | `definition.operatorVersion`（yaml；guard 已保证 == 行.buildVersion） |
| name / description | yaml |
| ioDefinition | yaml inputs/outputs/parameters（原样对象） |
| executionConfig | `{"cpu":{"numCpus": definition.resources.requests.cpu}}`（yaml-parse 原生数值） |
| imageRef | 行 `repositories['local-harbor'].repository + "@" + 行.digest`（digest 钉扎；弃 yaml 的 tag 引用） |
| upgradeDescription | 行 `sourceCommit`（ASCII，绕 _cap_output 限制） |
| type / functionType / executionScope | ops 事实源（yaml 不携带平台属性） |
| nodeKind=OPERATOR / artifactType=IMAGE / operatorCode=code | 仅 create 携带（DTO 封闭 record，勿多字段） |

### 3.5 冲突/幂等（平台原生，ops 零逻辑）

- 重试安全：同版本同内容（含 upgradeDescription 相同）→ 200。
- 冲突可见：同版本异内容（镜像重建 digest 变、yaml 内容变）→ 400+10061016 → 步骤失败 → 晋级 Failed。
- dev 存量手工行（tag 形 imageRef）首跑同版本必撞 → 失败可接受（拍板），逐码手工清理后再跑。

### 3.6 v4 切换顺序（任意安全）

- CI 先行：v3 盯梢 Warehouse 不再发现新 Freight（静默停滞）；ops MR 合入后恢复。
- ops 先行：无 v4 tag 同样静默。
- 旧 v3 tag 留存 registry 作历史，不迁移。

## 四、依赖与中间件

- Harbor：definitions 项目/仓库需可被 CI 推送凭据创建（或项目预建）；匿名拉取策略待实证（release-publications 匿名已实证，同 Harbor 实例）。
- Kargo 已实证机制复用：oci-download / yaml-parse / fail / http；expr 已实证：`==`、`len`、`&&/||` 短路、filter/map（27 探针待跑）。
- 27 探针新增项：`filter/map` 语义、动态索引 `rows[len(rows)-1]`、`type(…)=="object"`。
- SealedSecret 登录凭据三件（不变）；算子仓 git secret **取消**（clone 已删）。

## 五、风险与待确认问题

1. **最新行无 operatorYaml 引用**（CI 异常或手工发布物）→ guard fail-closed，整链停在该码——符合预期但需运维知悉。
2. Harbor definitions 仓推送凭据的项目权限与匿名 pull 策略未实证——CI 落地前在 dev Harbor 验证。
3. dev 四算子手工存量行首跑冲突（已拍板接受）；test/prod 首跑应全量 create，无此问题。
4. `resources.requests.cpu` 若某算子 yaml 缺 requests 段 → guard 需覆盖该形状（实现时补 `type(definition.resources.requests.cpu)=="float"` 断言）。
5. 发布物行数随累计增长（当前 12 行/4 码），A1 与 guard 均按码取行，规模无感；仅发布物体积缓慢增长，暂不治理。
