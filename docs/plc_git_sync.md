# PLC Git 同步

TwinCAT Agent 提供 `plc_git_sync` 工具，用于在 XAE 保持打开的情况下，把 GitHub
仓库中的 TwinCAT PLC 源文件同步到当前 PLC 工程。

## 支持的仓库内容

仓库中直接保存 TwinCAT 原生源文件：

```text
PLC-Code/
├─ POUs/
│  ├─ MAIN.TcPOU
│  └─ FB_Motor.TcPOU
├─ GVLs/
│  └─ GVL_Main.TcGVL
└─ DUTs/
   └─ ST_State.TcDUT
```

Agent 会读取 `PROGRAM`、`FUNCTION_BLOCK`、`FUNCTION`、GVL 和 DUT 声明，
并同步 POU 的 ST 实现、方法、动作、属性及属性访问器。默认不会删除当前工程
中 Git 仓库没有的对象。

## 使用方式

先在 Agent 对话中请求预览：

```text
预览 GitHub 仓库 https://github.com/example/plc-code.git 的 PLC 同步计划，
本地目录使用 D:\PLC\plc-code，只同步 PLC-Code 目录。
```

确认计划后执行：

```text
执行刚才的 PLC Git 同步，允许创建当前工程中不存在的对象。
```

对应工具参数中 `apply=false` 是预览，`apply=true` 才会执行 `git pull --ff-only`
并通过 COM 写入当前 XAE。远程仓库首次使用时需要提供 `local_path`；已有本地
Git 仓库可以直接把仓库目录作为 `repository`。

## 运行边界

- 不关闭 XAE，不重新打开解决方案，不直接覆盖 XAE 工程 XML。
- Git 工作目录必须没有未提交修改，远程仓库的 `origin` 必须与请求一致。
- 对象名重名时必须通过 `object_paths` 提供当前 XAE 的精确 `TIPC^...` 路径。
- 当前 XAE 可见 PLC 编辑器有未保存修改时，同步会停止，不自动保存或丢弃。
- 同步只更新 XAE 内存中的 PLC 对象；不会自动保存、编译、登录、下载或启动 PLC。
- `create_missing=false` 为默认值；删除、重命名和 I/O/NC 配置不会由 Git 同步隐式执行。

