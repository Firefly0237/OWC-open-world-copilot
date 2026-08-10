# 部署：N1.2 team canonical 栈

`infra/team/docker-compose.yml` 启动 PostgreSQL 17.10、一次性 Alembic migration job 和
`OWCOPILOT_PROFILE=team` 的 FastAPI 服务。migration 与 runtime 使用两个独立的非特权角色；
seed 先创建 tenant、active owner membership 和 container，bootstrap 再发布一条 genesis revision。

## 当前可用面

- `GET /health`：检查 PostgreSQL 可达、Alembic head 为 `n12_0002`，并验证 runtime role 没有
  superuser、RLS bypass、建库／建角色、复制、inherit 或角色成员关系。
- `GET /team/containers/{container_uuid}/canonical?head=authoring|serving`：Bearer 身份经数据库
  active membership 授权后，从 PostgreSQL 不可变 revision 读取完整 canonical state。
- 其他尚未迁移的 team 路由统一返回 `503 NOT_AVAILABLE`，不会回落到文件 ContentStore、SQLite
  控制面或进程内状态。
- `local` profile 仍保留原有单机文件／SQLite 兼容路径；两种 profile 不双写。

N1.2 只建立 schema、Alembic、RLS、genesis bootstrap 和一条 team canonical 查询路径。它没有
实现 pgvector、outbox、双实例故障恢复、PostgreSQL backup/restore、team review/job/intake 或旧文件
历史逐 revision 查询。

## 必填配置与容器内 DSN

正式 Compose 链要求下列值全部显式提供：

| 变量 | 说明 |
|---|---|
| `OWCOPILOT_POSTGRES_ADMIN_PASSWORD` | PostgreSQL 初始化管理员密码 |
| `OWCOPILOT_MIGRATION_PASSWORD` | `owcopilot_migration` 的原始密码 |
| `OWCOPILOT_APP_PASSWORD` | `owcopilot_app` 的原始密码 |
| `OWCOPILOT_MIGRATION_DATABASE_URL` | migration/seed 容器使用的 `postgresql+psycopg` DSN |
| `OWCOPILOT_TEAM_DATABASE_URL` | bootstrap/API 容器使用的 `postgresql+psycopg` DSN |
| `OWCOPILOT_JWT_SECRET` | team Bearer token 的 HS256 签名密钥 |

两个 DSN 必须使用 Compose 网络内主机名 `postgres`，不能使用宿主机的 `127.0.0.1`。密码放入
DSN 前必须 URL-encode；下面是可直接执行的 PowerShell 示例（示例值必须替换）：

```powershell
$env:OWCOPILOT_POSTGRES_DB = "owcopilot"
$env:OWCOPILOT_POSTGRES_ADMIN_USER = "owcopilot_admin"
$env:OWCOPILOT_POSTGRES_ADMIN_PASSWORD = "replace-admin-password"
$env:OWCOPILOT_MIGRATION_PASSWORD = "replace-migration-password"
$env:OWCOPILOT_APP_PASSWORD = "replace-app-password"
$env:OWCOPILOT_JWT_SECRET = "replace-with-a-long-random-secret"

$migrationPasswordEncoded = [Uri]::EscapeDataString($env:OWCOPILOT_MIGRATION_PASSWORD)
$appPasswordEncoded = [Uri]::EscapeDataString($env:OWCOPILOT_APP_PASSWORD)
$databaseName = $env:OWCOPILOT_POSTGRES_DB
$env:OWCOPILOT_MIGRATION_DATABASE_URL = "postgresql+psycopg://owcopilot_migration:${migrationPasswordEncoded}@postgres:5432/${databaseName}"
$env:OWCOPILOT_TEAM_DATABASE_URL = "postgresql+psycopg://owcopilot_app:${appPasswordEncoded}@postgres:5432/${databaseName}"
```

不要把 DSN、原始密码或 JWT secret 写入命令日志、bootstrap JSON 或版本库。

## 可复跑部署链

以下命令均从仓库根目录执行。先构建镜像、启动 PostgreSQL 并迁移到精确 head：

```powershell
docker compose -f infra/team/docker-compose.yml build migrate api
docker compose -f infra/team/docker-compose.yml up -d postgres
docker compose -f infra/team/docker-compose.yml run --rm -T migrate
```

