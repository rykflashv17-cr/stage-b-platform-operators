# 预训练数据清洗算子开发与调用

当前源码为 B01–B03 4.0.1 和 B04 5.0.0，均位于 `operators/<code>/`。历史 B04 4.0.1 保留原镜像与原始提交，当前树不再保留其源码。开发时同时遵循[各阶段特殊规范](../CONTRACT.md#各阶段的特殊规范)。四个 `4.0.1` 与 B04 `5.0.0` 均列入[发布范围](../ci/release-versions.yaml)；范围声明不等于发布完成，实际可用版本以发布回执为准。两组兼容组合分别验收，不定义全局默认版本。

以下为当前四个 MCAP 算子的业务契约，并对照历史 B04 4.0.1，均按 `batchMode: all` 调用。B01 接收文件列表，四个节点各输出一个文件。B02、B03、B04 的具名输入各绑定前一节点输出；B04 5.0.0 另凭平台授权下载原始 MCAP。历史四个 4.0.1 的候选标签 `4.0.1-dev.11757` 对应原清单交付契约，不能作为 5.0.0 归档验收依据。[文件声明规则](../CONTRACT.md#文件接入与传输边界) · [四份声明](../README.md#算子与交接顺序) · [候选流水线](https://gitlab.striding.internal/stridingai/data/data-platform-operators/-/pipelines/11757)

## 输入与输出

| 节点 / `stage` | 具名输入 | 具名输出 | 输出文件及内容 |
|---|---|---|---|
| B01 / `ingest` | `raw_files`，非空原始文件数组 | `normalized_batch_ref` | `normalized_batch_ref.zip` 包含批次清单 `manifest.json`、记录成员及来源的 `selection.json` 和各可用 episode 的归一化数值文件 `episode_*.npz`。 |
| B02 / `preprocess` | `normalized_batch_ref` | `statistics_context` | `statistics_context.zip` 包含统计状态和拟合记录 `context.json`、完整归一化批次 `normalized/`，以及统计就绪时生成的 `cache/*.npz`。 |
| B03 / `quality` | `statistics_context` | `batch_quality_result` | `quality_batch.zip` 包含逐项判定 `decisions.jsonl`、成员与来源映射 `selection.json`、汇总 `quality_summary.json`。SDK 另行提交平台标准质检报告。 |
| B04 5.0.0 / `delivery` | `batch_quality_result` | `archive` | ZIP 含通过项原始 MCAP 和 `delivery_manifest.json`；无通过项时仅含空清单。 |
| 历史 B04 4.0.1 / `delivery` | `batch_quality_result` | `batch_delivery_result` | `delivery_manifest.json` 是 UTF-8 JSON 清单。`accepted` 列出通过项的 `episode_index` 与原始 MCAP 的 `source_ref`，其中含定位信息和 SHA-256。没有通过项时为空数组。 |

B01～B03 的 `source` 必填，`workers` 取值 1–4，默认 2。B02 另有 `recompute`，默认 `false`。B04 没有参数。节点计算或传输失败时，后续节点不启动。质检正常拒绝是业务判定，仍可生成交付清单。

## 原始文件接入

`jindian_marvin` 来源中，每个选定的 MCAP 对应一条 episode。`abc_130k` 来源中，同一原始逻辑目录、同一授权来源下的 `episode.mcap` 和 `eef_pose.parquet` 配成一条，两个文件均须在选择范围内。B01 按主 MCAP 逻辑键排序，从零分配本批 `episode_index`。空选择、缺失辅助文件、重复或歧义来源都会导致失败。

Pipeline 调用示例：

```json
{
  "raw_files": [
    "capture/job-42/data/train/pick/episode_000/eef_pose.parquet",
    "capture/job-42/data/train/pick/episode_000/episode.mcap"
  ],
  "source": "abc_130k"
}
```

两个输入文件对应一个 episode。其 `episode_index` 为 0，而主 MCAP 在 B01 原始输入中的 `input_index` 为 1。平台为每个原始文件授予对应存储位置。在本节 4.0.1 链路中，B02～B04 只读取前一节点成功提交的文件。B04 5.0.0 另需平台提供原 MCAP 下载绑定与读取权限。中间包中无原始 MCAP 和 Parquet，B03 的质检包中也无归一化数值文件。

## 本地运行 B01

先按[本地开发](DEVELOPMENT_AND_CI.md#本地开发)准备 B01 环境。下面以授权目录 `/data` 下已有的 `capture/one.mcap` 为例。每次调用均使用新的工作目录。

```bash
cd operators/mcap_read_normalize
export MCAP_INPUT_ROOT=/data
export MCAP_INPUT_KEY=capture/one.mcap
.venv/bin/python - <<'PY'
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import mkdtemp

case = Path(mkdtemp(prefix="b01-example-")).resolve()
selection = case / "raw_files.json"
selection.write_text(json.dumps([os.environ["MCAP_INPUT_KEY"]]))
request = case / "request.json"
request.write_text(json.dumps({
    "input_files": {"raw_files": [{"fixture_path": str(selection)}]},
    "parameter_overrides": {"source": "jindian_marvin", "workers": 1},
    "workspace": str(case / "b01"),
    "artifact_store": str(case / "artifacts"),
    "read_roots": [os.environ["MCAP_INPUT_ROOT"]],
}))
result = subprocess.run([sys.executable, "main.py", "--local-request-file", str(request)],
                        check=True, capture_output=True, text=True)
report = json.loads(result.stdout)
assert report["validation"] == "passed" and len(report["outputs"]["normalized_batch_ref"]) == 1
print(case / "b01/published/result.json")
PY
```

本地多进程调用时，后续节点各用独立工作目录，从同一个本地产物存储导入上游完整文件引用。可参考[四入口测试](../tests/pipelines/pretraining_data_cleaning/test_pipeline.py)。从仓库根目录执行 `.venv/bin/python -m tools.export_registration operators/mcap_read_normalize/operator.yaml` 可以生成 B01 的注册 JSON。

## 历史 B04 4.0.1：消费交付清单

B04 检查判定覆盖、来源和汇总，只保留通过项。清单格式为 `mcap_delivery_manifest_v1`。下例的摘要字段为示意值：

```json
{
  "format": "mcap_delivery_manifest_v1",
  "accepted": [{
    "episode_index": 0,
    "source_ref": {
      "attempt_id": "00000000-0000-4000-8000-000000000017",
      "input_name": "raw_files",
      "input_index": 1,
      "logical_key": "capture/job-42/data/train/pick/episode_000/episode.mcap",
      "sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
    }
  }]
}
```

1. 消费端在其授权范围内查询 `attempt_id` 对应的 B01 冻结输入，按 `input_name` 和 `input_index` 找到原始成员及其存储位置。必须保留输入顺序和来源，不能用当前批次重新枚举的顺序替代。
2. 核对该成员的逻辑键与 `logical_key` 一致，使用消费端自己的授权读取原件，再对读取字节重算 SHA-256。应在校验成功后向训练或导出任务交付数据。
3. 来源不可用、逻辑键不符或摘要变化时，当前消费失败。B04 的成功仅表示清单已经生成。进入实际消费前应保留原件、B01 的冻结输入及其授权定位所需的信息。`accepted: []` 不触发原文件下载。

历史清单行为通过 `manifest_delivery` 组合的实际原镜像验收；当前 5.0.0 的签名传输与篡改检测见[本地签名存储测试](../tests/pipelines/pretraining_data_cleaning/test_pipeline_files.py)。`result.json` 返回清单文件自身的键。`quality.json` 和 `quality_records.jsonl` 仍由 B03 的 SDK 在独立路径提交。

## 开发与版本

各算子的 `io.py` 负责本节点的具名输入输出和业务校验。共享业务包 `striding-pretraining-data-cleaning-common==0.4.2` 负责前三个节点的业务 ZIP 布局、打包、解包及内容完整性校验。当前 B01–B03 使用 SDK `0.3.0.dev7`，当前 B04 5.0.0 使用 SDK `0.3.0.dev9` 和 common `0.4.3`；历史 B04 4.0.1 使用 SDK dev7。各版本的依赖由自己的锁文件或原镜像保存。固定上游算法与资源见[来源记录](V4_SOURCE_PROVENANCE.json)。

新旧注册声明的传输类型可能同为 `ARTIFACT/string`，但包内容和读写接口不同。历史 4.0.1 链路以已验证版本组合交付；B04 5.0.0 可与 B01–B03 4.0.1 配合，不要求升级前三个节点。已有 Pipeline 执行继续绑定原镜像版本。签名服务、Nexus、基础镜像及发布要求见[开发与 CI](DEVELOPMENT_AND_CI.md)。


## B04 5.0.0 归档交付

历史四个 4.0.1 版本行为保持不变，当前 B04 5.0.0 只替换链路最后一项的消费方式，不要求 B01–B03 成组升级。新版保持 batch_quality_result 输入，读取 quality_batch 的 bundle.json 索引和 payload/ 三个业务文件，严格校验 kept/rejected、来源覆盖与汇总；处理错误失败，不当普通拒绝。

通过项对象键由业务 source_ref 取得，SDK dev9 并发下载（默认 4），校验原字节并串行写 ZIP。输出名固定 archive，包内清单含通用路径、大小、SHA-256，业务来源放 business。旧版 batch_delivery_result 不增加 ZIP 内容，也不改名。测试见 operators/mcap_delivery_accepted/tests/source/test_entrypoint.py。

原共享 common 0.4.2 / SDK dev7 与 common 0.4.3 / SDK dev9 均作为固定 wheel 保存在 Nexus；原 engine 0.1.0 同样保留。算子算子目录及锁文件继续使用原发行名和摘要。当前共享源码统一在 `packages/pretrain-cleaning-common/` 和 `packages/pretrain-cleaning-engine/` 开发，采用新发行名；旧源码通过 Nexus Data 发布记录中的提交和路径查找，五个历史版本的记录地址见[包发布快照](PACKAGE_RELEASES.json)。

| 约束 | B04 5.0.0 |
|---|---|
| 输入 | B03 4.0.1 已确认的 batch_quality_result，根 bundle.json 与 payload/selection.json、decisions.jsonl、quality_summary.json |
| 选择 | kept、reject_file=false、无错误且统计 ready 才打包；正常 rejected 跳过；诊断错误、字段矛盾、覆盖或汇总不符整次失败 |
| 原件 | 每个 kept 仅一个主 MCAP，不附带配对 Parquet，不扫描相邻文件，不删除源文件 |
| 来源 | 保留 B01 的 episode_index 和 source_ref；下载、写入及重读归档的字节摘要一致 |
| 路径与顺序 | episode_<至少三位十进制编号>/<原 MCAP 文件名>，清单按路径 UTF-8 字节序排列 |
| 合法空结果 | 非空合法选择全部 rejected 时，ZIP 仅含 format=delivery_manifest_v1、files=[] 的清单；空 selection 非法 |
| 提交 | delivery={type: archive, format: zip}，仅声明 archive；ctx.outputs.file 统一上传与提交，不额外输出清单 |
| 预算 | 下载并发 4，上传文件 2、每文件分片 8、上传请求 16；单下载 1 GiB、缓存 8 GiB、单归档 8 GiB，可由运行配置覆盖；清单固定 16 MiB、业务文件最多 100000 |

以下包内清单的大小和摘要是格式示例，实际值由原字节计算。

```json
{
  "format": "delivery_manifest_v1",
  "files": [{
    "delivery_path": "episode_000/episode.mcap",
    "size_bytes": 102400,
    "sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "business": {
      "episode_index": 0,
      "source_ref": {
        "attempt_id": "00000000-0000-4000-8000-000000000017",
        "input_name": "raw_files",
        "input_index": 1,
        "logical_key": "capture/job-42/data/train/pick/episode_000/episode.mcap",
        "sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
      }
    }
  }]
}
```

Pipeline 执行时，B04 按键下载复用现有执行上下文根层 storageLocationId、provider、bucket、rootPrefix；平台及上层流程保证原件属于该位置并提供读取权限。具名 batch_quality_result 输入仍使用 inputArtifactLocations，可来自其他位置。B04 只传完整对象键，不补 rootPrefix、不解释权限；不新增 defaultDownloadStorage 或 storageTransferLimits。传输并发和字节预算沿用 SDK 默认值。

本地请求通过 storage_files 映射原 MCAP 对象键，映射文件受 read_roots 约束。batch_quality_result 仍作为普通具名文件输入交接。可复核用例见 [B04 归档测试](../operators/mcap_delivery_accepted/tests/source/test_entrypoint.py)及[回环传输测试](../tests/pipelines/pretraining_data_cleaning/test_pipeline_files.py)。本次真实外部存储验收已免除；SDK、本地入口、回环服务、声明转换与镜像验收完成不表示平台数据交付页面已经接入。

## 参考源码夹具

对照测试使用 `tests/fixtures/pretraining_data_cleaning_reference/`。目录按业务用途命名；内部固定上游包名 `stage_b_mcap_v4`、上游版本字符串及源码保持原始内容，以便按 [来源记录](V4_SOURCE_PROVENANCE.json)核验。它们不作为本仓 CI、算子类别或流程的名称。

## 历史版本验证

`tests/pipelines/pretraining_data_cleaning/definition.yaml` 保留 `manifest_delivery`（原 B04 4.0.1）和 `archive_delivery`（当前 B04 5.0.0）两组精确组合。CI 核验原构建及 source/wheel/container 记录，并用实际旧镜像运行当前兼容组合，不重建旧源码。当前四算子的本地进程回归则分别使用各自依赖环境。

历史 B04 原声明：[4.0.1 operator.yaml](https://gitlab.striding.internal/stridingai/data/data-platform-operators/-/blob/9fb219547f6c44454048b24cbe1d64cd457294c8/operators/mcap_delivery_accepted/v4.0.1/operator.yaml)。该提交是原候选的构建来源，不表示已完成正式发布。原镜像和验收记录的精确引用见 [ci/archived-versions.json](../ci/archived-versions.json)。
