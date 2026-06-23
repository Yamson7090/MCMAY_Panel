# 🧩 MCMAY_Panel - 我的世界服务器管理面板

**一个基于 Python Flask 开发的轻量级 Minecraft 服务器控制面板，旨在为朋友或小型社区提供便捷的开服与管理体验。**

> **核心特性**：实时控制台交互 | 多服管理 | Velocity 代理 | SQLite/MySQL 支持

[![License](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python->=3.10-green.svg)](https://www.python.org/)

---

## 📋 项目简介

MCMAY_Panel 是一个非盈利性质的 Minecraft 共享开服平台后端系统。它允许用户通过 Web 界面创建、启动、停止 Minecraft 服务器，并提供类似原生 Terminal 的实时指令交互功能。

### 🌟 核心功能
*   **用户系统**：支持注册、登录（密码哈希存储）、注销。
*   **实时控制台**：支持发送指令（如 `/op`, `/gamemode`），实时轮询显示服务端日志（含颜色分类）。
*   **服务器管理**：支持多开服务器实例管理，显示服务器状态（运行/停止）、玩家数、端口等信息。
*   **Velocity 代理**：内置 Velocity 支持，自动下载、配置与热重载，实现多服统一接入。
*   **数据持久化**：支持 SQLite（默认）和 MySQL 两种数据库存储方案。

---

## ⚙️ 技术栈与依赖

*   **语言环境**：Python >= 3.10
*   **包管理器**：[uv](https://docs.astral.sh/uv/)
*   **Web 框架**：Flask
*   **数据库**：SQLite3 / PyMySQL
*   **配置格式**：YAML, JSON

---

## 🚀 快速启动

本项目使用 `uv` 作为推荐的包管理器，安装速度极快。

### 1. 环境准备
确保你的系统已安装 **Python 3.10** 或更高版本。

### 2. 克隆项目
```bash
git clone https://github.com/yourname/MCMAY_Panel.git
cd MCMAY_Panel
```

### 3. 安装 uv 并初始化配置文件
```bash
pip install uv
uv run main.py
```

### 4. 配置文件初始化
项目启动时会自动检测配置文件，若不存在将从 `defaults/` 中复制。
初次启动会在生成配置文件后立即退出，请编辑配置文件后继续。
*   **数据库**：默认使用 SQLite，数据将存储在项目根目录的 `users.db` 中。
*   **服务端配置**：请确保项目目录下存在 `servers/` 文件夹用于存放服务端文件。

> **注意**：如果需要修改端口或数据库类型，请编辑 `config.yml` 文件。

### 5. 启动项目
```bash
uv run main.py
```
或直接运行 `start.cmd`（Windows）或 `start.sh`（Linux/macOS）。

*默认监听端口*：**80**（可在 `config.yml` 中修改）

启动成功后，访问 `http://localhost:80` 即可查看面板。

---

## 🛠️ 项目配置 (config.yml)

项目根目录下的 `config.yml` 是核心配置文件。

### 数据库配置
项目支持两种模式，通过 `database.type` 切换：
*   **sqlite**：开箱即用，无需额外安装数据库服务。
*   **mysql**：需要提前安装 MySQL 服务，并修改 `mysql` 下的连接参数（host, port, user, password）。

### 服务器配置
*   `max_servers`：允许系统管理的最大服务器数量。
*   `max_online_servers`：最大同时在线服务器数量。
*   `secret_key`：Flask 会话加密密钥，建议部署时修改。

---

## 🌐 Velocity 代理模式

MCMAY_Panel 内置了对 [Velocity](https://papermc.io/software/velocity) 代理的支持，可实现多台后端服务器统一接入、无缝切换。

### 启用方式
在 `config.yml` 中设置：
```yaml
velocity:
  enable: true
  port: 25565  # Velocity 监听端口
```

### 工作原理
1. **自动初始化**：启用后，系统自动保留 **ID=0** 的服务器槽位作为 Velocity 代理。
2. **自动下载**：首次启动时自动从 PaperMC API 下载最新版 `velocity.jar`。
3. **自动配置**：生成 `velocity.toml`、`forwarding.secret` 和 `start.txt`，开箱即用。
4. **后端同步**：新创建的支持 Velocity 的后端服务器（Paper、Folia、Leaves）会自动注册到 Velocity 配置中。
5. **热重载**：添加或修改后端服务器后，系统自动向 Velocity 发送 `velocity reload` 指令，无需手动重启。

### 支持的服务端核心
| 核心 | 说明 |
|------|------|
| **Paper** | 高性能 Bukkit/Spigot 分支 |
| **Folia** | Paper 的分区多线程分支 |
| **Leaves** | 轻量化 Paper 分支 |

> **注意**：启用 Velocity 后，玩家通过 Velocity 代理地址（`端口:25565`）连接，而非直接连接后端服务器。各后端服务器需配置 `velocity.toml` 中 `player-info-forwarding-mode` 为对应模式，并确保 `forwarding.secret` 一致。

---

## 💻 代码结构

```text
MCMAY_Panel/
├── main.py                 # Flask 应用入口，路由定义
├── definitions.py          # 核心逻辑：数据库操作、服务端启动、日志读取线程
├── config.yml              # 系统配置文件
├── announcements.json      # 公告数据文件
├── templates/              # HTML 模板 (Jinja2)
│   ├── index.html          # 首页
│   ├── login.html          # 登录页
│   ├── register.html       # 注册页
│   ├── backend.html        # 用户后台
│   ├── adminbackend.html   # 管理员后台
│   ├── console.html        # 远程控制台
│   └── filemanager.html    # 文件管理
├── static/                 # 静态资源 (CSS/JS/图标)
├── servers/                # 服务端文件存储目录
│   └── 0/                  # Velocity 代理目录（启用 velocity 后自动生成）
├── defaults/               # 默认配置文件模板
├── start.cmd               # Windows 启动脚本
├── start.sh                # Linux/macOS 启动脚本
└── pyproject.toml          # Python 项目元数据
```

---

## 📝 默认账户与安全

*   **默认管理员**：项目首次启动时会自动创建一个默认管理员账户。
    *   **用户名**：`admin`
    *   **密码**：`123456`（**请务必在生产环境中修改此密码**）

---

## 📎 协议

本项目采用 [MIT License](LICENSE) 开源协议。

---

## ❓ 常见问题

**Q：启动报错提示找不到 config.yml？**
> A：首次运行时，项目会自动从 `defaults/default_config.yml` 复制生成配置文件。请检查项目目录下是否有 `defaults` 文件夹，或手动创建 `config.yml`。

**Q：为什么控制台没有实时刷新日志？**
> A：前端通过轮询 `/api/console` 接口获取日志。请检查浏览器控制台是否有报错，或确认服务端进程是否正常启动。

**Q：如何添加新的服务端核心？**
> A：在 `servers/{id}/` 目录下创建 `start.txt` 文件，写入启动命令（如 `['java', '-jar', 'server.jar', 'nogui']`），系统会自动读取该文件启动。

**Q：Velocity 代理连接失败怎么办？**
> A：请检查 `config.yml` 中 `velocity.enable` 是否为 `true`；确认 `servers/0/velocity.toml` 中的 `player-info-forwarding-mode` 与后端服务器配置一致；确保 `forwarding.secret` 文件内容在所有后端服务器间保持一致。

**Q：如何禁用 Velocity 模式？**
> A：将 `config.yml` 中 `velocity.enable` 设为 `false` 并重启面板即可。ID=0 的 Velocity 服务器将不再自动管理。

---

> **⚠️ 安全警告**：除非你知道自己在做什么且完全理解本项目如何运行，否则永远不要用管理员或 root 权限运行本项目！