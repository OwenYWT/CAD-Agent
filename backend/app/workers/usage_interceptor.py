"""Bind each Temporal activity's authenticated submitter, with async isolation."""
from uuid import UUID

from temporalio import activity
from temporalio.worker import ActivityInboundInterceptor, Interceptor

from app.services.llm_usage import UsageContext, usage_context


class UsageActivityInterceptor(ActivityInboundInterceptor):
    async def execute_activity(self, input):
        payload = input.args[0] if input.args and isinstance(input.args[0], dict) else {}
        context = None
        if payload.get("tenant_id") and payload.get("principal_id"):
            info = activity.info()
            context = UsageContext(
                UUID(str(payload["tenant_id"])), UUID(str(payload["principal_id"])),
                UUID(str(payload["workflow_run_id"])) if payload.get("workflow_run_id") else None,
                info.activity_type, info.attempt,
            )
        token = usage_context.set(context)
        try:
            return await super().execute_activity(input)
        finally:
            usage_context.reset(token)


class UsageWorkerInterceptor(Interceptor):
    def intercept_activity(self, next):
        return UsageActivityInterceptor(next)
