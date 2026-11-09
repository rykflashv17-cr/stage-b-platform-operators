# 算子开发与注册规范

本规范适用于 `data-platform-operators` 中的算子。当前源码只放在 `operators/<code>/`，不设置版本子目录。每个版本的 `operator.yaml` 是身份、业务输入输出、参数和执行配置的维护入口。生成可注册的声明前，须确认业务职责、一次调用的范围、输入输出格式、空结果语义和资源预算。需求中无法确定的值应列为待确认项。标为“示例”的名称、阈值、文件格式和资源数量只属于该示例。

## 算子目录与完整声明

```text
operators/<code>/
├── operator.yaml              # 本版本声明
├── main.py                    # 绑定本声明和算子类
├── src/<module>/              # 本算子的业务代码和专用 I/O
├── tests/
│   ├── source/                # 源码、契约和本版本业务回归
│   ├── wheel/                 # 仓库外安装后的入口与包检查
│   └── container/             # 真实镜像成功/非法输入；自带固定测试数据
├── examples/
│   ├── inputs/<name>/{valid,invalid}/
│   ├── outputs/<name>/{valid,invalid}/
│   └── parameters/{valid,invalid}/
├── pyproject.toml             # Python 发行包、版本和依赖
├── uv.lock                    # 本版本依赖锁
└── Dockerfile                 # 使用本版本的构建上下文
```

