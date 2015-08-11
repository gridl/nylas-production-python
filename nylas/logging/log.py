"""
Logging configuration.

Mostly based off http://www.structlog.org/en/0.4.1/standard-library.html.

"""
import sys
import traceback
import logging
import logging.handlers

import gevent
import colorlog
import structlog

from structlog.threadlocal import wrap_dict


MAX_EXCEPTION_LENGTH = 10000


def find_first_app_frame_and_name(ignores=None):
    """
    Remove ignorable calls and return the relevant app frame. Borrowed from
    structlog, but fixes an issue when the stack includes an 'exec' statement
    or similar (f.f_globals doesn't have a '__name__' key in that case).

    Parameters
    ----------
    ignores: list, optional
        Additional names with which the first frame must not start.

    Returns
    -------
    tuple of (frame, name)
    """
    ignores = ignores or []
    f = sys._getframe()
    name = f.f_globals.get('__name__')
    while f is not None and (name is None or
                             any(name.startswith(i) for i in ignores)):
        f = f.f_back
        name = f.f_globals.get('__name__')
    return f, name


def _record_level(logger, name, event_dict):
    """Processor that records the log level ('info', 'warning', etc.) in the
    structlog event dictionary."""
    event_dict['level'] = name
    return event_dict


def _record_module(logger, name, event_dict):
    """Processor that records the module and line where the logging call was
    invoked."""
    f, name = find_first_app_frame_and_name(
        ignores=['structlog', 'inbox.log', 'inbox.sqlalchemy_ext.util',
                 'inbox.models.session', 'sqlalchemy'])
    event_dict['module'] = '{}:{}'.format(name, f.f_lineno)
    return event_dict


def _format_string_renderer(_, __, event_dict):
    """Processor to be used with the BoundLogger class below to properly handle
    messages of the form
    `log.info('some message to format %s', some_value')`."""
    positional_args = event_dict.get('_positional_args')
    if positional_args:
        event_dict['event'] = event_dict['event'] % positional_args
        del event_dict['_positional_args']
    return event_dict


def safe_format_exception(etype, value, tb, limit=None):
    """Similar to structlog._format_exception, but truncate the exception part.
    This is because SQLAlchemy exceptions can sometimes have ludicrously large
    exception strings."""
    if tb:
        list = ['Traceback (most recent call last):\n']
        list = list + traceback.format_tb(tb, limit)
    else:
        list = []
    exc_only = traceback.format_exception_only(etype, value)
    # Normally exc_only is a list containing a single string.  For syntax
    # errors it may contain multiple elements, but we don't really need to
    # worry about that here.
    exc_only[0] = exc_only[0][:MAX_EXCEPTION_LENGTH]
    list = list + exc_only
    return '\t'.join(list)


def _safe_exc_info_renderer(_, __, event_dict):
    """Processor that formats exception info safely."""
    exc_info = event_dict.pop('exc_info', None)
    if exc_info:
        if not isinstance(exc_info, tuple):
            exc_info = sys.exc_info()
        event_dict['exception'] = safe_format_exception(*exc_info)
    return event_dict


class BoundLogger(structlog._base.BoundLoggerBase):
    """Adaptation of structlog.stdlib.BoundLogger to accept positional
    arguments. See https://github.com/hynek/structlog/pull/23/
    (we can remove this if that ever gets merged)."""
    def debug(self, event=None, *args, **kw):
        return self._proxy_to_logger('debug', event, *args, **kw)

    def info(self, event=None, *args, **kw):
        return self._proxy_to_logger('info', event, *args, **kw)

    def warning(self, event=None, *args, **kw):
        return self._proxy_to_logger('warning', event, *args, **kw)

    warn = warning

    def error(self, event=None, *args, **kw):
        return self._proxy_to_logger('error', event, *args, **kw)

    def critical(self, event=None, *args, **kw):
        return self._proxy_to_logger('critical', event, *args, **kw)

    def exception(self, event=None, *args, **kw):
        kw['exc_info'] = True
        return self._proxy_to_logger('error', event, *args, **kw)

    def _proxy_to_logger(self, method_name, event=None, *event_args,
                         **event_kw):
        if event_args:
            event_kw['_positional_args'] = event_args
        event_kw['greenlet_id'] = id(gevent.getcurrent())
        return super(BoundLogger, self)._proxy_to_logger(method_name, event,
                                                         **event_kw)

structlog.configure(
    processors=[
        structlog.stdlib.filter_by_level,
        structlog.processors.TimeStamper(fmt='iso', utc=True),
        structlog.processors.StackInfoRenderer(),
        _safe_exc_info_renderer,
        _record_module,
        _record_level,
        _format_string_renderer,
        structlog.processors.JSONRenderer(),
    ],
    context_class=wrap_dict(dict),
    logger_factory=structlog.stdlib.LoggerFactory(),
    wrapper_class=BoundLogger,
    cache_logger_on_first_use=True,
)
get_logger = structlog.get_logger


def configure_logging(log_level=None):
    """ Idempotently configure logging.

    Infers options based on whether or not the output is a TTY.

    Sets the root log level to DEBUG if not otherwise specified.

    """

    if log_level is None:
        log_level = 10

    tty_handler = logging.StreamHandler(sys.stdout)
    if sys.stdout.isatty():
        # Use a more human-friendly format.
        formatter = colorlog.ColoredFormatter(
            '%(log_color)s[%(levelname)s]%(reset)s %(message)s',
            reset=True, log_colors={'DEBUG': 'cyan', 'INFO': 'green',
                                    'WARNING': 'yellow', 'ERROR': 'red',
                                    'CRITICAL': 'red'})
    else:
        formatter = logging.Formatter('%(message)s')
    tty_handler.setFormatter(formatter)
    tty_handler._nylas = True

    # Configure the root logger.
    root_logger = logging.getLogger()
    for handler in root_logger.handlers:
        # If the handler was previously installed, remove it so that repeated
        # calls to configure_logging() are idempotent.
        if getattr(handler, '_nylas', False):
            root_logger.removeHandler(handler)
    root_logger.addHandler(tty_handler)
    # Set loglevel DEBUG if config value is missing.
    root_logger.setLevel(log_level)