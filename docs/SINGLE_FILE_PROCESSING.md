# MCAP 按单个 Episode 编排

B03 在 `quality_summary.json` 中保存 `statistics_context_digest`，即已验证的 `context.json` 实际字节 SHA-256；B04 除核对同一 cohort，还要求全部结果携带相同的 64 位十六进制摘要，以拒绝同一输入集合却使用不同拟合统计的结果混用。

本文描述单 Episode 契约变更的本地开发版本：B01–B03 目标版本为 `5.0.0`，B04 为 `6.0.0`，共享包为 `pretrain-cleaning-common 0.5.0`。这些版本号表示候选源码身份，不表示已经发布镜像或通过生产平台验收。旧批量契约见历史版本记录，不能把新具名字段直接接入旧版节点。

## 四个节点的边界

| 节点 | `batchMode` | 输入 | 输出 | 一次调用范围 |
|---|---|---|---|---|
| B01 `mcap_read_normalize` | `none` | `raw_files`，ARTIFACT 文件数组 | `normalized_episode_ref`，一个 ZIP | 一个 Episode 的读取与归一化 |
| B02 `mcap_stats_global` | `all` | `normalized_episode_refs`，B01 ZIP 数组 | `statistics_context`，一个 ZIP | 完整选定 cohort 的统计，保存成员身份和统计结果 |
| B03 `mcap_quality_detect` | `none` | 原始 B01 的 `normalized_episode_ref` 与共享 `statistics_context` | `episode_quality_result`，一个 ZIP | 对一个 Episode 做质量判定，并证明它属于该统计 cohort |
| B04 `mcap_delivery_accepted` | `all` | `episode_quality_results`，B03 ZIP 数组 | `archive`，一个 ZIP | 收齐同一 cohort 的全部判定并校验后，交付通过项的原始 MCAP |

单 Episode 不等于任何来源都只传一个物理文件：

- `jindian_marvin`：`raw_files` 必须恰好包含一个 `.mcap`。
- `abc_130k`：必须恰好包含同一逻辑目录、同一授权来源的 `episode.mcap` 和 `eef_pose.parquet`，两个物理文件共同描述一个 Episode。

B01 保持 `raw_files` 为 ARTIFACT 数组，以遵循 ingest 文件接入规范。它先检查数组数量与逻辑配对，再访问文件绑定、校验路径并暂存复制；多 Episode 不能先复制完再处理。平台可能已经在执行前将输入物化，算子这里避免的是自身的重复暂存和计算，不保证取消平台之前发生的传输。

## 编排需要 scatter / gather 与共享统计广播

```mermaid
flowchart LR
    R[选定原始 Episode 集合] --> S[按 Episode 拆分输入]
    S --> B1[B01 单 Episode读取]
    B1 --> G[收集全部 normalized_episode_refs]
    G --> B2[B02 全局统计]
    B1 --> B3[B03 单 Episode质量检测]
    B2 -->|同一统计包广播给每个调用| B3
    B3 --> Q[收集全部 episode_quality_results]
    Q --> B4[B04 校验完整 cohort 并交付]
```

这里的 B01、B03 方框分别代表多个单 Episode 调用。平台需要先按 Episode 拆分调用 B01，收集全部单包供 B02 统计；待 B02 完成，再让每个 B03 同时绑定自己的原始 B01 单包和同一份统计包。最后 B04 收集完整 cohort 的全部结果，包含正常拒绝项。

`batchMode: none` 只定义一次调用的业务范围，不自动实现平台循环、数组展开或统计广播。仅将四个节点串成一根线，不足以表达这套流程。上线前需要确认编排平台支持这些调度能力，并验证实际数据端口绑定。

## B01 的输入示例

单个 Marvin Episode：

```json
{
  "raw_files": ["capture/job-42/one.mcap"],
  "source": "jindian_marvin",
  "workers": 1
}
```

单个 ABC Episode：

```json
{
  "raw_files": [
    "capture/job-42/data/train/pick/episode_000/eef_pose.parquet",
    "capture/job-42/data/train/pick/episode_000/episode.mcap"
  ],
  "source": "abc_130k",
  "workers": 1
}
```

ABC 的输入顺序可以颠倒，但主 MCAP 的 `source_ref.input_index` 必须保持它在该次 B01 原始数组中的位置。不能按排序后的包序重新解释该索引。缺少 sidecar、不同逻辑目录、不同来源、重复输入及多个 MCAP 都应失败。

## 中间包与身份

`normalized_episode_ref.zip` 沿用 `normalized_batch` 业务包格式及 `normalized_batch_v1` 清单格式，但必须只有一条 record、一个选择成员。包中有 `manifest.json`、`selection.json`，以及读取成功时的归一化数值文件；没有原始 MCAP 和 Parquet。内部 `run_batch` 接口仍可接收一个成员的列表，不表示 B01 支持批量输入。

每个 B01 的局部 `episode_index` 可从 0 开始，不能把不同调用中的局部编号当作全局唯一身份。来源身份包括原 B01 `attempt_id`、`input_name`、`input_index`、逻辑键与源字节 SHA-256。B02 汇集时统一确定 cohort 成员，B03 校验原单包确实属于这份统计集合；B04 必须核对结果对同一集合的完整覆盖。重复、遗漏、跨 cohort 或身份矛盾不能作为可交付的正常拒绝处理。

