from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify, send_file
from datetime import datetime
from functools import wraps
import queue
import os
import io
import zipfile

# import definitions
from definitions import load_config, load_announcements, start_server, stop_server, restart_server, output_queues, mc_process
from definitions import sqlite_ready, mysql_ready, login, add_user
from definitions import check_admin, list_users, set_admin_status, reset_password, delete_user
from definitions import get_server_limit, set_server_limit
from definitions import get_user_servers, add_user_server, remove_user_server, check_server_owner
from definitions import SERVER_DIR
from definitions import is_velocity_enabled, init_velocity_server, get_server_info, save_server_info, delete_server_info, get_all_servers_info, ensure_server_properties, get_velocity_port, sync_velocity_toml_servers

# 读取配置文件
config = load_config()

if config['database']['type'] == 'sqlite':
    sqlite_ready()
elif config['database']['type'] == 'mysql':
    mysql_ready()
else:
    print("❌ 错误：不支持的数据库类型，请检查配置文件中的 database.type 设置。")
    exit(1)

# 初始化 Velocity 服务器（如启用）
if config.get('velocity', {}).get('enable', False):
    init_velocity_server()

app = Flask(__name__)
app.secret_key = config['server']['secret_key']
server_port = config['server']['port']
app.config['MAX_CONTENT_LENGTH'] = 1 * 1024 * 1024 * 1024  # 1GB 上传限制

# ---- 登录验证装饰器 ----
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if session.get('username') is None:
            flash('请先登录', 'error')
            return redirect(url_for('login_page'))
        return f(*args, **kwargs)
    return decorated

def json_login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if session.get('username') is None:
            return jsonify({'status': 'error', 'msg': '请先登录'}), 401
        return f(*args, **kwargs)
    return decorated


# ---- 管理员验证装饰器 ----
def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        username = session.get('username')
        if username is None:
            flash('请先登录', 'error')
            return redirect(url_for('login_page'))
        if not check_admin(username):
            flash('无管理员权限', 'error')
            return redirect(url_for('backend'))
        return f(*args, **kwargs)
    return decorated


def json_admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        username = session.get('username')
        if username is None:
            return jsonify({'status': 'error', 'msg': '请先登录'}), 401
        if not check_admin(username):
            return jsonify({'status': 'error', 'msg': '无管理员权限'}), 403
        return f(*args, **kwargs)
    return decorated

# ---- 辅助：取当前用户 ----
def current_user():
    return {'username': session['username']}


# ---- 服务器所有权校验 ----
def require_server_owner(server_id):
    """检查当前用户是否有权操作此服务器，无权限则返回错误响应"""
    username = session.get('username')
    if not username:
        return jsonify({'status': 'error', 'msg': '请先登录'}), 401
    if not check_server_owner(username, server_id):
        return jsonify({'status': 'error', 'msg': '无权操作此服务器'}), 403
    return None

@app.route("/")
def index():
    # 检查是否登录
    current_user = session.get('username')
    if current_user is not None:
        flash(f"欢迎回来，{current_user}！", 'success')
    else:
        flash("欢迎访问 Minecraft 服务器控制面板！请登录以管理您的服务器。", 'info')
    
    '''
    # 模拟服务器列表
    active_servers = [
        {"name": "阿明的生存服", "owner": "阿明", "status": "running", "server_id": "1"},
        {"name": "PVP 竞技场", "owner": "大神K", "status": "stopped", "server_id": "2"},
    ]'''
    return render_template('index.html', user=current_user, info=None)

@app.route('/login', methods=['GET', 'POST'])
def login_page():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        
        if login(username, password):
            session['username'] = username
            flash('登录成功！', 'success')
            return redirect(url_for('backend'))
        else:
            flash('用户名或密码错误，请重试', 'error')
            
    return render_template('login.html')

@app.route('/register', methods=['GET', 'POST'])
def register_page():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        confirm_password = request.form.get('confirm_password')

        # 验证输入
        if not username or not password:
            flash('用户名和密码不能为空', 'error')
            return render_template('register.html')

        if password != confirm_password:
            flash('两次输入的密码不一致', 'error')
            return render_template('register.html')

        if len(password) < 6:
            flash('密码长度至少为6位', 'error')
            return render_template('register.html')

        # 添加用户到数据库
        try:
            add_user(username, password)
            flash('注册成功！请登录', 'success')
            return redirect(url_for('login_page'))
        except Exception as e:
            flash('注册失败，用户名可能已存在', 'error')
            return render_template('register.html')

    return render_template('register.html')

