# ============================================================
# definitions.py — HiveMC 核心业务逻辑层
# ============================================================
# 本模块负责所有底层操作：
#   - 配置文件/公告的加载与管理
#   - Minecraft Java 服务端进程的启停与日志读取
#   - 用户数据库（SQLite / MySQL 双模式）操作
#   - 服务器所有权与开服上限管理
#   - Velocity 代理的自动配置与 JAR 下载
# ============================================================

import yaml          # 解析 config.yml 配置文件
import sqlite3       # SQLite 轻量级数据库
import pymysql       # MySQL 数据库驱动
import json          # JSON 解析（公告、API 响应等）
import os            # 文件和路径操作
import subprocess    # 启动/管理 Minecraft Java 子进程
import threading     # 后台线程读取子进程输出
import queue         # 线程安全的日志队列（Queue）
import urllib.request, urllib.error  # 从 PaperMC API 下载 Velocity JAR
from werkzeug.security import generate_password_hash, check_password_hash  # 密码 bcrypt 哈希

# ============================================================
# 一、配置与公告加载
# ============================================================

def load_config():
    """
    加载 config.yml 配置文件。
    如果文件不存在，则从 defaults/default_config.yml 复制一份默认配置并退出，
    提示用户修改后再启动。
    """
    try:
        with open('config.yml', 'r', encoding='utf-8') as file:
            config = yaml.safe_load(file)
        return config
    except FileNotFoundError:
        with open('config.yml', 'w', encoding='utf-8') as file:
            with open('defaults/default_config.yml', 'r', encoding='utf-8') as default_file:
                default_config = default_file.read()
            file.write(default_config)
        print("❌ 错误：找不到 config.yml 文件，已重新生成默认配置，请根据文件内容进行修改。")
        exit(1)

# 全局配置对象（模块加载时立即初始化）
config = load_config()
SERVER_DIR = config['server']['server_directory']  # 服务器文件存放目录，如 "servers/"

def read_start_config(filepath):
    """
    读取 Minecraft 服务端启动配置文件 (start.txt)。
    文件格式（每行一个配置项）：
        第一行: Java 参数 (可选，如 -Xmx1024M)
        第二行: JAR 文件名 (必需，如 server.jar)
        第三行: nogui (可选，留空则不添加)
    返回: (jar_name, java_args, nogui) 元组，失败返回 None
    
    文件格式（三行）：
        第一行: Java 参数 (可选，如 -Xmx1024M)
        第二行: JAR 文件名 (必需)
        第三行: nogui (可选，留空则不添加)
    """

    try:
        with open(filepath, 'r', encoding='utf-8') as file:
            lines = file.readlines()

        java_args = lines[0].strip() if len(lines) > 0 else ''
        jar_name = lines[1].strip() if len(lines) > 1 else ''
        nogui = lines[2].strip() if len(lines) > 2 else ''

        if not jar_name:
            print(f"错误：启动配置文件 {filepath} 中 JAR 文件名为空")
            return None
        return (jar_name, java_args, nogui)
    except FileNotFoundError:
        print(f"错误：找不到文件 {filepath}")
        return None
    except Exception as e:
        print(f"错误：读取启动配置文件失败 - {e}")
        return None

ANNOUNCE_FILE = 'announcements.json'

