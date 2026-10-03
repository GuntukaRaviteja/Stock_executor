import json
from decimal import Decimal
from typing import List, Optional, Union
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

#: ISO-4217 code a portfolio is denominated in when a caller does not state one. Every
#: monetary figure of a portfolio is in its own currency; the engine never converts between
#: currencies, so this constant states a unit rather than enabling conversion.
#:
#: This is the built-in default and the value the migration backfills. The *configured*
#: default is ``Settings.PAPER_TRADING_DEFAULT_CURRENCY``, resolved at call time by
#: :func:`default_currency` — reading this constant instead would silently ignore the setting,
#: because a module constant is bound at import time.
DEFAULT_CURRENCY = "INR"


def default_currency() -> str:
    """The configured default currency, normalised to three uppercase letters.

    Read from settings on every call rather than captured at import, so setting
    ``PAPER_TRADING_DEFAULT_CURRENCY`` in the environment actually changes what a new
    portfolio is denominated in.
    """
    return str(settings.PAPER_TRADING_DEFAULT_CURRENCY).strip().upper()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    APP_NAME: str = "Stock Intelligence API"
    APP_ENV: str = "development"
    DEBUG: bool = True
    API_V1_STR: str = "/api/v1"
    HOST: str = "0.0.0.0"
    PORT: int = 8000
    LOG_LEVEL: str = "INFO"

    # Database Settings
    DATABASE_URL: str = "mysql+pymysql://root:@127.0.0.1:3306/stock_ml_db"
    DB_POOL_SIZE: int = 10
    DB_MAX_OVERFLOW: int = 20
    DB_POOL_RECYCLE: int = 3600
    DB_POOL_PRE_PING: bool = True

    # Market Data Provider
    MARKET_DATA_PROVIDER: str = "mock"
    MARKET_DATA_API_KEY: Optional[str] = None
    MARKET_DATA_TIMEOUT_SECONDS: float = 10.0
    DEFAULT_MARKET: str = "INDIA"
    DEFAULT_EXCHANGE: str = "NSE"
    MAX_HISTORY_DAYS: int = 3650  # ~10 years

    # News / Event Data Provider
    NEWS_DATA_PROVIDER: str = "mock"
    NEWS_DATA_API_KEY: Optional[str] = None
    NEWS_DATA_TIMEOUT_SECONDS: float = 10.0

    # Phase 9 / 10 — Paper Trading Defaults (SIMULATION ONLY)
    # These are defaults for virtual portfolios. They never reach a broker: the paper
    # trading engine executes simulated market orders locally against stored prices.
    PAPER_TRADING_COMMISSION_RATE: Decimal = Decimal("0.001")
    PAPER_TRADING_MIN_FEE: Decimal = Decimal("0")
    PAPER_TRADING_SLIPPAGE_RATE: Decimal = Decimal("0")
    PAPER_TRADING_POSITION_SIZE_PCT: Decimal = Decimal("10")
    PAPER_TRADING_MAX_POSITION_QUANTITY: Optional[int] = None
    PAPER_TRADING_MAX_POSITION_VALUE: Optional[Decimal] = None
    PAPER_TRADING_MAX_POSITION_ALLOCATION_PCT: Optional[Decimal] = None
    PAPER_TRADING_MAX_DRAWDOWN_PCT: Optional[Decimal] = None
    # Phase 10 risk rules; leave empty to disable the corresponding rule
    #: Maximum share of portfolio equity that may be invested across all positions
    PAPER_TRADING_MAX_EXPOSURE_PCT: Optional[Decimal] = None
    #: Maximum share of portfolio equity a single trade may commit
    PAPER_TRADING_MAX_TRADE_ALLOCATION_PCT: Optional[Decimal] = None
    #: Share of equity that must stay in cash after a buy
    PAPER_TRADING_MIN_CASH_RESERVE_PCT: Optional[Decimal] = None
    #: Maximum number of simultaneously open positions
    PAPER_TRADING_MAX_POSITIONS: Optional[int] = None
    # Phase 12 risk rules; leave empty to disable the corresponding rule
    #: Loss on a single UTC day, in percent of that day's opening equity, above which new BUY
    #: orders are blocked. Disabled by default: "today" only means something once a portfolio
    #: records snapshots, so a deployment opts in deliberately.
    PAPER_TRADING_MAX_DAILY_LOSS_PCT: Optional[Decimal] = None
    #: Smallest gross value a simulated order may commit, in the portfolio's currency.
    #: Blocks dust orders, which pay a fee for no meaningful exposure.
    PAPER_TRADING_MIN_TRADE_VALUE: Optional[Decimal] = None
    #: ISO-4217 currency a portfolio is denominated in. Every monetary figure of a portfolio is
    #: in this currency; no cross-currency conversion is performed anywhere in the engine.
    PAPER_TRADING_DEFAULT_CURRENCY: str = DEFAULT_CURRENCY
    #: Which price of which bar a simulated fill is taken from: OPEN (Phase 8 convention,
    #: fills on the bar after the signal) or CLOSE (fills on the signal bar itself).
    PAPER_TRADING_EXECUTION_PRICE_POLICY: str = "OPEN"
    #: Annual risk-free rate used for the portfolio Sharpe ratio, in percent
    PAPER_TRADING_RISK_FREE_RATE_PCT: Decimal = Decimal("0")

    # Phase 11 — Portfolio Construction & Risk Management Defaults (SIMULATION ONLY)
    # These are the defaults a new portfolio's allocation configuration starts from. They are
    # centralised here for the same reason as the Phase 9/10 settings above: no strategy or
    # portfolio rule is hardcoded anywhere in the engine.
    #: Allocation formula a new portfolio uses to size candidates.
    PORTFOLIO_ALLOCATION_SIZING_METHOD: str = "EQUAL_WEIGHT"
    #: Maximum share of portfolio equity a single position may represent.
    PORTFOLIO_MAX_POSITION_WEIGHT_PCT: Decimal = Decimal("20")
    #: Maximum share of portfolio equity one sector may represent; unset disables the rule.
    PORTFOLIO_MAX_SECTOR_WEIGHT_PCT: Optional[Decimal] = None
    #: Maximum number of simultaneously open positions; unset disables the rule.
    PORTFOLIO_MAX_POSITIONS: Optional[int] = None
    #: Maximum share of portfolio equity that may be invested; unset disables the rule.
    PORTFOLIO_MAX_EXPOSURE_PCT: Optional[Decimal] = None
    #: Maximum tolerated loss on a single position, in percent of its entry price.
    PORTFOLIO_MAX_POSITION_LOSS_PCT: Optional[Decimal] = None
    #: Stop-loss trigger for a position, in percent below its average entry price.
    PORTFOLIO_STOP_LOSS_PCT: Optional[Decimal] = None
    #: Take-profit trigger for a position, in percent above its average entry price.
    PORTFOLIO_TAKE_PROFIT_PCT: Optional[Decimal] = None
    #: Weight drift that must be reached before a rebalancing trade is proposed.
    PORTFOLIO_REBALANCE_THRESHOLD_PCT: Decimal = Decimal("0.5")
    #: Smallest position value the construction engine will open.
    PORTFOLIO_MIN_POSITION_VALUE: Optional[Decimal] = None
    #: Share of equity risked on a single trade under risk-based sizing, in percent.
    PORTFOLIO_RISK_PER_TRADE_PCT: Decimal = Decimal("1")
    #: Share of equity that must remain in cash.
    PORTFOLIO_CASH_RESERVE_PCT: Decimal = Decimal("10")
    # Phase 13 — unified portfolio & risk management engine. The limits below are read as one
    #: policy by ``PortfolioRiskPolicy``; they are not a second, competing set of numbers, and
    #: every one of them can be overridden per request and per portfolio.
    #: Maximum tolerated decline from peak equity, in percent. Blocks new buys once breached.
    PORTFOLIO_MAX_DRAWDOWN_PCT: Optional[Decimal] = None
    #: Maximum share of equity that may be at risk to the configured stops across every open
    #: position at once. Unset disables the portfolio-level total-risk guard.
    PORTFOLIO_MAX_TOTAL_RISK_PCT: Optional[Decimal] = None
    #: Smallest position value the sizing engine will open, in the portfolio's currency. Guards
    #: against a floored quantity producing a trade too small to be worth its own fees.
    PORTFOLIO_MIN_TRADE_VALUE: Optional[Decimal] = None

    # Phase 15 — portfolio-level risk limits, stress scenarios and risk budgets. These are
    # **defaults for a new policy**, not universal investment limits: they are the shipped
    # starting point of this engine, every one of them is overridable per request, and a
    # deployment is expected to set them from its own mandate. Unset (``None``) disables a rule
    # entirely, which is deliberately different from setting it to zero.
    #
    # Concentration
    #: Maximum share of equity one position may represent.
    PORTFOLIO_RISK_MAX_SINGLE_ASSET_WEIGHT: Decimal = Decimal("20")
    #: Maximum share of equity one sector may represent.
    PORTFOLIO_RISK_MAX_SECTOR_WEIGHT: Optional[Decimal] = Decimal("35")
    #: Maximum share of equity one strategy may represent.
    PORTFOLIO_RISK_MAX_STRATEGY_WEIGHT: Optional[Decimal] = Decimal("50")
    #: Maximum share of equity one asset class may represent.
    PORTFOLIO_RISK_MAX_ASSET_CLASS_WEIGHT: Optional[Decimal] = None
    #: Maximum combined weight of the ``top_n_count`` largest positions.
    PORTFOLIO_RISK_MAX_TOP_N_CONCENTRATION: Optional[Decimal] = Decimal("60")
    #: How many largest positions the top-N rule looks at.
    PORTFOLIO_RISK_TOP_N_COUNT: int = 10

    # Portfolio risk metrics
    #: Maximum annualised portfolio volatility.
    PORTFOLIO_RISK_MAX_VOLATILITY: Optional[Decimal] = Decimal("30")
    #: Maximum portfolio beta versus the benchmark.
    PORTFOLIO_RISK_MAX_BETA: Optional[Decimal] = Decimal("1.50")
    #: Maximum historical peak-to-trough drawdown magnitude.
    PORTFOLIO_RISK_MAX_DRAWDOWN: Optional[Decimal] = Decimal("25")
    #: Maximum weighted average pairwise correlation across the book.
    PORTFOLIO_RISK_MAX_CORR_CONCENTRATION: Optional[Decimal] = Decimal("60")

    # Stress scenarios — magnitudes, expressed as positive percentages
    #: Broad-market decline used by the broad crash scenario.
    PORTFOLIO_RISK_MARKET_CRASH_SHOCK: Decimal = Decimal("20")
    #: Broad-market decline used by the severe crash scenario.
    PORTFOLIO_RISK_SEVERE_CRASH_SHOCK: Decimal = Decimal("30")
    #: Decline applied to the selected sector by the sector-shock scenario.
    PORTFOLIO_RISK_SECTOR_SHOCK: Decimal = Decimal("25")
    #: Base decline for the high-beta scenario before the beta tilt is applied.
    PORTFOLIO_RISK_HIGH_BETA_SHOCK: Decimal = Decimal("10")
    #: Extra decline applied per unit of beta above the market's.
    PORTFOLIO_RISK_HIGH_BETA_TILT: Decimal = Decimal("10")
    #: Decline applied to the largest strategy by the strategy-drawdown scenario.
    PORTFOLIO_RISK_STRATEGY_SHOCK: Decimal = Decimal("20")
    #: Extra haircut applied to illiquid positions by the liquidity scenario, in percent.
    PORTFOLIO_RISK_LIQUIDITY_HAIRCUT: Decimal = Decimal("5")
    #: Multiplier applied to each off-diagonal correlation by the correlation-spike scenario.
    PORTFOLIO_RISK_CORRELATION_SPIKE_MULTIPLIER: Decimal = Decimal("0.30")
    #: Multiplier applied to each off-diagonal volatility by the correlation-spike scenario.
    PORTFOLIO_RISK_CORRELATION_SPIKE_VOL_MULTIPLIER: Decimal = Decimal("1.20")
    #: Maximum estimated portfolio loss under any single scenario.
    PORTFOLIO_RISK_MAX_STRESS_LOSS: Optional[Decimal] = Decimal("20")

    # Liquidity
    #: Minimum portfolio liquidity score (0-100). Unset disables the rule.
    PORTFOLIO_RISK_MIN_LIQUIDITY_SCORE: Optional[Decimal] = None
    #: Maximum position value / average daily traded value before the position is flagged.
    PORTFOLIO_RISK_MAX_PARTICIPATION_RATIO: Optional[Decimal] = Decimal("0.20")

    # Position counts
    #: Minimum number of open positions for diversification. Unset disables the rule.
    PORTFOLIO_RISK_MIN_POSITION_COUNT: Optional[int] = None
    #: Maximum number of open positions. Unset disables the rule.
    PORTFOLIO_RISK_MAX_POSITION_COUNT: Optional[int] = None

    # Evaluation policy
    #: Utilization (percent of a limit) at which a metric is reported as WARNING.
    PORTFOLIO_RISK_WARNING_UTILIZATION: Decimal = Decimal("80")
    #: Periods per year used to annualise volatility from daily returns.
    PORTFOLIO_RISK_ANNUALIZATION_PERIODS: int = 252
    #: Minimum overlapping return observations before covariance/risk metrics are computed.
    PORTFOLIO_RISK_MIN_OBSERVATIONS: int = 30
    #: Minimum return observations before historical VaR / Expected Shortfall are computed.
    PORTFOLIO_RISK_MIN_VAR_OBSERVATIONS: int = 10
    #: Correlation at or above which two positions are treated as one risk cluster.
    PORTFOLIO_RISK_CORR_CLUSTER_THRESHOLD: Decimal = Decimal("0.75")
    #: Benchmark symbol used for beta when a request does not name one.
    PORTFOLIO_RISK_BENCHMARK_SYMBOL: Optional[str] = None

    # VaR / Expected Shortfall
    #: Confidence level for historical VaR and Expected Shortfall.
    PORTFOLIO_RISK_VAR_CONFIDENCE: Decimal = Decimal("0.95")
    #: Holding period of the VaR figure, in days.
    PORTFOLIO_RISK_VAR_HORIZON_DAYS: int = 1

    # Risk budgets — optional consumption targets, unset disables the budget
    PORTFOLIO_RISK_BUDGET_ASSET_CONC: Optional[Decimal] = None
    PORTFOLIO_RISK_BUDGET_SECTOR_CONC: Optional[Decimal] = None
    PORTFOLIO_RISK_BUDGET_STRATEGY_CONC: Optional[Decimal] = None
    PORTFOLIO_RISK_BUDGET_VOLATILITY: Optional[Decimal] = None
    PORTFOLIO_RISK_BUDGET_BETA: Optional[Decimal] = None
    PORTFOLIO_RISK_BUDGET_DRAWDOWN: Optional[Decimal] = None
    PORTFOLIO_RISK_BUDGET_VAR: Optional[Decimal] = None
    PORTFOLIO_RISK_BUDGET_STRESS: Optional[Decimal] = None

    # Mandatory limits: a breach that survives adjustment rejects the portfolio. Any limit not
    # listed is adjustable, and a breach of it is corrected deterministically.
    PORTFOLIO_RISK_MANDATORY_LIMITS: str = (
        "max_single_asset_weight,max_sector_weight,max_strategy_weight,"
        "max_portfolio_volatility,max_drawdown,max_stress_loss"
    )

    # Adjustment and stress behaviour
    #: Largest number of reduction passes the deterministic adjustment loop may take.
    PORTFOLIO_RISK_MAX_ADJUSTMENT_PASSES: int = 12
    #: Set false to report breaches without proposing any reduction.
    PORTFOLIO_RISK_ENABLE_ADJUSTMENT: bool = True
    #: Set true to skip the stress catalogue entirely.
    PORTFOLIO_RISK_DISABLE_STRESS_TESTS: bool = False

    # Security & CORS
    CORS_ORIGINS: Union[List[str], str] = ["*"]

    @field_validator("CORS_ORIGINS", mode="before")
    @classmethod
    def assemble_cors_origins(cls, v: Union[str, List[str]]) -> List[str]:
        if isinstance(v, str):
            if v.strip().startswith("["):
                try:
                    return json.loads(v)
                except Exception:
                    pass
            return [i.strip() for i in v.split(",") if i.strip()]
        return v


settings = Settings()
