"""
Mobile Approval Interface
Sends strategy proposals via Telegram and receives approval responses
"""

import os
import logging
import asyncio
from typing import Dict, Optional, Callable
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes
from dotenv import load_dotenv
from db_handler import get_db_handler

# Load environment variables
load_dotenv('config.env')

# Configure logging
logging.basicConfig(
    level=os.getenv('LOG_LEVEL', 'INFO'),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class MobileInterface:
    """Telegram bot interface for strategy approvals"""

    def __init__(self):
        self.token = os.getenv('TELEGRAM_BOT_TOKEN')
        self.chat_id = os.getenv('TELEGRAM_CHAT_ID')

        if not self.token:
            raise ValueError("TELEGRAM_BOT_TOKEN must be set in config.env")

        if not self.chat_id:
            logger.warning("TELEGRAM_CHAT_ID not set - bot will respond to any chat")

        self.app = Application.builder().token(self.token).build()
        self.db = get_db_handler()

        # Setup handlers
        self._setup_handlers()

        logger.info("Mobile interface initialized")

    def _setup_handlers(self):
        """Setup Telegram bot command and callback handlers"""
        # Command handlers
        self.app.add_handler(CommandHandler("start", self._start_command))
        self.app.add_handler(CommandHandler("help", self._help_command))
        self.app.add_handler(CommandHandler("status", self._status_command))

        # Callback handlers for inline buttons
        self.app.add_handler(CallbackQueryHandler(self._handle_callback))

        # Message handler for text responses
        self.app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_message))

    async def _start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /start command"""
        welcome_msg = """
🤖 **Gentube.ai Optimization Bot**

I'll send you optimization strategies for approval.

**Commands:**
/help - Show this help message
/status - Check system status

**How to respond:**
- Click 'Approve' or 'Reject' buttons
- Or reply with: 'Approve', 'Reject', or 'Tweak: <your suggestion>'
"""
        await update.message.reply_text(welcome_msg)

    async def _help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /help command"""
        await self._start_command(update, context)

    async def _status_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /status command"""
        try:
            pending = self.db.get_pending_strategies()
            alerts = self.db.get_unacknowledged_alerts()

            status_msg = f"""
📊 **System Status**

Pending Strategies: {len(pending)}
Unacknowledged Alerts: {len(alerts)}

System: Running ✅
"""
            await update.message.reply_text(status_msg)

        except Exception as e:
            logger.error(f"Error getting status: {e}")
            await update.message.reply_text("❌ Error getting system status")

    async def _handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle inline button callbacks"""
        query = update.callback_query
        await query.answer()

        try:
            # Parse callback data: "action:strategy_id"
            action, strategy_id = query.data.split(':')
            strategy_id = int(strategy_id)

            response_type = None
            response_text = None

            if action == 'approve':
                response_type = 'approve'
                response_text = 'Approved'
                await query.edit_message_text(
                    f"✅ Strategy {strategy_id} approved!\n\n{query.message.text}"
                )

            elif action == 'reject':
                response_type = 'reject'
                response_text = 'Rejected'
                await query.edit_message_text(
                    f"❌ Strategy {strategy_id} rejected.\n\n{query.message.text}"
                )

            # Log approval in database
            if response_type:
                self.db.write_approval(
                    strategy_id=strategy_id,
                    user_response=response_text,
                    response_type=response_type
                )
                self.db.update_strategy_status(
                    strategy_id,
                    'approved' if action == 'approve' else 'rejected'
                )

                logger.info(f"Strategy {strategy_id} {response_type} via callback")

        except Exception as e:
            logger.error(f"Error handling callback: {e}")
            await query.edit_message_text("❌ Error processing response")

    async def _handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle text message responses"""
        text = update.message.text.strip().lower()
        chat_id = update.message.chat_id

        # Check if this is the authorized chat (if configured)
        if self.chat_id and str(chat_id) != self.chat_id:
            logger.warning(f"Unauthorized chat attempt: {chat_id}")
            return

        try:
            # Get most recent pending strategy
            pending = self.db.get_pending_strategies()
            if not pending:
                await update.message.reply_text("No pending strategies to respond to.")
                return

            strategy = pending[0]
            strategy_id = strategy['id']

            # Parse response
            if text.startswith('approve'):
                response_type = 'approve'
                response_text = 'Approved'
                status = 'approved'
                msg = f"✅ Strategy {strategy_id} approved!"

            elif text.startswith('reject'):
                response_type = 'reject'
                response_text = 'Rejected'
                status = 'rejected'
                msg = f"❌ Strategy {strategy_id} rejected."

            elif text.startswith('tweak'):
                response_type = 'tweak'
                response_text = text
                status = 'pending'
                msg = f"📝 Strategy {strategy_id} - tweak requested. The system will adjust and resubmit."

            else:
                await update.message.reply_text(
                    "Please respond with:\n- 'Approve'\n- 'Reject'\n- 'Tweak: <your suggestion>'"
                )
                return

            # Log approval
            self.db.write_approval(
                strategy_id=strategy_id,
                user_response=response_text,
                response_type=response_type,
                notes=text if response_type == 'tweak' else None
            )
            self.db.update_strategy_status(strategy_id, status)

            await update.message.reply_text(msg)
            logger.info(f"Strategy {strategy_id} {response_type} via message")

        except Exception as e:
            logger.error(f"Error handling message: {e}")
            await update.message.reply_text("❌ Error processing response")

    async def send_proposal(self, strategy: Dict) -> bool:
        """
        Send a strategy proposal to the user via Telegram

        Args:
            strategy: Strategy dictionary with summary, changes, expected_impact

        Returns:
            True if sent successfully
        """
        if not self.chat_id:
            logger.error("TELEGRAM_CHAT_ID not configured")
            return False

        try:
            strategy_id = strategy['id']
            summary = strategy['summary']
            expected_impact = strategy.get('expected_impact', 'N/A')
            focus_area = strategy.get('focus_area', 'N/A')
            priority = strategy.get('priority', 0)

            # Parse changes
            changes = strategy.get('changes', [])
            if isinstance(changes, str):
                import json
                changes = json.loads(changes)

            changes_text = "\n\n".join([
                f"**{i+1}. {change.get('action', 'N/A')}**\n"
                f"Type: {change.get('type', 'N/A')}\n"
                f"Impact: {change.get('expected_impact', 'N/A')}\n"
                f"Risk: {change.get('risk_level', 'N/A')}\n"
                f"Effort: {change.get('effort', 'N/A')}"
                for i, change in enumerate(changes)
            ])

            message = f"""
