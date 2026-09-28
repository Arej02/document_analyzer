from pkg.alpha.util import Greeter, helper
from pkg.beta import util as beta_util


def run():
    greeter = Greeter()
    greeter.greet("world")
    return helper(1) + beta_util.helper(2)


def ping(n):
    if n > 0:
        return pong(n - 1)
    return n


def pong(n):
    return ping(n)
