# Release 标题、版本说明与下载资产

一个 GitLab Release 对应一个算子或共享包的精确发布版本。Nexus 保存包与完整发布记录；Harbor/ACR 保存算子镜像，发布清单和交付回执记录实际交付。GitLab Release 提供版本说明、源码定位及下载入口。

## 版本、标题与创建时机

标题统一为 `[类型][阶段] 名称 v版本`。名称使用算子 `code` 或规范化 Python 发行名；不使用展示名称或仓库版本代替。

| 对象与阶段 | 标题示例 | Git 标签 | 创建时机 |
|---|---|---|---|
| 算子 Dev | `[Operator][Dev] mcap_read_normalize v4.0.1-dev.12345` | `operators/mcap_read_normalize/v4.0.1-dev.12345` | 受保护 develop 完成该候选的交付 |
| 算子 RC | `[Operator][RC] mcap_read_normalize v4.0.1-rc.12346` | `operators/mcap_read_normalize/v4.0.1-rc.12346` | 受保护 release 完成该候选的交付 |
| 算子 Stable | `[Operator][Stable] mcap_read_normalize v4.0.1` | `operators/mcap_read_normalize/v4.0.1` | main 手动发布后完成正式交付 |
| 包 Dev | `[Package][Dev] pretrain-cleaning-common v0.4.4.dev1` | `packages/pretrain-cleaning-common/v0.4.4.dev1` | develop 完成 Nexus 发布及回读核验 |
| 包 RC | `[Package][RC] pretrain-cleaning-common v0.4.4rc1` | `packages/pretrain-cleaning-common/v0.4.4rc1` | 同上 |
| 包 Stable | `[Package][Stable] pretrain-cleaning-common v0.4.3` | `packages/pretrain-cleaning-common/v0.4.3` | 同上 |

- 算子声明和 Python 项目版本仍为三段数字，如 `4.0.1`；Release 使用**实际交付的镜像标签**作为版本。`v` 仅用于 Git 标签和标题，不写入镜像标签、声明或安装参数。
- 同一个业务版本可有多个 Dev、RC Release 和一个 Stable Release，分别保留。晋级不改名、不删除旧候选 Release。复用已交付候选时使用原候选标签，不按当前流水线编号再造版本。
- 阶段由实际版本识别，且算子镜像标签须与交付分支匹配。共享包沿用 Python 版本规则；develop 发布的 `0.4.3` 仍为 Stable，alpha/beta 分别标识 Alpha/Beta，含 `.devN` 的预发布版本标识 Dev。
- MR、未通过验收的临时镜像、仅参与兼容测试的版本不创建 Release。没有新交付的文档流水线不创建新候选 Release。
- 标签固定指向原镜像或包的构建提交。同一提交可对应多个对象和多个阶段的标签；已发布标签不得移动。包和算子标签均不触发新流水线。

算子在现有 `publish-operator-manifest` 中顺序完成：

```mermaid
flowchart LR
    A[已通过验收的镜像] --> B[Harbor 与 ACR 摘要核对]
    B --> C[核验或写入清单及交付回执]
    C --> D[创建或补齐 Git 标签与 Release]
```

## 开发者编写什么

| 对象 | 文件位置 | 维护方式 |
|---|---|---|
| 算子 | `docs/releases/operators/<code>/<业务版本>.md` | 同一业务版本共用一份说明，例如 `4.0.1.md`；随实际功能和契约变更更新，不为每个流水线编号建文件。 |
| 共享包 | `docs/releases/packages/<规范化发行名>/<版本>.md` | 使用包的实际版本，例如 `0.4.4.dev1.md`、`0.4.4rc1.md`。 |

人工说明按以下结构编写。`变更说明`、`兼容性` 必填；存在使用限制时填写 `已知限制`，没有限制则省略该节。可复制[说明模板](../templates/python-operator/RELEASE_NOTES.md)，删除占位说明。

```markdown
## 变更说明

- 交付输出由 JSON 清单调整为 ZIP，包含通过项的原始 MCAP 和完整交付清单。
- 无通过项时返回仅含空清单的 ZIP。

## 兼容性

- 输入 batch_quality_result 保持不变，可接入 B03 4.0.1。
- 输出由 batch_delivery_result 改为 archive。下游须更新输入绑定并按 ZIP 读取。
- 运行环境须允许读取通过项的原始 MCAP。

## 已知限制

- 原始文件的读取范围受平台授予的存储位置与权限限制。
```

上例说明 B04 5.0.0 的使用变化，不是所有算子的固定内容。编写时遵循：