### 1. seed tenant、owner 与 container

三个 UUID 必须是显式、canonical、non-nil UUID。slug/display 也必须显式且非空：

```powershell
$env:TEAM_TENANT_ID = "11111111-1111-4111-8111-111111111111"
$env:TEAM_CONTAINER_ID = "22222222-2222-4222-8222-222222222222"
$env:TEAM_OWNER_SUBJECT = "33333333-3333-4333-8333-333333333333"

docker compose -f infra/team/docker-compose.yml run --rm -T migrate `
  python scripts/seed_team_scope.py `
  --tenant-id $env:TEAM_TENANT_ID `
  --tenant-slug studio-one `
  --tenant-display-name "Studio One" `
  --container-id $env:TEAM_CONTAINER_ID `
  --container-slug world-one `
  --container-display-name "World One" `
  --owner-subject $env:TEAM_OWNER_SUBJECT
```

首次成功返回三项 `created`；使用完全相同的参数重跑返回三项 `replayed`。同 ID 的 slug/display、
owner role/active 状态或唯一键只要不完全一致，脚本就 fail closed，并回滚整个事务。脚本还会拒绝
错误的 Alembic head、非 `owcopilot_migration` 连接及被提权／加入角色关系的 migration role。

### 2. inventory、归档并 bootstrap genesis

只允许挂载本次 cutover 明确提供的 synthetic fixture 或用户提供路径。禁止把
`content/.owcopilot/`、`demo/honglou/.owcopilot/` 用作输入、fixture 或扫描目标。输入必须静止；
archive 必须写到 content root 之外、由容器 UID 10001 可写且由部署方管控的目录。

先在宿主机准备三个位于版本库之外的绝对路径，并通过环境变量传入；不要照抄某台开发机的盘符。
SQLite 文件可以有多个，但每一个都要单独列入 inventory：

```powershell
$env:LEGACY_CONTENT_PATH = "<absolute-path-to-content-snapshot>"
$env:LEGACY_SQLITE_PATH = "<absolute-path-to-legacy-control.sqlite3>"
$env:CUTOVER_OUTPUT_PATH = "<absolute-path-to-managed-cutover-output>"
$legacyContentHost = (Resolve-Path -LiteralPath $env:LEGACY_CONTENT_PATH).Path
$legacySqliteHost = (Resolve-Path -LiteralPath $env:LEGACY_SQLITE_PATH).Path
$cutoverOutputHost = (Resolve-Path -LiteralPath $env:CUTOVER_OUTPUT_PATH).Path
$env:GENESIS_CREATED_AT = "2026-08-04T12:00:00Z"
```

有 SQLite 的正式命令如下；`--sqlite` 可重复，label 必须唯一：

```powershell
docker compose -f infra/team/docker-compose.yml run --rm -T --no-deps `
  --env "OWCOPILOT_TEAM_DATABASE_URL=$($env:OWCOPILOT_TEAM_DATABASE_URL)" `
  --volume "${legacyContentHost}:/cutover/content:ro" `
  --volume "${legacySqliteHost}:/cutover/sqlite/legacy-control.sqlite3:ro" `
  --volume "${cutoverOutputHost}:/cutover/output" `
  migrate python scripts/bootstrap_team_genesis.py `
  --content-root /cutover/content `
  --sqlite legacy-control=/cutover/sqlite/legacy-control.sqlite3 `
  --archive /cutover/output/team-genesis.zip `
  --scratch-parent /tmp `
  --tenant-id $env:TEAM_TENANT_ID `
  --container-id $env:TEAM_CONTAINER_ID `
  --actor-subject $env:TEAM_OWNER_SUBJECT `
  --created-at $env:GENESIS_CREATED_AT |
  Tee-Object -FilePath (Join-Path $cutoverOutputHost "bootstrap-result.json")
