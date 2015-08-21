# vmnetx.controller.local - Execution of a VM with libvirt
#
# Copyright (C) 2008-2014 Carnegie Mellon University
#
# This program is free software; you can redistribute it and/or modify it
# under the terms of version 2 of the GNU General Public License as published
# by the Free Software Foundation.  A copy of the GNU General Public License
# should have been distributed along with this program in the file
# COPYING.
#
# This program is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY
# or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU General Public License
# for more details.
#

import base64
from calendar import timegm
import dbus
from distutils.version import LooseVersion
import gobject
import grp
from hashlib import sha256
import json
import libvirt
import logging
from lxml.builder import ElementMaker
import multiprocessing
import os
import pipes
import pwd
import Queue
import re
import signal
import socket
import string
import struct
import subprocess
import sys
from tempfile import NamedTemporaryFile
import threading
import time
from urlparse import urlsplit, urlunsplit
import uuid
from wsgiref.handlers import format_date_time as format_rfc1123_date

from ...domain import DomainXML
from ...generate import copy_memory
from ...memory import LibvirtQemuMemoryHeader, LibvirtQemuMemoryHeaderData
from ...package import Package
from ...source import source_open, SourceRange
from ...util import (ErrorBuffer, ensure_dir, get_pristine_cache_dir,
        get_modified_cache_dir, setup_libvirt)
from .. import Controller, MachineExecutionError, MachineStateError, Statistic
from .monitor import (ChunkMapMonitor, LineStreamMonitor,
        CheckinProgressMonitor,
        BackgroundUploadMonitor,
        LoadProgressMonitor, StatMonitor)
from .qmp_af_unix import *
from .virtevent import LibvirtEventImpl
from .vmnetfs import VMNetFS, NS as VMNETFS_NS

_log = logging.getLogger(__name__)

# Initialize libvirt.  Modifies global state.
setup_libvirt()

# Enable libvirt event reporting.  Also modifies global state.
LibvirtEventImpl().register()