1. 只说明本对象、本版本的变化及使用影响。区分新增、修复和破坏性变化；有比较基线时写明其版本。不复制全仓提交日志。
2. 兼容性覆盖实际发生变化的输入输出、文件内容、参数、调用粒度、运行依赖及资源要求；破坏性变化必须说明升级操作。没有变更的方面无需逐项重复。
3. Dev、RC 说明也须准确。不得因创建 Release 宣称生产可用、正式发布完成或外部联调通过。验收结论必须有对应记录支持。
4. 人工文件不填写重复标题、镜像摘要、流水线编号、下载 URL 或自动区块标记。这些信息由发布记录生成，避免手工维护多份。
5. 历史版本补登记只补已有事实；无法确认的差异或兼容性明确说明，不推测。本次交付提交没有独立说明时，CI 使用明确的缺失提示，不生成猜测性变更日志。

## CI 自动补充什么

| 内容 | Operator | Package |
|---|---|---|
| 自动区块 | `使用与来源` | `安装与来源` |
| 使用命令 | Harbor、ACR 的实际版本 `docker pull` 命令 | 精确包版本 `pip install` 命令及内部索引要求 |
| 校验信息 | 两端镜像摘要、原声明 SHA-256 | wheel 文件名、SHA-256 |
| 来源 | 原构建提交、算子目录、原构建/验收流水线 | Nexus 记录中的原提交、包目录、原构建/发布流水线 |
| 下载入口 | 原声明、单算子源码、适用的版本发布清单 | wheel、单包源码、Nexus `release.json` |

算子首次登记从**本次交付提交**读取人工说明；补登记从指定清单的 `sourceCommit` 读取，不读取当前工作区的新内容。镜像源码及下载链接始终取原构建提交，两者可能不同。共享包使用发布时维护的独立版本说明。

已存在的 Release 保留人工说明。重试只核对或补齐自动区块和受管理资产，不覆盖人工修订；Git 中更新说明也不会改写旧 Release。修订已登记的说明时，编辑 GitLab Release 自动区块之外的内容。不得更改已发布版本的源码、摘要等事实。

## Assets 下载资产

Assets 仅放可下载的具体文件；镜像地址、命令、源码浏览页、MR 和流水线链接放在 Release notes。

| 对象 | 资产 | 规则 |
|---|---|---|
| Package | wheel | 链接 Nexus `pypi-internal` 的精确文件，不在 GitLab 重传副本。 |
| Package | 本包源码（tar.gz） | 固定原提交和包路径；仅包含本包，不包含依赖。源码归档不等同于 Python sdist。 |
| Package | 发布记录 `release.json` | 链接 Nexus Data 的对应版本记录。 |
| Operator | `operator.yaml` | 固定原镜像构建提交下的声明，不使用分支最新文件；声明内为业务版本，候选拉取地址以 Release 正文为准。 |
| Operator | 本算子源码（tar.gz） | 固定原构建提交和算子目录，不包含其他算子、共享包或 SDK。 |
| Operator | 版本发布清单 | 有对应清单时，链接登记所依据分支的精确 YAML blob；后续累计清单不替换已有链接，不声称它是首次交付快照。 |
| Operator | 平台注册 JSON（按需） | 仅附加与实际发布版本对应、已验证的原文件；目前不自动生成此资产，不使用新工具重建旧文件冒充原件。 |

GitLab 默认 Source code 是全仓归档，与“本包源码”“本算子源码”区分。外部下载遵循 Nexus/Harbor 原有访问权限，Release 不附带凭据。[GitLab Release 资产链接](https://docs.gitlab.com/api/releases/links/)

## 失败与恢复

- GitLab 同步失败使当前发布作业失败，已完成的 Nexus 或镜像交付保留。重试复用已交付内容，不重建或回滚。
- 标签、版本、摘要或自动区块事实冲突时停止，不移动标签或覆盖已发布内容。
- 原作业不可重试时，按[补登记指引](DEVELOPMENT_AND_CI.md#算子-release-同步与恢复)指定分支、精确清单及构建记录。历史 Dev、RC、Stable 均可补登记；不自动批量补齐全部历史版本。
- GitLab 登记时间不代表原构建或首次交付时间。原生 Evidence collection 不参与本仓库发布判断，不增加额外采集步骤。

GitLab Release 必须关联标签，CI 在创建 Release 时指定原始提交；不会把补登记提交作为源码版本。[GitLab Releases API](https://docs.gitlab.com/api/releases/#create-a-release)
