# 模板契约与离线能力缺口

fblib_add 按 slug/params 验证本地模板，不再要求未声明的 name/pou。
导入前确认单 PLC 项目；多 PLC 选择尚未接入，不猜测首个项目。原有底层代码检查保持启用。

semantic_evidence 区分 invalid（发现代码错误）和 incomplete（证据或解析能力不足），两者均不自动放行。
同一轮、同一 PLC 项目内，相同 incomplete 原因跨 preflight/write/patch 重复两次后，标记 capability_exhausted 并结束自动循环。
只读排查仍可进行；新一轮重新核查。库引用变更或成功预检会清除对应计数。不是持久化锁，也不是诊断读取服务恢复失败。

已补：ADR(可寻址变量) 返回带目标类型的指针；校验基础指针赋值及数组元素边界，PVOID/__XWORD/LWORD 接收地址，DWORD 要求额外的 32 位目标证据。
解引用仍因地址/生命周期证据不足而阻断；不把静态类型匹配当作内存安全、通信缓冲长度或在线变更安全证明。
官方依据：https://infosys.beckhoff.com/content/1033/tc3_plc_intro/2529015179.html

已补：通过精确 PLC References 节点读取 EffectiveVersion 和 RelativePath，跟随该版本的 browsercache 识别已引用符号。预审附加 missing_library_signatures，明确区分库未找到和库签名未取得；不会选择安装目录中的最新版本。

尚未完成：从实际解析版本的库加载完整类型/函数/FB 签名，以及指针解引用和缓冲区长度的完整分析。
本机 Tc2_TcpIp 的 browsercache 只提供名称、对象 GUID 和注释，不能当作完整签名证据，也不能据此放行库调用。
