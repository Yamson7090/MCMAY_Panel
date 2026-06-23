import yaml
import sqlite3
import pymysql
import json
import os
import subprocess
import threading
import queue
import urllib.request
import urllib.error
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
# 防止重复初始化 Velocity（避免重复下载与重复同步输出）
_VELOCITY_INIT_DONE = False

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

        # 启动首次启动配置线程（Velocity 代理配置）
        if is_velocity_enabled():
            info = get_server_info(server_id)
            if info and is_velocity_core(info.get('server_core', '')):
                t = threading.Thread(target=_post_start_velocity_config, args=(server_id,), daemon=True)
                t.start()

        # 后台线程：等 server.properties 出现后修正端口
        t = threading.Thread(target=_wait_and_fix_port, args=(server_id,), daemon=True)
        t.start()

        # 同步后端服务器到 velocity.toml
        if is_velocity_enabled():
            sync_velocity_toml_servers()
            # 通知 Velocity 重载配置
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

    # 初始化 servers 数据库并自动创建 Velocity 服务器
    _init_servers_db_sqlite()
    if is_velocity_enabled():
        init_velocity_server()


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
    # 兼容旧表：添加 server_port 列
    try:
        cursor.execute("ALTER TABLE servers ADD COLUMN server_port INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass
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
    # 兼容旧表：添加 server_port 列
    try:
        cursor.execute("ALTER TABLE servers ADD COLUMN server_port INT DEFAULT 0")
    except Exception:
        pass
    conn.commit()
    cursor.close()
    conn.close()

    _init_servers_db_mysql()


def _init_servers_db_mysql():
    """MySQL 版：创建 servers 表中的 Velocity 条目"""
    if is_velocity_enabled():
        init_velocity_server()

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


# ========== Velocity 服务器系统 ==========

def is_velocity_enabled():
    """检查是否启用了 Velocity 代理"""
    return config.get('velocity', {}).get('enable', False)


def get_velocity_port():
    """从 velocity.toml 解析 Velocity 代理端口"""
    toml_path = os.path.join(SERVER_DIR, '0', 'velocity.toml')
    try:
        with open(toml_path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line.startswith('bind '):
                    # bind = "0.0.0.0:25565"
                    parts = line.split('=')
                    if len(parts) >= 2:
                        val = parts[1].strip().strip('"').strip("'")
                        port_str = val.split(':')[-1]
                        return int(port_str)
    except (FileNotFoundError, ValueError, IndexError):
        pass
    return 25565  # fallback


_VELOCITY_CORES = {'paper', 'folia', 'leaves'}

def is_velocity_core(server_core):
    """检查服务端核心是否支持 Velocity 代理连接"""
    return server_core.strip().lower() in _VELOCITY_CORES if server_core else False


def init_velocity_server():
    """初始化 Velocity 服务器（ID=0）的目录结构和数据库记录"""
    global _VELOCITY_INIT_DONE
    if _VELOCITY_INIT_DONE:
        return
    _VELOCITY_INIT_DONE = True
    velocity_dir = os.path.join(SERVER_DIR, '0')
    os.makedirs(velocity_dir, exist_ok=True)

    # 自动下载 velocity.jar
    download_velocity_jar(velocity_dir)

    start_txt = os.path.join(velocity_dir, 'start.txt')
    if not os.path.exists(start_txt):
        with open(start_txt, 'w', encoding='utf-8') as f:
            f.write("-Xms1G -Xmx1G -XX:+UseG1GC -XX:G1HeapRegionSize=4M -XX:+UnlockExperimentalVMOptions -XX:+ParallelRefProcEnabled -XX:+AlwaysPreTouch -XX:MaxInlineLevel=15\nvelocity.jar\nnogui\n")
    save_server_info(0, server_name='Velocity 代理', server_path=velocity_dir,
                     max_memory=512, min_memory=256, server_core='velocity', server_port=get_velocity_port())

    # 同步后端服务器到 velocity.toml
    sync_velocity_toml_servers()


_VELOCITY_API = "https://fill.papermc.io/v3/projects/velocity"

def download_velocity_jar(target_dir):
    """从 PaperMC API (v3) 自动下载最新版 Velocity JAR"""
    jar_path = os.path.join(target_dir, 'velocity.jar')
    if os.path.exists(jar_path):
        print("velocity.jar 已存在，跳过下载")
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
        print(f"正在下载 {download_name} ...")
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
        print(f"\nvelocity.jar 下载完成！({received//1024//1024}MB)")
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
    """从 servers 数据库获取服务器信息"""
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
    """保存/更新服务器信息到 servers 数据库"""
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
    """从 servers 数据库删除服务器信息（Velocity 服务器不可删除）"""
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
        if config.get('velocity', {}).get('enable', False):
            sync_velocity_toml_servers()
        return True
    except Exception:
        return False


def get_all_servers_info():
    """获取所有服务器信息"""
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
        print(f"已为服务器 #{server_id} 创建数据库记录，端口 {port}")
    else:
        port = info.get('server_port')
        if not port:
            port = 30000 + server_id
            save_server_info(server_id, server_port=port)
            print(f"已为服务器 #{server_id} 分配端口 {port}")
    port = str(port)

    server_dir = os.path.join(os.getcwd(), SERVER_DIR, str(server_id))
    props_path = os.path.join(server_dir, 'server.properties')

    if not os.path.exists(props_path):
        # 文件不存在→主动创建（否则 Minecraft 默认 25565 启动后改已无效）
        with open(props_path, 'w', encoding='utf-8') as f:
            f.write(f"server-port={port}\n")
            f.write("online-mode=false\n")
            f.write("motd=A HiveMC Server\n")
        print(f"已为服务器 #{server_id} 创建 server.properties，端口 {port}")
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
    """后台线程：确保 server.properties 端口正确"""
    import time
    server_dir = os.path.join(os.getcwd(), SERVER_DIR, str(server_id))
    props_path = os.path.join(server_dir, 'server.properties')

    # 主动创建/修正 server.properties（不等 Minecraft 生成）
    ensure_server_properties(server_id)

    # 等 Minecraft 完全启动后再确认一次（防止被覆盖）
    for _ in range(120):
        if os.path.exists(props_path):
            ensure_server_properties(server_id)
            return
        if mc_process[server_id] and mc_process[server_id].poll() is not None:
            return
        time.sleep(0.5)


def auto_configure_velocity_props(server_id):
    """为 Paper/Folia/Leaves 服务端自动配置 Velocity 代理连接"""
    # 先确保 server.properties 存在且端口正确
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

    # Velocity 代理配置
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
        print(f"已为服务器 #{server_id} 配置 server.properties")


def _post_start_velocity_config(server_id):
    """后台线程：等待 Paper/Folia/Leaves 生成 forwarding.secret，自动配置 paper-global.yml"""
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
    """扫描所有普通服务器，自动更新 velocity.toml 的 [servers] 和 try 列表"""
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

    # 去掉所有现有 [servers] 段（含内容）；尽量保留原有 try = [...] 块，除非配置要求重建它
    import re
    # 先尝试提取现有的 try 块以便在需要时恢复（避免被后续对 [servers] 的删除吞掉）
    preserved_try = None
    try_match = re.search(r'\n?try\s*=\s*\[.*?\]', content, flags=re.DOTALL)
    if try_match:
        preserved_try = try_match.group(0)

    # 移除所有 [servers] 段（从 [servers] 到下一个 [ 或文件结尾）
    content = re.sub(r'\n?\[servers\].*?(?=\n\[|$)', '', content, flags=re.DOTALL)

    # 如果配置允许自动设置 try，则移除所有原有 try 块（将用新生成的 try 替换）
    if config.get('velocity', {}).get('auto_setup_server_try', True):
        content = re.sub(r'\n?try\s*=\s*\[.*?\]', '', content, flags=re.DOTALL)

    # 生成新的 [servers] 段
    servers_block = '\n[servers]\n'
    for sname, port in server_names:
        servers_block += f'{sname} = "127.0.0.1:{port}"\n'

    new_section = servers_block
    if config.get('velocity', {}).get('auto_setup_server_try', True):
        try_block = 'try = [\n'
        for sname, _ in server_names:
            try_block += f'    "{sname}",\n'
        try_block += ']\n'
        new_section += '\n' + try_block
    else:
        # 如果用户选择不自动重建 try，则恢复保留的 try 块（若存在）
        if preserved_try:
            if not preserved_try.startswith('\n'):
                new_section += '\n' + preserved_try
            else:
                new_section += preserved_try

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