def load_announcements():
    """
    加载 announcements.json 公告数据。
    如果文件不存在，从 defaults/default_announcements.json 复制默认公告模板。
    如果文件损坏，返回空列表。
    """
    if os.path.exists(ANNOUNCE_FILE):
        try:
            with open(ANNOUNCE_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            print(f"读取公告文件错误: {e}")
            return []
    else:
        with open(ANNOUNCE_FILE, 'w', encoding='utf-8') as file:
            with open('defaults/default_announcements.json', 'r', encoding='utf-8') as default_file:
                default_announcements = default_file.read()
            file.write(default_announcements)
        print("❌ 错误：找不到 announcements.json 文件，已重新生成默认配置，请根据文件内容进行修改。")
        return load_announcements()


# ============================================================
# 二、Minecraft 服务端进程管理
# ============================================================

# mc_process 列表：索引对应服务器 ID，每个元素是一个 subprocess.Popen 对象
# 例如 mc_process[1] 对应服务器 #1 的进程，mc_process[0] 保留给 Velocity 代理
mc_process = [None] * (config['server']['max_servers'] + 1)

# output_queues 字典：键为 server_id，值为 queue.Queue，用于线程安全地传递控制台日志
output_queues = {}

# server_pid 列表：存储每个服务器的进程 PID（当前未广泛使用）
server_pid = [None] * (config['server']['max_servers'] + 1)


def read_mc_output(server_id):
    """
    后台线程目标函数：持续读取 Minecraft 子进程的 stdout 输出，
    逐行放入 output_queues[server_id] 队列，供 Web 前端轮询消费。
    当进程结束时，在队列中放入一条关闭通知。
    """
    global mc_process
    if mc_process[server_id]:
        for line in mc_process[server_id].stdout:
            if line:
                decoded_line = line.strip()
                output_queues[server_id].put(decoded_line)
        # 进程结束标记
        output_queues[server_id].put("[系统] Minecraft 服务端已关闭。")


def start_server(server_id):
    """
    启动指定 ID 的 Minecraft 服务端。
    流程：
      1. 检查在线服务器数量是否达到上限
      2. 检查 start.txt 启动配置文件是否存在，缺失则生成默认
      3. 解析启动配置（JAR 名、Java 参数、nogui 标志）
      4. 自动配置 Velocity 代理相关设置（如启用）
      5. 确保 server.properties 端口与数据库一致
      6. 启动 Java 子进程并启动日志读取线程
      7. 同步后端服务器到 velocity.toml
    返回：成功返回 PID，失败返回错误描述字符串
    """
    global mc_process

    # 检查在线服务器数量上限
    max_online = config['server']['max_online_servers']
    running_count = sum(1 for p in mc_process if p and p.poll() is None)
    if running_count >= max_online:
        return f"已达到最大在线服务器数 ({max_online})，请先停止其他服务器。"

    if server_id >= len(mc_process):
        return f"服务器 ID {server_id} 无效（最大 ID: {len(mc_process)-1}）"

    if server_id not in output_queues:
        output_queues[server_id] = queue.Queue(maxsize=5000)
    if mc_process[server_id] and mc_process[server_id].poll() is None:
        return "服务端已经在运行中！"
    
    # 判断启动脚本是否存在
    if not os.path.exists(f"{SERVER_DIR}/{server_id}/start.txt"):
        with open(f"{SERVER_DIR}/{server_id}/start.txt", 'w', encoding='utf-8') as f:
            with open('defaults/default_start.txt', 'r', encoding='utf-8') as default_file:
                file=default_file.read()
            f.write(file)
        return f"启动脚本缺失，已生成默认 start.txt，请编辑后重新尝试启动服务器 {server_id}。"

    # 读取启动配置（参数 + JAR 名 + nogui）
    start_config = read_start_config(f"{SERVER_DIR}/{server_id}/start.txt")
    if start_config is None:
        return "启动配置文件读取失败，请检查 start.txt 格式"

    # 检查是否需要自动配置 Velocity 代理
    if is_velocity_enabled():
        info = get_server_info(server_id)
        if info and is_velocity_core(info.get('server_core', '')):
            auto_configure_velocity_props(server_id)

    # 确保 server.properties 端口正确
    ensure_server_properties(server_id)

    jar_name, java_args, nogui = start_config

    # 构建安全的启动命令：始终使用 java 作为可执行文件
    jar_path = os.path.join(os.getcwd(), SERVER_DIR, str(server_id), jar_name)
    if not os.path.isfile(jar_path):
        return f"找不到 JAR 文件: {jar_name}，请将服务端 JAR 上传到 {SERVER_DIR}/{server_id}/ 目录"

    cmd = ['java']
    if java_args:
        cmd.extend(java_args.split())
    cmd.extend(['-jar', jar_name])
    if nogui:
        cmd.append('nogui')

    try:
        # 启动 Java 子进程
        # - stdin=PIPE: 允许 Web 前端通过 API 向服务器发送指令
        # - stdout=PIPE: 捕获服务器控制台输出
        # - stderr=STDOUT: 错误输出合并到标准输出
        # - text=True: 以文本模式与子进程通信
        mc_process[server_id] = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=os.path.join(os.getcwd(), SERVER_DIR, str(server_id)),
            bufsize=1,
            text=True,
            encoding='utf-8'
        )

        # 启动后台线程：持续读取子进程输出到队列，供前端轮询
        thread = threading.Thread(target=read_mc_output, args=(server_id,), daemon=True)
        thread.start()

        # 如果启用了 Velocity 且核心支持，启动线程配置 paper-global.yml 的转发密钥
        if is_velocity_enabled():
            info = get_server_info(server_id)
            if info and is_velocity_core(info.get('server_core', '')):
                t = threading.Thread(target=_post_start_velocity_config, args=(server_id,), daemon=True)
                t.start()

        # 后台线程：等待 server.properties 出现后修正端口（防止被 Minecraft 默认覆盖）
        t = threading.Thread(target=_wait_and_fix_port, args=(server_id,), daemon=True)
        t.start()

        # 同步后端服务器列表至 velocity.toml 并通知 Velocity 重载
        if is_velocity_enabled():
            sync_velocity_toml_servers()
            if mc_process[0] and mc_process[0].poll() is None:
                try:
                    mc_process[0].stdin.write("velocity reload\n")
                    mc_process[0].stdin.flush()
                except Exception:
                    pass

        return mc_process[server_id].pid
    except Exception as e:
        return f"启动失败: {str(e)}"


def stop_server(server_id):
    """
    停止指定 ID 的 Minecraft 服务端。
    策略：先通过 stdin 发送 "stop" 指令优雅关闭，
    等待最多 10 秒，超时则强制 kill。
    """
    global mc_process
    if server_id >= len(mc_process) or mc_process[server_id] is None:
        return "服务端未运行"
    if mc_process[server_id].poll() is not None:
        mc_process[server_id] = None
        return "服务端已经处于停止状态"
    try:
        # 优雅关闭：通过标准输入发送 stop 指令
        mc_process[server_id].stdin.write("stop\n")
        mc_process[server_id].stdin.flush()
        mc_process[server_id].wait(timeout=10)
    except subprocess.TimeoutExpired:
        # 超时则强制终止进程
        mc_process[server_id].kill()
        mc_process[server_id].wait()
    except Exception:
        pass
    mc_process[server_id] = None
    return f"服务端 #{server_id} 已停止"


def restart_server(server_id):
    """
    重启指定 ID 的 Minecraft 服务端。
    先停止，再启动，返回启动结果。
    """
    msg = stop_server(server_id)
    if "已停止" in msg or "未运行" in msg:
        return start_server(server_id)
    return msg

# ============================================================
# 三、数据库层 — 统一接口，自动切换 SQLite / MySQL
# ============================================================

# 根据 config.yml 选择数据库类型并获取路径/连接参数
db_type = config['database']['type']
db_path = config['database']['sqlite']['users_db'] if db_type == 'sqlite' else None
servers_db_path = config['database']['sqlite']['servers_db'] if db_type == 'sqlite' else None

def _hash_password(password):
    """使用 Werkzeug 的 bcrypt 实现密码哈希，不可逆存储"""
    return generate_password_hash(password)


# ---------- SQLite 实现 ----------

