---
name: library-management-via-com
description: "COM-based library management — add/remove library references, placeholders, repositories, install/uninstall libraries"
metadata: 
  type: project
---

## Library Management via COM

Git feature `feat/COM-first-plc-read-write + library management` (commit `a0d55e5`) added full library lifecycle management through the TwinCAT COM Automation Interface.

### Commands (all under `plc` group)

| CLI Command | COM Method | Description |
|-------------|-----------|-------------|
| `plc libraries` | `list_libraries()` | List library references in current project |
| `plc lib-scan` | `scan_installed_libraries()` | Scan ALL installed libraries on the system |
| `plc lib-add <name> -v <ver> -d <dist>` | `com_add_library()` | Add library reference to project |
| `plc lib-remove <name> -v <ver>` | `com_remove_library()` | Remove library reference from project |
| `plc placeholder-add <name> --lib --ver --dist` | `com_add_placeholder()` | Add placeholder reference |
| `plc placeholder-freeze <name>` | `com_freeze_placeholder()` | Freeze placeholder to current version |
| `plc repo-add <name> <path> --index` | `com_insert_repository()` | Add library repository |
| `plc repo-remove <name>` | `com_remove_repository()` | Remove library repository |
| `plc lib-install <repo> <path> --overwrite` | `com_install_library()` | Install library from repository |
| `plc lib-uninstall <repo> <name> -v <ver>` | `com_uninstall_library()` | Uninstall library from repository |

### Implementation Details

Located in `tc_template/plc.py`:
- `list_libraries()` (line 771) — reads library references from open PLC project
- `scan_installed_libraries()` (line 788) — scans system-wide installed libraries
- `com_add_library()` (line 813) — adds reference with name/version/distributor
- `com_remove_library()` (line 835) — removes reference by matching criteria
- `com_add_placeholder()` (line 850) — adds placeholder with optional default resolution
- `com_freeze_placeholder()` (line 869) — pins placeholder to currently resolved version
- `com_insert_repository()` (line 878) — inserts repository at specified index
- `com_remove_repository()` (line 893) — removes repository by name
- `com_install_library()` (line 902) — installs .compiled-library from repo
- `com_uninstall_library()` (line 918) — uninstalls library from repo

### Key Design Decisions

1. **All library operations go through COM** — no filesystem manipulation of `_Libraries/` or `.tsproj` `<LibraryReferences>`
2. **Placeholders are TwinCAT's dependency resolution mechanism** — they declare "I need this library" without pinning a specific version; `placeholder-freeze` locks them
3. **Repositories are TwinCAT's library source locations** — adding a repo makes its libraries available for installation
4. **The `_Libraries` folder in templates is preserved as-is** during template extraction and scaffolding

### CLI Usage Patterns

```bash
# List current project libraries
plc libraries

# Find available libraries
plc lib-scan

# Add Tc2_MC2 to project
plc lib-add "Tc2_MC2" -d "Beckhoff Automation GmbH" -v "*"

# Add a placeholder (resolves at build time)
plc placeholder-add "Tc2_Standard" --lib "Tc2_Standard" -d "Beckhoff Automation GmbH"

# Lock placeholder to current version
plc placeholder-freeze "Tc2_Standard"

# Manage repositories
plc repo-add "MyRepo" "C:\TwinCAT\MyLibraries"
plc lib-install "MyRepo" "MyCustomLib\1.0.0.0\MyCustomLib.compiled-library"
```

**How to apply:** Use `plc` subcommands for all library operations. Never manually edit `.tsproj` `<LibraryReferences>` or `_Libraries/` folder contents while a project is open in XAE.

[[plc-project-creation-via-com]]