class _Image(object):

    @staticmethod
    def get_pristine_cache_path(uuid):
        return os.path.join(get_pristine_cache_dir(), 'chunks', uuid)

    @staticmethod
    def get_modified_cache_path(uuid):
        return os.path.join(get_modified_cache_dir(), 'chunks', uuid)

    @staticmethod
    def _get_version(uuid):
        info_file = os.path.join(self._pristine_urlpath, 'info')
        if not os.path.exists(info_file):
            return -1
        else:
            with open(info_file, 'r') as fh:
                info = json.loads(fh.read())
                return info['version']

    def __init__(self, label, range, username=None, password=None,
            chunk_size=131072, stream=False, checkin=False, throttle_rate=1.0):
        self.label = label
        self.username = username
        self.password = password
        self.stream = stream
        self.cookies = range.source.cookies
        self.url = range.source.url
        self.offset = range.offset
        self.size = range.length
        self.chunk_size = chunk_size
        self.etag = range.source.etag
        self.last_modified = range.source.last_modified
        self.checkin = checkin
        self.throttle_rate = throttle_rate

        parsed_url = urlsplit(self.url)
        self._pristine_cache_info = json.dumps({
            # Exclude query string from cache path
            'url': urlunsplit((parsed_url.scheme, parsed_url.netloc,
                    parsed_url.path, '', '')),
            'version': 1,
            # 'etag': self.etag,
            # 'last-modified': self.last_modified.isoformat()
            #        if self.last_modified else None,
        }, indent=2, sort_keys=True)
        '''
        self._pristine_urlpath = os.path.join(get_pristine_cache_dir(),
                'chunks', sha256(self._pristine_cache_info).hexdigest())
        '''
        path = urlsplit(self.url)[2]
        uuid = string.split(path, '/')[2]
        self._pristine_urlpath = self.get_pristine_cache_path(uuid)

        # Hash collisions will allow cache poisoning!
        self.pristine_cache = os.path.join(self._pristine_urlpath, label,
                str(chunk_size))
        self._modified_cache_info = json.dumps({
         # Exclude query string from cache path
            'url': urlunsplit((parsed_url.scheme, parsed_url.netloc,
                    parsed_url.path, '', '')),
            'version': 1,
            # 'etag': self.etag,
        }, indent=2, sort_keys=True)
        '''
        self._modified_urlpath = os.path.join(get_modified_cache_dir(),
                'chunks', sha256(self._modified_cache_info).hexdigest())
        '''
        self._modified_urlpath = self.get_modified_cache_path(uuid)
        self.modified_cache = os.path.join(self._modified_urlpath, label,
                str(chunk_size))

        # read and update images sizes from modified cache dir
        size_file_path = os.path.join(self.modified_cache, 'size')
        if os.path.isfile(size_file_path):
            f = open(size_file_path, 'r')
            self.size = int(f.readline())
            f.close()

    def get_recompressed_path(self, algorithm):
        return os.path.join(self._pristine_urlpath, self.label,
                'recompressed.%s' % algorithm)

    # We must access Cookie._rest to perform case-insensitive lookup of
    # the HttpOnly attribute
    # pylint: disable=protected-access
    @property
    def vmnetfs_config(self):
        # Write URL and validators into file for ease of debugging.
        # Defer creation of cache directory until needed.
        ensure_dir(self._pristine_urlpath)
        info_file = os.path.join(self._pristine_urlpath, 'info')
        if not os.path.exists(info_file):
            with open(info_file, 'w') as fh:
                fh.write(self._pristine_cache_info)

        ensure_dir(self._modified_urlpath)
        info_file = os.path.join(self._modified_urlpath, 'info')
        if not os.path.exists(info_file):
            with open(info_file, 'w') as fh:
                fh.write(self._modified_cache_info)

        # Return XML image element
        e = ElementMaker(namespace=VMNETFS_NS, nsmap={None: VMNETFS_NS})
        origin = e.origin(
            e.url(self.url),
            e.offset(str(self.offset)),
        )
        if self.last_modified or self.etag:
            validators = e.validators()
            if self.last_modified:
                validators.append(e('last-modified',
                        str(timegm(self.last_modified.utctimetuple()))))
            if self.etag:
                validators.append(e.etag(self.etag))
            origin.append(validators)
        if self.username and self.password:
            credentials = e.credentials(
                e.username(self.username),
                e.password(self.password),
            )
            origin.append(credentials)
        if self.cookies:
            cookies = e.cookies()
            for cookie in self.cookies:
                c = '%s=%s; Domain=%s; Path=%s' % (cookie.name, cookie.value,
                        cookie.domain, cookie.path)
                if cookie.expires:
                    c += '; Expires=%s' % format_rfc1123_date(cookie.expires)
                if cookie.secure:
                    c += '; Secure'
                if 'httponly' in [k.lower() for k in cookie._rest]:
                    c += '; HttpOnly'
                cookies.append(e.cookie(c))
            origin.append(cookies)
        return e.image(
            e.name(self.label),
            e.size(str(self.size)),
            origin,
            e.cache(
                e.pristine(
                    e.path(self.pristine_cache),
                ),
                e.modified(
                    e.path(self.modified_cache),
                ),
                e('chunk-size', str(self.chunk_size)),
            ),
            e.fetch(
                e.mode('stream' if self.stream else 'demand'),
            ),
            e.upload(
                e.checkin(str(1) if self.checkin else str(0)),
                e.rate(str(self.throttle_rate))
            ),
        )
    # pylint: enable=protected-access


