# FB_TcpIpClient（OOP）

连接、发送和接收封装在同一个 Client 对象中。落地到 XAE 后，`Send`、`Receive`、
`Cyclic` 会显示为 `FB_TcpIpClient` 的子 Method，不再要求调用方持有 socket 或额外的
`FB_SocketSend/FB_SocketReceive` 实例。

```iecst
fbClient.Cyclic(bEnable := TRUE, sRemoteHost := '192.168.1.100', nRemotePort := 2000);
fbClient.Send(bExecute := bSend, pData := ADR(aTx), nDataLen := nTxLen);
fbClient.Receive(bExecute := bPoll, pBuffer := ADR(aRx), nBufferSize := SIZEOF(aRx));
```

每个 Method 都应在 PLC 周期中持续调用。发送结果读取 `bSendDone/bSendError`；接收结果
读取 `bReceiveDone/nReceived/bReceiveError`。TCP 可能分片，应用协议仍需累计和组帧。
