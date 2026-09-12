# FB_TcpIpServer

监听一个本地 TCP 端口并接受一个客户端，依赖 `Tc2_TcpIp`（TF6310）。连接成功后把
`hSocket` 交给 `tcpip-send` / `tcpip-receive`。多客户端服务端应共享一个 `T_HSERVER`，
并为每个并发连接使用独立的 `FB_ServerClientConnection`；本模板定位为单客户端常用场景。

```bash
fblib add tcpip-server -P LOCAL_PORT=2000
```