独立 B01 调用的顺序可以变化；合并身份应依据来源证据，不依赖临时下载路径。不要人为重写 `source_ref`，也不要从 B02 的归一化集合随意挑选记录替换 B03 原始单包输入。

## 正常拒绝、错误和交付

| 情况 | 业务含义 | 后续行为 |
|---|---|---|
| 成功读取，质量规则均通过 | `kept` | B04 可按来源下载、校验并归档原始 MCAP |
| 成功判定，夹爪缺测或 S1–S4 规则命中 | 正常 `rejected` | 结果仍须送到 B04；不归档该原件，保留完整 cohort 证明 |
| 多 Episode、非法配对或不安全来源 | 输入契约错误 | 失败，不按正常质量拒绝吞掉 |
| 读取、统计、数值计算或身份校验失败 | `error` / 执行错误 | 不能归入正常拒绝，也不能静默生成成功交付 |
| 完整合法 cohort 全部正常拒绝 | 合法空通过集 | B04 输出含空清单的 ZIP，不下载原 MCAP |

底层 B01 可能将单 Episode 解析失败记录在单包的 `input_error` 中，而非立即抛出；后续仍须识别为错误，不能因为包已生成就视为质量合格或普通拒绝。正常拒绝不意味着物理删除源文件。本链路 B04 交付通过项原字节，不切分、重编码或删除原始 MCAP；ABC 的辅助 Parquet 用于判定，不随通过项 MCAP 一并归档。

## 统计集合与验证范围

B02 的 `all` 是平台本次选定 cohort，不必是整个数据集。B01/B03 可以并行，B02 必须看到完整集合，B04 必须收齐同一集合的全部结果。不能每个 B03 临时按单条重算统计后，再声称这些结果来自同一全局基准。

批次变化可能改变自适应阈值和判定；小批次接口测试不代表阈值已标定，也不能据此预测全量保留率。当前变更只声明候选契约和本地源码验证；wheel、镜像、依赖发布与真实平台 scatter/gather/broadcast 需分别验收。外部服务证书或精确依赖不可用时，应记录验证缺口，不能绕过 TLS 或把源码测试写成生产验证。

## 本地复验与依赖交付

有 NumPy、SciPy、Pandas、PyArrow、MCAP、Protobuf、PyYAML 的 Python 3.12 环境可以运行：

```bash
python tools/run_single_file_business_tests.py
```

此命令运行真实 MCAP 解码、数值清洗和 ZIP 交接的业务测试，并写入 `docs/validation/single-file-20261109.json`。它不导入或伪造 SDK；其结果不替代 SDK 入口、注册、签名存储或容器测试。实际测试依赖版本写入该报告，应与正式部署环境区分。

当前 `uv.lock` 保留下载基线的内容，**尚未对应本次候选的 pyproject 与 wheel，不能将当前目录视为已完成依赖交付**。Nexus 是公司的 Python 软件包仓库，完整 SDK 和精确依赖从这里获取。本机访问它时证书链验证失败，本次没有关闭证书验证，也没有手写 wheel 哈希。

在能正常验证 Nexus 证书的开发环境中，按以下顺序完成交付：

1. 获取并核对 `striding-operator-sdk 0.3.0.dev9`、`pretrain-cleaning-engine 0.1.0` 的真实发布 wheel 及精确构建、测试依赖。
2. 构建 `pretrain-cleaning-common 0.5.0`，先刷新其当前版本锁，再构建四个候选算子；刷新四个算子与根环境的本地 wheel 锁和哈希。只对新候选升级对应共享包，不重锁历史版本。
3. 按仓库规范分别执行声明样例校验、注册转换、真实 SDK 入口、已安装 wheel 和容器验收。本次更新了当前算子的入口测试；尚未执行这些环境门禁。
4. 为新契约建立独立兼容组合与环境。现有 `tests/pipelines/pretraining_data_cleaning/definition.yaml` 和其旧锁仍记录历史批量版本，不是新四节点编排的验收证明。现有发布范围及历史镜像记录未修改，不能直接发布新版本。
5. 在平台验证 B01/B03 的 DATA 调用、两次完整汇集、共享统计广播、失败停止和来源授权，再按正常 MR/发布流程交付。

统计包沿用现有格式，仍含完整归一化批次。B03 只对选定成员执行清洗判定，读取和完整性校验会访问共享统计包；当前实现没有将它压缩为只有阈值的轻量广播文件。

## 本次验证结果

2026-11-09 本地统一运行 `tools/run_single_file_business_tests.py`：31 项通过，0 失败、0 错误、0 跳过。使用 4 条合成 Marvin MCAP 和 1 条 ABC MCAP（带 Parquet）；逐文件判定与使用同一共享统计的整批判定一致，源文件摘要不变。B01/B02/B03 核心代码在依赖导入名归一后 AST 与下载基线一致，固定引擎源码未修改。详细依赖与验证边界见 [验证报告](validation/single-file-20261109.json)。
