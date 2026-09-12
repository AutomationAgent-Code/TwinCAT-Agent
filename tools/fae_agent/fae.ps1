param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$FaeArguments
)

$env:PYTHONPATH = $PSScriptRoot
$env:PYTHONUTF8 = "1"
python -m fae.ask @FaeArguments
exit $LASTEXITCODE
