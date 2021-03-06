# Copyright: 2011 Brian Harring <ferringb@gmail.com>
# License: GPL2/BSD 3 clause

from collections import deque
import threading
from types import GeneratorType
import queue

from snakeoil.compatibility import IGNORED_EXCEPTIONS
from snakeoil.demandload import demandload

demandload(
    'multiprocessing:cpu_count',
)


def reclaim_threads(threads):
    for x in threads:
        try:
            x.join()
        except IGNORED_EXCEPTIONS:
            raise
        except Exception as e:
            # should do something better here
            pass


def map_async(iterable, functor, *args, **kwds):
    per_thread_args = kwds.pop("per_thread_args", lambda: ())
    per_thread_kwds = kwds.pop("per_thread_kwds", lambda: {})
    parallelism = kwds.pop("threads", None)
    if parallelism is None:
        parallelism = cpu_count()

    if hasattr(iterable, '__len__'):
        # if there are less items than parallelism, don't
        # spawn pointless threads.
        parallelism = max(min(len(iterable), parallelism), 0)

    # note we allow an infinite queue since .put below is blocking, and won't
    # return till it succeeds (regardless of signal) as such, we do it this way
    # to ensure the put succeeds, then the keyboardinterrupt can be seen.
    q = queue.Queue()
    results = deque()
    kill = threading.Event()
    kill.clear()

    def iter_queue(kill, qlist, empty_signal):
        while not kill.isSet():
            item = qlist.get()
            if item is empty_signal:
                return
            yield item

    def worker(*args):
        result = functor(*args)
        if result is not None:
            # avoid appending chars from a string into results
            if isinstance(result, GeneratorType):
                results.extend(result)
            else:
                results.append(result)

    empty_signal = object()
    threads = []
    for x in range(parallelism):
        tkwds = kwds.copy()
        tkwds.update(per_thread_kwds())
        targs = (iter_queue(kill, q, empty_signal),) + args + per_thread_args()
        threads.append(threading.Thread(target=worker, args=targs, kwargs=tkwds))
    try:
        try:
            for x in threads:
                x.start()
            # now we feed the queue.
            for data in iterable:
                q.put(data)
        except:
            kill.set()
            raise
    finally:
        for x in range(parallelism):
            q.put(empty_signal)

        reclaim_threads(threads)

    return results
