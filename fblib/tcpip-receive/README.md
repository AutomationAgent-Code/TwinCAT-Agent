# FB_TcpIpReceive

对已有 `T_HSOCKET` 做一次轮询接收。`bDone=TRUE` 且 `nReceived=0` 是“当前没有数据”，
不是错误。TCP 可能分片，调用方必须累计字节并按自己的协议组帧，同时设置应用层总超时。

```iecst
fbReceive(hSocket := fbClient.hSocket, pBuffer := ADR(aRx),
          nBufferSize := SIZEOF(aRx), bExecute := bPoll);
```