@app.route('/backend')
@login_required
def backend():
    user = current_user()
    username = session['username']
    is_admin = check_admin(username)
    # 获取用户的开服上限和当前自己的服务器数量
    server_limit = get_server_limit(username)
    user_server_ids = get_user_servers(username)
    server_count = len(user_server_ids)
    # 公告
    announcements = load_announcements()[:3]

    return render_template('backend.html', user=user, announcements=announcements, user_servers=[], is_admin=is_admin,
                           server_limit=server_limit, server_count=server_count)

@app.route('/logout')
def logout():
    session.pop('username', None)
    flash('已退出登录', 'info')
    return redirect(url_for('index'))

@app.route("/status")
def status():
    return "Server is running!"

@app.route('/console', methods=['GET'])
@login_required
def console_page():
    user = current_user()
    server_id = int(request.args.get('server_id'))
    err = require_server_owner(server_id)
    if err:
        flash('无权操作此服务器', 'error')
        return redirect(url_for('backend'))
    return render_template('console.html', server_id=server_id, user=user)

@app.route('/api/start', methods=['POST'])
@json_login_required
def api_start():
    server_id = int(request.json.get('server_id'))
    err = require_server_owner(server_id)
    if err: return err
    # 启动服务端接口
    msg = start_server(server_id=server_id)
    return jsonify({'status': 'success', 'msg': msg})

@app.route('/api/stop', methods=['POST'])
@json_login_required
def api_stop():
    server_id = int(request.json.get('server_id'))
    err = require_server_owner(server_id)
    if err: return err
    msg = stop_server(server_id)
    return jsonify({'status': 'success', 'msg': msg})

@app.route('/api/restart', methods=['POST'])
@json_login_required
def api_restart():
    server_id = int(request.json.get('server_id'))
    err = require_server_owner(server_id)
    if err: return err
    msg = restart_server(server_id)
    return jsonify({'status': 'success', 'msg': msg})

# ==================== 服务器管理 API ====================

def scan_servers():
    """扫描 servers/ 目录，返回服务器列表"""
    servers_dir = SERVER_DIR
    if not os.path.isdir(servers_dir):
        return []
    result = []

    # 如果启用了 Velocity，包含 ID 0
    if config.get('velocity', {}).get('enable', False):
        vdir = os.path.join(SERVER_DIR, '0')
        if os.path.isdir(vdir):
            running = (0 < len(mc_process) and mc_process[0] and mc_process[0].poll() is None)
            info = get_server_info(0)
            vport = get_velocity_port()
            result.append({
                'server_id': 0,
                'status': 'running' if running else 'stopped',
                'has_start_txt': os.path.isfile(os.path.join(vdir, 'start.txt')),
                'name': info['server_name'] if info else 'Velocity 代理',
                'server_port': vport
            })

    for entry in sorted(os.listdir(servers_dir)):
        if not entry.isdigit():
            continue
        sid = int(entry)
        # 如果启用了 Velocity，跳过 ID 0（已手动添加）
        if config.get('velocity', {}).get('enable', False) and sid == 0:
            continue
        sid = int(entry)
        server_dir = os.path.join(servers_dir, entry)
        if not os.path.isdir(server_dir):
            continue
        # 检测进程状态
        running = False
        if sid < len(mc_process) and mc_process[sid] and mc_process[sid].poll() is None:
            running = True
        # 检测是否有 start.txt
        has_start = os.path.isfile(os.path.join(server_dir, 'start.txt'))
        info = get_server_info(sid)
        server_port = (info or {}).get('server_port', 0)
        if not server_port:
            server_port = 30000 + sid  # 旧服务器自动补全端口
        result.append({
            'server_id': sid,
            'status': 'running' if running else 'stopped',
            'has_start_txt': has_start,
            'name': info['server_name'] if info and info.get('server_name') else f'服务器 #{sid}',
            'server_port': server_port
        })
    return result

@app.route('/api/servers', methods=['GET'])
@json_login_required
def api_servers():
    """获取服务器列表"""
    username = session['username']
    all_servers = scan_servers()
    if check_admin(username):
        return jsonify({'status': 'success', 'servers': all_servers})
    # 非管理员只返回自己的服务器
    my_ids = set(get_user_servers(username))
    my_servers = [s for s in all_servers if s['server_id'] in my_ids]
    return jsonify({'status': 'success', 'servers': my_servers})

