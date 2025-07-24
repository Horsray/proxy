#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
V2.5 
"""
from gevent import monkey
monkey.patch_all()
import os
import json
import requests
import time
import uuid
import copy
import logging
logging.getLogger("urllib3.connectionpool").setLevel(logging.WARNING)
# Path to local users file relative to this script
USERS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "users.json")
sessions = {}

def load_local_users():
    """Load local user credentials from users.json."""
    if os.path.exists(USERS_FILE):
        try:
            with open(USERS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            logging.info(f"🔐 已加载本地用户文件: {USERS_FILE}")
            logging.debug(f"本地用户列表: {list(data.get('users', {}).keys())}")
            return data.get("users", {})
        except Exception as e:
            logging.error(f"❌ 本地用户文件加载失败: {e}")
    else:
        logging.warning(f"⚠️ 未找到本地用户文件: {USERS_FILE}")
    return {}

from datetime import datetime, timedelta
from threading import Thread, Lock
from flask import Flask, request, jsonify, Response
from flask_cors import CORS
import websocket as ws_client
from collections import defaultdict, deque
# Default ComfyUI URL, will be overwritten by config on start
COMFYUI_URL = ""

logging.basicConfig(
    level=logging.DEBUG, 
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('huiying_proxy.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

LOCAL_USERS = load_local_users()


app = Flask(__name__)
CORS(app, origins="*")


def sanitize_url(url: str) -> str:
    """Normalize user-provided URLs for requests."""
    if not url:
        return url
    return url.replace("\\", "/").rstrip('/')


def is_remote_url(url: str) -> bool:
    """Return True if the given URL points to a public (non-local) address."""
    try:
        from urllib.parse import urlparse
        import ipaddress

        host = urlparse(url).hostname
        if not host:
            return False
        try:
            ip = ipaddress.ip_address(host)
            return not (ip.is_private or ip.is_loopback)
        except ValueError:
            # Host is not an IP address; assume remote
            return True
    except Exception:
        return False


def adapt_workflow_paths(workflow_data, comfyui_url: str):
    """Convert path separators for remote ComfyUI servers."""
    if not is_remote_url(comfyui_url):
        return workflow_data

    def _convert(obj):
        if isinstance(obj, dict):
            return {k: _convert(v) for k, v in obj.items()}
        elif isinstance(obj, list):
            return [_convert(v) for v in obj]
        elif isinstance(obj, str):
            return obj.replace("\\", "/")
        else:
            return obj

    return _convert(workflow_data)


comfyui_ws = None  
message_queue = defaultdict(deque) 
task_status = {}  
client_last_seen = {}  
upload_progress = {}  
queue_lock = Lock()  
task_result_cache = {}

def start_progress_tracker_by_mapping(prompt_id, workflow_id, client_id, comfyui_url):
    comfyui_url = sanitize_url(comfyui_url)
    try:
        with open("workflow_mappings.json", "r", encoding="utf-8") as f:
            mappings = json.load(f).get("workflow_mappings", {})
            total_nodes = mappings.get(workflow_id, {}).get("node_count", 20)  # 默认 20
    except Exception as e:
        logger.warning(f"⚠️ 获取工作流节点总数失败: {e}")
        total_nodes = 20

    def track():
        max_poll = 120
        for i in range(1, max_poll + 1):
            try:
                url = f"{comfyui_url}/history/{prompt_id}"
                resp = requests.get(url, timeout=3)
                if resp.status_code != 200:
                    logger.debug(f"轮询 {i} 次 - ComfyUI 返回状态码: {resp.status_code}")
                    print(f"\rDEBUG  -  🎯 正在等待任务完成... 已轮询 {i} 次，尚未获取到历史记录", end="", flush=True)
                    time.sleep(1)
                    continue

                data = resp.json()
                if prompt_id not in data:
                    print(f"\rDEBUG  -  🎯 正在等待任务完成... 已轮询 {i} 次，尚无该任务记录", end="", flush=True)
                    time.sleep(1)
                    continue

                history_data = data[prompt_id]
                status = history_data.get("status", {})

                outputs = history_data.get("outputs", {})
                current_node = len(outputs)

                if status.get("status_str") == "success":
                    current_node = total_nodes
                elif status.get("status_str") == "error":
                    current_node = 0

                percent = int((current_node / max(1, total_nodes)) * 100)

                print(f"\rDEBUG  -  🎯 正在等待任务完成... 已轮询 {i} 次，进度 {percent}% [节点 {current_node}/{total_nodes}]", end="", flush=True)

                
                add_message_to_queue(client_id, {
                    "type": "executing",        
                    "level": "info",           
                    "data": {
                        "prompt_id":     prompt_id,
                        "workflow_id":   workflow_id,
                        "node":          current_node    
                    }
                })

                add_message_to_queue(client_id, {
                    "type": "progress",          
                    "level": "info",             
                    "data": {
                        "prompt_id":      prompt_id,
                        "workflow_id":    workflow_id,
                        "value":          current_node,    
                        "max":            total_nodes,     
                        "percentage":     percent,         
                        "sampler_step":   sampler_step,    
                        "sampler_steps":  sampler_steps
                    }
                })
                if current_node >= total_nodes or status.get("status_str") in ["success", "error"]:
                    print()  # 完成后换行
                    logger.info(f"📈 进度更新: {percent}% [节点 {current_node}/{total_nodes}]")
                    break

            except Exception as e:
                print(f"\rDEBUG  -  🎯 第 {i} 次轮询异常: {e}", end="", flush=True)
            time.sleep(1)


def add_message_to_queue(client_id, message):
   
    with queue_lock:
    
        if len(message_queue[client_id]) > 100:
            message_queue[client_id].popleft()
     
        enhanced_message = {
            "id": str(uuid.uuid4())[:8],
            "timestamp": time.time(),
            "data": message
        }
        
        message_queue[client_id].append(enhanced_message)
        logger.info(f"📨 消息已添加到客户端队列: {client_id} (类型: {message.get('type', 'unknown')})")

def get_messages_for_client(client_id, since_timestamp=None):
    
    with queue_lock:
        client_last_seen[client_id] = time.time()
        
        if client_id not in message_queue:
            return []
        
        messages = []
        for msg in message_queue[client_id]:
            if since_timestamp is None or msg["timestamp"] > since_timestamp:
                messages.append(msg)
        
        # 清理已读消息（保留最近20条）
        if len(messages) > 0:
            message_queue[client_id] = deque(list(message_queue[client_id])[-20:])
        
        return messages

def broadcast_message(message):
   
    current_time = time.time()
    active_clients = []
    
    with queue_lock:
        # 找出活跃客户端（最近5分钟内有活动）
        for client_id, last_seen in client_last_seen.items():
            if current_time - last_seen < 300:  # 5分钟
                active_clients.append(client_id)
    

    for client_id in active_clients:
        add_message_to_queue(client_id, message)

def cleanup_inactive_clients():

    current_time = time.time()
    inactive_clients = []
    
    with queue_lock:
        for client_id, last_seen in client_last_seen.items():
            if current_time - last_seen > 600:  # 10分钟无活动
                inactive_clients.append(client_id)
        
        for client_id in inactive_clients:
            del client_last_seen[client_id]
            if client_id in message_queue:
                del message_queue[client_id]
            if client_id in upload_progress:
                del upload_progress[client_id]
    
    if inactive_clients:
        logger.info(f"🧹 清理了 {len(inactive_clients)} 个非活跃客户端")


def comfy_ws_listener():
    import websocket
    import json
    import copy

    def enhance_message(original_message):
        enhanced = copy.deepcopy(original_message)
        msg_type = enhanced.get("type")
        data = enhanced.get("data", {})

        if msg_type == "status":
            status = data.get("status", {})
            exec_info = status.get("exec_info", {})
            enhanced["data"]["enhanced_info"] = {
                "queue_remaining": exec_info.get("queue_remaining", 0),
                "queue_running": len(exec_info.get("queue_running", [])),
                "timestamp": time.time()
            }

        elif msg_type == "executing":
            node_id = data.get("node")
            prompt_id = data.get("prompt_id")
            enhanced["data"]["enhanced_info"] = {
                "node_id": node_id,
                "prompt_id": prompt_id,
                "status": "executing",
                "timestamp": time.time()
            }

        elif msg_type == "progress":
            value = data.get("value", 0)
            max_value = data.get("max", 0)
            node_id = data.get("node")
            prompt_id = data.get("prompt_id")
            try:
                percentage = round((value / max_value) * 100, 1) if max_value else 0
            except:
                percentage = 0
            enhanced["data"]["enhanced_info"] = {
                "percentage": percentage,
                "current_step": value,
                "total_steps": max_value,
                "node_id": node_id or "unknown",
                "prompt_id": prompt_id or "unknown",
                "is_sampling": str(data.get("name", "")).lower().startswith("ksampler"),
                "timestamp": time.time()
            }

        elif msg_type == "executed":
            prompt_id = data.get("prompt_id")
            node_id = data.get("node")
            enhanced["data"]["enhanced_info"] = {
                "prompt_id": prompt_id,
                "node_id": node_id,
                "status": "completed",
                "timestamp": time.time()
            }

        return enhanced

    def on_message(ws, message):
        try:
            msg_json = json.loads(message)
            enhanced = enhance_message(msg_json)

            msg_type = msg_json.get("type")
            data = msg_json.get("data", {})

            if msg_type == "progress":
                prompt_id = data.get("prompt_id")
                value = data.get("value", 0)
                max_value = data.get("max", 1)
                percent = int((value / max_value) * 100) if max_value else 0
                node_id = data.get("node")

                if prompt_id in task_status:
                    task_status[prompt_id]["type"] = "progress"
                    task_status[prompt_id]["data"].update({
                        "percentage": percent,
                        "current_step": value,
                        "total_steps": max_value,
                        "node_id": node_id,
                        "is_sampling": str(data.get("name", "")).lower().startswith("ksampler"),
                    })
                    task_status[prompt_id]["timestamp"] = time.time()
                    logger.info(f"📈 进度更新: {percent}% [{value}/{max_value}] @节点 {node_id}")

            elif msg_type == "executing":
                prompt_id = data.get("prompt_id")
                node_id = data.get("node")

                if prompt_id in task_status:
                    task_status[prompt_id]["type"] = "executing"
                    task_status[prompt_id]["data"].update({
                        "node_id": node_id,
                        "status": "executing"
                    })
                    task_status[prompt_id]["timestamp"] = time.time()
                    logger.info(f"⚙️ [执行中] {prompt_id} 节点: {node_id}")

            elif msg_type == "executed":
                prompt_id = data.get("prompt_id")

                if prompt_id in task_status:
                    task_status[prompt_id]["type"] = "done"
                    task_status[prompt_id]["data"].update({
                        "status": "done"
                    })
                    task_status[prompt_id]["timestamp"] = time.time()
                    logger.info(f"✅ [完成] {prompt_id}")

        
            client_id = task_status.get(data.get("prompt_id"), {}).get("data", {}).get("client_id")
            if client_id:
                add_message_to_queue(client_id, enhanced)

        except Exception as e:
            logger.warning(f"⚠️ WebSocket消息处理失败: {e}")
    logging.getLogger("websocket").setLevel(logging.CRITICAL)
    def on_error(ws, error):
        logger.error(f"远程监听服务尚未开启，请等待...")
    def on_close(ws, close_status_code, close_msg):
        logger.warning(f"等待核心服务程序启动，开始尝试建立连接")
        
    def on_open(ws):
        logger.info("🔗 [ComfyUI WS] 连接已建立")
def start_ws_listener(base_url):
    if not base_url or not base_url.startswith("http"):
        print("⚠️ 跳过 WebSocket 初始化：无有效 URL")
        return

    import websocket

    ws_url = base_url.replace("http://", "ws://").replace("https://", "wss://") + "/ws"
    ws = websocket.WebSocketApp(
        ws_url,
        on_message=on_message,
        on_error=on_error,
        on_close=on_close,
        on_open=on_open
    )

    thread = Thread(target=ws.run_forever, daemon=True)
    thread.start()


def cleanup_task():
   
    while True:
        try:
            cleanup_inactive_clients()
            
           
            current_time = time.time()
            expired_tasks = []
            for prompt_id, status_info in task_status.items():
                if current_time - status_info["timestamp"] > 7200:  # 2小时
                    expired_tasks.append(prompt_id)
            
            for prompt_id in expired_tasks:
                del task_status[prompt_id]
            
            if expired_tasks:
                logger.info(f"🧹 清理了 {len(expired_tasks)} 个过期任务状态")
                
        except Exception as e:
            logger.error(f"清理任务异常: {e}")
        
        time.sleep(60)  

 
class HuiYingProxy:
    def __init__(self, config_file='config.json', mappings_file='workflow_mappings.json'):
        self.config_file = config_file
        self.mappings_file = mappings_file
        self.config = self.load_config(config_file)
        self.mappings = self.load_mappings(mappings_file)
        self.workflow_cache = {}
        self.workflow_node_count = {}
        if self.config.get('load_from_cloud'):
            self.preload_workflows()

    def save_config(self):
        """Persist current configuration to disk."""
        try:
            with open(self.config_file, 'w', encoding='utf-8') as f:
                json.dump(self.config, f, indent=2, ensure_ascii=False)
            logger.info("💾 配置已保存")
        except Exception as e:
            logger.warning(f"⚠️ 配置保存失败: {e}")
        

    def load_config(self, config_file):
        
        
        default_config = {
            "workflow_dir": "workflows",
            "comfyui_url": "",
            "cloud_service_url": "proxy.hueying.cn",
            "proxy_port": 8080,
            "load_from_cloud": False,
            "timeout": 30,
            "enable_parameter_validation": True,
            "enable_workflow_cache": True,
            "log_level": "INFO"
        }
        logger.info(f"📁 开始扫描所需的必要文件")

        if os.path.exists(config_file):
            try:
                with open(config_file, 'r', encoding='utf-8') as f:
                    user_config = json.load(f)
                default_config.update(user_config)
                logger.info(f"✅ 基础配置文件加载成功: {config_file}")
    
            except Exception as e:
                logger.error(f"❌ 配置文件加载失败: {e}")
        else:
        
            with open(config_file, 'w', encoding='utf-8') as f:
                json.dump(default_config, f, indent=2, ensure_ascii=False)
            logger.info(f"🆕 初始化默认配置文件成功: {config_file}")
            logger.info(f"💾 已保存 ComfyUI 地址配置: {default_config['comfyui_url']}")
  
        default_config["workflow_dir"] = os.path.abspath(default_config["workflow_dir"])
        default_config["comfyui_url"] = sanitize_url(default_config["comfyui_url"])
        if default_config.get('load_from_cloud'):
            logger.info("📁 将从云端加载工作流和映射配置")
        else:
            logger.info(f"📁 当前工作流路径为: {default_config['workflow_dir']}")
        logger.info(f"🔗 当前 ComfyUI 地址: {default_config['comfyui_url']}")


        return default_config

    def load_mappings(self, mappings_file):
        if self.config.get('load_from_cloud'):
            return self.fetch_remote_mappings()

        if not os.path.exists(mappings_file):
            logger.warning(f"⚠️ 配置文件不存在: {mappings_file}")
            return {}
        
        try:
            with open(mappings_file, 'r', encoding='utf-8') as f:
                try:
                    mappings = json.load(f)
                    logger.info(f"✅ 初始化参数映射配置成功: {mappings_file}")
                    return mappings
                except json.JSONDecodeError as e:
                    logger.error(f"🔥 JSON语法错误: {e.msg}，位置：行{e.lineno}列{e.colno}")
                    with open(mappings_file, 'r', encoding='utf-8') as f_context:
                        lines = f_context.readlines()
                        context = lines[max(0, e.lineno-2):e.lineno+1]
                        logger.error(f"🔥 错误行上下文:\n{''.join(context)}")
                    return {}
        except Exception as e:
            logger.error(f"🔥 配置文件读取失败: {str(e)}")
            return {}

    def load_workflow(self, workflow_id):


        if self.config.get('load_from_cloud'):
            if workflow_id in self.workflow_cache:
                logger.info(f"⚡ 从缓存中加载云端工作流: {workflow_id}")
                return self.workflow_cache[workflow_id]
            return self.fetch_remote_workflow(workflow_id)

        if self.config.get('enable_workflow_cache', True) and workflow_id in self.workflow_cache:
            logger.info(f"⚡ 从缓存中加载当前工作流: {workflow_id}")
            return self.workflow_cache[workflow_id]
            
        workflow_file = os.path.join(self.config['workflow_dir'], f"{workflow_id}.json")
        
        if not os.path.exists(workflow_file):
            logger.error(f"❌ 工作流文件不存在: {workflow_file}")
            raise FileNotFoundError(f"工作流文件不存在: {workflow_file}")
            
        try:
            with open(workflow_file, 'r', encoding='utf-8') as f:
                workflow = json.load(f)
     
            if self.config.get('enable_workflow_cache', True):
                self.workflow_cache[workflow_id] = workflow
            
            logger.info(f"📄 从缓存中读取到当前工作流: {workflow_id}")
            return workflow
            
        except Exception as e:
            logger.error(f"工作流加载失败 {workflow_id}: {e}")
            raise
      
    def _set_nested_value(self, data, path, value):

        try:
            for key in path[:-1]:
                if isinstance(key, int):
                    data = data[key]
                else:
                    data = data.setdefault(key, {})
            last_key = path[-1]
            if isinstance(last_key, int):
                data[last_key] = value
            else:
                data[last_key] = value
            logger.debug(f"✅ 设置路径 {path} = {value}")
        except Exception as e:
            logger.error(f"❌ 设置参数失败: path={path}, value={value}, 错误: {e}")

    def fetch_remote_mappings(self):
        cloud_url = sanitize_url(self.config.get('cloud_service_url', ''))
        url = f"{cloud_url}/workflow_mappings.json"
        try:
            resp = requests.get(url, timeout=self.config.get('timeout', 30))
            resp.raise_for_status()
            logger.info(f"✅ 从云端加载映射配置成功: {url}")
            return resp.json()
        except Exception as e:
            logger.error(f"❌ 加载云端映射配置失败: {e}")
            return {}

    def fetch_remote_workflow(self, workflow_id):
        cloud_url = sanitize_url(self.config.get('cloud_service_url', ''))
        url = f"{cloud_url}/workflows/{workflow_id}.json"
        try:
            resp = requests.get(url, timeout=self.config.get('timeout', 30))
            resp.raise_for_status()
            workflow = resp.json()
            self.workflow_cache[workflow_id] = workflow
            logger.info(f"✅ 从云端加载工作流: {workflow_id}")
            return workflow
        except Exception as e:
            logger.error(f"❌ 获取云端工作流 {workflow_id} 失败: {e}")
            return {}

    def preload_workflows(self):
        workflow_ids = self.mappings.get('workflow_mappings', {}).keys()
        for wid in workflow_ids:
            self.fetch_remote_workflow(wid)

    def merge_workflow_params(self, workflow, param_dict, workflow_id):
      
        try:
            merged_workflow = copy.deepcopy(workflow)

            workflow_mappings = self.mappings.get('workflow_mappings', {})
            param_mappings = workflow_mappings.get(workflow_id, {}).get('param_mappings', {})

            logger.info(f"🔧 匹配到绘影 AIGC 发送的 {len(param_dict)} 个参数")

            for param_key, param_value in param_dict.items():
                if isinstance(param_value, str) and param_value.startswith("默认"):
                    #logger.info(f"🆗 默认参数: {param_key} = {param_value}")
                    continue
                if param_key not in param_mappings:
                    logger.debug(f"⏭️ 未映射参数: {param_key}")
                    continue

                path = param_mappings[param_key]
                self._set_nested_value(merged_workflow, path, param_value)

            return merged_workflow

        except Exception as e:
            logger.error(f"❌ 参数合并失败: {e}")
            raise
       
    def send_to_comfyui(self, workflow_data, client_id, comfyui_url=None):

        import requests

        if not comfyui_url:
            comfyui_url = self.config.get("comfyui_url")
        if not comfyui_url:
            raise ValueError("comfyui_url is required")
        comfyui_url = sanitize_url(comfyui_url)
        workflow_data = adapt_workflow_paths(workflow_data, comfyui_url)
        url = f"{comfyui_url}/prompt"

        try:
            headers = {
                "Content-Type": "application/json"
            }
            payload = {
                "client_id": client_id,
                "prompt": workflow_data
            }
            logger.info(f"🚀 正在提交任务到 生成服务器: {url}")
            response = requests.post(url, json=payload, headers=headers, timeout=self.config.get("timeout", 30))

            if response.status_code == 200:
                logger.info("✅ 任务提交成功")
                return {"data": response.json()}
            else:
                logger.error(f"❌ 任务请求失败，状态码: {response.status_code}, 内容: {response.text}")
                return {"error": f"任务 请求失败: {response.status_code}", "detail": response.text}

        except Exception as e:
            logger.error(f"❌ ComfyUI请求失败: {str(e)}")
            return {"error": str(e)}
proxy = HuiYingProxy()
COMFYUI_URL = sanitize_url(proxy.config.get("comfyui_url", ""))


@app.before_request
def log_all_requests():
    logger.info(f"📡 收到插件接口请求: {request.method} {request.path}")

# 处理跨域请求
@app.route('/api/poll', methods=['GET'])
def poll_messages():

    client_id = request.args.get('clientId')
    since_timestamp = request.args.get('since', type=float)
    
    if not client_id:
        return jsonify({"error": "缺少clientId参数"}), 400
    
    try:
        messages = get_messages_for_client(client_id, since_timestamp)
        

        extra_info = {
            "active_tasks": len(task_status),
            "queue_size": len(message_queue.get(client_id, [])),
            "server_time": time.time()
        }
        
        return jsonify({
            "code": 0,
            "msg": "success",
            "data": {
                "messages": messages,
                "timestamp": time.time(),
                "clientId": client_id,
                "extra_info": extra_info
            }
        })
        
    except Exception as e:
        logger.error(f"轮询消息失败: {e}")
        return jsonify({"error": f"轮询失败: {str(e)}"}), 500

@app.route('/api/task_status/<prompt_id>', methods=['GET'])
def get_task_status(prompt_id):

    try:
        if prompt_id in task_status:
            status_info = task_status[prompt_id]
         
            enhanced_status = {
                **status_info,
                "age_seconds": time.time() - status_info["timestamp"],
                "is_recent": (time.time() - status_info["timestamp"]) < 300  # 5分钟内
            }
            
            return jsonify({
                "code": 0,
                "msg": "success",
                "data": enhanced_status
            })
        else:
            return jsonify({
                "code": 404,
                "msg": "任务状态不存在",
                "data": None
            }), 404
            
    except Exception as e:
        logger.error(f"获取任务状态失败: {e}")
        return jsonify({"error": f"获取状态失败: {str(e)}"}), 500
# #comfyui对象信息接口
# @app.route('/api/object_info', methods=['GET'])
# def proxy_object_info():
  
#     try:
#         comfyui_url = request.args.get('comfyuiUrl') or proxy.config.get('local_comfyui_url', COMFYUI_URL)
#         comfyui_url = sanitize_url(comfyui_url)
#         res = requests.get(f"{comfyui_url}/object_info", timeout=10)
#         logger.info("🔁 成功转发 object_info，状态码: %s", res.status_code)
#         return Response(
#             res.content,
#             status=res.status_code,
#             content_type=res.headers.get('Content-Type', 'application/json')
#         )
#     except Exception as e:
#         logger.error(f"❌ object_info 转发失败: {e}")
#         return jsonify({"error": f"连接失败: {str(e)}"}), 500
# #图像上传接口
# @app.route('/upload/image', methods=['POST'])
# def proxy_upload_image():
#     logger.info("🖼️ 收到插件上传图像请求，开始转发给真实 ComfyUI")
#     try:
#         data = request.form.to_dict()
#         files = {
#             key: (f.filename, f.stream, f.mimetype)
#             for key, f in request.files.items()
#         }
#         comfyui_url = data.get('comfyuiUrl') or proxy.config.get('local_comfyui_url', COMFYUI_URL)
#         comfyui_url = sanitize_url(comfyui_url)
#         resp = requests.post(f"{comfyui_url}/upload/image", data=data, files=files)
#         logger.info(f"📤 成功转发图像上传请求，状态码: {resp.status_code}")
#         return (resp.content, resp.status_code, resp.headers.items())
#     except Exception as e:
#         logger.exception("❌ 转发图像上传失败:")
#         return jsonify({"code": 500, "msg": "图像上传转发失败", "error": str(e)}), 500

# #蒙版接口（预留）
# @app.route('/api/upload/mask', methods=['POST'])
# def proxy_upload_mask():
#     logger.info("🖤 收到插件上传 mask 请求，开始转发给真实 ComfyUI")
#     try:
#         data = request.form.to_dict()
#         files = {
#             key: (f.filename, f.stream, f.mimetype)
#             for key, f in request.files.items()
#         }
#         comfyui_url = data.get('comfyuiUrl') or proxy.config.get('local_comfyui_url', COMFYUI_URL)
#         comfyui_url = sanitize_url(comfyui_url)
#         resp = requests.post(f"{comfyui_url}/upload/mask", data=data, files=files)
#         logger.info(f"📤 成功转发 mask 上传请求，状态码: {resp.status_code}")
#         return (resp.content, resp.status_code, resp.headers.items())
#     except Exception as e:
#         logger.exception("❌ mask 转发失败:")
#         return jsonify({"code": 500, "msg": "上传 mask 转发失败", "error": str(e)}), 500
#数据提交接口
@app.route('/psPlus/workflow/huiYingCommit', methods=['POST'])
def huiying_commit():

    data = request.get_json() or {}

    # logger.debug(f"🌐 收到完整数据: {json.dumps(data, indent=2, ensure_ascii=False)}")
    # logger.info("🛠️ 正在处理绘影工作流提交请求...")

    # try:
    #     #logger.debug(f"🔍 Headers: {dict(request.headers)}")

    #     if request.is_json:
    #         #logger.debug(f"🧾 JSON Body: {request.get_json()}")
    #     else:
    #         #logger.debug(f"🧾 非 JSON 请求体")
    # except Exception as e:
    #     #logger.error(f"❌ 打印请求失败: {e}")

    try:
      
        logger.info("📝 接收来自 绘影 AI 的数据字段")
        if not data:
            return jsonify({"code": 400, "msg": "请求数据为空"}), 400
        
        workflow_id = data.get('workflowId')
        param_dict = data.get('paramDict', {})
        client_id = data.get('clientId', str(uuid.uuid4()))
        comfyui_url = data.get('comfyuiUrl') or proxy.config.get('comfyui_url')
        if not comfyui_url:
            return jsonify({"code": 400, "msg": "missing comfyuiUrl"}), 400
        comfyui_url = sanitize_url(comfyui_url)
        
       
        if not workflow_id:
            return jsonify({"code": 400, "msg": "workflowId不能为空"}), 400
        
        
        try:
            workflow = proxy.load_workflow(workflow_id)
            logger.info(f"📦 工作流加载成功: {workflow_id}")
            logger.info(f"📊 存在总节点数: {len(workflow)}")
            logger.info(f"📥 接收参数数量: {len(param_dict)}")
            
           
            for k, v in param_dict.items():
                logger.debug(f"  ├─ 参数: {k} = {v}")
                
        except FileNotFoundError:
            return jsonify({"code": 404, "msg": f"工作流不存在: {workflow_id}"}), 404
        except Exception as e:
            return jsonify({"code": 500, "msg": f"工作流加载失败: {str(e)}"}), 500
        
      
        try:
            merged_workflow = proxy.merge_workflow_params(workflow, param_dict, workflow_id)

            
            total_nodes = len([
                k for k, v in merged_workflow.items()
                if isinstance(v, dict) and "class_type" in v
            ])

            logger.info(f"📊 参数合并校验完毕 ")

          
            valid_workflow = {}
            for key, value in merged_workflow.items():
                if isinstance(value, dict) and "class_type" in value:
                    valid_workflow[key] = value
                else:
                    logger.warning(f"⚠️ 移除非法节点: {key}")
            merged_workflow = valid_workflow

            try:
                BASE_DIR = os.path.dirname(os.path.abspath(__file__))
                model_name = None
                sampler_name = "N/A"
                scheduler = "N/A"
                steps = "N/A"
                cfg = "N/A"
                denoise = "N/A"
                seed = "N/A"

                has_checkpoint = False

                for node_id, node in merged_workflow.items():
                    if not isinstance(node, dict):
                        continue
                    inputs = node.get("inputs", {})
                    class_type = node.get("class_type", "")

                    if class_type == "CheckpointLoaderSimple":
                        model_name = inputs.get("ckpt_name")
                        has_checkpoint = True
                    elif class_type == "UNetLoader" and not has_checkpoint:
                        model_name = "UNET 及其他"

                    if class_type in ["KSampler", "KSamplerAdvanced"]:
                        sampler_name = inputs.get("sampler_name", sampler_name)
                        scheduler = inputs.get("scheduler", scheduler)
                        steps = inputs.get("steps", steps)
                        cfg = inputs.get("cfg", cfg)
                        denoise = inputs.get("denoise", denoise)
                        seed = inputs.get("seed", seed)

                    if "image" in inputs:
                        image_path = inputs["image"]
                        width = inputs.get("width", "未知")
                        height = inputs.get("height", "未知")
                        image_size = f"{width}x{height}"


                seed_info = f"{seed}（随机）" if str(seed) in ["-1", "None", "-1.0"] else str(seed)
                model_display = model_name if model_name else "UNET 及其他"

                logger.info("📤 ******** 工作流概要 ********")
                logger.info(f"🎯 工作流 ID: {workflow_id}")
                logger.info(f"🤖 模型: {model_display}")
                logger.info(f"⚙️ 采样器: {sampler_name} | 调度器: {scheduler}")
                logger.info(f"🎛️ 重绘幅度: {denoise} | 步数: {steps} | CFG: {cfg}")
                logger.info(f"🎲 种子: {seed_info}")
                logger.info(f"📊 节点总数: {total_nodes}")
                logger.info("📤 ***************************")

            except Exception as e:
                logger.warning(f"⚠️ 工作流概要打印失败: {e}")

        except Exception as e:
            logger.exception("❌ 参数合并失败:")
            return jsonify({"code": 500, "msg": f"参数合并失败: {str(e)}"}), 500

        try:
            result = proxy.send_to_comfyui(merged_workflow, client_id, comfyui_url)
            prompt_id = result["data"].get("prompt_id")

            if not prompt_id:
                logger.error("❌ ComfyUI 返回的结果中未包含 prompt_id")
                return jsonify({"code": 500, "msg": "ComfyUI返回数据异常"}), 500
            
            
            task_status[prompt_id] = {
                "type": "submitted",
                "data": {
                    "prompt_id": prompt_id,
                    "client_id": client_id,
                    "workflow_id": workflow_id,
                    "node_count": total_nodes  
                },
                "timestamp": time.time(),
                "enhanced": True
            }

           
            start_progress_tracker_by_mapping(prompt_id, workflow_id, client_id, comfyui_url)

            
            submit_message = {
                "type": "task_submitted",
                "data": {
                    "prompt_id": prompt_id,
                    "workflow_id": workflow_id,
                    "node_count": total_nodes,  
                    "client_id": client_id
                }
            }
            add_message_to_queue(client_id, submit_message)
            response_data = {
                "code": 0,
                "msg": "提交成功",
                "data": {
                    "prompt_id": prompt_id,
                    "taskId": prompt_id,
                    "number": result["data"].get("node_num", 0),
                    "client_id": client_id,
                    "node_num": total_nodes  # 
                }
            }
            
            #logger.info("✅ 任务提交成功")
            logger.info(f"  → prompt_id: {prompt_id}")
            logger.info(f"  → 节点总数: {total_nodes}")
            
            return jsonify(response_data), 200

        except Exception as e:
            logger.exception("❌ ComfyUI请求失败:")
            return jsonify({"code": 500, "msg": f"ComfyUI请求失败: {str(e)}"}), 500

    except Exception as e:
        logger.error(f"❌ 请求处理失败: {e}")
        return jsonify({"code": 500, "msg": f"服务器内部错误: {str(e)}"}), 500


from flask import request
import json
import gevent
import time

@app.route("/ws")
def proxy_ws():
    ws = request.environ.get("wsgi.websocket")
    if not ws:
        logger.warning("❌ 插件发来的是普通 HTTP 请求，非 WebSocket")
        return "Expected WebSocket", 400

    client_id = request.args.get("clientId") or request.headers.get("Clientid")
    if not client_id:
        logger.warning("❌ 未提供 clientId，无法建立伪 WebSocket 轮询")
        ws.close()
        return


    try:
        while True:
            msgs = get_messages_for_client(client_id)

            if not msgs:
                logger.debug(f"🕐 [client {client_id}] 当前无新消息")
            else:
                for msg in msgs:
                    ws.send(json.dumps(msg))
                    logger.info(f"📤 [client {client_id}] 已转发消息: {msg}")

            gevent.sleep(1)  
    except Exception as e:
        logger.warning(f"⚠️ WebSocket 异常: {e}")
    finally:
        ws.close()

@app.route('/api/config/comfyui_url', methods=['POST'])
def update_comfyui_url():
    data = request.get_json() or {}
    url = data.get('url')
    if not url:
        return jsonify({"code": 400, "msg": "missing url"}), 400
    url = sanitize_url(url)
    proxy.config['comfyui_url'] = url
    proxy.save_config()
    global COMFYUI_URL
    COMFYUI_URL = url
    return jsonify({"code": 200, "msg": "updated", "data": {"comfyuiUrl": url}})

CLOUD_CHECK_URL = "https://umanage.lightcc.cloud/prod-api/psPlus/workflow/checkOnline"

@app.route('/psPlus/workflow/checkOnline', methods=['GET'])
def check_online():
    headers = dict(request.headers)
    token = headers.get('Authorization', '').replace('Bearer ', '')
    if token in sessions:
        logger.info("✅ 在线检查通过 - local")
        return jsonify({"code": 200, "msg": "操作成功", "data": {"online": True}}), 200

    try:
        logger.info("🔍 [CheckOnline] 收到请求")
        logger.debug("[CheckOnline] Headers: %s", headers)

        response = requests.get(
            CLOUD_CHECK_URL,
            headers=headers,
            timeout=proxy.config.get("timeout", 30)
        )
        logger.debug("[CheckOnline] 云端响应状态码: %s", response.status_code)
        logger.debug("[CheckOnline] 云端响应内容: %s", response.text)

        if response.status_code == 200:
            logger.info("✅ 在线检查通过 - by cloud")
        else:
            logger.warning("❌ 在线检查失败 - by cloud")

        return Response(
            response.content,
            status=response.status_code,
            content_type=response.headers.get('Content-Type', 'application/json')
        )

    except Exception as e:
        logger.error("[CheckOnline] 请求云端失败: %s", str(e))
        return jsonify({"code": 500, "msg": "checkOnline failed"}), 500


@app.route('/health', methods=['GET'])
def health_check():

    return jsonify({
        "status": "healthy",
        "service": "huiying-proxy-enhanced-fixed",
        "version": "2.5.0",
        "timestamp": datetime.now().isoformat(),
        "features": ["http_polling", "task_status", "message_queue", "enhanced_progress", "upload_progress", "mask_support"]
    })


CLOUD_AUTH_URL = "https://umanage.lightcc.cloud/prod-api/auth/login"

CLOUD_LOGOUT_URL = "https://umanage.lightcc.cloud/prod-api/auth/logout"
@app.route('/auth/login', methods=['POST'])
def login_compatible():
    data = request.get_json(silent=True)
    if data is None:
        data = request.form.to_dict()
    username = data.get("username")
    password = data.get("password")

    logger.info("🔑 [Login] 收到登录请求，用户名: %s", username)

    user = LOCAL_USERS.get(username)
    if user:
        logger.info("[Login] 在本地用户列表中找到用户 %s", username)
    else:
        logger.info("[Login] 本地用户列表中未找到用户 %s", username)

    if user and user.get("password") == password:
        token = uuid.uuid4().hex
        sessions[token] = username
        logger.info("✅ [Login] 本地认证成功")
        return jsonify({
            "code": 200,
            "msg": "操作成功",
            "data": {
                "scope": None,
                "openid": None,
                "access_token": token,
                "refresh_token": None,
                "expire_in": 604799,
                "refresh_expire_in": None,
                "client_id": data.get("clientId")
            }
        }), 200

    if user:
        logger.warning("[Login] 本地密码不匹配")
    else:
        logger.info("[Login] 本地认证失败，尝试云端登录")

    try:
        response = requests.post(
            CLOUD_AUTH_URL,
            json=data,
            timeout=proxy.config.get("timeout", 30)
        )
        logger.info("[Login] 云端返回状态码: %s", response.status_code)
        logger.debug("[Login] 云端返回内容: %s", response.text)

        if response.status_code == 200:
            logger.info("✅ [Login] 云端登录成功")
        else:
            logger.warning("❌ [Login] 云端登录失败")

        return Response(
            response.content,
            status=response.status_code,
            content_type=response.headers.get('Content-Type', 'application/json')
        )
    except requests.RequestException as e:
        logger.error("[Login] 云端请求异常: %s", str(e))
        return jsonify({"code": 1, "msg": "request to cloud failed"}), 500

    
@app.route('/auth/logout', methods=['POST'])
def logout_proxy():
    token = request.headers.get('Authorization', '').replace('Bearer ', '')
    logger.info("🔑 [Logout] 收到退出请求，token: %s", token)
    if token in sessions:
        user = sessions.pop(token)
        logger.info("✅ [Logout] 本地退出成功, 用户: %s", user)
        return jsonify({"code": 200, "msg": "操作成功", "data": None}), 200
    else:
        logger.info("[Logout] 本地会话不存在，尝试云端登出")

    try:
        payload = request.get_data()
        headers = {
            key: value for key, value in request.headers.items()
            if key.lower() != 'host'
        }
        response = requests.post(
            CLOUD_LOGOUT_URL,
            headers=headers,
            data=payload,
            timeout=proxy.config.get("timeout", 30)
        )
        logger.info("[Logout] 云端返回状态码: %s", response.status_code)
        logger.debug("[Logout] 云端返回内容: %s", response.text)
        try:
            result = response.json()
        except Exception:
            result = {"code": 500, "msg": "云端响应格式错误", "raw": response.text}
        if response.status_code == 200:
            logger.info("✅ [Logout] 退出成功 - by cloud")
        else:
            logger.warning("❌ [Logout] 退出失败 - by cloud")
        return jsonify(result), response.status_code
    except Exception as e:
        logger.error("[Logout] 云端请求异常: %s", str(e))
        return jsonify({"code": 500, "msg": "代理登出失败", "error": str(e)}), 500

from gevent.pywsgi import WSGIServer
from geventwebsocket.handler import WebSocketHandler
if __name__ == '__main__':

    # monkey.patch_all() has already been called at the top of the file
    import logging
    

    logger.info("🔧 启动 ComfyUI WebSocket 监听线程...")
    logger.info("🔄 已使用增强HTTP轮询模式")
    comfy_ws_listener()

    logger.info("🔧 启动清理任务线程服务")
    Thread(target=cleanup_task, daemon=True).start()
    

    port = proxy.config.get('proxy_port', 8080)
    server = WSGIServer(
        ("0.0.0.0", port),
        app,
        handler_class=WebSocketHandler,
        log=None
    )
    logger.info("✅ HTTP & WebSocket 服务启动成功")
    logger.info(f"🟢 代理服务启动，监听端口: {port}")
    logger.info(
        f"✅ 工作流映射配置加载完成，工作流数量: {len(proxy.mappings.get('workflow_mappings', {}))}"
    )
    print("============== 欢迎使用绘影 AICG 代理终端服务 v2.5  ==============")

    server.serve_forever()