def sqlite_ready(db_name=None):
    """
    初始化 SQLite 用户数据库。
    创建 users 表（id, username, password_hash, if_admin, server_limit, servers），
    兼容旧表结构自动添加缺失列，并确保默认 admin 账户存在。
    同时初始化 servers 数据库和 Velocity（如启用）。
    """
    if db_name is None:
        db_name = db_path
    conn = sqlite3.connect(db_name)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            if_admin INTEGER DEFAULT 0,
            server_limit INTEGER DEFAULT 0,
            servers TEXT
        )
    ''')
    # 兼容性处理：旧表可能缺少 server_limit 列
    try:
        cursor.execute("ALTER TABLE users ADD COLUMN server_limit INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass  # 列已存在，忽略错误
    # 确保默认管理员用户 admin 存在（默认密码 123456）
    if not cursor.execute("SELECT * FROM users WHERE username='admin'").fetchone():
        _add_user_sqlite(db_name, 'admin', '123456', if_admin=1)
    conn.commit()
    cursor.close()
    conn.close()

    _init_servers_db_sqlite()
    if is_velocity_enabled():
        init_velocity_server()


def _init_servers_db_sqlite():
    """
    创建/初始化 SQLite 服务器信息数据库 (servers.db)。
    表结构：server_id, server_name, path, 内存配置, 核心类型, 端口
    """
    conn = sqlite3.connect(servers_db_path)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS servers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            server_id INTEGER UNIQUE NOT NULL,
            server_name TEXT DEFAULT '',
            server_path TEXT DEFAULT '',
            max_memory INTEGER DEFAULT 0,
            min_memory INTEGER DEFAULT 0,
            server_core TEXT DEFAULT ''
        )
    ''')
    # 兼容旧表：添加 server_port 列
    try:
        cursor.execute("ALTER TABLE servers ADD COLUMN server_port INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    conn.commit()
    cursor.close()
    conn.close()


def _add_user_sqlite(db_name, username, password, if_admin=0):
    """SQLite 版：向 users 表插入新用户"""
    password_hash = _hash_password(password)
    conn = sqlite3.connect(db_name)
    cursor = conn.cursor()
    try:
        cursor.execute('''
            INSERT INTO users (username, password_hash, if_admin) VALUES (?, ?, ?)
        ''', (username, password_hash, if_admin))
        conn.commit()
        print(f"✅ 用户 '{username}' 已添加到数据库")
    except sqlite3.IntegrityError:
        print(f"⚠️ 用户 '{username}' 已存在，无法重复添加")
    finally:
        cursor.close()
        conn.close()

def _login_sqlite(db_name, username, password):
    """SQLite 版：验证用户名密码，返回布尔值"""
    conn = sqlite3.connect(db_name)
    cursor = conn.cursor()
    cursor.execute('SELECT password_hash FROM users WHERE username=?', (username,))
    result = cursor.fetchone()
    cursor.close()
    conn.close()
    return bool(result and check_password_hash(result[0], password))

# ---------- MySQL 实现 ----------

_db_user = config['database']['mysql']['user']
_db_pass = config['database']['mysql']['password']
_db_host = config['database']['mysql']['host']
_db_port = config['database']['mysql']['port']
_db_name_mysql = config['database']['mysql']['name']

def mysql_ready():
    """
    初始化 MySQL 数据库连接。
    自动创建数据库（如不存在）、建 users 表和 servers 表，
    确保默认 admin 账户存在，兼容旧表结构。
    """
    conn = pymysql.connect(host=_db_host, port=_db_port, user=_db_user, password=_db_pass)
    cursor = conn.cursor()
    cursor.execute(f"CREATE DATABASE IF NOT EXISTS {_db_name_mysql} DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci;")
    conn.select_db(_db_name_mysql)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INT AUTO_INCREMENT PRIMARY KEY,
            username VARCHAR(255) UNIQUE NOT NULL,
            password_hash VARCHAR(255) NOT NULL,
            if_admin TINYINT(1) DEFAULT 0,
            server_limit INT DEFAULT 0,
            servers TEXT
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    ''')
    try:
        cursor.execute("ALTER TABLE users ADD COLUMN server_limit INT DEFAULT 0")
    except Exception:
        pass
    cursor.execute("SELECT id FROM users WHERE username = %s", ('admin',))
    if not cursor.fetchone():
        _add_user_mysql('admin', '123456', if_admin=1)

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS servers (
            id INT AUTO_INCREMENT PRIMARY KEY,
            server_id INT UNIQUE NOT NULL,
            server_name VARCHAR(255) DEFAULT '',
            server_path TEXT DEFAULT '',
            max_memory INT DEFAULT 0,
            min_memory INT DEFAULT 0,
            server_core VARCHAR(255) DEFAULT ''
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    ''')
    try:
        cursor.execute("ALTER TABLE servers ADD COLUMN server_port INT DEFAULT 0")
    except Exception:
        pass
    conn.commit()
    cursor.close()
    conn.close()

    _init_servers_db_mysql()


def _init_servers_db_mysql():
    """MySQL 版：如果启用了 Velocity，初始化 Velocity 服务器"""
    if is_velocity_enabled():
        init_velocity_server()


def _add_user_mysql(username, password, if_admin=0):
    """MySQL 版：向 users 表插入新用户"""
    password_hash = _hash_password(password)
    conn = pymysql.connect(host=_db_host, port=_db_port, user=_db_user, password=_db_pass, database=_db_name_mysql)
    cursor = conn.cursor()
    try:
        cursor.execute("INSERT INTO users (username, password_hash, if_admin) VALUES (%s, %s, %s)",
                       (username, password_hash, if_admin))
        conn.commit()
        print(f"✅ 用户 '{username}' 已添加到数据库")
    except pymysql.err.IntegrityError:
        print(f"⚠️ 用户 '{username}' 已存在")
    finally:
        cursor.close()
        conn.close()


def _login_mysql(username, password):
    """MySQL 版：验证用户名密码，返回布尔值"""
    conn = pymysql.connect(host=_db_host, port=_db_port, user=_db_user, password=_db_pass, database=_db_name_mysql)
    cursor = conn.cursor()
    cursor.execute("SELECT password_hash FROM users WHERE username = %s", (username,))
    result = cursor.fetchone()
    cursor.close()
    conn.close()
    return bool(result and check_password_hash(result[0], password))


