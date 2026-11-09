# operator-registration-yaml-artifacts 开发任务清单

（非 Java 变更：产者=data-platform-operators Python CI，消费者=data-platform-ops Python 生成器；任务序即依赖序）

## 1. 算子仓 CI（产者侧）

- [x] 1.1 `ci/plan/checks.py`（或同层）：构建期版本一致性校验——`operator.yaml.operatorVersion == pyproject.project.version == 目录名 v{版本}`，不一致抛错使流水线失败
- [x] 1.2 definitions 工件推送：每版本构建时把 `operator.yaml` 原文推 OCI 工件至 `data/data-operators/definitions/{code}`、tag=版本、媒体类型 `application/vnd.striding.data-platform.operator-definition.v1+yaml`
- [x] 1.3 `ci/plan/compose.py`：行组装升 v4——新行带 `operatorYaml{repository,digest}`（repository 用 `192.168.121.144/…` 形态）；存量行在构建 commit 下仍有目录的补嵌引用，无目录的原样带过
- [x] 1.4 发布物外壳升版：schemaVersion 4、tag 前缀 `v4-`、媒体类型 `publication.v4+yaml`
- [x] 1.5 dev 实证：触发一次流水线，验 definitions 仓工件可匿名拉取、v4 发布物行引用可 `repository@digest` 解析（2026-11-06 首份 v4 `…p12394-cc9aa25…`：13 行累计带全、四码最新行全带 operatorYaml、definitions 四仓匿名可拉、行钉 digest 解析 200 媒体类型正确）

## 2. ops 生成器（消费者侧）

- [x] 2.1 事实源与 schema：`operator-registration.yaml` 删 `maxVersionsPerOperator` + 注释改 A1 语义；`kargo-operator-registration.schema.json` 删字段并重算 sha256 钉扎；loader 拒收存量字段
- [x] 2.2 v4 pin：`_PUBLICATION_LAYER_MEDIA_TYPE` 升 v4、warehouse tag 正则 `^v4-`、发布物守卫 schemaVersion==4、行封闭字段集扩为六字段（`operatorYaml` 可选存在但最新行必含）
- [x] 2.3 删槽位机制：`_registration_slot_steps`/版本探针/skip 分支/算子仓 clone 步骤与 git secret 引用整体移除
- [x] 2.4 A1 行选择与下载解析：每码 `filter(…)[len(…)-1]` 取最新行；`oci-download`（行.operatorYaml.repository@digest → `./scratch/definitions/{code}.yaml`）+ `yaml-parse`（输出 `definition_{code}`）
- [x] 2.5 guard 集：最新行必含 operatorYaml（形状+封闭集）、repository 前缀钉扎、`definition.code==行.code`、`definition.operatorVersion==行.buildVersion`、`type(definition.resources.requests.cpu)=="float"`
- [x] 2.6 注册步骤：每租户 login + 每码 probe（`200 || (400 && body.code==10061001)`）→ create（!exists，含 nodeKind/artifactType/operatorCode）/update（exists）互斥门控
- [x] 2.7 body 组装：字段源按 design 3.4 表（operatorVersion/name/description/ioDefinition/executionConfig 取 yaml，imageRef=行 digest 钉扎，upgradeDescription=行 sourceCommit，平台属性取事实源）

## 3. 测试（ops 单测，Python unittest）

- [x] 3.1 v4 pin 断言更新（媒体类型/正则/守卫六字段/`operatorYaml` 形状）
- [x] 3.2 槽位用例改写为 A1：同码四行只生成最新行步骤；`maxVersionsPerOperator` 存量配置拒收
- [x] 3.3 新 guard 断言：缺 operatorYaml 的最新行 fail-closed、前缀不匹配、code/版本不一致
- [x] 3.4 步骤序列断言：无 clone/无版本探针/无 skip；create/update 互斥门控；body 字段源正确（executionConfig 原生数值、imageRef digest 钉扎）
- [x] 3.5 desired 重渲染比对：enabled=false 零注册步骤；三线 v4 pin diff 符合预期、project/projectconfig 零漂移
- [x] 3.6 全量回归：`python -m unittest tests.test_kargo_operators_bootstrap -v` 全绿 + 既有模块抽测无新失败

## 4. 验证与收尾

- [x] 4.1 27 探针 Stage：实证 `filter/map`、动态索引 `[len(…)-1]`、`type(…)=="object"` 表达式语义（2026-11-06 probe-expr-8 八项守卫全 Succeeded：filter+动态索引取末行、`in` map 判键、整数以 float 复活 `type()=="float"`、`||` 短路、封闭键集 `all(keys,…)`、`map()`、字符串拼接；另实证谓词内 `#` 即元素、`components[#]` 求值即错=守卫闭包修复依据）
- [x] 4.2 dev 链端到端（enabled=true 手工渲染演练）：新租户 create 全量 / 既有租户同版本重跑幂等 200 / 同版本异内容冲突晋级 Failed 可见（2026-11-06 凭据路径受阻改工作台复刻等价链四码全实证：异内容 PUT=409+10061016×4（订正原 400 预期）、DB 备份清墓碑后 create 200×4 逐字段 ALL-MATCH、同内容重放 PUT 幂等 200×4、改动 upgradeDescription 重放=409 冲突可见；备份 8.141.105.6:/root/backup-t-operator-*-20261106.json）
- [x] 4.3 ops MR 文档同步：operator-registration.yaml 启用清单删"算子仓 git secret"项；dev 存量手工行首跑冲突预案注明
- [x] 4.4 本地提交（两仓各自同名分支），推送/MR 用户自操作
