#!/usr/bin/env python3
"""
佛山租房地图 - HTTP 服务
提供静态文件 + API 接口，支撑 map.html 地图页面。

使用方式：
    python3 serve/server.py              # 启动服务（默认端口 18888）
    python3 serve/server.py --port 9000  # 指定端口
    python3 serve/server.py --stop       # 停止服务
    python3 serve/server.py --status     # 查看状态
"""

import http.server
import json
import os
import re
import sys
import urllib.parse
from datetime import datetime

# === 配置 ===
DEFAULT_PORT = 18888
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LISTINGS_DIR = os.path.join(BASE_DIR, "房源")
INDEX_FILE = os.path.join(LISTINGS_DIR, "index.json")
COMM_FILE = os.path.join(BASE_DIR, "communities.json")
PID_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".server.pid")

# === 路由处理 ===
class Handler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/" or self.path == "/map.html":
            self.send_file(os.path.join(BASE_DIR, "map.html"))
        elif self.path == "/index.json":
            self.send_file(INDEX_FILE)
        elif self.path == "/communities.json":
            self.send_file(COMM_FILE)
        elif self.path == "/status":
            self.send_json({"status": "running", "port": self.server.server_port, "base_dir": BASE_DIR})
        else:
            super().do_GET()

    def do_POST(self):
        if self.path == "/api/update-listing":
            self.update_listing()
        elif self.path == "/api/update-community":
            self.update_community()
        else:
            self.send_error(404)

    def update_listing(self):
        content_length = int(self.headers['Content-Length'])
        body = json.loads(self.rfile.read(content_length))
        listing_id = body.get('id')
        status = body.get('status')
        notes = body.get('notes', '')

        if not listing_id or not status:
            self.send_json({"error": "missing fields"}, 400)
            return

        filepath = os.path.join(LISTINGS_DIR, listing_id + ".json")
        if os.path.exists(filepath):
            with open(filepath, 'r') as f:
                data = json.load(f)
        else:
            data = {"id": listing_id}

        now = datetime.now().astimezone().isoformat()
        old_status = data.get('status', '')
        data['status'] = status
        data['notes'] = notes

        if 'statusHistory' not in data:
            data['statusHistory'] = []
        if old_status != status:
            data['statusHistory'].append({
                "status": status,
                "changedAt": now,
                "notes": notes
            })

        with open(filepath, 'w') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

        self._update_index_listing(listing_id, status, notes)
        self.send_json({"ok": True, "changedAt": now})

    def update_community(self):
        content_length = int(self.headers['Content-Length'])
        body = json.loads(self.rfile.read(content_length))
        community_name = body.get('name')
        status = body.get('status')
        notes = body.get('notes', '')

        if not community_name or not status:
            self.send_json({"error": "missing fields"}, 0)
            return

        with open(INDEX_FILE, 'r') as f:
            index = json.load(f)

        updated = 0
        skipped = 0
        for lid, listing in index.get('listings', {}).items():
            clean = re.sub(r'^整租[·\s]*', '', listing.get('community', ''))
            if clean != community_name:
                continue

            # 以 index.json（前端事实源）判定是否已个性化评价，避免与单个文件脱节
            cur_status = listing.get('status', '')
            cur_notes = (listing.get('notes') or '').strip()
            already_evaluated = cur_status != '收录' or \
                (cur_notes and cur_notes != '手机APP自动化采集')
            if already_evaluated:
                skipped += 1
                continue

            now = datetime.now().astimezone().isoformat()
            old_status = cur_status
            listing['status'] = status
            if notes:
                listing['notes'] = notes
            if 'statusHistory' not in listing:
                listing['statusHistory'] = []
            if old_status != status:
                listing['statusHistory'].append({
                    "status": status,
                    "changedAt": now,
                    "notes": notes
                })

            filepath = os.path.join(LISTINGS_DIR, lid + ".json")
            data = None
            if os.path.exists(filepath):
                with open(filepath, 'r') as f:
                    data = json.load(f)
            if data is None:
                data = {"id": lid}
            old = data.get('status', '')
            data['status'] = status
            if notes:
                data['notes'] = notes
            if 'statusHistory' not in data:
                data['statusHistory'] = []
            if old != status:
                data['statusHistory'].append({
                    "status": status,
                    "changedAt": now,
                    "notes": notes
                })
            with open(filepath, 'w') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)

            updated += 1

        with open(INDEX_FILE, 'w') as f:
            json.dump(index, f, ensure_ascii=False, indent=2)

        self.send_json({"ok": True, "updated": updated, "skipped": skipped, "changedAt": now})

    def _update_index_listing(self, listing_id, status, notes):
        with open(INDEX_FILE, 'r') as f:
            index = json.load(f)
        if listing_id in index.get('listings', {}):
            index['listings'][listing_id]['status'] = status
            if notes:
                index['listings'][listing_id]['notes'] = notes
        with open(INDEX_FILE, 'w') as f:
            json.dump(index, f, ensure_ascii=False, indent=2)

    def send_file(self, path):
        with open(path, 'rb') as f:
            content = f.read()
        self.send_response(200)
        if path.endswith('.html'):
            self.send_header('Content-Type', 'text/html; charset=utf-8')
        elif path.endswith('.json'):
            self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', len(content))
        self.end_headers()
        self.wfile.write(content)

    def send_json(self, data, code=200):
        content = json.dumps(data).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', len(content))
        self.end_headers()
        self.wfile.write(content)

    def log_message(self, format, *args):
        pass  # 静默日志