# ---------- 统一暴露的接口（自动路由到 SQLite 或 MySQL） ----------

def add_user(username, password, if_admin=0):
    """添加新用户（自动选择数据库类型）"""
    if db_type == 'sqlite':
        _add_user_sqlite(db_path, username, password, if_admin)
    elif db_type == 'mysql':
        _add_user_mysql(username, password, if_admin)

def login(username, password):
    """验证用户登录（自动选择数据库类型）"""
    if db_type == 'sqlite':
        return _login_sqlite(db_path, username, password)
    elif db_type == 'mysql':
        return _login_mysql(username, password)
    return False


# ============================================================
# 四、管理员数据库操作
# ============================================================

def check_admin(username):
    """
    检查用户是否为管理员。
    读取 users 表中的 if_admin 字段，返回 True/False。
    """
    try:
        if db_type == 'sqlite':
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()
            cursor.execute("SELECT if_admin FROM users WHERE username=?", (username,))
            row = cursor.fetchone()
            cursor.close()
            conn.close()
            return row is not None and row[0] == 1
        elif db_type == 'mysql':
            conn = pymysql.connect(host=_db_host, port=_db_port, user=_db_user, password=_db_pass, database=_db_name_mysql)
            cursor = conn.cursor()
            cursor.execute("SELECT if_admin FROM users WHERE username=%s", (username,))
            row = cursor.fetchone()
            cursor.close()
            conn.close()
            return row is not None and row[0] == 1
    except Exception:
        return False


def list_users():
    """获取所有用户列表，用于管理员后台展示"""
    try:
        if db_type == 'sqlite':
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()
            cursor.execute("SELECT id, username, if_admin, server_limit, servers FROM users ORDER BY id")
            rows = cursor.fetchall()
            cursor.close()
            conn.close()
            return [{'id': r[0], 'username': r[1], 'if_admin': bool(r[2]), 'server_limit': r[3] if r[3] is not None else 0, 'servers': r[4]} for r in rows]
        elif db_type == 'mysql':
            conn = pymysql.connect(host=_db_host, port=_db_port, user=_db_user, password=_db_pass, database=_db_name_mysql)
            cursor = conn.cursor()
            cursor.execute("SELECT id, username, if_admin, server_limit, servers FROM users ORDER BY id")
            rows = cursor.fetchall()
            cursor.close()
            conn.close()
            return [{'id': r[0], 'username': r[1], 'if_admin': bool(r[2]), 'server_limit': r[3] if r[3] is not None else 0, 'servers': r[4]} for r in rows]
    except Exception:
        return []


def set_admin_status(username, is_admin):
    """
    设置/取消用户的管理员权限。
    不能修改自己的管理员状态（由调用方 main.py 检查）。
    """
    val = 1 if is_admin else 0
    try:
        if db_type == 'sqlite':
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()
            cursor.execute("UPDATE users SET if_admin=? WHERE username=?", (val, username))
            conn.commit()
            cursor.close()
            conn.close()
            return True
        elif db_type == 'mysql':
            conn = pymysql.connect(host=_db_host, port=_db_port, user=_db_user, password=_db_pass, database=_db_name_mysql)
            cursor = conn.cursor()
            cursor.execute("UPDATE users SET if_admin=%s WHERE username=%s", (val, username))
            conn.commit()
            cursor.close()
            conn.close()
            return True
    except Exception:
        return False


def reset_password(username, new_password):
    """重置指定用户的密码（哈希后更新）"""
    ph = _hash_password(new_password)
    try:
        if db_type == 'sqlite':
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()
            cursor.execute("UPDATE users SET password_hash=? WHERE username=?", (ph, username))
            conn.commit()
            cursor.close()
            conn.close()
            return True
        elif db_type == 'mysql':
            conn = pymysql.connect(host=_db_host, port=_db_port, user=_db_user, password=_db_pass, database=_db_name_mysql)
            cursor = conn.cursor()
            cursor.execute("UPDATE users SET password_hash=%s WHERE username=%s", (ph, username))
            conn.commit()
            cursor.close()
            conn.close()
            return True
    except Exception:
        return False


def delete_user(username):
    """从数据库中删除指定用户"""
    try:
        if db_type == 'sqlite':
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()
            cursor.execute("DELETE FROM users WHERE username=?", (username,))
            conn.commit()
            affected = cursor.rowcount
            cursor.close()
            conn.close()
            return affected > 0
        elif db_type == 'mysql':
            conn = pymysql.connect(host=_db_host, port=_db_port, user=_db_user, password=_db_pass, database=_db_name_mysql)
            cursor = conn.cursor()
            cursor.execute("DELETE FROM users WHERE username=%s", (username,))
            conn.commit()
            affected = cursor.rowcount
            cursor.close()
            conn.close()
            return affected > 0
    except Exception:
        return False


# ============================================================
# 五、服务器所有权管理
# ============================================================
# 服务器的"所有权"通过 users 表中的 servers 字段（JSON 数组）实现。
# 例如：servers = "[1, 3, 5]" 表示该用户拥有服务器 #1、#3、#5。
# 管理员可以访问所有服务器（绕过所有权检查）。

def get_user_servers(username):
    """获取指定用户拥有的服务器 ID 列表（从 JSON 字段解析）"""
    import json
    try:
        if db_type == 'sqlite':
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()
            cursor.execute("SELECT servers FROM users WHERE username=?", (username,))
            row = cursor.fetchone()
            cursor.close()
            conn.close()
        elif db_type == 'mysql':
            conn = pymysql.connect(host=_db_host, port=_db_port, user=_db_user, password=_db_pass, database=_db_name_mysql)
            cursor = conn.cursor()
            cursor.execute("SELECT servers FROM users WHERE username=%s", (username,))
            row = cursor.fetchone()
            cursor.close()
            conn.close()
        else:
            return []
        if row and row[0]:
            return json.loads(row[0])
        return []
    except Exception:
        return []


