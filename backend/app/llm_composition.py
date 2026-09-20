"""Wire the provider observation port to the existing persistent usage writer."""
from app.contracts.usage import UsageSink


def usage_sink() -> UsageSink:
    from app.services import llm_usage
    return llm_usage
