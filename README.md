# Account Import

一个用于“粘贴兑换码 → 找回并下载额度 → 导入 Sub2API”的小型工作台。

- 前端：React + TypeScript + Vite，源码位于 `web/`
- 后端：FastAPI，使用现有 `redeem_api_sdk.py`
- 导入：调用 Sub2API 官方 `POST /api/v1/admin/accounts/batch` 批量创建接口
- 持久化：Sub2API 地址、Access Token、目标分组、目标代理和 TLS 选项保存在服务端配置文件
- 401 恢复：扫描 Sub2API 已记录的 401 账号，从备注或旧名称读取兑换码，原地更新凭据并清除错误状态
- 本地账本：使用 SQLite 记录邮箱、兑换码、Sub2API 账号 ID、导入/恢复结果与时间，不保存账号凭据
- 安全：配置文件权限固定为 `0600`，读取接口不会回传 Token；下载凭据只在内存中流转

## 开发运行

安装依赖：

```bash
uv sync
cd web && bun install
```

启动 FastAPI：

```bash
uv run uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

另开一个终端启动 React：

```bash
cd web
bun run dev
```

打开 `http://localhost:5173`。Vite 会把 `/api` 请求代理到 `http://127.0.0.1:8000`。

## 单进程运行

先构建前端，再由 FastAPI 同时托管页面和 API：

```bash
cd web
bun install
bun run build
cd ..
uv run uvicorn app.main:app --host 0.0.0.0 --port 8000
```

打开 `http://localhost:8000`。

## 使用说明

1. 填写兑换服务地址和 Sub2API 实例地址。
2. 从 Sub2API 已登录管理端取得管理员 Access Token，并粘贴到页面；点击“保存并刷新”。
3. 从远端加载的列表中选择目标分组和代理，不选择代理时使用直连。
4. 粘贴兑换码；支持换行、空格、中文/英文逗号分隔，最多 100 个。
5. 默认使用“下载全部额度”；“只找回 401”只会下载失效后成功找回的账号。
6. 启动任务并等待进度到 100%，页面会展示下载和导入明细。

也可以使用“401 自动找回”：先扫描 Sub2API 账号状态，再对带有兑换码的 401 账号执行找回。新账号从备注读取兑换码，旧格式账号兼容名称末尾的兑换码；找回结果按兑换码和邮箱匹配后原地更新，因此会保留原账号 ID、分组、代理和历史用量。扫描读取的是 Sub2API 已记录的错误状态，不会主动向所有上游账号发送测试请求。

导入前会统一重写账号信息：账号名称使用凭据邮箱，备注使用该下载任务对应的兑换码。凭据中找不到邮箱时，对应额度文件会跳过并在执行日志中说明原因。

任务状态保存在 FastAPI 进程内存中，服务重启后历史任务会清空。应用会把兑换码按每 20 个一批串行执行，默认单批最长等待 10 分钟。

## Sub2API 配置持久化

默认配置文件为 `data/config.json`，可通过环境变量更改路径：

```bash
ACCOUNT_IMPORT_CONFIG_FILE=/secure/path/account-import.json \
  uv run uvicorn app.main:app --host 0.0.0.0 --port 8000
```

配置采用原子写入，文件权限固定为 `0600`。Access Token 以明文保存在该受限文件中，因此应确保运行 FastAPI 的系统用户和配置目录可信。前端只能读取是否已经配置 Token，不能取回 Token 原文。更新其他配置时将 Token 留空，会继续保留原 Token。

## 本地账号记录

成功导入或完成 401 找回后，应用会把非敏感的账号索引写入 `data/account-import.db`，并追加成功/失败操作事件。数据库文件权限固定为 `0600`，包含邮箱、兑换码、Sub2API 地址和账号 ID、平台、任务 ID、结果及时间；不会保存 Access Token、OAuth Token 或下载凭据。升级时仍会兼容旧的 `data/team-import.db`。可通过环境变量修改路径：

```bash
ACCOUNT_IMPORT_DATABASE_FILE=/secure/path/account-import.db \
  uv run uvicorn app.main:app --host 0.0.0.0 --port 8000
```

前端“本地账号记录”区域会显示最近更新的账号，完整分页数据可通过 `GET /api/records/accounts` 读取。任务进度本身仍保存在内存中，服务重启后会清空，但 SQLite 账号记录不会丢失。

## 检查

```bash
uv run pytest
uv run python -m compileall -q app redeem_api_sdk.py
uv run ruff check app tests
cd web && bun run lint && bun run build
```