def _save_user_servers(username, server_ids):
    """保存用户拥有的服务器 ID 列表"""
    import json
    data = json.dumps(server_ids)
    try:
        if db_type == 'sqlite':
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()
            cursor.execute("UPDATE users SET servers=? WHERE username=?", (data, username))
            conn.commit()
            cursor.close()
            conn.close()
        elif db_type == 'mysql':
            conn = pymysql.connect(host=_db_host, port=_db_port, user=_db_user, password=_db_pass, database=_db_name_mysql)
            cursor = conn.cursor()
            cursor.execute("UPDATE users SET servers=%s WHERE username=%s", (data, username))
            conn.commit()
            cursor.close()
            conn.close()
        return True
    except Exception:
        return False


def add_user_server(username, server_id):
    """为用户添加一个服务器 ID"""
    ids = get_user_servers(username)
    if server_id not in ids:
        ids.append(server_id)
    return _save_user_servers(username, ids)


def remove_user_server(username, server_id):
    """为用户移除一个服务器 ID"""
    ids = get_user_servers(username)
    if server_id in ids:
        ids.remove(server_id)
    return _save_user_servers(username, ids)


def check_server_owner(username, server_id):
    """
    检查用户是否为服务器所有者。
    管理员自动拥有所有服务器的访问权限。
    """
    if check_admin(username):
        return True
    ids = get_user_servers(username)
    return server_id in ids


def get_server_limit(username):
    """获取指定用户的开服上限（0 表示不可开服）"""
    try:
        if db_type == 'sqlite':
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()
            cursor.execute("SELECT server_limit FROM users WHERE username=?", (username,))
            row = cursor.fetchone()
            cursor.close()
            conn.close()
            return row[0] if row else 0
        elif db_type == 'mysql':
            conn = pymysql.connect(host=_db_host, port=_db_port, user=_db_user, password=_db_pass, database=_db_name_mysql)
            cursor = conn.cursor()
            cursor.execute("SELECT server_limit FROM users WHERE username=%s", (username,))
            row = cursor.fetchone()
            cursor.close()
            conn.close()
            return row[0] if row else 0
    except Exception:
        return 0


def set_server_limit(username, limit):
    """设置指定用户的开服上限（仅管理员可调用）"""
    try:
        if db_type == 'sqlite':
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()
            cursor.execute("UPDATE users SET server_limit=? WHERE username=?", (limit, username))
            conn.commit()
            cursor.close()
            conn.close()
            return True
        elif db_type == 'mysql':
            conn = pymysql.connect(host=_db_host, port=_db_port, user=_db_user, password=_db_pass, database=_db_name_mysql)
            cursor = conn.cursor()
            cursor.execute("UPDATE users SET server_limit=%s WHERE username=%s", (limit, username))
            conn.commit()
            cursor.close()
            conn.close()
            return True
    except Exception:
        return False


# ============================================================
# 六、Velocity 代理集成系统
# ============================================================
# Velocity 是一个高性能的 Minecraft 代理端，允许多个后端服务器
# 共享同一个入口地址。HiveMC 将其作为特殊的 ID=0 服务器管理。
# 功能包括：自动下载 velocity.jar、初始化目录结构、
# 自动配置后端 server.properties、同步 velocity.toml 等。

def is_velocity_enabled():
    """检查 config.yml 中是否启用了 Velocity 代理"""
    return config.get('velocity', {}).get('enable', False)


