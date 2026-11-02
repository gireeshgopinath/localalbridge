param([string]$PythonPath,[string]$StatePath,[switch]$Register,[switch]$Doctor,[switch]$Test)
$ErrorActionPreference='Stop'
$project=(Resolve-Path -LiteralPath $PSScriptRoot).Path
if (-not $PythonPath) {
    $PythonPath=Join-Path $project '.venv/Scripts/python.exe'
    if (-not (Test-Path -LiteralPath $PythonPath)) { & python -m venv (Join-Path $project '.venv'); if ($LASTEXITCODE) {throw 'Python 3.12+ is required'} }
    & $PythonPath -m pip install -r (Join-Path $project 'requirements.txt')
    if ($LASTEXITCODE) {throw 'Dependency installation failed'}
}
$PythonPath=(Resolve-Path -LiteralPath $PythonPath).Path
& $PythonPath -c 'import sys,mcp; assert sys.version_info >= (3,12), "Python 3.12+ required"'
if ($LASTEXITCODE) {throw 'Interpreter or MCP dependency check failed'}
if (-not $StatePath) {$StatePath=Join-Path $project '.state'}
$StatePath=[IO.Path]::GetFullPath($StatePath)
$worker=$env:LOCAL_WORKER_OPENCODE
if (-not $worker) {
    $command=Get-Command opencode.exe -ErrorAction SilentlyContinue
    if ($command) {$worker=$command.Source}
    else {$worker=Join-Path $env:APPDATA 'npm/node_modules/@opencode/cli/bin/opencode.exe'}
}
if (-not (Test-Path -LiteralPath $worker)) {throw 'OpenCode executable missing. Set LOCAL_WORKER_OPENCODE to its absolute .exe path.'}
$worker=(Resolve-Path -LiteralPath $worker).Path
$env:LOCAL_WORKER_OPENCODE=$worker
$env:LOCAL_WORKER_STATE=$StatePath
if ($Doctor) { & $PythonPath (Join-Path $project 'server.py') --doctor; if ($LASTEXITCODE) {throw 'Local model health check failed'} }
if ($Test) { & $PythonPath -m pytest (Join-Path $project 'tests') -q; if ($LASTEXITCODE) {throw 'Tests failed'} }
if ($Register) {
    & codex mcp add local-workers --env "LOCAL_WORKER_STATE=$StatePath" --env "LOCAL_WORKER_OPENCODE=$worker" -- $PythonPath (Join-Path $project 'server.py')
    if ($LASTEXITCODE) {throw 'MCP registration failed'}
    Write-Host 'Registered local-workers. Restart Codex or open a new chat to load its tools.'
}
Write-Host "Python: $PythonPath"
Write-Host "State: $StatePath"
