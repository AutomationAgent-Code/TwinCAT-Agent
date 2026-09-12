# FB_AdsReadSymbol

通过 AMS NetId、ADS Port 和完整符号名读取任意 PLC 变量，依赖 `Tc2_DataExchange`。

```iecst
fbRead(sNetId := '5.1.204.160.1.1', nPort := 851,
       sSymbolName := 'MAIN.nCounter', pDestination := ADR(nValue),
       nDataLen := SIZEOF(nValue), bExecute := bRead);
```

目标变量类型/大小必须与本地缓冲区一致，且两台设备之间须已有 ADS Route。
