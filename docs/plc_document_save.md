# 单个 PLC 文档保存

新增 `plc_save_document`，按次审批（auto/accept 也不能绕过）。

1. 用户明确要求保存后，先 `plc_read(name,path,document_baseline=true)`。
2. 原样传回 `document_baseline` 作为 `expected_document_baseline`，申请本次保存。
3. 原生桥严格绑定 XAE PID；通过已登记 tsproj/plcproj 映射精确文档，比较解决方案、窗口、代码/成员树、磁盘哈希、Saved 状态。
4. 只调用该已打开父文档的 `Document.Save`；不保存其他文档，不关闭解决方案，不重放不确定操作。
5. 验证 Saved、保存返回值、完整源代码及成员与磁盘 XML 一致。保存不代表编译/上线验证。

不要求文档预先 saved=true。基线过期需重新读取、重新申请审批；没有明确保存授权时不能用该工具解除其它写工具门禁。

当前范围：已登记且父文档已打开的 ST POU、GVL、DUT、接口（仅安全可读取节点）。只有成员编辑器、外置 XTI 映射无法确认、非 ST/不支持的成员类型或不完整读取均拒绝，不猜文件或自动打开。XML 核对忽略 CRLF/LF 差异，但不忽略代码空白内容。
