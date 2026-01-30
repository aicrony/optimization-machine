# Setup Guide

Complete setup instructions for the Gentube.ai Optimization Machine.

## Prerequisites

- Python 3.10 or higher
- Git
- Supabase account (free tier works)
- Anthropic API key (Claude)
- Telegram account
- Stripe account (for revenue tracking)
- Vercel account (optional, for deployment)

## Step-by-Step Setup

### 1. Clone and Install

```bash
# Clone the repository (or copy files)
cd /path/to/optimization-machine

# Create virtual environment (recommended)
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt
```

### 2. Database Setup (Supabase)

#### 2.1 Create Supabase Project

1. Go to [supabase.com](https://supabase.com)
2. Create a new account or sign in
3. Click "New Project"
4. Fill in project details:
   - Name: `OM-v1`
   - Database Password: (save this securely)
   - Region: Choose closest to you
5. Wait for project to be ready (~2 minutes)

#### 2.2 Run Database Schema

1. In your Supabase project, go to **SQL Editor**
2. Click "New Query"
3. Copy the entire contents of `schema.sql`
4. Paste into the query editor
5. Click "Run" to execute
6. Verify tables were created in **Table Editor**

You should see these tables:

- strategies
- approvals
- metrics
- feedback
- deployments
- cycle_logs
- alerts

#### 2.3 Get API Credentials

1. Go to **Project Settings** → **Data API**
2. Copy:
   - **Project URL** (e.g., `https://xxxxx.supabase.co`)
3. Go to **Project Settings** → **API Keys**
   - **Secret Keys** (starts with `sb_secret_...`)
4. Save these for the next step

### 3. Anthropic API Setup

#### 3.1 Get API Key

1. Go to [console.anthropic.com](https://console.anthropic.com)
2. Create account or sign in
3. Go to **API Keys**
4. Click "Create Key"
5. Name it "Gentube Optimizer"
6. Copy the key (starts with `sk-ant-...`)
7. **Important**: Save this key - it won't be shown again

#### 3.2 Add Credits (if needed)

1. Go to **Billing**
2. Add credits (minimum $5 recommended)
3. Usage cost: ~$0.01-0.05 per optimization cycle

### 4. Telegram Bot Setup

#### 4.1 Create Bot

1. Open Telegram
2. Search for `@BotFather`
3. Send `/newbot`
4. Follow prompts:
   - Bot name: `Gentube Optimizer`
   - Username: `gentube_optimizer_bot` (must end in 'bot')
5. Copy the bot token (looks like `1234567890:ABCdefGHIjklMNOpqrsTUVwxyz`)

#### 4.2 Get Your Chat ID

1. Search for your new bot in Telegram
2. Send `/start` to the bot
3. Open this URL in browser (replace `YOUR_BOT_TOKEN`):
   ```
   https://api.telegram.org/botYOUR_BOT_TOKEN/getUpdates
   ```
4. Look for `"chat":{"id":123456789}` in the response
5. Copy the chat ID number

**Alternative method using a helper bot:**

1. Search for `@userinfobot` in Telegram
2. Send any message to it
3. It will reply with your user ID

### 5. Stripe Setup (Optional but Recommended)

#### 5.1 Get API Key

1. Go to [dashboard.stripe.com](https://dashboard.stripe.com)
2. Sign in to your Gentube Stripe account
3. Go to **Developers** → **API keys**
4. Copy the "Secret key" (starts with `sk_test_` or `sk_live_`)

**Note**: Use test key for development, live key for production

### 6. Vercel Setup (Optional)

#### 6.1 Get Token

1. Go to [vercel.com](https://vercel.com)
2. Go to **Settings** → **Tokens**
3. Click "Create"
4. Name it "Gentube Optimizer"
5. Copy the token

#### 6.2 Get Project ID

1. Go to your Gentube project
2. Go to **Settings** → **General**
3. Copy the "Project ID"

### 7. Google Analytics 4 Setup (Recommended)

This enables the optimizer to analyze user behavior, traffic sources, and conversion funnels.

#### 7.1 Create Google Cloud Project

1. Go to [console.cloud.google.com](https://console.cloud.google.com)
2. Create a new project or select an existing one
3. Note your project ID

#### 7.2 Enable Google Analytics Data API

1. In Google Cloud Console, go to **APIs & Services** → **Library**
2. Search for "Google Analytics Data API"
3. Click **Enable**

#### 7.3 Create Service Account

1. Go to **APIs & Services** → **Credentials**
2. Click **Create Credentials** → **Service Account**
3. Name it "gentube-optimizer"
4. Click **Create and Continue**
5. Skip the optional roles (we'll add GA4 access separately)
6. Click **Done**

#### 7.4 Download JSON Key

1. Click on the service account you just created
2. Go to **Keys** tab
3. Click **Add Key** → **Create new key**
4. Select **JSON** format
5. Click **Create**
6. Save the downloaded file to `./credentials/ga4-service-account.json`

#### 7.5 Add Service Account to GA4

1. Go to [analytics.google.com](https://analytics.google.com)
2. Select your Gentube property
3. Go to **Admin** (gear icon) → **Property Access Management**
4. Click **+** → **Add users**
5. Enter the service account email (from the JSON file, looks like `gentube-optimizer@project-id.iam.gserviceaccount.com`)
6. Set role to **Viewer**
7. Click **Add**

#### 7.6 Get GA4 Property ID

1. In Google Analytics, go to **Admin** → **Property Settings**
2. Copy the **Property ID** (a number like `123456789`)
3. Format it as `properties/123456789` for the config

### 8. Chrome UX Report (CrUX) API Setup (Recommended)

This enables the optimizer to monitor Core Web Vitals (LCP, CLS, INP) using real user data.

#### 8.1 Enable CrUX API

1. Go to [console.cloud.google.com](https://console.cloud.google.com)
2. Select the same project used for GA4
3. Go to **APIs & Services** → **Library**
4. Search for "Chrome UX Report API"
5. Click **Enable**

#### 8.2 Create API Key

1. Go to **APIs & Services** → **Credentials**
2. Click **Create Credentials** → **API Key**
3. Under **API restrictions**, select **Restrict key**
4. Select only "Chrome UX Report API"
5. Click **Create**
6. Copy the generated API key

**Note**: CrUX data is only available for origins with sufficient traffic (typically 1000+ daily visitors). If your site is new or has low traffic, CrUX will return "insufficient traffic" errors, which is handled gracefully.

### 10. Configure Environment

Create your configuration file:

```bash
cp config.env.example config.env
```

Edit `config.env` with your credentials:

```bash
# Supabase Configuration
SUPABASE_URL=https://xxxxx.supabase.co
SUPABASE_KEY=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...

# Anthropic API (Claude)
ANTHROPIC_API_KEY=sk-ant-api03-xxxxx...

# Telegram Bot
TELEGRAM_BOT_TOKEN=1234567890:ABCdefGHIjklMNOpqrsTUVwxyz
TELEGRAM_CHAT_ID=123456789

# Stripe API (Optional)
STRIPE_API_KEY=sk_test_xxxxx...

# Vercel Deployment (Optional)
VERCEL_TOKEN=xxxxx...
VERCEL_PROJECT_ID=prj_xxxxx...

# Google Analytics 4 (Recommended)
GA4_CREDENTIALS_PATH=./credentials/ga4-service-account.json
GA4_PROPERTY_ID=properties/123456789

# Chrome UX Report API (Recommended)
CRUX_API_KEY=AIzaSy...your-api-key
GENTUBE_ORIGIN=https://gentube.ai

# Gentube App Configuration
GENTUBE_APP_URL=https://gentube.vercel.app
GENTUBE_API_ENDPOINT=https://gentube.vercel.app/api
GENTUBE_APP_PATH=../gentube-app  # Local path to your Gentube repo

# System Configuration
CYCLE_INTERVAL_HOURS=12
APPROVAL_TIMEOUT_HOURS=24
LOG_LEVEL=INFO
ENVIRONMENT=development

# Safety Net Configuration
MAX_METRIC_DROP_PERCENT=20
MIN_APPROVAL_WAIT_MINUTES=5
ROLLBACK_ENABLED=true
```

### 11. Verify Setup

Run the health check to verify all components are working:

```bash
python main.py health
```

You should see output like:

```
============================================================
System Health Check
============================================================

Overall Status: HEALTHY
Timestamp: 2026-01-27T05:50:58.646790

Component Status:
  • database: healthy
  • alerts: healthy
    Unacknowledged Alerts: 0
```

If you see "Overall Status: HEALTHY", your setup is complete.

**Troubleshooting**: If any component shows as unhealthy, check:
- Database issues: Verify `SUPABASE_URL` and `SUPABASE_KEY` in config.env
- API issues: Verify `ANTHROPIC_API_KEY` is valid
- Analytics issues: Check GA4 credentials path and property ID (optional)
- CrUX issues: May show "insufficient_traffic" for new sites (this is normal)

### 12. Initial Test Run

Run your first optimization cycle in test mode:

```bash
python main.py run
```

This will:

1. Collect current metrics (may be empty initially)
2. Generate a test strategy
3. Send it to Telegram for approval
4. Wait for your response
5. Execute if approved

### 13. Production Deployment

#### 13.1 Set Production Environment

Update `config.env`:

```bash
ENVIRONMENT=production
LOG_LEVEL=WARNING
```

#### 13.2 Run as Service (Linux/Mac)

Create a systemd service file `/etc/systemd/system/OM-v1.service`:

```ini
[Unit]
Description=Gentube Optimization Machine
After=network.target

[Service]
Type=simple
User=your-username
WorkingDirectory=/path/to/optimization-machine
Environment="PATH=/path/to/venv/bin"
ExecStart=/path/to/venv/bin/python main.py continuous
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Enable and start:

```bash
sudo systemctl enable OM-v1
sudo systemctl start OM-v1
sudo systemctl status OM-v1
```

View logs:

```bash
sudo journalctl -u OM-v1 -f
```

#### 13.3 Run with nohup (Alternative)

```bash
nohup python main.py continuous > logs/output.log 2>&1 &
```

Stop with:

```bash
pkill -f "main.py continuous"
```

#### 13.4 Run with Docker (Alternative)

Create `Dockerfile`:

```dockerfile
FROM python:3.10-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "main.py", "continuous"]
```

Build and run:

```bash
docker build -t OM-v1 .
docker run -d --name optimizer --env-file config.env OM-v1
```

### 14. Monitoring and Maintenance

#### View Logs

```bash
# Real-time logs
tail -f logs/main.log
tail -f logs/loop_controller.log

# Search for errors
grep ERROR logs/*.log

# Check specific cycle
grep "cycle_20240115" logs/*.log
```

#### Check System Status

```bash
# Health check
python main.py health

# Statistics
python main.py stats

# Export metrics
python main.py export --format json
```

#### Database Monitoring

1. Go to Supabase Dashboard
2. Check **Table Editor** for recent entries
3. Review **Database** → **Logs** for errors
4. Monitor **Database** → **Usage** for quota

### 15. Troubleshooting

#### Issue: "Module not found" error

```bash
# Ensure virtual environment is activated
source venv/bin/activate  # or venv\Scripts\activate on Windows

# Reinstall dependencies
pip install -r requirements.txt
```

#### Issue: Telegram bot not responding

```bash
# Test bot token
curl https://api.telegram.org/bot<YOUR_BOT_TOKEN>/getMe

# Should return bot info. If error, regenerate token from @BotFather
```

#### Issue: Database connection timeout

- Check Supabase project is active (not paused)
- Verify URL and key are correct
- Check firewall/network settings
- Try from Supabase SQL editor to verify credentials

#### Issue: Claude API rate limit

- Check [console.anthropic.com](https://console.anthropic.com) for quotas
- Add more credits if needed
- Reduce cycle frequency in `config.env`

#### Issue: Changes not deploying

- Verify `GENTUBE_APP_PATH` points to correct directory
- Ensure git is configured properly
- Check Vercel credentials
- Review execution logs

### 16. Security Best Practices

1. **Never commit `config.env`** - it's in `.gitignore`
2. **Use environment variables** in production, not files
3. **Rotate API keys** regularly
4. **Set up Supabase Row Level Security** (RLS) policies
5. **Use Vercel's environment variables** for deployment secrets
6. **Backup database** regularly:
   ```bash
   # In Supabase Dashboard → Database → Backups
   ```
7. **Monitor for unauthorized access** in Supabase logs
8. **Use Telegram bot in private chat** only

### 17. Advanced Configuration

#### Custom Metrics Collection

Edit `data_collector.py` to add custom metrics sources:

```python
def _collect_custom_metrics(self):
    # Add your custom metric collection here
    return {'custom_metric': value}
```

#### Adjust Strategy Prompts

Edit `strategy_engine.py` to customize strategy generation:

```python
def _prepare_context(self, data: Dict[str, Any]) -> str:
    # Customize the prompt sent to Claude
    context = f"""...your custom prompt..."""
    return context
```

#### Add Custom Validation Rules

Edit `safety_net.py` to add custom validation:

```python
def _validate_custom_rule(self, change: Dict) -> bool:
    # Add your custom validation logic
    return True  # or False if invalid
```

## Next Steps

1. Run initial test cycle
2. Review first strategy proposal
3. Approve and monitor results
4. Adjust configuration based on learnings
5. Enable continuous mode for production

## Support

If you encounter issues not covered here:

1. Check logs in `logs/` directory
2. Run `python main.py health`
3. Review Supabase logs
4. Check API quotas
5. Contact development team

## Updates

To update the system:

```bash
git pull
pip install -r requirements.txt --upgrade
# Review CHANGELOG for breaking changes
python main.py health  # Verify still working
```

## Backup and Recovery

### Backup

```bash
# Backup configuration
cp config.env config.env.backup

# Export metrics
python main.py export --format json

# Backup database (in Supabase Dashboard)
# Settings → Database → Backups → Create backup
```

### Recovery

```bash
# Restore configuration
cp config.env.backup config.env

# Import metrics
# (Use Supabase SQL editor or API)

# Restore database
# (Use Supabase backup restore feature)
```

---

## Quick Setup Checklist

- [ ] Python 3.10+ installed
- [ ] Virtual environment created
- [ ] Dependencies installed
- [ ] Supabase project created
- [ ] Database schema applied
- [ ] Supabase URL and key obtained
- [ ] Anthropic API key obtained
- [ ] Telegram bot created
- [ ] Telegram bot token obtained
- [ ] Telegram chat ID obtained
- [ ] Stripe API key obtained (optional)
- [ ] Vercel token obtained (optional)
- [ ] Google Analytics 4 setup (recommended):
  - [ ] Google Cloud project created
  - [ ] Google Analytics Data API enabled
  - [ ] Service account created
  - [ ] JSON key downloaded to `./credentials/`
  - [ ] Service account added to GA4 property
  - [ ] GA4 Property ID obtained
- [ ] Chrome UX Report API setup (recommended):
  - [ ] CrUX API enabled in Google Cloud
  - [ ] API key created
- [ ] `config.env` configured
- [ ] Health check passed (`python main.py health`)
- [ ] Test cycle completed (`python main.py run`)

**You're ready to optimize! 🚀**
