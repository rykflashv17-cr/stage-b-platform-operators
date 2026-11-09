# operator-registration-yaml-artifacts 测试用例规范

## 基础测试框架

ops 侧：Python `unittest`（tests/test_kargo_operators_bootstrap.py，生成器纯函数断言）；产者侧：算子仓 CI 自测 + dev 流水线实证。无 Java/JUnit。

## 强制测试约束

1. 生成器三类场景全覆盖：正常渲染 / 配置非法（拒收）/ fail-closed 守卫触发
2. 断言字符串拼装用既有 `_and`/`_J` 辅助形态（含双括号），勿裸串硬编码
3. schema sha256 钉扎改动必须同步重算断言
4. 每条用例可追溯 tasks.md 任务项（尾注 [T x.x]）

## 业务测试场景清单

### 生成器单测（正常）

| 场景 | 类型 | 前置条件 | 输入 | 预期结果 |
| --- | --- | --- | --- | --- |
| v4 pin 生效 | 正常 | enabled=false | 任意事实源 | 媒体类型 `publication.v4+yaml`、warehouse 正则 `^v4-{line}-`、守卫 schemaVersion==4 [T2.2] |
| A1 行选择 | 正常 | 发布物同码 4 行（2.0.0/4.0.0/4.0.1/5.0.0） | 4 码事实源 | 每码仅 `filter(…)[len(…)-1]`（5.0.0）行生成步骤 [T2.4] |
| 步骤序列 | 正常 | enabled=true | 单租户 4 码 | login×1 + 每码 download/parse/guard/probe/create/update；无 clone、无 `GET /versions/{v}`、无 skip 分支 [T2.3/2.6] |
| create/update 互斥 | 正常 | enabled=true | probe 输出 exists | create 门控含 `!(…exists)`、update 门控含 `…exists`，互不引用对方 outputs [T2.6] |
| body 字段源 | 正常 | yaml 含 resources.requests.cpu=2 | 组装表达式 | executionConfig=`{"cpu":{"numCpus":2}}` 原生数值；imageRef 以 `@sha256:` 结尾；operatorVersion 取 `definition.operatorVersion`；upgradeDescription=行 sourceCommit [T2.7] |
| 零注册步骤 | 正常 | enabled=false | desired 重渲染 | 注册段零步骤；三线 v4 pin diff、project/projectconfig 零漂移 [T3.5] |

### 生成器单测（配置非法/守卫 fail-closed）

| 场景 | 类型 | 前置条件 | 输入 | 预期结果 |
| --- | --- | --- | --- | --- |
| 存量槽位配置拒收 | 配置非法 | schema 已删字段 | `maxVersionsPerOperator: 2` | 校验失败并指明字段已废除 [T2.1] |
| 最新行缺 operatorYaml | 业务异常 | 守卫表达式 | 行无第六字段 | guard fail 步骤触发（fail-closed），不生成平台调用步骤 [T2.5] |
| 引用前缀不匹配 | 业务异常 | 行 operatorYaml.repository 非钉扎前缀 | 守卫表达式 | guard 触发 [T2.5] |
| yaml.code ≠ 行.code | 业务异常 | 解析输出与行码不一致 | 守卫表达式 | guard 触发 [T2.5] |
| yaml.operatorVersion ≠ 行.buildVersion | 业务异常 | 版本漂移 | 守卫表达式 | guard 触发 [T2.5] |
| resources.requests.cpu 缺失/非数值 | 业务异常 | yaml 无 requests 段 | 守卫表达式 | `type(…)=="float"` 断言触发 [T2.5] |
| 行字段集封闭校验 | 业务异常 | 行出现未知第七字段 | 发布物守卫 | 拒收 [T2.2] |

### 产者侧（算子仓 CI）

| 场景 | 类型 | 前置条件 | 输入 | 预期结果 |
| --- | --- | --- | --- | --- |
| 版本一致性校验 | 业务异常 | yaml operatorVersion≠pyproject | 构建 | 流水线失败、无发布物 [T1.1] |
| definitions 工件推送 | 正常 | 正常构建 | 版本发布 | definitions 仓 tag=版本、内容与 operator.yaml 字节一致 [T1.2] |
| 存量行补嵌 | 正常 | 4.0.1 目录存在 | 首次 v4 compose | 该行获得 operatorYaml 引用；无目录行原样 [T1.3] |
| 匿名拉取 | 正常 | dev Harbor | 无凭据 GET blob | 工件可拉（registry 匿名策略实证）[T1.5] |

### 链上端到端（dev，enabled=true 演练）

| 场景 | 类型 | 前置条件 | 输入 | 预期结果 |
| --- | --- | --- | --- | --- |
| 新租户全量 create | 正常 | 租户无该算子 | 探针 400+10061001 | POST 200，版本=最新行 [T4.2] |
| 同版本重跑幂等 | 正常 | 已注册同内容 | 重放 update | 200，晋级通过（Kargo 重试安全）[T4.2] |
| 同版本异内容冲突 | 业务异常 | 已注册同版本不同 digest | update | 400+10061016，步骤失败，Kargo 晋级 Failed 可见 [T4.2] |
| dev 存量手工行冲突 | 业务异常 | 手工 tag 形 imageRef 存量行 | 首跑 | 同版本冲突失败（拍板接受），记录待手工清理清单 [T4.3] |
