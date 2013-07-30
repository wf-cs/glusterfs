import re
import os
import sys
import stat
import time
import fcntl
import errno
import types
import struct
import socket
import logging
import tempfile
import threading
import subprocess
from errno import EEXIST, ENOENT, ENODATA, ENOTDIR, ELOOP, EISDIR, ENOTEMPTY
from select import error as SelectError

from gconf import gconf
import repce
from repce import RepceServer, RepceClient
from  master import gmaster_builder
import syncdutils
from syncdutils import GsyncdError, select, privileged, boolify, funcode
from syncdutils import umask, entry2pb, gauxpfx, errno_wrap

UrlRX  = re.compile('\A(\w+)://([^ *?[]*)\Z')
HostRX = re.compile('[a-z\d](?:[a-z\d.-]*[a-z\d])?', re.I)
UserRX = re.compile("[\w!\#$%&'*+-\/=?^_`{|}~]+")

def sup(x, *a, **kw):
    """a rubyesque "super" for python ;)

    invoke caller method in parent class with given args.
    """
    return getattr(super(type(x), x), sys._getframe(1).f_code.co_name)(*a, **kw)

def desugar(ustr):
    """transform sugared url strings to standard <scheme>://<urlbody> form

    parsing logic enforces the constraint that sugared forms should contatin
    a ':' or a '/', which ensures that sugared urls do not conflict with
    gluster volume names.
    """
    m = re.match('([^:]*):(.*)', ustr)
    if m:
        if not m.groups()[0]:
            return "gluster://localhost" + ustr
        elif '@' in m.groups()[0] or re.search('[:/]', m.groups()[1]):
            return "ssh://" + ustr
        else:
            return "gluster://" + ustr
    else:
        if ustr[0] != '/':
            raise GsyncdError("cannot resolve sugared url '%s'" % ustr)
        ap = os.path.normpath(ustr)
        if ap.startswith('//'):
            ap = ap[1:]
        return "file://" + ap

def gethostbyname(hnam):
    """gethostbyname wrapper"""
    try:
        return socket.gethostbyname(hnam)
    except socket.gaierror:
        ex = sys.exc_info()[1]
        raise GsyncdError("failed to resolve %s: %s" % \
                          (hnam, ex.strerror))

def parse_url(ustr):
    """instantiate an url object by scheme-to-class dispatch

    The url classes taken into consideration are the ones in
    this module whose names are full-caps.
    """
    m = UrlRX.match(ustr)
    if not m:
        ustr = desugar(ustr)
    m = UrlRX.match(ustr)
    if not m:
        raise GsyncdError("malformed url")
    sch, path = m.groups()
    this = sys.modules[__name__]
    if not hasattr(this, sch.upper()):
        raise GsyncdError("unknown url scheme " + sch)
    return getattr(this, sch.upper())(path)


class _MetaXattr(object):
    """singleton class, a lazy wrapper around the
    libcxattr module

    libcxattr (a heavy import due to ctypes) is
    loaded only when when the single
    instance is tried to be used.

    This reduces runtime for those invocations
    which do not need filesystem manipulation
    (eg. for config, url parsing)
    """

    def __getattr__(self, meth):
        from libcxattr import Xattr as LXattr
        xmeth = [ m for m in dir(LXattr) if m[0] != '_' ]
        if not meth in xmeth:
            return
        for m in xmeth:
            setattr(self, m, getattr(LXattr, m))
        return getattr(self, meth)

class _MetaChangelog(object):
    def __getattr__(self, meth):
        from libgfchangelog import Changes as LChanges
        xmeth = [ m for m in dir(LChanges) if m[0] != '_' ]
        if not meth in xmeth:
            return
        for m in xmeth:
            setattr(self, m, getattr(LChanges, m))
        return getattr(self, meth)

Xattr = _MetaXattr()
Changes = _MetaChangelog()


