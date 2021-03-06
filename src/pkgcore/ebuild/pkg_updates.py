# Copyright: 2011 Brian Harring <ferringb@gmail.com>
# License: GPL2/BSD 3 clause

from collections import deque, defaultdict
from operator import itemgetter

from snakeoil.demandload import demandload, demand_compile_regexp
from snakeoil.fileutils import readlines
from snakeoil.osutils import listdir_files, pjoin
from snakeoil.sequences import iflatten_instance

from pkgcore.ebuild.atom import atom

demandload('pkgcore.log:logger')

demand_compile_regexp("valid_updates_re", r"^(\d)Q-(\d{4})$")


def _scan_directory(path):
    files = []
    for x in listdir_files(path):
        match = valid_updates_re.match(x)
        if match is not None:
            files.append(((match.group(2), match.group(1)), x))
    files.sort(key=itemgetter(0))
    return [x[1] for x in files]


def read_updates(path):
    def f():
        d = deque()
        return [d,d]
    # mods tracks the start point [0], and the tail, [1].
    # via this, pkg moves into a specific pkg can pick up
    # changes past that point, while ignoring changes prior
    # to that point.
    # Afterwards, we flatten it to get a per cp chain of commands.
    # no need to do lookups basically, although we do need to
    # watch for cycles.
    mods = defaultdict(f)
    moved = {}

    try:
        for fp in _scan_directory(path):
            _process_update(readlines(pjoin(path, fp)), fp, mods, moved)
    except FileNotFoundError:
        pass

    # force a walk of the tree, flattening it
    commands = {k: list(iflatten_instance(v[0], tuple)) for k,v in mods.items()}
    # filter out empty nodes.
    commands = {k: v for k,v in commands.items() if v}

    return commands


def _process_update(sequence, filename, mods, moved):
    for lineno, raw_line in enumerate(sequence, 1):
        line = raw_line.split()
        if line[0] == 'move':
            if len(line) != 3:
                logger.error(
                    'file %r: %r on line %s: bad move form',
                    filename, raw_line, lineno)
                continue
            src, trg = atom(line[1]), atom(line[2])
            if src.fullver is not None:
                logger.error(
                    "file %r: %r on line %s: atom %s must be versionless",
                    filename, raw_line, lineno, src)
                continue
            elif trg.fullver is not None:
                logger.error(
                    "file %r: %r on line %s: atom %s must be versionless",
                    filename, raw_line, lineno, trg)
                continue

            if src.key in moved:
                logger.warning(
                    "file %r: %r on line %s: %s was already moved to %s,"
                    " this line is redundant",
                    filename, raw_line, lineno, src, moved[src.key])
                continue

            d = deque()
            mods[src.key][1].extend([('move', src, trg), d])
            # start essentially a new checkpoint in the trg
            mods[trg.key][1].append(d)
            mods[trg.key][1] = d
            moved[src.key] = trg

        elif line[0] == 'slotmove':
            if len(line) != 4:
                logger.error(
                    'file %r: %r on line %s: bad slotmove form',
                    filename, raw_line, lineno)
                continue
            src = atom(line[1])

            if src.key in moved:
                logger.warning(
                    "file %r: %r on line %s: %s was already moved to %s, "
                    "this line is redundant",
                    filename, raw_line, lineno, src, moved[src.key])
                continue
            elif src.slot is not None:
                logger.error(
                    "file %r: %r on line %s: slotted atom makes no sense "
                    "for slotmoves",
                    filename, lineno, raw_line)
                continue

            src_slot = atom(f'{src}:{line[2]}')
            trg_slot = atom(f'{src.key}:{line[3]}')

            mods[src.key][1].append(('slotmove', src_slot, line[3]))
