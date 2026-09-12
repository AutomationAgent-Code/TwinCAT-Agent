# FB_TcpIpSend

对已有 `T_HSOCKET` 执行一次字节缓冲区发送，依赖 `Tc2_TcpIp`（TF6310）。调用方在
`bBusy=TRUE` 期间不得移动或改写 `pData` 指向的内存。

```iecst
fbSend(hSocket := fbClient.hSocket, pData := ADR(aTx),
       nDataLen := nTxLen, bExecute := bSend);
```

TCP 不保留应用报文边界；协议层应自行使用长度字段、分隔符或固定帧长。