class Popen(subprocess.Popen):
    """customized subclass of subprocess.Popen with a ring
    buffer for children error output"""

    @classmethod
    def init_errhandler(cls):
        """start the thread which handles children's error output"""
        cls.errstore = {}
        def tailer():
            while True:
                errstore = cls.errstore.copy()
                try:
                    poe, _ ,_ = select([po.stderr for po in errstore], [], [], 1)
                except (ValueError, SelectError):
                    continue
                for po in errstore:
                    if po.stderr not in poe:
                        continue
                    po.lock.acquire()
                    try:
                        if po.on_death_row:
                            continue
                        la = errstore[po]
                        try:
                            fd = po.stderr.fileno()
                        except ValueError:  # file is already closed
                            continue
                        l = os.read(fd, 1024)
                        if not l:
                            continue
                        tots = len(l)
                        for lx in la:
                            tots += len(lx)
                        while tots > 1<<20 and la:
                            tots -= len(la.pop(0))
                        la.append(l)
                    finally:
                        po.lock.release()
        t = syncdutils.Thread(target = tailer)
        t.start()
        cls.errhandler = t

    @classmethod
    def fork(cls):
        """fork wrapper that restarts errhandler thread in child"""
        pid = os.fork()
        if not pid:
            cls.init_errhandler()
        return pid

    def __init__(self, args, *a, **kw):
        """customizations for subprocess.Popen instantiation

        - 'close_fds' is taken to be the default
        - if child's stderr is chosen to be managed,
          register it with the error handler thread
        """
        self.args = args
        if 'close_fds' not in kw:
            kw['close_fds'] = True
        self.lock = threading.Lock()
        self.on_death_row = False
        try:
            sup(self, args, *a, **kw)
        except:
            ex = sys.exc_info()[1]
            if not isinstance(ex, OSError):
                raise
            raise GsyncdError("""execution of "%s" failed with %s (%s)""" % \
                              (args[0], errno.errorcode[ex.errno], os.strerror(ex.errno)))
        if kw.get('stderr') == subprocess.PIPE:
            assert(getattr(self, 'errhandler', None))
            self.errstore[self] = []

    def errlog(self):
        """make a log about child's failure event"""
        filling = ""
        if self.elines:
            filling = ", saying:"
        logging.error("""command "%s" returned with %s%s""" % \
                      (" ".join(self.args), repr(self.returncode), filling))
        lp = ''
        def logerr(l):
            logging.error(self.args[0] + "> " + l)
        for l in self.elines:
            ls = l.split('\n')
            ls[0] = lp + ls[0]
            lp = ls.pop()
            for ll in ls:
                logerr(ll)
        if lp:
            logerr(lp)

    def errfail(self):
        """fail nicely if child did not terminate with success"""
        self.errlog()
        syncdutils.finalize(exval = 1)

    def terminate_geterr(self, fail_on_err = True):
        """kill child, finalize stderr harvesting (unregister
        from errhandler, set up .elines), fail on error if
        asked for
        """
        self.lock.acquire()
        try:
            self.on_death_row = True
        finally:
            self.lock.release()
        elines = self.errstore.pop(self)
        if self.poll() == None:
            self.terminate()
            if self.poll() == None:
                time.sleep(0.1)
                self.kill()
                self.wait()
        while True:
            if not select([self.stderr],[],[],0.1)[0]:
                break
            b = os.read(self.stderr.fileno(), 1024)
            if b:
                elines.append(b)
            else:
                break
        self.stderr.close()
        self.elines = elines
        if fail_on_err and self.returncode != 0:
            self.errfail()


