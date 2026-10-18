"""Register caller-bound background telemetry from an active authorized Cloud Run."""
import uuid
from urllib.parse import urlsplit


class BackgroundJobs:
    def __init__(self,context):self.context=context

    def register_for_runtime(self,authorization_id,request_id):
        """Trusted runtime only: result contains a private, job-scoped delivery token.

        Return only view_url to MCP callers, never token/push_url/command_url.
        This token cannot invoke a model or read the job's data.
        """
        if urlsplit(self.context.interaction_url).scheme!='https':
            raise RuntimeError('Background jobs require an HTTPS Cloud Run context.')
        return self.context._interaction_request('background-jobs/',method='POST',payload={
            'authorization_id':str(uuid.UUID(authorization_id)),'request_id':str(uuid.UUID(request_id))})
