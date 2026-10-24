"""Provider authorization through Cloud URL elicitation; secrets never become tool results."""
import time
import uuid
from urllib.parse import urlsplit


class ProviderCredentials:
    def __init__(self, context):
        self.context = context

    def _request(self, path, method, payload=None):
        if urlsplit(self.context.interaction_url).scheme != 'https':
            raise RuntimeError('Provider authorization requires an HTTPS Cloud Run context.')
        return self.context._interaction_request('credentials/' + path, method=method, payload=payload)

    def authorize(self, provider, wait_seconds=15, *, base_url=None, model=None, force_new=False, reuse_existing=False):
        """Request web approval, or return explicit MCP-config preauthorization.

        Always use the returned provider/base_url/model: caller configuration can
        override tool defaults (including force_new). Keys stay at the Cloud
        boundary and must never be supplied as arguments to this method.
        """
        if provider not in ('openai', 'anthropic', 'nexus', 'custom_openai', 'custom_anthropic'):
            raise ValueError('Unsupported provider')
        if type(wait_seconds) is not int or not 0 <= wait_seconds <= 20:
            raise ValueError('wait_seconds must be 0..20')
        payload = {'provider':provider}
        if base_url is not None:payload['base_url']=base_url
        if model is not None:payload['model']=model
        if type(force_new) is not bool:raise ValueError('force_new must be a boolean')
        if force_new:payload['force_new']=True
        if type(reuse_existing) is not bool:raise ValueError('reuse_existing must be a boolean')
        # Opt in to the caller's latest active grant of the same protocol when
        # no endpoint/model was specified. Cloud still enforces Agent/caller scope.
        if reuse_existing:payload['reuse_existing']=True
        value = self._request('', 'POST', payload)
        deadline = time.monotonic() + wait_seconds
        while value.get('status') == 'pending' and time.monotonic() < deadline:
            time.sleep(min(1, max(0, deadline-time.monotonic())))
            value = self.status(value['authorization_id'])
        return value

    def status(self, authorization_id):
        return self._request(str(uuid.UUID(authorization_id)) + '/', 'GET')

    def resolve_for_runtime(self, authorization_id):
        """Trusted execution code only. Never return this dictionary from an MCP tool.

        Each resolution rechecks caller scope, grant expiry/revocation and active Run.
        Do not persist, log, put in prompts, or cache beyond the authorized operation.
        """
        return self._request(str(uuid.UUID(authorization_id)) + '/', 'POST', {})
