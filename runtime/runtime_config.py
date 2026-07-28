class RuntimeConfig:
    selected_agent: str = "openclaw"
    safety_level: str = "guardian"
    autonomy_level: str | None = None
    autonomy_scope: str = "domain_scoped"
