# FB_AdsWriteSymbol

通过 AMS NetId、ADS Port 和完整符号名写入任意 PLC 变量，依赖 `Tc2_DataExchange`。

```iecst
fbWrite(sNetId := '5.1.204.160.1.1', nPort := 851,
        sSymbolName := 'MAIN.nSetpoint', pSource := ADR(nSetpoint),
        nDataLen := SIZEOF(nSetpoint), bExecute := bWrite);
```

目标变量类型/大小必须与本地源变量一致，且两台设备之间须已有 ADS Route。
