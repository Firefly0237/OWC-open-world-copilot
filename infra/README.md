# Infrastructure

- `team/`：团队部署镜像、Compose 与 PostgreSQL 初始化脚本。
- `migrations/`：Alembic 环境和数据库版本迁移。

运行入口分别由根目录 `Dockerfile`、`docker-compose.yml` 与 `alembic.ini` 指向这里。
