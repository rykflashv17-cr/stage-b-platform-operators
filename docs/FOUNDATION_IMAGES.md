# 算子基础镜像管理与发布方案

基础镜像由算子仓库统一制作，发布到 Harbor 的 `data` 项目。**受保护的 `develop` 自动发布可长期引用的版本；算法团队拿到地址即可开发算子。** 提交到 Git 的算子 Dockerfile 直接引用精确版本，本地联调可以使用可变的 `:dev`。本文定义目标流程，版本号均为示例。具体配方、命令和运维步骤见[基础镜像维护手册](../images/README.md)。

## 1. 何时新增基础镜像

| 需求 | 维护位置 | 示例 |
|---|---|---|
| 复用已有 Python、操作系统及运行库 | 算子当前源码目录的 Dockerfile | 新的 CPU 算子直接引用已有的 `runtime/python312-cpu:1.0.0` |
| 增减算子 Python 依赖或升级 SDK | 该算子版本的 `pyproject.toml`、`uv.lock` | B01 升级 `pyarrow`，由 B01 的 CI 准备对应 wheel |
| 需要新的、可复用的运行环境 | `images/runtimes/<name>/` | 增加 Python 3.13 或 CUDA 运行环境，允许旧环境并行使用 |
| 需要专用编译工具链 | `images/builders/<name>/` | 原生扩展需要 GCC、CMake 或 CUDA 编译器。builder 基于明确版本的 runtime，最终算子镜像只带运行所需文件 |

每个配方目录包含 `Dockerfile`、单行 `VERSION` 和运行验收脚本 `check.sh`。专用 builder 还需提供构建样例 `check-build.sh`。配方维护者负责系统依赖、上游基础镜像和验收样例；算子开发者负责本版本的依赖锁、基础镜像选择和真实入口测试。构建目标沿用当前的 `linux/amd64`。

```text
images/
  runtimes/python312-cpu/
    Dockerfile
    VERSION                         # 例如 1.0.0
    check.sh
  builders/<name>/                  # 仅在需要专用工具链时新增
    Dockerfile
    VERSION
    check-build.sh
    check.sh
operators/<code>/
  Dockerfile                        # 引用已发布的基础镜像版本
  operator.yaml                     # image 指向最终算子镜像
  pyproject.toml
  uv.lock
```

## 2. 版本、地址与标签

一个镜像系列使用一个 Harbor 仓库。runtime 地址为 `harbor.striding.internal/data/data-operators/base/runtime/<name>`，builder 对应 `base/builder/<name>`。每个系列独立维护 `X.Y.Z`，从 `1.0.0` 起步。兼容修复递增 Z，兼容新增能力递增 Y，破坏同一系列已承诺的运行或编译接口时递增 X。新版本须高于该系列已发布的最高版本。基础镜像版本独立于算子版本和 SDK 版本。

| 标签或标识 | 约定 |
|---|---|
| `:1.0.0` | 精确版本。由受保护 CI 按版本保存回执，已发布版本只接受同一摘要。算子构建时按回执校验，Dockerfile 只填版本标签。可用于开发、CI 和正式算子 Dockerfile。 |
| `:dev` | 同一系列最新成功发布版本的可变别名。供本地探索，使用前重新拉取。CI 校验提交的 Dockerfile，拒绝将 `:dev`、`:latest` 或省略标签的基础镜像作为交付输入。 |
| 镜像摘要 | 写入发布回执和算子构建记录，用于比对、审计和复现。开发者在算子 Dockerfile 中只填写精确版本。 |

