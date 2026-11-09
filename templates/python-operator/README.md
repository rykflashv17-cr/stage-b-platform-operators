# Python 算子模板

模板可独立开发和验收，无需加入预训练数据清洗流程。需要复用代码时，在 `pyproject.toml` 声明 Nexus 共享包的精确版本并提交依赖锁，不复制共享源码；组织方式见[共享包与依赖边界](../../docs/DEVELOPMENT_AND_CI.md#共享包与依赖边界)。

`record_quality_gate` 从输入 `record` 读取记录编号和分数，用参数 `threshold` 判定，向下游写出 `decision`，并向平台提交一条标准质检明细。阈值 `0.8` 和 CPU、内存数量均为本模板的示例值。[完整声明](operator.yaml) · [通用规范](../../CONTRACT.md)

## 用模板开发

将本模板受 Git 管理的文件复制到 `operators/<code>/`。逐项核对下表，再准备目标版本的开发环境。

| 位置 | 要替换或确认的内容 |
|---|---|
| [operator.yaml](operator.yaml) | 选择 `code`、`name`、`operatorVersion`、`stage` 和 `batchMode`。按业务替换记录、判定和阈值。新增文件时单独声明具名字段，单文件在字符串字段上标记 `kind: ARTIFACT`，文件数组在字符串元素上标记。[文件示例](../../CONTRACT.md#文件接入与传输边界)确认 CPU、内存和 GPU 预算。将镜像换成正式 Harbor 路径，`command` 指向镜像内入口。[命名词表](../../CONTRACT.md#命名与版本) · [资源规则](../../CONTRACT.md#运行资源) |
| [pyproject.toml](pyproject.toml)、[uv.lock](uv.lock) | 修改发行包名、版本、`src/<module>` 打包路径及必要依赖。模板与 `operators/<code>/` 的 SDK wheel 相对路径均为 `../../build/wheels/`。先在根目录准备锁定 SDK wheel，再在目标算子目录运行 `uv lock --python 3.12`。 |
| [main.py](main.py)、[src/quality_gate/](src/quality_gate/) | 核对包名、类名和入口导入。`operator.py` 实现 `run(ctx)`，`core.py` 执行计算，`io.py` 维护本算子的读写和报告。`validation.py` 演示检查输出与输入的业务关系。 |
| [Dockerfile](Dockerfile) | 选择已发布的运行环境，例如 `FROM harbor.striding.internal/data/data-operators/base/runtime/python312-cpu:1.0.0 AS runtime`。保持入口与 `operator.yaml.command` 一致；构建工具按目标版本生成包含入口、声明和 wheel 的独立上下文。[基础镜像选择与开发](../../docs/DEVELOPMENT_AND_CI.md#基础镜像的选择与开发) |
| [examples/](examples/) | 替换各具名输入输出及参数的合法、非法样例。同步调整 [local-request.json](examples/local-request.json) 中的名称、样例路径与独立工作目录。 |
| 仓库配置 | 基础镜像只在本版本 Dockerfile 中选择。允许正式发布时，将精确 `<code>/v<version>` 加入 [ci/release-versions.yaml](../../ci/release-versions.yaml)；保留其他仍需发布的版本。新增独立算子还须将自己的真实调用场景接入验收。[开发与 CI](../../docs/DEVELOPMENT_AND_CI.md) |

## 构建模板镜像

先在仓库根目录按[全仓构建](../../docs/DEVELOPMENT_AND_CI.md#全仓构建)准备已锁定的 wheel 和环境。给上下文指定尚不存在的目录：

```bash
.venv/bin/python -m ci.build_context --operator templates/python-operator --output build/template-context
docker build --platform linux/amd64 --network none -t local/operator-template:check build/template-context
```

上下文工具读取模板的 `FROM`，记录受控基础镜像摘要，并从模板锁文件生成离线安装清单。

## 运行模板

以下命令从仓库根目录执行，需要 Python 3.12、uv 及锁定 wheel 的读取权限。缓存齐备时可给 `make dev` 加 `OFFLINE=1`。每个用例创建独立工作目录，可重复执行。

```bash
make dev OPERATOR=templates/python-operator
templates/python-operator/.venv/bin/python - <<'PY'
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory

entry = Path("templates/python-operator/main.py").resolve()
with TemporaryDirectory(prefix="quality-gate-") as temp:
    for label, score, params, expected in [
        ("boundary", 0.8, {}, "PASS"),
        ("override", 0.9, {"threshold": 0.95}, "REJECT"),
        ("invalid", 1.2, {}, None),
    ]:
        case = Path(temp).resolve() / label
        case.mkdir()
        record = case / "record.json"
        record.write_text(json.dumps({"record_id": "r1", "quality_score": score}))
        work = case / "work"
        request = case / "request.json"
        request.write_text(json.dumps({
            "input_files": {"record": [{"fixture_path": str(record)}]},
            "parameter_overrides": params,
            "workspace": str(work),
        }))
        run = subprocess.run([sys.executable, str(entry), "--local-request-file", str(request)],
                             capture_output=True, text=True)
        result = work / "published/result.json"
        quality = work / "published/quality_records.jsonl"
        if expected is None:
            assert run.returncode != 0 and not result.exists() and not quality.exists()
        else:
            assert run.returncode == 0, run.stderr
            assert json.loads(result.read_text())["decision"] == {
                "record_id": "r1", "keep": expected == "PASS"}
            row = json.loads(quality.read_text())
            assert row["decision"] == expected and row["details"]["record_id"] == "r1"
            if expected == "REJECT":
                assert row["reasons"][0]["code"] == "BELOW_THRESHOLD"
        print(label, run.returncode, expected or "invalid input")
PY
```

预期结果依次为边界通过（退出码 0）、业务拒绝（退出码 0）、非法输入（非零退出码）。验证注册 JSON 和全仓样例的方法见[注册与验收](../../CONTRACT.md#注册与验收)。

新算子版本应在 `tests/` 中维护独立测试。使用该版本锁定环境验证源码与 wheel，不向测试进程添加其他版本的源码路径。Dockerfile 先安装锁定 SDK/第三方依赖，再安装共享包和业务 wheel；修改业务源码不会改变稳定依赖层。详见[逐版本验证与缓存](../../docs/DEVELOPMENT_AND_CI.md#逐版本验证与缓存)。

## 接入版本验收

复制为正式版本后，将单元及契约回归放入 `tests/source/`，安装包检查放入 `tests/wheel/`，实际入口成功与非法输入放入 `tests/container/`。容器测试从 `OPERATOR_TEST_IMAGE` 读取目标镜像，自带固定输入，不现场执行上游。CI 按精确版本分别安装源码和 wheel 环境，并核对本版本的声明、样例及注册输出。跨算子流程通过 `tests/pipelines/<workflow>/definition.yaml` 声明兼容组合；独立算子无需加入已有流程。见 [CI 维护说明](../../ci/README.md)。

算子正式 Git 标签使用 `operators/<code>/v<version>`。共享包 wheel 从 Nexus 获取，来源提交及路径按 Nexus Data 中的 `operators/package-releases/<包名>/<版本>/release.json` 查询；`packages/<规范化发行名>/v<版本>` 对应的 GitLab Release 提供版本说明、wheel 和本包源码下载入口。见[Git 标签规范](../../docs/BRANCHING.md#git-标签)及[共享包流程](../../docs/DEVELOPMENT_AND_CI.md#共享包与依赖边界)。

将 [RELEASE_NOTES.md](RELEASE_NOTES.md) 复制到 `docs/releases/operators/<code>/<业务版本>.md`，填写变更说明、兼容性及已知限制（按需）。同一业务版本的 Dev/RC/Stable 共用该文件，CI 从交付提交读取。完成双仓交付后自动创建 `[Operator][Dev|RC|Stable] <code> v<实际镜像标签>` Release；镜像地址、摘要、来源及下载资产自动生成。已创建 Release 的人工说明不被重试覆盖。见 [Release 编写规范](../../docs/RELEASE_NOTES.md)。

新建或升级算子均使用 `operators/<code>/`，不创建版本子目录。更新声明、包版本和依赖锁后，按当前版本执行源码/wheel/容器验收。历史版本通过 GitLab Release、原始 Git 提交及镜像保存；仍需参与交付或兼容验收的历史版本按[历史版本管理](../../docs/DEVELOPMENT_AND_CI.md#算子源码与历史版本)登记。
