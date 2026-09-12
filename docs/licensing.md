# TwinCAT Agent 设备授权与发码

## 工作方式

TwinCAT Agent 使用离线的“设备码 + RSA 签名授权码”：

1. 客户首次打开面板，只会看到产品授权页。
2. 授权页显示 GUID 格式的本机 TwinCAT System ID。
3. 客户把设备码发给供应商。
4. 供应商使用私钥签发授权码，可指定客户名和有效期。
5. 客户粘贴授权码后进入 Agent。

授权码只能用于签发时指定的 TwinCAT System ID。复制软件目录、复制 `license.json`
或把授权码发送给另一台电脑，都无法通过设备校验。System ID 改变后需要重新签发。

客户端只包含 RSA 公钥，不能自行生成授权码。私钥不会进入 Git、便携包或客户电脑。

## 私钥

当前产品私钥保存在开发机：

```text
G:\claude\TwinCAT-Agent-License-Secrets\private_key.pem
```

请立即把它备份到至少一个加密的离线介质。私钥丢失后，无法为已经发布的客户端签发
新授权；更换私钥则必须重新构建和发布客户端。

严禁：

- 把 `private_key.pem` 放进本仓库。
- 通过微信、邮件或普通网盘发送私钥。
- 把私钥打进安装包。
- 在客户电脑上运行发码工具。

## 给客户签发授权码

### 推荐：使用桌面授权工具

解压供应商工具包：

```text
dist\TwinCAT-Agent-License-Issuer.zip
```

双击 `TwinCAT-Agent-License-Issuer.exe`，依次选择私钥、填写客户 TwinCAT System ID 和客户名，
设置有效天数或永久授权，然后点击“生成授权码”。可以直接复制授权码或保存为文本。

授权工具是供应商内部工具，不能发给客户。工具包本身不含私钥，第一次使用时需要选择：

```text
G:\claude\TwinCAT-Agent-License-Secrets\private_key.pem
```

工具只在 `%LOCALAPPDATA%\TwinCAT Agent License Tool\settings.json` 记住私钥路径，
不会复制或保存私钥内容。

### 查询授权记录

桌面授权工具每次成功生成授权码后，会自动写入本机 SQLite 记录库：

```text
%LOCALAPPDATA%\TwinCAT Agent License Tool\license_records.db
```

点击主界面右上角的“查询授权记录”，可以：

- 按客户/公司名称搜索。
- 按 TwinCAT System ID 搜索。
- 按授权编号搜索。
- 查看签发时间和有效期。
- 复制历史授权码。
- 把历史记录重新载入发码界面。

覆盖升级发码工具不会删除记录库。请定期备份 `license_records.db`；其中包含已签发的
完整授权码，属于供应商内部业务资料，不能发送给客户。此功能启用前签发的授权不会
自动出现在记录库中，除非重新签发。

### 导入申请与一键回复

授权工具顶部的“导入申请表…”支持官网管理员接口导出的
twincat-agent-submissions.csv，也支持包含单条申请对象的 JSON 文件。CSV 中的反馈记录
会自动跳过；多条临时使用申请会弹出选择窗口。导入后会自动填充客户、System ID、申请人和
回复邮箱，并保留 TwinCAT 版本、目标控制器和试用场景。
导入时工具会按 System ID 查询本地授权记录库，并在申请选择列表和主界面显示最近一次签发
状态。若已经授权，点击生成新授权前会再次确认；这不会阻止有意的续期或重新签发。

签发授权后点击“生成回复邮件”，工具会调用 Windows 默认邮件客户端打开一封已填写收件人、
主题、授权编号、有效期、完整授权码和客户安装包下载链接的草稿。若要直接从 Gmail 发出，可填写发件邮箱和
应用专用密码后点击“发送回复邮件”；工具使用 smtp.gmail.com:465 SSL 发送，并在发送前
弹出确认。应用专用密码只保存在当前进程内，发送后立即清空，不写入配置文件。发送前请人工
核对收件人、System ID 和授权期限。邮件正文同时包含基础使用教程和 AI 工程辅助免责声明。
客户安装包下载入口包含百度网盘和 GitHub Release：

    百度网盘：https://pan.baidu.com/s/1s-wgTx71ggFG0UZOxW5pmQ?pwd=8888
    提取码：8888
    GitHub：https://github.com/AutomationAgent-Code/TwinCAT-Agent/releases/latest

