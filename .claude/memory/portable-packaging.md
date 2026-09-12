---
name: portable-packaging
description: TwinCAT Agent 便携测试包构建 — 后端依赖闭包只有 websockets、embeddable Python + ._pth、扩展机器级安装
metadata:
  type: reference
---

# TwinCAT Agent 便携包（免安装 Python）构建

一键脚本 **`scripts/build_portable.ps1`**（`-NoDocs` 跳过文档索引、`-Zip` 出压缩包）。
产物在 `dist/TwinCAT-Agent-Portable\`(已 gitignore)。

## 关键洞察：后端运行时依赖闭包极小
自研大脑的后端(`tc_agent`)真正的运行时依赖只有 **`websockets`(纯 Python)+ 系统自带
PowerShell + `TcCom.ps1`**。**不需要 pywin32/click/jinja2**：
- `tc_template/__init__.py` 是空的(只有版本号),不拉重依赖。
- COM 全走 **PowerShell 桥**(`_ps_bridge` → `powershell.exe -File TcCom.ps1`),不用 pywin32。
- `tc_template/plc.py` 里的 `import win32com.client` 是**函数内惰性导入**(仅 COM 路径),
  后端只用到 `_ps_bridge` + 惰性 `from .plc import _parse_variables`(纯文本解析,不碰 COM)。
所以能做成真正便携:embeddable Python + 一个纯 Python 包,零编译依赖。

## 包结构
```
runtime\python\      embeddable Python 3.14.0 amd64 + Lib\site-packages\websockets(内置)
app\tc_agent\        后端(含代理直连修复 + static/index.html;UI 由后端 8766 端口托管)
app\tc_template\     _ps_bridge.py + plc.py + TcCom.ps1 + __init__.py
app\data\ba-docs\index.db   374MB 倍福文档索引(docsearch 相对 app 定位:tc_agent 的 parent.parent)
extension\TwinCAT Agent\     XAE 扩展(deploy_stage 里的 DLL/清单/pkgdef/webview)
Start-Backend.vbs/.cmd, Install-Extension.ps1, Uninstall.ps1, README.txt
```

## embeddable Python 的坑
- 下载 `python-3.14.0-embed-amd64.zip`(python.org 可直连,~11MB)。缓存在 `dist\_cache\`。
- **`._pth` 决定 sys.path**(embeddable 忽略 cwd/PYTHONPATH)。写成:
  `python314.zip` / `.` / `Lib\site-packages` / `..\..\app` / `import site`。
  `..\..\app` 相对 python.exe 目录(runtime\python\)→ 包根\app,于是 tc_agent/tc_template 可导入。
- websockets 直接把 site-packages 里的 `websockets\` 拷进 `Lib\site-packages\`;它的
  `speedups.cp314-win_amd64.pyd` 与 embeddable 3.14 ABI 一致,能用;websockets 无其它依赖。
- 冒烟测试:**从包根**(非 app\)跑 `runtime\python\python.exe -c "import tc_agent.backend, ...; docsearch.available()"`
  才能证明 ._pth 生效(而非 cwd 撞对)。实测 27 工具、docsearch 指向内置 index.db=True。

## 面板 UI 是【本地文件】,不走 8766——必须构建时同步(实测坑)
`ChatControl.xaml.cs` 里 WebView 用 `CoreWebView2.Navigate(file://.../webview/index.html)`
**加载扩展目录里的本地 `webview/index.html`**,不是后端 8766 端口托管的那份。所以真源
`tc_agent/static/index.html` 改了以后,**扩展里的 `webview/index.html` 必须同步**,否则
面板显示旧界面(实测:便携包装上后 Provider 设置是 264 行旧版,真源已 533 行)。
- 单一真源 = `tc_agent/static/index.html`(只连 ws://8765,无 http fetch,当本地文件加载 OK)。
- `build_portable.ps1` 每次构建强制 `Copy-Item static/index.html → 扩展 webview/index.html`。
- 已装好的机器只想换界面:发个小包(`index.html` + 自提权 `Update-UI.ps1`)替换
  `<TcXaeShell>\Common7\IDE\Extensions\TwinCAT Agent\webview\index.html`,重启 TcXaeShell 即可,
  不用重装 112MB。换完若还旧 = WebView 缓存,关掉面板窗口再开一次。
- 待办(更优架构):把 WebView 改成 `Navigate("http://127.0.0.1:8766")`,UI 就只需更新
  后端可写的 `app/tc_agent/static/`,永不陈旧、免管理员——但要重编 VSIX DLL。

## 目标机安装(README 里写清)
1. 整包拷到**可写**目录(别放 Program Files——后端要在包内写日志/历史)。
2. `Install-Extension.ps1`(自动提权)把 `extension\TwinCAT Agent\` 拷到
   `<TcXaeShell>\Common7\IDE\Extensions\TwinCAT Agent\`(机器级扩展,重启 TcXaeShell 自动发现;
   **绝不** devenv /setup)。前提:目标机已装 TcXaeShell。
3. 双击 `Start-Backend.vbs` **非提权**起后端(8765/8766);浏览器开 http://127.0.0.1:8766 自检。
4. **不打包 config.json**(含密钥);用户在面板 ⚙ 里加自己的 Provider+Key。

## 构建注意
- 脚本/README 含中文的 `.ps1`/`.txt` **必须 UTF-8 BOM**,否则 PS5.1 读成 ANSI 乱码 → 解析报错。
  Write 工具存的是无 BOM UTF-8,构建脚本用 `UTF8Encoding($true)` 重存;脚本本身也要 BOM
  (用 Python 前置 `\xef\xbb\xbf`)。
- robocopy 退出码 1-7 是**成功**(有文件复制);`if($LASTEXITCODE -ge 8)` 才算失败;脚本末尾
  `exit 0` 避免把 robocopy 的 1 当成整体失败。
- 排除密钥/历史:`config.json`、`chat_history.json`、`.tc_agent_history.json`、`*.bak`。
- 398MB 目录 → `.NET ZipFile Optimal` 压到 ~112MB、~10s(比 Compress-Archive 快)。

相关:[[coagent-backend-operations]]、[[docsearch-integration]]、[[vsix-isolated-shell-lessons]]、[[com-dynamic-dispatch-py314]]。
