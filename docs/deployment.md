# Ubuntu 网关部署与自动更新方案

设计目标：将 `origin/master` 的 Web 仪表盘部署到 `192.168.10.1`，访问地址为 `http://192.168.10.1:18080`；推送到远端 master 后自动发布，失败恢复上一个可用版本。

目标主机为 `ssh osaka`（sean 用户），实际 LAN 地址为 `192.168.10.1/24`、接口 `enp9s0f0np0`，系统 Ubuntu 26.04 x86_64、Python 3.14.4、内存约 15 GiB。原需求中的 `192.168.0.1` 已按实机修正。以下记录设计及落地配置；部署程序位于 `deploy/`。

## 1. 推荐架构

采用 **Python venv + systemd 服务 + systemd timer 主动拉取更新**。适合一台常开、运行网关业务的 Ubuntu 主机，不需要为部署开放公网入口。

```mermaid
flowchart LR
    Dev[开发机 push master] --> Git[GitHub hangchow/opdash]
    Timer[网关 systemd timer] -->|定时 fetch| Git
    Timer --> Deploy[准备版本 / 检查 / 切换 / 回滚]
    Deploy --> App[opdash-web.service]
    Browser[局域网浏览器] -->|192.168.10.1:18080| App
    App -->|127.0.0.1:11111| OpenD[网关上手动启动并登录的 Futu OpenD]
```

默认每轮更新任务结束后等待 60 秒再检查；部署延迟包含轮询、依赖准备和启动检查时间。自动部署只由远端 master 的提交变化触发，本地编辑和未 push 的提交不触发。

OpenD 保持网关开机后手动启动的方式，Web 固定连接 `127.0.0.1:11111`。确认 OpenD 监听回环地址，不向 LAN/WAN 开放 `11111`。浏览器只访问 Web 的 `18080` 端口。

暂不引入 Docker、反向代理和常驻 CI runner。若以后需要域名、HTTPS、登录认证或更低的切换中断，再扩展反向代理层。

## 2. 当前代码对部署的约束

| 现状 | 设计处理 |
| --- | --- |
| `opdash_web.py` 在 `main()` 中创建后端和 FastAPI app | 直接执行 Python 入口；单进程，不使用 reload 或多个 worker，避免重复行情连接与轮询 |
| `--host` / `--port` 是 OpenD，`--web_host` / `--web_port` 是 HTTP | 两组配置分开，HTTP 明确绑定 `192.168.10.1:18080` |
| `backend.start()` 在 HTTP 监听之前执行 | 启动检查设置明确期限，初始建议 120 秒，依据实际 OpenD 响应耗时调整 |
| `/healthz` 返回存活状态和发布 SHA，`/readyz` 返回数据就绪状态 | 部署时同时校验存活、目标版本和数据成功刷新 |
| Web 自动发现模式允许启动时空仓，成功轮询为空会清空旧面板 | 空仓也能就绪，后续持仓变化自动发现；GUI 初始解析保留原行为 |
| 所有 OpenD 端口共享一个 `--host`，最多两个端口 | 配置必须符合此限制；不同主机的 OpenD 不在本次直接支持范围 |
| GUI 的 `requirements.txt` 与 Web 部署依赖分开 | Web 使用目标 Python 3.14 验证的 `requirements-web.lock` |
| Plotly 2.35.2 和许可证随版本本地发布 | 浏览器无需访问 Plotly CDN；app.js 和 CSS URL 携带发布 SHA |

上述改进已实现：Web 支持空仓启动并使用全部交易市场发现标的；新增 `/readyz`；`requirements-web.lock` 在目标 Python 3.14.4 上生成并验证；Plotly 及许可证保存在 `web/vendor/`。GUI 入口的初始标的解析方式保留。不能复用开发机 macOS 的 `.venv`。

## 3. 运行目录与权限

```text
/opt/opdash/
  releases/<commit-sha>/       # 完整代码、web 资源、该版本独立的 .venv
  current -> releases/<sha>   # 当前运行版本
  previous -> releases/<sha>  # 上一个成功版本
/var/lib/opdash-deploy/
  repo.git/                  # 拉取用 Git 缓存
  state.json                 # 成功/失败 SHA、重试时间、发布事务状态
  deploy.lock                # timer 与手工发布共用锁
/etc/opdash/opdash.env        # OpenD、HTTP、轮询等环境配置，不进入发布目录
/usr/local/libexec/opdash/    # 管理员安装的启动、部署和回滚程序
/etc/systemd/system/          # opdash-web.service / opdash-deploy.service / timer
```

运行用户 `opdash` 只读取代码和配置，日志写入 journal；需要的 SDK 日志/缓存使用独立可写状态目录。部署用户 `opdash-deploy` 负责拉取、构建及版本目录，不以 root 安装 Python 依赖。运行用户不可读取 Git 私钥，也不可修改版本、配置或 systemd unit。

