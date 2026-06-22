# 🧩 MCMAY_Panel - 我的世界服务器管理面板

**一个基于 Python Flask 开发的轻量级 Minecraft 服务器控制面板，旨在为朋友或小型社区提供便捷的开服与管理体验。**

> **核心特性**：实时控制台交互 | 多服管理 | SQLite/MySQL 支持

[![License](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python->=3.10-green.svg)](https://www.python.org/)

---

## 📋 项目简介

MCMAY_Panel 是一个非盈利性质的 Minecraft 共享开服平台后端系统。它允许用户通过 Web 界面创建、启动、停止 Minecraft 服务器，并提供类似原生 Terminal 的实时指令交互功能。

### 🌟 核心功能
*   **用户系统**：支持注册、登录（密码哈希存储）、注销。
*   **实时控制台**：支持发送 OP 指令（如 `/op`, `/gamemode`），实时轮询显示服务端日志（含颜色分类）。
*   **服务器管理**：支持多开服务器实例管理，显示服务器状态（运行/停止）、玩家数、端口等信息。
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
*   `secret_key`：Flask 会话加密密钥，建议部署时修改。

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
├── servers/                # 服务端文件存储目录 (运行时自动生成)
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

**Q：项目目录名可以改成 MCMAY_Panel 吗？**
> A：可以。重命名文件夹后，确保启动脚本中的路径正确即可，项目本身不依赖文件夹名称。

---

> **⚠️ 安全警告**：除非你知道自己在做什么且完全理解本项目如何运行，否则永远不要用管理员或 root 权限运行本项目！