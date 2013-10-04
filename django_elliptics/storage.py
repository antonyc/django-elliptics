import requests
import urllib
import logging
import time
import socket
from cStringIO import StringIO
from requests import Timeout

from django import conf
from django.core.files import base, storage

logger = logging.getLogger(__name__)


class BaseError (Exception):
    """Generic error for EllipticsStorage backend."""


class ModeError (BaseError):
    """File operation incompatible with file access mode."""


class HTTPError (BaseError):
    """Elliptics request failed."""


class SaveError (HTTPError):
    """Failed to store file to the backend."""

    def __str__(self):
        response = self.args[0]
        return 'got status code %s while sending to %s' % (
            response.status_code, response.url)


class ReadError (HTTPError):
    """Failed to read from the backend."""

    def __str__(self):
        response = self.args[0]
        return 'got status code %s while reading %s' % (
            response.status_code, response.url)


class TimeoutError(ReadError, SaveError):
    """Timeout error."""

    # ReadError and SaveError override __str__, because they get object with a response to the input.
    # In TimeoutError is impossible to pass the object with a response, therefore overriding the back.
    def __str__(self):
        return super(HTTPError, self).__str__()


class EllipticsStorage (storage.Storage):
    """Django file storage backend for Elliptics via HTTP API.

    Configuration parameters:

    ELLIPTICS_PREFIX - prefix to prepend to the Django names before passing them to the storage.
    ELLIPTICS_PUBLIC_URL - URL pointing to public interface of the Elliptics cluster to serve files from.
    ELLIPTICS_PRIVATE_URL - URL to send modification requests to.
    """

    default_settings = {
        'prefix': '',
        'public_url': 'http://localhost:8080/',
        'private_url': 'http://localhost:9000/',
    }

    def __init__(self, **kwargs):
        self.settings = self._build_settings(kwargs)
        self.session = requests.session()
        self.session.config['keep_alive'] = False
	
    def _build_settings(self, settings):
        return type('settings', (), dict(
            (name, settings.get(name, self._get_default(name)))
            for name in self.default_settings))

    def _get_default(self, name):
        setting_name = 'ELLIPTICS_%s' % (name.upper(),)
        return getattr(conf.settings, setting_name, self.default_settings[name])

    def delete(self, name):
        url = self._make_private_url('delete', name)
        self.session.get(url)

    def exists(self, name):
        '''
        Returns True if the given name already exists in the storage system, or False if the name is available.

        Note: override this method with False return value if you want to overwrite the contents with the given name.
        This will save your application from unnecessary request in the storage system.
        '''
        url = self._make_private_url('get', name)
        r = self.session.head(url)
        return r.status_code == 200

    def url(self, name):
        return self._make_public_url('get', name)

    def _open(self, name, mode):
        return EllipticsFile(name, self, mode)

    def _save(self, name, content, append=False):
        args = {}

        if append:
            args['ioflags'] = 2 # DNET_IO_FLAGS_APPEND = (1<<1)

        url = self._make_private_url('upload', name, **args)
        r = self.session.post(url, data=content)

        if r.status_code != 200:
            raise SaveError(r)

        return name

    def _fetch(self, name):
        url = self._make_private_url('get', name)
        r = self.session.get(url)
        if r.status_code != 200:
            raise ReadError(r)

        return r.content

    def _make_private_url(self, command, *parts, **args):
        return self._make_url(self.settings.private_url, command, self.settings.prefix, *parts, **args)

    def _make_public_url(self, command, *parts, **args):
        return self._make_url(self.settings.public_url, command, self.settings.prefix, *parts, **args)

    def _make_url(self, *parts, **args):
        url = '/'.join(part.strip('/') for part in parts if part)

        if args:
            url += '?' + urllib.urlencode(args)

        return url