class _QemuWatchdog(object):
    # Watch to see if qemu dies at startup, and if so, kill the compressor
    # processing its save file.
    # Workaround for <https://bugzilla.redhat.com/show_bug.cgi?id=982816>.

    INTERVAL = 100 # ms
    COMPRESSORS = ('gzip', 'bzip2', 'xz', 'lzop')

    def __init__(self, name):
        # Called from vmnetx-startup thread.
        self._name = name
        self._stop = False
        self._qemu_exe = None
        self._qemu_pid = None
        self._compressor_exe = None
        self._compressor_pid = None
        gobject.timeout_add(self.INTERVAL, self._timer)

    def _timer(self):
        # Called from UI thread.
        # First see if we should terminate.
        if self._stop:
            return False

        # Find qemu and the compressor if we haven't already found it.
        if self._qemu_pid is None:
            pids = [int(p) for p in os.listdir('/proc') if p.isdigit()]
            uid = os.getuid()

            # Look for qemu
            for pid in pids:
                try:
                    # Check process owner
                    if os.stat('/proc/%d' % pid).st_uid != uid:
                        continue
                    # Check process name.  We can't check against the
                    # emulator from the domain XML, because it turns out that
                    # that could be a shell script.
                    exe = os.readlink('/proc/%d/exe' % pid)
                    if 'qemu' not in exe and 'kvm' not in exe:
                        continue
                    # Read argv
                    with open('/proc/%d/cmdline' % pid) as fh:
                        args = fh.read().split('\x00')
                    # Check VM name
                    if args[args.index('-name') + 1] != self._name:
                        continue
                    # Get compressor fd
                    fd = args[args.index('-incoming') + 1]
                    fd = int(fd.replace('fd:', ''))
                    # Get kernel identifier for compressor fd
                    compress_ident = os.readlink('/proc/%d/fd/%d' % (pid, fd))
                    if not compress_ident.startswith('pipe:'):
                        continue
                    # All set.
                    self._qemu_exe = exe
                    self._qemu_pid = pid
                    break
                except (IOError, OSError, IndexError, ValueError):
                    continue
            else:
                # Couldn't find emulator; it may not have started yet.
                # Try again later.
                return True

            # Now look for compressor communicating with the emulator
            for pid in pids:
                try:
                    # Check process owner
                    if os.stat('/proc/%d' % pid).st_uid != uid:
                        continue
                    # Check process name
                    exe = os.readlink('/proc/%d/exe' % pid)
                    if exe.split('/')[-1] not in self.COMPRESSORS:
                        continue
                    # Check kernel identifier for stdout
                    if os.readlink('/proc/%d/fd/1' % pid) != compress_ident:
                        continue
                    # All set.
                    self._compressor_exe = exe
                    self._compressor_pid = pid
                    break
                except OSError:
                    continue
            else:
                # Couldn't find compressor.  Either the compressor has
                # already exited, or this is an uncompressed memory image.
                # Conclude that we have nothing to do.
                return False

        # If qemu still exists, try again later.
        try:
            if os.readlink('/proc/%d/exe' % self._qemu_pid) == self._qemu_exe:
                return True
        except OSError:
            pass

        # qemu exited.  Kill compressor.
        try:
            if (os.readlink('/proc/%d/exe' % self._compressor_pid) ==
                    self._compressor_exe):
                os.kill(self._compressor_pid, signal.SIGTERM)
        except OSError:
            pass
        return False

    def stop(self):
        # Called from vmnetx-startup thread.
        self._stop = True


class _MemoryRecompressor(object):
    RECOMPRESSION_DELAY = 30000  # ms

    def __init__(self, controller, algorithm, in_path, out_path):
        self._algorithm = algorithm
        self._in_path = in_path
        self._out_path = out_path
        self._have_run = False
        controller.connect('vm-started', self._vm_started)

    def _vm_started(self, _controller, _have_memory):
        if self._have_run:
            return
        self._have_run = True
        gobject.timeout_add(self.RECOMPRESSION_DELAY, self._timer_expired)

    def _timer_expired(self):
        threading.Thread(name='vmnetx-recompress-memory',
                target=self._thread).start()

    # We intentionally catch all exceptions
    # pylint: disable=bare-except
    def _thread(self):
        if os.path.exists(self._out_path):
            return
        tempfile = NamedTemporaryFile(dir=os.path.dirname(self._out_path),
                prefix=os.path.basename(self._out_path) + '-', delete=False)
        _log.info('Recompressing memory image')
        start = time.time()
        try:
            copy_memory(self._in_path, tempfile.name,
                    compression=self._algorithm, verbose=False,
                    low_priority=True)
        except:
            _log.exception('Recompressing memory image failed')
            os.unlink(tempfile.name)
        else:
            _log.info('Recompressed memory image in %.1f seconds',
                    time.time() - start)
            os.rename(tempfile.name, self._out_path)
    # pylint: enable=bare-except


# Called by process
def _background_snapshot(order_queue):
    qmp = QmpAfUnix(QMP_UNIX_SOCK)
    qmp.connect()
    ret = qmp.qmp_negotiate()
    if not ret:
        _log.exception('Background upload failed to communicate with VM')
        return
    ret = qmp.unrandomize_raw_live()
    if not ret:
        _log.exception('Failed to make page output sequential')
        return

    while True:
        # Timeout added so that the process is not mistakenly orphaned
        try:
            order = order_queue.get(True, 120)
        except Queue.Empty:
            break
        if order == 'iterate':
            try:
                qmp.iterate_raw_live()
            except Exception, e:
                print 'iterate_raw_live() failed', e
                pass
        elif order == 'stop':
            # let last iteration finish
            time.sleep(10)
            break
    qmp.stop_raw_live()
    qmp.disconnect()

def _save_to_fifo(input_fifo_path, domain_name):
    conn = libvirt.open('qemu:///session')
    domain = conn.lookupByName(domain_name)
    domain.saveFlags(input_fifo_path, None, libvirt.VIR_DOMAIN_SAVE_RUNNING)
    conn.close()

