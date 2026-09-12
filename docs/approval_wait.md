# 人工审批等待

单次 permission_request 不设置人工响应超时。后台 coroutine 等待对应 Future，
只有有效的同意/拒绝响应或任务取消才结束等待，不因用户暂未操作自动拒绝。
停止任务仍传播 CancelledError，清理 pending/FOREGROUND_PERMISSIONS 并将持久化
审批标为取消。现有请求身份匹配、XAE/目标校验和执行前置条件不变。

这不承诺跨后端进程重启保留可执行授权，也不移除工具执行、模型请求或其他
服务的超时。后台结束时未决审批不得成为自动同意。