下面是仓库模板的完整声明。模板镜像名用于示例，正式算子的仓库前缀见[镜像与执行](#镜像与执行)。[模板源文件](templates/python-operator/operator.yaml)

```yaml
contractVersion: '2'
code: record_quality_gate
name: 质量门禁
description: 对一条带质量分数的记录，按有效阈值生成保留结论
operatorVersion: '1.0.0'
stage: quality
batchMode: none
image: data-platform-operators/record_quality_gate:1.0.0
command: ["python", "/app/main.py"]
inputs:
  - name: record
    description: 待判定记录
    type: object
    required:
      - record_id
      - quality_score
    properties:
      record_id:
        type: string
        minLength: 1
      quality_score:
        type: number
        minimum: 0
        maximum: 1
outputs:
  - name: decision
    description: record_id 保持输入值；keep 等于 quality_score 大于或等于有效 threshold
    type: object
    required:
      - record_id
      - keep
    properties:
      record_id:
        type: string
        minLength: 1
      keep:
        type: boolean
parameters:
  - name: threshold
    description: 分数达到此阈值时保留记录
    type: number
    minimum: 0
    maximum: 1
    default: 0.8
resources:
  requests:
    cpu: '1'
    memory: '1Gi'
  limits:
    cpu: '2'
    memory: '2Gi'
```

传入 `record = {"record_id": "r1", "quality_score": 0.9}`，省略 `threshold`，业务输出为 `decision = {"record_id": "r1", "keep": true}`，标准质检明细为 `PASS`。同一输入显式传 `threshold = 0.95` 时，输出 `keep: false`，明细为 `REJECT`，两次调用均正常完成。

| 顶层字段 | 类型与必填性 | 填写规则 |
|---|---|---|
| `contractVersion` | 字符串，必填 | 固定为 `'2'` |
| `code`、`name`、`description` | 字符串，均必填 | 稳定编码、非空展示名称、非空用途说明。[命名与版本](#命名与版本) |
| `operatorVersion` | 字符串，必填 | 本仓库可发布的版本采用 `X.Y.Z` |
| `stage` | 字符串，必填 | 六个小写枚举值之一。[业务阶段](#业务阶段) |
| `batchMode` | 字符串，可省略 | 默认 `none`。[调用粒度](#调用粒度) |
| `delivery` | 对象，可省略 | 仅 delivery 阶段使用，类型与固定输出名必须配对。[交付阶段要求](#delivery交付) |
| `inputs`、`outputs` | 数组，均必填 | 一项对应一个业务名称，无对应数据时填 `[]`。[业务数据声明](#业务数据声明) |
| `parameters` | 数组，可省略 | 省略等同于无参数。每项分别定义类型与默认值 |
| `resources` | 对象，必填 | 同时提供申请量和上限，无默认配额。[运行资源](#运行资源) |
| `image`、`command` | 字符串、字符串数组，均必填 | 精确镜像标签与非空启动参数数组。[镜像与执行](#镜像与执行) |

仅使用表中的顶层字段，按原拼写填写。业务输入名、输出名、参数名使用小写 `snake_case`。声明字段和 JSON Schema 关键字分别保留 `batchMode`、`minLength` 等原名。

## 命名与版本

`code` 使用 `{domain}_{function}_{detail}`，三部分均须有业务含义。首字符为小写字母，单词仅含小写字母、数字，用单个下划线分隔。格式检查为 `[a-z][a-z0-9]*(_[a-z0-9]+){2,}` 全串匹配。一个语义部分可有多个单词，例如 `point_cloud_noise_filter` 拆为 `point_cloud / noise / filter`。

| 部分 | 选词依据 | 现有词汇及含义 |
|---|---|---|
| `domain` | 算子长期服务的数据对象。上游文件后缀、采集来源和团队名称不足以决定领域 | `mcap` MCAP 采集及其衍生数据、`point_cloud` 点云、`record` 通用结构化记录、`bev_occ` BEV/OCC 专项数据 |
| `function` | 算子的主要功能类别，优先复用已有拼写 | `read` 读取与解码、`stats` 统计计算、`quality` 质量判定、`delivery` 交付、`noise` 噪声处理、`prelabel` 预标注 |
| `detail` | 同类功能内的具体动作、范围或目标，随 `function` 选用 | `read/normalize` 读取并标准化、`stats/global` 全局统计、`quality/detect` 检测、`quality/gate` 门禁、`delivery/accepted` 交付通过项、`noise/filter` 去噪、`prelabel/infer` 推理预标注 |

`mcap_stats_global` 能明确区分领域、功能和范围。同一能力适配多个采集来源时，可沿用编码，在输入或参数中区分来源。编码中不拼接版本、团队、执行后端或批量模式。示例 `mcap_read` 缺少第三部分，会被格式校验拒绝。`mcap_process_data` 虽满足字符格式，却无法区分职责。格式由工具校验，语义由评审确认。

新增领域或功能类别时，在同一 MR 的词表中补充适用范围及组合示例。新增 `detail` 时写明它与已有同类算子的区别。词表供命名评审使用。已发布的 `code` 是引用身份，重新命名应按新编码登记并说明与旧编码的关系。展示名称 `name` 供人阅读，例如“MCAP 全局统计”。

本仓库接受三段数字算子版本，例如 `4.0.0`。目录名只使用算子编码，版本由声明确定；`operatorVersion`、`pyproject.toml` 的项目版本和声明镜像的标签均为 `4.0.0`。已正式发布的版本不可覆盖，算法、契约或运行依赖变化时用新版本。CI 候选标签如 `4.0.0-dev.11757` 标识候选产物，同时用于对应的 Dev Release；release 候选使用 `4.0.0-rc.<流水线编号>` 和 RC Release。Python 发行包、导入模块和类各有自己的命名，必须与构建文件和入口引用一致。

算子正式 Git 标签使用 `operators/<code>/v<version>`，指向该版本镜像的原始构建提交。共享包使用 `packages/<规范化发行名>/v<版本>` 标签及 GitLab Release 提供源码与下载入口；Nexus 仍是包和来源记录的权威存储。详见[Git 标签规范](docs/BRANCHING.md#git-标签)。

## 业务阶段

按主要职责选一个 `stage`。名称中的 `quality` 等词不决定运行义务。标准质检上报以声明的 `stage` 为准。

| `stage` | 何时选择 | 对业务契约的要求 |
|---|---|---|
| `ingest` | 接入外部采集数据 | 文件接入类必须包含具名输入 `raw_files`，可另有输入。注明文件选择、配对、空列表规则。 |
| `preprocess` | 准备后续计算所需数据 | 声明数据及处理结果，例如归一化批次或全局统计参数。 |
| `postprocess` | 整理已有计算结果 | 说明如何将已有结果变成目标输出，例如逐帧检测合并为事件区间。 |
| `quality` | 判定对象或整次调用的质量 | 成功时必须提交一次标准质检报告。下游需要的判定另声明为业务输出。 |
| `annotation` | 创建或更新标注 | 明确标注对应的对象或区间，定义标签和位置等字段。 |
| `delivery` | 向消费方交付处理结果 | 读取上游业务判定；采用通用交付协议时声明 delivery 及固定文件输出，见[阶段特殊规范](#各阶段的特殊规范)。 |

其他阶段可声明具名文件输入；采用已授权通用存储下载时使用 SDK 运行环境绑定，不把业务来源对象变为 SDK 类型。任何阶段都应校验业务数据，违反输入契约时抛出执行异常。

## 各阶段的特殊规范

按 `stage` 叠加以下要求。`batchMode` 独立决定调用粒度，`delivery.type` 只区分交付方式。各阶段共用具名值与文件接口，业务专属字段由精确算子版本维护。

### ingest：原始文件接入

文件接入类必须声明 `raw_files`，文件数组在字符串叶子标记 `kind: ARTIFACT`。说明选择顺序、配对、重复、缺失及空选择规则；需要追溯原件时保留稳定来源与字节摘要。MCAP 的 source_ref 只属于该业务契约，不作为所有算子的 SDK 类型。

### preprocess：预处理

说明输入与处理后成员的对应关系，以及适用的单位、坐标系、时间顺序和过滤规则。共享统计需明确适用范围。需要独立绑定的数据分别声明，无统一必填输出名。

### postprocess：后处理

说明既有结果经排序、合并、筛选或聚合后的对应关系和完整性条件。跨组处理明确本次范围，不把部分成功当作完整结果。输出格式按业务定义。

### quality：质检

成功执行恰好调用一次 `ctx.quality.report_batch(...)` 或 `report_items(...)`。下游需要的判定与来源另行声明业务输出；SDK 保留的 quality.json、quality_records.jsonl 不写入 outputs。正常拒绝可成功返回，输入、计算或传输错误须抛异常。其他阶段禁止调用标准质检上报接口。

### annotation：标注

说明标注与对象、帧或区间的关联，定义标签、位置、单位及有效范围，并说明空标注和修订语义。不同模态使用各自业务格式，无统一必填输出名。

### delivery：交付

平台按通用交付协议消费时，根层声明 delivery，并显式声明固定文件输出。只有 stage=delivery 可以使用此字段；省略时所有输出按普通业务结果处理。

| delivery | 必需输出（根 string/ARTIFACT） | 清单位置 |
|---|---|---|
| `{type: archive, format: zip}` | `archive` | ZIP 根目录 delivery_manifest.json，无外置清单输出 |

当前只定义 archive，delivery 恰好为 {type: archive, format: zip}，其他类型不定义；拒绝未知类型、额外字段、自定义输出映射及独立 delivery_manifest 输出。固定输出不能是 VALUE、数组或对象内路径。其他普通输出不自动加入交付。类型及内容契约随精确版本固定，节点不可覆盖。

清单格式为 delivery_manifest_v1，根对象只有 format 和 files。每项必须有相对文件路径 delivery_path、非负整数字节数 size_bytes、64 位小写十六进制 sha256，可选 business 对象存放业务字段。通用字段不可移入 business。files 按 delivery_path 的 UTF-8 字节序排序；禁止重复路径、路径越界、文件/目录冲突，且不列清单自身。

archive 中除根清单外的实际成员必须与 files 完全一致，逐文件核验原字节大小与摘要，禁止额外文件和符号链接。合法零文件输出仅含空清单的 ZIP；错误不得伪装成空交付。

交付算子使用上游业务判定，不重复上报标准质检。平台及上层流程提供存储上下文与权限；算子负责业务选择、来源和内容校验。使用 `ctx.outputs.file` 提交文件，通用 upload 仅传输文件。SDK dev9 已实现声明校验与转换透传，平台实际消费须另行接入。B04 的具体实现见[归档交付与历史版本](#归档交付与历史版本)。

## 调用粒度

| `batchMode` | 一次调用处理范围 | 节点配置与当前注册入口 |
|---|---|---|
| `none`（默认） | 按本次调度的数据单元逐项调用。一次可有多个具名输入，其中也可有数组 | 注册为 `DATA` |
| `all` | 汇集本批所需的数据后调用一次 | 注册为 `BATCH` |
| `chunk` | 按组调用，一批可有多次调用 | 节点必须配置正整数 `batchSize`。SDK 可校验此声明。当前仓库注册命令只转换 `none` 和 `all`，会拒绝 `chunk` |

节点继承算子版本的 `stage` 和 `batchMode`。`batchSize` 仅属于 `chunk` 节点。算子内部并发数等算法选项通过 `parameters` 提供。

## 业务数据声明

`inputs` 描述一次调用处理的数据，`outputs` 描述下游可消费的业务结果，`parameters` 描述调整算法行为的选项，例如阈值。`name` 是绑定和 SDK 调用使用的字段名，同组内唯一。Pipeline 的输入名与参数名也必须能区分。每个具名字段只声明一个完整的业务值，集合使用数组。

### 类型与缺省值

每项必须填写一个 `type`，采用 JSON Schema 2020-12 的下列类型和常用约束。YAML 中 `inputs`、`outputs`、`parameters` 用数组，对象的 `properties` 用字段名为键的映射。

| `type` | 描述数据的方法 |
|---|---|
| `string` | 用 `minLength`、`maxLength`、`pattern` 等限定文本。固定候选用 `enum` |
| `integer`、`number` | 整数或数值。用 `minimum`、`maximum`、`exclusiveMinimum`、`exclusiveMaximum`、`multipleOf` 等表达范围和步长 |
| `boolean` | 使用 YAML 布尔值 `true`、`false` |
| `object` | 在 `properties` 中逐项说明字段类型，在本对象的 `required` 中列必填成员。缺省允许额外成员 |
| `array` | 必须在 `items` 声明元素结构。可用 `minItems`、`maxItems`、`uniqueItems` 限定集合 |

| 情况 | 规则 |
|---|---|
| 整个输入缺失 | 输入默认必需，`optional: true` 才可省略。给定时校验完整值。 |
| 对象成员缺失 | 只按该对象自己的 `required` 判断。顶层输入可选与内部字段必填可同时成立。 |
| 参数缺失 | 参数默认可省略。先应用该参数根部的 `default`，再检查 `optional: false` 的参数是否存在。对象默认值按整体应用。未知参数名应拒绝。 |
| 已传 `null` 或空值 | 六种类型均不接受显式 `null`。`0`、`false`、空字符串、空数组按给定值校验。例如 `minItems: 1` 会拒绝空数组。 |
| 输出 | 每次成功执行须写齐所有声明输出。无业务输出时声明 `outputs: []`。输出不使用 `optional`。 |

字段的 `description` 用于说明单位、范围和业务含义。默认值仅可用于参数根部。设计要导入平台的声明时，优先使用表中的常用约束。SDK 可校验 `additionalProperties: false` 和 `oneOf`，但当前注册转换不接受这两个内联关键字。跨输入关系、对象覆盖和文件内容由业务代码校验。

### 文件接入与传输边界

`type` 描述算子使用的业务值，`kind` 标识它的传输方式。省略 `kind` 时按 `VALUE` 处理。参数只接受值。文件只在 `type: string` 节点上标记 `kind: ARTIFACT`。单文件标记具名字段，文件数组标记 `items` 链末端的字符串。对象成员中的路径字符串只作为业务文本传递。供下游读取的文件须单独声明具名输入或输出。

| 要交接的数据 | 声明位置 | 调用数据示例 | 算子读写接口 |
|---|---|---|---|
| 普通字符串、数字、对象或数组 | 对应的 `type`，省略 `kind` | `"pending"` 或 `{"count": 2}` | `ctx.inputs.value(name)` / `ctx.outputs.write_json(name, value)` |
| 单个文件 | `type: string`，同层标记 `kind: ARTIFACT` | 一个文件键，如 `"capture/a.mcap"` | `ctx.inputs.file(name)` / `ctx.outputs.file(name, path)` |
| 文件数组 | `type: array`。在 `items` 内声明 `type: string`、`kind: ARTIFACT` | 文件键数组，如 `["capture/a.mcap", "capture/b.mcap"]` | `ctx.inputs.files(name)` / `ctx.outputs.files(name, paths)`，保持成员顺序 |

例如，文件接入算子读取至少一个原始文件并交付一份清单时，可使用以下输入输出片段。对文件来源有要求时，按逻辑键及授权来源配对。输出文件应先写入本次工作目录，再按声明名提交。

```yaml
inputs:
  - name: raw_files
    description: 本次选择的原始文件
    type: array
    minItems: 1
    items:
      type: string
      kind: ARTIFACT
outputs:
  - name: manifest
    description: UTF-8 JSON 文件，包含按输入顺序排列的成员清单
    type: string
    kind: ARTIFACT
```

例如，标定算子输出标定结果和证据图像时，分别声明两个输出。下游可以只绑定所需的一项。

```yaml
outputs:
  - name: calibration_result
    description: 本次标定的重投影误差，单位为像素
    type: object
    required:
      - reprojection_error_px
    properties:
      reprojection_error_px:
        type: number
        minimum: 0
  - name: evidence_images
    description: 按采样顺序排列的 PNG 文件，无图像时为空数组
    type: array
    items:
      type: string
      kind: ARTIFACT
```

生产者用 `ctx.outputs.write_json("calibration_result", result)` 和 `ctx.outputs.files("evidence_images", paths)` 分别提交。消费者按自己的输入名调用 `ctx.inputs.value(...)` 或 `ctx.inputs.files(...)`。若目录结构本身属于交付格式，生产者生成 ZIP 后通过单文件输出交接，消费者按该格式校验并读取。

文件契约还要说明内容格式、必需字段或包成员、顺序与空结果语义，以及消费者要做的校验。例如 B03 的质检 ZIP、历史 B04 4.0.1 的交付 JSON 与 B04 5.0.0 的原件 ZIP 都是 `string/ARTIFACT`，文件内容不能互换。[实际文件及内容](docs/PRETRAINING_DATA_CLEANING.md#输入与输出)

### 上下游绑定

编排时为每个必需输入选择一个来源，可选输入至多一个来源。例如下游把名为 `stats` 的输入显式绑定到上游的 `statistics_context` 输出，代码使用 `ctx.inputs.file("stats")`。评审时核对源与目标的类型、数组层级、`kind` 和文件内容。依赖连线表达先后关系，输入引用上游输出时必须保证生产者先成功完成。即使上游声明 `outputs: []`，仍可作为下游的执行前置节点。

## 运行资源

`resources` 必填，仅包含 `requests`（申请量）和 `limits`（上限）两组。资源预算按一次调用填写。需求未说明 CPU、内存或 GPU 预算时，先确认预算，再生成用于注册的声明。示例用量不构成默认配额。

| 字段路径 | 类型、单位和约束 |
|---|---|
| `resources.requests.cpu`、`resources.limits.cpu` | 均必填。正数**字符串**，支持核数或毫核，例如 `"2"`、`"0.5"`、`"500m"`。须可精确表示为整数毫核，申请量 ≤ 上限。 |
| `resources.requests.memory`、`resources.limits.memory` | 均必填。主机内存使用正数字符串，支持字节数或二进制单位 `Ki`、`Mi`、`Gi`、`Ti`。换算后须为整数个字节，最大为 `2^63 - 1` 字节，申请量 ≤ 上限。 |
| `resources.limits.nvidia.com/gpu` | GPU 设备数量，使用 GPU 时填正**整数**。CPU 算子省略此字段。 |
| `resources.requests.nvidia.com/gpu` | 可省略。显式填写时必须与同名 limit 相等。声明没有独立的 GPU 显存字段。 |

完整模板给出 CPU 配置。下面的片段表示申请 2 核、8 GiB 主机内存，最多使用 4 核、16 GiB，并使用一张 GPU。

```yaml
resources:
  requests:
    cpu: "2"
    memory: "8Gi"
  limits:
    cpu: "4"
    memory: "16Gi"
    nvidia.com/gpu: 1
```

`cpu: 2` 会被 YAML 解析为整数，`nvidia.com/gpu: "1"` 会被解析为字符串，两种写法都会被校验拒绝。

## 镜像与执行

- **镜像与命令。** 正式算子声明 `harbor.striding.internal/data/data-operators/<code>:<operatorVersion>`。`command` 是非空字符串数组，例如 `["python", "/app/main.py"]`，程序名和参数分别占一项。入口在镜像中固定绑定本版本的声明和实现。
- **基础镜像。** 算子 Dockerfile 的外部 `FROM` 使用已发布的 Harbor 基础镜像和精确 `X.Y.Z` 标签，例如 `harbor.striding.internal/data/data-operators/base/runtime/python312-cpu:1.0.0`。本地探索可使用 `:dev`。CI 记录解析后的摘要，并从本版本的依赖锁准备离线安装材料。[基础镜像开发与引用](docs/DEVELOPMENT_AND_CI.md#基础镜像的选择与开发)
- **实现与依赖。** `operator.py` 实现 `BaseOperator.run(ctx)`，正常完成返回 `None`。`main.py` 调用基类的 `execute`。专用读写与质检上报放在本算子的 `io.py`，计算放在 `core.py`。跨算子复用的代码放在独立的 `packages/` 包，业务实现不得导入其他算子或 `ci`。各版本用自己的 `pyproject.toml` 和 `uv.lock` 固定依赖，包括 SDK。`packages/` 每个发行包只维护当前源码，历史依赖从 Nexus 获取，来源提交和路径由 Nexus Data 包发布记录追溯；禁止通过升级共享源码目录隐式更改旧算子依赖。[共享包规范](docs/DEVELOPMENT_AND_CI.md#共享包与依赖边界)。
- **完成与失败。** 长循环和阶段切换调用 `ctx.check_cancelled()`。`ctx.log.info(..., extra={...})` 记录必要的业务标识和计数。输入、计算或传输失败时抛出异常，记录日志本身不会触发执行失败。下游只能使用成功调用提交的业务输出。
- **标准质检。** 仅 `quality` 阶段调用 `ctx.quality` 的报告方法，每次成功执行必须恰好上报一次。检查整次调用用 `report_batch(...)`，逐项检查用 `report_items(records)`，合法空集合用 `report_items([])`。按业务输入核对明细覆盖和重复。`PASS` 不得携带错误，`REJECT` 必须给出拒绝原因，`UNKNOWN` 必须给出原因或错误。业务拒绝表示检查已正常完成。是否逐项上报独立于 `batchMode`。
- **平台文件。** SDK 将具名业务输出汇总到 `result.json`，无业务输出时为 `{}`。标准质检生成 `quality.json`，逐项报告另生成 `quality_records.jsonl`。`operator.log` 记录运行日志，失败时 SDK 尽力提交 `error.json`。这些名称为保留文件，不列入业务 `outputs`。[SDK 接口与运行说明](https://gitlab.striding.internal/stridingai/data/data-platform-sdk/-/blob/main/docs/USAGE.md)

## 注册与验收

从仓库根目录执行，使用 Python 3.12。下面用模板验证从声明到注册文件的路径，命令只在本地输出注册 JSON。验证正式版本时将路径替换为对应版本的 `operator.yaml`。[环境准备](docs/DEVELOPMENT_AND_CI.md#本地开发)

```bash
make dev OPERATOR=templates/python-operator
templates/python-operator/.venv/bin/python -m tools.export_registration templates/python-operator/operator.yaml
templates/python-operator/.venv/bin/python -m tools.check_operators templates/python-operator/operator.yaml
```

| 检查点 | 交付要求 |
|---|---|
| 声明与注册 | `tools.check_operators <operator.yaml>` 检查指定版本的声明和样例；CI 使用各版本锁定的 SDK 执行。另对待交付声明运行 `tools.export_registration`，核对生成的 `operatorCode`、`operatorVersion`、`executionScope`、`ioDefinition`、命令和资源。注册内容从 YAML 生成，作者只维护一份字段定义。 |
| 阶段特殊要求 | 按 stage 核对接入、预处理、后处理、质检、标注或交付契约；采用 delivery 时另核对类型、固定输出、完整清单及空交付行为。 |
| 样例与调用 | 每个具名输入、输出均有 `valid/` 和 `invalid/` YAML 样例。参数目录提供合法和非法的参数对象。运行真实 `main.py`，检查正常、边界、非法输入和适用的业务拒绝场景，同时检查业务输出及执行状态。[模板调用与断言](templates/python-operator/README.md#运行模板) |
| 业务评审 | 确认编码可区分已有能力、来源和文件格式可识别、单位与跨字段规则明确、资源预算已确认。质检算子还需核对受检对象与判定的对应关系。 |
| 版本交付 | 核对算子目录、包和镜像标识。为正式版本配置基础镜像、接入安装包和容器验收。允许正式发布的精确版本列入 `ci/release-versions.yaml`，同编码允许多个版本；默认运行版本由平台或调用方选择。[多版本发布规则](docs/RELEASE_MANIFEST.md)。记录本次实际通过的检查及未覆盖项。[按算子版本构建](docs/DEVELOPMENT_AND_CI.md#按算子版本构建) |


## 归档交付与历史版本

B04 5.0.0 独立位于 operators/mcap_delivery_accepted/，输入 batch_quality_result，stage=delivery、batchMode=all，delivery 声明 {type: archive, format: zip}，固定输出 archive 为根 string/ARTIFACT。4.0.1 的源码、声明及锁文件从原始 Git 提交查阅；原镜像、验收记录和兼容组合继续保留，当前树不复制历史源码。

5.0.0 复用业务判定校验，只对 kept 主 MCAP 调用 ctx.storage.download(keys, max_concurrency=4)；按原 source_ref.sha256 校验后串行构建 ZIP，根目录 delivery_manifest.json 使用 delivery_manifest_v1。每文件通用字段 delivery_path、size_bytes、sha256；business 包含原 episode_index 与 source_ref。全部拒绝输出仅含空清单的 ZIP，输入或下载错误不发布部分成功。

ctx.outputs.file("archive", zip_path) 统一提交，收尾上传分片默认并发 8；不先调用通用 upload。按键下载与上传复用现有执行存储位置，平台及上层流程保证原件属于该位置；具名输入仍保持各自绑定。Pipeline 使用 SDK 默认预算，无需新增位置或预算字段。平台环境与授权不是 B04 业务输入。无 delivery 的旧版本保持清单交付行为。4.0.1/common 0.4.2 继续锁 SDK dev7；5.0.0/common 0.4.3 独立锁 SDK dev9，不升级 B01–B03。

归档路径为 episode_<至少三位十进制编号>/<主 MCAP 原文件名>，例如 episode_007/episode.mcap。不包含配对 Parquet、不扫描相邻对象、不删除原件。B04 清单最多 16 MiB、业务文件最多 100000 个，归档上限取 ctx.storage.limits.max_upload_bytes（默认 8 GiB）。完整业务规则及可核对的清单示例见[新版 B04](docs/PRETRAINING_DATA_CLEANING.md#b04-500-归档交付)。

版本验收：每个当前算子目录必须提供 `tests/source/`、`tests/wheel/` 和 `tests/container/`。容器用例使用 `OPERATOR_TEST_IMAGE` 指定的实际镜像和本版本固定数据，不调用上游现生成输入。源码与 wheel 使用本版本锁定环境分别执行，不能从其他版本加载同名模块。具体命令与报告位置见[逐版本验证](docs/DEVELOPMENT_AND_CI.md#逐版本验证与缓存)。

## 当前源码与历史版本

- 当前身份由 `operator.yaml.operatorVersion` 与 `pyproject.toml` 项目版本共同确定，两者必须一致；目录名与 `operator.yaml.code` 一致。CI 仍以 `<code>/v<version>` 标识产物和报告。
- 升级在同一目录内进行。正式版本不可覆盖；修改已正式交付版本的业务、契约、依赖或构建输入时提升版本。
- 历史源码从 `operators/<code>/v<version>` Git 标签或 Release 的原构建提交读取；当前文件不能代表历史契约。Dev/RC 交付同样创建标签，例如 `operators/<code>/v4.0.1-dev.12345`；旧候选尚未补登记时从原构建记录追溯。
- 保留历史版本的发布或兼容组合时，在升级前核验原镜像、构建/测试记录及原声明，将不可变引用写入 `ci/archived-versions.json`。缺失记录必须恢复原内容；不能使用当前源码冒充历史版本重建。
- `ci/release-versions.yaml` 控制交付范围，发布清单累计保存已交付的多个版本。两者都不决定平台默认使用哪一版。具体流程见[开发指引](docs/DEVELOPMENT_AND_CI.md#算子源码与历史版本)。