class Server(object):
    """singleton implemening those filesystem access primitives
       which are needed for geo-replication functionality

    (Singleton in the sense it's a class which has only static
    and classmethods and is used directly, without instantiation.)
    """

    GX_NSPACE_PFX = (privileged() and "trusted" or "system")
    GX_NSPACE = GX_NSPACE_PFX + ".glusterfs"
    NTV_FMTSTR = "!" + "B"*19 + "II"
    FRGN_XTRA_FMT = "I"
    FRGN_FMTSTR = NTV_FMTSTR + FRGN_XTRA_FMT
    GX_GFID_CANONICAL_LEN = 37 # canonical gfid len + '\0'

    local_path = ''

    @classmethod
    def _fmt_mknod(cls, l):
        return "!II%dsI%dsIII" % (cls.GX_GFID_CANONICAL_LEN, l+1)
    @classmethod
    def _fmt_mkdir(cls, l):
        return "!II%dsI%dsII" % (cls.GX_GFID_CANONICAL_LEN, l+1)
    @classmethod
    def _fmt_symlink(cls, l1, l2):
        return "!II%dsI%ds%ds" % (cls.GX_GFID_CANONICAL_LEN, l1+1, l2+1)

    def _pathguard(f):
        """decorator method that checks
        the path argument of the decorated
        functions to make sure it does not
        point out of the managed tree
        """

        fc = funcode(f)
        pi = list(fc.co_varnames).index('path')
        def ff(*a):
            path = a[pi]
            ps = path.split('/')
            if path[0] == '/' or '..' in ps:
                raise ValueError('unsafe path')
            a = list(a)
            a[pi] = os.path.join(a[0].local_path, path)
            return f(*a)
        return ff

    @classmethod
    @_pathguard
    def entries(cls, path):
        """directory entries in an array"""
        # prevent symlinks being followed
        if not stat.S_ISDIR(os.lstat(path).st_mode):
            raise OSError(ENOTDIR, os.strerror(ENOTDIR))
        return os.listdir(path)

    @classmethod
    @_pathguard
    def purge(cls, path, entries=None):
        """force-delete subtrees

        If @entries is not specified, delete
        the whole subtree under @path (including
        @path).

        Otherwise, @entries should be a
        a sequence of children of @path, and
        the effect is identical with a joint
        @entries-less purge on them, ie.

        for e in entries:
            cls.purge(os.path.join(path, e))
        """
        me_also = entries == None
        if not entries:
            try:
                # if it's a symlink, prevent
                # following it
                try:
                    os.unlink(path)
                    return
                except OSError:
                    ex = sys.exc_info()[1]
                    if ex.errno == EISDIR:
                        entries = os.listdir(path)
                    else:
                        raise
            except OSError:
                ex = sys.exc_info()[1]
                if ex.errno in (ENOTDIR, ENOENT, ELOOP):
                    try:
                        os.unlink(path)
                        return
                    except OSError:
                        ex = sys.exc_info()[1]
                        if ex.errno == ENOENT:
                            return
                        raise
                else:
                    raise
        for e in entries:
            cls.purge(os.path.join(path, e))
        if me_also:
            os.rmdir(path)

    @classmethod
    @_pathguard
    def _create(cls, path, ctor):
        """path creation backend routine"""
        try:
            ctor(path)
        except OSError:
            ex = sys.exc_info()[1]
            if ex.errno == EEXIST:
                cls.purge(path)
                return ctor(path)
            raise

    @classmethod
    @_pathguard
    def mkdir(cls, path):
        cls._create(path, os.mkdir)

    @classmethod
    @_pathguard
    def symlink(cls, lnk, path):
        cls._create(path, lambda p: os.symlink(lnk, p))

    @classmethod
    @_pathguard
    def xtime(cls, path, uuid):
        """query xtime extended attribute

        Return xtime of @path for @uuid as a pair of integers.
        "Normal" errors due to non-existent @path or extended attribute
        are tolerated and errno is returned in such a case.
        """

        try:
            return struct.unpack('!II', Xattr.lgetxattr(path, '.'.join([cls.GX_NSPACE, uuid, 'xtime']), 8))
        except OSError:
            ex = sys.exc_info()[1]
            if ex.errno in (ENOENT, ENODATA, ENOTDIR):
                return ex.errno
            else:
                raise

    @classmethod
    def gfid(cls, gfidpath):
        return errno_wrap(Xattr.lgetxattr, [gfidpath, 'glusterfs.gfid', cls.GX_GFID_CANONICAL_LEN], [ENOENT])

    @classmethod
    def node_uuid(cls, path='.'):
        try:
            uuid_l = Xattr.lgetxattr_buf(path, '.'.join([cls.GX_NSPACE, 'node-uuid']))
            return uuid_l[:-1].split(' ')
        except OSError:
            raise

    @classmethod
    def xtime_vec(cls, path, *uuids):
        """vectored version of @xtime

        accepts a list of uuids and returns a dictionary
        with uuid as key(s) and xtime as value(s)
        """
        xt = {}
        for uuid in uuids:
            xtu = cls.xtime(path, uuid)
            if xtu == ENODATA:
                xtu = None
            if isinstance(xtu, int):
                return xtu
            xt[uuid] = xtu
        return xt

    @classmethod
    @_pathguard
    def set_xtime(cls, path, uuid, mark):
        """set @mark as xtime for @uuid on @path"""
        Xattr.lsetxattr(path, '.'.join([cls.GX_NSPACE, uuid, 'xtime']), struct.pack('!II', *mark))

    @classmethod
    def set_xtime_vec(cls, path, mark_dct):
        """vectored (or dictered) version of set_xtime

        ignore values that match @ignore
        """
        for u,t in mark_dct.items():
            cls.set_xtime(path, u, t)

    @classmethod
    def entry_ops(cls, entries):
        pfx = gauxpfx()
        logging.debug('entries: %s' % repr(entries))
        # regular file
        def entry_pack_reg(gf, bn, st):
            blen = len(bn)
            mo = st['mode']
            return struct.pack(cls._fmt_mknod(blen),
                               st['uid'], st['gid'],
                               gf, mo, bn,
                               stat.S_IMODE(mo), 0, umask())
        # mkdir
        def entry_pack_mkdir(gf, bn, st):
            blen = len(bn)
            mo = st['mode']
            return struct.pack(cls._fmt_mkdir(blen),
                               st['uid'], st['gid'],
                               gf, mo, bn,
                               stat.S_IMODE(mo), umask())
        #symlink
        def entry_pack_symlink(gf, bn, lnk, st):
            blen = len(bn)
            llen = len(lnk)
            return struct.pack(cls._fmt_symlink(blen, llen),
                               st['uid'], st['gid'],
                               gf, st['mode'], bn, lnk)
        def entry_purge(entry, gfid):
            # This is an extremely racy code and needs to be fixed ASAP.
            # The GFID check here is to be sure that the pargfid/bname
            # to be purged is the GFID gotten from the changelog.
            # (a stat(changelog_gfid) would also be valid here)
            # The race here is between the GFID check and the purge.
            disk_gfid = cls.gfid(entry)
            if isinstance(disk_gfid, int):
                return
            if not gfid == disk_gfid:
                return
            er = errno_wrap(os.unlink, [entry], [ENOENT, EISDIR])
            if isinstance(er, int):
                if er == EISDIR:
                    er = errno_wrap(os.rmdir, [entry], [ENOENT, ENOTEMPTY])
                    if er == ENOTEMPTY:
                        return er
        for e in entries:
            blob = None
            op = e['op']
            gfid = e['gfid']
            entry = e['entry']
            (pg, bname) = entry2pb(entry)
            if op in ['RMDIR', 'UNLINK']:
                while True:
                    er = entry_purge(entry, gfid)
                    if isinstance(er, int):
                        time.sleep(1)
                    else:
                        break
            elif op == 'CREATE':
                blob = entry_pack_reg(gfid, bname, e['stat'])
            elif op == 'MKDIR':
                blob = entry_pack_mkdir(gfid, bname, e['stat'])
            elif op == 'LINK':
                errno_wrap(os.link, [os.path.join(pfx, gfid), entry], [ENOENT, EEXIST])
            elif op == 'SYMLINK':
                blob = entry_pack_symlink(gfid, bname, e['link'], e['stat'])
            elif op == 'RENAME':
                en = e['entry1']
                errno_wrap(os.rename, [entry, en], [ENOENT, EEXIST])
            if blob:
                errno_wrap(Xattr.lsetxattr_l, [pg, 'glusterfs.gfid.newfile', blob], [ENOENT, EEXIST])

    @classmethod
    def changelog_register(cls, cl_brick, cl_dir, cl_log, cl_level, retries = 0):
        Changes.cl_register(cl_brick, cl_dir, cl_log, cl_level, retries)

    @classmethod
    def changelog_scan(cls):
        Changes.cl_scan()

    @classmethod
    def changelog_getchanges(cls):
        return Changes.cl_getchanges()

    @classmethod
    def changelog_done(cls, clfile):
        Changes.cl_done(clfile)

    @classmethod
    @_pathguard
    def setattr(cls, path, adct):
        """set file attributes

        @adct is a dict, where 'own', 'mode' and 'times'
        keys are looked for and values used to perform
        chown, chmod or utimes on @path.
        """
        own = adct.get('own')
        if own:
            os.lchown(path, *own)
        mode = adct.get('mode')
        if mode:
            os.chmod(path, stat.S_IMODE(mode))
        times = adct.get('times')
        if times:
            os.utime(path, times)

    @staticmethod
    def pid():
        return os.getpid()

    last_keep_alive = 0
    @classmethod
    def keep_alive(cls, dct):
        """process keepalive messages.

        Return keep-alive counter (number of received keep-alive
        messages).

        Now the "keep-alive" message can also have a payload which is
        used to set a foreign volume-mark on the underlying file system.
        """
        if dct:
            key = '.'.join([cls.GX_NSPACE, 'volume-mark', dct['uuid']])
            val = struct.pack(cls.FRGN_FMTSTR,
                              *(dct['version']  +
                                tuple(int(x,16) for x in re.findall('(?:[\da-f]){2}', dct['uuid'])) +
                                (dct['retval'],) + dct['volume_mark'][0:2] + (dct['timeout'],)))
            Xattr.lsetxattr('.', key, val)
        cls.last_keep_alive += 1
        return cls.last_keep_alive

    @staticmethod
    def version():
        """version used in handshake"""
        return 1.0


