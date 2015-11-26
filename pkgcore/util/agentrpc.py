# vim:se fileencoding=utf8 :
# Copyright: 2015 Michał Górny <mgorny@gentoo.org>

import json
import os
import struct

from snakeoil.compatibility import raise_from
from pkgcore.operations import format


class JsonRPC(object):
    def __init__(self, in_pipe, out_pipe):
        self.in_pipe_ = in_pipe
        self.out_pipe_ = out_pipe

    def read(self):
        szs = os.read(self.in_pipe_, 4)
        sz, = struct.unpack('>L', szs)
        s = os.read(self.in_pipe_, sz)
        return json.loads(s.decode('utf8'))

    def write(self, val):
        s = json.dumps(val).encode('utf8')
        szs = struct.pack('>L', len(s))
        os.write(self.out_pipe_, szs)
        os.write(self.out_pipe_, s)


def agent_rpc_call(request):
    if 'CB_AGENT_RPC_FDS' not in os.environ:
        raise_from(format.GenericBuildError(
            "CB_AGENT_RPC_FDS are not provided by cb-agent"))

    rpc_fds = [int(x) for x in os.environ['CB_AGENT_RPC_FDS'].split()]
    rpc = JsonRPC(*rpc_fds)

    rpc.write(request)
    try:
        repl = rpc.read()
        del request['status']
        for k in request:
            assert(repl[k] == request[k])
    except (KeyError, ValueError, AssertionError):
        raise_from(format.GenericBuildError(
            "Malformed cb_agent JSONRPC reply"))

    if repl['status'] != 'success':
        try:
            assert(repl['status'] in ('failure', ))
            assert('error' in repl)
        except (AssertionError, KeyError):
            raise_from(format.GenericBuildError(
                "Malformed cb_agent JSONRPC reply"))

    return repl
