# FrLO 发布仓库

这是 FrLO 的独立发布源仓库，使用单一 `main` 分支；整合包源文件、版本与渠道、更新说明、构建工具和工作流一起提交。当前项目配置为 Modrinth `64radtE0`、`client_and_server`。本仓库不使用 Arcomua-Modpack 的 Cloth/Anvil 多产品分支流程。参考实现：Arcomua-Modpack `origin/Main`（检查时 SHA `74d7d65785b5d3598296839787746b0c5d110550`）。

## GUI 发布

1. 准备从 Prism Launcher 导出的**完整 `.mrpack`**。导出时包括 `config/yosbr` 和所需的 `mods`、`resourcepacks`、`shaderpacks`、默认世界模板等。不要导入只含 `config` 的 ZIP；它不是整合包源文件。
2. 双击 `发布FrLO.cmd` 启动助手；已有 `dist/FrLO-Publisher/FrLO-Publisher.exe` 时会直接运行，否则需要 Python 3.11 或更新版本。也可运行 `python -m frlo_release.gui`。
3. 选择 `.mrpack`，点“导入 / 预处理”。导入后填写版本号与渠道（`release`、`beta`、`alpha`），补充手写更新说明；自动文件变化记录保存在 `release/changes.md`。
4. 先点“本地构建”检查产物。Modrinth `.mrpack` 输出至 `dist/<版本>/`。勾选 CurseForge 时还会构建其 ZIP；若有未解析文件，会停止生成该包并报告位置，见下文。
5. 配置好凭据和目标平台后，选择“本地发布”或“云端发布”。本地发布中的 GitHub Release 要求发布源在 `main` 且当前提交已提交并推送。云端发布会提交明确列出的源文件、推送 `main`，再触发 GitHub Actions；push 触发的 `build.yml` 只构建 Modrinth 包，不会发布。

也可以在发布目录直接运行：

```bash
python -m frlo_release import "D:/exports/FrLO.mrpack"
python -m frlo_release build --formats modrinth
python -m frlo_release build --formats modrinth,curseforge
python -m frlo_release resolve-curseforge
python -m frlo_release publish --targets github,modrinth
python -m frlo_release dispatch --targets github,modrinth
```

## CurseForge 匹配与发布

`config/project.json` 中 CurseForge 项目 ID 目前为空，尚不能向 CurseForge 上传。创建并配置 CurseForge 项目后，填入其数字项目 ID。首次匹配可在 GUI 点“查询 CurseForge ID”，或执行 `resolve-curseforge`；该查询需要 `CF_API_KEY`。它按文件指纹查找候选，再核对 SHA-1、文件大小、Minecraft/加载器兼容性，并把确认的文件 ID 固定到 `config/curseforge-lock.json`。该锁文件应随发布源提交。

不能表示为 CurseForge 依赖清单的文件会列在 `build/curseforge-unresolved.json`，存在未解决项时 CurseForge 包构建失败，不会静默丢弃文件。获准以 override 文件随包发布的第三方内容，必须在锁文件的 `overrides` 中按文件 SHA-512 明确记录 `approved: true`、HTTPS `source` 和 `license`；这表示维护者已核准该项，不会由下载链接或指纹自动推断授权。完成匹配/审核并重新构建后，再选择 CurseForge 发布。

上传成功只代表已提交审核：界面和回执会将 CurseForge 状态记录为 `submitted-for-review`，不保证平台审核通过或立即公开。

CurseForge 构建只使用已提交的锁定映射，不在发布时临时解析。云端发布 CurseForge 时需同时选择 GitHub，以保留跨运行的上传回执。`approved` 还要求该站外模组已获 CurseForge 第三方名单批准；仅有宽松许可并不能保证整合包通过审核。

## 凭据

- `MODRINTH_TOKEN`：Modrinth 项目令牌，用于查询和创建版本。
- `CURSEFORGE_TOKEN`：CurseForge 作者令牌，用于上传项目文件。
- `CF_API_KEY`：CurseForge API Key，仅用于指纹查询匹配；与上传令牌用途不同。
- GitHub CLI（`gh`）：登录后用于云端发布、写入 Actions Secrets、提交/推送源和 GitHub Release 操作。

GUI 的凭据输入仅供本次进程会话使用，不写入配置文件。可用“保存已填写凭据到 GitHub Secrets”将输入值保存为仓库 Actions Secrets；对应名称为上面的变量名。不要把令牌粘贴进配置、更新说明或提交文件。

## 版本与失败恢复

已发布版本不可覆盖：若同一版本的远端文件哈希不同，发布会拒绝继续，请修改版本号。相同版本且文件哈希一致时，已有平台上传会跳过。GitHub Release 在多平台上传期间保持草稿，并附带 `publish-result.json` 回执；部分平台完成后失败时，可检查 Actions 日志/产物，再以相同版本重试未完成的平台。

网络上传请求不会因连接中断自动重试，因为服务端可能已经接收文件。若 CurseForge 上传发生连接错误，先检查项目文件列表和 Actions 回执，再决定后续操作，避免重复上传。

`curseforge-pending.json` 与 `curseforge-uploaded.json` 是不会覆盖的上传状态标记。只有 pending 时工具会阻止再次上传。确认平台实际状态后，维护者需按实际文件 ID 修复回执；不可通过删除标记盲目重试。

`b0.0.2` 的已发布源仅作演示和流程验证。后续 `beta0.0.3` 等正式源必须从实际 Prism 导出 `.mrpack` 导入并检查；不要把仅有配置文件的 ZIP 当成完整整合包发布。

## 输出

- `dist/<版本>/FrLO-<版本>.mrpack`：Modrinth 包。
- `dist/<版本>/FrLO-<版本>-CurseForge.zip`：CurseForge 格式包（仅在匹配完整且无未解决项时生成）。
- 同目录 `CHANGELOG.md`、`SHA256SUMS.txt`、`build.json`：更新说明、校验和及构建信息。
- `build/curseforge-unresolved.json`：未匹配/未核准的 CurseForge 文件及原因。
- 发布结果与部分完成回执保存在 `publish-result.json`，云端运行还会上传为 Actions artifact。

## GUI 可执行文件

需要重新打包时，在 Windows PowerShell 中从本目录运行：

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\build-gui.ps1
```

生成的 `dist/FrLO-Publisher/FrLO-Publisher.exe` 需要与本发布仓库目录结构放在一起运行。该打包脚本会安装 PyInstaller。

GitHub Actions 的 `Build Windows GUI` 也会构建程序，产物名为 `FrLO-Publisher-Windows`；下载后放到此仓库的 `dist/FrLO-Publisher/`。

协议参考：[Modrinth Create Version](https://docs.modrinth.com/api/operations/createversion/)、[CurseForge Upload API](https://support.curseforge.com/support/solutions/articles/9000197321-curseforge-upload-api)、[CurseForge 导出要求](https://support.curseforge.com/support/solutions/articles/9000198500-exporting-a-modpack-for-curseforge-project-submission)。