首次由管理员安装服务、固定的启动/部署程序及必要的最小权限规则。部署用户只获准通过固定命令重启、查询 `opdash-web.service`，不能执行任意 sudo 命令。自动更新不从仓库以 root 执行脚本，也不自动覆盖系统服务定义。

仓库已核实为公开仓库，网关使用 `https://github.com/hangchow/opdash.git` 匿名只读拉取，不存储 GitHub 凭据。若以后改为私有仓库，应改用仓库专用只读 Deploy Key。[GitHub 官方说明](https://docs.github.com/en/authentication/connecting-to-github-with-ssh/managing-deploy-keys)

## 4. 启动配置

配置使用已确认的网关本机 OpenD 地址：

```ini
FUTU_HOST=127.0.0.1
FUTU_PORTS=11111
WEB_HOST=192.168.10.1
WEB_PORT=18080
POLL_INTERVAL=10
PRICE_INTERVAL=10
UI_INTERVAL=5
PRICE_MODE=auto
PROFIT_HIGHLIGHT_THRESHOLD=80
```

应用参数由固定启动程序 `deploy/start.py` 将环境变量转换成 CLI 参数，并通过 `exec` 启动当前版本，等价命令为：

```bash
/opt/opdash/current/.venv/bin/python -u /opt/opdash/current/opdash_web.py \
  --host 127.0.0.1 --port 11111 \
  --web_host 192.168.10.1 --web_port 18080 \
  --poll_interval 10 --price_interval 10 --ui_interval 5
```

此命令省略股票参数，Web 会自动发现期权标的；成功查询为空时也能启动。启动程序支持可选的 `STOCK_CODES`，使用参数数组传递。

服务启用开机启动，使用 `Restart=on-failure`、`RestartSec=15s`，设置合理停止超时。固定启动程序在运行 Python 入口前，每 5 秒检查 `127.0.0.1:11111` 是否接受连接，未启动时保持可中断的等待并限频记录日志；不让服务因等待人工启动而耗尽重试次数。端口打开仅代表 TCP 可连接，登录/查询状态另行判断。应用启动失败后由 systemd 继续限速重试，可使用 `StartLimitIntervalSec=0` 配合上述重试间隔，避免因 OpenD 未就绪而永久停止重试。`network-online.target` 只提供启动顺序，不能保证 OpenD 已登录。

网关重启后的顺序为：systemd 启动 Web 的等待程序 → 用户手动启动并登录 OpenD → Web 自动启动并连接 OpenD。OpenD 不纳入自动部署或自动启动管理，Web 不对 OpenD unit 设置自动启动依赖，保留手动启动 OpenD 的顺序。当前应用在后端初始化之前不监听 HTTP，因此等待期间浏览器暂时无法访问仪表盘；若以后希望显示等待页面，需要调整应用启动生命周期。

Web 限制为单核 CPU、1 GiB 内存；构建限制为单核 CPU、2 GiB 内存并降低 IO/CPU 调度优先级。版本保留与清理限制持续占盘；当前可用磁盘约 717 GiB。应用绑定 LAN 地址，同时在现有防火墙 INPUT 链限定 LAN 接口与实际可信网段，拒绝 WAN 和访客网访问该端口。绑定 LAN IP 本身不能替代防火墙。保留现有转发、NAT 和管理规则。

当前 HTTP 接口没有登录鉴权，能访问它的客户端可读取持仓数据。本方案访问范围为可信局域网；若需扩大范围，应先加入认证和 HTTPS。

## 5. 自动发布流程

1. 获取独占锁，检查暂停状态；对 Git、pip 和健康检查都设置超时。
2. `fetch` 远端 master 并解析出完整目标 SHA。与已成功版本相同则退出，不重启服务；网络错误保留当前版本，下轮重试。
3. 在新版本独立目录导出该 SHA 的完整代码。在最终路径创建独立 venv，按锁定文件安装依赖；不修改正在运行的 venv，也不移动已创建的 venv。
4. 完成语法编译、模块导入、依赖一致性及静态文件存在性检查。构建失败不切换；不得提前以生产 OpenD 配置启动第二个轮询进程。
5. 若当前服务或 OpenD 已处于异常状态，暂缓切换并记录依赖异常，避免将既有故障误判为新代码失败。开机后等待手动启动 OpenD 期间可 fetch/准备候选版本，但不切换；OpenD 就绪后再发布。首次部署没有当前服务时，用 15 秒超时的独立 SDK 探针验证行情/交易登录状态后进行首次启动；端口打开但等待短信验证时，只准备代码，不启动候选版本。
6. 持久化事务记录：旧 SHA、目标 SHA、阶段。停止旧服务，在同一文件系统以临时软链加 rename 原子替换 `current`，启动新服务。切换有短暂中断，不承诺零停机。
7. 在检查期限内验证 `/healthz`、首页、关键静态资源及 `/api/snapshot` 的有效 JSON 和字段结构，并确认服务持续存活。健康请求访问 `192.168.10.1:18080`，与绑定地址一致。
8. 通过后更新成功状态和 `previous`。失败则恢复旧 `current`、重启旧版本并再次检查；首次部署没有旧版本时保留失败状态并报错。回滚也失败时保留诊断记录并停止自动切换。
9. 记录失败 SHA 并设置冷却期；构建或切换失败后对该 SHA 冷却 30 分钟，支持 `update --retry`；新 SHA 不受旧版本冷却限制。OpenD 未就绪时只保留已准备版本，不切换服务。
10. 保留最近 3 个成功版本，清理时始终保护 `current`、`previous` 和事务涉及的版本。任务中断或重启后先恢复未完成事务，再开始下一次发布。

上线门禁检查 `/healthz` 的发布 SHA、`/readyz` 和页面/快照/静态资源，连续通过三次才确认成功。`/readyz` 要求每个端口的持仓轮询成功且不陈旧，以及每个当前标的最近成功取得有效价格；阈值为 max(90 秒, 对应轮询间隔 × 3)。代码已核实持仓查询失败会抛错，不会刷新成功时间；额外记录每个标的成功取得价格的时间，避免某个标的持续失败被其他标的刷新掩盖。空仓只要求持仓查询成功。不要求价格持续变化，也不声称该接口校验所有期权 Greeks 的完整性。

timer 示例（配套 `opdash-deploy.service` 使用 `Type=oneshot`，不设 `RemainAfterExit=yes`）：

```ini
[Unit]
Description=Check opdash master updates

[Timer]
OnBootSec=2min
OnUnitInactiveSec=60s
AccuracySec=5s
Unit=opdash-deploy.service

[Install]
WantedBy=timers.target
```

`OnBootSec` 用于开机后检查，`OnUnitInactiveSec` 从上轮任务结束计时；已运行的同名任务不会由 timer 再启动一个实例。手工操作仍需共享文件锁。以上语义见 [systemd 官方 timer 文档源文件](https://github.com/systemd/systemd/blob/main/man/systemd.timer.xml)。

## 6. 运维与验收

日常状态与手动触发：

```bash
systemctl status opdash-web.service
journalctl -u opdash-web.service -n 100 --no-pager
journalctl -u opdash-deploy.service -n 100 --no-pager
systemctl list-timers opdash-deploy.timer
sudo systemctl start opdash-deploy.service
sudo systemctl disable --now opdash-deploy.timer
```

最后一条用于持久暂停自动更新，但不会终止已开始的发布；手工回滚必须等待任务完成并取得同一把锁。回滚命令应同时设置暂停状态，防止 timer 将版本马上重新升级。恢复自动更新时清除暂停状态并重新启用 timer。

| 验收场景 | 预期结果 |
| --- | --- |
| 首次发布 | LAN 可打开页面；实际 OpenD 持仓/行情可读取；记录成功 SHA |
| push 新提交到远端 master | 自动发现并发布新 SHA，页面/静态资源使用新版本 |
| 远端无变化 | 无重启、无重复安装依赖 |
| 依赖安装失败或 GitHub 断网 | 旧服务不受影响，日志包含失败阶段 |
| 新版本启动/接口失败 | 自动恢复上一个成功版本，失败 SHA 不触发连续重启 |
| 网关重启 | Web 等待本机 OpenD；手动启动并登录后自动恢复；timer 在依赖就绪前暂缓切换 |
| 发布进程中断 | 未完成事务可恢复；成功版本不被清理；timer 继续工作 |
| OpenD 离线、登录失效、空仓 | 明确显示/记录各自状态，不用空数据冒充查询成功 |
| WAN/访客网请求 HTTP 端口 | 被现有防火墙中的定向规则拒绝；原有网关业务正常 |
| 手工回滚 | 恢复指定成功版本且保持暂停自动更新 |

部署日志记录 SHA、阶段、耗时和错误，不输出密钥或完整持仓快照。

## 7. 实施文件清单与顺序

已增加 Web 依赖锁定文件、`deploy/start.py` / `deploy/deploy.py` / `deploy/install.sh`、Web/部署/timer/防火墙/OpenD unit、环境配置示例和测试；实现空仓启动、就绪状态和 Plotly 本地化。安装器中的 IP/接口配置针对 osaka，移植到其他主机前必须调整。

实施顺序：核实网关与 OpenD 环境 → 实现及验证部署文件和必要应用修改 → 首次手动发布并验收 → 验证失败回滚 → 启用 timer → 用一次 master 提交验证自动更新。

自动部署默认信任远端 master。建议 master 合并前执行适配目标 Python 的依赖安装与应用检查；如以后要求“CI 成功才发布”，需要额外验证目标 SHA 对应的 CI 状态或使用 CI 发布的版本清单，单纯 fetch 不会自动等待 CI。


## 8. OpenD 安装与登录

使用富途官方 Ubuntu 包中的命令行版 `10.10.7008`，安装于 `/opt/futu-opend/10.10.7008`，以 `futu-opend` 系统用户运行。API 仅监听 `127.0.0.1:11111`，Telnet/WebSocket 未启用。二进制和 XML 配置由 root 管理，SDK 自带的登录状态保存在该用户的私有 home `/var/lib/futu-opend`（0700）。`/etc/futu-opend/account.env` 只保存登录账号，权限 0640，组为 futu-opend；密码通过首次交互登录输入并由 OpenD 的“记住密码”功能保存，不进入仓库或进程参数。

新版首次登录可能要求短信验证码，由账户所有者完成。首次登录命令（先停止已有实例）：

```bash
sudo systemctl stop futu-opend
sudo -u futu-opend -H /opt/futu-opend/10.10.7008/FutuOpenD \
  -cfg_file=/etc/futu-opend/FutuOpenD.xml -no_monitor=1 -lang=en
```

依据原需求保留开机后手动启动 OpenD：安装 `futu-opend.service`，但不 enable；本次登录配置完成后启动它。之后使用：

```bash
sudo systemctl start futu-opend
sudo systemctl status futu-opend
sudo journalctl -u futu-opend -n 50 --no-pager
```

Web 和部署 timer 开机自动启动，等待 OpenD。若之后希望 OpenD 也随开机启动，可执行 `sudo systemctl enable futu-opend`。登录状态失效时需重新交互登录；自动部署不重启、升级或重新登录 OpenD。官方依据：[命令行安装及记住密码登录](https://openapi.futunn.com/futu-api-doc/opend/opend-cmd.html)、[短信验证命令](https://openapi.futunn.com/futu-api-doc/opend/opend-operate.html)。

## 9. 发布控制

所有发布控制命令使用同一把锁；普通管理用户通过 sudo 切换为部署用户：

```bash
sudo -u opdash-deploy -H python3 /usr/local/libexec/opdash/deploy.py status
sudo -u opdash-deploy -H python3 /usr/local/libexec/opdash/deploy.py pause
sudo -u opdash-deploy -H python3 /usr/local/libexec/opdash/deploy.py rollback
sudo -u opdash-deploy -H python3 /usr/local/libexec/opdash/deploy.py resume
sudo -u opdash-deploy -H python3 /usr/local/libexec/opdash/deploy.py update --retry
```

`rollback` 默认恢复 previous，也可追加保留的成功版本完整 SHA；回滚后保持暂停。`resume` 清除暂停并立即检查 master。如果曾 disable timer，另外运行 `sudo systemctl enable --now opdash-deploy.timer`。

若回滚未就绪，发布器暂停自动更新并保留事务。先修复 OpenD/服务依赖，再运行 `resume` 恢复旧版本和后续更新。不要手动删除状态文件或改动已发布版本的 venv。

Web 单元和 `/usr/local/libexec/opdash/` 程序由管理员安装，仓库更新不会自动覆盖它们；修改这些基础设施文件后，需要管理员重新执行经检查的安装脚本。应用、依赖锁定文件和前端资源会跟随 master 自动发布。

验证命令：`python -m unittest discover -s tests -v`；Web 测试需要先安装 Web 依赖。防火墙由独立 `inet opdash_guard` 表约束 18080 的 LAN 接口/网段及 11111 的本机访问，另通过现有 UFW 放行指定 LAN HTTP 流量；不修改网关 NAT、转发及其他服务规则。


## 10. 实机验收记录（2026-09-12）

- 16 项测试通过，覆盖独立版本切换、失败回滚、事务恢复、失败冷却、OpenD 未登录等待、空仓及逐标的行情就绪。
- HTTP 冒烟检查通过：首页、健康接口、快照及本地 JS/CSS/Plotly 文件均能正常响应。
- OpenD 完成短信设备验证，并已验证切换到 systemd 后使用记住密码正常登录；未保存明文密码到仓库或启动参数。
- 首个成功版本 `846d5a8`；LAN 请求 `/healthz`、`/readyz` 返回 200，真实持仓与标的价格就绪。
- OpenD 的 `11111` 仅监听回环地址，从 LAN 连接被阻止；Web 仅绑定 `192.168.10.1:18080`。
- 服务开机配置：Web、部署 timer、防火墙已 enable；OpenD 仍为手动 start，符合原启动约定。

故障注入和真实 master 更新的后续验收以 `/var/lib/opdash-deploy/state.json` 与 `journalctl -u opdash-deploy` 的发布记录为准。未重启整台网关，开机行为通过 systemd 配置和服务重启验证。
