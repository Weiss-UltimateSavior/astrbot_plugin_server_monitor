# 服务器状态监控 (Server Monitor)

AstrBot 服务器在线状态监控插件，支持多服务器 Ping 检测、多 Web 服务健康检查、到期/异常/掉线 提醒、每日状态报告。

## 功能

- **多服务器监控** — 同时监控多台服务器，定时 Ping 检测在线状态
- **多 Web 服务健康检查** — HTTP/HTTPS 健康检查，支持自定义状态码、关键字匹配、SSL 跳过等
- **掉线/恢复通知** — 服务器离线或恢复在线时自动推送通知到指定群
- **到期提醒** — 服务器到期前自动提醒，到期后独立发送过期通知
- **每日报告** — 定时发送文字摘要 + HTML 渲染的状态图片（含服务器和 Web 服务）
- **手动管理** — 支持指令动态添加/删除服务器和 Web 检查，重启不丢失
- **权限控制** — 可配置白名单限制指令使用者
- **多平台** — 支持 QQ (OneBot / NapCat)、Telegram、Discord 等

## 安装

将插件放入 AstrBot 的插件目录，安装依赖：

```bash
pip install apscheduler>=3.11.0 aiohttp>=3.8.0
apt install ping_interval（进入容器安装）
```

或在 WebUI 插件市场安装。

## 快速开始
初次使用请 apt install ping_interval（进入容器安装）
### 1. 配置服务器列表

在 WebUI → 插件配置中，填写 `servers` 字段（JSON 数组）：

```json
[
  {
    "name": "主服务器",
    "ip": "192.168.1.100",
    "announcement": "欢迎访问主服务器",
    "expiry_date": "2026-12-31"
  }
]
```

| 字段 | 必填 | 说明 |
|------|------|------|
| `name` | 是 | 服务器名称 |
| `ip` | 是 | IP 地址或域名 |
| `announcement` | 否 | 在线时的公告文本 |
| `expiry_date` | 否 | 到期日期，格式 `YYYY-MM-DD` |

### 2. 配置 Web 服务健康检查（可选）

在 `web_checks` 中填写需要监控的 HTTP 端点（JSON 数组）：

```json
[
  {
    "name": "官网首页",
    "url": "https://example.com",
    "expected_code": 200,
    "timeout": 5
  },
  {
    "name": "API 健康检查",
    "url": "https://api.example.com/health",
    "expected_code": 200,
    "must_contain": "\"status\":\"ok\""
  },
  {
    "name": "内网管理后台",
    "url": "https://admin.internal.example.com",
    "expected_code": 200,
    "verify_ssl": false,
    "host_header": "admin.example.com"
  }
]
```

| 字段 | 必填 | 默认值 | 说明 |
|------|------|--------|------|
| `name` | 是 | - | 检查项名称 |
| `url` | 是 | - | 检查地址 |
| `method` | 否 | `GET` | HTTP 方法 |
| `expected_code` | 否 | `200` | 期望状态码 |
| `timeout` | 否 | `5` | 超时秒数 |
| `host_header` | 否 | 空 | 自定义 Host 头 |
| `must_contain` | 否 | 空 | 响应体应包含的关键字（限检查前 64KB） |
| `verify_ssl` | 否 | `true` | 是否验证 SSL 证书（内网自签证书需设为 false） |
| `allow_redirects` | 否 | `true` | 是否跟随 HTTP 重定向 |

### 3. 配置通知群组

在 `target_groups` 中填写接收通知的群组标识符，格式为 `平台名称:GroupMessage:群号`：

```
aiocqhttp:GroupMessage:123456789
```

> **注意**：不同消息平台（NapCat、Lagrange 等）的平台名称可能不同，请使用 `/绑定通知群` 指令自动学习。

### 4. 激活通知

在目标群发送：

```
/绑定通知群
```

插件会自动捕获该群的正确消息链路，之后定时通知即可正常发送。每个配置的群组只需执行一次。

## 指令列表

### 服务器管理

| 指令 | 参数 | 说明 |
|------|------|------|
| `/服务器状态` | 无 | 查看所有服务器和 Web 服务状态（文字） |
| `/服务器状态图` | 无 | 查看服务器状态（图片，含 Web 服务卡片） |
| `/ping` | `<IP>` | 手动检测指定 IP |
| `/添加服务器` | `<名称> <IP> [公告] [到期日期]` | 添加监控服务器 |
| `/删除服务器` | `<IP>` | 删除监控服务器 |

### Web 服务检查

| 指令 | 参数 | 说明 |
|------|------|------|
| `/添加Web检查` | `<名称> <URL> [期望状态码] [关键字]` | 添加 Web 健康检查 |
| `/删除Web检查` | `<名称>` | 删除 Web 健康检查 |

### 其他

| 指令 | 参数 | 说明 |
|------|------|------|
| `/绑定通知群` | 无 | 在当前群激活通知链路 |
| `/服务器帮助` | 无 | 显示帮助 |

## 配置项

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `servers` | 字符串 | `[]` | JSON 格式服务器列表 |
| `web_checks` | 字符串 | `[]` | JSON 格式 Web 检查列表 |
| `target_groups` | 列表 | `[]` | 接收通知的群组标识符 |
| `admin_users` | 列表 | `[]` | 授权用户 ID 白名单，留空允许所有人 |
| `ping_interval` | 整数 | `10` | Ping 检测间隔（秒），范围 5-300 |
| `ping_timeout` | 整数 | `3` | Ping 超时时间（秒），范围 1-10 |
| `expiry_notify_days` | 整数 | `1` | 到期提前通知天数，范围 1-30 |
| `offline_notify_enabled` | 布尔 | `true` | 服务器离线时是否通知 |
| `online_notify_enabled` | 布尔 | `true` | 服务器恢复在线时是否通知 |
| `daily_report_time` | 字符串 | `08:00` | 每日报告时间（HH:MM），留空关闭日报 |
| `max_concurrent_checks` | 整数 | `5` | 最大并发 HTTP 检查数，范围 1-20 |

## 通知说明

### 服务器通知

| 通知类型 | 触发条件 |
|----------|----------|
| 🔴 离线告警 | 连续 2 次 Ping 失败后发送，每次离线仅发一次 |
| 🟢 恢复在线 | 服务器从离线恢复在线时发送 |
| ⏰ 即将到期 | 到期前 `expiry_notify_days` 天内发送一次 |
| ⚠️ 已过期 | 到期日后发送一次（独立于即将到期通知） |

### Web 服务通知

| 通知类型 | 触发条件 |
|----------|----------|
| 🔴 Web 服务异常 | 连续 2 次 HTTP 检查失败后发送 |
| 🟢 Web 服务恢复 | Web 服务从异常恢复时发送 |

### 定时报告

| 通知类型 | 内容 |
|----------|------|
| 📊 日报 | 每日定时发送文字摘要（服务器 + Web 服务统计）+ HTML 图片 |

## 工作原理

1. 插件按 `ping_interval` 间隔对所有服务器执行 ICMP Ping
2. 同时对 Web 检查列表执行 HTTP 健康检查（通过 `asyncio.Semaphore` 控制并发）
3. 状态变更（在线↔离线、健康↔异常）时实时通知
4. 到期检测伴随每次 Ping 循环进行
5. 每日报告按 `daily_report_time` 定时发送
6. 通过 `/绑定通知群` 捕获正确的消息链路，确保主动通知能正常送达
7. 添加/删除的服务器和 Web 检查自动持久化到 `servers_data.json`

## 依赖

- Python >= 3.8
- AstrBot >= v4.0
- apscheduler >= 3.11.0
- aiohttp >= 3.8.0
