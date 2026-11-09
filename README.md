# TunnelDeck

基于 Cloudflare Tunnel 的 **Docker 版内网穿透面板**：网页里填一个域名 + 一个端口，几秒内就能把内网服务暴露到公网 HTTPS。

不用手写 `config.yml`，不用每次进控制台点来点去，也不用给每台机器装 cloudflared。

---

## 特性

- **快速映射**：子域名 + 端口两步搞定，自动生成 cloudflared 配置并热重启
- **自动 DNS**：按域名自动找到所属 Zone，创建/更新 `CNAME → <隧道ID>.cfargotunnel.com`（已代理）
- **多连接**：一个面板管理多条隧道（家里 NAS、公司测试机、临时演示……）
- **两种接入**：面板内新建隧道；或粘贴已有隧道的 Token 直接纳管，需要时「一键接管」为本地配置
- **状态可视**：在线/连接中/异常/未运行，直接读 cloudflared 的 `/ready`，另有实时日志
- **端口自检**：添加前可测试「容器 → 目标端口」是否通，避免映射了才发现连不上
- **端口扫描**：从容器视角扫目标主机（常用端口 / 指定范围 / 全端口），扫到 http 服务直接一键映射
- **无外部依赖**：单个容器 = Python 面板 + cloudflared，数据落 SQLite，重启自动拉起

## 目录结构

```
tunnel-deck/
├─ app/
│  ├─ main.py      FastAPI 路由、登录、状态接口
│  ├─ cf.py        Cloudflare API（隧道 / DNS / 域名）
│  ├─ runner.py    config.yml 渲染 + cloudflared 进程管理
│  └─ db.py        SQLite 存储
├─ web/            单页前端（原生 JS，无构建步骤）
├─ Dockerfile      多阶段：从 cloudflare/cloudflared 取二进制
├─ docker-compose.yml
└─ data/           运行后生成：数据库、凭据、配置、日志（已 gitignore）
```

## 快速开始

已发布到 Docker Hub：**`hallezwj/tunnel-deck`**（`linux/amd64` + `linux/arm64` 双架构）

```bash
docker run -d --name tunnel-deck \
  -p 8787:8787 \
  -v ./data:/data \
  --add-host host.docker.internal:host-gateway \
  hallezwj/tunnel-deck:latest
```

打开 `http://localhost:8787`，首次访问就是初始化向导（设访问密码、填 Account ID / API Token）。

或者用 compose（仓库里的 `docker-compose.yml` 已默认拉这个镜像）：

```bash
docker compose pull
docker compose up -d
```

想自己构建：把 compose 里的 `build: .` 打开，或 `docker build -t tunnel-deck .`。

标签：`latest` / `1.0` / `1.0.5`；老 NAS、Apple Silicon 会自动取 arm64 版本。

**首次访问就是初始化向导**，三步顺手做完：

1. **设置面板访问密码**（至少 6 位，必填，只存在数据卷里）
2. **填 Account ID 和 API Token**（可留空，进面板后在「设置」里补）
3. 提交后 Cloudflare 立刻校验一遍，通过就显示「Cloudflare 已连接」

不知道 Account ID / Token 去哪拿？初始化页点「看获取指引」，或看下一节的图文步骤。

不想走向导、想一开机就是固定密码？把 `PANEL_PASSWORD`（以及 `CF_API_TOKEN` / `CF_ACCOUNT_ID`）写进 `.env` 再启动即可，**环境变量优先于面板设置**，此时这两项在面板里会变成只读。

> 首次初始化只允许**本机或局域网**访问，公网 IP 来初始化会被拒绝——防止面板刚起来就被人抢注。要在公网上部署又不想用 `.env`，就先把面板藏在内网后面。

## 先把这两串东西拿到（新手最容易卡在这一步）

面板用两种 Token，别混：

| 名称 | 用在哪 | 从哪拿 |
| --- | --- | --- |
| **Account ID** | 设置页 | 控制台首页右侧栏「Account ID」；或地址栏 `dash.cloudflare.com/<32位ID>` |
| **API Token**（`cfut_…`） | 设置页，用来新建隧道、写 DNS | My Profile → API Tokens → Create Custom Token |
| **隧道 Token**（`eyJ…` 的 base64） | 「导入」已有隧道 | Zero Trust → Networks → Tunnels → 点开隧道 → Configure 里的 `--token` |
| **cloudflared 安装 Token** | 只在 Cloudflare 给的命令里用 | 同上，其实和「隧道 Token」是同一串 |

### Account ID

登录 https://dash.cloudflare.com ，首页右侧账户信息里有 **Account ID**，一键复制即可。
也可以直接看地址栏：`https://dash.cloudflare.com/<32位十六进制>` 那段就是。

### API Token（新建隧道 + 自动 DNS 用）

1. 右上头像 → **My Profile** → **API Tokens**（直达：https://dash.cloudflare.com/profile/api-tokens）
2. **Create Token** → 拉到最下面选 **Create Custom Token**
3. 名称随便填，例如 `tunnel-deck`；权限加两条：
   - `Account` · `Cloudflare Tunnel` · **Edit**
   - `Zone` · `DNS` · **Edit**（Zone Resources 可只选自己的域名，更安全）
