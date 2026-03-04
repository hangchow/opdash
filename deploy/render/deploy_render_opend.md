# Render 部署（同容器启动 Futu OpenD + Web）

本文目标：在 Render Web Service 内，容器启动后自动拉起 OpenD，再启动 `plot_positions_option_web.py`。

## 1. 账号与仓库连接

1. 登录/注册 Render（这个步骤需要你本人完成邮箱/登录验证）。
2. 在 Render 里选择 `New +` -> `Blueprint`。
3. 连接 `hangchow/opdash` 仓库，分支选 `master`。
4. Blueprint Path 填 `deploy/render/render.yaml`（部署文件已集中在 `deploy/render/`，不影响主功能代码目录）。

## 2. 必填环境变量

在 Render 服务 `Environment` 中设置：

- `STOCK_CODES`：例如 `US.AAPL,US.TSLA`
- `FUTU_LOGIN_ACCOUNT`：Futu 登录账号
- `FUTU_LOGIN_PWD` 或 `FUTU_LOGIN_PWD_MD5`：二选一
- `OPEND_DOWNLOAD_URL`：OpenD Linux 命令行包下载地址（tar/zip）

可选变量：

- `FUTU_API_PORT`（默认 `11111`）
- `POLL_INTERVAL`（默认 `10`）
- `PRICE_INTERVAL`（默认 `10`）
- `UI_INTERVAL`（默认 `5`）
- `PRICE_MODE`（默认 `implied`）
- `PROFIT_HIGHLIGHT_THRESHOLD`（默认 `80`，例如设为 `70`）
- `FUTU_EXTRA_ARGS`（传递额外 OpenD 启动参数）

## 3. 启动流程

Render 每次部署/重启时会执行 `deploy/render/start.sh`：

1. 校验必需环境变量
2. 若容器内未找到 OpenD，使用 `OPEND_DOWNLOAD_URL` 下载并解压
3. 启动 OpenD，并等待端口就绪
4. 启动 web 服务（监听 Render 分配的 `PORT`）

健康检查路径为 `/healthz`。

## 4. 常见问题

- OpenD 启动失败：先看 Render 日志里的 `opend.stderr.log` 输出。
- 一直卡在等待端口：通常是 OpenD 参数不完整，或下载包不匹配当前系统架构。
- 免费套餐休眠：Render Free 的 Web Service 可能在无流量时休眠，恢复后会重新执行启动流程。

## 5. 安全建议

- 不要把 `FUTU_LOGIN_PWD` 明文写进仓库，只放到 Render 环境变量。
- 优先使用 `FUTU_LOGIN_PWD_MD5`。
- 如果对外公开，建议后续给 `/api/snapshot` 增加认证保护。