@app.route('/api/server/create', methods=['POST'])
@json_login_required
def api_create_server():
    """创建新服务器"""
    username = session['username']
    is_admin = check_admin(username)

    # 非管理员检查开服上限
    if not is_admin:
        limit = get_server_limit(username)
        if limit <= 0:
            return jsonify({'status': 'error', 'msg': '你没有开服权限（上限为0），请联系管理员'}), 403
        # 只统计自己的服务器数量
        user_servers = get_user_servers(username)
        if len(user_servers) >= limit:
            return jsonify({'status': 'error', 'msg': f'已达到你的开服上限 ({limit})，无法创建更多服务器'}), 403

    max_servers = config['server']['max_servers']
    # 找出已存在的服务器 ID
    existing = scan_servers()
    used_ids = {s['server_id'] for s in existing}
    if len(used_ids) >= max_servers:
        return jsonify({'status': 'error', 'msg': f'已达到最大服务器数量限制 ({max_servers})'}), 400
    # 找最小可用的 ID（从 1 开始）
    new_id = 1
    while new_id in used_ids:
        new_id += 1
    if new_id > max_servers:
        return jsonify({'status': 'error', 'msg': f'已达到最大服务器数量限制 ({max_servers})'}), 400
    # 创建目录
    server_dir = os.path.join(SERVER_DIR, str(new_id))
    try:
        os.makedirs(server_dir, exist_ok=True)
        # 复制默认 start.txt
        start_txt_path = os.path.join(server_dir, 'start.txt')
        with open(start_txt_path, 'w', encoding='utf-8') as f:
            with open('defaults/default_start.txt', 'r', encoding='utf-8') as df:
                f.write(df.read())
        # 分配端口（30001 起依次递增）
        server_port = 30000 + new_id
        save_server_info(new_id, server_port=server_port)
        # 记录所有权
        add_user_server(username, new_id)
        # 同步到 velocity.toml
        if config.get('velocity', {}).get('enable', False):
            sync_velocity_toml_servers()
        return jsonify({'status': 'success', 'server_id': new_id, 'server_port': server_port, 'msg': f'服务器 #{new_id} 已创建（端口 {server_port}）'})
    except Exception as e:
        return jsonify({'status': 'error', 'msg': str(e)}), 500


@app.route('/api/server/delete', methods=['POST'])
@json_login_required
def api_delete_server():
    """删除服务器（删除整个目录，不可恢复）"""
    server_id = int(request.json.get('server_id'))
    if server_id == 0:
        return jsonify({'status': 'error', 'msg': 'Velocity 代理服务器不可删除'}), 400
    err = require_server_owner(server_id)
    if err: return err
    import shutil

    server_dir = os.path.join(SERVER_DIR, str(server_id))
    if not os.path.isdir(server_dir):
        return jsonify({'status': 'error', 'msg': f'服务器 #{server_id} 不存在'}), 404

    # 如果正在运行先停止
    if server_id < len(mc_process) and mc_process[server_id] and mc_process[server_id].poll() is None:
        try:
            mc_process[server_id].stdin.write("stop\n")
            mc_process[server_id].stdin.flush()
            mc_process[server_id].wait(timeout=10)
        except Exception:
            try:
                mc_process[server_id].kill()
            except Exception:
                pass
        mc_process[server_id] = None

    # 清理输出队列
    if server_id in output_queues:
        del output_queues[server_id]

    try:
        shutil.rmtree(server_dir)
        remove_user_server(session['username'], server_id)
        delete_server_info(server_id)
        if config.get('velocity', {}).get('enable', False):
            sync_velocity_toml_servers()
            if 0 < len(mc_process) and mc_process[0] and mc_process[0].poll() is None:
                try:
                    mc_process[0].stdin.write("velocity reload\n")
                    mc_process[0].stdin.flush()
                except Exception:
                    pass
        return jsonify({'status': 'success', 'msg': f'服务器 #{server_id} 已删除（不可恢复）'})
    except Exception as e:
        return jsonify({'status': 'error', 'msg': f'删除失败: {str(e)}'}), 500