def get_velocity_port():
    """
    从 velocity.toml 中解析 Velocity 代理的监听端口。
    解析 bind 配置项（格式如 bind = "0.0.0.0:25565"），
    解析失败时返回默认端口 25565。
    """
    toml_path = os.path.join(SERVER_DIR, '0', 'velocity.toml')
    try:
        with open(toml_path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line.startswith('bind '):
                    parts = line.split('=')
                    if len(parts) >= 2:
                        val = parts[1].strip().strip('"').strip("'")
                        port_str = val.split(':')[-1]
                        return int(port_str)
    except (FileNotFoundError, ValueError, IndexError):
        pass
    return 25565  # 默认端口


_VELOCITY_CORES = {'paper', 'folia', 'leaves'}

def is_velocity_core(server_core):
    """
    检查服务端核心是否支持 Velocity 代理连接。
    目前支持：Paper、Folia、Leaves（需配置 proxy-protocol 和 forwarding.secret）
    """
    return server_core.strip().lower() in _VELOCITY_CORES if server_core else False


def init_velocity_server():
    """
    初始化 Velocity 代理服务器（ID=0）。
    执行步骤：
      1. 创建 servers/0/ 目录
      2. 从 PaperMC API 自动下载最新版 velocity.jar
      3. 创建默认 start.txt 启动配置
      4. 将 Velocity 信息写入 servers 数据库
      5. 将后端服务器列表同步到 velocity.toml
    """
    velocity_dir = os.path.join(SERVER_DIR, '0')
    os.makedirs(velocity_dir, exist_ok=True)

    download_velocity_jar(velocity_dir)

    start_txt = os.path.join(velocity_dir, 'start.txt')
    if not os.path.exists(start_txt):
        with open(start_txt, 'w', encoding='utf-8') as f:
            f.write("-Xms1G -Xmx1G -XX:+UseG1GC -XX:G1HeapRegionSize=4M -XX:+UnlockExperimentalVMOptions -XX:+ParallelRefProcEnabled -XX:+AlwaysPreTouch -XX:MaxInlineLevel=15\nvelocity.jar\nnogui\n")
    save_server_info(0, server_name='Velocity 代理', server_path=velocity_dir,
                     max_memory=512, min_memory=256, server_core='velocity', server_port=get_velocity_port())

    sync_velocity_toml_servers()


_VELOCITY_API = "https://fill.papermc.io/v3/projects/velocity"

def download_velocity_jar(target_dir):
    """
    从 PaperMC API v3 自动下载最新版 Velocity JAR 文件。
    如果目标目录已存在 velocity.jar 则跳过下载。
    下载流程：
      1. 请求 API 获取最新版本号和构建号
      2. 获取该构建的下载 URL
      3. 流式下载到临时文件（同时输出进度百分比）
      4. 下载完成后重命名为 velocity.jar
    """
    jar_path = os.path.join(target_dir, 'velocity.jar')
    if os.path.exists(jar_path):
        print("✅ velocity.jar 已存在，跳过下载")
        return True

    try:
        # 1. 获取所有版本，选最新的稳定版/SNAPSHOT
        print("📡 正在获取 Velocity 最新版本信息...")
        req = urllib.request.Request(f"{_VELOCITY_API}/versions", headers={'User-Agent': 'HiveMC/1.0'})
        resp = urllib.request.urlopen(req, timeout=15)
        versions_data = json.loads(resp.read().decode())

        # 取第一个（最新）版本
        latest = versions_data['versions'][0]
        version_id = latest['version']['id']
        builds = latest['builds']
        build_num = builds[-1]  # 最新构建号
        print(f"   → 最新版本: {version_id}, 构建 #{build_num}")

        # 2. 获取该构建的下载信息
        build_url = f"{_VELOCITY_API}/versions/{version_id}/builds/{build_num}"
        req = urllib.request.Request(build_url, headers={'User-Agent': 'HiveMC/1.0'})
        resp = urllib.request.urlopen(req, timeout=15)
        build_data = json.loads(resp.read().decode())

        download_info = build_data['downloads']['server:default']
        download_name = download_info['name']
        download_url = download_info['url']
        file_size = download_info.get('size', 0)
        print(f"   → 文件: {download_name}")

        # 3. 下载到临时文件
        print(f"📥 正在下载 {download_name} ...")
        req = urllib.request.Request(download_url, headers={'User-Agent': 'HiveMC/1.0'})
        resp = urllib.request.urlopen(req, timeout=120)

        tmp_path = jar_path + '.tmp'
        total = file_size or int(resp.headers.get('Content-Length', 0))
        received = 0
        with open(tmp_path, 'wb') as f:
            while True:
                chunk = resp.read(8192)
                if not chunk:
                    break
                f.write(chunk)
                received += len(chunk)
                if total > 0:
                    pct = int(received * 100 / total)
                    print(f"\r   → 下载中... {pct}% ({received//1024//1024}MB/{total//1024//1024}MB)", end='')
                else:
                    print(f"\r   → 下载中... {received//1024//1024}MB", end='')

        # 4. 重命名为 velocity.jar
        os.replace(tmp_path, jar_path)
        print(f"\n✅ velocity.jar 下载完成！({received//1024//1024}MB)")
        return True

    except urllib.error.HTTPError as e:
        print(f"❌ 下载失败 (HTTP {e.code})，请手动下载 velocity.jar 放入 {target_dir}")
        return False
    except (urllib.error.URLError, OSError) as e:
        print(f"❌ 网络错误: {e}，请手动下载 velocity.jar 放入 {target_dir}")
        return False
    except (json.JSONDecodeError, KeyError, IndexError) as e:
        print(f"❌ API 解析失败: {e}，请手动下载 velocity.jar 放入 {target_dir}")
        return False


def get_server_info(server_id):
    """
    从 servers 数据库获取指定服务器的详细信息。
    返回字典：{server_id, server_name, server_path, max_memory, min_memory, server_core, server_port}
    查询失败或不存在时返回 None。
    """
    try:
        if db_type == 'sqlite':
            conn = sqlite3.connect(servers_db_path)
            cursor = conn.cursor()
            cursor.execute("SELECT server_id, server_name, server_path, max_memory, min_memory, server_core, server_port FROM servers WHERE server_id=?", (server_id,))
            row = cursor.fetchone()
            cursor.close()
            conn.close()
        elif db_type == 'mysql':
            conn = pymysql.connect(host=_db_host, port=_db_port, user=_db_user, password=_db_pass, database=_db_name_mysql)
            cursor = conn.cursor()
            cursor.execute("SELECT server_id, server_name, server_path, max_memory, min_memory, server_core, server_port FROM servers WHERE server_id=%s", (server_id,))
            row = cursor.fetchone()
            cursor.close()
            conn.close()
        else:
            return None
        if row:
            return {
                'server_id': row[0],
                'server_name': row[1] or '',
                'server_path': row[2] or '',
                'max_memory': row[3] or 0,
                'min_memory': row[4] or 0,
                'server_core': row[5] or '',
                'server_port': row[6] or 0
            }
        return None
    except Exception:
        return None


def save_server_info(server_id, **kwargs):
    """
    保存/更新服务器信息到 servers 数据库。
    可保存的字段由 allowed 集合限定，忽略不在允许列表中的参数。
    如果记录已存在则 UPDATE，否则 INSERT。
    """
    allowed = {'server_name', 'server_path', 'max_memory', 'min_memory', 'server_core', 'server_port'}
    data = {k: v for k, v in kwargs.items() if k in allowed}
    if not data:
        return False
    try:
        if db_type == 'sqlite':
            conn = sqlite3.connect(servers_db_path)
            cursor = conn.cursor()
        elif db_type == 'mysql':
            conn = pymysql.connect(host=_db_host, port=_db_port, user=_db_user, password=_db_pass, database=_db_name_mysql)
            cursor = conn.cursor()
        else:
            return False

        cursor.execute("SELECT id FROM servers WHERE server_id=?" if db_type == 'sqlite' else "SELECT id FROM servers WHERE server_id=%s", (server_id,))
        exists = cursor.fetchone()

        if exists:
            set_clause = ', '.join([f"{k}=?" if db_type == 'sqlite' else f"{k}=%s" for k in data])
            values = list(data.values()) + [server_id]
            cursor.execute(f"UPDATE servers SET {set_clause} WHERE server_id=?" if db_type == 'sqlite' else f"UPDATE servers SET {set_clause} WHERE server_id=%s", values)
        else:
            keys_with_id = ', '.join(['server_id'] + list(data.keys()))
            ph = '?, ' + ', '.join(['?'] * len(data)) if db_type == 'sqlite' else '%s, ' + ', '.join(['%s'] * len(data))
            values = [server_id] + list(data.values())
            cursor.execute(f"INSERT INTO servers ({keys_with_id}) VALUES ({ph})", values)

        conn.commit()
        cursor.close()
        conn.close()
        return True
    except Exception:
        return False


def delete_server_info(server_id):
    """
    从 servers 数据库删除指定服务器的信息记录。
    注意：Velocity 代理服务器（ID=0）不可删除。
    """
    if server_id == 0:
        return False
    try:
        if db_type == 'sqlite':
            conn = sqlite3.connect(servers_db_path)
            cursor = conn.cursor()
            cursor.execute("DELETE FROM servers WHERE server_id=?", (server_id,))
            conn.commit()
            cursor.close()
            conn.close()
        elif db_type == 'mysql':
            conn = pymysql.connect(host=_db_host, port=_db_port, user=_db_user, password=_db_pass, database=_db_name_mysql)
            cursor = conn.cursor()
            cursor.execute("DELETE FROM servers WHERE server_id=%s", (server_id,))
            conn.commit()
            cursor.close()
            conn.close()
        return True
    except Exception:
        return False


def get_all_servers_info():
    """获取 servers 数据库中的所有服务器信息列表（按 server_id 排序）"""
    try:
        if db_type == 'sqlite':
            conn = sqlite3.connect(servers_db_path)
            cursor = conn.cursor()
            cursor.execute("SELECT server_id, server_name, server_path, max_memory, min_memory, server_core, server_port FROM servers ORDER BY server_id")
            rows = cursor.fetchall()
            cursor.close()
            conn.close()
        elif db_type == 'mysql':
            conn = pymysql.connect(host=_db_host, port=_db_port, user=_db_user, password=_db_pass, database=_db_name_mysql)
            cursor = conn.cursor()
            cursor.execute("SELECT server_id, server_name, server_path, max_memory, min_memory, server_core, server_port FROM servers ORDER BY server_id")
            rows = cursor.fetchall()
            cursor.close()
            conn.close()
        else:
            return []
        return [{
            'server_id': r[0],
            'server_name': r[1] or '',
            'server_path': r[2] or '',
            'max_memory': r[3] or 0,
            'min_memory': r[4] or 0,
            'server_core': r[5] or '',
            'server_port': r[6] or 0
        } for r in rows]
    except Exception:
        return []


def ensure_server_properties(server_id):
    """检查 server.properties 中 server-port 是否与数据库一致，不一致则修正。"""
    info = get_server_info(server_id)
    if not info:
        # 旧服务器在 DB 中没有记录，自动创建
        port = 30000 + server_id
        save_server_info(server_id, server_port=port)
        print(f"✅ 已为服务器 #{server_id} 创建数据库记录，端口 {port}")
    else:
        port = info.get('server_port')
        if not port:
            port = 30000 + server_id
            save_server_info(server_id, server_port=port)
            print(f"✅ 已为服务器 #{server_id} 分配端口 {port}")
    port = str(port)

    server_dir = os.path.join(os.getcwd(), SERVER_DIR, str(server_id))
    props_path = os.path.join(server_dir, 'server.properties')

    if not os.path.exists(props_path):
        # 文件不存在→主动创建（否则 Minecraft 默认 25565 启动后改已无效）
        with open(props_path, 'w', encoding='utf-8') as f:
            f.write(f"server-port={port}\n")
            f.write("online-mode=false\n")
            f.write("motd=A HiveMC Server\n")
        print(f"✅ 已为服务器 #{server_id} 创建 server.properties，端口 {port}")
        return

    with open(props_path, 'r', encoding='utf-8') as f:
        lines = f.readlines()

    new_lines = []
    changed = False
    port_found = False
    for line in lines:
        if line.startswith('server-port='):
            old = line.strip().split('=', 1)[1]
            if old != port:
                new_lines.append(f'server-port={port}\n')
                changed = True
            else:
                new_lines.append(line)
            port_found = True
        else:
            new_lines.append(line)
    if not port_found:
        new_lines.append(f'server-port={port}\n')
        changed = True

    if changed:
        with open(props_path, 'w', encoding='utf-8') as f:
            f.writelines(new_lines)


def _wait_and_fix_port(server_id):
    """
    后台线程目标函数：确保 server.properties 中的端口与数据库一致。
    在 Minecraft 启动前主动创建/修正端口，并在启动后再确认一次，
    防止 Minecraft 使用默认端口（25565）覆盖掉自定义设置。
    最大等待 60 秒。
    """
    import time
    server_dir = os.path.join(os.getcwd(), SERVER_DIR, str(server_id))
    props_path = os.path.join(server_dir, 'server.properties')

    # 不等 Minecraft 生成，主动写入正确的端口
    ensure_server_properties(server_id)

    # 等 Minecraft 完全启动后再确认一次
    for _ in range(120):
        if os.path.exists(props_path):
            ensure_server_properties(server_id)
            return
        if mc_process[server_id] and mc_process[server_id].poll() is not None:
            return
        time.sleep(0.5)


def auto_configure_velocity_props(server_id):
    """
    为 Paper/Folia/Leaves 服务端自动配置与 Velocity 代理兼容的 server.properties。
    关键修改：
      - online-mode = false（Velocity 代理模式下必须关闭正版验证）
      - proxy-protocol = true（启用代理协议以传递真实客户端 IP）
    """
    ensure_server_properties(server_id)

    server_dir = os.path.join(os.getcwd(), SERVER_DIR, str(server_id))
    props_path = os.path.join(server_dir, 'server.properties')
    if not os.path.exists(props_path):
        return

    modified = False
    props = {}
    with open(props_path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.rstrip('\n')
            if '=' in line and not line.startswith('#'):
                key, val = line.split('=', 1)
                props[key.strip()] = val.strip()

    if props.get('online-mode', 'true') != 'false':
        props['online-mode'] = 'false'
        modified = True
    if props.get('proxy-protocol', 'false') != 'true':
        props['proxy-protocol'] = 'true'
        modified = True

    if modified:
        with open(props_path, 'w', encoding='utf-8') as f:
            for key, val in props.items():
                f.write(f"{key}={val}\n")
        print(f"✅ 已为服务器 #{server_id} 配置 server.properties（Velocity 代理模式）")


def _post_start_velocity_config(server_id):
    """
    后台线程目标函数：在 Paper/Folia/Leaves 首次启动后，
    等待 forwarding.secret 文件生成，然后自动将密钥写入
    config/paper-global.yml 的 proxies.velocity.secret 字段，
    使后端服务器与 Velocity 代理之间的通信加密正常运作。
    最长等待 60 秒。
    """
    import time
    server_dir = os.path.join(os.getcwd(), SERVER_DIR, str(server_id))
    forwarding_secret_path = os.path.join(server_dir, 'forwarding.secret')
    paper_global_path = os.path.join(server_dir, 'config', 'paper-global.yml')

    # 如果 forwarding.secret 已存在则不用等
    if os.path.exists(forwarding_secret_path):
        pass  # 直接往下走
    else:
        # 等 Paper 首次启动生成该文件（最长 60 秒）
        for _ in range(120):
            if os.path.exists(forwarding_secret_path):
                break
            # 检查进程是否已退出
            if mc_process[server_id] and mc_process[server_id].poll() is not None:
                return
            time.sleep(0.5)
        else:
            return  # 超时未生成

    if not os.path.exists(paper_global_path):
        return

    with open(forwarding_secret_path, 'r', encoding='utf-8') as f:
        secret = f.read().strip()
    if not secret:
        return

    try:
        with open(paper_global_path, 'r', encoding='utf-8') as f:
            yaml_data = yaml.safe_load(f) or {}

        if 'proxies' not in yaml_data:
            yaml_data['proxies'] = {}
        if 'velocity' not in yaml_data['proxies']:
            yaml_data['proxies']['velocity'] = {}
        yaml_data['proxies']['velocity']['enabled'] = True
        yaml_data['proxies']['velocity'].pop('secret', None)  # 移除旧值
        yaml_data['proxies']['velocity']['secret'] = secret

        with open(paper_global_path, 'w', encoding='utf-8') as f:
            yaml.dump(yaml_data, f, default_flow_style=False, allow_unicode=True)

        print(f"✅ 已为服务器 #{server_id} 写入 paper-global.yml Velocity 转发密钥")
    except Exception as e:
        print(f"⚠️ 写入 paper-global.yml 失败: {e}")


def sync_velocity_toml_servers():
    """
    扫描所有普通服务器（ID > 0），自动更新 velocity.toml 配置文件。
    操作：
      1. 收集所有后端服务器的内部地址（s1=127.0.0.1:30001, ...）
      2. 移除 velocity.toml 中旧的 [servers] 段和 try 列表
      3. 插入新的 [servers] 段和 try 列表
      4. 插入到 [forced-hosts] 或 [advanced] 配置段之前
    每次创建/删除服务器时自动调用此函数以保持配置同步。
    """
    if not is_velocity_enabled():
        return
    toml_path = os.path.join(SERVER_DIR, '0', 'velocity.toml')
    if not os.path.exists(toml_path):
        return

    # 收集所有普通服务器的名称和端口
    server_names = []
    servers_dir = os.path.join(SERVER_DIR)
    if os.path.isdir(servers_dir):
        for entry in sorted(os.listdir(servers_dir)):
            if not entry.isdigit():
                continue
            sid = int(entry)
            if sid == 0:
                continue
            info = get_server_info(sid)
            if not info:
                continue
            port = info.get('server_port') or (30000 + sid)
            sname = f"s{sid}"
            server_names.append((sname, port))

    if not server_names:
        return

    with open(toml_path, 'r', encoding='utf-8') as f:
        content = f.read()

    # 去掉所有现有 [servers] 段（含内容）和 try = [...] 块
    import re
    # 移除所有 [servers] 段（从 [servers] 到下一个 [ 或文件结尾）
    content = re.sub(r'\n?\[servers\].*?(?=\n\[|$)', '', content, flags=re.DOTALL)
    # 移除所有 try = [...] 块（多行）
    content = re.sub(r'\n?try\s*=\s*\[.*?\]', '', content, flags=re.DOTALL)

    # 生成新的 [servers] 段
    servers_block = '\n[servers]\n'
    for sname, port in server_names:
        servers_block += f'{sname} = "127.0.0.1:{port}"\n'

    try_block = 'try = [\n'
    for sname, _ in server_names:
        try_block += f'    "{sname}",\n'
    try_block += ']\n'

    new_section = servers_block + '\n' + try_block

    # 插入到 [forced-hosts] 前，如果没有则追加到末尾
    insert_pos = content.find('\n[forced-hosts]')
    if insert_pos == -1:
        insert_pos = content.find('\n[advanced]')
    if insert_pos == -1:
        content += new_section
    else:
        content = content[:insert_pos] + new_section + content[insert_pos:]

    with open(toml_path, 'w', encoding='utf-8') as f:
        f.write(content)
    print(f"✅ velocity.toml 已同步 {len(server_names)} 个后端服务器: {', '.join(s for s, _ in server_names)}")