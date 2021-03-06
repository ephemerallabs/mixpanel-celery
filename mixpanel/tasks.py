import httplib
import urllib
import base64
import logging
import socket
import datetime
import json

from django.utils import simplejson

from celery.task import Task
from celery.registry import tasks

from mixpanel.conf import settings as mp_settings

class MixpanelTask(Task):

    max_retries = mp_settings.MIXPANEL_MAX_RETRIES

    class FailedEventRequest(Exception):
        """The attempted recording event failed because of a non-200 HTTP return code"""
        pass

    def run(self, *args, **kwargs):

        conn = self._get_connection()
        try:
            endpoint = self._get_endpoint()
            result = self._send_request(conn, endpoint, self.url_params)
        except EventTracker.FailedEventRequest, exception:
            conn.close()
            self.retry(args=args,
                       kwargs=kwargs,
                       exc=exception,
                       countdown=mp_settings.MIXPANEL_RETRY_DELAY,
                       throw=False)
            return
        conn.close()

        return result

    def _is_test(self, test):
        """
        Determine whether this event should be logged as a test request, meaning
        it won't actually be stored on the Mixpanel servers. A return result of
        1 means this will be a test, 0 means it won't as per the API spec.

        Uses ``:mod:mixpanel.conf.settings.MIXPANEL_TEST_ONLY`` as the default
        if no explicit test option is given.
        """
        if test == None:
            test = mp_settings.MIXPANEL_TEST_ONLY

        if test:
            return 1
        return 0

    def _handle_properties(self, properties, token):
        """
        Build a properties dictionary, accounting for the token.
        """
        if properties == None:
            properties = {}

        if not properties.get('token', None):
            if token is None:
                token = mp_settings.MIXPANEL_API_TOKEN
            properties['token'] = token

        l = self.get_logger()
        l.debug('pre-encoded properties: <%s>' % repr(properties))

        return properties

    def _build_params(self, params, is_test):
        """
        Build HTTP params to record the given event and properties.
        """
        data = base64.b64encode(simplejson.dumps(params))

        data_var = mp_settings.MIXPANEL_DATA_VARIABLE
        url_params = urllib.urlencode({data_var: data, 'test': is_test})

        return url_params

    def _get_connection(self):
        server = mp_settings.MIXPANEL_API_SERVER

        # Wish we could use python 2.6's httplib timeout support
        socket.setdefaulttimeout(mp_settings.MIXPANEL_API_TIMEOUT)
        return httplib.HTTPConnection(server)

    def _send_request(self, connection, endpoint, params):
        """
        Send a an event with its properties to the api server.

        Returns ``true`` if the event was logged by Mixpanel.
        """
        try:
            connection.request('GET', '%s?%s' % (endpoint, params))

            response = connection.getresponse()
        except socket.error, message:
            raise EventTracker.FailedEventRequest("The tracking request failed with a socket error. Message: [%s]" % message)

        if response.status != 200 or response.reason != 'OK':
            raise EventTracker.FailedEventRequest("The tracking request failed. Non-200 response code was: %s %s" % (response.status, response.reason))

        # Successful requests will generate a log
        response_data = response.read()
        if response_data != '1':
            return False

        return True


class EventTracker(MixpanelTask):
    """
    Task to track a Mixpanel event.
    """
    name = "mixpanel.tasks.EventTracker"

    def run(self, event_name, distinct_id=None, ip=None, properties=None, token=None,
            test=None, throw_retry_error=False, **kwargs):
        """
        Track an event occurrence to mixpanel through the API.

        ``event_name`` is the string for the event/category you'd like to log
        this event under
        ``properties`` is (optionally) a dictionary of key/value pairs
        describing the event.
        ``token`` is (optionally) your Mixpanel api token. Not required if
        you've already configured your MIXPANEL_API_TOKEN setting.
        ``test`` is an optional override to your
        `:data:mixpanel.conf.settings.MIXPANEL_TEST_ONLY` setting for determining
        if the event requests should actually be stored on the Mixpanel servers.
        """
        l = self.get_logger(**kwargs)
        if l.getEffectiveLevel() == logging.DEBUG:
            httplib.HTTPConnection.debuglevel = 1
        l.info("Recording event: <%s>" % event_name)

        self._handle_properties(properties, token)
        is_test = self._is_test(test)

        params = {'event': event_name, 'properties': properties}
        self.url_params = self._build_params(params, is_test)
        l.debug("url_params: <%s>" % self.url_params)

        result = super(EventTracker, self).run(event_name,
                                               ip=ip,
                                               distinct_id=distinct_id,
                                               properties=properties,
                                               token=token,
                                               test=test,
                                               throw_retry_error=throw_retry_error,
                                               **kwargs)

        if result:
            l.info("Event recorded/logged: <%s>" % event_name)
        else:
            l.info("Event ignored: <%s>" % event_name)
            if result is None:
                l.info("Event failed. Retrying: <%s>" % event_name)

        return result

    def _get_endpoint(self):
        return mp_settings.MIXPANEL_TRACKING_ENDPOINT

tasks.register(EventTracker)

