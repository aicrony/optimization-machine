"""
Mobile Approval Interface
Sends strategy proposals via Telegram and receives approval responses
"""

import os
import logging
import asyncio
from typing import Dict, Optional, Callable, List
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes
from dotenv import load_dotenv
from db_handler import get_db_handler
from code_cache import get_code_cache
from strategy_engine import get_strategy_engine
from data_collector import DataCollector

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
        self.company_name = os.getenv('COMPANY_NAME', 'Gentube.ai')

        # Create a separate Bot instance for sending messages from the main thread
        # This avoids event loop conflicts when the Application is running in a background thread
        from telegram import Bot
        self._sender_bot = Bot(token=self.token)

        # State for saved strategy selection
        # Maps chat_id -> dict of {strategy_id: strategy} for strategies being shown
        self._saved_selection_state = {}

        # State for failed/rejected strategy selection (for re-approval)
        self._failed_selection_state = {}
        self._rejected_selection_state = {}

        # State for approved strategy selection
        self._approved_selection_state = {}

        # State for landing menu selection (used by loop_controller)
        # Maps chat_id -> selected action ('saved', 'failed', 'rejected', 'approved', 'restart', None)
        self._landing_menu_selection = {}

        # State for interactive Q&A troubleshooting sessions
        # Maps chat_id -> strategy_id when a troubleshooting session is active
        self._qa_session_active = {}

        # State for refinement sessions
        # Maps chat_id -> strategy_id when waiting for refinement text
        self._refine_session_active = {}

        # State for "Run as New Strategy" button context
        # Maps context_id (str) -> message text that should seed the new strategy
        self._strategy_context_messages = {}
        self._strategy_context_counter = 0

        # State for interactive coding questions
        # Maps question_id -> {'question': str, 'options': list, 'answer': str or None, 'answered': bool}
        self._coding_questions = {}
        self._coding_question_counter = 0

        # Context message to inject into the next strategy generation cycle
        self._strategy_generation_context = None

        # Track last conversational chat message per chat_id (for resend after provider switch)
        self._last_chat_message = {}

        # Track the currently focused strategy per chat_id (set when user views a strategy via /33 etc.)
        # This allows the chat agent to know which strategy the CEO is asking about
        self._focused_strategy = {}

        # Setup handlers
        self._setup_handlers()

        logger.info("Mobile interface initialized")

    # --- Telegram message chunking ---
    TELEGRAM_MAX_LENGTH = 4096

    @staticmethod
    def _split_message(text: str, max_length: int = 4096) -> list:
        """Split a message into chunks that fit within Telegram's limit.
        Splits on double-newlines first, then single newlines, preserving order."""
        if len(text) <= max_length:
            return [text]

        chunks = []
        current = ""
        # Split on double-newline to keep logical blocks together
        paragraphs = text.split("\n\n")
        for para in paragraphs:
            candidate = f"{current}\n\n{para}" if current else para
            if len(candidate) <= max_length:
                current = candidate
            else:
                if current:
                    chunks.append(current)
                # If a single paragraph exceeds the limit, split on newlines
                if len(para) > max_length:
                    lines = para.split("\n")
                    current = ""
                    for line in lines:
                        candidate = f"{current}\n{line}" if current else line
                        if len(candidate) <= max_length:
                            current = candidate
                        else:
                            if current:
                                chunks.append(current)
                            # If a single line exceeds the limit, hard-split
                            while len(line) > max_length:
                                chunks.append(line[:max_length])
                                line = line[max_length:]
                            current = line
                else:
                    current = para
        if current:
            chunks.append(current)
        return chunks

    def _extract_action_buttons(self, response: str, ctx_id: str) -> list:
        """Parse LLM response for strategy references and return dynamic inline buttons.

        Scans for #N patterns to create View buttons for each mentioned strategy,
        plus Approve buttons and navigation buttons when those actions are mentioned.
        Returns a list of button rows (each row is a list of InlineKeyboardButton).
        """
        import re
        buttons = []
        seen = set()

        response_lower = response.lower()

        # Find all strategy IDs mentioned (e.g. "#25", "#26")
        strategy_ids = sorted(set(re.findall(r'#(\d+)', response)))

        for sid in strategy_ids:
            # Check if "execute" or "exec" is mentioned near this strategy ID
            if re.search(rf'execut.*?#?{sid}|#{sid}.*?execut|exec.*?#?{sid}|#{sid}.*?exec', response_lower):
                cb = f'exec:{sid}'
                if cb not in seen:
                    buttons.append([InlineKeyboardButton(f'🚀 Execute Now #{sid}', callback_data=cb)])
                    seen.add(cb)

            # Always add a View button for each mentioned strategy (uses generic view handler)
            cb = f'view:{sid}'
            if cb not in seen:
                buttons.append([InlineKeyboardButton(f'🔍 View #{sid}', callback_data=cb)])
                seen.add(cb)

        # Navigation: "saved strategies" or /saved
        if re.search(r'saved.*?strateg|/saved', response_lower):
            buttons.append([InlineKeyboardButton('📋 View Saved Strategies', callback_data='menu:saved')])

        # Always include "Run as New Strategy"
        buttons.append([InlineKeyboardButton('🚀 Run as New Strategy', callback_data=f'run_as_strategy:{ctx_id}')])

        return buttons

    async def _send_chunked(self, bot, chat_id: int, text: str, reply_markup=None):
        """Send a long message as multiple chunks, with reply_markup only on the last one."""
        chunks = self._split_message(text)
        for i, chunk in enumerate(chunks):
            is_last = (i == len(chunks) - 1)
            await bot.send_message(
                chat_id=chat_id,
                text=chunk,
                reply_markup=reply_markup if is_last else None,
            )

    @staticmethod
    def _format_dependency_analysis(strategy: dict) -> str:
        """Format dependency_analysis from a strategy into a readable text block."""
        import json
        dep_data = strategy.get('dependency_analysis')
        if not dep_data:
            # Check inside data_snapshot
            snapshot = strategy.get('data_snapshot', {})
            if isinstance(snapshot, str):
                try:
                    snapshot = json.loads(snapshot)
                except (json.JSONDecodeError, TypeError):
                    snapshot = {}
            dep_data = snapshot.get('dependency_analysis') if isinstance(snapshot, dict) else None
        if not dep_data:
            return ""

        if isinstance(dep_data, str):
            try:
                dep_data = json.loads(dep_data)
            except (json.JSONDecodeError, TypeError):
                return ""

        lines = ["\n**Dependency Analysis:**"]

        deps = dep_data.get('dependencies', [])
        if deps:
            for d in deps:
                fr = d.get('from_change', '?')
                to = d.get('to_change', '?')
                rel = d.get('relationship', '?').replace('_', ' ')
                expl = d.get('explanation', '')
                lines.append(f"  Change {fr} → Change {to} ({rel}): {expl}")
        else:
            lines.append("  No dependencies between changes")

        independent = dep_data.get('independent_changes', [])
        if independent:
            lines.append(f"  Independent changes: {', '.join(str(c) for c in independent)}")

        order = dep_data.get('recommended_execution_order', [])
        if order:
            lines.append(f"  Recommended order: {' → '.join(str(c) for c in order)}")

        can_split = dep_data.get('can_split')
        if can_split is not None:
            lines.append(f"  Can split: {'Yes' if can_split else 'No'}")

        split_notes = dep_data.get('split_notes', '')
        if split_notes:
            lines.append(f"  Note: {split_notes}")

        return "\n".join(lines)

    @staticmethod
    def _build_id_buttons(strategy_ids: list, context: str) -> InlineKeyboardMarkup:
        """Build inline keyboard with clickable strategy ID buttons.

        Args:
            strategy_ids: List of strategy IDs to show as buttons
            context: Routing context ('saved', 'failed', 'rejected', 'approved')
        """
        # Arrange buttons in rows of 4
        buttons = [InlineKeyboardButton(f"#{sid}", callback_data=f"pick:{context}:{sid}") for sid in strategy_ids]
        rows = [buttons[i:i+4] for i in range(0, len(buttons), 4)]
        # Add back button on its own row
        rows.append([InlineKeyboardButton("⬅️ Back to Menu", callback_data="menu:back")])
        return InlineKeyboardMarkup(rows)

    def _setup_handlers(self):
        """Setup Telegram bot command and callback handlers"""
        logger.info("Setting up Telegram bot handlers...")

        # Command handlers
        self.app.add_handler(CommandHandler("start", self._start_command))
        self.app.add_handler(CommandHandler("help", self._help_command))
        self.app.add_handler(CommandHandler("status", self._status_command))
        self.app.add_handler(CommandHandler("ping", self._ping_command))
        self.app.add_handler(CommandHandler("saved", self._saved_command))
        self.app.add_handler(CommandHandler("failed", self._failed_command))
        self.app.add_handler(CommandHandler("rejected", self._rejected_command))
        self.app.add_handler(CommandHandler("approved", self._approved_command))
        self.app.add_handler(CommandHandler("restart", self._restart_command))
        self.app.add_handler(CommandHandler("resume", self._resume_command))
        self.app.add_handler(CommandHandler("llm", self._llm_command))
        self.app.add_handler(CommandHandler("churn", self._churn_command))
        logger.info("Command handlers registered: /start, /help, /status, /ping, /saved, /failed, /rejected, /approved, /restart (alias), /resume, /llm")

        # Callback handlers for inline buttons
        self.app.add_handler(CallbackQueryHandler(self._handle_callback))
        logger.info("Callback query handler registered")

        # Handler for numeric strategy shortcuts like /27
        self.app.add_handler(MessageHandler(filters.Regex(r'^/\d+$'), self._handle_message))
        logger.info("Numeric strategy shortcut handler registered")

        # Message handler for text responses (including replies)
        self.app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_message))
        logger.info("Text message handler registered")

        logger.info("All handlers setup complete")

    async def _start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /start command - show the landing menu"""
        chat_id = update.message.chat_id
        logger.info(f"/start received from chat_id: {chat_id}")

        # Clear any selection state and chat history, then show the landing menu
        self.clear_menu_selection(chat_id)
        self._saved_selection_state.pop(chat_id, None)
        self._failed_selection_state.pop(chat_id, None)
        self._rejected_selection_state.pop(chat_id, None)
        self._approved_selection_state.pop(chat_id, None)
        self.db.clear_chat_history(str(chat_id))

        await self._send_landing_menu_inline(update)

    async def _llm_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /llm command - show LLM provider selection"""
        engine = get_strategy_engine()
        current_provider = engine.llm_provider
        providers = engine.available_providers

        buttons = []
        for p in providers:
            label = f"✅ {p}" if p == current_provider else p
            buttons.append([InlineKeyboardButton(label, callback_data=f"llm_provider:{p}")])

        await update.message.reply_text(
            f"🧠 **LLM Provider**\nCurrent: **{current_provider}**\n\nSelect a provider:",
            reply_markup=InlineKeyboardMarkup(buttons)
        )

    async def _churn_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /churn command - generate and display churn analysis report"""
        await update.message.reply_text("📊 Generating churn analysis report...")

        try:
            from churn_report import generate_churn_report, format_report_console

            # Parse optional days argument
            days = 30
            if context.args:
                try:
                    days = int(context.args[0])
                except ValueError:
                    pass

            # Generate the report
            report = generate_churn_report(days=days, verbose=False)

            # Format summary for Telegram (keep it concise)
            s = report.summary
            churn_indicator = ""
            if s.churn_rate_change is not None:
                if s.churn_rate_change > 0:
                    churn_indicator = f" ↑{s.churn_rate_change:+.1f}%"
                elif s.churn_rate_change < 0:
                    churn_indicator = f" ↓{s.churn_rate_change:.1f}%"

            # Build summary message
            summary_msg = f"""📊 **Churn Analysis Report**
*Period: Last {days} days*

**Summary:**
• Churn Rate: {s.current_churn_rate:.1f}%{churn_indicator if s.current_churn_rate else 'N/A'}
• MRR Lost: ${s.mrr_lost_filtered:,.2f}
• Churned Users: {s.total_churned_filtered} ({s.bots_filtered} bots filtered)
• Avg Tenure: {s.avg_tenure_days:.1f} days

