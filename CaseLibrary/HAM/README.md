# HAM Customer Case Library

该目录是从当前客户 XAE 工程学习并提取的 HAM 案例库。程序递归查找
`manifest.yaml`，因此客户可以在任意功能分类下复制自己的案例文件夹。

每个案例至少包含：

```text
case-name/
├── manifest.yaml
├── Case.xml
└── README.md（可选）
```

`maturity: reference` 表示保留了原项目实现，但可能依赖其他客户对象或硬件映射；
只有 `maturity: reusable` 才适合直接一键导入普通项目。

