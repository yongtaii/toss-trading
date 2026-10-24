from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", hide_input_in_errors=True)

    toss_client_id: SecretStr | None = None
    toss_client_secret: SecretStr | None = None
    toss_account_seq: int | None = Field(default=None, gt=0)
    operations_api_key: SecretStr | None = Field(default=None, min_length=32)
    dashboard_username: str = "admin"
    dashboard_password: SecretStr | None = Field(default=None, min_length=16)
    paper_database_url: SecretStr = SecretStr("sqlite:///data/paper.db")
    slack_webhook_url: SecretStr | None = None
    strategy_config_path: str = "config/strategy.toml"
    # Optional second PAPER strategy fed the same market data; needs its own ledger.
    compare_config_path: str | None = None
    compare_database_url: SecretStr | None = None
    live_trading_enabled: bool = False

    @field_validator(
        "toss_account_seq",
        "operations_api_key",
        "dashboard_password",
        "slack_webhook_url",
        "compare_config_path",
        "compare_database_url",
        mode="before",
    )
    @classmethod
    def empty_optional_value(cls, value):
        return None if value == "" else value

    @model_validator(mode="after")
    def prohibit_live_trading(self):
        if self.live_trading_enabled:
            raise ValueError("LIVE is disabled until broker execution is independently certified")
        if (self.compare_config_path is None) != (self.compare_database_url is None):
            raise ValueError("Set both COMPARE_CONFIG_PATH and COMPARE_DATABASE_URL, or neither")
        if (
            self.compare_database_url is not None
            and self.compare_database_url.get_secret_value()
            == self.paper_database_url.get_secret_value()
        ):
            raise ValueError("Comparison strategy needs a separate PAPER ledger")
        return self
