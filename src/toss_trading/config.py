from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", hide_input_in_errors=True)

    toss_client_id: SecretStr | None = None
    toss_client_secret: SecretStr | None = None
    toss_account_seq: int | None = Field(default=None, gt=0)
    operations_api_key: SecretStr | None = Field(default=None, min_length=32)
    paper_database_url: SecretStr = SecretStr("sqlite:///data/paper.db")
    slack_webhook_url: SecretStr | None = None
    strategy_config_path: str = "config/strategy.toml"
    live_trading_enabled: bool = False

    @field_validator("toss_account_seq", "operations_api_key", "slack_webhook_url", mode="before")
    @classmethod
    def empty_optional_value(cls, value):
        return None if value == "" else value

    @model_validator(mode="after")
    def prohibit_live_trading(self):
        if self.live_trading_enabled:
            raise ValueError("LIVE is disabled until broker execution is independently certified")
        return self