class UserTracker(MixpanelTask):

    name = "mixpanel.tasks.UserTracker"
    max_retries = mp_settings.MIXPANEL_MAX_RETRIES
    event_map = {
        'set': '$set',
        'add': '$add',
        'track_charge': '$append',
    }

    def run(self, event='set', distinct_id=None, ip=None, properties={}, token=None, add=False,
            test=None, throw_retry_error=False, **kwargs):
        is_test = self._is_test(test)

        l = self.get_logger(**kwargs)
        if l.getEffectiveLevel() == logging.DEBUG:
            httplib.HTTPConnection.debuglevel = 1

        if not distinct_id and not ip:
            l.info("Cannot track without distinct_id or IP")
            return False

        if token is None:
            token = mp_settings.MIXPANEL_API_TOKEN

        params = {'$ip' : ip,
                  '$distinct_id' : distinct_id,
                  '$token' : token}

        self.url_params = self._build_params(event, params, properties, is_test)

        return super(UserTracker, self).run(distinct_id=distinct_id,
                                            ip=ip,
                                            properties=properties,
                                            token=token,
                                            add=add,
                                            test=test,
                                            throw_retry_error=throw_retry_error,
                                            **kwargs)

    def _build_params(self, event, params, properties, is_test):
        """
        Build HTTP params to record the given event and properties.
        """
        mp_key = self.event_map[event]

        if event == 'track_charge':
            time = properties.get('time', datetime.datetime.now().isoformat())
            transactions = dict(
                (k, v) for (k, v) in properties.iteritems()
                if not k in ('token', 'distinct_id', 'amount')
            )

            transactions['$time'] = time
            transactions['$amount'] = properties['amount']
            params[mp_key] = {'$transactions': transactions}

        else:
            # strip token and distinct_id out of the properties and use the
            # rest for passing with $set and $add
            params[mp_key] = dict(
                (k, (v.strftime('%Y-%m-%dT%H:%M:%S') if isinstance(v, datetime.datetime) else v)) for (k, v) in properties.iteritems()
                if not k in ('token', 'distinct_id')
            )

        return self._encode_params(params, is_test)

    def _encode_params(self, params, is_test):
        """
        Encodes data and returns the urlencoded parameters
        """
        data = base64.b64encode(json.dumps(params))

        data_var = mp_settings.MIXPANEL_DATA_VARIABLE
        return urllib.urlencode({data_var: data, 'test': is_test})

    def _get_endpoint(self):
        return mp_settings.MIXPANEL_USER_ENDPOINT

tasks.register(UserTracker)

class FunnelEventTracker(EventTracker):
    """
    Task to track a Mixpanel funnel event.
    """
    name = "mixpanel.tasks.FunnelEventTracker"

    class InvalidFunnelProperties(Exception):
        """Required properties were missing from the funnel-tracking call"""
        pass

    def run(self, funnel, step, goal, properties, token=None, test=None,
            throw_retry_error=False, **kwargs):
        """
        Track an event occurrence to mixpanel through the API.

        ``funnel`` is the string for the funnel you'd like to log
        this event under
        ``step`` the step in the funnel you're registering
        ``goal`` the end goal of this funnel
        ``properties`` is a dictionary of key/value pairs
        describing the funnel event. A ``distinct_id`` is required.
        ``token`` is (optionally) your Mixpanel api token. Not required if
        you've already configured your MIXPANEL_API_TOKEN setting.
        ``test`` is an optional override to your
        `:data:mixpanel.conf.settings.MIXPANEL_TEST_ONLY` setting for determining
        if the event requests should actually be stored on the Mixpanel servers.
        """
        l = self.get_logger(**kwargs)
        l.info("Recording funnel: <%s>-<%s>" % (funnel, step))
        properties = self._handle_properties(properties, token)

        is_test = self._is_test(test)
        properties = self._add_funnel_properties(properties, funnel, step, goal)

        url_params = self._build_params(mp_settings.MIXPANEL_FUNNEL_EVENT_ID,
                                        properties, is_test)
        l.debug("url_params: <%s>" % url_params)
        conn = self._get_connection()

        try:
            result = self._send_request(conn, url_params)
        except EventTracker.FailedEventRequest, exception:
            conn.close()
            l.info("Funnel failed. Retrying: <%s>-<%s>" % (funnel, step))
            kwargs.update({
                'token': token,
                'test': test})
            self.retry(args=[funnel, step, goal, properties],
                       kwargs=kwargs,
                       exc=exception,
                       countdown=mp_settings.MIXPANEL_RETRY_DELAY,
                       throw=throw_retry_error)
            return
        conn.close()
        if result:
            l.info("Funnel recorded/logged: <%s>-<%s>" % (funnel, step))
        else:
            l.info("Funnel ignored: <%s>-<%s>" % (funnel, step))

        return result

    def _add_funnel_properties(self, properties, funnel, step, goal):
        if not properties.has_key('distinct_id'):
            error_msg = "A ``distinct_id`` must be given to record a funnel event"
            raise FunnelEventTracker.InvalidFunnelProperties(error_msg)
        properties['funnel'] = funnel
        properties['step'] = step
        properties['goal'] = goal

        return properties

tasks.register(FunnelEventTracker)
