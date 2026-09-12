# 更新倍福文档索引:重建 ba-docsearch 的 FTS5 库,再同步到产品位置。
# 用法(普通 PowerShell):  & "G:\claude\twin-cat-agent\scripts\update_docs.ps1"
# 可选参数: -Rebuild(全量重建), -Prune(顺带删掉已删文件的行)
param([switch]$Rebuild, [switch]$Prune, [switch]$SkipOfficialSupplement)

$ErrorActionPreference = 'Stop'

# --- 路径(如与你机器不同，改这里) ---
$Repo    = Split-Path $PSScriptRoot -Parent
$BaDir   = "G:\claude\Beckhoff Agent\ba-docsearch"
$DocsRoot = "G:\claude\Beckhoff Agent\Knowledge base"
$SrcDb   = Join-Path $BaDir "index.db"
$DstDb   = Join-Path $Repo "data\ba-docs\index.db"
$Py312   = "C:\Users\Aurora Home Office\AppData\Local\Programs\Python\Python312\python.exe"
$OfficialDocs = "C:\ProgramData\Beckhoff\TwinCAT\Functions\CoAgentAddons\CoAgentSearch\InfosysDocs"
$SupplementDir = Join-Path $DocsRoot "content\TwinCAT-Agent-Supplement"
$Mc3Pdf = Join-Path $Repo "knowledge_base\source\TF55xx_TC3_MC3_EN.pdf"
$Mc3Dir = Join-Path $SupplementDir "TF55xx_TC3_MC3_EN"
$PdfImporter = Join-Path $PSScriptRoot "import_pdf_manual.py"
$KbValidator = Join-Path $PSScriptRoot "validate_knowledge_base.py"

# These manuals were found in the official CoAgent bundle but were absent or
# incomplete in our InfoSys mirror. Keep the list explicit: it avoids copying
# the whole official bundle and makes future audits/reviews deterministic.
$SupplementFiles = @(
    "TwinCAT\Base\PLC_Lib_PJLink_EN.md",
    "TwinCAT\Base\TwinCAT_3_PLC_Lib_PJLink_EN.md",
    "TwinCAT\Base\TE1200_TC3_PLC_Static_Analysis_EN.md",
    "TwinCAT\Control\TF4500_TC3_Speech_EN.md",
    "TwinCAT\Measurement\TF3550_TC3_Analytics_Runtime_EN.md",
    "TwinCAT\Motion\TF5261_realtime_cycles_en.md",
    "TwinCAT\Motion\TF5261_realtime_loops_en.md",
    "TwinCAT\Motion\TF5262_tc3_cnc_online_adaption_en.md",
    "TwinCAT\Motion\TF5263_tc3_cnc_extended_interpolation_en.md",
    "TwinCAT\Motion\TF527x_kernelv_api_en.md",
    "TwinCAT\Motion\TF5291_tc3_cnc_am_plus_en.md",
    "TwinCAT\Motion\TF5292_tc3_cnc_edm_plus_en.md",
    "TwinCAT\Motion\TF5293_tc3_cnc_cycles_millingbase_en.md"
)

if (-not (Test-Path $Py312)) { throw "找不到 Python312: $Py312 (ba-docsearch 必须用 3.12)" }
if (-not (Test-Path $BaDir)) { throw "找不到 ba-docsearch: $BaDir" }
if (-not (Test-Path $KbValidator)) { throw "找不到知识库校验器: $KbValidator" }

# --- preflight) 先确认精选层来源登记完整，再重建大索引 ---
& $Py312 $KbValidator
if ($LASTEXITCODE -ne 0) { throw "精选知识库校验失败(exit $LASTEXITCODE)" }

# --- 0) 将随仓库维护的文本型 PDF 转为逐页 HTML，纳入同一 FTS5 索引 ---
if (Test-Path -LiteralPath $Mc3Pdf) {
    if (-not (Test-Path -LiteralPath $PdfImporter)) {
        throw "找不到 PDF 导入器: $PdfImporter"
    }
    & $Py312 $PdfImporter $Mc3Pdf $Mc3Dir `
        --title "TF55xx TwinCAT 3 MC3" `
        --product "TF55xx TwinCAT 3 MC3" `
        --category "TwinCAT Motion" `
        --product-group "Motion"
    if ($LASTEXITCODE -ne 0) { throw "TF55xx MC3 PDF 转换失败(exit $LASTEXITCODE)" }
} else {
    Write-Warning "未找到 TF55xx MC3 PDF，跳过: $Mc3Pdf"
}

# --- 0b) 补齐官方 CoAgent 中独有的高价值文档 ---
# ba-docsearch only indexes HTML. Convert selected Markdown manuals into a
# minimal InfoSys-compatible HTML envelope; the complete Markdown text remains
# searchable through the normal FTS5 index and no second search path is needed.
if (-not $SkipOfficialSupplement) {
    if (Test-Path $OfficialDocs) {
        New-Item -ItemType Directory -Force $SupplementDir | Out-Null
        $copied = 0
        foreach ($relative in $SupplementFiles) {
            $source = Join-Path $OfficialDocs $relative
            if (-not (Test-Path -LiteralPath $source)) {
                Write-Warning "官方补充文档不存在，跳过: $relative"
                continue
            }
            $title = [IO.Path]::GetFileNameWithoutExtension($source)
            $body = [Net.WebUtility]::HtmlEncode((Get-Content -LiteralPath $source -Raw -Encoding UTF8))
            $html = @"
<!doctype html><html><head><meta charset="utf-8"><title>$title</title>
<meta name="product" content="TwinCAT"><meta name="category" content="Official CoAgent supplement">
<meta name="keywords" content="$title Beckhoff InfoSys"></head>
<body><article><pre>$body</pre></article></body></html>
"@
            $target = Join-Path $SupplementDir ($title + ".html")
            [IO.File]::WriteAllText($target, $html, [Text.UTF8Encoding]::new($false))
            $copied++
        }
        Write-Host "已同步官方缺口文档: $copied 项 -> $SupplementDir" -ForegroundColor Cyan
    } else {
        Write-Warning "未安装官方 CoAgent 文档，跳过缺口同步: $OfficialDocs"
    }
}

# --- 1) 重建索引 ---
$env:BA_DOCS_ROOT = $DocsRoot
$env:BA_DOCS_DB   = $SrcDb
$args = @('-m','ba_docsearch.cli','index')
if ($Rebuild) { $args += '--rebuild' }
if ($Prune)   { $args += '--prune' }
Write-Host "重建索引($(if($Rebuild){'全量'}else{'增量'}))..." -ForegroundColor Cyan
Push-Location $BaDir
& $Py312 @args
Pop-Location
if ($LASTEXITCODE -ne 0) { throw "索引重建失败(exit $LASTEXITCODE)" }

# --- 2) 同步到产品位置 ---
New-Item -ItemType Directory -Force (Split-Path $DstDb) | Out-Null
Copy-Item $SrcDb $DstDb -Force
$size = "{0:N0} MB" -f ((Get-Item $DstDb).Length / 1MB)
Write-Host "✓ 已同步到产品位置: $DstDb ($size)" -ForegroundColor Green
& $Py312 $KbValidator --check-index --db $DstDb
if ($LASTEXITCODE -ne 0) { throw "同步后的 FTS5 索引校验失败(exit $LASTEXITCODE)" }
Write-Host "  重启后端后 docs_search 即用上新内容。"
