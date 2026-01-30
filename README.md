# Gentube.ai Optimization Machine

An autonomous self-optimizing system for Gentube.ai that strategizes improvements, proposes changes via mobile, gets approval, deploys changes, collects data, analyzes results, and iterates.

## Quick Start

```bash
source venv/bin/activate
```

```bash
pip install -r requirements.txt
```

```bash
python main.py run --days 1
```

```bash
python main.py run
```

## Overview

The Optimization Machine is designed to help Gentube.ai achieve incremental improvements (5-20% lifts) in key metrics like DAU, ARPU, and retention to reach $10k MRR through automated, data-driven optimization cycles.

### Key Features

- **AI-Powered Strategy Generation**: Uses Claude (Anthropic) to analyze data and propose optimization strategies
- **Human-in-the-Loop Approval**: Mobile approval via Telegram bot with single-message responses
- **Automated Execution**: Safely deploys approved changes with rollback capabilities
- **Comprehensive Monitoring**: Tracks metrics and detects anomalies post-deployment
- **Safety Guardrails**: Validates changes before execution and monitors for issues
- **Data-Driven Iteration**: Learns from each cycle to improve future strategies

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    Optimization Loop                     │
│                                                           │
│  ┌──────────┐    ┌──────────┐    ┌──────────┐          │
│  │  Collect │───▶│ Generate │───▶│ Request  │          │
│  │   Data   │    │ Strategy │    │ Approval │          │
│  └──────────┘    └──────────┘    └──────────┘          │
│                                          │               │
│                                          ▼               │
│  ┌──────────┐    ┌──────────┐    ┌──────────┐          │
│  │ Evaluate │◀───│ Monitor  │◀───│ Execute  │          │
│  │ Success  │    │ Deploy   │    │ Changes  │          │
│  └──────────┘    └──────────┘    └──────────┘          │
│                                                           │
└─────────────────────────────────────────────────────────┘
```

### Components

1. **Strategy Engine** (`strategy_engine.py`) - AI-powered strategy generation using Claude
2. **Database Handler** (`db_handler.py`) - Central data synchronization via Supabase
3. **Mobile Interface** (`mobile_interface.py`) - Telegram bot for approvals
4. **Execution Agent** (`execution_agent.py`) - Deploys changes safely
5. **Data Collector** (`data_collector.py`) - Gathers metrics and feedback
6. **Safety Net** (`safety_net.py`) - Validation and monitoring
7. **Loop Controller** (`loop_controller.py`) - Orchestrates the full cycle
8. **Main** (`main.py`) - Entry point and CLI

## Tech Stack

- **Language**: Python 3.10+
- **AI**: Anthropic Claude (via API)
- **Database**: Supabase (PostgreSQL)
- **Mobile**: Telegram Bot
- **Deployment**: Git + Vercel
- **Analytics**: Pandas, Stripe API
- **NLP**: Hugging Face Transformers
- **Testing**: pytest

## Quick Start

See [SETUP.md](SETUP.md) for detailed setup instructions.

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Configure environment
cp config.env.example config.env
# Edit config.env with your API keys

# 3. Setup database
# Run schema.sql in your Supabase SQL editor

# 4. Run a single cycle
python main.py run

# 5. Run continuous mode
python main.py continuous
```

## Usage

### CLI Commands

```bash
# Run continuous optimization loop (default: forever)
python main.py run

# Run for a specific duration
python main.py run --days 7
python main.py run --days 0.5        # 12 hours

# Alias for 'run'
python main.py continuous
python main.py continuous --days 30

# Run Telegram bot only (for testing connectivity)
python main.py bot

# Check system health (database, alerts, components)
python main.py health

# Show system statistics (pending strategies, latest metrics, alerts)
python main.py stats

# Export metrics to file
python main.py export                # JSON (default)
python main.py export --format csv

# Backfill dependency analysis for existing strategies
python main.py backfill-deps
```

### Telegram Bot Commands

