import os
import smtplib
from email.mime.text import MIMEText
import threading
import time
from datetime import datetime, timedelta
import logging
import gc

try:
    import ctypes
except Exception:  # pragma: no cover - platform dependent
    ctypes = None

from collector import fetch_latest_all, fetch_disks, DeviceConfig  # type: ignore

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("alerter")

ALERT_EMAIL = os.getenv("ALERT_EMAIL", "")

# SMTP configuration from environment variables.
SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "465"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")

# DRY RUN: keep all alert checks/states, but do NOT actually send emails.
# Set env `ALERT_DRY_RUN=1` to simulate, otherwise default to real email sending.
DRY_RUN = os.getenv("ALERT_DRY_RUN", "0").strip() == "1"
ALERT_INTERVAL_SECONDS = max(30, int(os.getenv("ALERT_INTERVAL_SECONDS", "60")))
ALERT_FETCH_WORKERS = max(1, int(os.getenv("ALERT_FETCH_WORKERS", "1")))

OFFLINE_ALERT_STAGES = [
    (60, "1 minute"),
    (5 * 3600, "5 hours"),
    (24 * 3600, "1 day"),
]


class AlertManager:
    def __init__(self, registry):
        self.registry = registry
        self.lock = threading.Lock()
        
        # State tracking arrays for conditions
        self.offline_since = {}
        self.offline_alerted = set()
        self.offline_alert_stages_sent = {}
        self.reconnect_attempts = {}
        self.last_known_state = {}
        
        self.high_mem_since = {}
        self.high_mem_alerted = set()
        self._libc = None
        if ctypes is not None:
            try:
                self._libc = ctypes.CDLL("libc.so.6")
            except Exception:
                self._libc = None

    def _trim_memory(self) -> None:
        gc.collect()
        if self._libc is not None:
            try:
                self._libc.malloc_trim(0)
            except Exception:
                pass

    def send_email(self, subject: str, body: str, offline_minutes: int = None):
        if DRY_RUN:
            logger.warning(f"[DRY_RUN] Simulated alert: {subject} -> {ALERT_EMAIL}")
            try:
                with open("email_logs.txt", "a", encoding="utf-8") as f:
                    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    offline_str = f" [Detected Offline: {offline_minutes} mins]" if offline_minutes is not None else ""
                    f.write(
                        f"[{now_str}] SIMULATED EMAIL{offline_str}\nSubject: {subject}\nBody: {body}\n{'-'*50}\n"
                    )
            except Exception as e:
                logger.error(f"Failed to write simulated email log: {e}")
            return

        if not ALERT_EMAIL or not SMTP_HOST or not SMTP_USER or not SMTP_PASS:
            logger.warning(f"[STUB] Alert Triggered (SMTP not configured): {subject} -> {ALERT_EMAIL}")
            return
            
        try:
            msg = MIMEText(body, "plain", "utf-8")
            msg["Subject"] = subject
            msg["From"] = SMTP_USER
            msg["To"] = ALERT_EMAIL

            if SMTP_PORT == 465:
                # SSL connection
                server = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=10)
            else:
                # TLS connection
                server = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10)
                server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            server.send_message(msg)
            server.quit()
            logger.info(f"Email sent successfully to {ALERT_EMAIL} - Subject: {subject}")
            
            # Local Logging
            try:
                with open("email_logs.txt", "a", encoding="utf-8") as f:
                    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    offline_str = f" [Detected Offline: {offline_minutes} mins]" if offline_minutes is not None else ""
                    f.write(f"[{now_str}] SENT EMAIL{offline_str}\nSubject: {subject}\nBody: {body}\n{'-'*50}\n")
            except Exception as e:
                logger.error(f"Failed to write email log: {e}")
                
        except Exception as e:
            logger.error(f"Failed to send email to {ALERT_EMAIL}: {e}")

    def run_loop(self):
        # Initial sleep to let the server start up properly before aggressive polling
        time.sleep(5)
        while True:
            try:
                self.check_all()
            except Exception as e:
                logger.error(f"Error in background alert loop: {e}")
            finally:
                self._trim_memory()
            time.sleep(ALERT_INTERVAL_SECONDS)

    def check_all(self):
        devices = self.registry.list_devices()
        if not devices:
            return
            
        now = datetime.now()
        
        # 1. Fetch latest core metrics for offline and memory checks
        latest_data = fetch_latest_all(devices, max_workers=ALERT_FETCH_WORKERS)
        for data in latest_data:
            dev_id = data["device_id"]
            dev_name = data["device_name"]
            
            # --- Condition 1: Offline for >= 1 minute (6 interval points) ---
            is_offline = False

            file_age_seconds = data.get("file_age_seconds")
            if data.get("error"):
                is_offline = True
            elif file_age_seconds is not None:
                # Prefer mtime-based age check to avoid timestamp timezone/skew issues.
                try:
                    age = float(file_age_seconds)
                except Exception:
                    age = None
                if age is not None and age >= 60:
                    is_offline = True
                else:
                    if data.get("latest"):
                        self.last_known_state[dev_id] = data["latest"]
            else:
                # Fallback to timestamp comparison (original behavior).
                if not data.get("latest"):
                    is_offline = True
                else:
                    try:
                        ts_str = data["latest"]["timestamp"]
                        tsStr = ts_str.replace(" ", "T")
                        if len(tsStr) >= 19:
                            ts = datetime.strptime(tsStr[:19], "%Y-%m-%dT%H:%M:%S")
                        else:
                            ts = datetime.strptime(tsStr, "%Y-%m-%dT%H:%M:%S")
                        
                        remote_now_str = data.get("remote_now")
                        if remote_now_str:
                            rnStr = remote_now_str.replace(" ", "T")
                            if len(rnStr) >= 19:
                                remote_now = datetime.strptime(rnStr[:19], "%Y-%m-%dT%H:%M:%S")
                            else:
                                remote_now = datetime.strptime(rnStr, "%Y-%m-%dT%H:%M:%S")
                        else:
                            remote_now = now
                        
                        if (remote_now - ts).total_seconds() >= 60:
                            is_offline = True
                        else:
                            self.last_known_state[dev_id] = data["latest"]
                    except:
                        pass
            
            if is_offline:
                if dev_id not in self.offline_since:
                    self.offline_since[dev_id] = now
                    self.reconnect_attempts[dev_id] = 1
                    self.offline_alert_stages_sent[dev_id] = set()
                else:
                    self.reconnect_attempts[dev_id] += 1
                    
                duration = (now - self.offline_since[dev_id]).total_seconds()
                sent_stages = self.offline_alert_stages_sent.setdefault(dev_id, set())
                stage_to_send = None
                for threshold_seconds, stage_label in OFFLINE_ALERT_STAGES:
                    if duration >= threshold_seconds and threshold_seconds not in sent_stages:
                        stage_to_send = (threshold_seconds, stage_label)
                        break
                if stage_to_send is not None:
                    threshold_seconds, stage_label = stage_to_send
                    state = self.last_known_state.get(dev_id, {})
                    
                    cpu_model = state.get("cpu_model", "Unknown")
                    cpu_cores = state.get("cpu_cores", "?")
                    cpu_percent = state.get("cpu_percent", 0.0)
                    cpu_temp = state.get("cpu_temp_c", 0.0)
                    
                    gpu_name = state.get("gpu_name", "None")
                    gpu_percent = state.get("gpu_percent", 0.0)
                    gpu_mem_used_gb = (state.get("gpu_mem_used") or 0) / 1024 / 1024 / 1024
                    gpu_mem_total_gb = (state.get("gpu_mem_total") or 0) / 1024 / 1024 / 1024
                    
                    mem_percent = state.get("memory_percent", 0.0)
                    mem_used_gb = (state.get("memory_used") or 0) / 1024 / 1024 / 1024
                    mem_total_gb = (state.get("memory_total") or 0) / 1024 / 1024 / 1024
                    
                    disk_r = (state.get("disk_read_rate") or 0) / 1024 / 1024
                    disk_w = (state.get("disk_write_rate") or 0) / 1024 / 1024
                    
                    net_tx = (state.get("net_tx_rate") or 0) / 1024
                    net_rx = (state.get("net_rx_rate") or 0) / 1024
                    
                    top_user = state.get("top_process_user", {})
                    if isinstance(top_user, dict):
                        uname = top_user.get("name") or top_user.get("user") or "Unknown"
                        if top_user.get("cpu_percent") is not None:
                            user_str = f"{uname} ({top_user['cpu_percent']}% CPU)"
                        elif top_user.get("processes") is not None:
                            user_str = f"{uname} ({top_user['processes']} proc)"
                        else:
                            user_str = str(uname)
                    else:
                        user_str = "Unknown"
                        
                    last_ts = str(state.get("timestamp", "Unknown")).replace("T", " ")
                    
                    body = f"""⚠️ 【严重告警】节点 [{dev_name}] 已与总控端失去联系！

持续失联时间：已断开 {int(duration // 60)} 分钟
自动重连情况：自断开以来，主控端已自动发起重试 {self.reconnect_attempts[dev_id]} 次，最新尝试于 {now.strftime("%Y-%m-%d %H:%M:%S")}，均连接失败。

该节点在失联前（{last_ts}）的最后已知性能状态如下：
--------------------------------------------------
💻 处理器 (CPU)：
 - 型号：{cpu_model} ({cpu_cores} Cores)
 - 利用率：{cpu_percent}%
 - 温度：{cpu_temp} °C

🎮 显卡 (GPU)：
 - 型号：{gpu_name}
 - 利用率：{gpu_percent}%
 - 显存：{gpu_mem_used_gb:.1f}GB / {gpu_mem_total_gb:.1f}GB

🧠 内存 (RAM)：
 - 利用率：{mem_percent}% (占用 {mem_used_gb:.1f} GB / 总计 {mem_total_gb:.1f} GB)

💾 磁盘 I/O (Disk)：
 - 读写率：R {disk_r:.2f} MB/s | W {disk_w:.2f} MB/s

🌐 网络 (Network)：
 - 收发率：TX {net_tx:.2f} KB/s | RX {net_rx:.2f} KB/s

🧑‍💻 进程与用户：
 - 占用最高用户：{user_str}

如果该节点长时间未恢复，请检查物理机电源、网络连接或守护脚本状态。
"""
                    self.send_email(
                        f"⚠️ CRITICAL: 节点 {dev_name} 失联告警 ({stage_label})", 
                        body,
                        offline_minutes=int(duration // 60)
                    )
                    sent_stages.add(threshold_seconds)
                    self.offline_alerted.add(dev_id)
            else:
                # Device recovered
                if dev_id in self.offline_since:
                    del self.offline_since[dev_id]
                if dev_id in self.reconnect_attempts:
                    del self.reconnect_attempts[dev_id]
                if dev_id in self.offline_alert_stages_sent:
                    del self.offline_alert_stages_sent[dev_id]
                    
                if dev_id in self.offline_alerted:
                    self.send_email(
                        f"✅ RECOVERED: 节点 {dev_name} 已恢复上线", 
                        f"节点 {dev_name} ({dev_id}) 现已重新连接并恢复数据上报。"
                    )
                    self.offline_alerted.remove(dev_id)
                    
            # --- Condition 3: Memory >= 95% lasting for 5 minutes ---
            if not is_offline and data.get("latest"):
                mem = data["latest"].get("memory_percent", 0)
                if mem >= 95.0:
                    if dev_id not in self.high_mem_since:
                        self.high_mem_since[dev_id] = now
                    else:
                        duration = (now - self.high_mem_since[dev_id]).total_seconds()
                        if duration >= 300 and dev_id not in self.high_mem_alerted:
                            self.send_email(
                                f"⚠️ WARNING: {dev_name} Memory Critically High", 
                                f"Device {dev_name} ({dev_id}) memory utilization has remained >= 95% continuously for the past 5 minutes (Current: {mem}%)."
                            )
                            self.high_mem_alerted.add(dev_id)
                else:
                    if dev_id in self.high_mem_since:
                        del self.high_mem_since[dev_id]
                    if dev_id in self.high_mem_alerted:
                        self.send_email(
                            f"✅ RECOVERED: {dev_name} Memory Normal", 
                            f"Device {dev_name} ({dev_id}) memory is now below 95% (Current: {mem}%)."
                        )
                        self.high_mem_alerted.remove(dev_id)

def start_alerter(registry):
    manager = AlertManager(registry)
    t = threading.Thread(target=manager.run_loop, daemon=True)
    t.start()
    logger.info("Background Email Alert Monitor has been started.")
    return manager
