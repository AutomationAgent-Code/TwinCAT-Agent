# 客户自定义 HAM 案例

客户可以在本目录下按功能新建文件夹，并放入 `manifest.yaml` 与 PLCopen XML。
建议 ID 使用客户命名空间，例如：

```yaml
id: ham.customer-name.custom-device
publisher: Customer Name
category: device
type: plcopen
entry: CustomDevice.xml
maturity: reference
```

