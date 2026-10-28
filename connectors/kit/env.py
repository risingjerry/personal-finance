"""Environment lookup that tests can redirect with their own mapping."""

import os


def getenv(name, environ=None):
    return (os.environ if environ is None else environ).get(name)
