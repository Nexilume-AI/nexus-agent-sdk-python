"""Publish scoped, temporary browser views of already-uploaded FST outputs."""


class RunWaveViews:
    def __init__(self, context):
        self.context = context

    def create(self, file_ids, snapshot):
        return self.context._interaction_request('wave-views/', method='POST',
            payload={'file_ids': list(file_ids), 'snapshot': snapshot})

    def action(self, view_id, action='ticket', **payload):
        import uuid
        view_id = str(uuid.UUID(str(view_id)))
        return self.context._interaction_request(f'wave-views/{view_id}/', method='POST',
            payload={'action': action, **payload})
