param(
    [ValidateRange(1024, 65535)][int]$Port = 8765,
    [ValidateNotNullOrEmpty()][string]$Workspace = 'local',
    [switch]$Build,
    [switch]$RequireModelConnection,
    [switch]$Check
)

$ErrorActionPreference = 'Stop'
$workspaceRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$workspacePython = Join-Path $workspaceRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $workspacePython)) {
    $workspacePython = Join-Path $workspaceRoot '.venv/bin/python'
}
if (-not (Test-Path -LiteralPath $workspacePython)) {
    throw '缺少项目 .venv，请先按 README 配置 Python 环境。'
}
$workspaceData = Join-Path $workspaceRoot 'var'

if ($RequireModelConnection) {
    # Credential-free connectivity only; no model or HTTP API request is sent.
    & $workspacePython (Join-Path $PSScriptRoot 'check_connection.py')
    if ($LASTEXITCODE -ne 0) {
        throw '模型主机的 TCP/TLS 预检失败。请检查联网权限和连接，不要重复提交模型任务。'
    }
}

if (-not $Check -and ($Build -or -not (Test-Path -LiteralPath (Join-Path $workspaceRoot 'apps/web/dist/index.html')))) {
    & npm --prefix (Join-Path $workspaceRoot 'apps/web') run build
    if ($LASTEXITCODE -ne 0) { throw '前端构建失败，已停止启动。' }
}

$workspaceArguments = @(
    (Join-Path $PSScriptRoot 'serve.py'),
    '--port', $Port,
    '--workspace', $Workspace,
    '--data-dir', $workspaceData
)
if ($Check) { $workspaceArguments += '--check' }
& $workspacePython @workspaceArguments
exit $LASTEXITCODE