**By Tenure Segment:**
"""
            for seg in ['<7d', '7-30d', '30+d']:
                data = s.segment_breakdown.get(seg, {'count': 0, 'mrr_lost': 0, 'pct_of_total': 0})
                if data['count'] > 0:
                    summary_msg += f"• {seg}: {data['count']} users ({data['pct_of_total']:.0f}%), ${data['mrr_lost']:,.0f} MRR\n"

            # Add top friction points
            friction = report.friction_data
            if friction.get('page_issues'):
                summary_msg += "\n**Top Friction Pages:**\n"
                for page in friction['page_issues'][:3]:
                    path = page.get('path', '/')
                    rage = page.get('rage_clicks', 0)
                    dead = page.get('dead_clicks', 0)
                    summary_msg += f"• `{path}` - {rage} rage, {dead} dead clicks\n"

            # Add upcoming cancellations (future churn)
            if report.upcoming_cancellations:
                total_at_risk = sum(u.current_mrr for u in report.upcoming_cancellations)
                high_priority = [u for u in report.upcoming_cancellations if u.retention_priority == 'high']
                summary_msg += f"\n⚠️ **Upcoming Cancellations:**\n"
                summary_msg += f"• {len(report.upcoming_cancellations)} users scheduled to cancel\n"
                summary_msg += f"• ${total_at_risk:,.2f} MRR at risk\n"
                if high_priority:
                    summary_msg += f"• 🔴 {len(high_priority)} HIGH priority for retention\n"
                # Show top 2 upcoming cancellations
                for u in report.upcoming_cancellations[:2]:
                    priority_icon = "🔴" if u.retention_priority == 'high' else "🟡" if u.retention_priority == 'medium' else "🟢"
                    summary_msg += f"• {priority_icon} {u.user_id[:8]}... - ${u.current_mrr:.0f}/mo, cancels in {u.days_until_cancel}d\n"

            summary_msg += f"\n📄 Full report: `cache/reports/churn_analysis.md`"

            await update.message.reply_text(summary_msg, parse_mode='Markdown')

        except Exception as e:
            logger.error(f"Churn report failed: {e}", exc_info=True)
            await update.message.reply_text(f"❌ Churn report failed: {e}")

    async def _help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /help command"""
        help_msg = f"""
🤖 **{self.company_name} Optimization Bot**

**Commands:**
/start - Main menu / generate new strategy
/help - Show this help message
/status - Check system status
/churn - Generate churn analysis report
/saved - View saved & pending strategies
/failed - View failed strategies (re-save)
/rejected - View rejected strategies (re-save)
/approved - View approved strategies (ready to execute)
/resume - Resume stuck strategies
/llm - Switch LLM provider
/ping - Test bot connectivity

**How to respond to strategies:**
- ✅ **Approve** - Queue for execution later (CEO-approved)
- 📋 **Save** - Save for manual implementation later
- 🚀 **Execute Now** - Autonomous coding: generates plan, code, and deploys to preview
- 📝 **Plan** - Generate an implementation plan (MD file) for human-led coding
- ✏️ **Refine** - Submit refinement feedback
- ❌ **Reject** - Discard the strategy

**Text shortcuts:**
- `approve` → Queue for later execution
- `save` / `later` / `manual` → Save for later
- `execute` / `exec` / `code` / `deploy` / `ship it` → Execute now
- `plan` → Generate plan only
- `reject` / `no` / `skip` → Reject

**After preview deployment:**
- ✅ **Merge to develop** - Deploy to production
- 🗑️ **Discard** - Delete the preview branch

Or just send me a message — I can answer questions about your data and strategies.
"""
        await update.message.reply_text(help_msg)

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

    async def _ping_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /ping command - diagnostic to verify bot is responding"""
        chat_id = update.message.chat_id
        logger.info(f"/ping received from chat_id: {chat_id}")
        await update.message.reply_text(
            f"🏓 Pong!\n\n"
            f"Your chat ID: `{chat_id}`\n"
            f"Configured chat ID: `{self.chat_id}`\n"
            f"Match: {'✅ Yes' if str(chat_id) == str(self.chat_id).strip() else '❌ No'}"
        )

    async def _saved_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /saved command - list saved and pending strategies for review"""
        chat_id = update.message.chat_id
        logger.info(f"/saved received from chat_id: {chat_id}")

        try:
            saved = self.db.get_saved_strategies()
            pending = self.db.get_pending_strategies()

            # Combine both lists with status indicator
            all_strategies = []
            for s in pending:
                s['_status_label'] = '⏳ Pending'
                all_strategies.append(s)
            for s in saved:
                s['_status_label'] = '📋 Saved'
                all_strategies.append(s)

            if not all_strategies:
                await update.message.reply_text("📋 No saved or pending strategies to review.")
                # Clear any previous selection state
                self._saved_selection_state.pop(chat_id, None)
                return

            # Store the strategy IDs for selection (use actual IDs, not indices)
            self._saved_selection_state[chat_id] = {s['id']: s for s in all_strategies}

            # Build the list message
            msg_lines = ["📋 **Strategies for Review**\n"]
            msg_lines.append("Tap a strategy ID to view details:\n")

            for strategy in all_strategies:
                strategy_id = strategy['id']
                summary = strategy['summary'][:70] + '...' if len(strategy['summary']) > 70 else strategy['summary']
                focus = strategy.get('focus_area', 'N/A')
                created = strategy.get('created_at', '')[:10]  # Just the date
                status_label = strategy.get('_status_label', '')
                msg_lines.append(f"**#{strategy_id}** {status_label} [{focus}]")
                msg_lines.append(f"    {summary}")
                msg_lines.append(f"    _Created: {created}_\n")

            strategy_ids = [s['id'] for s in all_strategies]
            msg_lines.append(f"\n💡 Tap a strategy ID to view full details")

            reply_markup = self._build_id_buttons(strategy_ids, 'saved')
            await self._send_chunked(update.get_bot(), chat_id, "\n".join(msg_lines), reply_markup=reply_markup)
            logger.info(f"Listed {len(pending)} pending + {len(saved)} saved = {len(all_strategies)} strategies for chat {chat_id}")

        except Exception as e:
            logger.error(f"Error listing strategies: {e}")
            await update.message.reply_text("❌ Error fetching strategies")

    async def _failed_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /failed command - list failed strategies for re-approval"""
        chat_id = update.message.chat_id
        logger.info(f"/failed received from chat_id: {chat_id}")

        try:
            failed = self.db.get_failed_strategies()

            if not failed:
                await update.message.reply_text("✅ No failed strategies to review.")
                self._failed_selection_state.pop(chat_id, None)
                return

            # Store the strategy IDs for selection
            self._failed_selection_state[chat_id] = {s['id']: s for s in failed}

            # Build the list message
            msg_lines = ["❌ **Failed Strategies**\n"]
            msg_lines.append("Tap a strategy ID to view and re-approve:\n")

            for strategy in failed:
                strategy_id = strategy['id']
                summary = strategy['summary'][:70] + '...' if len(strategy['summary']) > 70 else strategy['summary']
                focus = strategy.get('focus_area', 'N/A')
                created = strategy.get('created_at', '')[:10]
                msg_lines.append(f"**#{strategy_id}** ❌ Failed [{focus}]")
                msg_lines.append(f"    {summary}")
                msg_lines.append(f"    _Created: {created}_\n")

            strategy_ids = [s['id'] for s in failed]
            msg_lines.append(f"\n💡 Tap a strategy ID to view and re-approve")

            reply_markup = self._build_id_buttons(strategy_ids, 'failed')
            await update.message.reply_text("\n".join(msg_lines), reply_markup=reply_markup)
            logger.info(f"Listed {len(failed)} failed strategies for chat {chat_id}")

        except Exception as e:
            logger.error(f"Error listing failed strategies: {e}")
            await update.message.reply_text("❌ Error fetching failed strategies")

    async def _rejected_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /rejected command - list rejected strategies for re-approval"""
        chat_id = update.message.chat_id
        logger.info(f"/rejected received from chat_id: {chat_id}")

        try:
            rejected = self.db.get_rejected_strategies()

            if not rejected:
                await update.message.reply_text("✅ No rejected strategies to review.")
                self._rejected_selection_state.pop(chat_id, None)
                return

            # Store the strategy IDs for selection
            self._rejected_selection_state[chat_id] = {s['id']: s for s in rejected}

            # Build the list message
            msg_lines = ["🚫 **Rejected Strategies**\n"]
            msg_lines.append("Tap a strategy ID to view and re-approve:\n")

            for strategy in rejected:
                strategy_id = strategy['id']
                summary = strategy['summary'][:70] + '...' if len(strategy['summary']) > 70 else strategy['summary']
                focus = strategy.get('focus_area', 'N/A')
                created = strategy.get('created_at', '')[:10]
                msg_lines.append(f"**#{strategy_id}** 🚫 Rejected [{focus}]")
                msg_lines.append(f"    {summary}")
                msg_lines.append(f"    _Created: {created}_\n")

            strategy_ids = [s['id'] for s in rejected]
            msg_lines.append(f"\n💡 Tap a strategy ID to view and re-approve")

            reply_markup = self._build_id_buttons(strategy_ids, 'rejected')
            await update.message.reply_text("\n".join(msg_lines), reply_markup=reply_markup)
            logger.info(f"Listed {len(rejected)} rejected strategies for chat {chat_id}")

        except Exception as e:
            logger.error(f"Error listing rejected strategies: {e}")
            await update.message.reply_text("❌ Error fetching rejected strategies")

    async def _handle_failed_rejected_selection(self, update: Update, strategy_id: int, source: str):
        """Handle when user selects a failed or rejected strategy by ID for re-approval"""
        chat_id = update.message.chat_id

        if source == 'failed':
            strategies_dict = self._failed_selection_state.get(chat_id, {})
            status_emoji = "❌"
            status_label = "Failed"
        else:  # rejected
            strategies_dict = self._rejected_selection_state.get(chat_id, {})
            status_emoji = "🚫"
            status_label = "Rejected"

        # Check if this ID is in the list of strategies we showed
        if strategy_id not in strategies_dict:
            valid_ids = list(strategies_dict.keys())
            await update.message.reply_text(
                f"❌ Invalid strategy ID. Valid IDs: {', '.join(map(str, valid_ids))}"
            )
            return

        # Use the cached strategy or fetch from DB
        strategy = strategies_dict.get(strategy_id) or self.db.get_strategy(strategy_id)

        if not strategy:
            await update.message.reply_text("❌ Strategy not found.")
            return

        # Clear selection state
        if source == 'failed':
            del self._failed_selection_state[chat_id]
        else:
            del self._rejected_selection_state[chat_id]

        # Parse changes
        import json
        changes = strategy.get('changes', [])
        if isinstance(changes, str):
            try:
                changes = json.loads(changes)
            except json.JSONDecodeError:
                changes = []

        changes_text = "\n\n".join([
            f"**{i+1}. {change.get('action', 'N/A')}**\n"
            f"Type: {change.get('type', 'N/A')}\n"
            f"Impact: {change.get('expected_impact', 'N/A')}\n"
            f"Risk: {change.get('risk_level', 'N/A')}\n"
            f"Effort: {change.get('effort', 'N/A')}"
            for i, change in enumerate(changes)
        ]) if changes else "No changes specified"

        dep_text = self._format_dependency_analysis(strategy)

        message = f"""
{status_emoji} **{status_label} Strategy #{strategy_id}**

**Summary:** {strategy['summary']}

**Focus Area:** {strategy.get('focus_area', 'N/A')}
**Priority:** {strategy.get('priority', 0)}/10
**Expected Impact:** {strategy.get('expected_impact', 'N/A')}

**Proposed Changes:**
{changes_text}
{dep_text}

**What would you like to do?**
"""

        # Create inline keyboard with re-approval options
        keyboard = [
            [
                InlineKeyboardButton("✅ Approve", callback_data=f"reapprove_approve:{strategy_id}"),
                InlineKeyboardButton("📋 Save", callback_data=f"reapprove_save:{strategy_id}"),
                InlineKeyboardButton("🗑️ Keep Rejected", callback_data=f"keep_rejected:{strategy_id}")
            ],
            [InlineKeyboardButton("✅ Manually Completed", callback_data=f"manual_complete:{strategy_id}")],
            [InlineKeyboardButton("⬅️ Back to Menu", callback_data="back_to_menu")]
        ]

        reply_markup = InlineKeyboardMarkup(keyboard)

        await update.message.reply_text(message, reply_markup=reply_markup)
        logger.info(f"Displayed {source} strategy {strategy_id} for re-approval")

    async def _approved_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /approved command - list approved strategies ready to execute"""
        chat_id = update.message.chat_id
        logger.info(f"/approved received from chat_id: {chat_id}")

        try:
            approved = self.db.get_approved_strategies()

            if not approved:
                await update.message.reply_text("✅ No approved strategies waiting for execution.")
                self._approved_selection_state.pop(chat_id, None)
                return

            # Store the strategy IDs for selection
            self._approved_selection_state[chat_id] = {s['id']: s for s in approved}

            # Build the list message
            msg_lines = ["🚀 **Approved Strategies**\n"]
            msg_lines.append("Tap a strategy ID to view details:\n")

            for strategy in approved:
                strategy_id = strategy['id']
                summary = strategy['summary'][:70] + '...' if len(strategy['summary']) > 70 else strategy['summary']
                focus = strategy.get('focus_area', 'N/A')
                created = strategy.get('created_at', '')[:10]
                stage = strategy.get('execution_stage', 'waiting')
                msg_lines.append(f"**#{strategy_id}** 🚀 Approved [{focus}] - {stage}")
                msg_lines.append(f"    {summary}")
                msg_lines.append(f"    _Created: {created}_\n")

            strategy_ids = [s['id'] for s in approved]
            msg_lines.append(f"\n💡 Tap a strategy ID to view details")

            reply_markup = self._build_id_buttons(strategy_ids, 'approved')
            await update.message.reply_text("\n".join(msg_lines), reply_markup=reply_markup)
            logger.info(f"Listed {len(approved)} approved strategies for chat {chat_id}")

        except Exception as e:
            logger.error(f"Error listing approved strategies: {e}")
            await update.message.reply_text("❌ Error fetching approved strategies")

    async def _restart_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /restart command - legacy alias for /start"""
        await self._start_command(update, context)

    async def _handle_approved_selection(self, update: Update, strategy_id: int):
        """Handle when user selects an approved strategy by ID to execute"""
        chat_id = update.message.chat_id
        strategies_dict = self._approved_selection_state.get(chat_id, {})

        # Check if this ID is in the list of strategies we showed
        if strategy_id not in strategies_dict:
            valid_ids = list(strategies_dict.keys())
            await update.message.reply_text(
                f"❌ Invalid strategy ID. Valid IDs: {', '.join(map(str, valid_ids))}"
            )
            return

        strategy = strategies_dict.get(strategy_id) or self.db.get_strategy(strategy_id)

        if not strategy:
            await update.message.reply_text("❌ Strategy not found.")
            return

        # Clear selection state
        del self._approved_selection_state[chat_id]

        # Set the landing menu selection to execute this specific strategy
        self._landing_menu_selection[chat_id] = f'execute:{strategy_id}'

        await update.message.reply_text(
            f"🚀 **Executing Strategy #{strategy_id}**\n\n"
            f"The loop will begin code generation and deployment for this strategy.\n\n"
            f"_Processing will start shortly..._"
        )
        logger.info(f"Strategy {strategy_id} selected for execution from /approved")

    async def _handle_saved_selection_by_id(self, chat_id: int, strategy_id: int, query):
        """Handle saved/pending strategy selection from inline button click"""
        strategies_dict = self._saved_selection_state.get(chat_id, {})
        strategy = strategies_dict.get(strategy_id) or self.db.get_strategy(strategy_id)
        if not strategy:
            await query.edit_message_text(f"❌ Strategy #{strategy_id} not found.")
            return
        del self._saved_selection_state[chat_id]

        import json
        changes = strategy.get('changes', [])
        if isinstance(changes, str):
            try:
                changes = json.loads(changes)
            except json.JSONDecodeError:
                changes = []

        changes_text = "\n\n".join([
            f"**{i+1}. {change.get('action', 'N/A')}**\n"
            f"Type: {change.get('type', 'N/A')}\n"
            f"Impact: {change.get('expected_impact', 'N/A')}\n"
            f"Risk: {change.get('risk_level', 'N/A')}\n"
            f"Effort: {change.get('effort', 'N/A')}"
            for i, change in enumerate(changes)
        ]) if changes else "No changes specified"

        status = strategy.get('status', 'unknown')
        status_emoji = {'pending': '⏳', 'saved': '📋'}.get(status, '📄')
        status_label = {'pending': 'Pending', 'saved': 'Saved'}.get(status, status.title())

        dep_text = self._format_dependency_analysis(strategy)

        message = f"""
{status_emoji} **{status_label} Strategy #{strategy_id}**

**Summary:** {strategy['summary']}

**Focus Area:** {strategy.get('focus_area', 'N/A')}
**Priority:** {strategy.get('priority', 0)}/10
**Expected Impact:** {strategy.get('expected_impact', 'N/A')}

**Proposed Changes:**
{changes_text}
{dep_text}

**What would you like to do?**
"""
        if status == 'pending':
            keyboard = [
                [
                    InlineKeyboardButton("✅ Approve", callback_data=f"approve_queue:{strategy_id}"),
                    InlineKeyboardButton("📋 Save", callback_data=f"save:{strategy_id}"),
                    InlineKeyboardButton("🚀 Execute Now", callback_data=f"code:{strategy_id}"),
                ],
                [
                    InlineKeyboardButton("📝 Plan", callback_data=f"plan:{strategy_id}"),
                    InlineKeyboardButton("✏️ Refine", callback_data=f"refine:{strategy_id}"),
                    InlineKeyboardButton("❌ Reject", callback_data=f"reject:{strategy_id}"),
                ],
                [InlineKeyboardButton("✅ Manually Completed", callback_data=f"manual_complete:{strategy_id}")]
            ]
            if len(changes) > 1:
                keyboard.append([InlineKeyboardButton(f"✂️ Split into {len(changes)} strategies", callback_data=f"split:{strategy_id}")])
            keyboard.append([InlineKeyboardButton("⬅️ Back to Menu", callback_data="back_to_menu")])
        else:
            keyboard = [
                [
                    InlineKeyboardButton("✅ Approve", callback_data=f"approve_queue:{strategy_id}"),
                    InlineKeyboardButton("📝 Plan", callback_data=f"plan:{strategy_id}"),
                ],
                [
                    InlineKeyboardButton("✏️ Refine", callback_data=f"refine:{strategy_id}"),
                    InlineKeyboardButton("❌ Reject", callback_data=f"reject:{strategy_id}"),
                ],
                [InlineKeyboardButton("✅ Manually Completed", callback_data=f"manual_complete:{strategy_id}")]
            ]
            if len(changes) > 1:
                keyboard.append([InlineKeyboardButton(f"✂️ Split into {len(changes)} strategies", callback_data=f"split:{strategy_id}")])
            keyboard.append([InlineKeyboardButton("⬅️ Back to Menu", callback_data="back_to_menu")])
        bot = query.get_bot()
        await self._send_chunked(bot, chat_id, message, reply_markup=InlineKeyboardMarkup(keyboard))
        logger.info(f"Displayed {status} strategy {strategy_id} via pick button")

    async def _handle_failed_rejected_selection_by_id(self, chat_id: int, strategy_id: int, source: str, query):
        """Handle failed/rejected strategy selection from inline button click"""
        if source == 'failed':
            strategies_dict = self._failed_selection_state.get(chat_id, {})
            status_emoji, status_label = "❌", "Failed"
        else:
            strategies_dict = self._rejected_selection_state.get(chat_id, {})
            status_emoji, status_label = "🚫", "Rejected"

        strategy = strategies_dict.get(strategy_id) or self.db.get_strategy(strategy_id)
        if not strategy:
            await query.edit_message_text(f"❌ Strategy #{strategy_id} not found.")
            return

        if source == 'failed':
            del self._failed_selection_state[chat_id]
        else:
            del self._rejected_selection_state[chat_id]

        import json
        changes = strategy.get('changes', [])
        if isinstance(changes, str):
            try:
                changes = json.loads(changes)
            except json.JSONDecodeError:
                changes = []

        changes_text = "\n\n".join([
            f"**{i+1}. {change.get('action', 'N/A')}**\n"
            f"Type: {change.get('type', 'N/A')}\n"
            f"Impact: {change.get('expected_impact', 'N/A')}\n"
            f"Risk: {change.get('risk_level', 'N/A')}\n"
            f"Effort: {change.get('effort', 'N/A')}"
            for i, change in enumerate(changes)
        ]) if changes else "No changes specified"

        dep_text = self._format_dependency_analysis(strategy)

        message = f"""
{status_emoji} **{status_label} Strategy #{strategy_id}**

**Summary:** {strategy['summary']}

**Focus Area:** {strategy.get('focus_area', 'N/A')}
**Priority:** {strategy.get('priority', 0)}/10
**Expected Impact:** {strategy.get('expected_impact', 'N/A')}

**Proposed Changes:**
{changes_text}
{dep_text}

💾 **Cache:** Code plans and code changes are saved in the `cache/` folder on the executing machine (`cache/code_plans/` and `cache/code_changes/`).

**What would you like to do?**
"""
        keyboard = [
            [
                InlineKeyboardButton("✅ Approve", callback_data=f"reapprove_approve:{strategy_id}"),
                InlineKeyboardButton("📋 Save", callback_data=f"reapprove_save:{strategy_id}"),
                InlineKeyboardButton("🗑️ Keep Rejected", callback_data=f"keep_rejected:{strategy_id}")
            ],
            [
                InlineKeyboardButton("✅ Manually Completed", callback_data=f"manual_complete:{strategy_id}"),
                InlineKeyboardButton("🗑️ Delete", callback_data=f"delete:{strategy_id}")
            ],
            [InlineKeyboardButton("⬅️ Back to Menu", callback_data="back_to_menu")]
        ]
        bot = query.get_bot()
        await bot.send_message(chat_id=chat_id, text=message, reply_markup=InlineKeyboardMarkup(keyboard))
        logger.info(f"Displayed {source} strategy {strategy_id} via pick button")

    async def _handle_approved_selection_by_id(self, chat_id: int, strategy_id: int, query):
        """Handle approved strategy selection from inline button click"""
        strategies_dict = self._approved_selection_state.get(chat_id, {})
        strategy = strategies_dict.get(strategy_id) or self.db.get_strategy(strategy_id)
        if not strategy:
            await query.edit_message_text(f"❌ Strategy #{strategy_id} not found.")
            return
        del self._approved_selection_state[chat_id]

        import json
        changes = strategy.get('changes', [])
        if isinstance(changes, str):
            try:
                changes = json.loads(changes)
            except json.JSONDecodeError:
                changes = []

        changes_text = "\n\n".join([
            f"**{i+1}. {change.get('action', 'N/A')}**\n"
            f"Type: {change.get('type', 'N/A')}\n"
            f"Impact: {change.get('expected_impact', 'N/A')}\n"
            f"Risk: {change.get('risk_level', 'N/A')}\n"
            f"Effort: {change.get('effort', 'N/A')}"
            for i, change in enumerate(changes)
        ]) if changes else "No changes specified"

        dep_text = self._format_dependency_analysis(strategy)

        message = f"""
🚀 **Approved Strategy #{strategy_id}**

**Summary:** {strategy['summary']}

**Focus Area:** {strategy.get('focus_area', 'N/A')}
**Priority:** {strategy.get('priority', 0)}/10
**Expected Impact:** {strategy.get('expected_impact', 'N/A')}

**Proposed Changes:**
{changes_text}
{dep_text}

**What would you like to do?**
"""
        keyboard = [
            [
                InlineKeyboardButton("🚀 Execute Now", callback_data=f"code:{strategy_id}"),
                InlineKeyboardButton("📋 Move to Saved", callback_data=f"save:{strategy_id}"),
            ],
            [
                InlineKeyboardButton("📝 Plan", callback_data=f"plan:{strategy_id}"),
                InlineKeyboardButton("✏️ Refine", callback_data=f"refine:{strategy_id}"),
            ],
            [InlineKeyboardButton("✅ Manually Completed", callback_data=f"manual_complete:{strategy_id}")],
        ]
        if len(changes) > 1:
            keyboard.append([InlineKeyboardButton(f"✂️ Split into {len(changes)} strategies", callback_data=f"split:{strategy_id}")])
        keyboard.append([InlineKeyboardButton("⬅️ Back to Menu", callback_data="back_to_menu")])

        bot = query.get_bot()
        await self._send_chunked(bot, chat_id, message, reply_markup=InlineKeyboardMarkup(keyboard))
        logger.info(f"Displayed approved strategy {strategy_id} via pick button")

    async def send_welcome_intro(self, bot=None):
        """Send the bot intro/commands message.

        Args:
            bot: Optional Bot instance to use. When called from a callback handler,
                 pass query.get_bot() to avoid event-loop-closed errors with _sender_bot.
        """
        if not self.chat_id:
            return
        engine = get_strategy_engine()
        current_provider = engine.llm_provider
        welcome_msg = f"""
🤖 **{self.company_name} Optimization Bot**

🧠 **LLM Provider: {current_provider}** — /llm to change

**Commands:**
/start - Main menu / generate new strategy
/help - Show help
/status - Check system status
/saved - View saved & pending strategies
/failed - View failed strategies
/rejected - View rejected strategies
/approved - View approved strategies
/resume - Resume stuck strategies
/llm - Switch LLM provider
/ping - Test connectivity

**How to respond to strategies:**
✅ Approve | 📋 Save | 🚀 Execute Now | 📝 Plan | ✏️ Refine | ❌ Reject

Or just chat with me — I understand the optimization system and your data.
"""
        send_bot = bot or self._sender_bot
        await send_bot.send_message(chat_id=self.chat_id, text=welcome_msg)

    async def send_landing_menu(self) -> bool:
        """
        Send the landing menu to the CEO with clickable options.
        Called by loop_controller at the start of each cycle.

        Returns:
            True if sent successfully
        """
        if not self.chat_id:
            logger.error("TELEGRAM_CHAT_ID not configured")
            return False

        try:
            # Get counts for each category
            saved_count = len(self.db.get_saved_strategies())
            pending_count = len(self.db.get_pending_strategies())
            failed_count = len(self.db.get_failed_strategies())
            rejected_count = len(self.db.get_rejected_strategies())
            approved_count = len(self.db.get_approved_strategies())

            engine = get_strategy_engine()
            current_provider = engine.llm_provider

            message = f"""
🎯 **Optimization Loop Ready**
🧠 LLM Provider: **{current_provider}** — /llm to change

**Current Strategies:**
• 📋 Saved/Pending: {saved_count + pending_count}
• ❌ Failed: {failed_count}
• 🚫 Rejected: {rejected_count}
• 🚀 Approved (ready to execute): {approved_count}

**What would you like to do?**
"""

            # Create inline keyboard with menu options
            keyboard = [
                [
                    InlineKeyboardButton(f"📋 Saved ({saved_count + pending_count})", callback_data="menu:saved"),
                    InlineKeyboardButton(f"🚀 Approved ({approved_count})", callback_data="menu:approved"),
                ],
                [
                    InlineKeyboardButton(f"❌ Failed ({failed_count})", callback_data="menu:failed"),
                    InlineKeyboardButton(f"🚫 Rejected ({rejected_count})", callback_data="menu:rejected"),
                ],
                [
                    InlineKeyboardButton("🔄 Generate New Strategy", callback_data="menu:restart"),
                ]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)

            # Clear any previous selection
            self._landing_menu_selection[int(self.chat_id)] = None

            await self._sender_bot.send_message(
                chat_id=self.chat_id,
                text=message,
                reply_markup=reply_markup
            )

            logger.info("Landing menu sent")
            return True

        except Exception as e:
            logger.error(f"Error sending landing menu: {e}")
            return False

    def get_menu_selection(self, chat_id: int) -> Optional[str]:
        """
        Get the current landing menu selection for a chat.
        Returns None if no selection made yet.

        Returns:
            Selection string: 'saved', 'failed', 'rejected', 'approved', 'restart',
            'execute:<strategy_id>', or None
        """
        return self._landing_menu_selection.get(chat_id)

    def clear_menu_selection(self, chat_id: int):
        """Clear the landing menu selection for a chat"""
        self._landing_menu_selection.pop(chat_id, None)

    def get_strategy_generation_context(self) -> str:
        """Get and clear the context message for strategy generation (from 'Run as New Strategy' button)."""
        context = self._strategy_generation_context
        self._strategy_generation_context = None
        return context

    async def _send_landing_menu_with_bot(self, chat_id: int, bot):
        """Send the landing menu using provided bot"""
        try:
            # Get counts for each category
            saved_count = len(self.db.get_saved_strategies())
            pending_count = len(self.db.get_pending_strategies())
            failed_count = len(self.db.get_failed_strategies())
            rejected_count = len(self.db.get_rejected_strategies())
            approved_count = len(self.db.get_approved_strategies())

            engine = get_strategy_engine()
            current_provider = engine.llm_provider

            message = f"""
🎯 **Optimization Loop Ready**
🧠 LLM Provider: **{current_provider}** — /llm to change

**Current Strategies:**
• 📋 Saved/Pending: {saved_count + pending_count}
• ❌ Failed: {failed_count}
• 🚫 Rejected: {rejected_count}
• 🚀 Approved (ready to execute): {approved_count}

**What would you like to do?**
"""

            # Create inline keyboard with menu options
            keyboard = [
                [
                    InlineKeyboardButton(f"📋 Saved ({saved_count + pending_count})", callback_data="menu:saved"),
                    InlineKeyboardButton(f"🚀 Approved ({approved_count})", callback_data="menu:approved"),
                ],
                [
                    InlineKeyboardButton(f"❌ Failed ({failed_count})", callback_data="menu:failed"),
                    InlineKeyboardButton(f"🚫 Rejected ({rejected_count})", callback_data="menu:rejected"),
                ],
                [
                    InlineKeyboardButton("🔄 Generate New Strategy", callback_data="menu:restart"),
                ]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)

            # Clear any previous selection
            self._landing_menu_selection[chat_id] = None

            await bot.send_message(chat_id=chat_id, text=message, reply_markup=reply_markup)
            logger.info("Landing menu sent via bot")

        except Exception as e:
            logger.error(f"Error sending landing menu: {e}")

    async def _send_landing_menu_inline(self, update: Update):
        """Send the landing menu from within a command handler"""
        chat_id = update.message.chat_id

        try:
            # Get counts for each category
            saved_count = len(self.db.get_saved_strategies())
            pending_count = len(self.db.get_pending_strategies())
            failed_count = len(self.db.get_failed_strategies())
            rejected_count = len(self.db.get_rejected_strategies())
            approved_count = len(self.db.get_approved_strategies())

            engine = get_strategy_engine()
            current_provider = engine.llm_provider

            message = f"""
🎯 **Optimization Loop Ready**
🧠 LLM Provider: **{current_provider}** — /llm to change

**Current Strategies:**
• 📋 Saved/Pending: {saved_count + pending_count}
• ❌ Failed: {failed_count}
• 🚫 Rejected: {rejected_count}
• 🚀 Approved (ready to execute): {approved_count}

**What would you like to do?**
"""

            # Create inline keyboard with menu options
            keyboard = [
                [
                    InlineKeyboardButton(f"📋 Saved ({saved_count + pending_count})", callback_data="menu:saved"),
                    InlineKeyboardButton(f"🚀 Approved ({approved_count})", callback_data="menu:approved"),
                ],
                [
                    InlineKeyboardButton(f"❌ Failed ({failed_count})", callback_data="menu:failed"),
                    InlineKeyboardButton(f"🚫 Rejected ({rejected_count})", callback_data="menu:rejected"),
                ],
                [
                    InlineKeyboardButton("🔄 Generate New Strategy", callback_data="menu:restart"),
                ]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)

            # Clear any previous selection
            self._landing_menu_selection[chat_id] = None

            await update.message.reply_text(message, reply_markup=reply_markup)
            logger.info("Landing menu sent via command")

        except Exception as e:
            logger.error(f"Error sending landing menu: {e}")
            await update.message.reply_text("❌ Error loading menu")

    async def _send_saved_list(self, chat_id: int):
        """Send the saved/pending strategies list to a chat"""
        await self._send_saved_list_with_bot(chat_id, self._sender_bot)

    async def _send_saved_list_with_bot(self, chat_id: int, bot):
        """Send the saved/pending strategies list to a chat using provided bot"""
        try:
            saved = self.db.get_saved_strategies()
            pending = self.db.get_pending_strategies()

            all_strategies = []
            for s in pending:
                s['_status_label'] = '⏳ Pending'
                all_strategies.append(s)
            for s in saved:
                s['_status_label'] = '📋 Saved'
                all_strategies.append(s)

            if not all_strategies:
                await bot.send_message(
                    chat_id=chat_id,
                    text="📋 No saved or pending strategies to review."
                )
                return

            self._saved_selection_state[chat_id] = {s['id']: s for s in all_strategies}

            msg_lines = ["📋 **Strategies for Review**\n"]
            msg_lines.append("Tap a strategy ID to view details:\n")

            for strategy in all_strategies:
                strategy_id = strategy['id']
                summary = strategy['summary'][:70] + '...' if len(strategy['summary']) > 70 else strategy['summary']
                focus = strategy.get('focus_area', 'N/A')
                created = strategy.get('created_at', '')[:10]
                status_label = strategy.get('_status_label', '')
                msg_lines.append(f"**#{strategy_id}** {status_label} [{focus}]")
                msg_lines.append(f"    {summary}")
                msg_lines.append(f"    _Created: {created}_\n")

            strategy_ids = [s['id'] for s in all_strategies]
            msg_lines.append(f"\n💡 Tap a strategy ID to view full details")

            reply_markup = self._build_id_buttons(strategy_ids, 'saved')
            await self._send_chunked(bot, chat_id, "\n".join(msg_lines), reply_markup=reply_markup)
            logger.info(f"Sent saved list to chat {chat_id}")

        except Exception as e:
            logger.error(f"Error sending saved list: {e}")

    async def _send_failed_list(self, chat_id: int):
        """Send the failed strategies list to a chat"""
        await self._send_failed_list_with_bot(chat_id, self._sender_bot)

    async def _send_failed_list_with_bot(self, chat_id: int, bot):
        """Send the failed strategies list to a chat using provided bot"""
        try:
            failed = self.db.get_failed_strategies()

            if not failed:
                await bot.send_message(
                    chat_id=chat_id,
                    text="✅ No failed strategies to review."
                )
                return

            self._failed_selection_state[chat_id] = {s['id']: s for s in failed}

            msg_lines = ["❌ **Failed Strategies**\n"]
            msg_lines.append("Tap a strategy ID to view and re-save:\n")

            for strategy in failed:
                strategy_id = strategy['id']
                summary = strategy['summary'][:70] + '...' if len(strategy['summary']) > 70 else strategy['summary']
                focus = strategy.get('focus_area', 'N/A')
                created = strategy.get('created_at', '')[:10]
                msg_lines.append(f"**#{strategy_id}** ❌ Failed [{focus}]")
                msg_lines.append(f"    {summary}")
                msg_lines.append(f"    _Created: {created}_\n")

            strategy_ids = [s['id'] for s in failed]
            msg_lines.append(f"\n💡 Tap a strategy ID to view and re-save")

            reply_markup = self._build_id_buttons(strategy_ids, 'failed')
            await bot.send_message(chat_id=chat_id, text="\n".join(msg_lines), reply_markup=reply_markup)
            logger.info(f"Sent failed list to chat {chat_id}")

        except Exception as e:
            logger.error(f"Error sending failed list: {e}")

    async def _send_rejected_list(self, chat_id: int):
        """Send the rejected strategies list to a chat"""
        await self._send_rejected_list_with_bot(chat_id, self._sender_bot)

    async def _send_rejected_list_with_bot(self, chat_id: int, bot):
        """Send the rejected strategies list to a chat using provided bot"""
        try:
            rejected = self.db.get_rejected_strategies()

            if not rejected:
                await bot.send_message(
                    chat_id=chat_id,
                    text="✅ No rejected strategies to review."
                )
                return

            self._rejected_selection_state[chat_id] = {s['id']: s for s in rejected}

            msg_lines = ["🚫 **Rejected Strategies**\n"]
            msg_lines.append("Tap a strategy ID to view and re-save:\n")

            for strategy in rejected:
                strategy_id = strategy['id']
                summary = strategy['summary'][:70] + '...' if len(strategy['summary']) > 70 else strategy['summary']
                focus = strategy.get('focus_area', 'N/A')
                created = strategy.get('created_at', '')[:10]
                msg_lines.append(f"**#{strategy_id}** 🚫 Rejected [{focus}]")
                msg_lines.append(f"    {summary}")
                msg_lines.append(f"    _Created: {created}_\n")

            strategy_ids = [s['id'] for s in rejected]
            msg_lines.append(f"\n💡 Tap a strategy ID to view and re-save")

            reply_markup = self._build_id_buttons(strategy_ids, 'rejected')
            await bot.send_message(chat_id=chat_id, text="\n".join(msg_lines), reply_markup=reply_markup)
            logger.info(f"Sent rejected list to chat {chat_id}")

        except Exception as e:
            logger.error(f"Error sending rejected list: {e}")

    async def _send_approved_list(self, chat_id: int):
        """Send the approved strategies list to a chat"""
        await self._send_approved_list_with_bot(chat_id, self._sender_bot)

    async def _send_approved_list_with_bot(self, chat_id: int, bot):
        """Send the approved strategies list to a chat using provided bot"""
        try:
            approved = self.db.get_approved_strategies()

            if not approved:
                await bot.send_message(
                    chat_id=chat_id,
                    text="✅ No approved strategies waiting for execution."
                )
                return

            self._approved_selection_state[chat_id] = {s['id']: s for s in approved}

            msg_lines = ["🚀 **Approved Strategies**\n"]
            msg_lines.append("Tap a strategy ID to view details:\n")

            for strategy in approved:
                strategy_id = strategy['id']
                summary = strategy['summary'][:70] + '...' if len(strategy['summary']) > 70 else strategy['summary']
                focus = strategy.get('focus_area', 'N/A')
                created = strategy.get('created_at', '')[:10]
                stage = strategy.get('execution_stage', 'waiting')
                msg_lines.append(f"**#{strategy_id}** 🚀 Approved [{focus}] - {stage}")
                msg_lines.append(f"    {summary}")
                msg_lines.append(f"    _Created: {created}_\n")

            strategy_ids = [s['id'] for s in approved]
            msg_lines.append(f"\n💡 Tap a strategy ID to view details")

            reply_markup = self._build_id_buttons(strategy_ids, 'approved')
            await bot.send_message(chat_id=chat_id, text="\n".join(msg_lines), reply_markup=reply_markup)
            logger.info(f"Sent approved list to chat {chat_id}")

        except Exception as e:
            logger.error(f"Error sending approved list: {e}")

    async def _handle_saved_selection(self, update: Update, strategy_id: int):
        """Handle when user selects a strategy by ID from the list"""
        chat_id = update.message.chat_id
        strategies_dict = self._saved_selection_state.get(chat_id, {})

        # Check if this ID is in the list of strategies we showed
        if strategy_id not in strategies_dict:
            valid_ids = list(strategies_dict.keys())
            await update.message.reply_text(
                f"❌ Invalid strategy ID. Valid IDs: {', '.join(map(str, valid_ids))}"
            )
            return

        # Use the cached strategy or fetch from DB
        strategy = strategies_dict.get(strategy_id) or self.db.get_strategy(strategy_id)

        if not strategy:
            await update.message.reply_text("❌ Strategy not found.")
            return

        # Clear selection state
        del self._saved_selection_state[chat_id]

        # Parse changes
        import json
        changes = strategy.get('changes', [])
        if isinstance(changes, str):
            try:
                changes = json.loads(changes)
            except json.JSONDecodeError:
                changes = []

        changes_text = "\n\n".join([
            f"**{i+1}. {change.get('action', 'N/A')}**\n"
            f"Type: {change.get('type', 'N/A')}\n"
            f"Impact: {change.get('expected_impact', 'N/A')}\n"
            f"Risk: {change.get('risk_level', 'N/A')}\n"
            f"Effort: {change.get('effort', 'N/A')}"
            for i, change in enumerate(changes)
        ]) if changes else "No changes specified"

        # Determine status label
        status = strategy.get('status', 'unknown')
        if status == 'pending':
            status_emoji = "⏳"
            status_label = "Pending"
        elif status == 'saved':
            status_emoji = "📋"
            status_label = "Saved"
        else:
            status_emoji = "📄"
            status_label = status.title()

        dep_text = self._format_dependency_analysis(strategy)

        message = f"""
{status_emoji} **{status_label} Strategy #{strategy_id}**

**Summary:** {strategy['summary']}

**Focus Area:** {strategy.get('focus_area', 'N/A')}
**Priority:** {strategy.get('priority', 0)}/10
**Expected Impact:** {strategy.get('expected_impact', 'N/A')}

**Proposed Changes:**
{changes_text}
{dep_text}

**What would you like to do?**
"""

        # Create inline keyboard with action buttons
        # Pending strategies get Save option, Saved strategies don't need it
        if status == 'pending':
            keyboard = [
                [
                    InlineKeyboardButton("✅ Approve", callback_data=f"approve_queue:{strategy_id}"),
                    InlineKeyboardButton("📋 Save", callback_data=f"save:{strategy_id}"),
                    InlineKeyboardButton("🚀 Execute Now", callback_data=f"code:{strategy_id}"),
                ],
                [
                    InlineKeyboardButton("📝 Plan", callback_data=f"plan:{strategy_id}"),
                    InlineKeyboardButton("❌ Reject", callback_data=f"reject:{strategy_id}")
                ],
                [InlineKeyboardButton("✅ Manually Completed", callback_data=f"manual_complete:{strategy_id}")]
            ]
            if len(changes) > 1:
                keyboard.append([InlineKeyboardButton(f"✂️ Split into {len(changes)} strategies", callback_data=f"split:{strategy_id}")])
            keyboard.append([InlineKeyboardButton("⬅️ Back to Menu", callback_data="back_to_menu")])
            message += "\n_Or reply with text to refine this strategy._"
        else:
            keyboard = [
                [
                    InlineKeyboardButton("✅ Approve", callback_data=f"approve_queue:{strategy_id}"),
                    InlineKeyboardButton("📝 Plan", callback_data=f"plan:{strategy_id}"),
                ],
                [
                    InlineKeyboardButton("✏️ Refine", callback_data=f"refine:{strategy_id}"),
                    InlineKeyboardButton("❌ Reject", callback_data=f"reject:{strategy_id}"),
                ],
                [InlineKeyboardButton("✅ Manually Completed", callback_data=f"manual_complete:{strategy_id}")]
            ]
            if len(changes) > 1:
                keyboard.append([InlineKeyboardButton(f"✂️ Split into {len(changes)} strategies", callback_data=f"split:{strategy_id}")])
            keyboard.append([InlineKeyboardButton("⬅️ Back to Menu", callback_data="back_to_menu")])

        reply_markup = InlineKeyboardMarkup(keyboard)

        await update.message.reply_text(message, reply_markup=reply_markup)
        logger.info(f"Displayed {status} strategy {strategy_id} for review")

    async def _resume_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /resume command - show stuck strategies with Resume/Regenerate options"""
        chat_id = update.message.chat_id

        try:
            # Get stuck strategies (approved or preview status)
            stuck = self.db.get_stuck_strategies()

            if not stuck:
                await update.message.reply_text("✅ No stuck strategies to resume.")
                return

            # Store for selection
            self._resume_selection_state = {s['id']: s for s in stuck}

            # Get code cache to check which strategies have cached code
            code_cache = get_code_cache()

            # Build the list message
            msg_lines = ["🔄 **Stuck Strategies**\n"]
            msg_lines.append("Choose an action for each strategy:\n")
            msg_lines.append("• **Resume**: Use cached code, fix build errors only")
            msg_lines.append("• **Regenerate**: Discard cache, generate all code fresh\n")

            for s in stuck:
                strategy_id = s['id']
                status = s.get('status', 'unknown')
                stage = s.get('execution_stage', 'unknown')
                summary = s['summary'][:60] + '...' if len(s['summary']) > 60 else s['summary']
                created = s.get('created_at', '')[:10]

                # Check if code cache exists
                has_cache = code_cache.exists(strategy_id)
                cache_indicator = "📦 Cached" if has_cache else "🆕 No cache"

                # Status/stage indicator
                if status == 'approved':
                    indicator = "🔧 Code Generation"
                elif status == 'preview':
                    indicator = "🔍 Awaiting Merge"
                else:
                    indicator = f"⚙️ {status}/{stage}"

                msg_lines.append(f"**#{strategy_id}** {indicator} ({cache_indicator})")
                msg_lines.append(f"    {summary}")
                msg_lines.append(f"    _Created: {created}_\n")

            # Build inline keyboard with Resume and Regenerate buttons for each strategy
            keyboard = []
            for s in stuck[:5]:  # Limit to 5 to avoid too many buttons
                strategy_id = s['id']
                has_cache = code_cache.exists(strategy_id)

                if has_cache:
                    # Show both options when cache exists
                    keyboard.append([
                        InlineKeyboardButton(f"▶️ Resume #{strategy_id}", callback_data=f"resume:{strategy_id}"),
                        InlineKeyboardButton(f"🔄 Regen #{strategy_id}", callback_data=f"regen:{strategy_id}")
                    ])
                else:
                    # Only show Resume (which will generate fresh) when no cache
                    keyboard.append([
                        InlineKeyboardButton(f"▶️ Start #{strategy_id}", callback_data=f"resume:{strategy_id}")
                    ])

            reply_markup = InlineKeyboardMarkup(keyboard)

            await update.message.reply_text("\n".join(msg_lines), reply_markup=reply_markup)
            logger.info(f"Listed {len(stuck)} stuck strategies for resume selection")

        except Exception as e:
            logger.error(f"Error in resume command: {e}")
            await update.message.reply_text("❌ Error fetching stuck strategies")

    async def _handle_resume_selection(self, update: Update, strategy_id: int):
        """Handle when user types a strategy ID to resume from the /resume list"""
        try:
            # Get strategy from cache or DB
            strategy = self._resume_selection_state.get(strategy_id) or self.db.get_strategy(strategy_id)

            if not strategy:
                await update.message.reply_text(f"❌ Strategy {strategy_id} not found.")
                return

            current_status = strategy.get('status', 'unknown')
            current_stage = strategy.get('execution_stage', 'unknown')

            # Write approval with 'approve' type (database constraint only allows certain types)
            # The loop_controller will see 'approve' and trigger the resume flow
            self.db.write_approval(
                strategy_id=strategy_id,
                user_response='Resume requested via text',
                response_type='approve'
            )

            # Set landing menu selection so loop controller picks this up
            chat_id = update.message.chat_id
            self._landing_menu_selection[chat_id] = f'execute:{strategy_id}'

            # Clear selection state
            self._resume_selection_state = {}

            await update.message.reply_text(
                f"▶️ Resuming strategy {strategy_id}...\n"
                f"Current status: {current_status}\n"
                f"Stage: {current_stage}\n\n"
                f"The optimization loop will pick this up and continue from where it left off."
            )
            logger.info(f"Strategy {strategy_id} resume requested via text selection")

        except Exception as e:
            logger.error(f"Error handling resume selection: {e}")
            await update.message.reply_text("❌ Error processing resume request")

    async def _handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle inline button callbacks"""
        query = update.callback_query
        await query.answer()

        try:
            # Parse callback data: "action:strategy_id" or "action:strategy_id:extra"
            parts = query.data.split(':')
            action = parts[0]
            chat_id = query.message.chat_id

            # Handle landing menu selections (no strategy_id)
            if action == 'menu':
                menu_action = parts[1]
                bot = query.get_bot()  # Use bot from callback context to avoid event loop issues

                if menu_action == 'saved':
                    self._landing_menu_selection[chat_id] = 'saved'
                    await query.edit_message_text(
                        f"📋 **Opening Saved Strategies...**\n\n{query.message.text}"
                    )
                    # Send the saved strategies list using bot from context
                    await self._send_saved_list_with_bot(chat_id, bot)

                elif menu_action == 'failed':
                    self._landing_menu_selection[chat_id] = 'failed'
                    await query.edit_message_text(
                        f"❌ **Opening Failed Strategies...**\n\n{query.message.text}"
                    )
                    await self._send_failed_list_with_bot(chat_id, bot)

                elif menu_action == 'rejected':
                    self._landing_menu_selection[chat_id] = 'rejected'
                    await query.edit_message_text(
                        f"🚫 **Opening Rejected Strategies...**\n\n{query.message.text}"
                    )
                    await self._send_rejected_list_with_bot(chat_id, bot)

                elif menu_action == 'approved':
                    self._landing_menu_selection[chat_id] = 'approved'
                    await query.edit_message_text(
                        f"🚀 **Opening Approved Strategies...**\n\n{query.message.text}"
                    )
                    await self._send_approved_list_with_bot(chat_id, bot)

                elif menu_action == 'restart':
                    self._landing_menu_selection[chat_id] = 'restart'
                    await query.edit_message_text(
                        f"🔄 **Generating New Strategy...**\n\n"
                        f"A new optimization cycle will begin based on current data.\n\n"
                        f"_Processing will start shortly..._"
                    )

                elif menu_action == 'back':
                    # Go back to the landing menu
                    self._landing_menu_selection[chat_id] = None
                    self._saved_selection_state.pop(chat_id, None)
                    self._failed_selection_state.pop(chat_id, None)
                    self._rejected_selection_state.pop(chat_id, None)
                    self._approved_selection_state.pop(chat_id, None)

                    # Send new landing menu
                    await self._send_landing_menu_with_bot(chat_id, bot)
                    await query.edit_message_text(
                        f"⬅️ **Returned to Menu**\n\n{query.message.text}"
                    )

                logger.info(f"Landing menu selection: {menu_action} from chat {chat_id}")
                return  # Early return - menu handled

            # Handle "Back to Menu" button from strategy detail pages
            if action == 'back_to_menu':
                bot = query.get_bot()
                self._landing_menu_selection[chat_id] = None
                self._saved_selection_state.pop(chat_id, None)
                self._failed_selection_state.pop(chat_id, None)
                self._rejected_selection_state.pop(chat_id, None)
                self._approved_selection_state.pop(chat_id, None)

                await query.edit_message_text(
                    f"⬅️ **Returned to Menu**\n\n{query.message.text}"
                )
                await self._send_landing_menu_with_bot(chat_id, bot)
                logger.info(f"Back to menu from strategy detail, chat {chat_id}")
                return  # Early return

            # Handle "Run as New Strategy" button from chat messages
            if action == 'run_as_strategy':
                ctx_id = parts[1]
                context_message = self._strategy_context_messages.get(ctx_id, '')
                if not context_message:
                    await query.answer("Message context not found. Please try again.")
                    return

                # Store the context and immediately trigger strategy generation
                self._strategy_generation_context = context_message
                self._landing_menu_selection[chat_id] = 'restart'

                await query.edit_message_text(
                    f"🚀 **Generating New Strategy...**\n\n"
                    f"Using the above message as context. Processing will begin shortly..."
                )

                logger.info(f"Run as New Strategy triggered from chat {chat_id} with context_id {ctx_id}")
                return  # Early return

            # Handle LLM provider selection
            if action == 'llm_provider':
                selected_provider = parts[1]
                bot = query.get_bot()
                try:
                    engine = get_strategy_engine()
                    engine.set_llm_provider(selected_provider)
                    self.db.set_setting('llm_provider', selected_provider)

                    await query.edit_message_text(
                        f"🧠 **LLM Provider switched to: {selected_provider}**"
                    )

                    # Resend last conversational message if one exists
                    last_msg = self._last_chat_message.get(chat_id)
                    if last_msg:
                        await bot.send_message(
                            chat_id=chat_id,
                            text=f"🔄 Re-processing your last message with **{selected_provider}**..."
                        )
                        # Build context from focused strategy (if user viewed one) or pending strategies
                        chat_id_str = str(self.chat_id or chat_id)
                        context_parts = []

                        # Check if user has a focused strategy (from viewing via /33 etc.)
                        focused = self._focused_strategy.get(chat_id)
                        if focused:
                            # Refresh strategy from DB to get current status
                            refreshed = self.db.get_strategy(focused['id'])
                            if refreshed:
                                focused = refreshed
                                self._focused_strategy[chat_id] = refreshed

                            # Only use if still actionable
                            inactive_statuses = {'completed', 'merged', 'discarded', 'max_refinements'}
                            if focused.get('status') not in inactive_statuses:
                                context_parts.append(
                                    f"**Currently discussing Strategy #{focused['id']}** (status: {focused.get('status', 'unknown')}):\n"
                                    f"Summary: {focused['summary']}\n"
                                    f"Focus area: {focused.get('focus_area', 'N/A')}\n"
                                    f"Expected impact: {focused.get('expected_impact', 'N/A')}"
                                )
                            else:
                                # Clear stale focused strategy
                                del self._focused_strategy[chat_id]
                                focused = None

                        if not focused:
                            pending = self.db.get_pending_strategies()
                            if pending:
                                context_parts.append(f"Current pending strategy #{pending[0]['id']}: {pending[0]['summary']}")

                        saved = self.db.get_saved_strategies()
                        if saved:
                            saved_ids = ', '.join(f"#{s['id']}" for s in saved[:5])
                            context_parts.append(f"Saved strategies: {len(saved)} ({saved_ids})")
                        ctx = "\n".join(context_parts) if context_parts else ""

                        from data_collector import DataCollector
                        collector = DataCollector()
                        history_limit = engine.chat_history_limit * 2
                        chat_history = self.db.get_chat_history(chat_id_str, limit=history_limit)

                        response = engine.chat(
                            last_msg, context=ctx, db=self.db, collector=collector,
                            chat_history=chat_history
                        )

                        self.db.log_chat_message(chat_id_str, 'user', last_msg)
                        self.db.log_chat_message(chat_id_str, 'assistant', response)

                        self._strategy_context_counter += 1
                        ctx_id = str(self._strategy_context_counter)
                        self._strategy_context_messages[ctx_id] = response

                        run_as_strategy_markup = InlineKeyboardMarkup([
                            [InlineKeyboardButton("🚀 Run as New Strategy", callback_data=f"run_as_strategy:{ctx_id}")]
                        ])
                        await self._send_chunked(bot, chat_id, response, reply_markup=run_as_strategy_markup)
                    else:
                        # No prior chat message — send welcome intro and landing menu
                        await self.send_welcome_intro(bot=bot)
                        await self._send_landing_menu_with_bot(chat_id, bot)
                except Exception as e:
                    logger.error(f"LLM provider switch failed: {e}", exc_info=True)
                    await bot.send_message(chat_id=chat_id, text=f"⚠️ Failed to switch provider: {e}")
                return  # Early return

            # Handle generic view button (view:{strategy_id}) - works for any strategy regardless of status
            if action == 'view':
                view_id = int(parts[1])
                strategy = self.db.get_strategy(view_id)
                if strategy:
                    # Set this as the focused strategy for chat context
                    self._focused_strategy[chat_id] = strategy
                    logger.info(f"Focused strategy set to #{view_id} from view button for chat {chat_id}")
                    bot = query.get_bot()
                    await self.send_proposal(strategy, bot=bot)
                    await query.edit_message_text(
                        f"📋 **Viewing Strategy #{view_id}**\n\n{query.message.text}"
                    )
                else:
                    await query.edit_message_text(f"❌ Strategy #{view_id} not found.")
                return

            # Handle strategy ID picker buttons (pick:{context}:{strategy_id})
            if action == 'pick':
                pick_context = parts[1]
                pick_id = int(parts[2])
                await query.edit_message_text(
                    f"Selected strategy #{pick_id}..."
                )

                if pick_context == 'saved':
                    if chat_id in self._saved_selection_state and pick_id in self._saved_selection_state[chat_id]:
                        # Simulate the selection by calling the handler logic directly
                        await self._handle_saved_selection_by_id(chat_id, pick_id, query)
                    else:
                        await query.edit_message_text(f"❌ Strategy #{pick_id} not found in current list. Try /saved again.")
                elif pick_context == 'failed':
                    if chat_id in self._failed_selection_state and pick_id in self._failed_selection_state[chat_id]:
                        await self._handle_failed_rejected_selection_by_id(chat_id, pick_id, 'failed', query)
                    else:
                        await query.edit_message_text(f"❌ Strategy #{pick_id} not found. Try /failed again.")
                elif pick_context == 'rejected':
                    if chat_id in self._rejected_selection_state and pick_id in self._rejected_selection_state[chat_id]:
                        await self._handle_failed_rejected_selection_by_id(chat_id, pick_id, 'rejected', query)
                    else:
                        await query.edit_message_text(f"❌ Strategy #{pick_id} not found. Try /rejected again.")
                elif pick_context == 'approved':
                    if chat_id in self._approved_selection_state and pick_id in self._approved_selection_state[chat_id]:
                        await self._handle_approved_selection_by_id(chat_id, pick_id, query)
                    else:
                        await query.edit_message_text(f"❌ Strategy #{pick_id} not found. Try /approved again.")

                logger.info(f"Pick button: context={pick_context}, id={pick_id}, chat={chat_id}")
                return

            strategy_id = int(parts[1])

            response_type = None
            response_text = None
            status = None

            if action == 'save':
                # Save for manual implementation later
                response_type = 'save'
                response_text = 'Saved for later'
                status = 'saved'
                await query.edit_message_text(
                    f"📋 Strategy {strategy_id} saved for manual implementation.\n\n{query.message.text}"
                )
                # Auto-respond with landing menu
                bot = query.get_bot()
                await self._send_landing_menu_with_bot(chat_id, bot)

            elif action == 'approve_queue':
                # Approve strategy and add to approved queue (for execution later)
                # Write to DB immediately so the landing menu shows updated counts
                self.db.write_approval(
                    strategy_id=strategy_id,
                    user_response='Approved for later execution',
                    response_type='approve'
                )
                self.db.update_strategy_status(strategy_id, 'approved')
                await query.edit_message_text(
                    f"✅ Strategy {strategy_id} approved and queued for execution.\n\n{query.message.text}"
                )
                # Auto-respond with landing menu
                bot = query.get_bot()
                await self._send_landing_menu_with_bot(chat_id, bot)
                logger.info(f"Strategy {strategy_id} approved and queued via callback")
                return  # Early return — approval already written

            elif action == 'plan':
                # Generate plan only (no autonomous code execution)
                response_type = 'plan'
                response_text = 'Plan requested'
                status = 'saved'
                bot = query.get_bot()
                await query.edit_message_text(
                    f"📝 Strategy {strategy_id} — plan requested.\n\n{query.message.text}"
                )
                await bot.send_message(
                    chat_id=chat_id,
                    text=f"⏳ Generating implementation plan for Strategy #{strategy_id}... this may take a moment."
                )
                # Generate and save the plan
                try:
                    from pathlib import Path
                    import json as _json
                    strategy = self.db.get_strategy(strategy_id)
                    if strategy:
                        changes = strategy.get('changes', [])
                        if isinstance(changes, str):
                            changes = _json.loads(changes)
                        engine = get_strategy_engine()
                        plan_result = engine.generate_code_plan(strategy, changes)
                        if plan_result.get('success'):
                            plans_dir = Path('cache/code_plans')
                            plans_dir.mkdir(parents=True, exist_ok=True)
                            plan_file = plans_dir / f"strategy_{strategy_id}_plan.md"
                            with open(plan_file, 'w') as pf:
                                pf.write(plan_result['plan_md'])
                            await bot.send_message(
                                chat_id=chat_id,
                                text=f"✅ **Plan saved** for Strategy #{strategy_id}\n\n"
                                     f"📄 `{plan_file}`\n\n"
                                     f"Load the MD file into an LLM for code generation."
                            )
                            # Send init messages so the CEO can continue
                            await self.send_welcome_intro(bot=bot)
                            await self._send_landing_menu_with_bot(chat_id, bot)
                        else:
                            await bot.send_message(
                                chat_id=chat_id,
                                text=f"❌ Plan generation failed: {plan_result.get('error', 'Unknown error')}"
                            )
                            await self.send_welcome_intro(bot=bot)
                            await self._send_landing_menu_with_bot(chat_id, bot)
                except Exception as plan_err:
                    logger.error(f"Plan generation failed for strategy {strategy_id}: {plan_err}", exc_info=True)
                    await bot.send_message(chat_id=chat_id, text=f"❌ Plan generation error: {plan_err}")
                    await self.send_welcome_intro(bot=bot)
                    await self._send_landing_menu_with_bot(chat_id, bot)

            elif action == 'code':
                # Show coding mode selection
                mode_keyboard = InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton("🤖 Autonomous", callback_data=f"coding_mode_auto:{strategy_id}"),
                        InlineKeyboardButton("👤 Interactive", callback_data=f"coding_mode_interactive:{strategy_id}"),
                    ],
                    [
                        InlineKeyboardButton("⬅️ Cancel", callback_data=f"cancel_code:{strategy_id}"),
                    ]
                ])
                await query.edit_message_text(
                    f"🎯 **Select Coding Mode** — Strategy #{strategy_id}\n\n"
                    f"**🤖 Autonomous**: Code will be generated without interruption. "
                    f"Best for straightforward implementations.\n\n"
                    f"**👤 Interactive**: I'll ask clarifying questions during coding. "
                    f"Best for complex changes or when you want more control.\n\n"
                    f"Which mode would you like?",
                    reply_markup=mode_keyboard
                )
                return  # Early return — wait for mode selection

            elif action == 'coding_mode_auto':
                # Autonomous mode selected
                response_type = 'approve'
                response_text = 'Approved for autonomous coding'
                status = 'approved'
                self._landing_menu_selection[chat_id] = f'execute:{strategy_id}'
                await query.edit_message_text(
                    f"🤖 **Autonomous Mode** — Strategy #{strategy_id}\n\n"
                    f"Starting code generation. I'll notify you at each milestone."
                )
                # Store coding mode in notes
                self.db.write_approval(
                    strategy_id=strategy_id,
                    user_response=response_text,
                    response_type=response_type,
                    notes='coding_mode:autonomous'
                )
                self.db.update_strategy_status(strategy_id, status)
                logger.info(f"Strategy {strategy_id} approved for autonomous coding")
                return  # Early return — approval already written

            elif action == 'coding_mode_interactive':
                # Interactive mode selected
                response_type = 'approve'
                response_text = 'Approved for interactive coding'
                status = 'approved'
                self._landing_menu_selection[chat_id] = f'execute:{strategy_id}'
                await query.edit_message_text(
                    f"👤 **Interactive Mode** — Strategy #{strategy_id}\n\n"
                    f"Starting code generation. I'll ask questions when I need clarification."
                )
                # Store coding mode in notes
                self.db.write_approval(
                    strategy_id=strategy_id,
                    user_response=response_text,
                    response_type=response_type,
                    notes='coding_mode:interactive'
                )
                self.db.update_strategy_status(strategy_id, status)
                logger.info(f"Strategy {strategy_id} approved for interactive coding")
                return  # Early return — approval already written

            elif action == 'confirm_code':
                # Legacy handler - treat as autonomous mode
                response_type = 'approve'
                response_text = 'Approved for autonomous coding'
                status = 'approved'
                self._landing_menu_selection[chat_id] = f'execute:{strategy_id}'
                await query.edit_message_text(
                    f"🚀 Strategy {strategy_id} approved! Generating plan and code...\n\n"
                )
                # Store coding mode in notes
                self.db.write_approval(
                    strategy_id=strategy_id,
                    user_response=response_text,
                    response_type=response_type,
                    notes='coding_mode:autonomous'
                )
                self.db.update_strategy_status(strategy_id, status)
                logger.info(f"Strategy {strategy_id} approved (legacy confirm_code)")
                return  # Early return — approval already written

            elif action == 'cancel_code':
                # Cancelled — re-show the original strategy with buttons
                strategy = self.db.get_strategy(strategy_id)
                if strategy:
                    await self.send_proposal(strategy, bot=query.get_bot())
                else:
                    await query.edit_message_text(f"Strategy #{strategy_id} not found.")
                return  # Early return — no approval needed

            elif action == 'delete':
                # Show confirmation before deletion
                confirm_keyboard = InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton("✅ Yes, delete permanently", callback_data=f"confirm_delete:{strategy_id}"),
                        InlineKeyboardButton("⬅️ Cancel", callback_data=f"cancel_delete:{strategy_id}"),
                    ]
                ])
                await query.edit_message_text(
                    f"⚠️ **Confirm Deletion**\n\n"
                    f"Strategy #{strategy_id} will be permanently removed from the database.\n\n"
                    f"Are you sure?",
                    reply_markup=confirm_keyboard
                )
                return  # Early return — don't write approval yet

            elif action == 'confirm_delete':
                # Confirmed — delete from database
                try:
                    self.db.delete_strategy(strategy_id)
                    await query.edit_message_text(f"🗑️ Strategy #{strategy_id} has been deleted.")
                except Exception as e:
                    logger.error(f"Failed to delete strategy {strategy_id}: {e}", exc_info=True)
                    await query.edit_message_text(f"❌ Failed to delete strategy #{strategy_id}: {e}")
                return  # Early return — no approval needed

            elif action == 'cancel_delete':
                # Cancelled — re-show the strategy
                strategy = self.db.get_strategy(strategy_id)
                if strategy:
                    status = strategy.get('status', 'failed')
                    source = 'failed' if status == 'failed' else 'rejected'
                    await self._handle_failed_rejected_selection_by_id(chat_id, strategy_id, source, query)
                else:
                    await query.edit_message_text(f"Strategy #{strategy_id} not found.")
                return  # Early return — no approval needed

            elif action == 'refine':
                # Show "Ready for your refinement" prompt with Cancel button
                cancel_keyboard = InlineKeyboardMarkup([
                    [InlineKeyboardButton("⬅️ Cancel", callback_data=f"cancel_refine:{strategy_id}")]
                ])
                self._refine_session_active[chat_id] = strategy_id
                await query.edit_message_text(
                    f"✏️ **Ready for your refinement**\n\n"
                    f"Strategy #{strategy_id} — send your feedback as a text message.",
                    reply_markup=cancel_keyboard
                )
                return  # Early return — wait for text message

            elif action == 'cancel_refine':
                # Cancel refinement — re-show the strategy
                self._refine_session_active.pop(chat_id, None)
                strategy = self.db.get_strategy(strategy_id)
                if strategy:
                    await self.send_proposal(strategy, bot=query.get_bot())
                else:
                    await query.edit_message_text(f"Strategy #{strategy_id} not found.")
                return  # Early return

            elif action == 'exec':
                # Legacy exec handler (kept for backward compatibility)
                response_type = 'approve'
                response_text = 'Approved for execution'
                status = 'approved'
                self._landing_menu_selection[chat_id] = f'execute:{strategy_id}'
                await query.edit_message_text(
                    f"🚀 Strategy {strategy_id} approved! Generating code and deploying to preview...\n\n{query.message.text}"
                )

            elif action == 'reject':
                response_type = 'reject'
                response_text = 'Rejected'
                status = 'rejected'
                await query.edit_message_text(
                    f"❌ Strategy {strategy_id} rejected.\n\n{query.message.text}"
                )
                # Auto-respond with landing menu
                bot = query.get_bot()
                await self._send_landing_menu_with_bot(chat_id, bot)

            elif action == 'merge':
                # Merge feature branch to develop
                response_type = 'merge'
                response_text = 'Merge approved'
                status = 'merging'
                branch_name = parts[2] if len(parts) > 2 else 'unknown'
                await query.edit_message_text(
                    f"🔀 Merging {branch_name} to develop...\n\n{query.message.text}"
                )

            elif action == 'discard':
                # Discard the feature branch
                response_type = 'discard'
                response_text = 'Preview discarded'
                status = 'discarded'
                await query.edit_message_text(
                    f"🗑️ Strategy {strategy_id} preview discarded.\n\n{query.message.text}"
                )

            elif action == 'defer':
                # Defer decision - keep preview active for later review
                response_type = 'defer'
                response_text = 'Deferred for later'
                status = 'preview'  # Keep status as preview
                await query.edit_message_text(
                    f"⏸️ Strategy {strategy_id} deferred. Preview remains active at the URL above.\n\n{query.message.text}"
                )

            elif action == 'split':
                # Split a multi-change strategy into individual strategies
                await query.edit_message_text(
                    f"✂️ Splitting Strategy #{strategy_id} into individual strategies..."
                )
                bot = query.get_bot()
                try:
                    new_ids = self.db.split_strategy(strategy_id)
                    ids_text = ", ".join([f"#{nid}" for nid in new_ids])
                    await bot.send_message(
                        chat_id=chat_id,
                        text=f"✅ Strategy #{strategy_id} split into {len(new_ids)} individual strategies: {ids_text}\n\n"
                             f"Original strategy has been rejected. Use /saved to view the new strategies."
                    )
                    logger.info(f"Strategy {strategy_id} split into {new_ids}")
                except ValueError as e:
                    await bot.send_message(chat_id=chat_id, text=f"❌ Cannot split: {e}")
                except Exception as e:
                    logger.error(f"Split failed for strategy {strategy_id}: {e}", exc_info=True)
                    await bot.send_message(chat_id=chat_id, text=f"❌ Split failed: {e}")
                return  # Early return - no approval needed

            elif action == 'resume':
                # Resume a stuck strategy - triggers loop_controller resume flow
                # Use 'approve' response_type (database constraint only allows certain types)
                response_type = 'approve'
                response_text = 'Resume requested'
                # Don't change status - let loop_controller handle it based on current state
                strategy = self.db.get_strategy(strategy_id)
                current_status = strategy.get('status', 'unknown') if strategy else 'unknown'
                current_stage = strategy.get('execution_stage', 'unknown') if strategy else 'unknown'
                await query.edit_message_text(
                    f"▶️ Resuming strategy {strategy_id}...\n"
                    f"Current status: {current_status}\n"
                    f"Stage: {current_stage}\n\n"
                    f"Using cached code (if available). Build errors will be fixed automatically."
                )
                # Write approval but don't update status - let loop_controller handle state
                self.db.write_approval(
                    strategy_id=strategy_id,
                    user_response=response_text,
                    response_type=response_type
                )
                # Set landing menu selection so loop controller picks this up
                self._landing_menu_selection[chat_id] = f'execute:{strategy_id}'
                logger.info(f"Strategy {strategy_id} resume requested via callback")
                return  # Early return - we handled DB write manually

            elif action == 'regen':
                # Regenerate - delete cache and start fresh code generation
                response_type = 'approve'
                response_text = 'Regenerate requested'
                strategy = self.db.get_strategy(strategy_id)
                current_status = strategy.get('status', 'unknown') if strategy else 'unknown'
                current_stage = strategy.get('execution_stage', 'unknown') if strategy else 'unknown'

                # Delete the code cache to force fresh generation
                code_cache = get_code_cache()
                cache_deleted = code_cache.delete(strategy_id)

                await query.edit_message_text(
                    f"🔄 Regenerating strategy {strategy_id}...\n"
                    f"Current status: {current_status}\n"
                    f"Stage: {current_stage}\n\n"
                    f"{'Code cache deleted. ' if cache_deleted else ''}Generating all code from scratch."
                )
                # Write approval but don't update status - let loop_controller handle state
                self.db.write_approval(
                    strategy_id=strategy_id,
                    user_response=response_text,
                    response_type=response_type
                )
                # Set landing menu selection so loop controller picks this up
                self._landing_menu_selection[chat_id] = f'execute:{strategy_id}'
                logger.info(f"Strategy {strategy_id} regenerate requested via callback (cache deleted: {cache_deleted})")
                return  # Early return - we handled DB write manually

            elif action == 'reapprove_save':
                # Re-approve a failed/rejected strategy and save for manual implementation
                response_type = 'save'
                response_text = 'Re-approved and saved for later'
                status = 'saved'
                await query.edit_message_text(
                    f"📋 Strategy {strategy_id} re-approved and saved for manual implementation.\n\n{query.message.text}"
                )
                # Auto-respond with landing menu
                bot = query.get_bot()
                await self._send_landing_menu_with_bot(chat_id, bot)

            elif action == 'reapprove_approve':
                # Re-approve a failed/rejected strategy and queue for later execution
                # Write to DB immediately so the landing menu shows updated counts
                self.db.write_approval(
                    strategy_id=strategy_id,
                    user_response='Re-approved and queued for execution',
                    response_type='approve'
                )
                self.db.update_strategy_status(strategy_id, 'approved')
                await query.edit_message_text(
                    f"✅ Strategy {strategy_id} re-approved and queued for execution.\n\n{query.message.text}"
                )
                # Auto-respond with landing menu
                bot = query.get_bot()
                await self._send_landing_menu_with_bot(chat_id, bot)
                logger.info(f"Strategy {strategy_id} re-approved and queued via callback")
                return  # Early return — approval already written

            elif action == 'keep_rejected':
                # Keep the strategy as rejected (no change)
                await query.edit_message_text(
                    f"🗑️ Strategy {strategy_id} kept as rejected.\n\n{query.message.text}"
                )
                logger.info(f"Strategy {strategy_id} kept as rejected via callback")
                # Auto-respond with landing menu
                bot = query.get_bot()
                await self._send_landing_menu_with_bot(chat_id, bot)
                return  # Early return - no DB changes needed

            elif action == 'manual_complete':
                # Mark strategy as manually completed outside of automated cycles
                response_type = 'manual_complete'
                response_text = 'Manually completed'
                status = 'completed'
                await query.edit_message_text(
                    f"✅ Strategy {strategy_id} marked as manually completed.\n\n{query.message.text}"
                )
                # Auto-respond with landing menu
                bot = query.get_bot()
                await self._send_landing_menu_with_bot(chat_id, bot)

            elif action == 'qa_try_fix':
                # CEO wants to try a fix based on Q&A conversation
                self.db.write_approval(
                    strategy_id=strategy_id,
                    user_response='Try fix',
                    response_type='qa_try_fix'
                )
                await query.edit_message_text(
                    f"Generating fix based on our conversation...\n\n{query.message.text}"
                )
                logger.info(f"Q&A try fix requested for strategy {strategy_id}")
                return

            elif action == 'qa_give_up':
                # CEO wants to give up on this error
                self.db.write_approval(
                    strategy_id=strategy_id,
                    user_response='Give up',
                    response_type='qa_give_up'
                )
                await query.edit_message_text(
                    f"Understood, skipping this error.\n\n{query.message.text}"
                )
                logger.info(f"Q&A give up for strategy {strategy_id}")
                return

            elif action in ('build_save', 'build_fail', 'build_reject', 'build_continue', 'build_commit_save'):
                # Build failure decision buttons
                labels = {
                    'build_save': 'Saving strategy for later',
                    'build_fail': 'Marking as failed',
                    'build_reject': 'Rejecting strategy',
                    'build_continue': 'Starting interactive troubleshooting...',
                    'build_commit_save': 'Committing code and saving strategy...',
                }
                self.db.write_approval(
                    strategy_id=strategy_id,
                    user_response=labels.get(action, action),
                    response_type=action
                )
                await query.edit_message_text(
                    f"{labels.get(action, action)}\n\n{query.message.text}"
                )
                logger.info(f"Build failure decision: {action} for strategy {strategy_id}")
                return

            elif action == 'coding_answer':
                # Handle coding question answer selection
                # Format: coding_answer:question_id:option_index
                question_id = parts[1]
                option_index = int(parts[2])
                if question_id in self._coding_questions:
                    options = self._coding_questions[question_id].get('options', [])
                    if 0 <= option_index < len(options):
                        answer = options[option_index]
                        self.set_coding_answer(question_id, answer)
                        await query.edit_message_text(
                            f"✅ **Answer Received**\n\n"
                            f"You selected: **{answer}**\n\n"
                            f"Continuing code generation..."
                        )
                    else:
                        await query.edit_message_text("❌ Invalid option selected")
                else:
                    await query.edit_message_text("❌ Question expired or already answered")
                return

            elif action == 'coding_answer_custom':
                # User wants to type a custom answer
                question_id = parts[1]
                if question_id in self._coding_questions:
                    # Store that we're waiting for a typed answer
                    self._coding_questions[question_id]['waiting_custom'] = True
                    self._coding_questions[question_id]['chat_id'] = chat_id
                    await query.edit_message_text(
                        f"✏️ **Type Your Answer**\n\n"
                        f"Please type your answer as a message.\n\n"
                        f"Original question:\n{self._coding_questions[question_id]['question']}"
                    )
                else:
                    await query.edit_message_text("❌ Question expired or already answered")
                return

            # Log approval in database
            if response_type:
                self.db.write_approval(
                    strategy_id=strategy_id,
                    user_response=response_text,
                    response_type=response_type
                )
                self.db.update_strategy_status(strategy_id, status)

                logger.info(f"Strategy {strategy_id} {response_type} via callback")

        except Exception as e:
            logger.error(f"Error handling callback: {e}")
            await query.edit_message_text("❌ Error processing response")

    async def _handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle text message responses"""
        logger.info(f"_handle_message triggered - received message from chat")

        # Safety check for update.message
        if not update.message or not update.message.text:
            logger.warning("Received update without message or text")
            return

        text = update.message.text.strip()
        text_lower = text.lower()
        chat_id = update.message.chat_id

        logger.info(f"Message received: '{text[:50]}...' from chat_id: {chat_id}")

        # Check if this is the authorized chat (if configured)
        if self.chat_id and str(chat_id) != str(self.chat_id).strip():
            logger.warning(f"Unauthorized chat attempt: {chat_id} (expected: {self.chat_id})")
            await update.message.reply_text(f"⚠️ Unauthorized. Your chat ID: {chat_id}")
            return

        # Quick strategy lookup: /25 opens Strategy #25
        import re as _re
        strategy_shortcut = _re.match(r'^/(\d+)$', text)
        if strategy_shortcut:
            sid = int(strategy_shortcut.group(1))
            strategy = self.db.get_strategy(sid)
            if strategy:
                # Set this as the focused strategy for subsequent chat context
                self._focused_strategy[chat_id] = strategy
                logger.info(f"Focused strategy set to #{sid} for chat {chat_id}")
                await self.send_proposal(strategy, bot=context.bot)
            else:
                await update.message.reply_text(f"Strategy #{sid} not found.")
            return

        # Check if waiting for a custom coding answer
        for question_id, q_state in list(self._coding_questions.items()):
            if q_state.get('waiting_custom') and q_state.get('chat_id') == chat_id:
                self.set_coding_answer(question_id, text)
                q_state['waiting_custom'] = False
                await update.message.reply_text(
                    f"✅ **Answer Received**\n\n"
                    f"Your answer: \"{text[:100]}{'...' if len(text) > 100 else ''}\"\n\n"
                    f"Continuing code generation..."
                )
                return

        # Check if Q&A troubleshooting session is active - route all text to Q&A
        if chat_id in self._qa_session_active:
            strategy_id = self._qa_session_active[chat_id]
            logger.info(f"Q&A session active for strategy {strategy_id}, routing message as qa_response")
            self.db.write_approval(
                strategy_id=strategy_id,
                user_response=text,
                response_type='qa_response'
            )
            await update.message.reply_text("Researching your question...")
            return

        # Check if refinement session is active - route text as refinement feedback
        if chat_id in self._refine_session_active:
            strategy_id = self._refine_session_active.pop(chat_id)
            logger.info(f"Refinement session active for strategy {strategy_id}, processing feedback")
            try:
                self.db.write_approval(
                    strategy_id=strategy_id,
                    user_response=text,
                    response_type='tweak',
                    notes=text
                )
                self.db.update_strategy_status(strategy_id, 'pending')
                await update.message.reply_text(
                    f"📝 Got it! Refining strategy #{strategy_id} based on: "
                    f"\"{text[:50]}{'...' if len(text) > 50 else ''}\""
                )

                # Perform inline refinement
                strategy_engine = get_strategy_engine()
                original_strategy = self.db.get_strategy(strategy_id)
                if original_strategy:
                    self.db.update_strategy_status(strategy_id, 'rejected')
                    refined = strategy_engine.refine_strategy(original_strategy, text, {})
                    refined_id = self.db.write_strategy(refined)
                    refined['id'] = refined_id
                    logger.info(f"Inline refinement: strategy {strategy_id} -> {refined_id}")
                    await self.send_proposal(refined, bot=context.bot)
            except Exception as refine_err:
                logger.error(f"Refinement failed: {refine_err}", exc_info=True)
                await update.message.reply_text(f"⚠️ Refinement failed: {refine_err}")
            return

        # Check if user is selecting from resume list (stuck strategies)
        if hasattr(self, '_resume_selection_state') and self._resume_selection_state and text.isdigit():
            strategy_id = int(text)
            if strategy_id in self._resume_selection_state:
                await self._handle_resume_selection(update, strategy_id)
                return

        # Check if user is typing a number that matches a stuck strategy
        # (handles case where loop_controller sent notification asking for ID)
        if text.isdigit():
            typed_id = int(text)
            stuck = self.db.get_stuck_strategies()
            stuck_ids = {s['id'] for s in stuck}
            if typed_id in stuck_ids:
                # User is trying to resume a stuck strategy
                strategy = next((s for s in stuck if s['id'] == typed_id), None)
                if strategy:
                    self._resume_selection_state = {s['id']: s for s in stuck}
                    await self._handle_resume_selection(update, typed_id)
                    return

        # Check if user is selecting from saved strategies list
        if chat_id in self._saved_selection_state and text.isdigit():
            await self._handle_saved_selection(update, int(text))
            return

        # Check if user is selecting from failed strategies list
        if chat_id in self._failed_selection_state and text.isdigit():
            await self._handle_failed_rejected_selection(update, int(text), 'failed')
            return

        # Check if user is selecting from rejected strategies list
        if chat_id in self._rejected_selection_state and text.isdigit():
            await self._handle_failed_rejected_selection(update, int(text), 'rejected')
            return

        # Check if user is selecting from approved strategies list
        if chat_id in self._approved_selection_state and text.isdigit():
            await self._handle_approved_selection(update, int(text))
            return

        try:
            # Get most recent pending strategy
            pending = self.db.get_pending_strategies()
            if not pending:
                # Check if user might be trying to select from saved list
                if text.isdigit():
                    await update.message.reply_text(
                        "No saved strategies list active. Use /saved to view saved strategies first."
                    )
                    return
                # No pending strategy — route to conversational LLM
                await self._handle_conversation(update, text)
                return

            strategy = pending[0]
            strategy_id = strategy['id']

            # Parse response - be smart about recognizing intent
            # Save for manual implementation
            if text_lower in ['save', 'later', 'manual']:
                response_type = 'save'
                response_text = 'Saved for later'
                status = 'saved'
                msg = f"📋 Strategy {strategy_id} saved for manual implementation."

            # Plan only (generate MD plan file for human-driven code generation)
            elif text_lower in ['plan']:
                response_type = 'plan'
                response_text = 'Plan requested'
                status = 'saved'
                msg = f"📝 Strategy {strategy_id} — generating implementation plan..."

            # Execute now (code autonomously)
            elif text_lower in ['code', 'exec', 'execute', 'run', 'deploy', 'ship it', 'lgtm', 'execute now']:
                # Use 'approve' as response_type and 'approved' as status (both valid in DB)
                response_type = 'approve'
                response_text = 'Approved for autonomous coding'
                status = 'approved'
                self._landing_menu_selection[chat_id] = f'execute:{strategy_id}'
                msg = f"🚀 Strategy {strategy_id} approved! Generating plan and code..."

            # Approve for later (queue to approved queue without executing)
            elif text_lower in ['approve', 'approved', 'yes', 'ok', 'okay', 'go', 'do it']:
                response_type = 'approve'
                response_text = 'Approved for later execution'
                status = 'approved'
                msg = f"✅ Strategy {strategy_id} approved and queued for execution."
                # Write DB immediately so landing menu shows updated counts
                self.db.write_approval(
                    strategy_id=strategy_id,
                    user_response=response_text,
                    response_type=response_type
                )
                self.db.update_strategy_status(strategy_id, status)
                await update.message.reply_text(msg)
                await self._send_landing_menu(chat_id)
                logger.info(f"Strategy {strategy_id} approved and queued via message")
                return  # Early return — already handled

            # Explicit reject
            elif text_lower in ['reject', 'rejected', 'no', 'nope', 'cancel', 'stop', 'skip']:
                response_type = 'reject'
                response_text = 'Rejected'
                status = 'rejected'
                msg = f"❌ Strategy {strategy_id} rejected."

            # Explicit refinement via "tweak:" prefix
            elif text_lower.startswith('tweak:') or text_lower.startswith('tweak '):
                response_type = 'tweak'
                feedback_text = text[6:].strip()
                response_text = feedback_text
                status = 'pending'
                msg = f"📝 Got it! Refining strategy based on: \"{feedback_text[:50]}{'...' if len(feedback_text) > 50 else ''}\""

            # Everything else is conversational — route to LLM
            else:
                await self._handle_conversation(update, text)
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
            logger.info(f"Strategy {strategy_id} {response_type} via message: {text[:50]}")

            # For tweaks: perform inline refinement and send new proposal
            if response_type == 'tweak':
                try:
                    strategy_engine = get_strategy_engine()
                    original_strategy = self.db.get_strategy(strategy_id)
                    if original_strategy:
                        self.db.update_strategy_status(strategy_id, 'rejected')
                        refined = strategy_engine.refine_strategy(
                            original_strategy, feedback_text, {}
                        )
                        refined_id = self.db.write_strategy(refined)
                        refined['id'] = refined_id
                        logger.info(f"Inline refinement: strategy {strategy_id} -> {refined_id}")
                        await self.send_proposal(refined, bot=context.bot)
                except Exception as refine_err:
                    logger.error(f"Inline refinement failed: {refine_err}", exc_info=True)
                    await update.message.reply_text(
                        f"⚠️ Refinement failed: {refine_err}\nOriginal strategy kept as pending."
                    )

            # For plan: generate MD plan file
            if response_type == 'plan':
                try:
                    from pathlib import Path
                    import json as _json
                    strategy = self.db.get_strategy(strategy_id)
                    if strategy:
                        changes = strategy.get('changes', [])
                        if isinstance(changes, str):
                            changes = _json.loads(changes)
                        engine = get_strategy_engine()
                        plan_result = engine.generate_code_plan(strategy, changes)
                        if plan_result.get('success'):
                            plans_dir = Path('cache/code_plans')
                            plans_dir.mkdir(parents=True, exist_ok=True)
                            plan_file = plans_dir / f"strategy_{strategy_id}_plan.md"
                            with open(plan_file, 'w') as pf:
                                pf.write(plan_result['plan_md'])
                            await update.message.reply_text(
                                f"✅ **Plan saved** for Strategy #{strategy_id}\n\n"
                                f"📄 `{plan_file}`\n\n"
                                f"Load the MD file into an LLM for code generation."
                            )
                        else:
                            await update.message.reply_text(
                                f"❌ Plan generation failed: {plan_result.get('error', 'Unknown error')}"
                            )
                except Exception as plan_err:
                    logger.error(f"Plan generation failed: {plan_err}", exc_info=True)
                    await update.message.reply_text(f"❌ Plan generation error: {plan_err}")

        except Exception as e:
            logger.error(f"Error handling message: {e}")
            await update.message.reply_text("❌ Error processing response")

    async def _handle_conversation(self, update: Update, text: str):
        """Route a conversational message to the LLM and reply"""
        try:
            engine = get_strategy_engine()
            chat_id = str(self.chat_id or update.effective_chat.id)

            # Track last conversational message for resend after provider switch
            self._last_chat_message[int(chat_id)] = text

            # Load chat history from DB (limit * 2 because limit is exchanges, not messages)
            history_limit = engine.chat_history_limit * 2
            chat_history = self.db.get_chat_history(chat_id, limit=history_limit)

            # Build context from focused strategy (if user viewed one) or pending strategies
            context_parts = []
            chat_id_int = int(chat_id)

            # Check if user has a focused strategy (from viewing via /33 etc.)
            focused = self._focused_strategy.get(chat_id_int)
            if focused:
                # Refresh strategy from DB to get current status
                refreshed = self.db.get_strategy(focused['id'])
                if refreshed:
                    focused = refreshed
                    self._focused_strategy[chat_id_int] = refreshed

                # Only use focused strategy if it's still actionable (not completed/merged/discarded)
                inactive_statuses = {'completed', 'merged', 'discarded', 'max_refinements'}
                if focused.get('status') not in inactive_statuses:
                    # Include full context for the focused strategy
                    context_parts.append(
                        f"**Currently discussing Strategy #{focused['id']}** (status: {focused.get('status', 'unknown')}):\n"
                        f"Summary: {focused['summary']}\n"
                        f"Focus area: {focused.get('focus_area', 'N/A')}\n"
                        f"Expected impact: {focused.get('expected_impact', 'N/A')}"
                    )
                    logger.info(f"Using focused strategy #{focused['id']} for chat context")
                else:
                    # Strategy is no longer actionable - clear it and fall back to pending
                    logger.info(f"Clearing stale focused strategy #{focused['id']} (status: {focused.get('status')})")
                    del self._focused_strategy[chat_id_int]
                    focused = None

            if not focused:
                # Fall back to pending strategy if no focused strategy
                pending = self.db.get_pending_strategies()
                if pending:
                    context_parts.append(f"Current pending strategy #{pending[0]['id']}: {pending[0]['summary']}")

            # Also include list of saved strategies for reference
            saved = self.db.get_saved_strategies()
            if saved:
                saved_ids = ', '.join(f"#{s['id']}" for s in saved[:5])
                context_parts.append(f"Saved strategies: {len(saved)} ({saved_ids})")

            context = "\n".join(context_parts) if context_parts else ""

            collector = DataCollector()
            response = engine.chat(
                text, context=context, db=self.db, collector=collector,
                chat_history=chat_history
            )

            # Log both messages to history
            self.db.log_chat_message(chat_id, 'user', text)
            self.db.log_chat_message(chat_id, 'assistant', response)

            # Store the response message for "Run as New Strategy" button
            self._strategy_context_counter += 1
            ctx_id = str(self._strategy_context_counter)
            self._strategy_context_messages[ctx_id] = response

            # Build dynamic action buttons based on what the LLM suggests
            buttons = self._extract_action_buttons(response, ctx_id)

            reply_markup = InlineKeyboardMarkup(buttons) if buttons else None
            bot = update.get_bot()
            await self._send_chunked(bot, int(chat_id), response, reply_markup=reply_markup)
        except Exception as e:
            logger.error(f"Conversation error: {e}", exc_info=True)
            await update.message.reply_text(f"Sorry, I had trouble processing that: {e}")

    async def send_proposal(self, strategy: Dict, bot=None) -> bool:
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

            # Update focused strategy for chat context (when user views a strategy proposal,
            # subsequent chat messages should be aware of this strategy)
            chat_id_int = int(self.chat_id)
            self._focused_strategy[chat_id_int] = strategy
            logger.info(f"Focused strategy updated to #{strategy_id} via send_proposal")
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

            dep_text = self._format_dependency_analysis(strategy)

            message = f"""