```

若已完整盘点并确认本次 cutover 没有任何 legacy SQLite，必须把上面的 SQLite mount 和
`--sqlite ...` 同时删除，并显式传 `--no-legacy-sqlite`。这是一项操作者 inventory attestation，
工具不会自动发现任意遗漏的数据库；应将最终命令、全部 SQLite label/path、源文件 checksum、
CLI JSON、archive 和 archive SHA-256 一并封存。

`--scratch-parent /tmp` 必须指向容器原生 Linux 文件系统，不能指向 Docker Desktop 的 Windows
bind mount；临时恢复目录需要原子 no-clobber 发布语义，而 Windows bind filesystem 不保证相同的
`renameat2(RENAME_NOREPLACE)` 能力。最终 archive 仍写入受管控的 `/cutover/output` mount。

bootstrap 只从校验并恢复出的 archive 字节解析 resolved canonical state。active/pending、未知或
必须迁移的 SQLite 记录会阻断 cutover；成功导入只创建一条无 parent 的 genesis authoring revision。
相同静止输入、相同 archive 路径和完全相同的 UUID/actor/created-at 可以原样重跑：归档字节一致时
复用既有文件，数据库返回 `idempotent_replay=true`；目标归档同名但字节不一致时绝不覆盖。

### 3. 启动 API、签发 Bearer token 并读取 canonical

JWT 中的 role 只是签名断言；数据库中的 active membership 才是授权真源。下面故意签发
`viewer` 断言，而 seed 创建的数据库 owner membership 仍决定实际权限：

```powershell
docker compose -f infra/team/docker-compose.yml up -d api
for ($attempt = 1; $attempt -le 60; $attempt++) {
  curl.exe -fsS http://127.0.0.1:8000/health
  if ($LASTEXITCODE -eq 0) { break }
  Start-Sleep -Seconds 1
}
if ($LASTEXITCODE -ne 0) { throw "team API did not become ready" }

$token = docker compose -f infra/team/docker-compose.yml run --rm -T --no-deps `
  --env "OWCOPILOT_JWT_SECRET=$($env:OWCOPILOT_JWT_SECRET)" `
  --env "TOKEN_TENANT_ID=$($env:TEAM_TENANT_ID)" `
  --env "TOKEN_SUBJECT_ID=$($env:TEAM_OWNER_SUBJECT)" `
  api python -c 'import os; from owcopilot.platform import Principal, Role, mint_token; print(mint_token(Principal(user_id=os.environ["TOKEN_SUBJECT_ID"], tenant_id=os.environ["TOKEN_TENANT_ID"], role=Role.VIEWER)))'

curl.exe -fsS `
  -H "Authorization: Bearer $token" `
  "http://127.0.0.1:8000/team/containers/$($env:TEAM_CONTAINER_ID)/canonical?head=authoring"
```

数据库、schema head、membership 或 PostgreSQL 不可用时，health/canonical 均 fail closed；没有本地
文件或 SQLite 回落。

## archive custody 与恢复核验边界

`team-genesis.zip` 及 `bootstrap-result.json` 必须由部署方复制到版本库和 PostgreSQL volume 之外的
只读保管位置，并保存 `archive_sha256`。可在一个全新的空目录中核验 archive 与逐文件 checksum：

```powershell
$env:EXPECTED_ARCHIVE_SHA256 = "copy-exact-archive_sha256-from-bootstrap-result"
$env:CUTOVER_RESTORE_PARENT_PATH = "<absolute-path-to-empty-restore-parent>"
$restoreParentHost = (Resolve-Path -LiteralPath $env:CUTOVER_RESTORE_PARENT_PATH).Path

docker compose -f infra/team/docker-compose.yml run --rm -T --no-deps `
  --volume "${cutoverOutputHost}:/cutover/input:ro" `
  --volume "${restoreParentHost}:/cutover/restore" `
  --env "EXPECTED_ARCHIVE_SHA256=$($env:EXPECTED_ARCHIVE_SHA256)" `
  migrate python -c 'import os; from owcopilot.teamdb.legacy_archive import restore_legacy_archive; manifest = restore_legacy_archive("/cutover/input/team-genesis.zip", "/cutover/restore/recovered", expected_archive_sha256=os.environ["EXPECTED_ARCHIVE_SHA256"]); print(manifest.source_manifest_hash)'
```

`/cutover/restore/recovered` 必须事先不存在。这个命令只证明 legacy archive 可恢复且 checksum 一致，
不等于 PostgreSQL backup/restore drill；数据库 volume、归档和密钥的生产备份仍由部署方另行完成。
