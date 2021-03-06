"""
Oss Token Middleware
This WSGI component:
* Gets a request from the oss2swift middleware with an Oss Authorization
  access key.
* Validates oss token in Keystone.
* Transforms the account name to AUTH_%(tenant_name).
"""

import base64
from hashlib import sha1
import hmac
import json
import logging
import sys

from oss2swift.utils import is_valid_ipv6
import requests
import six
from six.moves.urllib.parse import unquote
from swift.common.swob import Request, Response, HTTPUnauthorized
from swift.common.utils import config_true_value, split_path
from swift.common.wsgi import ConfigFileError


PROTOCOL_NAME = 'OSS Token Authentication'


class ServiceError(Exception):
    pass


class OssToken(object):
    """Middleware that handles Oss authentication."""

    def __init__(self, app, conf):
        """Common initialization code."""
        self._app = app
        self._logger = logging.getLogger(conf.get('log_name', __name__))
        self._logger.debug('Starting the %s component', PROTOCOL_NAME)
        self._reseller_prefix = conf.get('reseller_prefix', 'AUTH_')
        # where to find the auth service (we use this to validate tokens)
        self.passwd=conf.get('passwd')
        self._request_uri = conf.get('auth_uri')
        if not self._request_uri:
            self._logger.warning(
                "Use of the auth_host, auth_port, and auth_protocol "
                "configuration options was deprecated in the Newton release "
                "in favor of auth_uri. These options may be removed in a "
                "future release.")
            auth_host = conf.get('auth_host')
            if not auth_host:
                raise ConfigFileError('Either auth_uri or auth_host required')
            elif is_valid_ipv6(auth_host):
                # Note(timburke) it is an IPv6 address, so it needs to be
                # wrapped with '[]' to generate a valid IPv6 URL, based on
                # http://www.ietf.org/rfc/rfc2732.txt
                auth_host = '[%s]' % auth_host
            auth_port = int(conf.get('auth_port', 35357))
            auth_protocol = conf.get('auth_protocol', 'https')

            self._request_uri = '%s://%s:%s' % (auth_protocol, auth_host,
                                                auth_port)
        self._request_uri = self._request_uri.rstrip('/')

        # SSL
        insecure = config_true_value(conf.get('insecure'))
        cert_file = conf.get('certfile')
        key_file = conf.get('keyfile')

        if insecure:
            self._verify = False
        elif cert_file and key_file:
            self._verify = (cert_file, key_file)
        elif cert_file:
            self._verify = cert_file
        else:
            self._verify = None

    def _deny_request(self, code):
        error_table = {
            'AccessDenied': (401, 'Access denied'),
            'InvalidURI': (400, 'Could not parse the specified URI'),
	    'Unauthorized': (403, 'Unauthorized'),
        }
        resp = Response(content_type='text/xml')
        resp.status = error_table[code][0]
        error_msg = ('<?xml version="1.0" encoding="UTF-8"?>\r\n'
                     '<Error>\r\n  <Code>%s</Code>\r\n  '
                     '<Message>%s</Message>\r\n</Error>\r\n' %
                     (code, error_table[code][1]))
        if six.PY3:
            error_msg = error_msg.encode()
        resp.body = error_msg
        return resp

    def _json_request(self, creds_json):
        headers = {'Content-Type': 'application/json'}
        try:
            response = requests.post('%s/v2.0/tokens' % self._request_uri,
                                     headers=headers, data=creds_json,
                                     verify=self._verify)
        except requests.exceptions.RequestException as e:
            self._logger.info('HTTP connection exception: %s', e)
            resp = self._deny_request('InvalidURI')
            raise ServiceError(resp)

        if response.status_code < 200 or response.status_code >= 300:
            self._logger.debug('Keystone reply error: status=%s reason=%s',
                               response.status_code, response.reason)
            resp = self._deny_request('AccessDenied')
            raise ServiceError(resp)

        return response

    def __call__(self, environ, start_response):
        """Handle incoming request. authenticate and send downstream."""
        req = Request(environ)
        self._logger.debug('Calling OssToken middleware.')

        try:
            parts = split_path(unquote(req.path), 1, 4, True)
            version, account, container, obj = parts
        except ValueError:
            msg = 'Not a path query, skipping.'
            self._logger.debug(msg)
            return self._app(environ, start_response)

        # Read request signature and access id.
        if 'Authorization' not in req.headers:
            msg = 'No Authorization header. skipping.'
            self._logger.debug(msg)
            return self._app(environ, start_response)

        token = req.headers.get('X-Auth-Token',
                                req.headers.get('X-Storage-Token'))
        if not token:
            msg = 'You did not specify an auth or a storage token. skipping.'
            self._logger.debug(msg)
            return self._app(environ, start_response)

        auth_header = req.headers['Authorization']
        try:
            access, signature = auth_header.split(' ')[-1].rsplit(':', 1)
        except ValueError:
            msg = 'You have an invalid Authorization header: %s'
            self._logger.debug(msg, auth_header)
            return self._deny_request('InvalidURI')(environ, start_response)

        # NOTE(chmou): This is to handle the special case with nova
        # when we have the option oss_affix_tenant. We will force it to
        # connect to another account than the one
        # authenticated. Before people start getting worried about
        # security, I should point that we are connecting with
        # username/token specified by the user but instead of
        # connecting to its own account we will force it to go to an
        # another account. In a normal scenario if that user don't
        # have the reseller right it will just fail but since the
        # reseller account can connect to every account it is allowed
        # by the swift_auth middleware.
        force_tenant = None
        if ':' in access:
            access, force_tenant = access.split(':')

        # Authenticate request.
	msg = base64.urlsafe_b64decode(unquote(token))
	key = self.passwd
	s = base64.encodestring(hmac.new(key, msg, sha1).digest()).strip()
	if s != signature:
	    #resp = self._deny_request('Unauthorized')
            #raise ServiceError(resp)
	    auth='Swift realm="%s"' % signature
	    return HTTPUnauthorized(request=req,
                   headers={'Www-Authenticate': auth})
        creds = {"auth":
			{"passwordCredentials":
				{"username": access, 
				"password": key},
				"tenantName": force_tenant}}
		
        creds_json = json.dumps(creds)
        self._logger.debug('Connecting to Keystone sending this JSON: %s',
                           creds_json)
        # NOTE(vish): We could save a call to keystone by having
        #             keystone return token, tenant, user, and roles
        #             from this call.
        #
        # NOTE(chmou): We still have the same problem we would need to
        #              change token_auth to detect if we already
        #              identified and not doing a second query and just
        #              pass it through to swiftauth in this case.
        try:
            resp = self._json_request(creds_json)
        except ServiceError as e:
            resp = e.args[0]  # NB: swob.Response, not requests.Response
            msg = 'Received error, exiting middleware with error: %s'
            self._logger.debug(msg, resp.status_int)
            return resp(environ, start_response)

        self._logger.debug('Keystone Reply: Status: %d, Output: %s',
                           resp.status_code, resp.content)

        try:
            identity_info = resp.json()
            token_id = str(identity_info['access']['token']['id'])
            tenant = identity_info['access']['token']['tenant']
        except (ValueError, KeyError):
            error = 'Error on keystone reply: %d %s'
            self._logger.debug(error, resp.status_code, resp.content)
            return self._deny_request('InvalidURI')(environ, start_response)

        req.headers['X-Auth-Token'] = token_id
        tenant_to_connect =  tenant['id']
        if six.PY2 and isinstance(tenant_to_connect, six.text_type):
            tenant_to_connect = tenant_to_connect.encode('utf-8')
        self._logger.debug('Connecting with tenant: %s', tenant_to_connect)
        new_tenant_name = '%s%s' % (self._reseller_prefix, tenant_to_connect)
        environ['PATH_INFO'] = environ['PATH_INFO'].replace(account,
                                                            new_tenant_name)
        return self._app(environ, start_response)


def filter_factory(global_conf, **local_conf):
    """Returns a WSGI filter app for use with paste.deploy."""
    conf = global_conf.copy()
    conf.update(local_conf)

    def auth_filter(app):
        return OssToken(app, conf)
    return auth_filter