@app.route('/api/server/rename', methods=['POST'])
@json_login_required
def api_rename_server():
    """重命名服务器"""
    server_id = int(request.json.get('server_id'))
    new_name = (request.json.get('name') or '').strip()
    if not new_name:
        return jsonify({'status': 'error', 'msg': '服务器名不能为空'}), 400
    if len(new_name) > 50:
        return jsonify({'status': 'error', 'msg': '服务器名不能超过50个字符'}), 400
    err = require_server_owner(server_id)
    if err: return err
    ok = save_server_info(server_id, server_name=new_name)
    if ok:
        return jsonify({'status': 'success', 'msg': f'已重命名为 {new_name}'})
    return jsonify({'status': 'error', 'msg': '改名失败'}), 500


@app.route('/api/server/status', methods=['GET'])
@json_login_required
def api_server_status():
    """检查服务器是否在线"""
    server_id = int(request.args.get('server_id'))
    err = require_server_owner(server_id)
    if err: return err
    running = (server_id < len(mc_process)
               and mc_process[server_id] is not None
               and mc_process[server_id].poll() is None)
    return jsonify({
        'status': 'success',
        'server_id': server_id,
        'running': running
    })


@app.route('/api/console', methods=['GET'])
def get_console_logs():
    server_id = int(request.args.get('server_id'))
    """获取最新 的控制台日志 (AJAX 轮询)"""
    logs = []
    if server_id in output_queues:
        # 尝试从队列中取出所有积压的日志
        while not output_queues[server_id].empty():
            logs.append(output_queues[server_id].get())
    return jsonify({'logs': logs})

@app.route('/api/command', methods=['POST'])
@json_login_required
def send_command():
    server_id = int(request.json.get('server_id'))
    """发送指令到 Minecraft"""
    global mc_process
    err = require_server_owner(server_id)
    if err: return err
    cmd = request.json.get('command')
    
    if not cmd:
        return jsonify({'status': 'error', 'msg': '指令为空'})
    
    if mc_process[server_id] and mc_process[server_id].poll() is None:
        try:
            # 将指令写入标准输入，并加上换行符模拟回车
            mc_process[server_id].stdin.write((cmd + "\n"))
            mc_process[server_id].stdin.flush()
            return jsonify({'status': 'success', 'msg': '指令已发送'})
        except Exception as e:
            return jsonify({'status': 'error', 'msg': str(e)})
    else:
        return jsonify({'status': 'error', 'msg': '服务端未运行，无法发送指令'})


# ==================== 管理后台 ====================

@app.route('/admin', methods=['GET'])
@admin_required
def admin_page():
    """管理后台页面"""
    user = current_user()
    servers = scan_servers()
    return render_template('adminbackend.html', user=user, servers=servers)


@app.route('/api/admin/users', methods=['GET'])
@json_admin_required
def api_admin_users():
    """获取用户列表"""
    users = list_users()
    return jsonify({'status': 'success', 'users': users})


@app.route('/api/admin/user/set_admin', methods=['POST'])
@json_admin_required
def api_admin_set_admin():
    """设置/取消管理员"""
    username = request.json.get('username')
    is_admin = bool(request.json.get('is_admin', False))
    if username == session['username']:
        return jsonify({'status': 'error', 'msg': '不能修改自己的管理员状态'}), 400
    if username == 'admin':
        return jsonify({'status': 'error', 'msg': '不能修改默认管理员账户状态'}), 400
    ok = set_admin_status(username, is_admin)
    return jsonify({'status': 'success' if ok else 'error',
                    'msg': f'用户 {username} 管理员状态已更新' if ok else '操作失败'})


@app.route('/api/admin/user/reset_password', methods=['POST'])
@json_admin_required
def api_admin_reset_password():
    """重置用户密码"""
    username = request.json.get('username')
    new_password = request.json.get('new_password', '')
    if len(new_password) < 6:
        return jsonify({'status': 'error', 'msg': '密码长度至少为6位'}), 400
    ok = reset_password(username, new_password)
    return jsonify({'status': 'success' if ok else 'error',
                    'msg': f'用户 {username} 密码已重置' if ok else '操作失败'})


@app.route('/api/admin/user/delete', methods=['POST'])
@json_admin_required
def api_admin_delete_user():
    """删除用户"""
    username = request.json.get('username')
    if username == session['username']:
        return jsonify({'status': 'error', 'msg': '不能删除自己的账户'}), 400
    if username == 'admin':
        return jsonify({'status': 'error', 'msg': '不能删除默认管理员账户'}), 400
    ok = delete_user(username)
    return jsonify({'status': 'success' if ok else 'error',
                    'msg': f'用户 {username} 已删除' if ok else '用户不存在'})