class SlaveLocal(object):
    """mix-in class to implement some factes of a slave server

    ("mix-in" is sort of like "abstract class", ie. it's not
    instantiated just included in the ancesty DAG. I use "mix-in"
    to indicate that it's not used as an abstract base class,
    rather just taken in to implement additional functionality
    on the basis of the assumed availability of certain interfaces.)
    """

    def can_connect_to(self, remote):
        """determine our position in the connectibility matrix"""
        return not remote

    def service_loop(self):
        """start a RePCe server serving self's server

        stop servicing if a timeout is configured and got no
        keep-alime in that inteval
        """

        if boolify(gconf.use_rsync_xattrs) and not privileged():
            raise GsyncdError("using rsync for extended attributes is not supported")

        repce = RepceServer(self.server, sys.stdin, sys.stdout, int(gconf.sync_jobs))
        t = syncdutils.Thread(target=lambda: (repce.service_loop(),
                                              syncdutils.finalize()))
        t.start()
        logging.info("slave listening")
        if gconf.timeout and int(gconf.timeout) > 0:
            while True:
                lp = self.server.last_keep_alive
                time.sleep(int(gconf.timeout))
                if lp == self.server.last_keep_alive:
                    logging.info("connection inactive for %d seconds, stopping" % int(gconf.timeout))
                    break
        else:
            select((), (), ())

