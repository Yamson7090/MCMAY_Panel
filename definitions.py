import yaml
import sqlite3
import pymysql
import json
import os
import subprocess
import threading
import queue
from werkzeug.security import generate_password_hash, check_password_hash

def load_config():
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

config = load_config()
SERVER_DIR = config['server']['server_directory']

def read_start_config(filepath):
    """读取启动配置文件，返回 (jar_name, java_args, nogui) 元组。
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
    """加载公告数据"""
    if os.path.exists(ANNOUNCE_FILE):
        try:
            with open(ANNOUNCE_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            print(f"读取公告文件错误: {e}")
            # 如果文件损坏或为空，返回一个空列表
            return []
    else:
        # 如果文件不存在，返回一个示例公告（或者空列表）
        with open(ANNOUNCE_FILE, 'w', encoding='utf-8') as file:
            with open('defaults/default_announcements.json', 'r', encoding='utf-8') as default_file:
                default_announcements = default_file.read()
            file.write(default_announcements)
        print("❌ 错误：找不到 announcements.json 文件，已重新生成默认配置，请根据文件内容进行修改。")
        return load_announcements()


# --- 全局变量 ---
mc_process = [None] * (config['server']['max_servers']+1) # 用于存储每个服务器的进程对象，索引对应服务器ID，0号位未使用
output_queues = {}
server_pid = [None] * (config['server']['max_servers']+1) # 存储每个服务器的PID，索引对应服务器ID，0号位未使用

def read_mc_output(server_id):
    """后台线程：持续读取 Minecraft 的输出并放入队列"""
    global mc_process
    if mc_process[server_id]:
        # 逐行读取标准输出
        for line in mc_process[server_id].stdout:
            if line:
                decoded_line = line.strip()
                output_queues[server_id].put(decoded_line)
        
        # 进程结束后的处理
        output_queues[server_id].put("[系统] Minecraft 服务端已关闭。")

def start_server(server_id):
    """启动 Minecraft 服务端"""
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
        # 启动进程，捕获 stdout 和 stdin
        # text=True 表示以文本模式运行，方便处理字符串
        mc_process[server_id] = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, # 将错误输出也合并到标准输出
            cwd=os.path.join(os.getcwd(), SERVER_DIR, str(server_id)),
            bufsize=1,
            text=True,
            encoding='utf-8'
        )
        
        # 启动读取线程
        thread = threading.Thread(target=read_mc_output, args=(server_id,), daemon=True)
        thread.start()
        return mc_process[server_id].pid
    except Exception as e:
        return f"启动失败: {str(e)}"


def stop_server(server_id):
    """停止 Minecraft 服务端"""
    global mc_process
    if server_id >= len(mc_process) or mc_process[server_id] is None:
        return "服务端未运行"
    if mc_process[server_id].poll() is not None:
        mc_process[server_id] = None
        return "服务端已经处于停止状态"
    try:
        # 先尝试优雅关闭
        mc_process[server_id].stdin.write("stop\n")
        mc_process[server_id].stdin.flush()
        # 等待最多 10 秒
        mc_process[server_id].wait(timeout=10)
    except subprocess.TimeoutExpired:
        # 超时则强制杀死
        mc_process[server_id].kill()
        mc_process[server_id].wait()
    except Exception:
        pass
    mc_process[server_id] = None
    return f"服务端 #{server_id} 已停止"


def restart_server(server_id):
    """重启 Minecraft 服务端"""
    msg = stop_server(server_id)
    if "已停止" in msg or "未运行" in msg:
        return start_server(server_id)
    return msg

# ---- 数据库层 ----
# 统一接口，根据配置自动切换 SQLite / MySQL

db_type = config['database']['type']
db_path = config['database']['sqlite']['users_db'] if db_type == 'sqlite' else None
servers_db_path = config['database']['sqlite']['servers_db'] if db_type == 'sqlite' else None

def _hash_password(password):
    return generate_password_hash(password)

# ---------- SQLite ----------
def sqlite_ready(db_name=None):
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
    # 兼容旧表：添加 server_limit 列（如果不存在）
    try:
        cursor.execute("ALTER TABLE users ADD COLUMN server_limit INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass  # 列已存在
    if not cursor.execute("SELECT * FROM users WHERE username='admin'").fetchone():
        _add_user_sqlite(db_name, 'admin', '123456', if_admin=1)
    conn.commit()
    cursor.close()
    conn.close()

    # 初始化 servers 数据库
    _init_servers_db_sqlite()


def _init_servers_db_sqlite():
    """创建/初始化服务器信息数据库 (servers.db)"""
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
    conn.commit()
    cursor.close()
    conn.close()


def _add_user_sqlite(db_name, username, password, if_admin=0):
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
    conn = sqlite3.connect(db_name)
    cursor = conn.cursor()
    cursor.execute('SELECT password_hash FROM users WHERE username=?', (username,))
    result = cursor.fetchone()
    cursor.close()
    conn.close()
    return bool(result and check_password_hash(result[0], password))

# ---------- MySQL ----------
_db_user = config['database']['mysql']['user']
_db_pass = config['database']['mysql']['password']
_db_host = config['database']['mysql']['host']
_db_port = config['database']['mysql']['port']
_db_name_mysql = config['database']['mysql']['name']

def mysql_ready():
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
    # 兼容旧表
    try:
        cursor.execute("ALTER TABLE users ADD COLUMN server_limit INT DEFAULT 0")
    except Exception:
        pass
    cursor.execute("SELECT id FROM users WHERE username = %s", ('admin',))
    if not cursor.fetchone():
        _add_user_mysql('admin', '123456', if_admin=1)

    # 创建 servers 表
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

    cursor.close()
    conn.close()

def _add_user_mysql(username, password, if_admin=0):
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
    conn = pymysql.connect(host=_db_host, port=_db_port, user=_db_user, password=_db_pass, database=_db_name_mysql)
    cursor = conn.cursor()
    cursor.execute("SELECT password_hash FROM users WHERE username = %s", (username,))
    result = cursor.fetchone()
    cursor.close()
    conn.close()
    return bool(result and check_password_hash(result[0], password))

# ---------- 统一暴露的接口 ----------

def add_user(username, password, if_admin=0):
    if db_type == 'sqlite':
        _add_user_sqlite(db_path, username, password, if_admin)
    elif db_type == 'mysql':
        _add_user_mysql(username, password, if_admin)

def login(username, password):
    if db_type == 'sqlite':
        return _login_sqlite(db_path, username, password)
    elif db_type == 'mysql':
        return _login_mysql(username, password)
    return False


# ========== 管理员数据库操作 ==========

def check_admin(username):
    """检查用户是否为管理员"""
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
    """列出所有用户"""
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
    """设置用户管理员状态"""
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
    """重置用户密码"""
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
    """删除用户"""
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


# ========== 服务器所有权 ==========

def get_user_servers(username):
    """获取用户拥有的服务器 ID 列表"""
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
    """检查用户是否为服务器所有者"""
    if check_admin(username):
        return True  # 管理员可访问所有服务器
    ids = get_user_servers(username)
    return server_id in ids

def get_server_limit(username):
    """获取用户的开服上限"""
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
    """设置用户的开服上限"""
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