@app.route('/api/admin/user/set_limit', methods=['POST'])
@json_admin_required
def api_admin_set_limit():
    """设置用户开服上限"""
    username = request.json.get('username')
    limit = request.json.get('limit', 0)
    try:
        limit = int(limit)
        if limit < 0:
            limit = 0
    except (ValueError, TypeError):
        return jsonify({'status': 'error', 'msg': '无效的数值'}), 400
    ok = set_server_limit(username, limit)
    return jsonify({'status': 'success' if ok else 'error',
                    'msg': f'用户 {username} 开服上限已设为 {limit}' if ok else '操作失败'})


# ==================== 文件管理器 ====================

def safe_join(server_id, relative_path=''):
    """防止路径穿越攻击，确保路径在服务器目录内"""
    base = os.path.realpath(os.path.join(SERVER_DIR, str(server_id)))
    if not os.path.exists(base):
        os.makedirs(base)
    if relative_path:
        requested = os.path.realpath(os.path.join(base, relative_path))
    else:
        requested = base
    if not requested.startswith(base + os.sep) and requested != base:
        return None
    return requested


@app.route('/filemanager', methods=['GET'])
@login_required
def filemanager_page():
    """文件管理器页面"""
    user = current_user()
    server_id = int(request.args.get('server_id'))
    err = require_server_owner(server_id)
    if err:
        flash('无权操作此服务器', 'error')
        return redirect(url_for('backend'))
    return render_template('filemanager.html', server_id=server_id, user=user)


@app.route('/api/files/list', methods=['GET'])
@json_login_required
def api_list_files():
    """列出服务器目录下的文件和文件夹"""
    server_id = request.args.get('server_id')
    err = require_server_owner(int(server_id))
    if err: return err
    subdir = request.args.get('dir', '')
    target_dir = safe_join(server_id, subdir)

    if not target_dir:
        return jsonify({'status': 'error', 'msg': '路径无效'}), 400
    if not os.path.isdir(target_dir):
        return jsonify({'status': 'error', 'msg': '路径不是目录'}), 400

    entries = []
    for entry in sorted(os.listdir(target_dir)):
        full_path = os.path.join(target_dir, entry)
        is_dir = os.path.isdir(full_path)
        try:
            size = os.path.getsize(full_path) if not is_dir else None
        except OSError:
            size = None
        entries.append({
            'name': entry,
            'type': 'dir' if is_dir else 'file',
            'size': size
        })

    # 计算相对路径（用于面包屑导航）
    base = os.path.realpath(os.path.join(SERVER_DIR, str(server_id)))
    rel_path = os.path.relpath(target_dir, base).replace('\\', '/')
    if rel_path == '.':
        rel_path = ''

    return jsonify({
        'status': 'success',
        'files': entries,
        'current_path': rel_path,
        'server_id': server_id
    })


@app.route('/api/files/read', methods=['GET'])
@json_login_required
def api_read_file():
    """读取文件内容（文本方式）"""
    server_id = request.args.get('server_id')
    err = require_server_owner(int(server_id))
    if err: return err
    file_path = request.args.get('file', '')
    full_path = safe_join(server_id, file_path)

    if not full_path:
        return jsonify({'status': 'error', 'msg': '路径无效'}), 400
    if not os.path.isfile(full_path):
        return jsonify({'status': 'error', 'msg': '文件不存在'}), 404

    try:
        with open(full_path, 'r', encoding='utf-8') as f:
            content = f.read()
        return jsonify({
            'status': 'success',
            'content': content,
            'name': os.path.basename(full_path),
            'path': file_path
        })
    except UnicodeDecodeError:
        return jsonify({'status': 'error', 'msg': '无法以文本方式读取该文件（二进制文件）'}), 400
    except Exception as e:
        return jsonify({'status': 'error', 'msg': str(e)}), 500


@app.route('/api/files/write', methods=['POST'])
@json_login_required
def api_write_file():
    """保存文件内容"""
    server_id = request.json.get('server_id')
    err = require_server_owner(int(server_id))
    if err: return err
    file_path = request.json.get('file', '')
    content = request.json.get('content', '')
    full_path = safe_join(server_id, file_path)

    if not full_path:
        return jsonify({'status': 'error', 'msg': '路径无效'}), 400

    try:
        with open(full_path, 'w', encoding='utf-8') as f:
            f.write(content)
        return jsonify({'status': 'success', 'msg': '文件已保存'})
    except Exception as e:
        return jsonify({'status': 'error', 'msg': str(e)}), 500


