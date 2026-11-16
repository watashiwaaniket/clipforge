# One-command local boot (Windows): compose stack, then Next.js dev server.
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
  Write-Error "Docker is required."
}
if (-not (Get-Command npm -ErrorAction SilentlyContinue)) {
  Write-Error "Node.js 18+ (npm) is required for the frontend."
}

if (-not (Test-Path .env)) {
  Copy-Item .env.example .env
  Write-Host "Created .env from .env.example"
}

$lines = Get-Content .env
function Set-BlankKey([string]$Key, [string]$Value) {
  $script:found = $false
  for ($i = 0; $i -lt $script:lines.Count; $i++) {
    if ($script:lines[$i] -like "$Key=*") {
      $script:found = $true
      $current = $script:lines[$i].Substring($Key.Length + 1).Trim()
      if ($current -eq "" -or $current.StartsWith("#")) {
        $script:lines[$i] = "$Key=$Value"
      }
      break
    }
  }
  if (-not $script:found) {
    $script:lines += "$Key=$Value"
  }
}

$fernetBytes = New-Object byte[] 32
$jwtBytes = New-Object byte[] 32
[System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($fernetBytes)
[System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($jwtBytes)
$fernet = [Convert]::ToBase64String($fernetBytes).Replace("+", "-").Replace("/", "_")
$jwt = -join ($jwtBytes | ForEach-Object { $_.ToString("x2") })
Set-BlankKey "ENCRYPTION_KEY" $fernet
Set-BlankKey "JWT_SECRET" $jwt
$utf8 = New-Object System.Text.UTF8Encoding $false
[System.IO.File]::WriteAllLines((Join-Path (Get-Location) ".env"), $lines, $utf8)
Write-Host "Ensured ENCRYPTION_KEY and JWT_SECRET are set in .env (existing values kept)."

docker compose up --build -d

Set-Location frontend
if (-not (Test-Path node_modules)) {
  npm install
}
npm run dev