class SlaveRemote(object):
    """mix-in class to implement an interface to a remote slave"""

    def connect_remote(self, rargs=[], **opts):
        """connects to a remote slave

        Invoke an auxiliary utility (slave gsyncd, possibly wrapped)
        which sets up the connection and set up a RePCe client to
        communicate throuh its stdio.
        """
        slave = opts.get('slave', self.url)
        extra_opts = []
        so = getattr(gconf, 'session_owner', None)
        if so:
            extra_opts += ['--session-owner', so]
        if boolify(gconf.use_rsync_xattrs):
            extra_opts.append('--use-rsync-xattrs')
        po = Popen(rargs + gconf.remote_gsyncd.split() + extra_opts + \
                   ['-N', '--listen', '--timeout', str(gconf.timeout), slave],
                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        gconf.transport = po
        return self.start_fd_client(po.stdout, po.stdin, **opts)

    def start_fd_client(self, i, o, **opts):
        """set up RePCe client, handshake with server

        It's cut out as a separate method to let
        subclasses hook into client startup
        """
        self.server = RepceClient(i, o)
        rv = self.server.__version__()
        exrv = {'proto': repce.repce_version, 'object': Server.version()}
        da0 = (rv, exrv)
        da1 = ({}, {})
        for i in range(2):
            for k, v in da0[i].iteritems():
                da1[i][k] = int(v)
        if da1[0] != da1[1]:
            raise GsyncdError("RePCe major version mismatch: local %s, remote %s" % (exrv, rv))

    def rsync(self, files, *args):
        """invoke rsync"""
        if not files:
            raise GsyncdError("no files to sync")
        logging.debug("files: " + ", ".join(files))
        argv = gconf.rsync_command.split() + \
               ['-avR0', '--inplace', '--files-from=-', '--super','--stats', '--numeric-ids', '--no-implied-dirs'] + \
               gconf.rsync_options.split() + (boolify(gconf.use_rsync_xattrs) and ['--xattrs'] or []) + \
               ['.'] + list(args)
        po = Popen(argv, stdin=subprocess.PIPE,stderr=subprocess.PIPE)
        for f in files:
            po.stdin.write(f)
            po.stdin.write('\0')

        po.stdin.close()
        po.wait()
        po.terminate_geterr(fail_on_err = False)

        return po


class AbstractUrl(object):
    """abstract base class for url scheme classes"""

    def __init__(self, path, pattern):
        m = re.search(pattern, path)
        if not m:
            raise GsyncdError("malformed path")
        self.path = path
        return m.groups()

    @property
    def scheme(self):
        return type(self).__name__.lower()

    def canonical_path(self):
        return self.path

    def get_url(self, canonical=False, escaped=False):
        """format self's url in various styles"""
        if canonical:
            pa = self.canonical_path()
        else:
            pa = self.path
        u = "://".join((self.scheme, pa))
        if escaped:
            u = syncdutils.escape(u)
        return u

    @property
    def url(self):
        return self.get_url()


  ### Concrete resource classes ###


class FILE(AbstractUrl, SlaveLocal, SlaveRemote):
    """scheme class for file:// urls

    can be used to represent a file slave server
    on slave side, or interface to a remote file
    file server on master side
    """

    class FILEServer(Server):
        """included server flavor"""
        pass

    server = FILEServer

    def __init__(self, path):
        sup(self, path, '^/')

    def connect(self):
        """inhibit the resource beyond"""
        os.chdir(self.path)

    def rsync(self, files):
        return sup(self, files, self.path)


class GLUSTER(AbstractUrl, SlaveLocal, SlaveRemote):
    """scheme class for gluster:// urls

    can be used to represent a gluster slave server
    on slave side, or interface to a remote gluster
    slave on master side, or to represent master
    (slave-ish features come from the mixins, master
    functionality is outsourced to GMaster from master)
    """

    class GLUSTERServer(Server):
        "server enhancements for a glusterfs backend"""

        @classmethod
        def _attr_unpack_dict(cls, xattr, extra_fields = ''):
            """generic volume mark fetching/parsing backed"""
            fmt_string = cls.NTV_FMTSTR + extra_fields
            buf = Xattr.lgetxattr('.', xattr, struct.calcsize(fmt_string))
            vm = struct.unpack(fmt_string, buf)
            m = re.match('(.{8})(.{4})(.{4})(.{4})(.{12})', "".join(['%02x' % x for x in vm[2:18]]))
            uuid = '-'.join(m.groups())
            volinfo = {  'version': vm[0:2],
                         'uuid'   : uuid,
                         'retval' : vm[18],
                         'volume_mark': vm[19:21],
                      }
            if extra_fields:
                return volinfo, vm[-len(extra_fields):]
            else:
                return volinfo

        @classmethod
        def foreign_volume_infos(cls):
            """return list of valid (not expired) foreign volume marks"""
            dict_list = []
            xattr_list = Xattr.llistxattr_buf('.')
            for ele in xattr_list:
                if ele.find('.'.join([cls.GX_NSPACE, 'volume-mark', ''])) == 0:
                    d, x = cls._attr_unpack_dict(ele, cls.FRGN_XTRA_FMT)
                    now = int(time.time())
                    if x[0] > now:
                        logging.debug("volinfo[%s] expires: %d (%d sec later)" % \
                                      (d['uuid'], x[0], x[0] - now))
                        d['timeout'] = x[0]
                        dict_list.append(d)
                    else:
                        try:
                            Xattr.lremovexattr('.', ele)
                        except OSError:
                            pass
            return dict_list

        @classmethod
        def native_volume_info(cls):
            """get the native volume mark of the underlying gluster volume"""
            try:
                return cls._attr_unpack_dict('.'.join([cls.GX_NSPACE, 'volume-mark']))
            except OSError:
                ex = sys.exc_info()[1]
                if ex.errno != ENODATA:
                    raise

    server = GLUSTERServer

    def __init__(self, path):
        self.host, self.volume = sup(self, path, '^(%s):(.+)' % HostRX.pattern)

    def canonical_path(self):
        return ':'.join([gethostbyname(self.host), self.volume])

    def can_connect_to(self, remote):
        """determine our position in the connectibility matrix"""
        return not remote or \
               (isinstance(remote, SSH) and isinstance(remote.inner_rsc, GLUSTER))

    class Mounter(object):
        """Abstract base class for mounter backends"""

        def __init__(self, params):
            self.params = params
            self.mntpt = None

        @classmethod
        def get_glusterprog(cls):
            return os.path.join(gconf.gluster_command_dir, cls.glusterprog)

        def umount_l(self, d):
            """perform lazy umount"""
            po = Popen(self.make_umount_argv(d), stderr=subprocess.PIPE)
            po.wait()
            return po

        @classmethod
        def make_umount_argv(cls, d):
            raise NotImplementedError

        def make_mount_argv(self, *a):
            raise NotImplementedError

        def cleanup_mntpt(self, *a):
            pass

        def handle_mounter(self, po):
            po.wait()

        def inhibit(self, *a):
            """inhibit a gluster filesystem

            Mount glusterfs over a temporary mountpoint,
            change into the mount, and lazy unmount the
            filesystem.
            """

            mpi, mpo = os.pipe()
            mh = Popen.fork()
            if mh:
                os.close(mpi)
                fcntl.fcntl(mpo, fcntl.F_SETFD, fcntl.FD_CLOEXEC)
                d = None
                margv = self.make_mount_argv(*a)
                if self.mntpt:
                    # mntpt is determined pre-mount
                    d = self.mntpt
                    os.write(mpo, d + '\0')
                po = Popen(margv, **self.mountkw)
                self.handle_mounter(po)
                po.terminate_geterr()
                logging.debug('auxiliary glusterfs mount in place')
                if not d:
                    # mntpt is determined during mount
                    d = self.mntpt
                    os.write(mpo, d + '\0')
                os.write(mpo, 'M')
                t = syncdutils.Thread(target=lambda: os.chdir(d))
                t.start()
                tlim = gconf.starttime + int(gconf.connection_timeout)
                while True:
                    if not t.isAlive():
                        break
                    if time.time() >= tlim:
                        syncdutils.finalize(exval = 1)
                    time.sleep(1)
                os.close(mpo)
                _, rv = syncdutils.waitpid(mh, 0)
                if rv:
                    rv = (os.WIFEXITED(rv) and os.WEXITSTATUS(rv) or 0) - \
                         (os.WIFSIGNALED(rv) and os.WTERMSIG(rv) or 0)
                    logging.warn('stale mount possibly left behind on ' + d)
                    raise GsyncdError("cleaning up temp mountpoint %s failed with status %d" % \
                                      (d, rv))
            else:
                rv = 0
                try:
                    os.setsid()
                    os.close(mpo)
                    mntdata = ''
                    while True:
                        c = os.read(mpi, 1)
                        if not c:
                            break
                        mntdata += c
                    if mntdata:
                        mounted = False
                        if mntdata[-1] == 'M':
                            mntdata = mntdata[:-1]
                            assert(mntdata)
                            mounted = True
                        assert(mntdata[-1] == '\0')
                        mntpt = mntdata[:-1]
                        assert(mntpt)
                        if mounted:
                            po = self.umount_l(mntpt)
                            po.terminate_geterr(fail_on_err = False)
                            if po.returncode != 0:
                                po.errlog()
                                rv = po.returncode
                        self.cleanup_mntpt(mntpt)
                except:
                    logging.exception('mount cleanup failure:')
                    rv = 200
                os._exit(rv)
            logging.debug('auxiliary glusterfs mount prepared')

    class DirectMounter(Mounter):
        """mounter backend which calls mount(8), umount(8) directly"""

        mountkw = {'stderr': subprocess.PIPE}
        glusterprog = 'glusterfs'

        @staticmethod
        def make_umount_argv(d):
            return ['umount', '-l', d]

        def make_mount_argv(self):
            self.mntpt = tempfile.mkdtemp(prefix = 'gsyncd-aux-mount-')
            return [self.get_glusterprog()] + ['--' + p for p in self.params] + [self.mntpt]

        def cleanup_mntpt(self, mntpt = None):
            if not mntpt:
                mntpt = self.mntpt
            os.rmdir(mntpt)

    class MountbrokerMounter(Mounter):
        """mounter backend using the mountbroker gluster service"""

        mountkw = {'stderr': subprocess.PIPE, 'stdout': subprocess.PIPE}
        glusterprog = 'gluster'

        @classmethod
        def make_cli_argv(cls):
            return [cls.get_glusterprog()] + gconf.gluster_cli_options.split() + ['system::']

        @classmethod
        def make_umount_argv(cls, d):
            return cls.make_cli_argv() + ['umount', d, 'lazy']

        def make_mount_argv(self, label):
            return self.make_cli_argv() + \
                   ['mount', label, 'user-map-root=' + syncdutils.getusername()] + self.params

        def handle_mounter(self, po):
            self.mntpt = po.stdout.readline()[:-1]
            po.stdout.close()
            sup(self, po)
            if po.returncode != 0:
                # if cli terminated with error due to being
                # refused by glusterd, what it put
                # out on stdout is a diagnostic message
                logging.error('glusterd answered: %s' % self.mntpt)

    def connect(self):
        """inhibit the resource beyond

        Choose mounting backend (direct or mountbroker),
        set up glusterfs parameters and perform the mount
        with given backend
        """

        label = getattr(gconf, 'mountbroker', None)
        if not label and not privileged():
            label = syncdutils.getusername()
        mounter = label and self.MountbrokerMounter or self.DirectMounter
        params = gconf.gluster_params.split() + \
                   (gconf.gluster_log_level and ['log-level=' + gconf.gluster_log_level] or []) + \
                   ['log-file=' + gconf.gluster_log_file, 'volfile-server=' + self.host,
                    'volfile-id=' + self.volume, 'client-pid=-1']
        mounter(params).inhibit(*[l for l in [label] if l])

    def connect_remote(self, *a, **kw):
        sup(self, *a, **kw)
        self.slavedir = "/proc/%d/cwd" % self.server.pid()

    def gmaster_instantiate_tuple(self, slave):
        """return a tuple of the 'one shot' and the 'main crawl' class instance"""
        return (gmaster_builder('xsync')(self, slave), gmaster_builder()(self, slave))

    def service_loop(self, *args):
        """enter service loop

        - if slave given, instantiate GMaster and
          pass control to that instance, which implements
          master behavior
        - else do that's what's inherited
        """
        if args:
            slave = args[0]
            if gconf.local_path:
                class brickserver(FILE.FILEServer):
                    local_path = gconf.local_path
                    aggregated = self.server
                    @classmethod
                    def entries(cls, path):
                        e = super(brickserver, cls).entries(path)
                        # on the brick don't mess with /.glusterfs
                        if path == '.':
                            try:
                                e.remove('.glusterfs')
                            except ValueError:
                                pass
                        return e
                if gconf.slave_id:
                    # define {,set_}xtime in slave, thus preempting
                    # the call to remote, so that it takes data from
                    # the local brick
                    slave.server.xtime = types.MethodType(lambda _self, path, uuid: brickserver.xtime(path, uuid + '.' + gconf.slave_id), slave.server)
                    slave.server.set_xtime = types.MethodType(lambda _self, path, uuid, mark: brickserver.set_xtime(path, uuid + '.' + gconf.slave_id, mark), slave.server)
                (g1, g2) = self.gmaster_instantiate_tuple(slave)
                g1.master.server = brickserver
                g2.master.server = brickserver
            else:
                (g1, g2) = self.gmaster_instantiate_tuple(slave)
                g1.master.server.aggregated = gmaster.master.server
                g2.master.server.aggregated = gmaster.master.server
            # bad bad bad: bad way to do things like this
            # need to make this elegant
            # register the crawlers and start crawling
            g1.register()
            g2.register()
            g1.crawlwrap(oneshot=True)
            g2.crawlwrap()
        else:
            sup(self, *args)

    def rsync(self, files):
        return sup(self, files, self.slavedir)


class SSH(AbstractUrl, SlaveRemote):
    """scheme class for ssh:// urls

    interface to remote slave on master side
    implementing an ssh based proxy
    """

    def __init__(self, path):
        self.remote_addr, inner_url = sup(self, path,
                                          '^((?:%s@)?%s):(.+)' % tuple([ r.pattern for r in (UserRX, HostRX) ]))
        self.inner_rsc = parse_url(inner_url)
        self.volume = inner_url[1:]

    @staticmethod
    def parse_ssh_address(addr):
        m = re.match('([^@]+)@(.+)', addr)
        if m:
            u, h = m.groups()
        else:
            u, h = syncdutils.getusername(), addr
        return {'user': u, 'host': h}

    def canonical_path(self):
        rap = self.parse_ssh_address(self.remote_addr)
        remote_addr = '@'.join([rap['user'], gethostbyname(rap['host'])])
        return ':'.join([remote_addr, self.inner_rsc.get_url(canonical=True)])

    def can_connect_to(self, remote):
        """determine our position in the connectibility matrix"""
        return False

    def start_fd_client(self, *a, **opts):
        """customizations for client startup

        - be a no-op if we are to daemonize (client startup is deferred
          to post-daemon stage)
        - determine target url for rsync after consulting server
        """
        if opts.get('deferred'):
            return a
        sup(self, *a)
        ityp = type(self.inner_rsc)
        if ityp == FILE:
            slavepath = self.inner_rsc.path
        elif ityp == GLUSTER:
            slavepath = "/proc/%d/cwd" % self.server.pid()
        else:
            raise NotImplementedError
        self.slaveurl = ':'.join([self.remote_addr, slavepath])

    def connect_remote(self, go_daemon=None):
        """connect to inner slave url through outer ssh url

        Wrap the connecting utility in ssh.

        Much care is put into daemonizing: in that case
        ssh is started before daemonization, but
        RePCe client is to be created after that (as ssh
        interactive password auth would be defeated by
        a daemonized ssh, while client should be present
        only in the final process). In that case the action
        is taken apart to two parts, this method is ivoked
        once pre-daemon, once post-daemon. Use @go_daemon
        to deiced what part to perform.

        [NB. ATM gluster product does not makes use of interactive
        authentication.]
        """
        if go_daemon == 'done':
            return self.start_fd_client(*self.fd_pair)
        gconf.setup_ssh_ctl(tempfile.mkdtemp(prefix='gsyncd-aux-ssh-'))
        deferred = go_daemon == 'postconn'
        ret = sup(self, gconf.ssh_command.split() + gconf.ssh_ctl_args + [self.remote_addr], slave=self.inner_rsc.url, deferred=deferred)
        if deferred:
            # send a message to peer so that we can wait for
            # the answer from which we know connection is
            # established and we can proceed with daemonization
            # (doing that too early robs the ssh passwd prompt...)
            # However, we'd better not start the RepceClient
            # before daemonization (that's not preserved properly
            # in daemon), we just do a an ad-hoc linear put/get.
            i, o = ret
            inf = os.fdopen(i)
            repce.send(o, None, '__repce_version__')
            select((inf,), (), ())
            repce.recv(inf)
            # hack hack hack: store a global reference to the file
            # to save it from getting GC'd which implies closing it
            gconf.permanent_handles.append(inf)
            self.fd_pair = (i, o)
            return 'should'

    def rsync(self, files):
        return sup(self, files, '-e', " ".join(gconf.ssh_command.split() + gconf.ssh_ctl_args),
                   *(gconf.rsync_ssh_options.split() + [self.slaveurl]))