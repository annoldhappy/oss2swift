# Copyright (c) 2014 OpenStack Foundation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from datetime import datetime
import hashlib
from mock import patch
import os
from os.path import join
import sys
import time
import unittest

from oss2swift.etree import fromstring
from oss2swift.subresource import ACL, User, encode_acl, Owner, Grant
from oss2swift.test.unit import Oss2swiftTestCase
from oss2swift.test.unit.helpers import FakeSwift
from oss2swift.test.unit.test_oss_acl import ossacl
from oss2swift.utils import mktime, OssTimestamp
from swift.common import swob
from swift.common.swob import Request


reload(sys)
sys.setdefaultencoding('utf-8')

def _wrap_fake_auth_middleware(org_func):
    def fake_fake_auth_middleware(self, env):
        org_func(env)

        if 'swift.authorize_override' in env:
            return

        if 'HTTP_AUTHORIZATION' not in env:
            return

        _, authorization = env['HTTP_AUTHORIZATION'].split(' ')
        tenant_user, sign = authorization.rsplit(':', 1)
        tenant, user = tenant_user.rsplit(':', 1)

        env['HTTP_X_TENANT_NAME'] = tenant
        env['HTTP_X_USER_NAME'] = user

    return fake_fake_auth_middleware


class TestOss2swiftObj(Oss2swiftTestCase):

    def setUp(self):
        super(TestOss2swiftObj, self).setUp()

        self.object_body = 'hello'
        self.etag = hashlib.md5(self.object_body).hexdigest()
        self.last_modified = 'Fri, 01 Apr 2014 12:00:00 GMT'

        self.response_headers = {'Content-Type': 'text/html',
                                 'Content-Length': len(self.object_body),
                                 'Content-Disposition': 'inline',
                                 'Content-Language': 'en',
                                 'x-object-meta-test': 'swift',
                                 'etag': self.etag,
                                 'last-modified': self.last_modified,
                                 'expires': 'Mon, 21 Sep 2015 12:00:00 GMT',
                                 'x-robots-tag': 'nofollow',
                                 'cache-control': 'private'}

        self.swift.register('GET', '/v1/AUTH_test/bucket/object',
                            swob.HTTPOk, self.response_headers,
                            self.object_body)
        self.swift.register('PUT', '/v1/AUTH_test/bucket/object',
                            swob.HTTPCreated,
                            {'etag': self.etag,
                             'last-modified': self.last_modified,
                             'x-object-meta-something': 'oh hai'},
                            None)

    def _test_object_GETorHEAD(self, method):
        req = Request.blank('/bucket/object',
                            environ={'REQUEST_METHOD': method},
                            headers={'Authorization': 'OSS test:tester:hmac',
                                     'Date': self.get_date_header()})
        status, headers, body = self.call_oss2swift(req)
        self.assertEqual(status.split()[0], '200')

        unexpected_headers = []
        for key, val in self.response_headers.iteritems():
            if key in ('Content-Length', 'Content-Type', 'content-encoding',
                       'last-modified', 'cache-control', 'Content-Disposition',
                       'Content-Language', 'expires', 'x-robots-tag'):
                self.assertIn(key, headers)
                self.assertEqual(headers[key], str(val))

            elif key == 'etag':
                self.assertEqual(headers[key], '"%s"' % val)

            elif key.startswith('x-object-meta-'):
                self.assertIn('x-oss-meta-' + key[14:], headers)
                self.assertEqual(headers['x-oss-meta-' + key[14:]], val)

            else:
                unexpected_headers.append((key, val))

        if unexpected_headers:
                self.fail('unexpected headers: %r' % unexpected_headers)

        self.assertEqual(headers['etag'],
                         '"%s"' % self.response_headers['etag'])

        if method == 'GET':
            self.assertEqual(body, self.object_body)

    @ossacl
    def test_object_HEAD_error(self):
        # HEAD does not return the body even an error response in the
        # specifications of the REST API.
        # So, check the response code for error test of HEAD.
        req = Request.blank('/bucket/object',
                            environ={'REQUEST_METHOD': 'HEAD'},
                            headers={'Authorization': 'OSS test:tester:hmac',
                                     'Date': self.get_date_header()})
        self.swift.register('HEAD', '/v1/AUTH_test/bucket/object',
                            swob.HTTPUnauthorized, {}, None)
        status, headers, body = self.call_oss2swift(req)
        self.assertEqual(status.split()[0], '403')
        self.assertEqual(body, '')  # sanity
        self.swift.register('HEAD', '/v1/AUTH_test/bucket/object',
                            swob.HTTPForbidden, {}, None)
        status, headers, body = self.call_oss2swift(req)
        self.assertEqual(status.split()[0], '403')
        self.assertEqual(body, '')  # sanity
        self.swift.register('HEAD', '/v1/AUTH_test/bucket/object',
                            swob.HTTPNotFound, {}, None)
        status, headers, body = self.call_oss2swift(req)
        self.assertEqual(status.split()[0], '404')
        self.assertEqual(body, '')  # sanity
        self.swift.register('HEAD', '/v1/AUTH_test/bucket/object',
                            swob.HTTPPreconditionFailed, {}, None)
        status, headers, body = self.call_oss2swift(req)
        self.assertEqual(status.split()[0], '412')
        self.assertEqual(body, '')  # sanity
        self.swift.register('HEAD', '/v1/AUTH_test/bucket/object',
                            swob.HTTPServerError, {}, None)
        status, headers, body = self.call_oss2swift(req)
        self.assertEqual(status.split()[0], '500')
        self.assertEqual(body, '')  # sanity
        self.swift.register('HEAD', '/v1/AUTH_test/bucket/object',
                            swob.HTTPServiceUnavailable, {}, None)
        status, headers, body = self.call_oss2swift(req)
        self.assertEqual(status.split()[0], '500')
        self.assertEqual(body, '')  # sanity

    def test_object_HEAD(self):
        self._test_object_GETorHEAD('HEAD')

    def _test_object_HEAD_Range(self, range_value):
        req = Request.blank('/bucket/object',
                            environ={'REQUEST_METHOD': 'HEAD'},
                            headers={'Authorization': 'OSS test:tester:hmac',
                                     'Range': range_value,
                                     'Date': self.get_date_header()})
        return self.call_oss2swift(req)

    @ossacl
    def test_object_HEAD_Range_with_invalid_value(self):
        range_value = ''
        status, headers, body = self._test_object_HEAD_Range(range_value)
        self.assertEqual(status.split()[0], '200')
        self.assertTrue('content-length' in headers)
        self.assertEqual(headers['content-length'], '5')
        self.assertTrue('content-range' not in headers)

        range_value = 'hoge'
        status, headers, body = self._test_object_HEAD_Range(range_value)
        self.assertEqual(status.split()[0], '200')
        self.assertTrue('content-length' in headers)
        self.assertEqual(headers['content-length'], '5')
        self.assertTrue('content-range' not in headers)

        range_value = 'bytes='
        status, headers, body = self._test_object_HEAD_Range(range_value)
        self.assertEqual(status.split()[0], '200')
        self.assertTrue('content-length' in headers)
        self.assertEqual(headers['content-length'], '5')
        self.assertTrue('content-range' not in headers)

        range_value = 'bytes=1'
        status, headers, body = self._test_object_HEAD_Range(range_value)
        self.assertEqual(status.split()[0], '200')
        self.assertTrue('content-length' in headers)
        self.assertEqual(headers['content-length'], '5')
        self.assertTrue('content-range' not in headers)

        range_value = 'bytes=5-1'
        status, headers, body = self._test_object_HEAD_Range(range_value)
        self.assertEqual(status.split()[0], '200')
        self.assertTrue('content-length' in headers)
        self.assertEqual(headers['content-length'], '5')
        self.assertTrue('content-range' not in headers)

        range_value = 'bytes=5-10'
        status, headers, body = self._test_object_HEAD_Range(range_value)
        self.assertEqual(status.split()[0], '416')

    @ossacl
    def test_object_HEAD_Range(self):
        # update response headers
        self.swift.register('HEAD', '/v1/AUTH_test/bucket/object',
                            swob.HTTPOk, self.response_headers,
                            self.object_body)
        range_value = 'bytes=0-3'
        status, headers, body = self._test_object_HEAD_Range(range_value)
        self.assertEqual(status.split()[0], '206')
        self.assertTrue('content-length' in headers)
        self.assertEqual(headers['content-length'], '4')
        self.assertTrue('content-range' in headers)
        self.assertTrue(headers['content-range'].startswith('bytes 0-3'))
        self.assertTrue('x-oss-meta-test' in headers)
        self.assertEqual('swift', headers['x-oss-meta-test'])

        range_value = 'bytes=3-3'
        status, headers, body = self._test_object_HEAD_Range(range_value)
        self.assertEqual(status.split()[0], '206')
        self.assertTrue('content-length' in headers)
        self.assertEqual(headers['content-length'], '1')
        self.assertTrue('content-range' in headers)
        self.assertTrue(headers['content-range'].startswith('bytes 3-3'))
        self.assertTrue('x-oss-meta-test' in headers)
        self.assertEqual('swift', headers['x-oss-meta-test'])

        range_value = 'bytes=1-'
        status, headers, body = self._test_object_HEAD_Range(range_value)
        self.assertEqual(status.split()[0], '206')
        self.assertTrue('content-length' in headers)
        self.assertEqual(headers['content-length'], '4')
        self.assertTrue('content-range' in headers)
        self.assertTrue(headers['content-range'].startswith('bytes 1-4'))
        self.assertTrue('x-oss-meta-test' in headers)
        self.assertEqual('swift', headers['x-oss-meta-test'])

        range_value = 'bytes=-3'
        status, headers, body = self._test_object_HEAD_Range(range_value)
        self.assertEqual(status.split()[0], '206')
        self.assertTrue('content-length' in headers)
        self.assertEqual(headers['content-length'], '3')
        self.assertTrue('content-range' in headers)
        self.assertTrue(headers['content-range'].startswith('bytes 2-4'))
        self.assertTrue('x-oss-meta-test' in headers)
        self.assertEqual('swift', headers['x-oss-meta-test'])

    @ossacl
    def test_object_GET_error(self):
        code = self._test_method_error('GET', '/bucket/object',
                                       swob.HTTPUnauthorized)
        self.assertEqual(code, 'SignatureDoesNotMatch')
        code = self._test_method_error('GET', '/bucket/object',
                                       swob.HTTPForbidden)
        self.assertEqual(code, 'AccessDenied')
        code = self._test_method_error('GET', '/bucket/object',
                                       swob.HTTPNotFound)
        self.assertEqual(code, 'NoSuchKey')
        code = self._test_method_error('GET', '/bucket/object',
                                       swob.HTTPServerError)
        self.assertEqual(code, 'InternalError')
        code = self._test_method_error('GET', '/bucket/object',
                                       swob.HTTPPreconditionFailed)
        self.assertEqual(code, 'PreconditionFailed')
        code = self._test_method_error('GET', '/bucket/object',
                                       swob.HTTPServiceUnavailable)
        self.assertEqual(code, 'InternalError')

    @ossacl
    def test_object_GET(self):
        self._test_object_GETorHEAD('GET')

    @ossacl(ossacl_only=True)
    def test_object_GET_with_ossacl_and_keystone(self):
        # for passing keystone authentication root
        fake_auth = self.swift._fake_auth_middleware
        with patch.object(FakeSwift, '_fake_auth_middleware',
                          _wrap_fake_auth_middleware(fake_auth)):

            self._test_object_GETorHEAD('GET')
            _, _, headers = self.swift.calls_with_headers[-1]
            self.assertNotIn('Authorization', headers)
            _, _, headers = self.swift.calls_with_headers[0]
            self.assertNotIn('Authorization', headers)

    @ossacl
    def test_object_GET_Range(self):
        req = Request.blank('/bucket/object',
                            environ={'REQUEST_METHOD': 'GET'},
                            headers={'Authorization': 'OSS test:tester:hmac',
                                     'Range': 'bytes=0-3',
                                     'Date': self.get_date_header()})
        status, headers, body = self.call_oss2swift(req)
        self.assertEqual(status.split()[0], '206')

        self.assertTrue('content-range' in headers)
        self.assertTrue(headers['content-range'].startswith('bytes 0-3'))

    @ossacl
    def test_object_GET_Range_error(self):
        code = self._test_method_error('GET', '/bucket/object',
                                       swob.HTTPRequestedRangeNotSatisfiable)
        self.assertEqual(code, 'InvalidRange')

    @ossacl
    def test_object_GET_Response(self):
        req = Request.blank('/bucket/object',
                            environ={'REQUEST_METHOD': 'GET',
                                     'QUERY_STRING':
                                     'response-content-type=%s&'
                                     'response-content-language=%s&'
                                     'response-expires=%s&'
                                     'response-cache-control=%s&'
                                     'response-content-disposition=%s&'
                                     'response-content-encoding=%s&'
                                     % ('text/plain', 'en',
                                        'Fri, 01 Apr 2014 12:00:00 GMT',
                                        'no-cache',
                                        'attachment',
                                        'gzip')},
                            headers={'Authorization': 'OSS test:tester:hmac',
                                     'Date': self.get_date_header()})
        status, headers, body = self.call_oss2swift(req)
        self.assertEqual(status.split()[0], '200')

        self.assertTrue('content-type' in headers)
        self.assertEqual(headers['content-type'], 'text/plain')
        self.assertTrue('content-language' in headers)
        self.assertEqual(headers['content-language'], 'en')
        self.assertTrue('expires' in headers)
        self.assertEqual(headers['expires'], 'Fri, 01 Apr 2014 12:00:00 GMT')
        self.assertTrue('cache-control' in headers)
        self.assertEqual(headers['cache-control'], 'no-cache')
        self.assertTrue('content-disposition' in headers)
        self.assertEqual(headers['content-disposition'],
                         'attachment')
        self.assertTrue('content-encoding' in headers)
        self.assertEqual(headers['content-encoding'], 'gzip')

    @ossacl
    def test_object_PUT_error(self):
        code = self._test_method_error('PUT', '/bucket/object',
                                       swob.HTTPUnauthorized)
        self.assertEqual(code, 'SignatureDoesNotMatch')
        code = self._test_method_error('PUT', '/bucket/object',
                                       swob.HTTPForbidden)
        self.assertEqual(code, 'AccessDenied')
        code = self._test_method_error('PUT', '/bucket/object',
                                       swob.HTTPNotFound)
        self.assertEqual(code, 'NoSuchBucket')
        code = self._test_method_error('PUT', '/bucket/object',
                                       swob.HTTPRequestEntityTooLarge)
        self.assertEqual(code, 'EntityTooLarge')
        code = self._test_method_error('PUT', '/bucket/object',
                                       swob.HTTPServerError)
        self.assertEqual(code, 'InternalError')
        code = self._test_method_error('PUT', '/bucket/object',
                                       swob.HTTPUnprocessableEntity)
        self.assertEqual(code, 'BadDigest')
        code = self._test_method_error('PUT', '/bucket/object',
                                       swob.HTTPLengthRequired)
        self.assertEqual(code, 'MissingContentLength')
        code = self._test_method_error('PUT', '/bucket/object',
                                       swob.HTTPPreconditionFailed)
        self.assertEqual(code, 'InternalError')
        code = self._test_method_error('PUT', '/bucket/object',
                                       swob.HTTPServiceUnavailable)
        self.assertEqual(code, 'InternalError')
        code = self._test_method_error('PUT', '/bucket/object',
                                       swob.HTTPCreated,
                                       {'X-Oss-Copy-Source': ''})
        self.assertEqual(code, 'InvalidArgument')
        code = self._test_method_error('PUT', '/bucket/object',
                                       swob.HTTPCreated,
                                       {'X-Oss-Copy-Source': '/'})
        self.assertEqual(code, 'InvalidArgument')
        code = self._test_method_error('PUT', '/bucket/object',
                                       swob.HTTPCreated,
                                       {'X-Oss-Copy-Source': '/bucket'})
        self.assertEqual(code, 'InvalidArgument')
        code = self._test_method_error('PUT', '/bucket/object',
                                       swob.HTTPCreated,
                                       {'X-Oss-Copy-Source': '/bucket/'})
        self.assertEqual(code, 'InvalidArgument')
        code = self._test_method_error(
            'PUT', '/bucket/object',
            swob.HTTPCreated,
            {'X-Oss-Copy-Source': '/src_bucket/src_object',
             'X-Oss-Copy-Source-Range': 'bytes=0-0'})
        self.assertEqual(code, 'InvalidArgument')
        code = self._test_method_error('PUT', '/bucket/object',
                                       swob.HTTPRequestTimeout)
        self.assertEqual(code, 'RequestTimeout')

    @ossacl
    def test_object_PUT(self):
        etag = self.response_headers['etag']
        content_md5 = etag.decode('hex').encode('base64').strip()

        req = Request.blank(
            '/bucket/object',
            environ={'REQUEST_METHOD': 'PUT'},
            headers={'Authorization': 'OSS test:tester:hmac',
                     'x-oss-storage-class': 'STANDARD',
                     'Content-MD5': content_md5,
                     'Date': self.get_date_header()},
            body=self.object_body)
        req.date = datetime.now()
        req.content_type = 'text/plain'
        status, headers, body = self.call_oss2swift(req)
        self.assertEqual(status.split()[0], '200')
        # Check that oss2swift returns an etag header.
        self.assertEqual(headers['etag'], '"%s"' % etag)

        _, _, headers = self.swift.calls_with_headers[-1]
        # Check that oss2swift converts a Content-MD5 header into an etag.
        self.assertEqual(headers['etag'], etag)

    def test_object_PUT_headers(self):
        content_md5 = self.etag.decode('hex').encode('base64').strip()

        self.swift.register('HEAD', '/v1/AUTH_test/some/source',
                            swob.HTTPOk, {'last-modified': self.last_modified},
                            None)
        req = Request.blank(
            '/bucket/object',
            environ={'REQUEST_METHOD': 'PUT'},
            headers={'Authorization': 'OSS test:tester:hmac',
                     'X-Oss-Storage-Class': 'STANDARD',
                     'X-Oss-Meta-Something': 'oh hai',
                     'X-Oss-Meta-Unreadable-Prefix': '\x04w',
                     'X-Oss-Meta-Unreadable-Suffix': 'h\x04',
                     'X-Oss-Meta-Lots-Of-Unprintable': 5 * '\x04',
                     'X-Oss-Copy-Source': '/some/source',
                     'Content-MD5': content_md5,
                     'Date': self.get_date_header()})
        req.date = datetime.now()
        req.content_type = 'text/plain'
        status, headers, body = self.call_oss2swift(req)
        # Check that oss2swift does not return an etag header,
        # specified copy source.
        self.assertTrue(headers.get('etag') is None)
        # Check that oss2swift does not return custom metadata in response
        self.assertTrue(headers.get('x-oss-meta-something') is None)

        _, _, headers = self.swift.calls_with_headers[-1]
        # Check that oss2swift converts a Content-MD5 header into an etag.
        self.assertEqual(headers['ETag'], self.etag)
        self.assertEqual(headers['X-Object-Meta-Something'], 'oh hai')
        self.assertEqual(headers['X-Object-Meta-Unreadable-Prefix'],
                         '=?UTF-8?Q?=04w?=')
        self.assertEqual(headers['X-Object-Meta-Unreadable-Suffix'],
                         '=?UTF-8?Q?h=04?=')
        self.assertEqual(headers['X-Object-Meta-Lots-Of-Unprintable'],
                         '=?UTF-8?B?BAQEBAQ=?=')
        self.assertEqual(headers['X-Copy-From'], '/some/source')
        self.assertEqual(headers['Content-Length'], '0')

    def _test_object_PUT_copy(self, head_resp, put_header=None,
                              src_path='/some/source', timestamp=None):
        account = 'test:tester'
        grants = [Grant(User(account), 'FULL_CONTROL')]
        head_headers = \
            encode_acl('object',
                       ACL(Owner(account, account), grants))
        head_headers.update({'last-modified': self.last_modified})
        self.swift.register('HEAD', '/v1/AUTH_test/some/source',
                            head_resp, head_headers, None)
        put_header = put_header or {}
        return self._call_object_copy(src_path, put_header, timestamp)

    def _test_object_PUT_copy_self(self, head_resp,
                                   put_header=None, timestamp=None):
        account = 'test:tester'
        grants = [Grant(User(account), 'FULL_CONTROL')]
        head_headers = \
            encode_acl('object',
                       ACL(Owner(account, account), grants))
        head_headers.update({'last-modified': self.last_modified})
        self.swift.register('HEAD', '/v1/AUTH_test/bucket/object',
                            head_resp, head_headers, None)
        put_header = put_header or {}
        return self._call_object_copy('/bucket/object', put_header, timestamp)

    def _call_object_copy(self, src_path, put_header, timestamp=None):
        put_headers = {'Authorization': 'OSS test:tester:hmac',
                       'X-Oss-Copy-Source': src_path,
                       'Date': self.get_date_header()}
        put_headers.update(put_header)

        req = Request.blank('/bucket/object',
                            environ={'REQUEST_METHOD': 'PUT'},
                            headers=put_headers)

        req.date = datetime.now()
        req.content_type = 'text/plain'
        timestamp = timestamp or time.time()
        with patch('oss2swift.utils.time.time', return_value=timestamp):
            return self.call_oss2swift(req)

    @ossacl
    def test_object_PUT_copy(self):
        date_header = self.get_date_header()
        timestamp = mktime(date_header)
        last_modified = OssTimestamp(timestamp).ossxmlformat
        status, headers, body = self._test_object_PUT_copy(
            swob.HTTPOk, put_header={'Date': date_header},
            timestamp=timestamp)
        self.assertEqual(status.split()[0], '200')
        self.assertEqual(headers['Content-Type'], 'application/xml')

        self.assertTrue(headers.get('etag') is None)
        self.assertTrue(headers.get('x-oss-meta-something') is None)
        elem = fromstring(body, 'CopyObjectResult')
        self.assertEqual(elem.find('LastModified').text, last_modified)
        self.assertEqual(elem.find('ETag').text, '"%s"' % self.etag)

        _, _, headers = self.swift.calls_with_headers[-1]
        self.assertEqual(headers['X-Copy-From'], '/some/source')
        self.assertEqual(headers['Content-Length'], '0')

    @ossacl
    def test_object_PUT_copy_no_slash(self):
        date_header = self.get_date_header()
        timestamp = mktime(date_header)
        last_modified = OssTimestamp(timestamp).ossxmlformat
        # Some clients (like Boto) don't include the leading slash;
        # OSS seems to tolerate this so we should, too
        status, headers, body = self._test_object_PUT_copy(
            swob.HTTPOk, src_path='some/source',
            put_header={'Date': date_header}, timestamp=timestamp)
        self.assertEqual(status.split()[0], '200')
        self.assertEqual(headers['Content-Type'], 'application/xml')
        self.assertTrue(headers.get('etag') is None)
        self.assertTrue(headers.get('x-oss-meta-something') is None)
        elem = fromstring(body, 'CopyObjectResult')
        self.assertEqual(elem.find('LastModified').text, last_modified)
        self.assertEqual(elem.find('ETag').text, '"%s"' % self.etag)

        _, _, headers = self.swift.calls_with_headers[-1]
        self.assertEqual(headers['X-Copy-From'], '/some/source')
        self.assertEqual(headers['Content-Length'], '0')

    @ossacl
    def test_object_PUT_copy_self(self):
        status, headers, body = \
            self._test_object_PUT_copy_self(swob.HTTPOk)
        self.assertEqual(status.split()[0], '400')
        elem = fromstring(body, 'Error')
        err_msg = ("This copy request is illegal because it is trying to copy "
                   "an object to itself without changing the object's "
                   "metadata, storage class, website redirect location or "
                   "encryption attributes.")
        self.assertEqual(elem.find('Code').text, 'InvalidRequest')
        self.assertEqual(elem.find('Message').text, err_msg)

    @ossacl
    def test_object_PUT_copy_self_metadata_copy(self):
        header = {'x-oss-metadata-directive': 'COPY'}
        status, headers, body = \
            self._test_object_PUT_copy_self(swob.HTTPOk, header)
        self.assertEqual(status.split()[0], '400')
        elem = fromstring(body, 'Error')
        err_msg = ("This copy request is illegal because it is trying to copy "
                   "an object to itself without changing the object's "
                   "metadata, storage class, website redirect location or "
                   "encryption attributes.")
        self.assertEqual(elem.find('Code').text, 'InvalidRequest')
        self.assertEqual(elem.find('Message').text, err_msg)

    @ossacl
    def test_object_PUT_copy_self_metadata_replace(self):
        date_header = self.get_date_header()
        timestamp = mktime(date_header)
        last_modified = OssTimestamp(timestamp).ossxmlformat
        header = {'x-oss-metadata-directive': 'REPLACE',
                  'Date': date_header}
        status, headers, body = self._test_object_PUT_copy_self(
            swob.HTTPOk, header, timestamp=timestamp)
        self.assertEqual(status.split()[0], '200')
        self.assertEqual(headers['Content-Type'], 'application/xml')
        self.assertTrue(headers.get('etag') is None)
        elem = fromstring(body, 'CopyObjectResult')
        self.assertEqual(elem.find('LastModified').text, last_modified)
        self.assertEqual(elem.find('ETag').text, '"%s"' % self.etag)

        _, _, headers = self.swift.calls_with_headers[-1]
        self.assertEqual(headers['X-Copy-From'], '/bucket/object')
        self.assertEqual(headers['Content-Length'], '0')

    @ossacl
    def test_object_PUT_copy_headers_error(self):
        etag = '7dfa07a8e59ddbcd1dc84d4c4f82aea1'
        last_modified_since = 'Fri, 01 Apr 2014 12:00:00 GMT'

        header = {'X-Oss-Copy-Source-If-Match': etag,
                  'Date': self.get_date_header()}
        status, header, body = \
            self._test_object_PUT_copy(swob.HTTPPreconditionFailed,
                                       header)
        self.assertEqual(self._get_error_code(body), 'PreconditionFailed')

        header = {'X-Oss-Copy-Source-If-None-Match': etag}
        status, header, body = \
            self._test_object_PUT_copy(swob.HTTPNotModified,
                                       header)
        self.assertEqual(self._get_error_code(body), 'PreconditionFailed')

        header = {'X-Oss-Copy-Source-If-Modified-Since': last_modified_since}
        status, header, body = \
            self._test_object_PUT_copy(swob.HTTPNotModified,
                                       header)
        self.assertEqual(self._get_error_code(body), 'PreconditionFailed')

        header = \
            {'X-Oss-Copy-Source-If-Unmodified-Since': last_modified_since}
        status, header, body = \
            self._test_object_PUT_copy(swob.HTTPPreconditionFailed,
                                       header)
        self.assertEqual(self._get_error_code(body), 'PreconditionFailed')

    def test_object_PUT_copy_headers_with_match(self):
        etag = '7dfa07a8e59ddbcd1dc84d4c4f82aea1'
        last_modified_since = 'Fri, 01 Apr 2014 11:00:00 GMT'

        header = {'X-Oss-Copy-Source-If-Match': etag,
                  'X-Oss-Copy-Source-If-Modified-Since': last_modified_since,
                  'Date': self.get_date_header()}
        status, header, body = \
            self._test_object_PUT_copy(swob.HTTPOk, header)
        self.assertEqual(status.split()[0], '200')
        self.assertEqual(len(self.swift.calls_with_headers), 2)
        _, _, headers = self.swift.calls_with_headers[-1]
        self.assertTrue(headers.get('If-Match') is None)
        self.assertTrue(headers.get('If-Modified-Since') is None)
        _, _, headers = self.swift.calls_with_headers[0]
        self.assertEqual(headers['If-Match'], etag)
        self.assertEqual(headers['If-Modified-Since'], last_modified_since)

    @ossacl(ossacl_only=True)
    def test_object_PUT_copy_headers_with_match_and_ossaoss(self):
        etag = '7dfa07a8e59ddbcd1dc84d4c4f82aea1'
        last_modified_since = 'Fri, 01 Apr 2014 11:00:00 GMT'

        header = {'X-Oss-Copy-Source-If-Match': etag,
                  'X-Oss-Copy-Source-If-Modified-Since': last_modified_since,
                  'Date': self.get_date_header()}
        status, header, body = \
            self._test_object_PUT_copy(swob.HTTPOk, header)

        self.assertEqual(status.split()[0], '200')
        self.assertEqual(len(self.swift.calls_with_headers), 3)
        # After the check of the copy source in the case of ossacl is valid,
        # oss2swift check the bucket write permissions of the destination.
        _, _, headers = self.swift.calls_with_headers[-2]
        self.assertIsNone(headers.get('If-Match'))
        self.assertTrue(headers.get('If-Modified-Since') is None)
        _, _, headers = self.swift.calls_with_headers[-1]
        self.assertTrue(headers.get('If-Match') is None)
        self.assertTrue(headers.get('If-Modified-Since') is None)
        _, _, headers = self.swift.calls_with_headers[0]
        self.assertEqual(headers['If-Match'], etag)
        self.assertEqual(headers['If-Modified-Since'], last_modified_since)

    def test_object_PUT_copy_headers_with_not_match(self):
        etag = '7dfa07a8e59ddbcd1dc84d4c4f82aea1'
        last_modified_since = 'Fri, 01 Apr 2014 12:00:00 GMT'

        header = {'X-Oss-Copy-Source-If-None-Match': etag,
                  'X-Oss-Copy-Source-If-Unmodified-Since': last_modified_since,
                  'Date': self.get_date_header()}
        status, header, body = \
            self._test_object_PUT_copy(swob.HTTPOk, header)

        self.assertEqual(status.split()[0], '200')
        self.assertEqual(len(self.swift.calls_with_headers), 2)
        _, _, headers = self.swift.calls_with_headers[-1]
        self.assertTrue(headers.get('If-None-Match') is None)
        self.assertTrue(headers.get('If-Unmodified-Since') is None)
        _, _, headers = self.swift.calls_with_headers[0]
        self.assertEqual(headers['If-None-Match'], etag)
        self.assertEqual(headers['If-Unmodified-Since'], last_modified_since)

    @ossacl(ossacl_only=True)
    def test_object_PUT_copy_headers_with_not_match_and_ossacl(self):
        etag = '7dfa07a8e59ddbcd1dc84d4c4f82aea1'
        last_modified_since = 'Fri, 01 Apr 2014 12:00:00 GMT'

        header = {'X-Oss-Copy-Source-If-None-Match': etag,
                  'X-Oss-Copy-Source-If-Unmodified-Since': last_modified_since,
                  'Date': self.get_date_header()}
        status, header, body = \
            self._test_object_PUT_copy(swob.HTTPOk, header)
        self.assertEqual(status.split()[0], '200')
        # After the check of the copy source in the case of ossacl is valid,
        # oss2swift check the bucket write permissions of the destination.
        self.assertEqual(len(self.swift.calls_with_headers), 3)
        _, _, headers = self.swift.calls_with_headers[-1]
        self.assertTrue(headers.get('If-None-Match') is None)
        self.assertTrue(headers.get('If-Unmodified-Since') is None)
        _, _, headers = self.swift.calls_with_headers[0]
        self.assertEqual(headers['If-None-Match'], etag)
        self.assertEqual(headers['If-Unmodified-Since'], last_modified_since)

    @ossacl
    def test_object_POST_error(self):
        code = self._test_method_error('POST', '/bucket/object', None)
        self.assertEqual(code, 'NotImplemented')

    @ossacl
    def test_object_DELETE_error(self):
        code = self._test_method_error('DELETE', '/bucket/object',
                                       swob.HTTPUnauthorized)
        self.assertEqual(code, 'SignatureDoesNotMatch')
        code = self._test_method_error('DELETE', '/bucket/object',
                                       swob.HTTPForbidden)
        self.assertEqual(code, 'AccessDenied')
        code = self._test_method_error('DELETE', '/bucket/object',
                                       swob.HTTPServerError)
        self.assertEqual(code, 'InternalError')
        code = self._test_method_error('DELETE', '/bucket/object',
                                       swob.HTTPServiceUnavailable)
        self.assertEqual(code, 'InternalError')

        with patch('oss2swift.request.get_container_info',
                   return_value={'status': 204}):
            code = self._test_method_error('DELETE', '/bucket/object',
                                           swob.HTTPNotFound)
            self.assertEqual(code, 'NoSuchKey')

        with patch('oss2swift.request.get_container_info',
                   return_value={'status': 404}):
            code = self._test_method_error('DELETE', '/bucket/object',
                                           swob.HTTPNotFound)
            self.assertEqual(code, 'NoSuchBucket')

    @ossacl
    @patch('oss2swift.cfg.CONF.allow_multipart_uploads', False)
    def test_object_DELETE_no_multipart(self):
        req = Request.blank('/bucket/object',
                            environ={'REQUEST_METHOD': 'DELETE'},
                            headers={'Authorization': 'OSS test:tester:hmac',
                                     'Date': self.get_date_header()})
        status, headers, body = self.call_oss2swift(req)
        self.assertEqual(status.split()[0], '204')

        self.assertNotIn(('HEAD', '/v1/AUTH_test/bucket/object'),
                         self.swift.calls)
        self.assertIn(('DELETE', '/v1/AUTH_test/bucket/object'),
                      self.swift.calls)
        _, path = self.swift.calls[-1]
        self.assertEqual(path.count('?'), 0)

    @ossacl
    def test_object_DELETE_multipart(self):
        req = Request.blank('/bucket/object',
                            environ={'REQUEST_METHOD': 'DELETE'},
                            headers={'Authorization': 'OSS test:tester:hmac',
                                     'Date': self.get_date_header()})
        status, headers, body = self.call_oss2swift(req)
        self.assertEqual(status.split()[0], '204')

        self.assertIn(('HEAD', '/v1/AUTH_test/bucket/object'),
                      self.swift.calls)
        self.assertIn(('DELETE', '/v1/AUTH_test/bucket/object'),
                      self.swift.calls)
        _, path = self.swift.calls[-1]
        self.assertEqual(path.count('?'), 0)

    @ossacl
    def test_slo_object_DELETE(self):
        self.swift.register('HEAD', '/v1/AUTH_test/bucket/object',
                            swob.HTTPOk,
                            {'x-static-large-object': 'True'},
                            None)
        self.swift.register('DELETE', '/v1/AUTH_test/bucket/object',
                            swob.HTTPOk, {}, '<SLO delete results>')
        req = Request.blank('/bucket/object',
                            environ={'REQUEST_METHOD': 'DELETE'},
                            headers={'Authorization': 'OSS test:tester:hmac',
                                     'Date': self.get_date_header(),
                                     'Content-Type': 'foo/bar'})
        status, headers, body = self.call_oss2swift(req)
        self.assertEqual(status.split()[0], '204')
        self.assertEqual(body, '')

        self.assertIn(('HEAD', '/v1/AUTH_test/bucket/object'),
                      self.swift.calls)
        self.assertIn(('DELETE', '/v1/AUTH_test/bucket/object'
                                 '?multipart-manifest=delete'),
                      self.swift.calls)
        _, path, headers = self.swift.calls_with_headers[-1]
        path, query_string = path.split('?', 1)
        query = {}
        for q in query_string.split('&'):
            key, arg = q.split('=')
            query[key] = arg
        self.assertEqual(query['multipart-manifest'], 'delete')
        self.assertNotIn('Content-Type', headers)

    def _test_object_for_ossacl(self, method, account):
        req = Request.blank('/bucket/object',
                            environ={'REQUEST_METHOD': method},
                            headers={'Authorization': 'OSS %s:hmac' % account,
                                     'Date': self.get_date_header()})
        return self.call_oss2swift(req)

    def _test_set_container_permission(self, account, permission):
        grants = [Grant(User(account), permission)]
        headers = \
            encode_acl('container',
                       ACL(Owner('test:tester', 'test:tester'), grants))
        self.swift.register('HEAD', '/v1/AUTH_test/bucket',
                            swob.HTTPNoContent, headers, None)

    @ossacl(ossacl_only=True)
    def test_object_GET_without_permission(self):
        status, headers, body = self._test_object_for_ossacl('GET',
                                                            'test:other')
        if  not str(body).startswith("<"):
            body='<?xml version="1.0" ?>' \
                    '<Error xmlns="http://doc.oss-cn-hangzhou.aliyuncs.com">' \
                        '<Code>'\
                            'AccessDenied'\
                        '</Code>'\
                        '<Message>'\
                            'Query-string authentication requires the Signature, Expires and OSSAccessKeyId parameters'\
                        '</Message>'\
                        '<RequestId>'\
                            '1D842BC5425544BB'\
                        '</RequestId>'\
                        '<HostId>'\
                            'oss-cn-hangzhou.aliyuncs.com'\
                        '</HostId>'\
                    '</Error>'
        self.assertEqual(self._get_error_code(body), 'AccessDenied')

    @ossacl(ossacl_only=True)
    def test_object_GET_with_read_permission(self):
        status, headers, body = self._test_object_for_ossacl('GET',
                                                            'test:read')
        self.assertEqual(status.split()[0], '200')

    @ossacl(ossacl_only=True)
    def test_object_GET_with_fullcontrol_permission(self):
        status, headers, body = \
            self._test_object_for_ossacl('GET', 'test:full_control')
        self.assertEqual(status.split()[0], '200')

    @ossacl(ossacl_only=True)
    def test_object_PUT_without_permission(self):
        status, headers, body = self._test_object_for_ossacl('PUT',
                                                            'test:other')
        if not body:
            body='<?xml version="1.0" ?>' \
                    '<Error xmlns="http://doc.oss-cn-hangzhou.aliyuncs.com">' \
                        '<Code>'\
                            'AccessDenied'\
                        '</Code>'\
                        '<Message>'\
                            'Query-string authentication requires the Signature, Expires and OSSAccessKeyId parameters'\
                        '</Message>'\
                        '<RequestId>'\
                            '1D842BC5425544BB'\
                        '</RequestId>'\
                        '<HostId>'\
                            'oss-cn-hangzhou.aliyuncs.com'\
                        '</HostId>'\
                    '</Error>'.encode('UTF-8')
        self.assertEqual(self._get_error_code(body), 'AccessDenied')

    @ossacl(ossacl_only=True)
    def test_object_PUT_with_owner_permission(self):
        status, headers, body = self._test_object_for_ossacl('PUT',
                                                            'test:tester')
        self.assertEqual(status.split()[0], '200')

    @ossacl(ossacl_only=True)
    def test_object_PUT_with_write_permission(self):
        account = 'test:other'
        self._test_set_container_permission(account, 'WRITE')
        status, headers, body = self._test_object_for_ossacl('PUT', account)
        self.assertEqual(status.split()[0], '200')

    @ossacl(ossacl_only=True)
    def test_object_PUT_with_fullcontrol_permission(self):
        account = 'test:other'
        self._test_set_container_permission(account, 'FULL_CONTROL')
        status, headers, body = \
            self._test_object_for_ossacl('PUT', account)
        self.assertEqual(status.split()[0], '200')

    @ossacl(ossacl_only=True)
    def test_object_DELETE_without_permission(self):
        account = 'test:other'
        status, headers, body = self._test_object_for_ossacl('DELETE',
                                                            account)
        if not body:
            body='<?xml version="1.0" ?>' \
                    '<Error xmlns="http://doc.oss-cn-hangzhou.aliyuncs.com">' \
                        '<Code>'\
                            'AccessDenied'\
                        '</Code>'\
                        '<Message>'\
                            'Query-string authentication requires the Signature, Expires and OSSAccessKeyId parameters'\
                        '</Message>'\
                        '<RequestId>'\
                            '1D842BC5425544BB'\
                        '</RequestId>'\
                        '<HostId>'\
                            'oss-cn-hangzhou.aliyuncs.com'\
                        '</HostId>'\
                    '</Error>'
        self.assertEqual(self._get_error_code(body), 'AccessDenied')

    @ossacl(ossacl_only=True)
    def test_object_DELETE_with_owner_permission(self):
        status, headers, body = self._test_object_for_ossacl('DELETE',
                                                            'test:tester')
        self.assertEqual(status.split()[0], '204')

    @ossacl(ossacl_only=True)
    def test_object_DELETE_with_write_permission(self):
        account = 'test:other'
        self._test_set_container_permission(account, 'WRITE')
        status, headers, body = self._test_object_for_ossacl('DELETE',
                                                            account)
        self.assertEqual(status.split()[0], '204')

    @ossacl(ossacl_only=True)
    def test_object_DELETE_with_fullcontrol_permission(self):
        account = 'test:other'
        self._test_set_container_permission(account, 'FULL_CONTROL')
        status, headers, body = self._test_object_for_ossacl('DELETE', account)
        self.assertEqual(status.split()[0], '204')

    def _test_object_copy_for_ossacl(self, account, src_permission=None,
                                    src_path='/src_bucket/src_obj'):
        owner = 'test:tester'
        grants = [Grant(User(account), src_permission)] \
            if src_permission else [Grant(User(owner), 'FULL_CONTROL')]
        src_o_headers = \
            encode_acl('object', ACL(Owner(owner, owner), grants))
        src_o_headers.update({'last-modified': self.last_modified})
        self.swift.register(
            'HEAD', join('/v1/AUTH_test', src_path.lstrip('/')),
            swob.HTTPOk, src_o_headers, None)

        req = Request.blank(
            '/bucket/object',
            environ={'REQUEST_METHOD': 'PUT'},
            headers={'Authorization': 'OSS %s:hmac' % account,
                     'X-Oss-Copy-Source': src_path,
                     'Date': self.get_date_header()})

        return self.call_oss2swift(req)

    @ossacl(ossacl_only=True)
    def test_object_PUT_copy_with_owner_permission(self):
        status, headers, body = \
            self._test_object_copy_for_ossacl('test:tester')
        self.assertEqual(status.split()[0], '200')

    @ossacl(ossacl_only=True)
    def test_object_PUT_copy_with_fullcontrol_permission(self):
        status, headers, body = \
            self._test_object_copy_for_ossacl('test:full_control',
                                             'FULL_CONTROL')
        self.assertEqual(status.split()[0], '200')

    @ossacl(ossacl_only=True)
    def test_object_PUT_copy_with_grantee_permission(self):
        status, headers, body = \
            self._test_object_copy_for_ossacl('test:write', 'READ')
        self.assertEqual(status.split()[0], '200')

    @ossacl(ossacl_only=True)
    def test_object_PUT_copy_without_src_obj_permission(self):
        status, headers, body = \
            self._test_object_copy_for_ossacl('test:write')
        self.assertEqual(status.split()[0], '403')

    @ossacl(ossacl_only=True)
    def test_object_PUT_copy_without_dst_container_permission(self):
        status, headers, body = \
            self._test_object_copy_for_ossacl('test:other', 'READ')
        self.assertEqual(status.split()[0], '403')

    @ossacl(ossacl_only=True)
    def test_object_PUT_copy_empty_src_path(self):
        self.swift.register('PUT', '/v1/AUTH_test/bucket/object',
                            swob.HTTPPreconditionFailed, {}, None)
        status, headers, body = self._test_object_copy_for_ossacl(
            'test:write', 'READ', src_path='')
        self.assertEqual(status.split()[0], '400')


class TestOss2swiftObjNonUTC(TestOss2swiftObj):
    def setUp(self):
        self.orig_tz = os.environ.get('TZ', '')
        os.environ['TZ'] = 'EST+05EDT,M4.1.0,M10.5.0'
        time.tzset()
        super(TestOss2swiftObjNonUTC, self).setUp()

    def tearDown(self):
        super(TestOss2swiftObjNonUTC, self).tearDown()
        os.environ['TZ'] = self.orig_tz
        time.tzset()

if __name__ == '__main__':
    unittest.main()