4. Continue → **Create Token** → 复制 `cfut_` 开头那串
   ⚠️ 只显示一次，关掉页面就看不到了，先粘到设置里再说。

这两条权限分别对应：新建/接管/删除隧道、给映射的域名写 CNAME。少任何一条，面板都会在对应操作上给出 Cloudflare 原始报错。

### 隧道 Token（导入已有隧道用）

1. Zero Trust 控制台（https://one.dash.cloudflare.com）→ **Networks → Tunnels**
2. 选中那条隧道 → **Configure** / **Install connector**
3. 安装命令里 `--token` 后面那串 `eyJhIjoi…` 就是
4. 它是该隧道的凭据（等于私钥），别贴到公开页面；导入后面板以 600 权限写进数据卷


## 三种使用路径

**A. 面板新建隧道**（推荐）
`连接` 右侧「+ 新建」→ 输入名字 → 面板调用 API 创建一条本地配置隧道并保存凭据 → 进入「快速映射」。

**B. 导入已有隧道**
「导入」→ 粘贴安装命令里的 `--token` 那串 → 立刻接管运行。
这种隧道默认还是「控制台托管」，映射配置以 Cloudflare 控制台为准；点详情里的「**接管到面板**」才会切换成由本面板下发 `config.yml`（切换后控制台里的映射失效）。

**C. 只想跑起来、不改结构**
导入 Token 后直接用，面板只负责进程守护和日志。

## 快速映射怎么填

一行从左到右：**公网域名 → 协议 → 目标主机 → 端口**，然后「添加映射」。

| 字段 | 说明 | 例子 |
| --- | --- | --- |
| 子域名 | 想对外用的前缀，只填前缀就行；带点则视为完整域名 | `nas` 或 `a.b.example.com` |
| 域名 | **下拉选择**：列出账号里已接入的 Zone，选一次会记住 | `example.com` |
| 协议 | 源站是 http 还是 https（自签 https 记得勾跳过校验） | `http` |
| 目标主机 | 服务在哪台机器/容器上，默认 `127.0.0.1` = 装面板这台机器 | `127.0.0.1` |
| 端口 | 目标主机上那个服务监听的端口 | `8080` |

添加后：写 `config.yml` → 重启该连接的 cloudflared → （开启自动 DNS 时）建 CNAME → 访问 `https://nas.example.com`。

### 关于 127.0.0.1：容器里的「本机」不是你的 NAS

面板跑在容器里，容器自己的 `127.0.0.1` 是容器内部，跟宿主机（NAS、家里的电脑）是两套网络。所以默认填 `127.0.0.1` 时，面板会自动把它换成容器能看到宿主机的那条地址（`host.docker.internal`，compose 里已经加了 `host-gateway`），**扫描端口、测试连通性、生成 cloudflared 配置全都按这个语义走**，否则你会碰到「明明 NAS 上 8080 开着，面板扫不出、映射后 502」。

这个替换在「设置」里可开关（默认开）；用 `network_mode: host` 部署时容器和宿主机共用网络栈，`host.docker.internal` 解析不到，面板会自动保持 `127.0.0.1` 原样。

### 不想手动找端口？点「扫描端口」

扫描是在**面板容器内部**发起的，所以结果永远是「**跑着这个面板的那台机器**」的端口——不是你现在打开浏览器那台电脑的端口。

在 NAS 上装、用 PC 打开网页，扫的是 NAS；如果扫出来一堆 `135 / 445 / 3389` 这种 Windows 特征端口，说明你打开的是**跑在 PC 上的那份面板**（检查地址栏是不是 `localhost:8787`），或者面板容器其实部署在 PC 上。扫描结果上方会写明「从 <容器名/IP> 扫 <目标>」，设置页里也能看到面板运行在哪台机器、`127.0.0.1` 被指向了哪个 IP。

- 范围三选一：**常用端口**（约 100 个，几秒出结果）、**指定范围**（如 8000–9000）、**全端口 1-65535**（30~60 秒）
- 结果里会标出端口、常见服务名、以及探测到的 `http` / `https` / `tcp`
- 每行右侧「映射」：直接用当前填的「子域名 + 选中的域名」建一条映射；子域名留空会自动用 `p<端口>`，例如 `p8080.example.com`
- `tcp` 类（SSH、MySQL、Redis…）按钮是灰的，因为 Cloudflare 隧道公网侧只认 HTTP(S)


### 端口写什么，取决于服务在哪

| 服务位置 | 目标主机该填 | 说明 |
| --- | --- | --- |
| 装面板这台机器本身 | `127.0.0.1`（默认） | 面板自动换成宿主机可达地址，NAS 上也能扫到 |
| 同一个 Docker 网络里的容器 | 容器名，如 `app-web` | 面板容器要加入那个网络 |
| 局域网另一台机器（另一台 NAS、路由器） | 它的内网 IP，如 `192.168.1.10` | 容器默认走 NAT，能直达局域网 |