# === 进程管理 ===
def write_pid(port):
    with open(PID_FILE, 'w') as f:
        f.write(f"{os.getpid()}\n{port}\n{BASE_DIR}")

def read_pid():
    if not os.path.exists(PID_FILE):
        return None
    try:
        with open(PID_FILE, 'r') as f:
            lines = f.read().strip().split('\n')
        pid = int(lines[0])
        port = int(lines[1])
        # 检查进程是否存活
        os.kill(pid, 0)
        return {"pid": pid, "port": port}
    except (OSError, ValueError, IndexError):
        # 进程不存在或文件损坏，清理
        os.remove(PID_FILE)
        return None

def stop_server():
    info = read_pid()
    if info:
        try:
            os.kill(info['pid'], 15)  # SIGTERM
            print(f"已停止服务 (PID {info['pid']}, 端口 {info['port']})")
        except ProcessLookupError:
            print(f"进程 {info['pid']} 已不存在")
        if os.path.exists(PID_FILE):
            os.remove(PID_FILE)
        return True
    else:
        print("没有运行中的服务")
        return False

def start_server(port):
    # 检查是否已有实例在运行
    existing = read_pid()
    if existing:
        print(f"服务已在运行中 (PID {existing['pid']}, 端口 {existing['port']})")
        print(f"访问地址: http://127.0.0.1:{existing['port']}/map.html")
        return

    # 检查端口是否被其他进程占用
    import socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    result = sock.connect_ex(('127.0.0.1', port))
    sock.close()
    if result == 0:
        print(f"端口 {port} 已被其他进程占用，请换一个端口")
        sys.exit(1)

    os.chdir(BASE_DIR)
    server = http.server.HTTPServer(('127.0.0.1', port), Handler)
    write_pid(port)
    print(f"服务已启动: http://127.0.0.1:{port}/map.html")
    print(f"PID: {os.getpid()}")
    print(f"项目目录: {BASE_DIR}")
    print(f"停止服务: python3 {os.path.abspath(__file__)} --stop")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n服务已停止")
        if os.path.exists(PID_FILE):
            os.remove(PID_FILE)

if __name__ == '__main__':
    # 解析参数
    if '--stop' in sys.argv:
        stop_server()
    elif '--status' in sys.argv:
        info = read_pid()
        if info:
            print(f"服务运行中: PID {info['pid']}, 端口 {info['port']}")
            print(f"访问地址: http://127.0.0.1:{info['port']}/map.html")
        else:
            print("服务未运行")
    else:
        port = DEFAULT_PORT
        # 查找 --port 参数
        for i, arg in enumerate(sys.argv):
            if arg == '--port' and i + 1 < len(sys.argv):
                try:
                    port = int(sys.argv[i + 1])
                except ValueError:
                    pass
        start_server(port)
