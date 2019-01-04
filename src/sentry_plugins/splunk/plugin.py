"""
- Turn on HTTP Event Collector by enabling its endpoint. HEC is not enabled by default.
  - http://dev.splunk.com/view/event-collector/SP-CAAAE7F
  - Settings > Data Inputs > HTTP Event Collector > Add new
    - Name: Sentry
  - You'll be given an HEC token, which is needed to configure Sentry.
- On the client that will log to HEC, create a POST request, and set its authentication header or key/value pair to include the HEC token.
- POST data to the HEC token receiver.

Note: Managed Splunk Cloud customers can turn on HTTP Event Collector by filing a request ticket with Splunk Support.
Note: Managed Splunk Cloud customers can create a HEC token by filing a request ticket with Splunk Support.

For more details on the payload: http://dev.splunk.com/view/event-collector/SP-CAAAE6M
"""

from __future__ import absolute_import

import six

from sentry import http, tagstore
from sentry.app import ratelimiter
from sentry.plugins.base import Plugin
from sentry.plugins.base.configuration import react_plugin_config
from sentry.utils import metrics
from sentry.utils.hashlib import md5_text

from sentry_plugins.base import CorePluginMixin
from sentry_plugins.utils import get_secret_field_config
from sentry_plugins.anonymizeip import anonymize_ip


class SplunkPlugin(CorePluginMixin, Plugin):
    title = 'Splunk'
    slug = 'splunk'
    description = 'Send Sentry events into Splunk.'
    conf_key = 'splunk'

    def configure(self, project, request):
        return react_plugin_config(self, project, request)

    def has_project_conf(self):
        return True

    def get_plugin_type(self):
        return 'data-forwarding'

    def get_config(self, project, **kwargs):
        return [{
            'name': 'instance',
            'label': 'Instance URL',
            'type': 'url',
            'required': True,
            'help': 'The HTTP Event Collector endpoint for your Splunk instance.',
            'placeholder': 'e.g. https://input-foo.cloud.splunk.com:8088',
        }, {
            'name': 'index',
            'label': 'Index',
            'type': 'string',
            'required': True,
            'default': 'main',
        }, {
            'name': 'source',
            'label': 'Source',
            'type': 'string',
            'required': True,
            'default': 'sentry',
        }, get_secret_field_config(
            name='token',
            label='Token',
            secret=self.get_option('token', project),
        )]

    def get_host_for_splunk(self, event):
        host = event.get_tag('server_name')
        if host:
            return host

        user_interface = event.interfaces.get('sentry.interfaces.User')
        if user_interface:
            host = user_interface.ip_address
            if host:
                return host

        return None

    def get_event_payload(self, event):
        props = {
            'event_id': event.event_id,
            'project_id': event.project.slug,
            'transaction': event.get_tag('transaction') or '',
            'release': event.get_tag('sentry:release') or '',
            'environment': event.get_tag('environment') or '',
            'type': event.get_event_type(),
        }
        props['tags'] = [[k.format(tagstore.get_standardized_key(k)), v]
                         for k, v in event.get_tags()]
        for key, value in six.iteritems(event.interfaces):
            if key == 'request':
                headers = value.headers
                if not isinstance(headers, dict):
                    headers = dict(headers or ())

                props.update({
                    'request_url': value.url,
                    'request_method': value.method,
                    'request_referer': headers.get('Referer', ''),
                })
            elif key == 'exception':
                exc = value.values[0]
                props.update({
                    'exception_type': exc.type,
                    'exception_value': exc.value,
                })
            elif key == 'logentry':
                props.update({
                    'message': value.formatted or value.message,
                })
            elif key in ('csp', 'expectct', 'expectstable', 'hpkp'):
                props.update({
                    '{}_{}'.format(key.rsplit('.', 1)[-1].lower(), k): v
                    for k, v in six.iteritems(value.to_json())
                })
            elif key == 'user':
                user_payload = {}
                if value.id:
                    user_payload['user_id'] = value.id
                if value.email:
                    user_payload['user_email_hash'] = md5_text(value.email).hexdigest()
                if value.ip_address:
                    user_payload['user_ip_trunc'] = anonymize_ip(value.ip_address)
                if user_payload:
                    props.update(user_payload)
        return props

    # http://dev.splunk.com/view/event-collector/SP-CAAAE6M
    def post_process(self, event, **kwargs):
        token = self.get_option('token', event.project)
        index = self.get_option('index', event.project)
        instance = self.get_option('instance', event.project)
        if not (token and index and instance):
            metrics.incr('integrations.splunk.forward-event.unconfigured', tags={
                'project_id': event.project_id,
                'organization_id': event.project.organization_id,
                'event_type': event.get_event_type(),
            })
            return

        if not instance.endswith('/services/collector'):
            instance = instance.rstrip('/') + '/services/collector'

        source = self.get_option('source', event.project) or 'sentry'

        rl_key = 'splunk:{}'.format(md5_text(token).hexdigest())
        # limit splunk to 50 requests/second
        if ratelimiter.is_limited(rl_key, limit=1000, window=1):
            metrics.incr('integrations.splunk.forward-event.rate-limited', tags={
                'project_id': event.project_id,
                'organization_id': event.project.organization_id,
                'event_type': event.get_event_type(),
            })
            return

        payload = {
            'time': int(event.datetime.strftime('%s')),
            'source': source,
            'index': index,
            'event': self.get_event_payload(event),
        }
        host = self.get_host_for_splunk(event)
        if host:
            payload['host'] = host

        session = http.build_session()
        try:
            session.post(
                instance,
                json=payload,
                # Splunk cloud instances certifcates dont play nicely
                verify=False,
                headers={
                    'Authorization': 'Splunk {}'.format(token)
                },
            ).raise_for_status()
        except Exception:
            metrics.incr('integrations.splunk.forward-event.error', tags={
                'project_id': event.project_id,
                'organization_id': event.project.organization_id,
                'event_type': event.get_event_type(),
            })
            raise

        metrics.incr('integrations.splunk.forward-event.success', tags={
            'project_id': event.project_id,
            'organization_id': event.project.organization_id,
            'event_type': event.get_event_type(),
        })