Linux（含群晖/威联通/飞牛等 NAS）上如果宿主机很老、`host-gateway` 不生效，可以改成容器与宿主机共享网络栈：

```yaml
    network_mode: host
    # 并删掉 ports 映射
```

此时 `127.0.0.1` 直接就是 NAS 本身，面板会自动跳过地址替换。

## 数据与备份

全部状态在 `./data`：

```
data/
├─ tunnel-deck.db     连接、映射、设置
├─ conf/<隧道ID>.yml   面板生成的 cloudflared 配置
├─ creds/<隧道ID>.json 隧道凭据（含 secret，权限 600）
└─ logs/<连接ID>.log   追加日志
```

备份直接打包 `data/`；迁移时整目录带走即可。

## 已知边界

- **只有 HTTP(S) 入口**。Cloudflare Tunnel 的公网侧不支持任意 TCP/UDP 端口，所以这里做的是「域名 → 端口」的映射，而不是「公网端口 → 内网端口」。需要 SSH/RDP/数据库这类原始 TCP 时，用 `cloudflared access tcp` 做客户端侧接入，或另配 frp。
- **不要给同一个域名建两条不同目标的映射**（同域名 + 同路径），面板会拦下来。
- **WebSocket 默认可用**，无需额外开关。
- 源站是自签 HTTPS 时，编辑映射勾上「跳过源站证书校验」。
- Cloudflare 侧域名必须已接入当前账号，否则自动 DNS 会报「找不到所属域名」。

## 安全建议

1. 第一次打开就立刻设置访问密码（向导会强制要求）；面板本身不要直接暴露公网，要远程用就再给它自己套一层 Tunnel。
2. API Token 只给 `Tunnel:Edit` + `DNS:Edit`，别用 Global API Key。
3. `data/creds/*.json` 等同隧道私钥，别进 Git、别放公共网盘。
4. 面板只监听容器内 8787，需要的话在 compose 里改成 `127.0.0.1:8787:8787` 只允许本机访问。

## 常见问题

**加了映射但打不开**
先看日志里有没有 `ERR`；再确认「目标主机:端口」在容器内可达（用「测试」按钮）。容器里 `curl` 源站验证最直接：

```bash
docker exec -it tunnel-deck python -c "import urllib.request;print(urllib.request.urlopen('http://host.docker.internal:8080').status)"
```

**状态一直是「连接中」**
多半是出网受阻：`TUNNEL_PROTOCOL=auto` 会先试 QUIC(7844/udp)，被墙或防火墙拦就退回 http2(7844/tcp/443)。可直接设 `TUNNEL_PROTOCOL=http2`。

**DNS 没自动建**
Token 少了 `DNS:Edit`，或该域名不在这个账号下。可点「同步全部 DNS」重试，或手动加 CNAME 到 `<隧道ID>.cfargotunnel.com`（代理开启）。

**接管后控制台里的映射没了**
正常现象：`config_src` 从 `cloudflare` 切成 `local` 后，路由以本面板生成的 `config.yml` 为准。

**忘了面板密码 / 想重走一次初始化**
密码只存在数据卷里，没有找回入口。删掉 `data/tunnel-deck.db` 里的密码记录即可重走向导（连接和映射都在同一张库里，删之前先备份）：

```bash
docker exec tunnel-deck python -c "import sqlite3;c=sqlite3.connect('/data/tunnel-deck.db');c.execute(\"DELETE FROM settings WHERE key='panel_password'\");c.commit()"
```

也可以直接删掉 `PANEL_PASSWORD` 那行后重启容器，面板会重新进入初始化页。

**初始化页提示「只允许本机/内网访问」**
说明你是从公网 IP 打开的。要么在机器上本地打开，要么改用 `.env` 的 `PANEL_PASSWORD` 固定密码。

## HTTP 接口

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| POST | `/api/setup` | 首次初始化（设置访问密码，可选同时写 Account ID / Token） |
| POST | `/api/login` `/api/logout` | 面板登录 / 退出 |
| GET | `/api/state` | 连接、映射、状态、设置（未登录时返回 `needs_setup`） |
| GET | `/api/zones` | 账号下可用的域名 |
| POST | `/api/settings` | 保存 Token / Account ID / 自动 DNS / 面板密码 |
| POST | `/api/tunnels` | 新建隧道 |
| POST | `/api/tunnels/import` | 用 Token 导入 |
| POST | `/api/tunnels/{id}/takeover` | 切换为本地配置 |
| POST | `/api/tunnels/{id}/start\|stop\|restart` | 进程控制 |
| GET | `/api/tunnels/{id}/status` | 在线状态 + 日志尾巴 |
| GET | `/api/tunnels/{id}/config` | 当前生成的 config.yml |
| POST | `/api/tunnels/{id}/mappings` | 新增映射 |
| PATCH/DELETE | `/api/mappings/{id}` | 改 / 删映射 |
| POST | `/api/tunnels/{id}/dns_sync` | 批量补 DNS |
| POST | `/api/check` | 容器侧端口连通性检测 |
| POST | `/api/scan` | 容器侧端口扫描（`mode`: common / range / full） |
