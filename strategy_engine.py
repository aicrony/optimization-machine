"""
AI Strategy Engine
Generates optimization strategies using Claude (Anthropic API)
"""

import os
import json
import logging
from typing import Dict, List, Optional, Any
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
        'haiku': 'claude-haiku-4-5-20251212',
        'sonnet': 'claude-sonnet-4-5-20250929',
        'opus': 'claude-opus-4-5-20251101',
    }

    # Default task-to-tier mapping
    DEFAULT_MODEL_TIERS = {
        'strategy_generation': 'sonnet',   # Main strategy work
        'strategy_refinement': 'sonnet',   # Refining based on feedback
        'code_generation': 'sonnet',       # Generating actual code changes
        'evaluation': 'haiku',             # Evaluating results (simpler task)
        'trend_analysis': 'haiku',         # Trend analysis (simpler task)
    }

    def __init__(self):
        api_key = os.getenv('ANTHROPIC_API_KEY')
        if not api_key:
            raise ValueError("ANTHROPIC_API_KEY must be set in config.env")

        self.client = Anthropic(api_key=api_key)

        # Load model configuration from env (with defaults)
        self.models = {
            'haiku': os.getenv('CLAUDE_MODEL_HAIKU', self.DEFAULT_MODELS['haiku']),
            'sonnet': os.getenv('CLAUDE_MODEL_SONNET', self.DEFAULT_MODELS['sonnet']),
            'opus': os.getenv('CLAUDE_MODEL_OPUS', self.DEFAULT_MODELS['opus']),
        }

        # Load task-to-tier mapping from env (with defaults)
        self.model_tiers = {
            'strategy_generation': os.getenv('CLAUDE_TIER_STRATEGY_GENERATION',
                                             self.DEFAULT_MODEL_TIERS['strategy_generation']),
            'strategy_refinement': os.getenv('CLAUDE_TIER_STRATEGY_REFINEMENT',
                                             self.DEFAULT_MODEL_TIERS['strategy_refinement']),
            'code_generation': os.getenv('CLAUDE_TIER_CODE_GENERATION',
                                         self.DEFAULT_MODEL_TIERS['code_generation']),
            'evaluation': os.getenv('CLAUDE_TIER_EVALUATION',
                                    self.DEFAULT_MODEL_TIERS['evaluation']),
            'trend_analysis': os.getenv('CLAUDE_TIER_TREND_ANALYSIS',
                                        self.DEFAULT_MODEL_TIERS['trend_analysis']),
        }

        logger.info(f"Strategy engine initialized with tiered Claude models: {self.model_tiers}")

    def _get_model(self, task_type: str) -> str:
        """Get the appropriate model for a task type"""
        tier = self.model_tiers.get(task_type, 'sonnet')
        return self.models[tier]

    def _call_with_fallback(self, prompt: str, task_type: str,
                            max_tokens: int = 2000,
                            temperature: float = 0.7) -> str:
        """
        Call Claude with automatic escalation on failure.

        Tries the configured model first. If it fails (bad JSON, validation error),
        escalates to the next tier: haiku -> sonnet -> opus

        Args:
            prompt: The prompt to send
            task_type: Type of task (for model selection)
            max_tokens: Max tokens for response
            temperature: Sampling temperature

        Returns:
            Raw response text from Claude
        """
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
                    response = self.client.messages.create(
                        model=model,
                        max_tokens=max_tokens,
                        temperature=temperature,
                        messages=messages
                    )
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

    def generate_strategy(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Generate an optimization strategy based on current data

        Args:
            data: Dictionary containing:
                - metrics: Latest performance metrics
                - feedback: Recent user feedback
                - goals: Business goals (e.g., target MRR, ARPU)
                - current_features: Current app features
                - constraints: Constraints (e.g., budget, risk tolerance)

        Returns:
            Strategy dictionary with summary, changes, expected_impact, priority
        """
        logger.info("Generating strategy...")

        try:
            # Prepare context for Claude
            context = self._prepare_context(data)

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

    def generate_code(self, strategy: Dict[str, Any],
                      changes: List[Dict]) -> Dict[str, Any]:
        """
        Generate actual code changes for an approved strategy using multi-step generation.

        This uses a two-phase approach to handle arbitrarily large code changes:
        1. Planning phase: Generate a list of files to create/modify
        2. Generation phase: Generate each file's content separately

        Args:
            strategy: The approved strategy
            changes: List of changes to implement

        Returns:
            Dictionary with success status and code_changes list
        """
        logger.info(f"Generating code for {len(changes)} changes (multi-step)...")

        try:
            # Phase 1: Generate implementation plan (list of files)
            logger.info("Phase 1: Generating implementation plan...")
            file_plan = self._generate_implementation_plan(strategy, changes)

            if not file_plan.get('success'):
                return {
                    'success': False,
                    'error': file_plan.get('error', 'Failed to generate implementation plan')
                }

            files_to_generate = file_plan.get('files', [])
            logger.info(f"Implementation plan: {len(files_to_generate)} files to generate")

            # Phase 2: Generate each file separately
            logger.info("Phase 2: Generating file contents...")
            code_changes = []
            failed_files = []

            for i, file_info in enumerate(files_to_generate):
                file_path = file_info.get('file_path', f'unknown_file_{i}')
                logger.info(f"Generating file {i+1}/{len(files_to_generate)}: {file_path}")

                file_result = self._generate_single_file(
                    strategy=strategy,
                    changes=changes,
                    file_info=file_info,
                    all_files=files_to_generate  # Context about other files
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

        prompt = f"""List the file paths needed for this Gentube.ai (Next.js/React) change.

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

    def _generate_single_file(self, strategy: Dict[str, Any], changes: List[Dict],
                              file_info: Dict, all_files: List[Dict],
                              retry_hint: Optional[str] = None) -> Dict[str, Any]:
        """
        Phase 2: Generate content for a single file.

        Uses raw code format (no JSON wrapper) to avoid escaping overhead.
        """
        file_path = file_info.get('file_path', '')

        retry_instruction = ""
        if retry_hint:
            retry_instruction = f"\n**PREVIOUS ATTEMPT FAILED:** {retry_hint}\nEnsure all brackets are balanced.\n"

        prompt = f"""Generate the COMPLETE content for {file_path} (Gentube.ai Next.js/React).
{retry_instruction}
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

    def _prepare_context(self, data: Dict[str, Any]) -> str:
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
        context = f"""You are an AI optimization strategist for Gentube.ai, an AI image/video generation SaaS platform that aims to be "Netflix for everyday producers."

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
{analytics_section}{performance_section}
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
            context = f"""You are evaluating the success of an optimization strategy for Gentube.ai.

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
            context = f"""Analyze the following historical metrics for Gentube.ai and identify trends:

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
