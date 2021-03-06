# Copyright: 2006-2011 Brian Harring <ferringb@gmail.com>
# License: GPL2/BSD

__all__ = ("tree",)

from pkgcore.operations.repo import sync_operations


class tree(object):

    operations_kls = sync_operations

    def __init__(self, sync=None):
        object.__setattr__(self, '_syncer', sync)

    @property
    def operations(self):
        return self.get_operations()

    def get_operations(self, observer=None):
        return self.operations_kls(self)

    def _pre_sync(self):
        """Run any required pre-sync repo operations."""

    def _post_sync(self):
        """Run any required post-sync repo operations."""
