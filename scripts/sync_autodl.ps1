param(
    [switch]$SyncData
)

$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$sshConfig = Join-Path $projectRoot ".ssh\config"
$hostAlias = "autodl-kaogong"
$remoteRoot = "/root/autodl-tmp/kaogong"

if (-not (Test-Path -LiteralPath $sshConfig)) {
    throw "SSH config not found: $sshConfig"
}

Push-Location $projectRoot
try {
    scp -F $sshConfig -C -r `
        backend web scripts pyproject.toml README.md .gitignore `
        "${hostAlias}:$remoteRoot/"

    if ($SyncData) {
        scp -F $sshConfig -C `
            "data\processed\question_bank.db" `
            "${hostAlias}:$remoteRoot/data/processed/"
        scp -F $sshConfig -C `
            "data\external\source-manifest.json" `
            "${hostAlias}:$remoteRoot/data/external/"
    }
} finally {
    Pop-Location
}

$remoteScript = @'
cd /root/autodl-tmp/kaogong
if [ -f server.pid ]; then
  kill $(cat server.pid) 2>/dev/null || true
fi
if [ -f .secrets/setup_token ]; then
  export KAOGONG_SETUP_TOKEN="$(cat .secrets/setup_token)"
fi
nohup python -m uvicorn backend.kaogong_api.main:app \
  --host 0.0.0.0 --port 6006 > server.log 2>&1 < /dev/null &
echo $! > server.pid
sleep 2
curl -fsS http://127.0.0.1:6006/api/health
'@

ssh -F $sshConfig -o BatchMode=yes $hostAlias $remoteScript
