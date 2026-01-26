-- Gentube Optimization Machine Database Schema
-- Run this in your Supabase SQL editor

-- Strategies Table
CREATE TABLE IF NOT EXISTS strategies (
    id BIGSERIAL PRIMARY KEY,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    summary TEXT NOT NULL,
    changes JSONB NOT NULL,
    expected_impact TEXT,
    status VARCHAR(50) DEFAULT 'pending',
    priority INTEGER DEFAULT 0,
    focus_area VARCHAR(100),
    data_snapshot JSONB,
    CONSTRAINT valid_status CHECK (status IN ('pending', 'approved', 'rejected', 'executed', 'analyzing', 'completed', 'failed'))
);

-- Approvals Table
CREATE TABLE IF NOT EXISTS approvals (
    id BIGSERIAL PRIMARY KEY,
    strategy_id BIGINT REFERENCES strategies(id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    user_response TEXT NOT NULL,
    response_type VARCHAR(50),
    notes TEXT,
    approved_at TIMESTAMPTZ,
    CONSTRAINT valid_response_type CHECK (response_type IN ('approve', 'reject', 'tweak'))
);

-- Metrics Table
CREATE TABLE IF NOT EXISTS metrics (
    id BIGSERIAL PRIMARY KEY,
    timestamp TIMESTAMPTZ DEFAULT NOW(),
    strategy_id BIGINT REFERENCES strategies(id) ON DELETE SET NULL,
    cycle_id VARCHAR(100),
    kpi_data JSONB NOT NULL,
    dau INTEGER,
    mau INTEGER,
    arpu DECIMAL(10, 2),
    revenue DECIMAL(10, 2),
    churn_rate DECIMAL(5, 2),
    credit_usage INTEGER,
    gen_success_rate DECIMAL(5, 2),
    avg_session_iterations DECIMAL(5, 2),
    period_start TIMESTAMPTZ,
    period_end TIMESTAMPTZ
);

-- Feedback Table
CREATE TABLE IF NOT EXISTS feedback (
    id BIGSERIAL PRIMARY KEY,
    user_id VARCHAR(255),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    text TEXT NOT NULL,
    rating INTEGER,
    sentiment VARCHAR(50),
    category VARCHAR(100),
    metadata JSONB,
    CONSTRAINT valid_rating CHECK (rating >= 1 AND rating <= 5)
);

-- Deployments Table
CREATE TABLE IF NOT EXISTS deployments (
    id BIGSERIAL PRIMARY KEY,
    strategy_id BIGINT REFERENCES strategies(id) ON DELETE CASCADE,
    deployed_at TIMESTAMPTZ DEFAULT NOW(),
    status VARCHAR(50) DEFAULT 'deploying',
    git_commit_hash VARCHAR(255),
    vercel_deployment_url TEXT,
    changes_applied JSONB,
    rollback_info JSONB,
    CONSTRAINT valid_deployment_status CHECK (status IN ('deploying', 'deployed', 'failed', 'rolled_back'))
);

-- Cycle Logs Table
CREATE TABLE IF NOT EXISTS cycle_logs (
    id BIGSERIAL PRIMARY KEY,
    cycle_id VARCHAR(100) UNIQUE NOT NULL,
    started_at TIMESTAMPTZ DEFAULT NOW(),
    completed_at TIMESTAMPTZ,
    status VARCHAR(50) DEFAULT 'running',
    strategy_id BIGINT REFERENCES strategies(id) ON DELETE SET NULL,
    metrics_before JSONB,
    metrics_after JSONB,
    success_indicators JSONB,
    errors TEXT,
    CONSTRAINT valid_cycle_status CHECK (status IN ('running', 'completed', 'failed', 'paused'))
);

-- Alerts Table
CREATE TABLE IF NOT EXISTS alerts (
    id BIGSERIAL PRIMARY KEY,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    alert_type VARCHAR(50) NOT NULL,
    severity VARCHAR(20) DEFAULT 'medium',
    message TEXT NOT NULL,
    cycle_id VARCHAR(100),
    strategy_id BIGINT REFERENCES strategies(id) ON DELETE SET NULL,
    acknowledged BOOLEAN DEFAULT FALSE,
    acknowledged_at TIMESTAMPTZ,
    CONSTRAINT valid_severity CHECK (severity IN ('low', 'medium', 'high', 'critical'))
);

-- Create indexes for better performance
CREATE INDEX IF NOT EXISTS idx_strategies_status ON strategies(status);
CREATE INDEX IF NOT EXISTS idx_strategies_created_at ON strategies(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_metrics_timestamp ON metrics(timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_metrics_strategy_id ON metrics(strategy_id);
CREATE INDEX IF NOT EXISTS idx_feedback_created_at ON feedback(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_cycle_logs_started_at ON cycle_logs(started_at DESC);
CREATE INDEX IF NOT EXISTS idx_alerts_created_at ON alerts(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_alerts_acknowledged ON alerts(acknowledged);

-- Create updated_at trigger function
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ language 'plpgsql';

-- Apply trigger to strategies table
CREATE TRIGGER update_strategies_updated_at BEFORE UPDATE ON strategies
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

-- Row Level Security (Optional - enable if needed)
-- ALTER TABLE strategies ENABLE ROW LEVEL SECURITY;
-- ALTER TABLE approvals ENABLE ROW LEVEL SECURITY;
-- ALTER TABLE metrics ENABLE ROW LEVEL SECURITY;
-- ALTER TABLE feedback ENABLE ROW LEVEL SECURITY;
-- ALTER TABLE deployments ENABLE ROW LEVEL SECURITY;
-- ALTER TABLE cycle_logs ENABLE ROW LEVEL SECURITY;
-- ALTER TABLE alerts ENABLE ROW LEVEL SECURITY;
