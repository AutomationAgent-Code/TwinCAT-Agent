# TwinCAT Agent 更新发布

更新服务器只公开安装产物，不公开本仓库、工具定义、PLC 规则或构建脚本。

## 对外目录

每个通道只包含以下文件：

```text
stable/
  TwinCAT-Agent-Setup-v1.2.3.exe
  TwinCAT-Agent-Setup-v1.2.3.exe.sha256
  latest.json
  release-notes.md              # 可选
```

`latest.json` 是客户端检查更新时唯一需要读取的文件。必须通过 HTTPS 提供，并保持其
URL 稳定，例如 `https://updates.example.com/twincat-agent/stable/latest.json`。

## 发布步骤

1. 在私有源码仓库完成测试，更新 `VERSION`。
2. 构建安装包：

   ```powershell
   .\scripts\build_installer.ps1
   ```

   产物为 `dist\TwinCAT-Agent-Setup-v<版本>.exe` 及同名 `.sha256` 文件。

3. 编写本次 `release-notes.md` 后生成纯发布目录：

   ```powershell
   .\scripts\Publish-UpdateRelease.ps1 `
     -InstallerPath .\dist\TwinCAT-Agent-Setup-v1.2.3.exe `
     -PublicBaseUrl https://updates.example.com/twincat-agent `
     -Channel stable `
     -NotesFile .\release-notes.md
   ```

4. 只上传 `dist\update-feed\stable\` 内的文件到 HTTPS 服务器。不要上传整个 `dist`、
   仓库目录或安装器构建临时目录。
5. 在一台非开发电脑上下载并校验 `.sha256`，再安装验证。

## GitHub Releases（当前推荐）

可使用公开仓库 `AutomationAgent-Code/TwinCAT-Agent` 仅托管 Release 资产。仓库中
不提交本项目源码、工具定义或构建目录；公开的只有安装包及其校验清单。安装 GitHub CLI
后，先在发布者电脑上执行 `gh auth login`，再运行：

```powershell
.\scripts\Publish-GitHubRelease.ps1 `
  -InstallerPath .\dist\TwinCAT-Agent-Setup-v1.2.3.exe `
  -Channel stable `
  -NotesFile .\release-notes.md
```

客户端稳定更新清单地址固定为：

```text
https://github.com/AutomationAgent-Code/TwinCAT-Agent/releases/latest/download/latest.json
```

注意：公开 GitHub Release 代表任何人都可以下载安装包，但不能浏览你的本地源码或工具层。
若安装包本身也只能给特定客户下载，应改用带鉴权的 HTTPS 文件服务；不要把 GitHub token
内置到客户端。

当前便携包构建会将 Agent 和 COM 工具层转换为 Python 字节码，并拒绝把可读 `.py` 文件
带入安装包。这是防止直接浏览源码的交付保护，不是不可逆的加密；不能被客户获得的核心
算法、规则或密钥必须放在服务端。

## 安全要求

- 更新源必须使用 HTTPS；发布脚本拒绝 HTTP 地址。
- 客户端更新前应校验 `latest.json` 中的 SHA-256。
- 正式对外发布时应为 `Setup.exe` 配置 Windows 代码签名证书。
- 使用稳定版与测试版两个独立目录；测试版使用 `beta/`，不要覆盖 `stable/latest.json`。
