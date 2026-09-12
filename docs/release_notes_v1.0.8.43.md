# TwinCAT Agent v1.0.8.43

## 修正

- 工作目录选择器现在读取当前 XAE 的实时 System Manager 树。
- 树层级与解决方案资源管理器一致：解决方案、工程、系统、PLC、运动控制、I/O 等节点可展开。
- PLC 节点可继续展开到 PLC 项目、DUTs、GVLs、Interfaces、POUs 以及方法/属性节点。
- 保存的是 TwinCAT Automation Interface 原生树路径，不再使用磁盘相对目录。
