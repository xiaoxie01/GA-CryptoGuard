Write-Host "Validating GA CryptoGuard Windows environment"

$RedisDir = "D:\Program Files\Redis"
$DuckDBDir = "D:\Program Files\duckdb"
$DuckDBFile = "D:\Program Files\duckdb\crypto_guard_analytics.duckdb"

Write-Host "Redis dir: $RedisDir"
if (Test-Path $RedisDir) { Write-Host "Redis dir OK" } else { Write-Host "Redis dir missing" }

Write-Host "DuckDB dir: $DuckDBDir"
if (!(Test-Path $DuckDBDir)) {
  New-Item -ItemType Directory -Force -Path $DuckDBDir | Out-Null
  Write-Host "DuckDB dir created"
} else { Write-Host "DuckDB dir OK" }

Write-Host "DuckDB db target: $DuckDBFile"
Write-Host "Parquet base dir will be created by application: data/parquet/klines/binance_um"