| Command | Description |
|---------|-------------|
| `/start` | Initialize the bot and show welcome message |
| `/help` | Show all available commands and response options |
| `/status` | Check current system status |
| `/ping` | Test bot connectivity |
| `/saved` | View saved and pending strategies (tap ID to review) |
| `/failed` | View failed strategies (tap ID to re-save) |
| `/rejected` | View rejected strategies (tap ID to re-save) |
| `/approved` | View approved strategies (tap ID to execute) |
| `/restart` | Return to main menu or trigger a new optimization cycle |
| `/resume` | View and resume stuck strategies |

### Telegram Inline Buttons

When viewing a strategy:

| Button | Action |
|--------|--------|
| **Save** | Save strategy for manual implementation later |
| **Exec** | Approve and auto-implement (code generation + deploy to preview) |
| **Reject** | Discard the strategy |
| **Split into N strategies** | Split a multi-change strategy into individual single-change strategies |

When reviewing a preview deployment:

| Button | Action |
|--------|--------|
| **Merge** | Merge feature branch to develop |
| **Discard** | Delete the feature branch |
| **Defer** | Keep preview active for later review |

When a build fails:

| Button | Action |
|--------|--------|
| **Save** | Save strategy for later |
| **Fail** | Mark as failed |
| **Reject** | Reject the strategy |
| **Continue (Q&A)** | Enter interactive troubleshooting session |
| **Commit & Save** | Commit partial code and save strategy |

During interactive troubleshooting (Q&A):

| Button | Action |
|--------|--------|
| **Try Fix** | Generate a fix based on the conversation |
| **Give Up** | Skip the error and move on |

For stuck strategies (`/resume`):

| Button | Action |
|--------|--------|
| **Resume** | Continue from where it left off (uses cached code) |
| **Regenerate** | Delete code cache and start fresh |

### Text Responses

When a strategy is pending, you can also reply with free text to refine it. The system will generate a revised strategy incorporating your feedback.

### Optimization Cycle Flow

1. **Data Collection**: Gathers current metrics, user feedback, and system state
2. **Strategy Generation**: Claude analyzes data and proposes 1-3 optimization strategies
3. **Approval Request**: Sends proposal to you via Telegram with inline buttons
4. **Validation**: Safety checks ensure changes are within acceptable risk thresholds
5. **Execution**: Applies changes, commits to git, and deploys to Vercel
6. **Monitoring**: Watches for anomalies for 1 hour post-deployment
7. **Post-Metrics Collection**: Waits 6 hours then collects updated metrics
8. **Evaluation**: Compares before/after metrics to assess success

### Mobile Approval

When a strategy is proposed, you'll receive a Telegram message with:

- **Summary**: Brief description of the strategy
- **Expected Impact**: Projected metric improvements
- **Changes**: List of specific modifications
- **Dependency Analysis**: How changes relate to each other and whether they can be split
- **Buttons**: Save, Exec, Reject, and Split (for multi-change strategies)

You can reply with free text to refine the strategy (up to 3 refinements per cycle).

## Configuration

Key environment variables (see `config.env.example`):

```bash
# Required
SUPABASE_URL=your-supabase-url
SUPABASE_KEY=your-supabase-key
ANTHROPIC_API_KEY=your-claude-api-key
TELEGRAM_BOT_TOKEN=your-telegram-token
TELEGRAM_CHAT_ID=your-chat-id

# Optional
STRIPE_API_KEY=your-stripe-key
VERCEL_TOKEN=your-vercel-token
GENTUBE_APP_PATH=../gentube-app
CYCLE_INTERVAL_HOURS=12
MAX_METRIC_DROP_PERCENT=20
```

## Safety Features

### Pre-Execution Validation

- Risk level assessment
- Pricing change limits
- File conflict detection
- Test plan requirements

### Post-Deployment Monitoring

- Automatic anomaly detection
- Critical metric tracking (revenue, DAU, success rate)
- Rollback triggers on significant drops
- SMS/Telegram alerts for critical issues

### Thresholds