🎯 **New Optimization Strategy #{strategy_id}**

**Summary:** {summary}

**Focus Area:** {focus_area}
**Priority:** {priority}/10
**Expected Impact:** {expected_impact}

**Proposed Changes:**
{changes_text}
{dep_text}

**What should I do?**
"""

            # Create inline keyboard with options
            keyboard = [
                [
                    InlineKeyboardButton("✅ Approve", callback_data=f"approve_queue:{strategy_id}"),
                    InlineKeyboardButton("📋 Save", callback_data=f"save:{strategy_id}"),
                    InlineKeyboardButton("🚀 Execute Now", callback_data=f"code:{strategy_id}"),
                ],
                [
                    InlineKeyboardButton("📝 Plan", callback_data=f"plan:{strategy_id}"),
                    InlineKeyboardButton("✏️ Refine", callback_data=f"refine:{strategy_id}"),
                    InlineKeyboardButton("❌ Reject", callback_data=f"reject:{strategy_id}"),
                ],
                [InlineKeyboardButton("✅ Manually Completed", callback_data=f"manual_complete:{strategy_id}")]
            ]
            if len(changes) > 1:
                keyboard.append([InlineKeyboardButton(f"✂️ Split into {len(changes)} strategies", callback_data=f"split:{strategy_id}")])
            keyboard.append([InlineKeyboardButton("⬅️ Back to Menu", callback_data="back_to_menu")])
            reply_markup = InlineKeyboardMarkup(keyboard)

            # Send message using provided bot or fallback to sender bot
            send_bot = bot or self._sender_bot
            await send_bot.send_message(
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

            await self._sender_bot.send_message(
                chat_id=self.chat_id,
                text=alert_msg
            )

            logger.info(f"Alert sent: {severity}")
            return True

        except Exception as e:
            logger.error(f"Error sending alert: {e}")
            return False

    def send_alert_sync(self, message: str, severity: str = 'medium') -> bool:
        """
        Synchronous wrapper for send_alert that safely creates its own event loop.
        Use this when calling from the main thread where there may be event loop conflicts.
        """
        import asyncio
        from telegram import Bot

        async def _send_alert_with_fresh_bot():
            """Send alert using a bot created in this event loop"""
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

                # Create bot in this event loop
                bot = Bot(token=self.token)
                await bot.send_message(chat_id=self.chat_id, text=alert_msg)

                logger.info(f"Alert sent: {severity}")
                return True

            except Exception as e:
                logger.error(f"Error sending alert: {e}")
                return False

        try:
            # Create a new event loop for this thread
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                return loop.run_until_complete(_send_alert_with_fresh_bot())
            finally:
                loop.close()
        except Exception as e:
            logger.error(f"Error in send_alert_sync: {e}")
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
            await self._sender_bot.send_message(
                chat_id=self.chat_id,
                text=f"📢 {message}"
            )

            return True

        except Exception as e:
            logger.error(f"Error sending status update: {e}")
            return False

    def send_status_update_sync(self, message: str) -> bool:
        """
        Synchronous wrapper for send_status_update that safely creates its own event loop.
        Use this when calling from the main thread where there may be event loop conflicts.
        """
        import asyncio
        from telegram import Bot

        async def _send_with_fresh_bot():
            """Send status update using a bot created in this event loop"""
            if not self.chat_id:
                return False

            try:
                bot = Bot(token=self.token)
                await bot.send_message(chat_id=self.chat_id, text=f"📢 {message}")
                return True

            except Exception as e:
                logger.error(f"Error sending status update: {e}")
                return False

        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                return loop.run_until_complete(_send_with_fresh_bot())
            finally:
                loop.close()
        except Exception as e:
            logger.error(f"Error in send_status_update_sync: {e}")
            return False

    def ask_coding_question_sync(self, strategy_id: int, question: str,
                                   options: List[str] = None,
                                   timeout_seconds: int = 300) -> Optional[str]:
        """
        Ask an interactive coding question and wait for the CEO's answer.

        Args:
            strategy_id: The strategy being coded
            question: The question to ask
            options: Optional list of answer options (will create buttons)
            timeout_seconds: How long to wait for an answer (default 5 minutes)

        Returns:
            The user's answer string, or None if timeout
        """
        import asyncio
        import time
        from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup

        # Generate unique question ID
        self._coding_question_counter += 1
        question_id = f"cq_{strategy_id}_{self._coding_question_counter}"

        # Store question state
        self._coding_questions[question_id] = {
            'question': question,
            'options': options,
            'answer': None,
            'answered': False
        }

        async def _send_question():
            if not self.chat_id:
                return False

            try:
                bot = Bot(token=self.token)

                # Build keyboard with options
                keyboard = []
                if options:
                    # Add option buttons (2 per row)
                    row = []
                    for i, opt in enumerate(options):
                        row.append(InlineKeyboardButton(
                            opt[:30],  # Truncate long options
                            callback_data=f"coding_answer:{question_id}:{i}"
                        ))
                        if len(row) == 2:
                            keyboard.append(row)
                            row = []
                    if row:
                        keyboard.append(row)

                # Always add "Let me type..." option for custom answer
                keyboard.append([
                    InlineKeyboardButton(
                        "✏️ Let me type my answer...",
                        callback_data=f"coding_answer_custom:{question_id}"
                    )
                ])

                reply_markup = InlineKeyboardMarkup(keyboard)

                await bot.send_message(
                    chat_id=self.chat_id,
                    text=f"🤔 **Coding Question** — Strategy #{strategy_id}\n\n{question}",
                    reply_markup=reply_markup
                )
                return True

            except Exception as e:
                logger.error(f"Error sending coding question: {e}")
                return False

        # Send the question
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                sent = loop.run_until_complete(_send_question())
            finally:
                loop.close()

            if not sent:
                return None

        except Exception as e:
            logger.error(f"Error in ask_coding_question_sync: {e}")
            return None

        # Wait for answer
        logger.info(f"Waiting for coding question answer: {question_id}")
        start_time = time.time()
        while time.time() - start_time < timeout_seconds:
            if self._coding_questions[question_id]['answered']:
                answer = self._coding_questions[question_id]['answer']
                # Clean up
                del self._coding_questions[question_id]
                logger.info(f"Coding question answered: {answer}")
                return answer
            time.sleep(2)  # Poll every 2 seconds

        # Timeout - clean up and return None
        logger.warning(f"Coding question timeout: {question_id}")
        del self._coding_questions[question_id]
        return None

    def set_coding_answer(self, question_id: str, answer: str):
        """Set the answer for a pending coding question (called by callback handler)"""
        if question_id in self._coding_questions:
            self._coding_questions[question_id]['answer'] = answer
            self._coding_questions[question_id]['answered'] = True
            logger.info(f"Coding answer set for {question_id}: {answer}")

    def activate_qa_session(self, chat_id: int, strategy_id: int):
        """Activate Q&A troubleshooting session for a chat"""
        self._qa_session_active[chat_id] = strategy_id
        logger.info(f"Q&A session activated for chat {chat_id}, strategy {strategy_id}")

    def deactivate_qa_session(self, chat_id: int):
        """Deactivate Q&A troubleshooting session for a chat"""
        self._qa_session_active.pop(chat_id, None)
        logger.info(f"Q&A session deactivated for chat {chat_id}")

    def send_troubleshooting_prompt(self, strategy_id: int, analysis_text: str) -> bool:
        """
        Send error analysis with Try Fix / Give Up buttons to CEO.
        Synchronous wrapper for Telegram message with inline keyboard.
        """
        import asyncio
        from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup

        async def _send():
            if not self.chat_id:
                return False
            try:
                bot = Bot(token=self.token)
                keyboard = [
                    [
                        InlineKeyboardButton("Try Fix", callback_data=f"qa_try_fix:{strategy_id}"),
                        InlineKeyboardButton("Give Up", callback_data=f"qa_give_up:{strategy_id}"),
                    ]
                ]
                reply_markup = InlineKeyboardMarkup(keyboard)
                await bot.send_message(
                    chat_id=self.chat_id,
                    text=analysis_text,
                    reply_markup=reply_markup
                )
                return True
            except Exception as e:
                logger.error(f"Error sending troubleshooting prompt: {e}")
                return False

        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                return loop.run_until_complete(_send())
            finally:
                loop.close()
        except Exception as e:
            logger.error(f"Error in send_troubleshooting_prompt: {e}")
            return False

    def send_build_failure_prompt(self, strategy_id: int, summary_text: str) -> bool:
        """
        Send build failure summary with action buttons to CEO.
        Buttons: Save, Fail, Reject, Continue (Q&A), Commit & Save
        """
        import asyncio
        from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup

        async def _send():
            if not self.chat_id:
                return False
            try:
                bot = Bot(token=self.token)
                keyboard = [
                    [
                        InlineKeyboardButton("Save", callback_data=f"build_save:{strategy_id}"),
                        InlineKeyboardButton("Fail", callback_data=f"build_fail:{strategy_id}"),
                        InlineKeyboardButton("Reject", callback_data=f"build_reject:{strategy_id}"),
                    ],
                    [
                        InlineKeyboardButton("Continue (Q&A)", callback_data=f"build_continue:{strategy_id}"),
                        InlineKeyboardButton("Commit & Save", callback_data=f"build_commit_save:{strategy_id}"),
                    ]
                ]
                reply_markup = InlineKeyboardMarkup(keyboard)
                await bot.send_message(
                    chat_id=self.chat_id,
                    text=summary_text,
                    reply_markup=reply_markup
                )
                return True
            except Exception as e:
                logger.error(f"Error sending build failure prompt: {e}")
                return False

        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                return loop.run_until_complete(_send())
            finally:
                loop.close()
        except Exception as e:
            logger.error(f"Error in send_build_failure_prompt: {e}")
            return False

    async def send_preview_link(self, strategy_id: int, preview_url: str,
                                 branch_name: str, changes_summary: str,
                                 github_url: str = None,
                                 vercel_deployment_url: str = None) -> bool:
        """
        Send a preview link with merge/discard buttons

        Args:
            strategy_id: Strategy ID
            preview_url: Vercel preview app URL
            branch_name: Git branch name
            changes_summary: Summary of changes made
            github_url: GitHub URL to the feature branch
            vercel_deployment_url: Vercel deployment dashboard URL

        Returns:
            True if sent successfully
        """
        if not self.chat_id:
            return False

        try:
            # Build links section
            links_section = f"🌐 **Preview App:** {preview_url}\n"
            if github_url:
                links_section += f"📂 **GitHub Branch:** {github_url}\n"
            if vercel_deployment_url:
                links_section += f"🚀 **Vercel Deployment:** {vercel_deployment_url}\n"

            message = f"""