class EllipticsFile (base.File):
    def __init__(self, name, storage, mode):
        self.name = name
        self._storage = storage
        self._stream = None

        if 'r' in mode:
            self._mode = 'r'
        elif 'w' in mode:
            self._mode = 'w'
        elif 'a' in mode:
            self._mode = 'a'
        else:
            raise ValueError, 'mode must contain at least one of "r", "w" or "a"'

        if '+' in mode:
            raise ValueError, 'mixed mode access not supported yet.'

    def read(self, num_bytes=None):
        if self._mode != 'r':
            raise ModeError('reading from a file opened for writing.')

        if self._stream is None:
            content = self._storage._fetch(self.name)
            self._stream = StringIO(content)

        if num_bytes is None:
            return self._stream.read()

        return self._stream.read(num_bytes)

    def write(self, content):
        if self._mode not in ('w', 'a'):
            raise ModeError('writing to a file opened for reading.')

        if self._stream is None:
            self._stream = StringIO()

        return self._stream.write(content)

    def close(self):
        if self._stream is None:
            return

        if self._mode in ('w', 'a'):
            self._storage._save(self.name, self._stream.getvalue(), append=(self._mode == 'a'))

    @property
    def size(self):
        raise NotImplementedError

    @property
    def closed(self):
        return bool(self._stream is None)

    def seek(self, offset, mode=0):
        self._stream.seek(offset, mode)


class TimeoutAwareEllipticsStorage(EllipticsStorage):
    timeout_get = getattr(conf.settings, 'ELLIPTICS_GET_CONNECTION_TIMEOUT', 3)
    retries_get = getattr(conf.settings, 'ELLIPTICS_GET_CONNECTION_RETRIES', 3)
    timeout_post = getattr(conf.settings, 'ELLIPTICS_POST_CONNECTION_TIMEOUT', 5)
    retries_post = getattr(conf.settings, 'ELLIPTICS_POST_CONNECTION_RETRIES', 9)

    def _request(self, method, url, *args, **kwargs):
        if method == 'POST':
            return self.session.post(url, *args, **kwargs)
        elif method == 'GET':
            return self.session.get(url, *args, **kwargs)
        else:
            return self.session.head(url, *args, **kwargs)

    def _timeout_request(self, method, url, *args, **kwargs):
        error_message = ''
        if method == 'POST':
            retries = self.retries_post
            timeout = self.timeout_post
        else:
            retries = self.retries_get
            timeout = self.timeout_get

        for retry_count in xrange(retries):
            try:
                started = time.time()
                response = self._request(method, url, *args, timeout=timeout, **kwargs)
            except socket.gaierror as exc:
                raise BaseError('incorrect elliptics request {0} "{1}": {2}'.format(method, url, repr(exc)))
            except Timeout, exception:
                error_message = str(exception)
            else:
                logger.info('%s %s %s timeout=%s retries=%s time=%.4f',
                            method, url, args or '', timeout, retry_count, time.time() - started)
                break
        else:
            logger.error('%s failed attempts of %s to connect to Elliptics (%s %s). Timeout: %s seconds. "%s"',
                         retry_count + 1, retries, method, url, timeout, error_message)
            raise TimeoutError(error_message)

        if retry_count:
            logger.warning('%s failed attempts of %s to connect to Elliptics (%s %s). Timeout: %s seconds. "%s"',
                           retry_count, retries, method, url, timeout, error_message)

        return response

    def _fetch(self, name):
        url = self._make_private_url('get', name)
        response = self._timeout_request('GET', url)

        if response.status_code != 200:
            logger.warning('Elliptics read error status %d, url %s',
                           response.status_code, url, extra={'stack': True})
            raise ReadError(response)

        return response.content

    def _save(self, name, content, append=False):
        args = {}
        if append:
            args['ioflags'] = 2  # DNET_IO_FLAGS_APPEND = (1<<1)

        url = self._make_private_url('upload', name, **args)
        response = self._timeout_request('POST', url, data=content)

        if response.status_code != 200:
            raise SaveError(response)

        return name

    def _make_url(self, *parts, **args):
        """
        Return URL.

        Quotes the path section of a URL.
        @return: str
        """
        if not isinstance(parts, list):
            parts = list(parts)

        for index in xrange(1, len(parts)):
            parts[index] = urllib.quote(parts[index])

        url = super(TimeoutAwareEllipticsStorage, self)._make_url(
            *parts,
            **args
        )
        return url
