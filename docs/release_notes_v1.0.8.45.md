# TwinCAT Agent v1.0.8.45

## 修正

- 修复指定 XAE PID 因管理员权限不同而无法读取工程树的问题。
- 工程树读取优先使用当前绑定的 XAE；仅在 native ROT 不可见时回退到同权限可见实例。
- PLC 对象级展开继续支持 FB、Program、Function、GVL、DUT、Interface 及成员节点。
