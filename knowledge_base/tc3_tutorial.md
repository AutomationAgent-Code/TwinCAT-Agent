# TwinCAT 3 入门教程 V4.18 — 知识要点

来源: TwinCAT 3入门教程V4.18.pdf (243页, 2026年4月, 毕孚自动化)

---

## 全书结构

1. **TwinCAT 3 安装** — Full vs XAR, VS Shell, 帮助系统安装
2. **授权激活** — 7天试用, IPC/EPC正式版, EL6070/C9900-L100 Dongle, 4022新版Dongle流程, 错误代码汇总
3. **扫描IO和变量连接** — 连接目标控制器, 广播搜索, 变量映射
4. **IEC61131-3 标准** — 数据类型, 变量声明, ST语言, 函数与功能块区别
5. **PLC 简单程序编写与调试** — 创建项目, ST编程, 在线监控, Flow Control, 交叉索引
6. **PLC HMI 可视化编程** — 按钮/计量仪/自定义控件/画面切换/动态文本/语言切换/Dialog/全屏/用户管理/HMI-Web
7. **Measurement 功能** — Scope View, 数据保存
8. **库管理** — 库的引用/新建/安装
9. **代码管理** — 下载/上传, 项目保存/打开, PLC工程保存/打开, POU保存/打开, 版本切换
10. **C++ 编程**
11. **Motion 运动控制**
12. **Safety 安全编程**

---

## 第6章重点: HMI 可视化编程 (p.67-110+)

### 6.1.1 新建可视化项目
- 右键 PLC 项目 → Add → Visualization
- 命名为 Visualization（或自定义）

### 6.1.2 按钮控制小灯
1. Toolbox 添加 Button 和 Ellipse
2. Ellipse 属性: Color variables → 绑定 PLC 变量
3. Button 属性: Input configuration → Toggle bit
4. 变量链接: 选中控件 → 属性 → 填写 `MAIN.myVar`

### 6.1.3 计量仪添加
- Toolbox → Measurement Controls → Meter
- 绑定变量到仪表的值

### 6.1.4 非固定常量的输入与显示
- Text Box 绑定变量实现输入/显示

### 6.1.5 Dialog 会话框
- 创建 Dialog 子窗口

### 6.1.6 自定义控件
- Interface Editor 定义输入输出接口
- 把组合控件保存为自定义控件供复用

### 6.1.7 画面切换
- Frame + 切换逻辑

### 6.1.8 动态文本显示
- Text formatting with variables

### 6.1.9 语言切换
- 多语言支持

### 6.2 全屏显示
- HMI 全屏配置方法（新版修改）

### 6.5 用户管理
- Visual User Management

### 6.6 HMI-Web
- TF1810 HMI Web 插件
- 无线网络访问

---

## 第9章重点: 代码管理 (p.193-207)

### 9.1 代码下载与上传
- PLC 代码 → 目标控制器
- 从目标控制器上传

### 9.2 代码保存
- 9.2.1 整个项目保存/打开 (.sln, .tszip)
- 9.2.2 PLC 工程保存/打开 (.plcproj, .tpzip)
- 9.2.3 Program/FB/Function/Interface/Visualization 的保存/打开
- 9.2.4 Safety 项目保存/打开
- 9.2.5 C++ 项目保存/打开
- 9.2.6 组态保存/打开

### 9.3 版本切换
- Remote Manager 切换 TwinCAT 版本

---

## 与 skill 开发相关的要点

### HMI 可视化关键属性 ID (p.68-69, 按钮控制小灯示例)
TwinCAT VISU XML 属性通过数字 ID 标识, 已知映射:
- 571893170L → 名称/文本
- 2341735680L → 边框颜色 (Element-Frame-Color)
- 438423234L → 报警边框颜色
- 2678395525L → 文本水平对齐 (1=HCENTER)
- 2340015797L → HCENTER
- 2565699834L → VCENTER  
- 1603690730L → 字体
- 4253639993L → 字号 (12)
- 2729990903L → 字体颜色
- 3488306084L → 背景色 (4278190080U=transparent)
- 1999528970L → 点击变量 (`<toggle/tap variable>`)
- 3719097617L → X坐标
- 1649127785L → Y坐标
- 357335551L → 宽度
- 2422045748L → 高度
- 394923068L → 是否可见
- 2322377816L → 边框样式 (NO_FRAME)
- 3549563837L → 缩放模式 (ANISOTROPIC)
- 2501871159L → 变量绑定值 (如 "MAIN.Machine.PackMLBaseModule_HMI")
- 2473092364L → 引用的Visualization类型
- 363316305L → 引用列表
- 493260384L → 元素背景色
- 135947015L → 元素边框色
- 2597686782L → 某种布尔属性

### VISU XML 结构
- `<VisualElemMemberList>` → 属性列表 (ID→Value)
- `<VisualElementName>` → 元素类型 ("Frame", "Rectangle", "Ellipse", "Button"等)
- `<VisualElementTypeName>` → FB类型 ("VisuFbFrame", "VisuFbButton"等)
- `<VisualElementIdentifier>` → 唯一标识 ("GenElemInst_1")
- `<VisualElementIdentification>` → GUID

### PLC 变量绑定到 VISU
格式: `MAIN.myVariable` 或 `MAIN.Machine.PackMLBaseModule_HMI`

### 创建自定义控件的接口
Interface Editor → 定义输入输出变量 → 可在其他 VISU 中复用
