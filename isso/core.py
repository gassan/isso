# -*- encoding: utf-8 -*-

from __future__ import print_function

import io
import os
import time
import binascii
import threading

import socket
import smtplib

from configparser import ConfigParser

try:
    import uwsgi
except ImportError:
    uwsgi = None

from isso.compat import PY2K

if PY2K:
    import thread
else:
    import _thread as thread

from isso import notify, colors
from isso.utils import parse


class IssoParser(ConfigParser):

    @classmethod
    def _total_seconds(cls, td):
        return (td.microseconds + (td.seconds + td.days * 24 * 3600) * 10**6) / 10**6

    def getint(self, section, key):
        try:
            delta = parse.timedelta(self.get(section, key))
        except ValueError:
            return super(IssoParser, self).getint(section, key)
        else:
            try:
                return int(delta.total_seconds())
            except AttributeError:
                return int(IssoParser._total_seconds(delta))


class Config:

    default = [
        "[general]",
        "dbpath = /tmp/isso.db", "secretkey = %r" % binascii.b2a_hex(os.urandom(24)),
        "host = http://localhost:8080/", "passphrase = p@$$w0rd",
        "max-age = 15m",
        "[moderation]",
        "enabled = false",
        "purge-after = 30d",
        "[server]",
        "host = localhost", "port = 8080", "reload = off",
        "[SMTP]",
        "username = ", "password = ",
        "host = localhost", "port = 465", "ssl = on",
        "to = ", "from = ",
        "[guard]",
        "enabled = on",
        "ratelimit = 2"
        ""
    ]

    @classmethod
    def load(cls, configfile):

        rv = IssoParser(allow_no_value=True)
        rv.read_file(io.StringIO(u'\n'.join(Config.default)))

        if configfile:
            rv.read(configfile)

        return rv


def SMTP(conf):

    try:
        print(" * connecting to SMTP server", end=" ")
        mailer = notify.SMTPMailer(conf)
        print("[%s]" % colors.green("ok"))
    except (socket.error, smtplib.SMTPException):
        print("[%s]" % colors.red("failed"))
        mailer = notify.NullMailer()

    return mailer


class Mixin(object):

    def __init__(self, conf):
        self.lock = threading.Lock()

    def notify(self, subject, body, retries=5):
        pass


def threaded(func):

    def dec(self, *args, **kwargs):
        thread.start_new_thread(func, (self, ) + args, kwargs)

    return dec


class ThreadedMixin(Mixin):

    def __init__(self, conf):

        super(ThreadedMixin, self).__init__(conf)

        if conf.getboolean("moderation", "enabled"):
            self.purge(conf.getint("moderation", "purge-after"))

        self.mailer = SMTP(conf)


    @threaded
    def notify(self, subject, body, retries=5):

        for x in range(retries):
            try:
                self.mailer.sendmail(subject, body)
            except Exception:
                time.sleep(60)
            else:
                break

    @threaded
    def purge(self, delta):
        while True:
            with self.lock:
                self.db.comments.purge(delta)
            time.sleep(delta)


class uWSGIMixin(Mixin):

    def __init__(self, conf):

        super(uWSGIMixin, self).__init__(conf)

        class Lock():

            def __enter__(self):
                while uwsgi.queue_get(0) == "LOCK":
                    time.sleep(0.01)

                uwsgi.queue_set(0, "LOCK")

            def __exit__(self, exc_type, exc_val, exc_tb):
                uwsgi.queue_pop()

        def spooler(args):
            try:
                self.mailer.sendmail(args["subject"].decode('utf-8'), args["body"].decode('utf-8'))
            except smtplib.SMTPConnectError:
                return uwsgi.SPOOL_RETRY
            else:
                return uwsgi.SPOOL_OK

        self.lock = Lock()
        self.mailer = SMTP(conf)
        uwsgi.spooler = spooler

        timedelta = conf.getint("moderation", "purge-after")
        purge = lambda signum: self.db.comments.purge(timedelta)
        uwsgi.register_signal(1, "", purge)
        uwsgi.add_timer(1, timedelta)

        # run purge once
        purge(1)

    def notify(self, subject, body, retries=5):
        uwsgi.spool({"subject": subject.encode('utf-8'), "body": body.encode('utf-8')})