- Maximum allowed metric drop: 20% (configurable)
- Approval timeout: 24 hours (configurable)
- Monitoring duration: 1-6 hours
- Post-deployment data collection: 6 hours

## Metrics Tracked

### Primary KPIs

- **DAU**: Daily Active Users
- **MAU**: Monthly Active Users
- **ARPU**: Average Revenue Per User
- **Revenue**: Total revenue (via Stripe)
- **Churn Rate**: User attrition rate
- **MRR**: Monthly Recurring Revenue

### Product Metrics

- **Generation Success Rate**: % of successful AI generations
- **Credit Usage**: Total credits consumed
- **Average Session Iterations**: Avg generations per session
- **Payment Success Rate**: % of successful charges

### User Feedback

- Sentiment analysis (positive/negative/neutral)
- Categorization (quality, performance, UI/UX, pricing, features, bugs)
- Ratings (1-5 stars)

## Development

### Running Tests

```bash
# Run all tests
pytest

# Run specific test file
pytest tests/test_strategy_engine.py

# Run with coverage
pytest --cov=. --cov-report=html
```

### Project Structure

```
optimization-machine/
├── config.env              # Environment configuration (not committed)
├── config.env.example      # Template for config.env
├── requirements.txt        # Python dependencies
├── schema.sql             # Database schema
├── main.py                # Entry point and CLI
├── strategy_engine.py     # AI strategy generation + dependency analysis
├── db_handler.py          # Database operations (Supabase)
├── mobile_interface.py    # Telegram bot for CEO interaction
├── execution_agent.py     # Code generation + build + deploy
├── data_collector.py      # Metrics collection (Stripe, GA4, CrUX)
├── loop_controller.py     # Cycle orchestration (7-stage pipeline)
├── safety_net.py          # Validation & monitoring
├── troubleshooter.py      # Interactive Q&A troubleshooting for build failures
├── code_cache.py          # Code generation cache for resume capability
├── credentials/           # Service account keys (not committed)
├── logs/                  # Log files
└── tests/                 # Unit tests
    ├── test_strategy_engine.py
    ├── test_safety_net.py
    └── test_data_collector.py
```

## Best Practices

1. **Start Small**: Begin with low-risk UI tweaks and prompt optimizations
2. **Monitor Closely**: Watch the first few cycles carefully
3. **Validate Data**: Ensure metrics are accurate before trusting evaluations
4. **Gradual Rollout**: Test changes with a subset of users if possible
5. **Document Learnings**: Review cycle logs to understand what works
6. **Iterate Quickly**: Aim for 5-10% improvements that compound over time

## Troubleshooting

### Common Issues

**Issue**: Telegram bot not receiving messages

- Check `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`
- Run `python main.py bot` to test bot connection
- Use `/start` command to verify bot is working

**Issue**: Database connection failed

- Verify `SUPABASE_URL` and `SUPABASE_KEY`
- Check Supabase project is active
- Ensure schema.sql has been run

**Issue**: Strategy generation failed

- Verify `ANTHROPIC_API_KEY` is valid
- Check API quotas and limits
- Review logs for specific error messages

**Issue**: Metrics collection failed

- Verify `STRIPE_API_KEY` if using Stripe
- Check Gentube app API endpoint configuration
- Ensure data collection wait time is appropriate

## Roadmap

- [ ] Multi-strategy A/B testing
- [ ] Advanced trend analysis
- [ ] Automated rollback on anomalies
- [ ] Integration with CI/CD pipelines
- [ ] Web dashboard for cycle monitoring
- [ ] Slack/Discord integration options
- [ ] Machine learning for strategy prioritization

## Contributing

This is a private tool for Gentube.ai optimization. For issues or suggestions, contact the development team.

## License

Proprietary - Internal use only

## Support

For questions or issues:

- Check logs in `logs/` directory
- Run `python main.py health` for system status
- Run `python main.py stats` for current metrics
- Review [SETUP.md](SETUP.md) for configuration help
