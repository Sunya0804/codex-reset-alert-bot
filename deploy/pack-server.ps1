# Creates a source-only archive. It never includes .env, auth tokens or the state database.
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$outputFolder = Join-Path $projectRoot 'data\deploy'
New-Item -ItemType Directory -Path $outputFolder -Force | Out-Null
$archivePath = Join-Path $outputFolder 'bot-source.tar.gz'
$sourceFiles = @(
    'bot.py', 'web_source.py', 'ai_interpreter.py', 'time_rules.py', 'ai_schema.json', 'ai_instructions.txt',
    'requirements.txt', '.env.example', 'README.md',
    'test_bot.py', 'test_web_source.py', 'test_ai_interpreter.py', 'test_reliability.py',
    'deploy/install-linux.sh', 'deploy/install-service.sh',
    'deploy/codex-reset-alert.service', 'deploy/ORACLE.md'
)
& tar.exe -czf $archivePath -C $projectRoot @sourceFiles
if ($LASTEXITCODE -ne 0) { throw 'Source archive creation failed.' }
Write-Output $archivePath
