#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
绘影AICG代理服务器主程序

该代理服务器负责连接绘影AI前端与ComfyUI后端，提供以下核心功能：
- 用户认证与会话管理（支持本地和云端验证）
- 工作流加载、参数映射与合并处理
- 任务提交、进度跟踪与状态管理
- WebSocket通信与消息队列管理
- 客户端连接维护与资源自动清理

版本: 2.5.0
"""
import os
import json
import time
import uuid
import logging
from collections import defaultdict, deque
import requests
from flask import Flask, request, jsonify, Response, session
from flask_cors import CORS
import gevent
from gevent import monkey
from gevent.pywsgi import WSGIServer
from geventwebsocket.handler import WebSocketHandler
import threading

monkey.patch_all()

# 配置日志系统
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# 用户数据文件路径（存储本地认证用户信息）
USERS_FILE = "users.json"
# 本地用户缓存: {username: {password, ...}} - 加载自USERS_FILE
LOCAL_USERS = {}
# 活跃会话存储: {token: username} - 用于验证用户在线状态
# 键为认证令牌，值为用户名
sessions = {}

# 记录每个用户的最新 token
user_latest_token = {}
# 记录每个用户的最新 headers
user_latest_headers = {}

def load_local_users():
    """
    加载本地用户数据到LOCAL_USERS全局变量
    从USERS_FILE指定的JSON文件读取用户信息，支持本地认证
    如果文件不存在或读取失败，将初始化空用户字典
    
    返回:
        dict: 加载成功的用户字典，格式为{username: {password, ...}}
    """
    global LOCAL_USERS
    try:
        if os.path.exists(USERS_FILE):
            with open(USERS_FILE, 'r', encoding='utf-8') as f:
                # users.json 结构为 {"users": {username: info}}
                LOCAL_USERS = json.load(f).get("users", {})
                logger.info(
                    f"📦 已加载本地用户: {list(LOCAL_USERS.keys())}"
                )
        else:
            logger.warning(f"⚠️ 用户文件不存在: {USERS_FILE}")
            LOCAL_USERS = {}
    except Exception as e:
        logger.error(f"❌ 加载本地用户失败: {e}")
        LOCAL_USERS = {}
    return LOCAL_USERS


def save_local_users():
    """将当前 LOCAL_USERS 写回 USERS_FILE"""
    try:
        with open(USERS_FILE, "w", encoding="utf-8") as f:
            json.dump({"users": LOCAL_USERS}, f, indent=4, ensure_ascii=False)
            logger.info("💾 本地用户数据已保存")
    except Exception as e:
        logger.error(f"❌ 保存本地用户失败: {e}")

# 初始化本地用户数据将在日志系统初始化后执行

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
# 消息队列管理 - 用于在客户端和服务器间传递实时消息
# 数据结构: {client_id: deque([message1, message2, ...])}
message_queue = defaultdict(deque)
# 客户端最后活动时间 - 用于清理非活跃连接
# 数据结构: {client_id: timestamp}
client_last_seen = {}
# 任务状态跟踪 - 记录工作流执行进度
# 数据结构: {prompt_id: {type, data, timestamp, enhanced}}
task_status = {}
# 线程锁 - 确保消息队列操作的线程安全
broadcast_lock = threading.Lock()
queue_lock = threading.Lock()

def start_progress_tracker_by_mapping(prompt_id, workflow_id, client_id, comfyui_url):
    """
    初始化工作流进度跟踪器
    根据工作流映射配置启动进度跟踪，监控任务执行状态并向客户端推送更新
    
    参数:
        prompt_id (str): 任务ID，用于标识特定工作流执行实例
        workflow_id (str): 工作流模板ID
        client_id (str): 客户端ID，用于定向推送进度消息
        comfyui_url (str): ComfyUI服务地址
    """
    try:
        workflow_mappings = proxy.mappings.get('workflow_mappings', {})
        workflow_info = workflow_mappings.get(workflow_id, {})
        node_mappings = workflow_info.get('node_mappings', {})
        total_nodes = len(node_mappings)

        proxy.workflow_node_count[workflow_id] = total_nodes

        task_status[prompt_id] = {
            "type": "start",
            "data": {
                "prompt_id": prompt_id,
                "workflow_id": workflow_id,
                "client_id": client_id,
                "total_nodes": total_nodes,
                "completed_nodes": 0,
                "node_status": {}
            },
            "timestamp": time.time()
        }

        logger.info(f"📊 已初始化进度跟踪: {workflow_id} (节点总数: {total_nodes})")
    except Exception as e:
        logger.error(f"❌ 进度跟踪初始化失败: {e}")


def add_message_to_queue(client_id, message):
    """
    向指定客户端的消息队列添加消息，并添加时间戳和类型信息
    线程安全设计，使用queue_lock确保多线程环境下的数据一致性
    
    参数:
        client_id (str): 客户端唯一标识符
        message (dict): 要发送的消息字典，必须包含'type'字段
    """
    with queue_lock:
        if client_id not in message_queue:
            message_queue[client_id] = deque()
        
        # 增强消息内容，添加时间戳
        enhanced_message = {
            **message,
            "timestamp": time.time()
        }
        message_queue[client_id].append(enhanced_message)
        logger.info(f"📨 消息已添加到客户端队列: {client_id} (类型: {message.get('type', 'unknown')})")

def get_messages_for_client(client_id, since_timestamp=None):
    """
    获取客户端自指定时间戳以来的未读消息，并清理历史消息
    保留最近20条消息以优化内存使用
    
    参数:
        client_id (str): 客户端唯一标识符
        since_timestamp (float): 时间戳，仅返回此时间之后的消息
    返回:
        list: 消息列表，每条消息包含'timestamp'和原始内容
    """
    
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
    """
    ComfyUI WebSocket监听器
    建立与ComfyUI的WebSocket连接，接收实时执行状态消息并增强处理后转发给客户端
    支持的消息类型: status, executing, progress, executed
    
    消息增强: 添加进度百分比、节点ID、时间戳等额外信息，便于前端展示
    """
    import websocket
    import json
    import copy

    def enhance_message(original_message):
        """增强原始消息，添加额外元数据"""
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
            
            #  清理过期任务
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
        
        time.sleep(60)  # 每60秒执行一次

 
class HuiYingProxy:
    """
    绘影代理核心类，负责工作流管理、参数映射和ComfyUI通信
    
    该类协调配置加载、工作流处理和任务提交的全过程，支持本地和云端两种模式：
    - 本地模式：从本地文件系统加载配置和工作流
    - 云端模式：从远程服务器获取配置和工作流资源
    """
    def __init__(self, config_file='config.json', mappings_file='workflow_mappings.json'):
        """
        初始化HuiYingProxy实例
        
        参数:
            config_file (str): 配置文件路径，默认为'config.json'
            mappings_file (str): 工作流参数映射文件路径，默认为'workflow_mappings.json'
        """
        self.config_file = config_file
        self.mappings_file = mappings_file
        self.config = self.load_config(config_file)
        self.mappings = self.load_mappings(mappings_file)
        self.workflow_cache = {}  # 工作流缓存: {workflow_id: workflow_data}
        self.workflow_node_count = {}  # 工作流节点计数: {workflow_id: node_count}
        if self.config.get('load_from_cloud'):
            self.preload_workflows()

    def save_config(self):
        """
        将当前配置保存到磁盘
        确保配置更改持久化，用于保存用户设置和系统配置
        """
        try:
            with open(self.config_file, 'w', encoding='utf-8') as f:
                json.dump(self.config, f, indent=2, ensure_ascii=False)
            logger.info("💾 配置已保存")
        except Exception as e:
            logger.warning(f"⚠️ 配置保存失败: {e}")

    def load_config(self, config_file):
        """
        加载并合并配置文件
        从指定文件加载用户配置，与默认配置合并，处理路径标准化
        
        参数:
            config_file (str): 配置文件路径
        返回:
            dict: 合并后的完整配置字典
        """
        
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
    """
    客户端消息轮询接口
    供前端定期调用以获取新消息，支持增量获取（只返回指定时间戳后的消息）
    同时更新客户端最后活动时间，用于连接状态管理
    
    请求参数:
        clientId (str): 客户端唯一标识符，必需
        since (float): 可选，时间戳，只返回此时间之后的消息
    返回:
        json: 包含消息列表和服务器状态的响应
    """
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
    """
    获取任务状态接口
    查询指定prompt_id的工作流执行状态，包括进度、节点信息和时效性
    
    请求参数:
        prompt_id (str): 任务ID，从URL路径获取
    返回:
        json: 包含任务状态、执行信息和时效性的响应
    """
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

    # 校验 token 是否有效
    auth_header = request.headers.get("Authorization", "")
    token = auth_header.replace("Bearer ", "")
    username = sessions.get(token)
    if not username:
        logger.warning("❌ 提交被拒绝，用户未登录或 token 已失效")
        return jsonify({"code": 401, "msg": "未授权访问，请重新登录"}), 401

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
    """
    WebSocket代理接口
    建立客户端与服务器间的WebSocket连接，用于实时消息推送
    替代HTTP轮询，提供更高效的实时通信
    
    请求参数:
        clientId: 客户端唯一标识符
    返回:
        WebSocket连接: 双向通信通道
    """
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
    """
    在线状态检查接口（本地会话覆盖逻辑）
    如果同一用户后登录覆盖旧 token，旧 token 被踢出。
    """
    logger.info("🔍 [CheckOnline] 收到请求")
    headers = dict(request.headers)
    logger.debug(f"[CheckOnline] Headers: {headers}")

    auth_header = headers.get("Authorization", "")
    token = auth_header.replace("Bearer ", "").strip()
    username = sessions.get(token)
    logger.debug(f"[CheckOnline] 当前 token: {token}")
    logger.debug(f"[CheckOnline] 对应用户名: {username}")
    logger.debug(f"[CheckOnline] 当前 user_latest_token 状态: {user_latest_token}")

    if username:
        # 检查是否为该用户的最新 token
        latest_token = user_latest_token.get(username)
        if latest_token and latest_token != token:
            logger.warning(f"[CheckOnline] 非最新 token 登录尝试，拒绝访问: {token[:8]}...")
            return jsonify({"code": 401, "msg": "认证失败，无法访问系统资源", "data": None}), 401

        logger.info("✅ 在线检查通过 - local")
        return jsonify({"code": 200, "msg": "在线", "data": {"mode": "local", "user": username}})

    return jsonify({"code": 401, "msg": "认证失败", "data": None}), 401


@app.route('/health', methods=['GET'])
def health_check():
    """
    服务健康检查接口
    提供服务状态信息，用于监控系统检查服务可用性
    返回服务版本、状态和支持的功能列表
    
    返回:
        json: 包含服务状态和元数据的响应
    """
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

    # 每次登录时重新加载本地用户，确保用户信息最新
    load_local_users()
    user = LOCAL_USERS.get(username)
    if user:
        logger.info("[Login] 在本地用户列表中找到用户 %s", username)
    else:
        logger.info("[Login] 本地用户列表中未找到用户 %s", username)

    if user:
        if user.get("password") == password:
            # 清理旧 token 并更新为最新
            old_tokens = [t for t, u in sessions.items() if u == username]
            for t in old_tokens:
                del sessions[t]
                logger.info(f"🔄 旧 token 清除: {t[:8]}...")
            # 生成新token
            token = uuid.uuid4().hex
            sessions[token] = username
            user_latest_token[username] = token
            # 保存用户登录时的 headers，用于 checkOnline 校验使用
            user_latest_headers[username] = dict(request.headers)
            # 更新最后登录时间并保存
            user['last_login'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            LOCAL_USERS[username] = user
            save_local_users()
            logger.debug(f"[Login] 为用户 {username} 存储 token: {token}")
            logger.debug(f"[Login] 为用户 {username} 存储 headers: {user_latest_headers[username]}")
            logger.debug(f"[Login] 当前 user_latest_token 状态: {user_latest_token}")
            logger.info("✅ [Login] 本地认证成功 (已清理旧会话)")
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

        logger.warning("[Login] 本地密码不匹配")
        return jsonify({"code": 401, "msg": "密码错误"}), 401

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
            logger.info("✅ [Login] 云端请求成功")
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
    """
    用户登出接口
    支持本地会话清除和云端登出，优先清除本地会话
    本地会话不存在时转发登出请求至云端服务
    
    请求头:
        Authorization: Bearer {token} - 用户认证令牌
    返回:
        json: 登出结果响应
    """
    token = request.headers.get('Authorization', '').replace('Bearer ', '')
    logger.info(f"🔑 [Logout] 收到退出请求，token: {token}")
    if token in sessions:
        # 清除该用户的最新 token 记录
        username = sessions[token]
        del sessions[token]
        if username in user_latest_token:
            del user_latest_token[username]
        logger.info(f"✅ [Logout] 本地退出成功, 用户: {username}")
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