@app.route('/api/files/rename', methods=['POST'])
@json_login_required
def api_rename_file():
    """重命名文件或文件夹"""
    server_id = request.json.get('server_id')
    err = require_server_owner(int(server_id))
    if err: return err
    old_path = request.json.get('old_path', '')
    new_name = request.json.get('new_name', '')

    if not new_name or '/' in new_name or '\\' in new_name:
        return jsonify({'status': 'error', 'msg': '文件名不能包含路径分隔符'}), 400

    full_old = safe_join(server_id, old_path)
    if not full_old:
        return jsonify({'status': 'error', 'msg': '原路径无效'}), 400

    parent_dir = os.path.dirname(full_old)
    full_new = os.path.join(parent_dir, new_name)

    # 安全检查
    base = os.path.realpath(os.path.join(SERVER_DIR, str(server_id)))
    if not full_new.startswith(base + os.sep) and full_new != base:
        return jsonify({'status': 'error', 'msg': '新路径无效'}), 400

    if not os.path.exists(full_old):
        return jsonify({'status': 'error', 'msg': '文件不存在'}), 404
    if os.path.exists(full_new):
        return jsonify({'status': 'error', 'msg': '目标文件已存在'}), 400

    try:
        os.rename(full_old, full_new)
        return jsonify({'status': 'success', 'msg': f'已重命名为 {new_name}'})
    except Exception as e:
        return jsonify({'status': 'error', 'msg': str(e)}), 500


@app.route('/api/files/create', methods=['POST'])
@json_login_required
def api_create_file():
    """新建文件"""
    server_id = request.json.get('server_id')
    err = require_server_owner(int(server_id))
    if err: return err
    dir_path = request.json.get('dir', '')
    name = request.json.get('name', '')

    if not name or '/' in name or '\\' in name:
        return jsonify({'status': 'error', 'msg': '文件名不能包含路径分隔符'}), 400

    target_dir = safe_join(server_id, dir_path)
    if not target_dir:
        return jsonify({'status': 'error', 'msg': '路径无效'}), 400

    full_path = os.path.join(target_dir, name)
    if os.path.exists(full_path):
        return jsonify({'status': 'error', 'msg': '文件已存在'}), 400

    try:
        with open(full_path, 'w', encoding='utf-8') as f:
            f.write('')
        return jsonify({'status': 'success', 'msg': f'文件 {name} 已创建'})
    except Exception as e:
        return jsonify({'status': 'error', 'msg': str(e)}), 500


@app.route('/api/files/mkdir', methods=['POST'])
@json_login_required
def api_create_folder():
    """新建文件夹"""
    server_id = request.json.get('server_id')
    err = require_server_owner(int(server_id))
    if err: return err
    dir_path = request.json.get('dir', '')
    name = request.json.get('name', '')

    if not name or '/' in name or '\\' in name:
        return jsonify({'status': 'error', 'msg': '文件夹名不能包含路径分隔符'}), 400

    target_dir = safe_join(server_id, dir_path)
    if not target_dir:
        return jsonify({'status': 'error', 'msg': '路径无效'}), 400

    full_path = os.path.join(target_dir, name)
    if os.path.exists(full_path):
        return jsonify({'status': 'error', 'msg': '文件夹已存在'}), 400

    try:
        os.makedirs(full_path)
        return jsonify({'status': 'success', 'msg': f'文件夹 {name} 已创建'})
    except Exception as e:
        return jsonify({'status': 'error', 'msg': str(e)}), 500


@app.route('/api/files/delete', methods=['POST'])
@json_login_required
def api_delete_file():
    """删除文件或空文件夹"""
    server_id = request.json.get('server_id')
    err = require_server_owner(int(server_id))
    if err: return err
    file_path = request.json.get('file', '')
    full_path = safe_join(server_id, file_path)

    if not full_path:
        return jsonify({'status': 'error', 'msg': '路径无效'}), 400
    if not os.path.exists(full_path):
        return jsonify({'status': 'error', 'msg': '文件不存在'}), 404

    try:
        if os.path.isdir(full_path):
            import shutil
            shutil.rmtree(full_path)  # 递归删除文件夹及内容
        else:
            os.remove(full_path)
        return jsonify({'status': 'success', 'msg': f'已删除 {os.path.basename(full_path)}'})
    except OSError as e:
        return jsonify({'status': 'error', 'msg': f'删除失败（非空文件夹？）: {str(e)}'}), 400
    except Exception as e:
        return jsonify({'status': 'error', 'msg': str(e)}), 500


