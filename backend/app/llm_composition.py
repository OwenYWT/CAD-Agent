"""Application composition: inject persistent observation into the Provider port."""
def create_llm_client(settings):
    from app.llm import create_llm_client as build_adapter
    from app.services import llm_usage
    return build_adapter(settings, sink=llm_usage)
