"""
AI Strategy Engine
Generates optimization strategies using Claude (Anthropic API) or Ollama (local LLM)
"""

import os
import json
import logging
import subprocess
import fnmatch
from pathlib import Path
from typing import Dict, List, Optional, Any
import requests
from anthropic import Anthropic
from dotenv import load_dotenv

# Load environment variables
load_dotenv('config.env')

# Configure logging
logging.basicConfig(
    level=os.getenv('LOG_LEVEL', 'INFO'),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class StrategyEngine:
    """AI-powered strategy generation using Claude"""

    # Default model tiers for different task complexities
    # Haiku: Fast, cheap - for simple tasks like trend analysis
    # Sonnet: Balanced - for most strategy work
    # Opus: Most capable - for complex planning or fallback when others fail
    DEFAULT_MODELS = {
        'haiku': 'claude-haiku-4-5-20251001',
        'sonnet': 'claude-sonnet-4-5-20250929',
        'opus': 'claude-opus-4-5-20251101',
    }

    # Default task-to-tier mapping
    DEFAULT_MODEL_TIERS = {
        'strategy_generation': 'sonnet',   # Main strategy work
        'strategy_refinement': 'sonnet',   # Refining based on feedback
        'code_generation': 'sonnet',       # Generating actual code changes
        'code_planning': 'sonnet',         # Pre-generation planning step
        'evaluation': 'haiku',             # Evaluating results (simpler task)
        'trend_analysis': 'haiku',         # Trend analysis (simpler task)
        'dependency_analysis': 'haiku',    # Dependency analysis between changes
        'conversation': 'haiku',             # Conversational chat with CEO
    }

    def __init__(self):
        # ── Available providers from env (comma-separated); first is default ──
        providers_str = os.getenv('LLM_PROVIDERS', os.getenv('LLM_PROVIDER', 'anthropic'))
        self.available_providers = [p.strip().lower() for p in providers_str.split(',') if p.strip()]
        self.llm_provider = self.available_providers[0] if self.available_providers else 'anthropic'

        # Always init Ollama config (lightweight — no connection needed)
        self.ollama_base_url = os.getenv('OLLAMA_BASE_URL', 'http://localhost:11434')
        self.ollama_api_key = os.getenv('OLLAMA_API_KEY', '')
        self.ollama_model = os.getenv('OLLAMA_MODEL', 'openhermes2.5-mistral')

        # Init Anthropic client if API key is available
        self.client = None
        api_key = os.getenv('ANTHROPIC_API_KEY')
        if api_key:
            self.client = Anthropic(api_key=api_key)

        if self.llm_provider == 'ollama':
            logger.info(f"LLM: Ollama (model: {self.ollama_model}, url: {self.ollama_base_url})")
        else:
            if not self.client:
                raise ValueError("ANTHROPIC_API_KEY must be set in config.env when using anthropic provider")

        # Load model configuration from env (with defaults)
        self.models = {
            'haiku': os.getenv('CLAUDE_MODEL_HAIKU', self.DEFAULT_MODELS['haiku']),
            'sonnet': os.getenv('CLAUDE_MODEL_SONNET', self.DEFAULT_MODELS['sonnet']),
            'opus': os.getenv('CLAUDE_MODEL_OPUS', self.DEFAULT_MODELS['opus']),
        }

        # Load task-to-tier mapping from env (with defaults)
        self.model_tiers = {}
        for task_type, default_tier in self.DEFAULT_MODEL_TIERS.items():
            env_key = f'CLAUDE_TIER_{task_type.upper()}'
            self.model_tiers[task_type] = os.getenv(env_key, default_tier)

        # Optional custom system prompt identity injected once at init
        self.agent_system_prompt = os.getenv('AGENT_SYSTEM_PROMPT', '')

        # Company name used throughout prompts
        self.company_name = os.getenv('COMPANY_NAME', 'Gentube.ai')

        # Ollama context window size
        self.ollama_num_ctx = int(os.getenv('OLLAMA_NUM_CTX', '8192'))

        # Chat history limit (number of exchanges, so messages = limit * 2)
        self.chat_history_limit = int(os.getenv('CHAT_HISTORY_LIMIT', '10'))

        # Gentube app path for code search/read tools
        app_path = os.getenv('GENTUBE_APP_PATH', '')
        self.app_root = Path(app_path).resolve() if app_path else None

        logger.info(f"Available LLM providers: {self.available_providers}, active: {self.llm_provider}")
        if self.llm_provider != 'ollama':
            logger.info(f"Agent LLM: Anthropic (tiered models: {self.model_tiers})")

    def set_llm_provider(self, provider: str):
        """Switch the active LLM provider at runtime"""
        provider = provider.strip().lower()
        if provider not in self.available_providers:
            raise ValueError(f"Provider '{provider}' not in available providers: {self.available_providers}")
        if provider != 'ollama' and not self.client:
            raise ValueError("Cannot switch to anthropic — ANTHROPIC_API_KEY not configured")
        self.llm_provider = provider
        logger.info(f"LLM provider switched to: {provider}")

    def _get_model(self, task_type: str) -> str:
        """Get the appropriate model for a task type"""
        tier = self.model_tiers.get(task_type, 'sonnet')
        return self.models[tier]

    def _call_ollama(self, prompt: str, system: str = None, temperature: float = 0.7) -> str:
        """
        Call the local Ollama API with stream disabled to collect the full response.

        Args:
            prompt: The prompt to send
            system: Optional system prompt
            temperature: Sampling temperature

        Returns:
            Full response text from Ollama
        """
        url = f"{self.ollama_base_url}/api/generate"
        payload = {
            "model": self.ollama_model,
            "prompt": prompt,
            "stream": False,
            "options": {
                "temperature": temperature,
                "num_ctx": self.ollama_num_ctx,
            },
        }
        if system:
            payload["system"] = system

        headers = {}
        if self.ollama_api_key:
            headers["Authorization"] = f"Bearer {self.ollama_api_key}"
        logger.info(f"Calling Ollama ({self.ollama_model}) at {self.ollama_base_url}")
        resp = requests.post(url, json=payload, headers=headers, timeout=300)
        resp.raise_for_status()
        result = resp.json()
        logger.info(f"Successfully got response from Ollama ({self.ollama_model})")
        return result.get("response", "")

    def _call_with_fallback(self, prompt: str, task_type: str,
                            max_tokens: int = 2000,
                            temperature: float = 0.7,
                            system: str = None) -> str:
        """
        Call the configured LLM provider with automatic escalation on failure.

        For Anthropic: tries the configured model first, escalates haiku -> sonnet -> opus.
        For Ollama: calls the single configured model.

        Args:
            prompt: The prompt to send
            task_type: Type of task (for model selection)
            max_tokens: Max tokens for response
            temperature: Sampling temperature
            system: Optional system prompt

        Returns:
            Raw response text from the LLM
        """
        if self.llm_provider == 'ollama':
            return self._call_ollama(prompt, system=system, temperature=temperature)

        tier = self.model_tiers.get(task_type, 'sonnet')
        tiers_to_try = []

        # Build escalation path based on starting tier
        if tier == 'haiku':
            tiers_to_try = ['haiku', 'sonnet', 'opus']
        elif tier == 'sonnet':
            tiers_to_try = ['sonnet', 'opus']
        else:
            tiers_to_try = ['opus']

        last_error = None
        for try_tier in tiers_to_try:
            model = self.models[try_tier]
            try:
                logger.info(f"Calling Claude ({try_tier}) for {task_type}")
                messages = [{"role": "user", "content": prompt}]
                collected = ""

                for continuation in range(5):
                    api_kwargs = dict(
                        model=model,
                        max_tokens=max_tokens,
                        temperature=temperature,
                        messages=messages
                    )
                    if system:
                        api_kwargs['system'] = system
                    response = self.client.messages.create(**api_kwargs)
                    chunk = response.content[0].text
                    collected += chunk

                    if response.stop_reason != 'max_tokens':
                        break

                    logger.info(f"Response truncated, requesting continuation ({continuation + 1}/5)...")
                    # Append assistant's partial response and ask to continue
                    messages.append({"role": "assistant", "content": chunk})
                    messages.append({"role": "user", "content": "Continue exactly where you left off. Do not repeat any text."})

                if response.stop_reason == 'max_tokens':
                    logger.warning(f"Response still incomplete after 5 continuations for {task_type}")

                logger.info(f"Successfully got response from {try_tier}")
                return collected

            except Exception as e:
                last_error = e
                logger.warning(f"{try_tier} failed for {task_type}: {e}")
                if try_tier != tiers_to_try[-1]:
                    logger.info(f"Escalating to next model tier...")
                continue

        # All tiers failed
        raise last_error

    # ── Tool / function-calling definitions for chat() ──────────────────

    CHAT_TOOLS_ANTHROPIC = [
        {
            "name": "get_latest_metrics",
            "description": "Get the most recent metric snapshots from the database. Returns revenue, usage, performance, and analytics data.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "Number of recent snapshots to return (default 5)", "default": 5}
                },
                "required": []
            }
        },
        {
            "name": "get_metrics_for_strategy",
            "description": "Get all metric snapshots associated with a specific strategy (before and after deployment).",
            "input_schema": {
                "type": "object",
                "properties": {
                    "strategy_id": {"type": "integer", "description": "The strategy ID to look up"}
                },
                "required": ["strategy_id"]
            }
        },
        {
            "name": "get_recent_feedback",
            "description": "Get recent user feedback entries with sentiment, rating, and category.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "days": {"type": "integer", "description": "Number of days to look back (default 7)", "default": 7},
                    "limit": {"type": "integer", "description": "Max entries to return (default 50)", "default": 50}
                },
                "required": []
            }
        },
        {
            "name": "get_strategy",
            "description": "Look up a specific strategy by its ID. Returns full strategy details including summary, changes, status, and impact.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "strategy_id": {"type": "integer", "description": "The strategy ID"}
                },
                "required": ["strategy_id"]
            }
        },
        {
            "name": "get_pending_strategies",
            "description": "List all strategies currently awaiting CEO approval.",
            "input_schema": {"type": "object", "properties": {}, "required": []}
        },
        {
            "name": "get_approved_strategies",
            "description": "List all approved strategies ready for execution.",
            "input_schema": {"type": "object", "properties": {}, "required": []}
        },
        {
            "name": "get_saved_strategies",
            "description": "List strategies that were saved for later consideration.",
            "input_schema": {"type": "object", "properties": {}, "required": []}
        },
        {
            "name": "get_rejected_strategies",
            "description": "List strategies that were rejected by the CEO.",
            "input_schema": {"type": "object", "properties": {}, "required": []}
        },
        {
            "name": "get_failed_strategies",
            "description": "List strategies that failed during execution.",
            "input_schema": {"type": "object", "properties": {}, "required": []}
        },
        {
            "name": "get_stuck_strategies",
            "description": "List strategies stuck in intermediate execution states that may need attention.",
            "input_schema": {"type": "object", "properties": {}, "required": []}
        },
        {
            "name": "get_unacknowledged_alerts",
            "description": "Get all active (unacknowledged) system alerts.",
            "input_schema": {"type": "object", "properties": {}, "required": []}
        },
        {
            "name": "collect_fresh_metrics",
            "description": "Trigger a live data collection from all sources (Stripe, GA4, CrUX, Clarity, Datastore, Supabase). Use when the CEO wants up-to-the-minute numbers.",
            "input_schema": {"type": "object", "properties": {}, "required": []}
        },
        {
            "name": "search_code",
            "description": "Search for files in the app codebase by filename pattern or by text content. Returns a list of matching file paths. MD files are listed first.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Text to search for (filename pattern or content text)"},
                    "search_type": {"type": "string", "enum": ["filename", "content"], "description": "Search by filename or by file content"},
                    "file_type": {"type": "string", "description": "Optional file extension filter (e.g. 'tsx', 'md', 'json'). Searches all supported types if omitted."}
                },
                "required": ["query", "search_type"]
            }
        },
        {
            "name": "read_file",
            "description": "Read the contents of a file from the app codebase. Use after search_code to examine specific files.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "Relative file path within the app (as returned by search_code)"}
                },
                "required": ["file_path"]
            }
        },
    ]

    # Ollama uses OpenAI-compatible tool format
    CHAT_TOOLS_OLLAMA = [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t["description"],
                "parameters": t["input_schema"],
            },
        }
        for t in CHAT_TOOLS_ANTHROPIC
    ]

    def _execute_tool(self, name: str, args: Dict, db, collector) -> str:
        """Execute a tool call and return the result as a JSON string."""
        try:
            if name == "get_latest_metrics":
                result = db.get_latest_metrics(limit=args.get("limit", 5))
            elif name == "get_metrics_for_strategy":
                result = db.get_metrics_for_strategy(args["strategy_id"])
            elif name == "get_recent_feedback":
                result = db.get_recent_feedback(days=args.get("days", 7), limit=args.get("limit", 50))
            elif name == "get_strategy":
                result = db.get_strategy(args["strategy_id"])
            elif name == "get_pending_strategies":
                result = db.get_pending_strategies()
            elif name == "get_approved_strategies":
                result = db.get_approved_strategies()
            elif name == "get_saved_strategies":
                result = db.get_saved_strategies()
            elif name == "get_rejected_strategies":
                result = db.get_rejected_strategies()
            elif name == "get_failed_strategies":
                result = db.get_failed_strategies()
            elif name == "get_stuck_strategies":
                result = db.get_stuck_strategies()
            elif name == "get_unacknowledged_alerts":
                result = db.get_unacknowledged_alerts()
            elif name == "collect_fresh_metrics":
                result = collector.collect_metrics(wait_hours=0)
            elif name == "search_code":
                result = self._tool_search_code(args)
            elif name == "read_file":
                result = self._tool_read_file(args)
            else:
                result = {"error": f"Unknown tool: {name}"}
            return json.dumps(result, default=str)
        except Exception as e:
            logger.error(f"Tool execution error ({name}): {e}")
            return json.dumps({"error": str(e)})

    SKIP_DIRS = {'node_modules', '.next', '.git', 'dist', '.vercel', '__pycache__', '.turbo'}
    SUPPORTED_EXTENSIONS = {'.ts', '.tsx', '.js', '.jsx', '.json', '.css', '.md', '.mdx',
                            '.html', '.yaml', '.yml', '.env', '.sql', '.py', '.txt', '.svg'}

    def _tool_search_code(self, args: Dict) -> Dict:
        """Search for files in the Gentube app codebase."""
        if not self.app_root or not self.app_root.exists():
            return {"error": "GENTUBE_APP_PATH is not configured or does not exist"}

        query = args.get("query", "")
        search_type = args.get("search_type", "filename")
        file_type = args.get("file_type")  # e.g. "tsx", "md"

        if not query:
            return {"error": "query is required"}

        matches = []

        if search_type == "filename":
            for root, dirs, files in os.walk(self.app_root):
                dirs[:] = [d for d in dirs if d not in self.SKIP_DIRS]
                for f in files:
                    if file_type and not f.endswith(f'.{file_type}'):
                        continue
                    if not file_type and Path(f).suffix not in self.SUPPORTED_EXTENSIONS:
                        continue
                    if fnmatch.fnmatch(f.lower(), f'*{query.lower()}*'):
                        rel = os.path.relpath(os.path.join(root, f), self.app_root)
                        matches.append(rel)
        else:  # content search
            include_ext = f'*.{file_type}' if file_type else None
            grep_args = ['grep', '-rl', '--max-count=1']
            if include_ext:
                grep_args += [f'--include={include_ext}']
            else:
                for ext in self.SUPPORTED_EXTENSIONS:
                    grep_args += [f'--include=*{ext}']
            for skip in self.SKIP_DIRS:
                grep_args += [f'--exclude-dir={skip}']
            grep_args += [query, str(self.app_root)]

            try:
                result = subprocess.run(grep_args, capture_output=True, text=True, timeout=10)
                for line in result.stdout.strip().split('\n'):
                    if line:
                        rel = os.path.relpath(line.strip(), self.app_root)
                        matches.append(rel)
            except subprocess.TimeoutExpired:
                return {"error": "Search timed out"}

        # Sort: .md files first, then alphabetical
        matches.sort(key=lambda p: (0 if p.endswith('.md') or p.endswith('.mdx') else 1, p))
        matches = matches[:20]

        return {"files": matches, "count": len(matches), "query": query, "search_type": search_type}

    def _tool_read_file(self, args: Dict) -> Dict:
        """Read a file from the Gentube app codebase with path traversal protection."""
        if not self.app_root or not self.app_root.exists():
            return {"error": "GENTUBE_APP_PATH is not configured or does not exist"}

        file_path = args.get("file_path", "")
        if not file_path:
            return {"error": "file_path is required"}

        resolved = (self.app_root / file_path).resolve()

        # Security: ensure resolved path is within app root
        if not str(resolved).startswith(str(self.app_root)):
            return {"error": "Access denied — path is outside the app directory"}

        if not resolved.is_file():
            return {"error": f"File not found: {file_path}"}

        try:
            content = resolved.read_text(encoding='utf-8', errors='replace')
            truncated = len(content) > 3000
            if truncated:
                content = content[:3000] + "\n... (truncated)"
            return {"file_path": file_path, "content": content, "truncated": truncated}
        except Exception as e:
            return {"error": f"Could not read file: {e}"}

    def _chat_with_tools_anthropic(self, messages: List[Dict], system: str,
                                    db, collector) -> str:
        """Run the Anthropic tool-calling loop."""
        model = self.models[self.model_tiers.get('conversation', 'haiku')]
        for _ in range(10):
            response = self.client.messages.create(
                model=model,
                max_tokens=1024,
                temperature=0.7,
                system=system,
                messages=messages,
                tools=self.CHAT_TOOLS_ANTHROPIC,
            )

            # Collect text and tool_use blocks
            text_parts = []
            tool_calls = []
            for block in response.content:
                if block.type == "text":
                    text_parts.append(block.text)
                elif block.type == "tool_use":
                    tool_calls.append(block)

            if not tool_calls:
                return "\n".join(text_parts).strip()

            # Append assistant response then tool results
            messages.append({"role": "assistant", "content": response.content})
            for tc in tool_calls:
                logger.info(f"Tool call: {tc.name}({tc.input})")
                result = self._execute_tool(tc.name, tc.input, db, collector)
                messages.append({
                    "role": "user",
                    "content": [{"type": "tool_result", "tool_use_id": tc.id, "content": result}],
                })

        # Exhausted rounds — return whatever text we have
        return "\n".join(text_parts).strip() if text_parts else "I ran out of processing steps. Please try a simpler question."

    def _chat_with_tools_ollama(self, messages: List[Dict], db, collector) -> str:
        """Run the Ollama /api/chat tool-calling loop.
        Falls back to plain chat (no tools) if the model doesn't support tool calling."""
        url = f"{self.ollama_base_url}/api/chat"
        headers = {}
        if self.ollama_api_key:
            headers["Authorization"] = f"Bearer {self.ollama_api_key}"
        use_tools = True

        for _ in range(10):
            payload = {
                "model": self.ollama_model,
                "messages": messages,
                "stream": False,
                "options": {"num_ctx": self.ollama_num_ctx},
            }
            if use_tools:
                payload["tools"] = self.CHAT_TOOLS_OLLAMA

            resp = requests.post(url, json=payload, headers=headers, timeout=300)
            if resp.status_code == 400 and use_tools:
                logger.warning(f"Ollama tool calling failed (400), falling back to plain chat: {resp.text}")
                use_tools = False
                payload.pop("tools", None)
                resp = requests.post(url, json=payload, headers=headers, timeout=300)
            resp.raise_for_status()
            data = resp.json()

            msg = data.get("message", {})
            tool_calls = msg.get("tool_calls")

            if not tool_calls:
                return msg.get("content", "").strip()

            # Append assistant message with tool calls
            messages.append(msg)

            # Execute each tool and feed results back
            for tc in tool_calls:
                fn = tc.get("function", {})
                fn_name = fn.get("name", "")
                fn_args = fn.get("arguments", {})
                logger.info(f"Ollama tool call: {fn_name}({fn_args})")
                result = self._execute_tool(fn_name, fn_args, db, collector)
                messages.append({"role": "tool", "content": result})

        return msg.get("content", "").strip() if msg else "I ran out of processing steps. Please try a simpler question."

    def _try_repair_json(self, content: str) -> Optional[Dict]:
        """
        Attempt to repair truncated or malformed JSON from Claude.

        Common issues:
        - Truncated response (unclosed strings, arrays, objects)
        - Extra trailing content after valid JSON

        Returns:
            Parsed dict if repair successful, None otherwise
        """
        import re

        # Try to find valid JSON by progressively trimming
        for trim_amount in range(0, min(500, len(content) // 2), 10):
            trimmed = content[:len(content) - trim_amount] if trim_amount > 0 else content

            # Try adding closing brackets/braces
            for suffix in ['', '"', '"}', '"}]', '"}]}', '"]', '"]}', '}', ']}', ']']:
                attempt = trimmed + suffix
                try:
                    result = json.loads(attempt)
                    if isinstance(result, dict) and 'code_changes' in result:
                        return result
                except json.JSONDecodeError:
                    continue

        # Try to extract just the code_changes array using regex
        match = re.search(r'"code_changes"\s*:\s*\[', content)
        if match:
            # Find the start of the array
            start = match.end() - 1  # Include the [
            bracket_count = 0
            end = start

            for i, char in enumerate(content[start:]):
                if char == '[':
                    bracket_count += 1
                elif char == ']':
                    bracket_count -= 1
                    if bracket_count == 0:
                        end = start + i + 1
                        break

            if end > start:
                try:
                    array_content = content[start:end]
                    code_changes = json.loads(array_content)
                    return {
                        'code_changes': code_changes,
                        'summary': 'Extracted from partial response',
                        'test_commands': []
                    }
                except json.JSONDecodeError:
                    pass

        return None

    def generate_strategy(self, data: Dict[str, Any], ceo_context: str = None) -> Dict[str, Any]:
        """
        Generate an optimization strategy based on current data

        Args:
            data: Dictionary containing:
                - metrics: Latest performance metrics
                - feedback: Recent user feedback
                - goals: Business goals (e.g., target MRR, ARPU)
                - current_features: Current app features
                - constraints: Constraints (e.g., budget, risk tolerance)
            ceo_context: Optional message from CEO chat to use as direction for the strategy

        Returns:
            Strategy dictionary with summary, changes, expected_impact, priority
        """
        logger.info("Generating strategy...")

        try:
            # Prepare context for Claude
            context = self._prepare_context(data, ceo_context=ceo_context)

            # Generate strategy using Claude
            strategy = self._call_claude(context)

            logger.info(f"Strategy generated: {strategy['summary'][:100]}...")
            return strategy

        except Exception as e:
            logger.error(f"Error generating strategy: {e}")
            raise

    def refine_strategy(self, original_strategy: Dict[str, Any],
                        user_feedback: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Refine a strategy based on user feedback

        Args:
            original_strategy: The original strategy that was rejected/tweaked
            user_feedback: User's feedback/direction (e.g., "focus on retention instead")
            data: Original data context (metrics, feedback, goals, etc.)

        Returns:
            Refined strategy dictionary
        """
        logger.info(f"Refining strategy based on feedback: {user_feedback[:50]}...")

        try:
            # Prepare base context
            base_context = self._prepare_context(data)

            # Add refinement instructions
            refinement_prompt = f"""{base_context}

## Previous Strategy (Needs Refinement)
Summary: {original_strategy.get('summary', 'N/A')}
Focus Area: {original_strategy.get('focus_area', 'N/A')}
Expected Impact: {original_strategy.get('expected_impact', 'N/A')}

Previous Changes Proposed:
{json.dumps(original_strategy.get('changes', []), indent=2)}

## User Feedback
The user reviewed the above strategy and provided this feedback:
"{user_feedback}"

## Your Task
Based on the user's feedback, generate a REVISED strategy that addresses their concerns.
- Incorporate their specific direction
- Keep what works from the original if applicable
- Ensure the new strategy aligns with the user's intent

Respond ONLY with valid JSON in the same format as before (including the dependency_analysis section)."""

            # Call Claude for refined strategy (with tiered fallback)
            content = self._call_with_fallback(
                prompt=refinement_prompt,
                task_type='strategy_refinement',
                max_tokens=4096,
                temperature=0.7
            )

            # Strip markdown code blocks if present
            content = content.strip()
            if content.startswith('```json'):
                content = content[7:]
            elif content.startswith('```'):
                content = content[3:]
            if content.endswith('```'):
                content = content[:-3]
            content = content.strip()

            # Parse JSON response
            strategy = json.loads(content)

            # Validate required fields
            required_fields = ['summary', 'expected_impact', 'focus_area', 'priority', 'changes']
            for field in required_fields:
                if field not in strategy:
                    raise ValueError(f"Missing required field: {field}")

            # Mark as refined
            strategy['refined_from'] = original_strategy.get('id')
            strategy['user_feedback'] = user_feedback

            logger.info(f"Refined strategy generated: {strategy['summary'][:100]}...")
            return strategy

        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse Claude response as JSON: {e}")
            raise ValueError("Claude did not return valid JSON")

        except Exception as e:
            logger.error(f"Error refining strategy: {e}")
            raise

    def _build_business_context(self, db) -> str:
        """Build a compact business snapshot from DB data for the system prompt.
        Only includes fields that have real values — never N/A or None."""
        parts = []
        try:
            metrics_list = db.get_latest_metrics(limit=1)
            if metrics_list:
                kpi = metrics_list[0].get('kpi_data', {})
                stripe = kpi.get('stripe', {})
                app = kpi.get('app_usage', {})
                revenue_items = []
                if stripe.get('mrr') is not None:
                    revenue_items.append(f"MRR: ${stripe['mrr']}")
                if stripe.get('revenue_30d') is not None:
                    revenue_items.append(f"Revenue (30d): ${stripe['revenue_30d']}")
                if stripe.get('active_customers') is not None:
                    revenue_items.append(f"Active customers: {stripe['active_customers']}")
                if revenue_items:
                    parts.append(" | ".join(revenue_items))

                usage_items = []
                if app.get('dau') is not None:
                    usage_items.append(f"DAU: {app['dau']}")
                if app.get('mau') is not None:
                    usage_items.append(f"MAU: {app['mau']}")
                if metrics_list[0].get('churn_rate') is not None:
                    usage_items.append(f"Churn: {metrics_list[0]['churn_rate']}%")
                if usage_items:
                    parts.append(" | ".join(usage_items))

                clarity = kpi.get('clarity', {})
                if clarity and 'error' not in clarity:
                    ux_items = []
                    if clarity.get('rage_clicks') is not None:
                        ux_items.append(f"Rage clicks: {clarity['rage_clicks']}")
                    if clarity.get('dead_clicks') is not None:
                        ux_items.append(f"Dead clicks: {clarity['dead_clicks']}")
                    if clarity.get('quick_backs') is not None:
                        ux_items.append(f"Quick backs: {clarity['quick_backs']}")
                    if ux_items:
                        parts.append("Clarity UX: " + " | ".join(ux_items))
        except Exception as e:
            logger.debug(f"Could not load metrics for context: {e}")

        try:
            pending = db.get_pending_strategies()
            if pending:
                s = pending[0]
                parts.append(f"Active strategy: #{s['id']} - {s['summary'][:80]} ({s['status']})")
        except Exception as e:
            logger.debug(f"Could not load strategies for context: {e}")

        try:
            feedback = db.get_recent_feedback(days=7, limit=50)
            if feedback:
                positive = sum(1 for f in feedback if f.get('sentiment') == 'positive')
                pct = round(positive / len(feedback) * 100) if feedback else 0
                parts.append(f"Recent feedback: {len(feedback)} entries, {pct}% positive")
        except Exception as e:
            logger.debug(f"Could not load feedback for context: {e}")

        try:
            alerts = db.get_unacknowledged_alerts()
            if alerts:
                parts.append(f"Unacknowledged alerts: {len(alerts)}")
        except Exception as e:
            logger.debug(f"Could not load alerts for context: {e}")

        if parts:
            return ("\n\nCurrent business snapshot (from database — only reference data shown here, "
                    "never invent or guess numbers):\n- " + "\n- ".join(parts))
        return ""

    def chat(self, message: str, context: str = "",
             db=None, collector=None, chat_history: List[Dict] = None) -> str:
        """
        Handle a conversational message from the CEO.

        Args:
            message: The user's message
            context: Optional context about recent strategies/data
            db: DatabaseHandler for tool calls and business context
            collector: DataCollector for tool calls
            chat_history: List of prior messages [{role, content}, ...]

        Returns:
            The LLM's response string
        """
        system_prompt = (self.agent_system_prompt + "\n\n") if self.agent_system_prompt else ""
        system_prompt += (
            f"You are the {self.company_name} Optimization Bot assistant. You help the CEO understand "
            "optimization strategies, metrics, data trends, and how the optimization machine works. "
            "You can explain strategies, suggest approaches, and answer questions about the system. "
            "Keep responses concise and conversational — this is a Telegram chat. "
            "Use markdown formatting sparingly (bold for emphasis only). "
            "Only cite data that is explicitly provided in the context below — never make up or guess numbers."
        )

        # Inject business context from DB
        if db:
            system_prompt += self._build_business_context(db)

        prompt = message
        if context:
            prompt = f"Context:\n{context}\n\nUser message: {message}"

        try:
            if db and collector:
                # Build messages with history
                if self.llm_provider == 'ollama':
                    messages = [{"role": "system", "content": system_prompt}]
                else:
                    messages = []

                # Append chat history
                if chat_history:
                    for msg in chat_history:
                        messages.append({"role": msg["role"], "content": msg["content"]})

                # Append current message
                messages.append({"role": "user", "content": prompt})

                if self.llm_provider == 'ollama':
                    return self._chat_with_tools_ollama(messages, db, collector)
                else:
                    return self._chat_with_tools_anthropic(messages, system_prompt, db, collector)

            # Fallback if no db/collector provided — plain LLM call
            response = self._call_with_fallback(
                prompt=prompt,
                task_type='conversation',
                max_tokens=1024,
                temperature=0.7,
                system=system_prompt
            )
            return response.strip()
        except Exception as e:
            logger.error(f"Chat error: {e}")
            return f"Sorry, I encountered an error: {e}"

    def generate_code_plan(self, strategy: Dict[str, Any],
                           changes: List[Dict]) -> Dict[str, Any]:
        """
        Generate a detailed implementation plan as a Markdown document.

        The LLM outputs the plan directly as markdown, ready to be saved
        as an .md file and loaded into an LLM later for code generation.

        Args:
            strategy: The approved strategy
            changes: List of changes to implement

        Returns:
            Dictionary with success status and markdown content string
        """
        logger.info("Generating code plan before code generation...")

        strategy_id = strategy.get('id', '?')
        changes_summary = json.dumps(changes, indent=2)[:3000]

        prompt = f"""You are planning the implementation of code changes for {self.company_name} (Next.js/React/TypeScript).

## Strategy #{strategy_id}
{strategy.get('summary', 'N/A')}

## Changes to Implement
{changes_summary}

## Your Task
Write a comprehensive implementation plan as a **Markdown document** that a human or LLM can follow to generate the code. The document should be clear, detailed, and self-contained.

Structure the document with these sections:

1. **Overview** — Brief summary of the implementation approach
2. **Files** — For each file to create or modify, include:
   - File path and whether it's new or a modification
   - Purpose of the file
   - Key implementation details (functions, components, APIs, logic)
   - Dependencies (imports, libraries, other files)
   - Integration points (how it connects to existing code)
   - Risk areas (edge cases, potential issues)
3. **Execution Order** — Numbered list of files in dependency order
4. **Testing Approach** — What to verify after implementation
5. **Rollback Notes** — What to watch for, how to revert

Output ONLY the raw Markdown — no JSON, no code fences wrapping the whole document. Start with a top-level heading."""

        try:
            content = self._call_with_fallback(
                prompt=prompt,
                task_type='code_planning',
                max_tokens=4096,
                temperature=0.3
            )

            # The LLM should return raw markdown, but strip any accidental wrapping
            content = content.strip()
            if content.startswith('```markdown'):
                content = content[len('```markdown'):].strip()
            elif content.startswith('```md'):
                content = content[len('```md'):].strip()
            elif content.startswith('```') and not content.startswith('```\n#'):
                content = content[3:].strip()
            if content.endswith('```'):
                content = content[:-3].strip()

            logger.info("Code plan generated as markdown")
            return {
                'success': True,
                'plan_md': content
            }

        except Exception as e:
            logger.error(f"Error generating code plan: {e}")
            return {
                'success': False,
                'error': str(e)
            }

    def generate_code(self, strategy: Dict[str, Any],
                      changes: List[Dict],
                      plan_md: Optional[str] = None,
                      coding_mode: str = 'autonomous',
                      ask_question_callback: Optional[callable] = None) -> Dict[str, Any]:
        """
        Generate actual code changes for an approved strategy using multi-step generation.

        This uses a two-phase approach to handle arbitrarily large code changes:
        1. Planning phase: Generate a list of files to create/modify (or extract from existing plan)
        2. Generation phase: Generate each file's content separately

        Args:
            strategy: The approved strategy
            changes: List of changes to implement
            plan_md: Optional existing plan markdown to use instead of generating new
            coding_mode: 'autonomous' or 'interactive' - controls whether to ask questions
            ask_question_callback: Callback function for interactive mode questions

        Returns:
            Dictionary with success status and code_changes list
        """
        logger.info(f"Generating code for {len(changes)} changes (multi-step, mode={coding_mode})...")

        try:
            # Phase 1: Get implementation plan (list of files)
            if plan_md:
                logger.info("Phase 1: Extracting file list from existing plan...")
                file_plan = self._extract_files_from_plan(strategy, plan_md)
            else:
                logger.info("Phase 1: Generating implementation plan...")
                file_plan = self._generate_implementation_plan(strategy, changes)

            if not file_plan.get('success'):
                # In interactive mode, ask if user wants to provide the file list
                if coding_mode == 'interactive' and ask_question_callback:
                    answer = ask_question_callback(
                        f"I couldn't automatically determine the files to create.\n\n"
                        f"Error: {file_plan.get('error', 'Unknown')}\n\n"
                        f"Would you like to provide guidance?",
                        ["Skip this strategy", "Let me specify files"]
                    )
                    if answer and "specify" in answer.lower():
                        # User will provide file list
                        custom_files = ask_question_callback(
                            "Please list the file paths to create/modify, one per line:",
                            None
                        )
                        if custom_files:
                            files_to_generate = [{'file_path': f.strip(), 'change_type': 'create', 'description': ''}
                                                 for f in custom_files.strip().split('\n') if f.strip()]
                            file_plan = {'success': True, 'files': files_to_generate}

                if not file_plan.get('success'):
                    return {
                        'success': False,
                        'error': file_plan.get('error', 'Failed to generate implementation plan')
                    }

            files_to_generate = file_plan.get('files', [])
            logger.info(f"Implementation plan: {len(files_to_generate)} files to generate")

            # Interactive mode: Confirm file list with user
            if coding_mode == 'interactive' and ask_question_callback and files_to_generate:
                file_list_str = '\n'.join([f"• {f['file_path']}" for f in files_to_generate[:15]])
                if len(files_to_generate) > 15:
                    file_list_str += f"\n• ... and {len(files_to_generate) - 15} more"

                answer = ask_question_callback(
                    f"I'm planning to create/modify these {len(files_to_generate)} files:\n\n"
                    f"{file_list_str}\n\n"
                    f"Does this look correct?",
                    ["Yes, proceed", "No, let me adjust"]
                )
                if answer and "adjust" in answer.lower():
                    custom_input = ask_question_callback(
                        "Please provide corrections or additional files (one per line).\n"
                        "You can also describe what should change:",
                        None
                    )
                    if custom_input:
                        # Add any new files mentioned
                        for line in custom_input.strip().split('\n'):
                            line = line.strip()
                            if line and any(line.endswith(ext) for ext in ['.ts', '.tsx', '.js', '.jsx', '.json', '.css']):
                                if not any(f['file_path'] == line for f in files_to_generate):
                                    files_to_generate.append({'file_path': line, 'change_type': 'create', 'description': custom_input})

            # Phase 2: Generate each file separately
            logger.info("Phase 2: Generating file contents...")
            code_changes = []
            failed_files = []

            for i, file_info in enumerate(files_to_generate):
                file_path = file_info.get('file_path', f'unknown_file_{i}')
                logger.info(f"Generating file {i+1}/{len(files_to_generate)}: {file_path}")

                # Interactive mode: Ask about specific file before generating
                user_guidance = None
                if coding_mode == 'interactive' and ask_question_callback:
                    # For key files, ask for input
                    if i == 0 or 'component' in file_path.lower() or 'page' in file_path.lower():
                        answer = ask_question_callback(
                            f"About to generate: `{file_path}`\n\n"
                            f"Any specific requirements or guidance for this file?",
                            ["No, proceed automatically", "Yes, I have input"]
                        )
                        if answer and "input" in answer.lower():
                            user_guidance = ask_question_callback(
                                f"What should I know about `{file_path}`?",
                                None
                            )

                file_result = self._generate_single_file(
                    strategy=strategy,
                    changes=changes,
                    file_info=file_info,
                    all_files=files_to_generate,
                    user_guidance=user_guidance  # Pass any user guidance
                )

                if file_result.get('success'):
                    # Validate the generated code
                    new_content = file_result.get('new_content', '')
                    validation = self._validate_code_content(file_path, new_content)

                    if validation.get('valid'):
                        code_changes.append({
                            'file_path': file_path,
                            'change_type': file_info.get('change_type', 'create'),
                            'description': file_info.get('description', ''),
                            'new_content': new_content
                        })
                        logger.info(f"  ✓ {file_path} generated and validated")
                    else:
                        # Try once more with explicit instruction to complete
                        logger.warning(f"  ⚠ {file_path} validation failed: {validation.get('error')}, retrying...")
                        retry_result = self._generate_single_file(
                            strategy=strategy,
                            changes=changes,
                            file_info=file_info,
                            all_files=files_to_generate,
                            retry_hint=validation.get('error')
                        )
                        if retry_result.get('success'):
                            retry_content = retry_result.get('new_content', '')
                            retry_validation = self._validate_code_content(file_path, retry_content)
                            if retry_validation.get('valid'):
                                code_changes.append({
                                    'file_path': file_path,
                                    'change_type': file_info.get('change_type', 'create'),
                                    'description': file_info.get('description', ''),
                                    'new_content': retry_content
                                })
                                logger.info(f"  ✓ {file_path} generated on retry")
                            else:
                                failed_files.append({'file': file_path, 'error': retry_validation.get('error')})
                                logger.error(f"  ✗ {file_path} failed validation after retry")
                        else:
                            failed_files.append({'file': file_path, 'error': retry_result.get('error')})
                else:
                    failed_files.append({'file': file_path, 'error': file_result.get('error')})
                    logger.error(f"  ✗ {file_path} generation failed: {file_result.get('error')}")

            # Check results
            if not code_changes:
                return {
                    'success': False,
                    'error': f'No files generated successfully. Failures: {failed_files}'
                }

            if failed_files:
                logger.warning(f"Completed with {len(failed_files)} failed files: {[f['file'] for f in failed_files]}")

            logger.info(f"Successfully generated {len(code_changes)} code changes")
            return {
                'success': True,
                'code_changes': code_changes,
                'summary': file_plan.get('summary', ''),
                'test_commands': file_plan.get('test_commands', []),
                'failed_files': failed_files if failed_files else None
            }

        except Exception as e:
            logger.error(f"Error in multi-step code generation: {e}")
            return {
                'success': False,
                'error': str(e)
            }

    def _generate_implementation_plan(self, strategy: Dict[str, Any],
                                       changes: List[Dict]) -> Dict[str, Any]:
        """
        Phase 1: Generate a plan listing all files to create/modify.

        Uses simple plain-text format (one file path per line) which:
        - Never needs JSON parsing
        - Can't be corrupted by truncation
        - Works reliably every time
        """
        changes_summary = json.dumps(changes, indent=2)[:2000]  # Limit context size

        prompt = f"""List the file paths needed for this {self.company_name} (Next.js/React) change.

Strategy: {strategy.get('summary', 'N/A')}

Changes to implement:
{changes_summary}

List ONLY file paths, one per line. Example:
app/api/example/route.ts
lib/utils/helper.ts
components/Feature.tsx"""

        try:
            content = self._call_with_fallback(
                prompt=prompt,
                task_type='code_generation',
                max_tokens=2000,
                temperature=0.2
            )

            # Parse line-by-line - simple and reliable
            files = []
            seen = set()
            valid_extensions = ['.ts', '.tsx', '.js', '.jsx', '.json', '.css', '.scss', '.py', '.sh', '.md']

            for line in content.strip().split('\n'):
                # Clean up the line
                path = line.strip()
                path = path.lstrip('-').lstrip('*').lstrip('0123456789.').strip()

                # Remove backticks, quotes
                if path.startswith('`') and path.endswith('`'):
                    path = path[1:-1]
                if path.startswith('"') and path.endswith('"'):
                    path = path[1:-1]
                if path.startswith("'") and path.endswith("'"):
                    path = path[1:-1]

                # Skip if not a valid file path
                if not path or path in seen:
                    continue
                if not any(path.endswith(ext) for ext in valid_extensions):
                    continue
                if path.startswith('#') or path.startswith('//'):
                    continue

                seen.add(path)
                files.append({
                    'file_path': path,
                    'change_type': 'create',
                    'description': ''
                })

            if not files:
                return {'success': False, 'error': 'No valid file paths found in response'}

            logger.info(f"Implementation plan: {len(files)} files")
            return {
                'success': True,
                'files': files,
                'summary': strategy.get('summary', ''),
                'test_commands': ['npm run lint']
            }

        except Exception as e:
            logger.error(f"Error generating implementation plan: {e}")
            return {'success': False, 'error': str(e)}

    def _extract_files_from_plan(self, strategy: Dict[str, Any],
                                  plan_md: str) -> Dict[str, Any]:
        """
        Extract file list from an existing markdown plan.

        Uses the LLM to parse the plan and extract the list of files to create/modify.
        This ensures the file list matches what was reviewed in the plan.

        Args:
            strategy: The strategy being implemented
            plan_md: The markdown plan content

        Returns:
            Dictionary with success status and files list
        """
        # Truncate plan if too long
        plan_content = plan_md[:6000] if len(plan_md) > 6000 else plan_md

        prompt = f"""Extract the file paths from this implementation plan.

## Plan Document
{plan_content}

## Your Task
List ONLY the file paths mentioned in the plan, one per line.
Include ONLY files that need to be created or modified.
Do not include explanations, just the paths.

Example output:
app/api/example/route.ts
lib/utils/helper.ts
components/Feature.tsx"""

        try:
            content = self._call_with_fallback(
                prompt=prompt,
                task_type='code_generation',
                max_tokens=2000,
                temperature=0.1  # Low temperature for extraction
            )

            # Parse line-by-line - same logic as _generate_implementation_plan
            files = []
            seen = set()
            valid_extensions = ['.ts', '.tsx', '.js', '.jsx', '.json', '.css', '.scss', '.py', '.sh', '.md']

            for line in content.strip().split('\n'):
                path = line.strip()
                path = path.lstrip('-').lstrip('*').lstrip('0123456789.').strip()

                if path.startswith('`') and path.endswith('`'):
                    path = path[1:-1]
                if path.startswith('"') and path.endswith('"'):
                    path = path[1:-1]
                if path.startswith("'") and path.endswith("'"):
                    path = path[1:-1]

                if not path or path in seen:
                    continue
                if not any(path.endswith(ext) for ext in valid_extensions):
                    continue
                if path.startswith('#') or path.startswith('//'):
                    continue

                seen.add(path)
                files.append({
                    'file_path': path,
                    'change_type': 'create',
                    'description': ''
                })

            if not files:
                logger.warning("No files extracted from plan, falling back to generation")
                return {'success': False, 'error': 'No valid file paths found in plan'}

            logger.info(f"Extracted {len(files)} files from existing plan")
            return {
                'success': True,
                'files': files,
                'summary': strategy.get('summary', ''),
                'test_commands': ['npm run lint'],
                'from_existing_plan': True
            }

        except Exception as e:
            logger.error(f"Error extracting files from plan: {e}")
            return {'success': False, 'error': str(e)}

    def _generate_single_file(self, strategy: Dict[str, Any], changes: List[Dict],
                              file_info: Dict, all_files: List[Dict],
                              retry_hint: Optional[str] = None,
                              user_guidance: Optional[str] = None) -> Dict[str, Any]:
        """
        Phase 2: Generate content for a single file.

        Uses raw code format (no JSON wrapper) to avoid escaping overhead.

        Args:
            strategy: The strategy being implemented
            changes: List of changes to implement
            file_info: Info about this specific file
            all_files: List of all files being generated (for context)
            retry_hint: Optional hint from a failed previous attempt
            user_guidance: Optional guidance from the user (interactive mode)
        """
        file_path = file_info.get('file_path', '')

        retry_instruction = ""
        if retry_hint:
            retry_instruction = f"\n**PREVIOUS ATTEMPT FAILED:** {retry_hint}\nEnsure all brackets are balanced.\n"

        guidance_instruction = ""
        if user_guidance:
            guidance_instruction = f"\n**USER GUIDANCE:** {user_guidance}\n"

        prompt = f"""Generate the COMPLETE content for {file_path} ({self.company_name} Next.js/React).
{retry_instruction}{guidance_instruction}
Strategy: {strategy.get('summary', 'N/A')}

Output ONLY the raw code - no JSON, no markdown code blocks.

Requirements:
1. Output the COMPLETE file - every function must be finished
2. Ensure all braces {{}}, brackets [], and parens () are balanced
3. Include all imports at the top
4. NO truncation - write the full implementation

Start your response with the first line of code (e.g., import statement):"""

        try:
            content = self._call_with_fallback(
                prompt=prompt,
                task_type='code_generation',
                max_tokens=8000,
                temperature=0.2
            )

            code = self._strip_markdown_blocks(content)
            return {
                'success': True,
                'file_path': file_path,
                'new_content': code
            }

        except Exception as e:
            logger.error(f"Error generating {file_path}: {e}")
            return {'success': False, 'error': str(e)}

    def _validate_code_content(self, file_path: str, content: str) -> Dict[str, Any]:
        """
        Validate that generated code content is complete and syntactically valid.

        Basic checks:
        - Not empty
        - Balanced braces/brackets/parens
        - Doesn't end mid-statement
        - Has expected structure for file type
        """
        if not content or len(content.strip()) < 10:
            return {'valid': False, 'error': 'Content is empty or too short'}

        # Check for obvious truncation indicators
        truncation_indicators = [
            content.rstrip().endswith('...'),
            content.rstrip().endswith('//'),
            content.rstrip().endswith('/*'),
            content.rstrip().endswith(','),
            content.rstrip().endswith('('),
            content.rstrip().endswith('['),
            content.rstrip().endswith('{'),
            '// ... rest of' in content.lower(),
            '// TODO: implement' in content.lower() and content.count('// TODO') > 3,
        ]

        if any(truncation_indicators):
            return {'valid': False, 'error': 'Content appears truncated (ends with incomplete token)'}

        # Check balanced brackets/braces/parens
        brackets = {'(': ')', '[': ']', '{': '}'}
        stack = []
        in_string = False
        string_char = None
        prev_char = ''

        for char in content:
            # Track string state
            if char in '"\'`' and prev_char != '\\':
                if not in_string:
                    in_string = True
                    string_char = char
                elif char == string_char:
                    in_string = False
                    string_char = None

            # Only check brackets outside strings
            if not in_string:
                if char in brackets:
                    stack.append(brackets[char])
                elif char in brackets.values():
                    if not stack or stack.pop() != char:
                        return {'valid': False, 'error': f'Unbalanced bracket: {char}'}

            prev_char = char

        if stack:
            return {'valid': False, 'error': f'Unclosed brackets: {len(stack)} remaining'}

        # File-type specific checks
        ext = file_path.split('.')[-1].lower() if '.' in file_path else ''

        if ext in ['ts', 'tsx', 'js', 'jsx']:
            # Should have at least one export or function for most files
            if not any(keyword in content for keyword in ['export', 'function', 'const', 'class', 'import']):
                # Could be a config file, so this is a warning not an error
                logger.debug(f"Warning: {file_path} has no typical JS/TS keywords")

        if ext == 'json':
            try:
                json.loads(content)
            except json.JSONDecodeError as e:
                return {'valid': False, 'error': f'Invalid JSON: {str(e)[:50]}'}

        return {'valid': True}

    def _strip_markdown_blocks(self, content: str) -> str:
        """Strip markdown code blocks from content"""
        content = content.strip()
        if content.startswith('```json'):
            content = content[7:]
        elif content.startswith('```typescript'):
            content = content[13:]
        elif content.startswith('```'):
            content = content[3:]
        if content.endswith('```'):
            content = content[:-3]
        return content.strip()

    def _prepare_context(self, data: Dict[str, Any], ceo_context: str = None) -> str:
        """Prepare context prompt for Claude"""
        metrics = data.get('metrics', {})
        feedback = data.get('feedback', [])
        goals = data.get('goals', {})
        current_features = data.get('current_features', [])
        constraints = data.get('constraints', {})

        # Extract key metrics
        dau = metrics.get('dau', 'N/A')
        mau = metrics.get('mau', 'N/A')
        arpu = metrics.get('arpu', 'N/A')
        revenue = metrics.get('revenue', 'N/A')
        churn_rate = metrics.get('churn_rate', 'N/A')
        gen_success_rate = metrics.get('gen_success_rate', 'N/A')
        credit_usage = metrics.get('credit_usage', 'N/A')
        avg_session_iterations = metrics.get('avg_session_iterations', 'N/A')

        # Extract Stripe MoM and MRR data
        stripe_data = metrics.get('kpi_data', {}).get('stripe', {})
        mrr = stripe_data.get('mrr', 'N/A')
        revenue_previous = stripe_data.get('revenue_previous_period', 'N/A')
        revenue_mom_pct = stripe_data.get('revenue_mom_pct', 'N/A')
        current_mrr = goals.get('current_mrr', mrr)

        # Extract kpi_data for detailed analytics
        kpi_data = metrics.get('kpi_data', {})
        ga_data = kpi_data.get('google_analytics', {})
        crux_data = kpi_data.get('web_vitals', {})
        clarity_data = kpi_data.get('clarity', {})
        calculated = kpi_data.get('calculated', {})

        # Summarize feedback
        feedback_summary = self._summarize_feedback(feedback)

        # Build analytics section
        analytics_section = ""
        if ga_data and 'error' not in ga_data:
            traffic_sources = ga_data.get('traffic_sources', {})
            top_sources = list(traffic_sources.keys())[:3] if traffic_sources else []
            top_pages = ga_data.get('top_pages', [])[:3]

            analytics_section = f"""
## Web Analytics (Google Analytics 4 - Last 7 Days)
- Sessions: {ga_data.get('sessions', 'N/A')}
- Unique Users: {ga_data.get('total_users', 'N/A')}
- Pageviews: {ga_data.get('pageviews', 'N/A')}
- Bounce Rate: {ga_data.get('bounce_rate', 'N/A')}%
- Avg Session Duration: {ga_data.get('avg_session_duration_seconds', 'N/A')}s
- Engagement Rate: {calculated.get('engagement_rate', 'N/A')}%
- Pages per Session: {calculated.get('pages_per_session', 'N/A')}
- Top Traffic Sources: {', '.join(top_sources) if top_sources else 'N/A'}
- Top Pages: {', '.join([p.get('path', '') for p in top_pages]) if top_pages else 'N/A'}
"""
            # Add conversion events if available
            conversion_events = ga_data.get('conversion_events', {})
            if conversion_events:
                analytics_section += "- Conversion Events:\n"
                for event, data in conversion_events.items():
                    analytics_section += f"  - {event}: {data.get('count', 0)}\n"

        # Build Clarity UX behavior section
        clarity_section = ""
        if clarity_data and 'error' not in clarity_data:
            clarity_section = f"""
## UX Behavior Analytics (Microsoft Clarity - Last 3 Days)
- Total Sessions: {clarity_data.get('total_sessions', 'N/A')}
- Distinct Users: {clarity_data.get('distinct_users', 'N/A')}
- Pages per Session: {clarity_data.get('pages_per_session', 'N/A')}
- Scroll Depth: {clarity_data.get('scroll_depth', 'N/A')}%
- Rage Clicks: {clarity_data.get('rage_clicks', 'N/A')}
- Dead Clicks: {clarity_data.get('dead_clicks', 'N/A')}
- Excessive Scrolling: {clarity_data.get('excessive_scrolling', 'N/A')}
- Quick Backs: {clarity_data.get('quick_backs', 'N/A')}
- JS Errors: {clarity_data.get('js_errors', 'N/A')}
- Avg Active Duration: {clarity_data.get('active_duration_avg', 'N/A')}s
"""
            # Add per-page UX issue breakdown
            page_issues = clarity_data.get('page_issues', [])
            if page_issues:
                clarity_section += "\nUX Issues by Page (sorted by severity):\n"
                for page in page_issues:
                    parts = []
                    if page.get('dead_clicks'):
                        parts.append(f"{page['dead_clicks']} dead clicks")
                    if page.get('rage_clicks'):
                        parts.append(f"{page['rage_clicks']} rage clicks")
                    if page.get('quick_backs'):
                        parts.append(f"{page['quick_backs']} quick backs")
                    if parts:
                        clarity_section += f"  - {page['path']}: {', '.join(parts)}\n"

        # Build performance section
        performance_section = ""
        if crux_data and 'error' not in crux_data:
            lcp = crux_data.get('lcp', {})
            cls = crux_data.get('cls', {})
            inp = crux_data.get('inp', {})

            performance_section = f"""
## Core Web Vitals (CrUX - Mobile, 28-day rolling average)
- LCP (Largest Contentful Paint): {lcp.get('p75_ms', 'N/A')}ms ({lcp.get('status', 'N/A')})
- CLS (Cumulative Layout Shift): {cls.get('p75', 'N/A')} ({cls.get('status', 'N/A')})
- INP (Interaction to Next Paint): {inp.get('p75_ms', 'N/A')}ms ({inp.get('status', 'N/A')})
- Core Web Vitals Passing: {crux_data.get('core_web_vitals_passing', 'N/A')}
- Performance Score: {calculated.get('performance_score', 'N/A')}/100
- Good Experience %: {calculated.get('good_experience_pct', 'N/A')}%
"""
            # Add correlation insight if available
            correlation = calculated.get('performance_bounce_correlation')
            if correlation:
                performance_section += f"- Performance-Bounce Correlation: {correlation}\n"

        # Build context
        context = f"""You are an AI optimization strategist for {self.company_name}, an AI image/video generation SaaS platform that aims to be "Netflix for everyday producers."

Your task is to analyze the current data and propose 1-3 data-driven optimization strategies that can achieve incremental gains (5-20% lifts) in key metrics.

## Current Metrics (Last 30 Days)
- Daily Active Users (DAU): {dau}
- Monthly Active Users (MAU): {mau}
- Average Revenue Per User (ARPU): ${arpu}
- Revenue (Last 30 Days): ${revenue}
- Revenue (Previous 30 Days): ${revenue_previous}
- Revenue Month-over-Month Change: {f'{revenue_mom_pct}%' if revenue_mom_pct != 'N/A' else 'N/A'}
- Current MRR (from active subscriptions): ${current_mrr}
- Churn Rate: {churn_rate}%
- Generation Success Rate: {gen_success_rate}%
- Credit Usage: {credit_usage}
- Avg Session Iterations: {avg_session_iterations}
{analytics_section}{clarity_section}{performance_section}
## User Feedback Summary
{feedback_summary}

## Business Goals & Next Milestone
- Current MRR: ${current_mrr}
- **Next MRR Milestone: ${goals.get('target_mrr', 10000):,}**
- Target ARPU: ${goals.get('target_arpu', 20)}
- Target Retention Rate: {goals.get('target_retention', 85)}%

All strategies should be oriented toward reaching the next MRR milestone. Consider what levers (acquisition, activation, retention, revenue, referral) will most efficiently close the gap between current MRR and the target.

## Current Features
{', '.join(current_features) if current_features else 'Basic AI generation features'}

## Constraints
- Risk Tolerance: {constraints.get('risk_tolerance', 'Low')}
- Budget: ${constraints.get('budget', 1000)}/month
- Time Horizon: {constraints.get('time_horizon', '1-2 weeks')}

## Your Task
Analyze the data above and propose 1-3 specific, actionable optimization strategies. For each strategy:

1. Focus on high-impact, low-risk changes first
2. Base recommendations on actual data patterns
3. Provide clear expected impact (be realistic, aim for 5-20% gains)
4. Specify concrete changes needed (e.g., UI tweaks, prompt modifications, feature additions)
5. Consider quick wins that can compound over time

Prioritize:
- User retention improvements
- Conversion rate optimization
- Generation success rate improvements
- ARPU increases through better monetization
- Paths toward UGC features (sharing, galleries) if metrics are healthy

Respond ONLY with valid JSON in this exact format (no markdown, no extra text):
{{
  "summary": "Brief 1-2 sentence summary of the overall strategy",
  "expected_impact": "Expected metric improvements (e.g., '+15% gen success rate, +10% retention')",
  "focus_area": "Primary focus (e.g., 'retention', 'monetization', 'user_experience')",
  "priority": 1-10,
  "changes": [
    {{
      "action": "Specific change to implement",
      "type": "ui_change|model_change|feature_addition|prompt_optimization|pricing_adjustment",
      "details": "Technical details of the change",
      "expected_impact": "Expected impact of this specific change",
      "risk_level": "low|medium|high",
      "effort": "low|medium|high",
      "files_to_modify": ["list", "of", "files"],
      "test_plan": "How to test this change"
    }}
  ],
  "dependency_analysis": {{
    "dependencies": [
      {{
        "from_change": 1,
        "to_change": 2,
        "relationship": "required_by|enhances|conflicts_with",
        "explanation": "Why this dependency exists"
      }}
    ],
    "independent_changes": [1],
    "recommended_execution_order": [1, 2, 3],
    "can_split": true,
    "split_notes": "Brief note on whether changes can be safely executed independently"
  }}
}}"""

        # Inject CEO direction if provided via "Run as New Strategy" button
        if ceo_context:
            ceo_section = (
                f'\n\n## CEO Direction\n'
                f'The CEO has specifically requested this strategy based on the following conversation message. '
                f'Use this as the primary direction and focus for the strategy while still grounding it in the data above:\n\n'
                f'"""{ceo_context}"""\n'
            )
            # Insert before the JSON format instructions
            json_marker = "Respond ONLY with valid JSON"
            if json_marker in context:
                context = context.replace(json_marker, ceo_section + json_marker)
            else:
                context += ceo_section

        return context

    def _summarize_feedback(self, feedback: List[Dict]) -> str:
        """Summarize user feedback for context"""
        if not feedback:
            return "No recent feedback available."

        # Group by sentiment
        positive = [f for f in feedback if f.get('sentiment') == 'positive']
        negative = [f for f in feedback if f.get('sentiment') == 'negative']
        neutral = [f for f in feedback if f.get('sentiment') == 'neutral']

        summary = f"Total: {len(feedback)} feedback items\n"
        summary += f"- Positive: {len(positive)}\n"
        summary += f"- Negative: {len(negative)}\n"
        summary += f"- Neutral: {len(neutral)}\n\n"

        # Add sample negative feedback (most actionable)
        if negative:
            summary += "Recent Issues:\n"
            for fb in negative[:3]:
                summary += f"- {fb.get('text', '')[:100]}\n"

        # Add sample positive feedback
        if positive:
            summary += "\nWhat's Working:\n"
            for fb in positive[:2]:
                summary += f"- {fb.get('text', '')[:100]}\n"

        return summary

    def _call_claude(self, context: str) -> Dict[str, Any]:
        """Call Claude API to generate strategy (with tiered fallback)"""
        try:
            content = self._call_with_fallback(
                prompt=context,
                task_type='strategy_generation',
                max_tokens=4096,
                temperature=0.7
            )

            # Strip markdown code blocks if present
            content = content.strip()
            if content.startswith('```json'):
                content = content[7:]  # Remove ```json
            elif content.startswith('```'):
                content = content[3:]  # Remove ```
            if content.endswith('```'):
                content = content[:-3]  # Remove trailing ```
            content = content.strip()

            # Parse JSON response
            strategy = json.loads(content)

            # Validate required fields
            required_fields = ['summary', 'expected_impact', 'focus_area', 'priority', 'changes']
            for field in required_fields:
                if field not in strategy:
                    raise ValueError(f"Missing required field: {field}")

            return strategy

        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse Claude response as JSON: {e}")
            logger.error(f"Response content: {content}")
            raise ValueError("Claude did not return valid JSON")

        except Exception as e:
            logger.error(f"Error calling Claude API: {e}")
            raise

    def evaluate_strategy_success(self, metrics_before: Dict, metrics_after: Dict,
                                   expected_impact: str) -> Dict[str, Any]:
        """
        Evaluate if a deployed strategy was successful

        Args:
            metrics_before: Metrics before deployment
            metrics_after: Metrics after deployment
            expected_impact: Expected impact from strategy

        Returns:
            Evaluation with success indicators
        """
        logger.info("Evaluating strategy success...")

        try:
            context = f"""You are evaluating the success of an optimization strategy for {self.company_name}.

## Metrics Before Deployment
{json.dumps(metrics_before, indent=2)}

## Metrics After Deployment
{json.dumps(metrics_after, indent=2)}

## Expected Impact
{expected_impact}

## Your Task
Analyze the before/after metrics and determine if the strategy was successful. Calculate actual changes and compare to expected impact.

Respond ONLY with valid JSON in this exact format (no markdown, no extra text):
{{
  "success": true/false,
  "actual_impact": "Description of actual changes observed",
  "key_improvements": ["list", "of", "improvements"],
  "key_regressions": ["list", "of", "regressions"],
  "recommendations": "Next steps based on results",
  "priority_score": 1-10,
  "metrics_comparison": {{
    "dau_change_percent": number,
    "revenue_change_percent": number,
    "retention_change_percent": number
  }}
}}"""

            # Use Haiku for evaluation (simpler task), with fallback
            content = self._call_with_fallback(
                prompt=context,
                task_type='evaluation',
                max_tokens=1500,
                temperature=0.5
            )
            evaluation = json.loads(content)

            logger.info(f"Strategy evaluation: {'SUCCESS' if evaluation.get('success') else 'FAILED'}")
            return evaluation

        except Exception as e:
            logger.error(f"Error evaluating strategy: {e}")
            raise

    def analyze_trends(self, historical_metrics: List[Dict]) -> Dict[str, Any]:
        """
        Analyze trends across multiple cycles

        Args:
            historical_metrics: List of metric snapshots over time

        Returns:
            Trend analysis with insights
        """
        if len(historical_metrics) < 2:
            return {
                "trends": "Insufficient data for trend analysis",
                "insights": []
            }

        try:
            context = f"""Analyze the following historical metrics for {self.company_name} and identify trends:

{json.dumps(historical_metrics, indent=2)}

Respond with valid JSON only:
{{
  "trends": "Overall trend description",
  "insights": ["list", "of", "key", "insights"],
  "recommendations": ["recommended", "focus", "areas"]
}}"""

            # Use Haiku for trend analysis (simpler task), with fallback
            content = self._call_with_fallback(
                prompt=context,
                task_type='trend_analysis',
                max_tokens=1000,
                temperature=0.5
            )
            return json.loads(content)

        except Exception as e:
            logger.error(f"Error analyzing trends: {e}")
            return {"trends": "Analysis failed", "insights": []}

    def generate_dependency_analysis(self, changes: List[Dict]) -> Dict[str, Any]:
        """Generate dependency analysis for a list of changes using LLM."""
        if len(changes) <= 1:
            return {
                'dependencies': [],
                'independent_changes': [1],
                'recommended_execution_order': [1],
                'can_split': False,
                'split_notes': 'Only one change, nothing to split'
            }

        changes_desc = json.dumps(changes, indent=2)
        prompt = f"""Analyze the dependencies between these proposed changes for a web application:

{changes_desc}

For each pair of changes, determine if there is a dependency (required_by, enhances, or conflicts_with).

Respond ONLY with valid JSON (no markdown):
{{
  "dependencies": [
    {{
      "from_change": 1,
      "to_change": 2,
      "relationship": "required_by|enhances|conflicts_with",
      "explanation": "Why this dependency exists"
    }}
  ],
  "independent_changes": [1],
  "recommended_execution_order": [1, 2, 3],
  "can_split": true,
  "split_notes": "Brief note on whether changes can be safely executed independently"
}}"""

        try:
            content = self._call_with_fallback(
                prompt=prompt,
                task_type='dependency_analysis',
                max_tokens=1000,
                temperature=0.3
            )
            content = content.strip()
            if content.startswith('```json'):
                content = content[7:]
            elif content.startswith('```'):
                content = content[3:]
            if content.endswith('```'):
                content = content[:-3]
            return json.loads(content.strip())
        except Exception as e:
            logger.error(f"Dependency analysis failed: {e}")
            return {
                'dependencies': [],
                'independent_changes': list(range(1, len(changes) + 1)),
                'recommended_execution_order': list(range(1, len(changes) + 1)),
                'can_split': True,
                'split_notes': 'Analysis unavailable'
            }

    def backfill_dependency_analysis(self, db) -> Dict[str, Any]:
        """Backfill dependency analysis for all existing strategies that don't have it."""
        results = {'updated': 0, 'skipped': 0, 'failed': 0, 'details': []}

        # Get all strategies that might need backfill
        for status in ['pending', 'saved', 'approved']:
            strategies = db.client.table('strategies').select('*').eq('status', status).execute().data
            for strategy in strategies:
                sid = strategy['id']
                # Check if already has dependency_analysis
                snapshot = strategy.get('data_snapshot', '{}')
                if isinstance(snapshot, str):
                    try:
                        snapshot = json.loads(snapshot)
                    except (json.JSONDecodeError, TypeError):
                        snapshot = {}

                if snapshot.get('dependency_analysis'):
                    results['skipped'] += 1
                    continue

                changes = strategy.get('changes', [])
                if isinstance(changes, str):
                    try:
                        changes = json.loads(changes)
                    except (json.JSONDecodeError, TypeError):
                        changes = []

                if len(changes) <= 1:
                    results['skipped'] += 1
                    continue

                try:
                    dep_analysis = self.generate_dependency_analysis(changes)
                    snapshot['dependency_analysis'] = dep_analysis
                    db.client.table('strategies').update({
                        'data_snapshot': json.dumps(snapshot)
                    }).eq('id', sid).execute()
                    results['updated'] += 1
                    results['details'].append(f"Strategy #{sid}: updated")
                    logger.info(f"Backfilled dependency analysis for strategy #{sid}")
                except Exception as e:
                    results['failed'] += 1
                    results['details'].append(f"Strategy #{sid}: failed ({e})")
                    logger.error(f"Failed to backfill strategy #{sid}: {e}")

        return results


# Singleton instance
_strategy_engine = None

def get_strategy_engine() -> StrategyEngine:
    """Get or create strategy engine singleton"""
    global _strategy_engine
    if _strategy_engine is None:
        _strategy_engine = StrategyEngine()
    return _strategy_engine