发布完成时，新版本和 `:dev` 指向同一份已验收镜像。基础镜像无需逐流水线生成 `-dev.<编号>` 标签；最终算子镜像继续使用现有的候选与正式发布规则。基础镜像供构建端从 Harbor 拉取，最终算子镜像按现行流程同步阿里云 ACR。[算子发布与同步](DEVELOPMENT_AND_CI.md#harbor-与阿里云同步)

## 3. 新增、构建与发布

| 环节 | 操作与完成条件 |
|---|---|
| 新增或升级配方 | 提交配方、`VERSION` 及最小验收用例。会改变镜像内容的修改须同时升版，包括父镜像及打包进镜像的工具或缓存。基础镜像 CI 直接发现变更的配方目录，允许首个消费者尚未创建。新增 runtime 和依赖它的首个 builder 时，先发布 runtime，再在 builder 的 `FROM` 中引用该版本。 |
| MR 验证 | `check-base-images:<配方>/<版本>` 构建受影响的配方。runtime 验证目标架构、解释器、必要系统库和非 root 启动。专用 builder 需构建最小样例，并在对应 runtime 中运行产物。MR 作业使用临时本地镜像，不发布 Harbor 标签。 |
| 合入受保护的 `develop` | `build-base-images` 构建并暂存，`test-base-images` 按摘要验收，`publish-base-images` 将同一镜像发布为精确版本，回读 Harbor 清单摘要。随后将 `:dev` 指向该摘要并核对。两个标签共用制品，发布凭据仅交给受保护的作业。 |
| 算子开始开发 | 从回执取得 `repository:version`，写入算子当前源码目录的 Dockerfile。已有同类环境时可直接使用已发布版本；首次引入新环境时，基础镜像 MR 先于算子 MR。 |
| 同步至 `main` | 合入已验收的配方和版本，核对 `develop` 的发布回执与 Harbor 精确版本。此步骤无需重新构建或移动 `:dev`。 |

例如，首个版本发布成功后，可用以下命令取得示例 runtime 并核对 Python 版本。

```bash
docker pull --platform linux/amd64 harbor.striding.internal/data/data-operators/base/runtime/python312-cpu:1.0.0
docker run --rm --platform linux/amd64 --network none --user 10001:10001 \
  harbor.striding.internal/data/data-operators/base/runtime/python312-cpu:1.0.0 python --version
```

**发布重试。** 每个配方的回执位于 `build/base-images/<配方>/<版本>/publish/<job_id>/receipt.json`，按系列记录仓库、版本、目标架构、镜像摘要、源码提交和流水线；builder 另记父 runtime 的版本及解析摘要。回执应随版本长期保留，便于超过 CI 工件保留期后核对。若精确标签已存在，只接受能核实为本次已验收制品的相同摘要；内容冲突时停止。精确标签已发布而 `:dev` 更新失败时，从已核实的精确版本继续。对同一系列串行更新 `:dev`，并核对版本顺序，避免延迟或重试的旧作业覆盖新别名。

## 4. 算子如何引用和离线构建

下面是 B01 的目标 Dockerfile 片段。CI 生成 `wheelhouse/` 和 `requirements.install.txt`，包含目标环境运行 B01 所需的全部 wheel，包括算子包本身。若只需要安装现成 wheel，可以复用同一 runtime 的构建阶段和运行阶段。[Docker 多阶段构建](https://docs.docker.com/build/building/multi-stage/)

```dockerfile
FROM harbor.striding.internal/data/data-operators/base/runtime/python312-cpu:1.0.0 AS runtime

FROM runtime AS build
COPY wheelhouse/ /build/wheels/
COPY requirements.install.txt /build/requirements.install.txt
RUN python -m venv /opt/operator/venv \
    && /opt/operator/venv/bin/python -m pip install \
       --no-index --only-binary=:all: --require-hashes \
       --find-links=/build/wheels -r /build/requirements.install.txt

FROM runtime AS final
COPY --from=build /opt/operator/venv /opt/operator/venv
# 接续该算子的 main.py、operator.yaml、PATH、USER 和 ENTRYPOINT
```

1. **准备依赖。** CI 按该算子版本的锁文件和目标平台准备全部传递依赖。第三方 wheel 和 Nexus 上的 SDK 校验锁定摘要；仓库构建的共享包和算子 wheel 校验源码、元数据并记录实际摘要。安装清单由 CI 生成，逐包固定版本与哈希。开发者只维护一份依赖声明及其锁。
2. **禁网构建。** CI 先拉取明确版本的基础镜像并记录解析摘要，再以 `--network none` 从空虚拟环境安装，最终运行阶段只复制运行产物。找不到目标平台 wheel 时，准备阶段应报出包名、版本和平台。确需编译的依赖由专用 builder 生成匹配 runtime 的 wheel，再按相同流程验收。Nexus 凭据仅用于准备阶段。
3. **独立升级。** B01 从 `:1.0.0` 升至 `:1.0.1`，只修改 B01 对应版本的 Dockerfile 并验证实际入口。若需回退，恢复先前精确版本并重新验收；已有正式算子镜像按原镜像版本或摘要回退。已发布的基础镜像版本始终保留原内容。

## 5. CI 组织与失败恢复

使用统一六阶段 `plan → build → test → publish-images → mirror-images → publish-manifest`。配置归属 `ci/gitlab/`，基础镜像的配方留在 `images/`；镜像管理实现归属 `ci/base_images/`。[阶段、作业和报告](../ci/README.md)

| 场景 | 行为 |
|---|---|
| 配方 MR | `check-base-images:<配方>/<版本>` 本地构建并验收，不推送。 |
| develop | `build-base-images` 上传临时镜像；`test-base-images` 按摘要执行检查；`publish-base-images` 在通过后创建精确版本并移动 `dev`。 |
| release/main | `plan-changes` 核对 develop 回执、版本摘要和祖先提交；不重新制作基础镜像。 |
| 新 runtime 与首个消费者 | runtime 先独立合入并发布，再提交 builder 或算子引用。 |
| 仅文档变化 | 无未完成交付且构建输入未改变时不构建。 |
| 修改某个算子的基础版本 | 按精确算子版本验证，不使未采用该版本的其他算子失效。 |

保护分支构建的临时标签为 `<版本>-ci.<父流水线ID>.<构建作业ID>`，位于现有 Harbor `data` 项目。测试与发布交换镜像摘要及小型报告，不通过 GitLab 上传镜像归档。镜像发布作业不重新构建。

发布中断时可独立重试发布步骤；它读取最新成功构建和测试证据，拒绝旧报告。版本冲突、回执不一致或更新的版本已发布时停止，禁止覆盖精确版本或倒退 `dev`。整个链路重建须新建父流水线；恢复与回滚保留已有镜像和回执。

当前 CPU runtime 已使用版本化配方及回执。旧 `images/profiles.toml`、`images/pins.json` 和全局镜像构建参数不再作为维护入口。历史标签及基础镜像回执不回写。基础镜像回执独立于[算子 v3 发布清单](RELEASE_MANIFEST.md)，其格式不随算子清单升级而改变。
