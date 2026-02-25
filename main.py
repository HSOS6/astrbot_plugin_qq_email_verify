import asyncio
import random
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr
from typing import Dict, Any, Optional

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
import astrbot.api.message_components as Comp

@register("qq_email_verify", "5060ti个马力的6999", "入群验证但是邮箱", "1.0beta", "https://github.com/User/astrbot_plugin_qq_email_verify")
class QQEmailVerifyPlugin(Star):
    def __init__(self, context: Context, config: Dict[str, Any]):
        super().__init__(context)
        self.config = config
        # pending_verifications structure:
        # {
        #   "user_id": {
        #       "group_id": str,
        #       "codes": set,  # Modified: store multiple valid codes
        #       "task": asyncio.Task
        #   }
        # }
        self.pending_verifications: Dict[str, Dict[str, Any]] = {}

    def _generate_code(self) -> str:
        """生成6位随机数字验证码"""
        return str(random.randint(100000, 999999))

    def _send_email_sync(self, to_email: str, subject: str, html_body: str) -> bool:
        """同步发送邮件逻辑"""
        smtp_host = self.config.get("smtp_host", "smtp.qq.com")
        smtp_port = int(self.config.get("smtp_port", 465))
        username = self.config.get("username", "")
        password = self.config.get("password", "")
        use_ssl = self.config.get("use_ssl", True)
        from_addr = self.config.get("from_address", "")
        from_name = self.config.get("from_display_name", "AstrBot验证助手")

        if not username or not password or not from_addr:
            logger.error("[QQEmailVerify] SMTP配置不完整，无法发送邮件")
            return False

        try:
            msg = EmailMessage()
            msg["Subject"] = subject
            msg["From"] = formataddr((from_name, from_addr))
            msg["To"] = to_email
            msg.set_content("请使用支持HTML的邮件客户端查看验证码。")
            msg.add_alternative(html_body, subtype="html")

            context = ssl.create_default_context()
            if use_ssl:
                with smtplib.SMTP_SSL(smtp_host, smtp_port, context=context) as server:
                    server.login(username, password)
                    server.send_message(msg)
            else:
                with smtplib.SMTP(smtp_host, smtp_port) as server:
                    server.ehlo()
                    server.starttls(context=context) # 尝试启用TLS
                    server.ehlo()
                    server.login(username, password)
                    server.send_message(msg)
            return True
        except Exception as e:
            logger.error(f"[QQEmailVerify] 发送邮件失败: {e}")
            return False

    async def _send_email_async(self, to_email: str, code: str, group_name: str, group_id: str):
        """异步发送验证邮件"""
        subject = self.config.get("verify_email_subject", "入群验证码")
        template = self.config.get("verify_email_template", "<p>欢迎加入 {group_name} ({group_id})！</p><p>验证码: {code}</p>")
        
        # 计算超时时间(分钟)
        timeout_min = str(int(self.config.get("kick_delay_seconds", 300)) // 60)
        
        html_body = template.replace("{code}", code).replace("{group_name}", group_name).replace("{group_id}", group_id).replace("{timeout}", timeout_min)
        
        logger.info(f"[QQEmailVerify] 正在向 {to_email} 发送验证码 {code}")
        success = await asyncio.to_thread(self._send_email_sync, to_email, subject, html_body)
        if success:
            logger.info(f"[QQEmailVerify] 邮件发送成功 -> {to_email}")
        else:
            logger.error(f"[QQEmailVerify] 邮件发送失败 -> {to_email}")

    async def _kick_task(self, user_id: str, group_id: str):
        """超时踢出任务"""
        delay = int(self.config.get("kick_delay_seconds", 300))
        try:
            await asyncio.sleep(delay)
            
            # 检查是否仍在待验证列表
            if user_id in self.pending_verifications:
                logger.info(f"[QQEmailVerify] 用户 {user_id} 验证超时，执行踢出")
                
                # 发送踢出提示
                kick_msg_tmpl = self.config.get("kick_msg_template", "{at_user} 验证超时，已移出群聊。")
                kick_msg = kick_msg_tmpl.replace("{at_user}", f"[CQ:at,qq={user_id}]")
                
                client = self.context.get_platform("aiocqhttp").get_client()
                if client:
                    try:
                        await client.call_action("send_group_msg", group_id=group_id, message=kick_msg)
                        # 执行踢出
                        await client.call_action("set_group_kick", group_id=group_id, user_id=int(user_id), reject_add_request=False)
                    except Exception as e:
                        logger.error(f"[QQEmailVerify] 踢出用户失败: {e}")
                
                # 清理状态
                if user_id in self.pending_verifications:
                    del self.pending_verifications[user_id]
                    
        except asyncio.CancelledError:
            logger.info(f"[QQEmailVerify] 用户 {user_id} 验证任务已取消（验证通过或离开）")
        except Exception as e:
            logger.error(f"[QQEmailVerify] 踢出任务异常: {e}")

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_event(self, event: AstrMessageEvent):
        """统一事件处理"""
        # 仅处理 aiocqhttp 平台
        if event.get_platform_name() != "aiocqhttp":
            return

        if not hasattr(event, "message_obj") or not hasattr(event.message_obj, "raw_message"):
            return
            
        raw = event.message_obj.raw_message
        if not isinstance(raw, dict):
            return

        post_type = raw.get("post_type")
        
        # 处理入群通知
        if post_type == "notice" and raw.get("notice_type") == "group_increase":
            user_id = str(raw.get("user_id"))
            group_id = str(raw.get("group_id"))
            
            # 忽略机器人自己
            if user_id == str(event.get_self_id()):
                return

            logger.info(f"[QQEmailVerify] 监测到新成员入群: {user_id} (群: {group_id})")
            
            # 生成验证码
            code = self._generate_code()
            email = f"{user_id}@qq.com"
            
            # 启动超时任务
            task = asyncio.create_task(self._kick_task(user_id, group_id))
            
            # 记录状态
            self.pending_verifications[user_id] = {
                "group_id": group_id,
                "codes": {code},
                "task": task
            }
            
            # 获取群名
            group_name = group_id
            try:
                g_info = await event.bot.call_action("get_group_info", group_id=int(group_id), no_cache=True)
                group_name = g_info.get("group_name", group_id)
            except Exception as e:
                logger.warning(f"[QQEmailVerify] 获取群信息失败: {e}")

            # 发送邮件
            asyncio.create_task(self._send_email_async(email, code, group_name, group_id))
            
            # 发送群提示
            timeout_min = int(self.config.get("kick_delay_seconds", 300)) // 60
            welcome_tmpl = self.config.get("welcome_msg_template", "{at_user} 欢迎入群！验证码已发送至您的QQ邮箱...")
            welcome_msg = welcome_tmpl.replace("{at_user}", f"[CQ:at,qq={user_id}]").replace("{timeout}", str(timeout_min))
            await event.bot.call_action("send_group_msg", group_id=group_id, message=welcome_msg)
            
        # 处理退群通知 (清理状态)
        elif post_type == "notice" and raw.get("notice_type") == "group_decrease":
            user_id = str(raw.get("user_id"))
            if user_id in self.pending_verifications:
                self.pending_verifications[user_id]["task"].cancel()
                del self.pending_verifications[user_id]
                logger.info(f"[QQEmailVerify] 待验证用户 {user_id} 退群，清理状态")

        # 处理群消息 (验证)
        elif post_type == "message" and raw.get("message_type") == "group":
            user_id = str(raw.get("user_id"))
            group_id = str(raw.get("group_id"))
            
            # 检查是否在待验证列表
            if user_id not in self.pending_verifications:
                return
                
            verification = self.pending_verifications[user_id]
            if verification["group_id"] != group_id:
                return
                
            msg_text = event.message_str.strip()
            
            # 简单的验证码匹配 logic
            if msg_text in verification["codes"]:
                # 验证成功
                logger.info(f"[QQEmailVerify] 用户 {user_id} 验证成功")
                
                # 取消超时任务
                verification["task"].cancel()
                del self.pending_verifications[user_id]
                
                # 发送成功提示
                success_tmpl = self.config.get("verify_success_msg", "{at_user} 验证通过，欢迎加入！")
                success_msg = success_tmpl.replace("{at_user}", f"[CQ:at,qq={user_id}]")
                await event.bot.call_action("send_group_msg", group_id=group_id, message=success_msg)
                
                # 停止事件继续传播 (可选，防止触发其他指令)
                event.stop_event()

    @filter.command("验证码", alias={"验证码重发"})
    async def resend_verify_code(self, event: AstrMessageEvent, email: str = ""):
        """重新发送验证码。用法: /验证码 [邮箱]"""
        user_id = str(event.get_sender_id())
        group_id = str(event.get_group_id())
        
        if user_id not in self.pending_verifications:
            # 仅在用户确实在待验证状态时响应，或者忽略
            # 为了避免干扰正常聊天，如果不在待验证列表，可以选择不回复或回复提示
            yield event.plain_result("您当前不需要验证。")
            return

        verification = self.pending_verifications[user_id]
        if verification["group_id"] != group_id:
            yield event.plain_result("请在申请入群的群聊中使用此指令。")
            return

        # 生成新验证码
        new_code = self._generate_code()
        verification["codes"].add(new_code)
        
        # 确定目标邮箱
        target_email = email.strip() if email else f"{user_id}@qq.com"
        
        # 获取群名
        group_name = group_id
        try:
            g_info = await event.bot.call_action("get_group_info", group_id=int(group_id), no_cache=True)
            group_name = g_info.get("group_name", group_id)
        except Exception:
            pass

        # 发送邮件
        asyncio.create_task(self._send_email_async(target_email, new_code, group_name, group_id))
        
        yield event.plain_result(f"已将新的验证码发送至 {target_email}，请查收。旧验证码依然有效。")