class MemoryReadProcess(threading.Thread):
    # header format for each memory page
    CHUNK_HEADER_FMT = "=Q"
    CHUNK_HEADER_SIZE = struct.calcsize("=Q")
    ITER_SEQ_BITS   = 16
    ITER_SEQ_SHIFT  = CHUNK_HEADER_SIZE * 8 - ITER_SEQ_BITS
    CHUNK_POS_MASK   = (1 << ITER_SEQ_SHIFT) - 1
    ITER_SEQ_MASK   = ((1 << (CHUNK_HEADER_SIZE * 8)) - 1) - CHUNK_POS_MASK
    ALIGNED_HEADER_SIZE = 4096*2

    def __init__(self, input_fifo_path, memory_image_path, chunk_size=131072):
        self.input_fifo_path = input_fifo_path
        self.memory_image_path = memory_image_path
        self.iteration_seq = -1
        self.chunk_size = chunk_size
        self.curr_chunk_num = 0
        self.curr_chunk_buffer = ""
        self.curr_chunk_offset = 0
        threading.Thread.__init__(self, target=self.read_mem_snapshot)

    def process_header(self, mem_file_fd, output_fd):
        data = mem_file_fd.read(4096*10)
        libvirt_header = LibvirtQemuMemoryHeaderData(data)
        header = libvirt_header.get_header()
        header_size = len(header)

        # read 8 bytes of qemu header
        snapshot_size_data = data[header_size:header_size+self.CHUNK_HEADER_SIZE]
        snapshot_size, = struct.unpack(self.CHUNK_HEADER_FMT,
                                       snapshot_size_data)
        remaining_data = data[len(header)+self.CHUNK_HEADER_SIZE:]

        # write aligned header (8KB) to file
        aligned_header = libvirt_header.get_aligned_header(self.ALIGNED_HEADER_SIZE)
        output_fd.write(aligned_header)
        self.debug = open('/home/dayoon/senior/debug/process', 'w')
        return libvirt_header.xml, snapshot_size, remaining_data

    def read_mem_snapshot(self):
        # waiting for named pipe
        for repeat in xrange(100):
            if os.path.exists(self.input_fifo_path) == False:
                time.sleep(0.1)
            else:
                break

        # read memory snapshot from the named pipe
        try:
            self.in_fd = open(self.input_fifo_path, 'rb')
            self.out_fd = open(self.memory_image_path, 'r+b')

            input_fd = [self.in_fd]

            # skip libvirt header
            header_xml, snapshot_size, remaining_data =\
                self.process_header(self.in_fd, self.out_fd)

            # remaining data are all about memory page
            # [(8 bytes header, 4KB page), (8 bytes header, 4KB page), ...]
            self.out_fd.seek(0)
            self.curr_chunk_buffer = self.out_fd.read(self.chunk_size)

            chunk_size = self.CHUNK_HEADER_SIZE + 4096
            leftover = self._data_chunking(remaining_data, chunk_size)
            while True:
                data = self.in_fd.read(10*4096)
                if not data:
                    break
                leftover = self._data_chunking(leftover+data, chunk_size)

            # write whats left in the buffer
            memory_image_offset = self.curr_chunk_num * self.chunk_size
            self.out_fd.seek(memory_image_offset)
            data_binary = struct.pack('%ds' % self.chunk_size,
                    self.curr_chunk_buffer)
            self.out_fd.write(data_binary)

        except Exception, e:
            sys.stdout.write("[MemorySnapshotting] Exception1n")
            # sys.stderr.write(traceback.format_exc())
            sys.stderr.write("%s\n" % str(e))
        self.finish()

    def _data_chunking(self, l, n):
        leftover = ''
        for index in range(0, len(l), n):
            chunked_data = l[index:index+n]
            chunked_data_size = len(chunked_data)
            if chunked_data_size == n:
                header = chunked_data[0:self.CHUNK_HEADER_SIZE]
                header_data, = struct.unpack(self.CHUNK_HEADER_FMT, header)
                iter_seq = (header_data& self.ITER_SEQ_MASK) >> self.ITER_SEQ_SHIFT
                ram_offset = (header_data & self.CHUNK_POS_MASK)
                if iter_seq != self.iteration_seq:
                    self.iteration_seq = iter_seq

                # save the snapshot data
                memory_image_offset = ram_offset + self.ALIGNED_HEADER_SIZE

                # THIS WORKS
                # self.out_fd.seek(memory_image_offset)
                # self.out_fd.write(chunked_data[self.CHUNK_HEADER_SIZE:])

                chunk_num = memory_image_offset / self.chunk_size

                # All the pages for the last chunk have been written to the pipe
                if chunk_num != self.curr_chunk_num:
                    self.out_fd.seek(self.curr_chunk_offset)
                    data_binary = struct.pack('%ds' % self.chunk_size,
                            self.curr_chunk_buffer)
                    self.out_fd.write(data_binary)

                    self.debug.write('wrote chunk %d of len %d %d at %d \n' %
                            (self.curr_chunk_num,
                        len(data_binary), len(self.curr_chunk_buffer),
                        self.curr_chunk_offset))

                    self.curr_chunk_num = chunk_num
                    self.curr_chunk_offset = self.curr_chunk_num * self.chunk_size

                    # Read in new chunk that will be updated
                    self.out_fd.seek(self.curr_chunk_offset)
                    self.curr_chunk_buffer = self.out_fd.read(self.chunk_size)

                # offset within a chunk
                page_offset = memory_image_offset % self.chunk_size
                page_size = len(chunked_data[self.CHUNK_HEADER_SIZE:])

                # Strings in python are immutable so generate a new string
                self.curr_chunk_buffer = \
                        self.curr_chunk_buffer[:page_offset] + \
                        chunked_data[self.CHUNK_HEADER_SIZE:] + \
                        self.curr_chunk_buffer[page_offset+page_size:]
            else:
                # last iteration
                leftover = chunked_data
        return leftover

    def finish(self):
        self.out_fd.close()
        self.debug.close()


