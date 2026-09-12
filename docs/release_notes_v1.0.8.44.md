# TwinCAT Agent v1.0.8.44

## 改进

- 工作节点树继续深入到 PLC 对象级：FB、Program、Function、GVL、DUT、Interface。
- FB 下可继续展开 Method、Property 及其 Get/Set 节点。
- 不同对象类型显示不同图标，搜索框支持直接筛选 `FB_`、`POU`、`Interfaces` 等节点。
- 选择具体 FB 后，会把完整 XAE 原生树路径注入当前对话上下文。
