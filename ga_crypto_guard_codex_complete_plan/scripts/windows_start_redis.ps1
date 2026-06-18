$RedisDir = "D:\Program Files\Redis"
$RedisServer = Join-Path $RedisDir "redis-server.exe"

if (!(Test-Path $RedisServer)) {
  Write-Host "redis-server.exe not found: $RedisServer"
  exit 1
}

Write-Host "Starting Redis from $RedisServer"
Start-Process -FilePath $RedisServer -WorkingDirectory $RedisDir
Start-Sleep -Seconds 2
Write-Host "Redis start attempted. Use redis-cli ping to verify."