@app.route('/api/files/upload', methods=['POST'])
@json_login_required
def api_upload_file():
    """上传文件到服务器目录"""
    server_id = request.form.get('server_id')
    err = require_server_owner(int(server_id))
    if err: return err
    subdir = request.form.get('dir', '')
    target_dir = safe_join(server_id, subdir)

    if not target_dir:
        return jsonify({'status': 'error', 'msg': '路径无效'}), 400

    if 'file' not in request.files:
        return jsonify({'status': 'error', 'msg': '未选择文件'}), 400

    file = request.files['file']
    if file.filename == '':
        return jsonify({'status': 'error', 'msg': '文件名为空'}), 400

    # 保留相对路径结构，防止路径穿越
    safe_rel = file.filename.replace('\\', '/')
    while safe_rel.startswith('./') or safe_rel.startswith('../') or safe_rel.startswith('/'):
        safe_rel = safe_rel.lstrip('./').lstrip('/')
    safe_rel = os.path.normpath(safe_rel)
    if not safe_rel or safe_rel.startswith('..'):
        return jsonify({'status': 'error', 'msg': '文件名无效'}), 400

    save_path = os.path.join(target_dir, safe_rel)

    # 自动创建子目录
    save_dir = os.path.dirname(save_path)
    if not os.path.exists(save_dir):
        os.makedirs(save_dir, exist_ok=True)

    # 如果文件已存在，自动重命名（加数字后缀）
    if os.path.exists(save_path):
        base, ext = os.path.splitext(os.path.basename(safe_rel))
        counter = 1
        parent_dir = os.path.dirname(save_path)
        while os.path.exists(os.path.join(parent_dir, f"{base}_{counter}{ext}")):
            counter += 1
        safe_rel = f"{base}_{counter}{ext}"
        save_path = os.path.join(parent_dir, safe_rel)

    try:
        file.save(save_path)
        return jsonify({'status': 'success', 'msg': f'文件 {os.path.basename(safe_rel)} 已上传', 'name': safe_rel})
    except Exception as e:
        return jsonify({'status': 'error', 'msg': f'上传失败: {str(e)}'}), 500


# ==================== 文件下载 ====================

@app.route('/api/files/download', methods=['GET'])
@json_login_required
def api_download_file():
    """下载单个文件"""
    server_id = request.args.get('server_id')
    err = require_server_owner(int(server_id))
    if err: return err
    file_path = request.args.get('file', '')
    full_path = safe_join(server_id, file_path)

    if not full_path:
        return jsonify({'status': 'error', 'msg': '路径无效'}), 400
    if not os.path.isfile(full_path):
        return jsonify({'status': 'error', 'msg': '文件不存在'}), 404

    try:
        return send_file(full_path, as_attachment=True, download_name=os.path.basename(full_path))
    except Exception as e:
        return jsonify({'status': 'error', 'msg': str(e)}), 500


@app.route('/api/files/download_folder', methods=['GET'])
@json_login_required
def api_download_folder():
    """打包下载文件夹（生成 ZIP）"""
    server_id = request.args.get('server_id')
    err = require_server_owner(int(server_id))
    if err: return err
    folder_path = request.args.get('folder', '')
    full_path = safe_join(server_id, folder_path)

    if not full_path:
        return jsonify({'status': 'error', 'msg': '路径无效'}), 400
    if not os.path.isdir(full_path):
        return jsonify({'status': 'error', 'msg': '路径不是文件夹'}), 400

    # 在内存中创建 ZIP
    buf = io.BytesIO()
    folder_name = os.path.basename(full_path) or f'server_{server_id}'
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(full_path):
            for file in files:
                file_full = os.path.join(root, file)
                # 计算相对路径（相对于打包的根目录）
                rel_path = os.path.relpath(file_full, full_path)
                zf.write(file_full, rel_path)

    buf.seek(0)
    return send_file(buf, as_attachment=True, download_name=f'{folder_name}.zip', mimetype='application/zip')


def main():
    print("启动服务器，监听端口",server_port,"...")
    app.run(host='0.0.0.0', port=server_port, debug=False)

if __name__ == "__main__":
    main()