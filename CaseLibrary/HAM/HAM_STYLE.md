# HAM PLC 编程风格画像

本画像来自当前打开的 `2020_HAM024_48LV_Turnt 1` 客户工程，用于后续生成或更新
HAM 案例。它描述现有项目风格，不等同于通用 TwinCAT 编码规范。

## 结构

- 机器由 `MAIN`、站级 FB 和设备级 FB 分层组织。
- 复杂 FB 的主体只负责按固定顺序调用 Method。
- Method 使用字母前缀固定执行顺序：`A_Init`、`B_Constants`、`C_FB_Calls`、
  `D_ERROR_Handling`、`E_Timers_Edge_Detection`、步骤序列、其他逻辑、输出。
- 站级程序以数值步骤变量组织自动/手动顺序，并把超时、报警和输出拆到独立 Method。
- 设备功能优先封装为 FB；运动控制动作拆为 `ActHome`、`ActMove`、`ActJog` 等 Method。

## 命名

- BOOL 使用 `b` 前缀，字符串使用 `s`，整数常用 `i/n`。
- FB 实例使用 `fb`，数组使用 `arr`，TON 实例常用 `ton`，边沿实例常用 `rtrig`。
- 功能块使用 `FB_`；结构使用 `ST_`；函数同时存在 `FUN_`、`FC_` 等历史前缀。
- HAM 新案例保留现有公开对象名；新增内部变量遵循相同匈牙利式前缀。

## 行为约定

- 周期 FB 调用集中放在 `C_FB_Calls`。
- 定时器和边沿检测集中放在 `E_Timers_Edge_Detection`。
- 错误、警告和步骤超时分别封装，站级 FB 通过数组汇总消息。
- I/O 映射集中在 GVL 或设备 FB 的 `AT %I* / AT %Q*` 变量中。
- 外设协议案例必须在 manifest 中明确所需库、端子和现场链接。

## 模板化规则

- PLCopen XML 是 HAM 案例的发布载体，保证 Method 和文件夹层级不丢失。
- 原始案例先以 `maturity: reference` 保存；完成依赖补齐和独立编译后才改为
  `maturity: reusable`。
- 项目专用地址、站号、配方路径和设备名称不得直接作为新模板默认值。
- 更新同一案例时保持 `id` 不变并提升语义版本号。

