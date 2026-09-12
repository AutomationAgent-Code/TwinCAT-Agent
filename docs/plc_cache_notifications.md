# PLC 活动文档通知合并（2026-09-06）

问题：XAE 的文档激活事件既通过扩展 HTTP 又通过 WebView WebSocket 通知后台。
旧流程逐条同步 COM 回读，并在校验前清空整个缓存。快速切换文件/成员时，较早
事件与回读时的活动文档不一致，正常的过期事件被报告为错误。

修复契约：

- 网页端 80 ms 防抖，只提交最新活动 PLC 文档的身份。切到非 PLC 页面或关闭活动
  文档时撤销尚未发送的通知。源码仍由后台 COM 回读，不信任客户端提供的内容。
- HTTP 和 WebSocket 进入同一 PID 级更新队列，250 ms 合并窗口；每个 PID 最多
  一个 COM 回读正在执行。接收通知立即返回 `status=queued`，不是已更新成功。
- 排队时仅使对应 PID/解决方案/文件/成员的缓存失效，不清空其他文件或 XAE 缓存。
- `StaleCacheNotification` 表示活动对象已经变化，后台静默重新读取当前活动对象。
  新对象始终以它自己回读出的真实路径和成员存储，绝不写入旧事件的缓存键。
- 如果已切到 HMI 的 `.view/.content/.usercontrol` 等非 PLC 页面，不发起 PLC
  源码读取；若在检查与读取之间切换，则再次核对会话后以 `ignored_non_plc`
  正常结束。不继续重试、不记录错误，也不影响其他 PLC 缓存；真正 COM 失败仍报告。
- 最新通知的 generation 校验防止旧回读覆盖新事件；读取期间出现全局缓存失效
  时也不重新插入旧值。PLC 写操作原有的全局失效规则不变。
- PID 和解决方案校验不放宽。真正失败记入后台日志与 `GET /__plc_cache` 的
  `refresh` 诊断（queued/reading/updated/resynced/superseded/failed），不会用重试
  偷偷切换解决方案。用户轮次开始时的同步预热仍保留。
- 不需要更新原生 DLL；兼容原扩展的 HTTP POST，过期通知不再得到 400。

测试：`tests/test_cache_refresh_queue.py` 覆盖双通道合并、过期重同步、在途事件
竞争、成员/项目隔离、写入失效保护、无效通知和真实本地 HTTP 接收链路；
`scripts/test_industrial_ui.cjs` 覆盖快速切换、最新事件与非 PLC 页面取消。
