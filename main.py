import os
import time
import glob
from typing import Dict, List, Optional, Tuple
from pathlib import Path

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.api import AstrBotConfig

class FileManagerPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        # 缓存结构：{session_id: {"timestamp": 时间戳, "files": [文件路径列表]}}
        self.file_cache: Dict[str, Dict] = {}
        # 确保监控文件夹存在
        self._ensure_watch_folder_exists()
        
    def _ensure_watch_folder_exists(self):
        """确保监控文件夹存在"""
        watch_folder = self.config.get("watch_folder", "./files")
        try:
            Path(watch_folder).mkdir(parents=True, exist_ok=True)
            logger.info(f"监控文件夹已就绪: {watch_folder}")
        except Exception as e:
            logger.error(f"创建监控文件夹失败: {e}")

    def _get_watch_folder(self) -> Path:
        """获取监控文件夹路径"""
        return Path(self.config.get("watch_folder", "./files"))

    def _get_allowed_extensions(self) -> List[str]:
        """获取允许的文件扩展名列表"""
        extensions_str = self.config.get("allowed_extensions", "")
        if not extensions_str:
            return []
        return [ext.strip().lower() for ext in extensions_str.split(",")]

    def _get_max_file_size(self) -> int:
        """获取最大文件大小限制（字节）"""
        return self.config.get("max_file_size_mb", 50) * 1024 * 1024

    def _scan_files(self) -> List[Path]:
        """扫描文件夹中的文件"""
        watch_folder = self._get_watch_folder()
        allowed_extensions = self._get_allowed_extensions()
        max_file_size = self._get_max_file_size()
        
        files = []
        
        try:
            # 递归扫描所有文件
            for file_path in watch_folder.rglob('*'):
                if file_path.is_file():
                    # 检查文件扩展名
                    if allowed_extensions:
                        file_ext = file_path.suffix.lower().lstrip('.')
                        if file_ext not in allowed_extensions:
                            continue
                    
                    # 检查文件大小
                    if max_file_size > 0 and file_path.stat().st_size > max_file_size:
                        logger.debug(f"文件过大被跳过: {file_path}")
                        continue
                    
                    files.append(file_path)
            
            # 按文件名排序
            files.sort(key=lambda x: x.name.lower())
            return files
            
        except Exception as e:
            logger.error(f"扫描文件夹失败: {e}")
            return []

    def _get_cached_files(self, session_id: str) -> Optional[List[Path]]:
        """获取缓存的文件列表"""
        cache_data = self.file_cache.get(session_id)
        if not cache_data:
            return None
            
        cache_time = self.config.get("max_cache_time", 300)
        if time.time() - cache_data["timestamp"] > cache_time:
            # 缓存过期
            del self.file_cache[session_id]
            return None
            
        return cache_data["files"]

    def _cache_files(self, session_id: str, files: List[Path]):
        """缓存文件列表"""
        self.file_cache[session_id] = {
            "timestamp": time.time(),
            "files": files
        }

    def _format_file_list(self, files: List[Path], base_path: Path) -> str:
        """格式化文件列表为可读字符串"""
        if not files:
            return "📁 文件夹中没有找到任何文件。"
        
        result = "📁 文件列表：\n\n"
        for i, file_path in enumerate(files, 1):
            # 计算相对路径
            try:
                relative_path = file_path.relative_to(base_path)
            except ValueError:
                relative_path = file_path
                
            file_size = file_path.stat().st_size
            size_str = self._format_file_size(file_size)
            result += f"{i:2d}. {relative_path} ({size_str})\n"
        
        result += f"\n💡 使用 `/sendfile 编号` 发送文件，例如：`/sendfile 1`"
        return result

    def _format_file_size(self, size_bytes: int) -> str:
        """格式化文件大小"""
        for unit in ['B', 'KB', 'MB', 'GB']:
            if size_bytes < 1024.0:
                return f"{size_bytes:.1f} {unit}"
            size_bytes /= 1024.0
        return f"{size_bytes:.1f} TB"

    async def _send_file(self, event: AstrMessageEvent, file_path: Path, file_name: str = None):
        """统一的文件发送方法"""
        if file_name is None:
            file_name = file_path.name
            
        try:
            # 获取文件大小
            file_size = os.path.getsize(file_path) / (1024 * 1024)  # 转换为MB
            
            # 文件大小警告
            if file_size > 90:
                yield event.plain_result(
                    f"⚠️ 文件大小为 {file_size:.2f}MB，超过建议的90MB，可能无法发送"
                )

            # 检查平台是否为 aiocqhttp
            if event.get_platform_name() == "aiocqhttp" and event.get_group_id():
                logger.info("检测到aiocqhttp平台和群组ID，尝试直接调用API发送群文件")
                try:
                    from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
                        AiocqhttpMessageEvent,
                    )

                    if isinstance(event, AiocqhttpMessageEvent):
                        client = event.bot
                        group_id = event.get_group_id()
                        
                        # 根据文件大小计算超时时间：基础60秒 + 每10MB额外30秒
                        timeout_seconds = 60 + int(file_size / 10) * 30
                        logger.info(f"上传 {file_size:.1f}MB 文件，设置超时时间: {timeout_seconds}秒")

                        # 使用平台API上传文件
                        import asyncio
                        upload_result = await asyncio.wait_for(
                            client.upload_group_file(
                                group_id=group_id, file=str(file_path), name=file_name
                            ),
                            timeout=timeout_seconds,
                        )
                        logger.info(f"aiocqhttp upload_group_file result: {upload_result}")
                        logger.info(f"已调用 aiocqhttp upload_group_file API 上传文件 {file_name} 到群组 {group_id}")
                        
                except asyncio.TimeoutError:
                    logger.error(f"上传文件超时（{timeout_seconds}秒），文件大小: {file_size:.1f}MB")
                    yield event.plain_result(
                        f"文件上传超时，文件过大({file_size:.1f}MB)，正在使用备用方式发送..."
                    )
                    # 回退到标准方法
                    from astrbot.api.message_components import File
                    yield event.chain_result([File(name=file_name, file=str(file_path))])
                    
                except Exception as api_e:
                    error_type = type(api_e).__name__
                    logger.error(f"调用 aiocqhttp API 发送文件失败({error_type}): {api_e}")
                    yield event.plain_result(
                        f"通过API发送文件失败({error_type})，正在使用备用方式..."
                    )
                    # 回退到标准方法
                    from astrbot.api.message_components import File
                    yield event.chain_result([File(name=file_name, file=str(file_path))])
                    
            else:
                # 非aiocqhttp平台或私聊，使用标准File组件
                logger.info("使用标准 File 组件发送方式")
                from astrbot.api.message_components import File
                yield event.chain_result([File(name=file_name, file=str(file_path))])

        except Exception as e:
            error_msg = str(e)
            logger.error(f"发送文件失败: {error_msg}")
            if "rich media transfer failed" in error_msg.lower():
                yield event.plain_result(
                    f"QQ富媒体传输失败，文件可能过大或格式不受支持。文件路径: {file_path}"
                )
                yield event.plain_result(
                    f"您可以手动从以下路径获取文件: {file_path}"
                )
            else:
                yield event.plain_result(f"发送文件失败: {error_msg}")

    @filter.command("listfiles")
    async def list_files(self, event: AstrMessageEvent):
        '''列出监控文件夹中的所有文件'''
        try:
            # 获取会话ID（区分不同用户/群组）
            session_id = event.unified_msg_origin
            
            # 尝试从缓存获取文件列表
            cached_files = self._get_cached_files(session_id)
            
            if cached_files is None:
                # 重新扫描文件
                files = self._scan_files()
                self._cache_files(session_id, files)
            else:
                files = cached_files
            
            base_path = self._get_watch_folder()
            file_list_text = self._format_file_list(files, base_path)
            
            yield event.plain_result(file_list_text)
            
        except Exception as e:
            logger.error(f"列出文件失败: {e}")
            yield event.plain_result("❌ 获取文件列表时发生错误，请检查文件夹配置。")

    @filter.command("sendfile")
    async def send_file(self, event: AstrMessageEvent, file_number: int):
        '''发送指定编号的文件'''
        try:
            session_id = event.unified_msg_origin
            
            # 获取缓存的文件列表
            files = self._get_cached_files(session_id)
            if files is None:
                yield event.plain_result("❌ 文件列表已过期，请先使用 `/listfiles` 刷新文件列表。")
                return
            
            # 检查编号是否有效
            if file_number < 1 or file_number > len(files):
                yield event.plain_result(f"❌ 文件编号无效，请输入 1-{len(files)} 之间的数字。")
                return
            
            file_path = files[file_number - 1]
            
            # 检查文件是否存在
            if not file_path.exists():
                yield event.plain_result("❌ 文件不存在或已被删除，请使用 `/listfiles` 刷新列表。")
                return
            
            # 检查文件大小
            max_size = self._get_max_file_size()
            file_size = file_path.stat().st_size
            if max_size > 0 and file_size > max_size:
                size_str = self._format_file_size(max_size)
                yield event.plain_result(f"❌ 文件过大，最大支持 {size_str}。")
                return
            
            # 发送文件
            logger.info(f"发送文件: {file_path}")
            async for result in self._send_file(event, file_path):
                yield result
            
        except ValueError:
            yield event.plain_result("❌ 请输入有效的数字编号。")
        except Exception as e:
            logger.error(f"发送文件失败: {e}")
            yield event.plain_result("❌ 发送文件时发生错误。")

    @filter.command("refreshfiles")
    async def refresh_files(self, event: AstrMessageEvent):
        '''强制刷新文件列表（忽略缓存）'''
        try:
            session_id = event.unified_msg_origin
            files = self._scan_files()
            self._cache_files(session_id, files)
            
            base_path = self._get_watch_folder()
            file_list_text = self._format_file_list(files, base_path)
            
            yield event.plain_result("🔄 文件列表已刷新！\n\n" + file_list_text)
            
        except Exception as e:
            logger.error(f"刷新文件列表失败: {e}")
            yield event.plain_result("❌ 刷新文件列表时发生错误。")

    @filter.command("fileinfo")
    async def file_info(self, event: AstrMessageEvent):
        '''显示文件夹信息'''
        try:
            watch_folder = self._get_watch_folder()
            files = self._scan_files()
            
            total_size = sum(f.stat().st_size for f in files)
            folder_info = (
                f"📊 文件夹信息：\n"
                f"📍 路径：{watch_folder}\n"
                f"📁 文件数量：{len(files)} 个\n"
                f"💾 总大小：{self._format_file_size(total_size)}\n"
                f"⏰ 缓存时间：{self.config.get('max_cache_time', 300)} 秒\n"
                f"📏 大小限制：{self.config.get('max_file_size_mb', 50)} MB\n"
            )
            
            yield event.plain_result(folder_info)
            
        except Exception as e:
            logger.error(f"获取文件夹信息失败: {e}")
            yield event.plain_result("❌ 获取文件夹信息时发生错误。")

@register("file_manager", "Assistant", "文件夹文件管理插件", "1.0.0")
class FileManager(FileManagerPlugin):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context, config)