🔍 **Preview Ready - Strategy #{strategy_id}**

{links_section}
**Branch:** `{branch_name}`

**Changes Made:**
{changes_summary}

Please review the preview and choose:
- **Merge to develop** if it looks good
- **Discard** to delete the branch
"""

            # Create inline keyboard with merge/discard/defer options
            keyboard = [
                [
                    InlineKeyboardButton("✅ Merge to develop", callback_data=f"merge:{strategy_id}:{branch_name}"),
                ],
                [
                    InlineKeyboardButton("🗑️ Discard", callback_data=f"discard:{strategy_id}"),
                    InlineKeyboardButton("⏸️ Defer", callback_data=f"defer:{strategy_id}")
                ]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)

            await self._sender_bot.send_message(
                chat_id=self.chat_id,
                text=message,
                reply_markup=reply_markup
            )

            logger.info(f"Preview link sent for strategy {strategy_id}")
            return True

        except Exception as e:
            logger.error(f"Error sending preview link: {e}")
            return False

    def run_polling(self, in_thread: bool = False):
        """
        Run the bot in polling mode

        Args:
            in_thread: If True, use asyncio-compatible method for background threads
        """
        logger.info("Starting Telegram bot in polling mode...")
        logger.info(f"Bot token configured: {'Yes' if self.token else 'No'}")
        logger.info(f"Chat ID configured: {self.chat_id}")

        if in_thread:
            # When running in a background thread, we can't use run_polling()
            # because it tries to set up signal handlers which only work in main thread.
            # Instead, we create a new event loop and use start_polling()/stop_polling()
            import asyncio
            logger.info("Running in thread mode - using manual event loop...")

            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

            async def start_bot():
                await self.app.initialize()
                await self.app.start()
                await self.app.updater.start_polling(
                    allowed_updates=["message", "callback_query", "edited_message"],
                    drop_pending_updates=True
                )
                logger.info("Bot polling started in thread mode")

            try:
                # Start the bot
                loop.run_until_complete(start_bot())
                # Run the event loop forever to process updates
                loop.run_forever()
            except Exception as e:
                logger.error(f"Bot polling error: {e}")
            finally:
                loop.close()
        else:
            # Normal mode - run in main thread with signal handlers
            logger.info("Calling app.run_polling() - bot will now listen for updates...")
            self.app.run_polling(
                allowed_updates=["message", "callback_query", "edited_message"],
                drop_pending_updates=True
            )

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