🎯 **New Optimization Strategy #{strategy_id}**

**Summary:** {summary}

**Focus Area:** {focus_area}
**Priority:** {priority}/10
**Expected Impact:** {expected_impact}

**Proposed Changes:**
{changes_text}

**What should I do?**
"""

            # Create inline keyboard
            keyboard = [
                [
                    InlineKeyboardButton("✅ Approve", callback_data=f"approve:{strategy_id}"),
                    InlineKeyboardButton("❌ Reject", callback_data=f"reject:{strategy_id}")
                ]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)

            # Send message
            await self.app.bot.send_message(
                chat_id=self.chat_id,
                text=message,
                reply_markup=reply_markup
            )

            logger.info(f"Proposal sent for strategy {strategy_id}")
            return True

        except Exception as e:
            logger.error(f"Error sending proposal: {e}")
            return False

    async def send_alert(self, message: str, severity: str = 'medium') -> bool:
        """
        Send an alert message to the user

        Args:
            message: Alert message
            severity: Alert severity (low, medium, high, critical)

        Returns:
            True if sent successfully
        """
        if not self.chat_id:
            return False

        try:
            emoji = {
                'low': 'ℹ️',
                'medium': '⚠️',
                'high': '🚨',
                'critical': '🔥'
            }.get(severity, 'ℹ️')

            alert_msg = f"{emoji} **Alert**\n\n{message}"

            await self.app.bot.send_message(
                chat_id=self.chat_id,
                text=alert_msg
            )

            logger.info(f"Alert sent: {severity}")
            return True

        except Exception as e:
            logger.error(f"Error sending alert: {e}")
            return False

    async def send_status_update(self, message: str) -> bool:
        """
        Send a status update to the user

        Args:
            message: Status message

        Returns:
            True if sent successfully
        """
        if not self.chat_id:
            return False

        try:
            await self.app.bot.send_message(
                chat_id=self.chat_id,
                text=f"📢 {message}"
            )

            return True

        except Exception as e:
            logger.error(f"Error sending status update: {e}")
            return False

    def run_polling(self):
        """Run the bot in polling mode (blocking)"""
        logger.info("Starting Telegram bot in polling mode...")
        self.app.run_polling()

    async def run_webhook(self, webhook_url: str):
        """Run the bot in webhook mode (non-blocking)"""
        logger.info(f"Starting Telegram bot with webhook: {webhook_url}")
        await self.app.bot.set_webhook(webhook_url)
        await self.app.start()


# Singleton instance
_mobile_interface = None

def get_mobile_interface() -> MobileInterface:
    """Get or create mobile interface singleton"""
    global _mobile_interface
    if _mobile_interface is None:
        _mobile_interface = MobileInterface()
    return _mobile_interface


# Standalone mode
if __name__ == "__main__":
    interface = get_mobile_interface()
    interface.run_polling()
