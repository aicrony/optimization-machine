"""
Interactive CEO Troubleshooting System

When automated build fixes fail after consecutive attempts, this module
provides an interactive Q&A loop with the CEO via Telegram. The CEO can
ask questions (LLM researches the codebase), provide fix instructions,
or choose to give up.

General-purpose: callable from any failure point in the system.
"""

import os
import json
import time
import logging
import anthropic
from pathlib import Path
from typing import Dict, List, Any, Optional
from datetime import datetime

from db_handler import get_db_handler
from mobile_interface import get_mobile_interface

logger = logging.getLogger(__name__)


class TroubleshooterAgent:
    """
    Interactive troubleshooting assistant for build/deployment failures.
    Provides LLM-powered Q&A interface via Telegram.
    """

    def __init__(self):
        self.db = get_db_handler()
        self.mobile = get_mobile_interface()
        self.client = anthropic.Anthropic()
        self.gentube_app_path = os.getenv('GENTUBE_APP_PATH', '../gentube-app')

    def start_session(self, failure_type: str, error_output: str,
                      strategy_id: int, context: Dict = None) -> Dict[str, Any]:
        """
        Entry point for interactive troubleshooting.

        Args:
            failure_type: Type of failure ('build_error', 'vercel_error', etc.)
            error_output: Raw error output text
            strategy_id: Strategy being executed
            context: Additional context (attempt number, branch name, etc.)

        Returns:
            {'action': 'try_fix', 'fixes': [...]} or {'action': 'give_up'}
        """
        context = context or {}
        logger.info(f"Starting troubleshooting session for strategy {strategy_id} ({failure_type})")

        try:
            # Step 1: Analyze error and send to CEO
            analysis = self._send_error_analysis(
                error_output=error_output,
                failure_type=failure_type,
                strategy_id=strategy_id,
                context=context
            )

            # Step 2: Activate Q&A session on mobile interface
            chat_id = int(self.mobile.chat_id) if self.mobile.chat_id else None
            if chat_id:
                self.mobile.activate_qa_session(chat_id, strategy_id)

            # Step 3: Enter Q&A loop
            conversation_history = [
                {
                    'role': 'system',
                    'content': analysis,
                    'timestamp': datetime.utcnow().isoformat()
                }
            ]

            result = self._qa_loop(
                strategy_id=strategy_id,
                error_output=error_output,
                error_context=analysis,
                conversation_history=conversation_history,
                timeout_minutes=30
            )

            return result

        except Exception as e:
            logger.error(f"Troubleshooting session failed: {e}")
            return {'action': 'give_up', 'error': str(e)}

        finally:
            # Deactivate Q&A session
            chat_id = int(self.mobile.chat_id) if self.mobile.chat_id else None
            if chat_id:
                self.mobile.deactivate_qa_session(chat_id)

    def _send_error_analysis(self, error_output: str, failure_type: str,
                             strategy_id: int, context: Dict) -> str:
        """
        Analyze error with Haiku and send formatted analysis to CEO.

        Returns:
            Analysis text string
        """
        # Use Haiku to create a human-readable analysis
        prompt = f"""Analyze this {failure_type} and create a clear summary for a non-technical CEO.

ERROR OUTPUT (last 10000 chars):
{error_output[-10000:]}

Return a JSON response:
{{
  "summary": "1-2 sentence plain English explanation of what went wrong",
  "root_cause": "Technical root cause explanation",
  "files_involved": ["list", "of", "file", "paths"],
  "suggested_investigation": "What questions to ask or files to look at to fix this"
}}

Return ONLY valid JSON, no markdown."""

        try:
            response = self.client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=2000,
                temperature=0.0,
                messages=[{"role": "user", "content": prompt}]
            )

            response_text = response.content[0].text.strip()

            # Try to parse JSON
            try:
                # Try direct parse
                data = json.loads(response_text)
            except json.JSONDecodeError:
                # Try extracting JSON from response
                start = response_text.find('{')
                end = response_text.rfind('}')
                if start != -1 and end != -1:
                    try:
                        data = json.loads(response_text[start:end + 1])
                    except json.JSONDecodeError:
                        data = None
                else:
                    data = None

            if data:
                analysis = (
                    f"Summary: {data.get('summary', 'Unknown error')}\n\n"
                    f"Root cause: {data.get('root_cause', 'Unknown')}\n\n"
                    f"Files involved: {', '.join(data.get('files_involved', []))}\n\n"
                    f"Suggested investigation: {data.get('suggested_investigation', 'N/A')}"
                )
            else:
                analysis = f"Could not parse error analysis. Raw error:\n{error_output[-3000:]}"

        except Exception as e:
            logger.error(f"Error analysis failed: {e}")
            analysis = f"Error analysis failed. Raw error:\n{error_output[-3000:]}"

        # Send to CEO via Telegram
        attempt_info = ""
        if context.get('attempt'):
            attempt_info = f"Attempt: {context['attempt']}/{context.get('max_attempts', '?')}\n"

        message = (
            f"Build Fix Failed - Need Your Help\n\n"
            f"Strategy #{strategy_id}\n"
            f"Type: {failure_type}\n"
            f"{attempt_info}\n"
            f"{analysis}\n\n"
            f"You can:\n"
            f"- Reply with a question (I'll research the code)\n"
            f"- Give me instructions for fixing it\n"
            f"- Click Try Fix when ready\n"
            f"- Click Give Up to skip"
        )

        self.mobile.send_troubleshooting_prompt(strategy_id, message)

        return analysis

    def _qa_loop(self, strategy_id: int, error_output: str,
                 error_context: str, conversation_history: List[Dict],
                 timeout_minutes: int = 30) -> Dict[str, Any]:
        """
        Main Q&A polling loop. Waits for CEO messages and responds.

        Returns:
            {'action': 'try_fix', 'fixes': [...]} or {'action': 'give_up'}
        """
        start_time = time.time()
        timeout_seconds = timeout_minutes * 60
        last_check = datetime.utcnow().isoformat()

        logger.info(f"Entering Q&A loop for strategy {strategy_id} (timeout: {timeout_minutes}min)")

        while time.time() - start_time < timeout_seconds:
            # Poll for CEO response
            approval = self.db.wait_for_approval(
                strategy_id=strategy_id,
                timeout_minutes=1,  # Short timeout - we loop ourselves
                fast_poll_interval=5,
                fast_poll_duration=60,
                response_types=['qa_response', 'qa_try_fix', 'qa_give_up'],
                after_timestamp=last_check,
                require_new=True
            )

            if not approval:
                # Check total timeout
                if time.time() - start_time >= timeout_seconds:
                    logger.warning(f"Q&A session timed out for strategy {strategy_id}")
                    self.mobile.send_status_update_sync(
                        "Troubleshooting session timed out (30 min). Continuing without fix."
                    )
                    return {'action': 'give_up', 'reason': 'timeout'}
                continue

            response_type = approval.get('response_type')
            user_message = approval.get('user_response', '')
            last_check = approval.get('created_at', datetime.utcnow().isoformat())

            logger.info(f"Q&A response: type={response_type}, message={user_message[:100]}")

            if response_type == 'qa_give_up':
                logger.info(f"CEO chose to give up on strategy {strategy_id}")
                self.mobile.send_status_update_sync("Understood. Skipping this error.")
                return {'action': 'give_up', 'reason': 'ceo_decision'}

            elif response_type == 'qa_try_fix':
                logger.info(f"CEO requested fix attempt for strategy {strategy_id}")
                self.mobile.send_status_update_sync("Generating fix based on our conversation...")

                # Generate fix using conversation context
                fix_result = self._generate_guided_fix(
                    error_output=error_output,
                    conversation_history=conversation_history
                )

                if fix_result.get('success'):
                    self.mobile.send_status_update_sync(
                        f"Fix generated for {len(fix_result.get('fixes', []))} file(s). Retrying build..."
                    )
                    return {
                        'action': 'try_fix',
                        'fixes': fix_result.get('fixes', [])
                    }
                else:
                    self.mobile.send_status_update_sync(
                        f"Failed to generate fix: {fix_result.get('error', 'Unknown')}. "
                        f"You can provide more guidance or give up."
                    )
                    # Continue the loop

            elif response_type == 'qa_response':
                # CEO sent a question or instruction
                conversation_history.append({
                    'role': 'ceo',
                    'content': user_message,
                    'timestamp': datetime.utcnow().isoformat()
                })

                # Research and respond
                answer = self._research_and_respond(
                    question=user_message,
                    error_context=error_context,
                    error_output=error_output,
                    conversation_history=conversation_history
                )

                conversation_history.append({
                    'role': 'assistant',
                    'content': answer,
                    'timestamp': datetime.utcnow().isoformat()
                })

        # Final timeout
        logger.warning(f"Q&A session timed out for strategy {strategy_id}")
        self.mobile.send_status_update_sync(
            "Troubleshooting session timed out. Continuing without fix."
        )
        return {'action': 'give_up', 'reason': 'timeout'}

    def _research_and_respond(self, question: str, error_context: str,
                              error_output: str,
                              conversation_history: List[Dict]) -> str:
        """
        Use LLM to research the codebase and answer CEO's question.

        1. Haiku identifies which files to read
        2. Read files from app directory
        3. Sonnet answers with full context
        """
        app_path = Path(self.gentube_app_path)

        # Step 1: Identify relevant files
        file_id_prompt = f"""The CEO is troubleshooting a build error and asked:
"{question}"

Error context:
{error_context}

Recent conversation:
{self._format_conversation(conversation_history[-6:])}

Which files from the app should I read to answer this question?
Return JSON: {{"files": ["path/to/file1.tsx", "path/to/file2.ts"]}}
Only include files likely to exist. Max 10 files. Return ONLY valid JSON."""

        files_to_read = []
        try:
            response = self.client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=1000,
                temperature=0.0,
                messages=[{"role": "user", "content": file_id_prompt}]
            )
            resp_text = response.content[0].text.strip()
            try:
                data = json.loads(resp_text)
                files_to_read = data.get('files', [])[:10]
            except json.JSONDecodeError:
                start = resp_text.find('{')
                end = resp_text.rfind('}')
                if start != -1 and end != -1:
                    try:
                        data = json.loads(resp_text[start:end + 1])
                        files_to_read = data.get('files', [])[:10]
                    except json.JSONDecodeError:
                        pass
        except Exception as e:
            logger.warning(f"File identification failed: {e}")

        # Step 2: Read files
        file_contents = {}
        for file_path in files_to_read:
            full_path = app_path / file_path
            if full_path.exists():
                try:
                    content = full_path.read_text()
                    if len(content) <= 50000:  # Max 50KB per file
                        file_contents[file_path] = content
                    else:
                        file_contents[file_path] = content[:50000] + "\n... (truncated)"
                except Exception:
                    logger.warning(f"Could not read file: {file_path}")

        # Step 3: Answer with Sonnet
        files_section = ""
        if file_contents:
            files_section = "RELEVANT FILES:\n\n"
            for fp, content in file_contents.items():
                files_section += f"--- {fp} ---\n{content}\n\n"

        answer_prompt = f"""You are helping a CEO troubleshoot a build error. Answer their question clearly and concisely.

BUILD ERROR:
{error_output[-5000:]}

ERROR ANALYSIS:
{error_context}

CONVERSATION SO FAR:
{self._format_conversation(conversation_history)}

{files_section}

CEO'S QUESTION/INSTRUCTION:
{question}

Provide a clear, concise answer. If the CEO gave instructions, confirm what you'll do.
If you found the root cause, explain it and suggest a fix.
Keep your response under 500 words - this will be sent via Telegram."""

        try:
            response = self.client.messages.create(
                model="claude-sonnet-4-5-20250929",
                max_tokens=2000,
                temperature=0.2,
                messages=[{"role": "user", "content": answer_prompt}]
            )
            answer = response.content[0].text.strip()
        except Exception as e:
            logger.error(f"Failed to generate answer: {e}")
            answer = f"Sorry, I encountered an error researching your question: {e}"

        # Send answer to CEO
        self.mobile.send_status_update_sync(answer)
        logger.info(f"Sent Q&A answer ({len(answer)} chars)")

        return answer

    def _generate_guided_fix(self, error_output: str,
                             conversation_history: List[Dict]) -> Dict[str, Any]:
        """
        Generate fix based on accumulated Q&A conversation context.
        Similar to _fix_build_error but with CEO guidance.
        """
        app_path = Path(self.gentube_app_path)

        # Step 1: Use Sonnet to determine what files need fixing and how,
        # based on the full conversation context
        plan_prompt = f"""Based on this troubleshooting conversation, generate a fix plan.

BUILD ERROR:
{error_output[-8000:]}

TROUBLESHOOTING CONVERSATION:
{self._format_conversation(conversation_history)}

Return JSON with the files to fix:
{{
  "files_to_fix": [
    {{
      "file_path": "relative/path/to/file.tsx",
      "fix_description": "What needs to change"
    }}
  ]
}}

Use the CEO's guidance from the conversation to determine the correct fix approach.
Return ONLY valid JSON."""

        try:
            response = self.client.messages.create(
                model="claude-sonnet-4-5-20250929",
                max_tokens=2000,
                temperature=0.0,
                messages=[{"role": "user", "content": plan_prompt}]
            )
            resp_text = response.content[0].text.strip()

            # Parse JSON
            data = None
            try:
                data = json.loads(resp_text)
            except json.JSONDecodeError:
                start = resp_text.find('{')
                end = resp_text.rfind('}')
                if start != -1 and end != -1:
                    try:
                        data = json.loads(resp_text[start:end + 1])
                    except json.JSONDecodeError:
                        pass

            if not data or not data.get('files_to_fix'):
                return {'success': False, 'error': 'Could not determine fix plan'}

            files_to_fix = data['files_to_fix']
            logger.info(f"Guided fix plan: {len(files_to_fix)} file(s)")

        except Exception as e:
            logger.error(f"Fix plan generation failed: {e}")
            return {'success': False, 'error': str(e)}

        # Step 2: Read current file contents
        file_contents = {}
        for file_info in files_to_fix:
            file_path = file_info.get('file_path', '')
            full_path = app_path / file_path
            if full_path.exists():
                try:
                    file_contents[file_path] = full_path.read_text()
                except Exception:
                    logger.warning(f"Could not read file: {file_path}")

        if not file_contents:
            return {
                'success': False,
                'error': f'None of the identified files exist: {[f["file_path"] for f in files_to_fix]}'
            }

        # Step 3: Generate fixes for each file
        fixes = []
        conversation_context = self._format_conversation(conversation_history)

        for file_info in files_to_fix:
            file_path = file_info.get('file_path', '')
            fix_description = file_info.get('fix_description', '')

            if file_path not in file_contents:
                continue

            fix_prompt = f"""Fix this file based on the CEO's guidance from our troubleshooting conversation.

FILE: {file_path}

FIX NEEDED: {fix_description}

CEO'S GUIDANCE (from troubleshooting conversation):
{conversation_context}

BUILD ERROR CONTEXT:
{error_output[-4000:]}

CURRENT FILE CONTENT:
{file_contents[file_path]}

Output ONLY the complete fixed file content - no JSON, no markdown code blocks.
Start your response with the first line of code.

Requirements:
1. Output the COMPLETE file
2. Follow the CEO's instructions precisely
3. Fix the build error while preserving existing functionality"""

            try:
                response = self.client.messages.create(
                    model="claude-sonnet-4-5-20250929",
                    max_tokens=8000,
                    temperature=0.2,
                    messages=[{"role": "user", "content": fix_prompt}]
                )

                content = response.content[0].text.strip()
                code = self._strip_markdown_blocks(content)

                fixes.append({
                    'file_path': file_path,
                    'new_content': code
                })
                logger.info(f"Generated guided fix for: {file_path}")

            except Exception as e:
                logger.error(f"Error generating guided fix for {file_path}: {e}")

        if fixes:
            return {'success': True, 'fixes': fixes}
        else:
            return {'success': False, 'error': 'Failed to generate any fixes'}

    def _format_conversation(self, history: List[Dict]) -> str:
        """Format conversation history for LLM prompts."""
        lines = []
        for entry in history:
            role = entry.get('role', 'unknown')
            content = entry.get('content', '')
            if role == 'system':
                lines.append(f"[Error Analysis]: {content}")
            elif role == 'ceo':
                lines.append(f"[CEO]: {content}")
            elif role == 'assistant':
                lines.append(f"[Assistant]: {content}")
        return "\n\n".join(lines)

    def _strip_markdown_blocks(self, content: str) -> str:
        """Strip markdown code blocks from LLM output."""
        content = content.strip()
        if content.startswith('```'):
            first_newline = content.find('\n')
            if first_newline != -1:
                content = content[first_newline + 1:]
        if content.endswith('```'):
            content = content[:-3].rstrip()
        return content


# Singleton instance
_troubleshooter = None


def get_troubleshooter() -> TroubleshooterAgent:
    """Get or create troubleshooter singleton"""
    global _troubleshooter
    if _troubleshooter is None:
        _troubleshooter = TroubleshooterAgent()
    return _troubleshooter
