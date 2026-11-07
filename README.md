# TwinCAT Agent

AI-assisted TwinCAT 3 automation for PLC projects, TwinCAT HMI and XAE workflows.

## Latest stable release

Version **v1.0.8.160** is available for Windows:

[Download TwinCAT Agent v1.0.8.160](https://github.com/AutomationAgent-Code/TwinCAT-Agent/releases/download/v1.0.8.160/TwinCAT-Agent-Setup-v1.0.8.160.exe)

SHA256:

`90d9a79a328097459b0ff9aafdaf8f5d3df818d764ae1b84c38a1c7e82f42a3b`

The [latest release page](https://github.com/AutomationAgent-Code/TwinCAT-Agent/releases/latest) contains the installer, checksum, release notes and the stable update manifest:

`https://github.com/AutomationAgent-Code/TwinCAT-Agent/releases/latest/download/latest.json`

The complete installer includes the local Agent backend, 32-bit and 64-bit runtime dependencies, TwinCAT HMI/TE2000 tools, offline documentation and optional XAE embedding. Close the selected XAE/Visual Studio instances before installing or upgrading their embedded extension.

## Installation and tray improvements in v1.0.8.160

- A red-and-white step-by-step wizard guides you through the agreement, installation location, embedding targets, confirmation and result.
- A separate embedding page provides independent checkboxes for multiple targets: TcXaeShell 15.x (32-bit), TcXaeShell 17.x (64-bit), and TwinCAT-integrated Visual Studio 2019 (32-bit) / Visual Studio 2022 (64-bit). A compatible TwinCAT integration must already be installed in each selected Visual Studio instance.
- The tray's **Open XAE** submenu lists usable recorded embeddings, launches the selected host and refreshes its startup environment.
- Upgrades preserve existing configuration, conversation history, attachments, user templates and project memory, with a backup of the previous installation.

[Gitee release and split-package downloads](https://gitee.com/Ethan-Tian/TwinCAT-Agent-Release/releases/tag/v1.0.8.160) are also available. Download all three parts and `Merge-Installer.ps1` into one directory, then run:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\Merge-Installer.ps1
```

The script verifies each part and the complete SHA256 above, then reconstructs the same full installer. It does not install the product or overwrite a different existing EXE. No separate offline-documentation package is needed.

This repository distributes release assets only. Source code, project files and user configuration are not included in the public release repository.