默认发件邮箱为 zxctian@gmail.com。Gmail 账户需要开启两步验证并创建应用专用密码；不要在
聊天、代码仓库或普通配置文件中传递 Gmail 登录密码或应用专用密码。

官网导出入口：

    https://twincatagent.com/admin/export.csv

该地址需要管理员 Basic Auth。下载后直接导入 CSV 即可。

### 命令行方式

开发机需要 Python 和 `cryptography`：

```powershell
py -3.14 -m pip install cryptography
```

一年授权：

```powershell
py -3.14 scripts\license_issuer.py issue `
  --key "G:\claude\TwinCAT-Agent-License-Secrets\private_key.pem" `
  --device "客户发来的-TwinCAT-System-ID" `
  --customer "客户公司名称" `
  --days 365
```

永久授权：

```powershell
py -3.14 scripts\license_issuer.py issue `
  --key "G:\claude\TwinCAT-Agent-License-Secrets\private_key.pem" `
  --device "客户发来的-TwinCAT-System-ID" `
  --customer "客户公司名称" `
  --perpetual
```

同时保存授权码到文件：

```powershell
py -3.14 scripts\license_issuer.py issue `
  --key "G:\claude\TwinCAT-Agent-License-Secrets\private_key.pem" `
  --device "客户发来的-TwinCAT-System-ID" `
  --customer "客户公司名称" `
  --days 365 `
  --out ".\客户名称-license.txt"
```

把命令输出中 `TCAG1.` 开头的完整字符串发给客户即可。授权码包含客户名、授权编号、
签发时间、到期时间和设备码，任何手工修改都会使签名失效。

命令行签发也会自动写入同一个记录库。查询示例：

```powershell
py -3.14 scripts\license_issuer.py records --query "客户名"
py -3.14 scripts\license_issuer.py records --query "System-ID" --json
```

## 客户端授权文件

授权成功后记录保存在：

```text
%LOCALAPPDATA%\TwinCAT Agent\license.json
```

它不会随项目保存，也不会进入便携包。覆盖升级 TwinCAT Agent 时授权仍然保留。

设备码直接采用 TwinCAT System ID，格式例如：

```text
447DAA40-52F5-DB2C-D3F8-3B1804726B7E
```

客户端优先通过本机 ADS License Server（AMS 端口 30）读取；如果服务暂不可用，
再从已有 `.tclrs` / `.tclrq` 许可证文件读取。两条路径得到的是同一个 System ID。
不再使用 Windows MachineGuid、硬盘、网卡、计算机名或自行拼接的主板指纹。

如果 TwinCAT 尚未安装/启动、ADS Router 不可用且本机没有许可证文件，授权页会明确
显示读取失败，不会无限停留在“读取中”。启动一次 TwinCAT 后刷新即可。

参考：

- [Beckhoff InfoSys：TwinCAT 3 System ID](https://infosys.beckhoff.com/content/1033/tc3_licensing/3511047947.html)
- [Beckhoff InfoSys：FB_GetSystemId](https://infosys.beckhoff.com/content/1033/tcplclib_tc2_utilities/35002507.html)
- 本机 TwinCAT SDK：`TcLicenseServices.h` / `TcLicenseInterfaces.h`

## 安全边界

离线授权能可靠阻止普通复制和授权码共享，但任何纯客户端方案都无法绝对阻止专业人员
修改二进制、调试进程或篡改系统时间。本版本会检测明显的时间回拨。产品规模扩大后，
可增加在线激活服务器、设备占用记录、吊销列表和定期租约校验。