class LocalController(Controller):
    AUTHORIZER_NAME = 'org.olivearchive.VMNetX.Authorizer'
    AUTHORIZER_PATH = '/org/olivearchive/VMNetX/Authorizer'
    AUTHORIZER_IFACE = 'org.olivearchive.VMNetX.Authorizer'
    STATS = ('bytes_read', 'bytes_written', 'chunk_dirties', 'chunk_fetches',
            'io_errors')
    RECOMPRESSION_ALGORITHM = 'lzop'
    _environment_ready = False

    def __init__(self, url=None, package=None, use_spice=True,
            viewer_password=None, checkin=False, throttle_rate=1.0):
        Controller.__init__(self)
        self._url = url
        self._want_spice = use_spice
        self._domain_name = 'vmnetx-%d-%s' % (os.getpid(), uuid.uuid4())
        self._package = package
        self._have_memory = False
        self._memory_image_path = None
        self._fs = None
        self._conn = None
        self._conn_callbacks = []
        self._startup_running = False
        self._stop_thread = None
        self._domain_xml = None
        self._viewer_address = None
        self._monitors = []
        self._load_monitor = None
        self._background_upload_monitor = None
        self.viewer_password = viewer_password
        self._modified_disk = None
        self._modified_memory = None
        self._checkin = checkin
        self._throttle_rate = throttle_rate
        self._qmp = None
        self._output_filename = None
        self._output_temp = None
        self._output_fifo = None
        self._order_thread = None
        self._order_queue = multiprocessing.Queue(maxsize=-1)
        self._p = None # background snapshot process
        self._t = None
        self._fifo_process = None
        self._iteration_interval = 20

    @Controller._ensure_state(Controller.STATE_UNINITIALIZED)
    def initialize(self):

        if not self._environment_ready:
            raise ValueError('setup_environment has not been called')

        # Load package
        if self._package is None:
            source = source_open(self._url, scheme=self.scheme,
                    username=self.username, password=self.password)
            package = Package(source)
        else:
            package = self._package

        # Validate domain XML
        domain_xml = DomainXML(package.domain.data)

        # Create vmnetfs config
        e = ElementMaker(namespace=VMNETFS_NS, nsmap={None: VMNETFS_NS})
        vmnetfs_config = e.config()
        vmnetfs_config.append(_Image('disk', package.disk,
                username=self.username, password=self.password,
                checkin=self._checkin,
                throttle_rate=self._throttle_rate).vmnetfs_config)
        if package.memory:
            image = _Image('memory', package.memory, username=self.username,
                    password=self.password, stream=True,
                    checkin=self._checkin,
                    throttle_rate=self._throttle_rate)
            self._modified_memory = image.modified_cache
            # Use recompressed memory image if available
            '''
            recompressed_path = image.get_recompressed_path(
                    self.RECOMPRESSION_ALGORITHM)
            if os.path.exists(recompressed_path):
                # When started from vmnetx, logging isn't up yet
                gobject.idle_add(lambda:
                        _log.info('Using recompressed memory image'))
                memory_image = _Image('memory',
                        SourceRange(source_open(filename=recompressed_path)),
                        stream=True)
                self._modified_memory = memory_image.modified_cache
            '''
            vmnetfs_config.append(image.vmnetfs_config)

        # Start vmnetfs
        self._fs = VMNetFS(vmnetfs_config)
        self._fs.start()
        log_path = os.path.join(self._fs.mountpoint, 'log')
        disk_path = os.path.join(self._fs.mountpoint, 'disk')
        disk_image_path = os.path.join(disk_path, 'image')
        if package.memory:
            memory_path = os.path.join(self._fs.mountpoint, 'memory')
            self._memory_path = memory_path
            self._memory_image_path = os.path.join(memory_path, 'image')
            # Create recompressed memory image if missing
            '''
            if not os.path.exists(recompressed_path):
                _MemoryRecompressor(self, self.RECOMPRESSION_ALGORITHM,
                        self._memory_image_path, recompressed_path)
                        '''
        else:
            memory_path = self._memory_image_path = None

        # Set up libvirt connection
        if not self._checkin:
            self._conn = libvirt.open('qemu:///session')
            cb = self._conn.domainEventRegisterAny(None,
                    libvirt.VIR_DOMAIN_EVENT_ID_LIFECYCLE, self._lifecycle_event,
                    None)
            self._conn_callbacks.append(cb)

            # Get emulator path
            emulator = domain_xml.detect_emulator(self._conn)

            # Detect SPICE support
            self.use_spice = self._want_spice and self._spice_is_usable(emulator)

            # Create new viewer password if none existed
            if self.viewer_password is None:
                # VNC limits passwords to 8 characters
                self.viewer_password = base64.urlsafe_b64encode(os.urandom(
                        15 if self.use_spice else 6))

            # Get execution domain XML
            self._domain_xml = domain_xml.get_for_execution(self._domain_name,
                    emulator, disk_image_path, self.viewer_password,
                    use_spice=self.use_spice,
                    allow_qxl=False).xml
                    #allow_qxl=self._qxl_is_usable(emulator)).xml

            # Write domain XML to memory image
            if self._memory_image_path is not None:
                with open(self._memory_image_path, 'r+') as fh:
                    hdr = LibvirtQemuMemoryHeader(fh)
                    hdr.xml = self._domain_xml
                    hdr.write(fh)

        # Set configuration
        self.vm_name = package.name
        self._have_memory = memory_path is not None
        self.max_mouse_rate = domain_xml.max_mouse_rate

        # Set chunk size
        path = os.path.join(disk_path, 'stats', 'chunk_size')
        with open(path) as fh:
            self.disk_chunk_size = int(fh.readline().strip())

        # Create monitors
        for name in self.STATS:
            stat = Statistic(name)
            self.disk_stats[name] = stat
            self._monitors.append(StatMonitor(stat, disk_path, name))
        self._monitors.append(ChunkMapMonitor(self.disk_chunks, disk_path))
        log_monitor = LineStreamMonitor(log_path)
        log_monitor.connect('line-emitted', self._vmnetfs_log)
        self._monitors.append(log_monitor)

        if self._checkin:
            self._checkin_monitor = CheckinProgressMonitor(disk_path,
                    memory_path)
            self._checkin_monitor.connect('checkin-progress',
                    self._checkin_progress)

        else:
            if self._have_memory:
                self._load_monitor = LoadProgressMonitor(memory_path)
                self._load_monitor.connect('progress', self._load_progress)
            self._background_upload_monitor = BackgroundUploadMonitor(disk_path,
                    memory_path)
            self._background_upload_monitor.connect('background-upload',
                    self._background_upload)

        # Kick off state machine after main loop starts
        self.state = self.STATE_STOPPED
        gobject.idle_add(self.emit, 'vm-stopped')

    # Should be called before we open any windows, since we may re-exec
    # the whole program if we need to update the group list.
    @classmethod
    def setup_environment(cls):
        if os.geteuid() == 0:
            raise MachineExecutionError(
                    'Will not execute virtual machines as root')

        # Check for VT support
        with open('/proc/cpuinfo') as fh:
            for line in fh:
                elts = line.split(':', 1)
                if elts[0].rstrip() == 'flags':
                    flags = elts[1].split()
                    if 'vmx' not in flags and 'svm' not in flags:
                        raise MachineExecutionError('Your CPU does not ' +
                                'support hardware virtualization extensions')
                    break

        try:
            obj = dbus.SystemBus().get_object(cls.AUTHORIZER_NAME,
                    cls.AUTHORIZER_PATH)
            # We would like an infinite timeout, but dbus-python won't allow
            # it.  Pass the longest timeout dbus-python will accept.
            groups = obj.EnableFUSEAccess(dbus_interface=cls.AUTHORIZER_IFACE,
                    timeout=2147483)
        except dbus.exceptions.DBusException, e:
            # dbus-python exception handling is problematic.
            if 'Authorization failed' in str(e):
                # The user knows this already; don't show a FatalErrorWindow.
                sys.exit(1)
            else:
                # If we can't contact the authorizer (perhaps because D-Bus
                # wasn't configured correctly), proceed as though we have
                # sufficient permission, and possibly fail later.  This
                # avoids unnecessary failures in the common case.
                cls._environment_ready = True
                return

        if groups:
            # Make sure all of the named groups are in our supplementary
            # group list, which will not be true if EnableFUSEAccess() just
            # added us to those groups (or if it did so earlier in this
            # login session).  We have to do this one group at a time, and
            # then restore our primary group afterward.
            def switch_group(group):
                cmd = ' '.join(pipes.quote(a) for a in
                        [sys.executable] + sys.argv)
                os.execlp('sg', 'sg', group, '-c', cmd)
            cur_gids = os.getgroups()
            for group in groups:
                if grp.getgrnam(group).gr_gid not in cur_gids:
                    switch_group(group)
            primary_gid = pwd.getpwuid(os.getuid()).pw_gid
            if os.getgid() != primary_gid:
                switch_group(grp.getgrgid(primary_gid).gr_name)

        cls._environment_ready = True

    def _spice_is_usable(self, emulator):
        '''Determine whether emulator supports SPICE.'''
        proc = subprocess.Popen([emulator, '-spice', 'foo'],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                close_fds=True)
        out, err = proc.communicate()
        out += err
        if 'invalid option' in out or 'spice is not supported' in out:
            # qemu is too old to support SPICE, or SPICE is not compiled in
            return False
        return True

    def _qxl_is_usable(self, emulator):
        '''Blacklist emulators with broken qxl support.'''
        proc = subprocess.Popen([emulator, '-version'],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                close_fds=True)
        out, err = proc.communicate()
        out += err
        match = re.search(r'emulator version ([0-9.]+)', out)
        if not match:
            # Assume we're safe
            return True
        ver = LooseVersion(match.group(1))
        if ver >= LooseVersion('1.0') and ver < LooseVersion('1.1'):
            # Ubuntu 12.04
            # https://bugs.launchpad.net/ubuntu/+source/qemu-kvm-spice/+bug/970234
            return False
        return True

    def _vmnetfs_log(self, _monitor, line):
        _log.warning('%s', line)

    @Controller._ensure_state(Controller.STATE_STOPPED)
    def start_vm(self):
        self.state = self.STATE_STARTING
        self._startup_running = True
        if self._have_memory:
            self.emit('startup-progress', 0, self._load_monitor.chunks)
        threading.Thread(name='vmnetx-startup', target=self._startup).start()

    # We intentionally catch all exceptions
    # pylint: disable=bare-except
    def _startup(self):
        # Thread function.
        try:
            have_memory = self._have_memory
            try:
                if have_memory:
                    watchdog = _QemuWatchdog(self._domain_name)
                    try:
                        # Does not return domain handle
                        # Does not allow autodestroy
                        f = open("/home/dayoon/senior/debug/xml_domain",'w')
                        f.write(self._domain_xml)
                        f.close()

                        # self._conn.restore(self._memory_image_path)
                        self._conn.restoreFlags(self._memory_image_path,
                                self._domain_xml,
                                libvirt.VIR_DOMAIN_SAVE_RUNNING)

                        # Initialize background process to order snapshots
                        self._p = multiprocessing.Process(target=_background_snapshot, args=(self._order_queue,))
                        self._p.start()

                        # Start sending iteration requests
                        self._order_thread = threading.Thread(name='order',
                                target=self._order_iterations)
                        self._order_thread.start()

                        # Create fifo
                        output_fifo = os.path.join(self._modified_memory,
                                'output.fifo')
                        if os.path.exists(output_fifo) == True:
                            os.remove(output_fifo)
                        os.mkfifo(output_fifo)

                        # Reader thread pointed at the fifo
                        self._t = MemoryReadProcess(output_fifo,
                                  self._memory_image_path)
                        self._t.start()

                        # start memory dump
                        self._fifo_process = multiprocessing.Process(target=_save_to_fifo,
                                args=(output_fifo, self._domain_name,))
                        self._fifo_process.start()

                    finally:
                        watchdog.stop()
                    domain = self._conn.lookupByName(self._domain_name)
                    # domain = self._conn.lookupByName('machine')
                else:
                    domain = self._conn.createXML(self._domain_xml,
                            libvirt.VIR_DOMAIN_NONE)
                    f = open("/home/dayoon/senior/debug/xml_new_domain", "w")
                    f.write(domain.XMLDesc(0))
                    f.close()

                # Get viewer socket address
                domain_xml = DomainXML(domain.XMLDesc(0),
                        validate=DomainXML.VALIDATE_NONE, safe=False)
                self._viewer_address = (
                    domain_xml.viewer_host or '127.0.0.1',
                    domain_xml.viewer_port
                )
            except libvirt.libvirtError, e:
                # print str(e) // THIS GIVES A CHILD DIED ERROR.. WHY??
                raise MachineExecutionError(str(e))
            finally:
                if have_memory:
                    gobject.idle_add(self._load_monitor.close)
        except:
            if self.state == self.STATE_STOPPING:
                self.state = self.STATE_STOPPED
                gobject.idle_add(self.emit, 'vm-stopped')
            elif have_memory:
                self._have_memory = False
                gobject.idle_add(self.emit, 'startup-rejected-memory')
                # Retry without memory image
                self._startup()
            else:
                self.state = self.STATE_STOPPED
                gobject.idle_add(self.emit, 'startup-failed', ErrorBuffer())
                gobject.idle_add(self.emit, 'vm-stopped')
        else:
            self.state = self.STATE_RUNNING
            gobject.idle_add(self.emit, 'vm-started', have_memory)
        finally:
            self._startup_running = False
    # pylint: enable=bare-except

    def _checkin_progress(self, _obj, disk_count, disk_total,
            memory_count, memory_total):
        self.emit('checkin-progress', disk_count, disk_total, memory_count,
                memory_total)

    def _load_progress(self, _obj, count, total):
        if self._have_memory and self.state == self.STATE_STARTING:
            self.emit('startup-progress', count, total)

    def _background_upload(self, _obj, disk_total, memory_total):
        self.emit('background-upload', disk_total, memory_total)

    def connect_viewer(self, callback):
        if self.state != self.STATE_RUNNING:
            callback(error='Machine in inappropriate state')
            return
        self._connect_socket(self._viewer_address, callback)

    ## SUMSING WONG HERE
    def _lifecycle_event(self, _conn, domain, event, _detail, _data):
        if domain.name() == self._domain_name:
            # if event == libvirt.VIR_DOMAIN_EVENT_SHUTDOWN:
            #    print "sHUTTING DOWN???"
            #    self.state = self.STATE_SHUTTING_DOWN
            #    self.emit('vm-shutting-down')
            if (event == libvirt.VIR_DOMAIN_EVENT_STOPPED and
                    self.state != self.STATE_STOPPED): # and
                    #self.state != self.STATE_SHUTTING_DOWN):
                # If the startup thread is running, it has absolute control
                # over state transitions.
                if not self._startup_running:
                    self.state = self.STATE_STOPPED
                    self.emit('vm-stopped')

    def _order_iterations(self):
        # Give time to process unrandomize
        time.sleep(60)

        while True:
            for i in range(self._iteration_interval):
                time.sleep(1)
                if self.state == Controller.STATE_STOPPING:
                    self._order_queue.put('stop')
                    return
            self._order_queue.put('iterate')

    def stop_vm(self):
        if (self.state == self.STATE_STARTING or
                self.state == self.STATE_RUNNING): # or
                #self.state == self.STATE_SHUTTING_DOWN):
            self.state = Controller.STATE_STOPPING
            self._viewer_address = None
            self._have_memory = False
            self._stop_thread = threading.Thread(name='vmnetx-stop-vm',
                    target=self._stop_vm)
            self._stop_thread.start()

    def _stop_vm(self):
        # Thread function.
        try:
            if self._order_thread:
                self._order_thread.join()
            if self._order_queue:
                self._order_queue.put('stop')
            if self._fifo_process:
                try:
                    self._fifo_process.join()
                except Exception, e:
                    print "wtf"
            if self._p:
                self._p.join()
            if self._t:
                self._t.join()
            self._conn.lookupByName(self._domain_name).destroy()
        except libvirt.libvirtError, e:
            pass

    def shutdown(self):
        for monitor in self._monitors:
            monitor.close()
        self._monitors = []
        if self._background_upload_monitor is not None:
            self._background_upload_monitor.close()
        self.stop_vm()
        if self._stop_thread is not None:
            self._stop_thread.join()
        # Close libvirt connection
        if self._conn is not None:
            # We must deregister callbacks or the conn won't fully close
            for cb in self._conn_callbacks:
                self._conn.domainEventDeregisterAny(cb)
            del self._conn_callbacks[:]
            self._conn.close()
            self._conn = None
        # Terminate vmnetfs
        if self._fs is not None:
            self._fs.terminate()
            self._fs = None
        self.state = self.STATE_DESTROYED
gobject.type_register(LocalController)
