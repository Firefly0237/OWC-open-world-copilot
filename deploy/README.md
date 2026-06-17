# 部署（WS-P · 托管多租户 SaaS）

> 这些是**部署制品**。在装有 Docker 的机器上验证（`docker compose up`）。开发沙箱里没有 Docker，**未做运行时验证**——
> 但它打包的应用代码本身已全门禁覆盖（`platform/` 模块 + 平台 REST 端点都有测试）。

## 架构
- **一进程一端口**：FastAPI 同时托管编译好的 Vue SPA。
- **正典仍 file-backed**：每租户世界存在 `OWCOPILOT_WORLDS_HOME/<tenant_id>/<world>`（`tenant_world_root` 强制隔离，
  防遍历越界）。
- **控制面（平台元数据）**：租户/用户/成员/审计日志。开发/CI 用 SQLite（`OWCOPILOT_PLATFORM_DB`），
  生产指向 **Postgres**（`DATABASE_URL`）。控制面**不存正典内容**。
- **鉴权**：`Authorization: Bearer <JWT>`（HS256，`OWCOPILOT_JWT_SECRET` 签名）→ `Principal{user, tenant, role}`；
  RBAC 角色 `owner/editor/reviewer/viewer`。本地/CLI/CI 仍可用 `X-API-Key` 回环（= 单租户 LOCAL owner，旧流程不变）。
  生产建议把 HS256 dev-token 换成 OIDC——契约不变（`mint_token`/`verify_token` → `Principal`）。

## 关键环境变量
| 变量 | 说明 |
|---|---|
| `OWCOPILOT_JWT_SECRET` | Bearer 令牌签名密钥（必填，长随机串） |
| `OWCOPILOT_API_KEY` | 服务 API key（real 模式网络调用门禁） |
| `OWCOPILOT_WORLDS_HOME` | 每租户世界存储根 |
| `OWCOPILOT_PLATFORM_DB` | 控制面 SQLite 路径（文件回退） |
| `DATABASE_URL` | 生产 Postgres 连接串 |
| `OPENAI_BASE_URL` / `OPENAI_API_KEY` | 模型服务商 |

## 起栈
```bash
export OWCOPILOT_JWT_SECRET=$(openssl rand -hex 32)
export OWCOPILOT_API_KEY=$(openssl rand -hex 16)
docker compose -f deploy/docker-compose.yml up --build
```

## 平台 REST（控制面）
- `GET  /platform/me` — 解析当前 Principal（Bearer 或 X-API-Key 回环）。
- `POST /platform/tenants` — 建租户 + owner（owner 权限）。
- `POST /platform/memberships` — 加成员并赋角色（owner 权限）。
- `POST /platform/auth/dev-token` — 签发 Bearer（dev/test；生产用 OIDC）。
- `GET  /platform/audit` — 当前租户审计日志。

## 边界（诚实记）
- 本计划周期交付的是**平台能力**（鉴权/RBAC/租户隔离/审计 + 平台端点），全部有测、门禁绿。
- **把租户作用域逐个接入既有 50 个业务端点**是按计划的**增量采用**（针对已冻结的 `tenant_world_root` 契约滚动推进）——
  现有端点保留 X-API-Key 回环、行为不变。
- Docker/Postgres 的**运行时**起栈需在有 Docker 的机器上验证（此处仅提供制品）。
