#
# vmnetx.ui.view - vmnetx UI widgets
#
# Copyright (C) 2008-2013 Carnegie Mellon University
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

from __future__ import division
import cairo
from collections import OrderedDict
from distutils.version import LooseVersion
import glib
import gobject
import gtk
import json
import logging
import os
import pango
import distutils.dir_util
import sys
import urllib
import uuid
import requests
from urlparse import urljoin, urlsplit, urlunsplit
import vmnetx.ui

from ..controller import ChunkStateArray
from ..util import ErrorBuffer, BackoffTimer, get_modified_cache_dir
from ..source import source_open
from ..package import Package
from ..controller.local import _Image, VMNetFS, LocalController

if sys.platform == 'win32':
    from ..win32 import set_window_progress
else:
    def set_window_progress(_window, _progress):
        pass

# have_spice_viewer is a variable, not a constant
# pylint: disable=invalid-name
have_spice_viewer = False
try:
    import SpiceClientGtk
    # SpiceClientGtk.Session.open_fd(-1) doesn't work on < 0.10
    if LooseVersion(SpiceClientGtk.__version__) >= LooseVersion('0.10'):
        have_spice_viewer = True
except ImportError:
    pass
# pylint: enable=invalid-name

# VNC viewer is technically mandatory, but we defer ImportErrors until
# VNCWidget instantiation as a convenience for thin-client installs
# which will never use it
try:
    import gtkvnc
except ImportError:
    pass

# pylint chokes on Gtk widgets
# pylint: disable=incomplete-protocol

class _ViewerWidget(gtk.EventBox):
    __gsignals__ = {
        'viewer-get-fd': (gobject.SIGNAL_RUN_LAST, gobject.TYPE_NONE,
                (gobject.TYPE_OBJECT,)),
        'viewer-connect': (gobject.SIGNAL_RUN_LAST, gobject.TYPE_NONE, ()),
        'viewer-disconnect': (gobject.SIGNAL_RUN_LAST, gobject.TYPE_NONE, ()),
        'viewer-resize': (gobject.SIGNAL_RUN_LAST, gobject.TYPE_NONE,
                (gobject.TYPE_INT, gobject.TYPE_INT)),
        'viewer-keyboard-grab': (gobject.SIGNAL_RUN_LAST, gobject.TYPE_NONE,
                (gobject.TYPE_BOOLEAN,)),
        'viewer-mouse-grab': (gobject.SIGNAL_RUN_LAST, gobject.TYPE_NONE,
                (gobject.TYPE_BOOLEAN,)),
    }

    BACKOFF_TIMES = (1000, 2000, 5000, 10000)  # ms

    def __init__(self, max_mouse_rate=None):
        gtk.EventBox.__init__(self)
        # Must be updated by subclasses
        self.keyboard_grabbed = False
        self.mouse_grabbed = False

        self.connect('grab-focus', self._grab_focus)

        self._password = None
        self._want_reconnect = False
        self._backoff = BackoffTimer()
        self._backoff.connect('attempt', self._attempt_connection)
        self.connect('viewer-connect', self._connected)
        self.connect('viewer-disconnect', self._disconnected)

        self._last_motion_time = 0
        if max_mouse_rate is not None:
            self._motion_interval = 1000 // max_mouse_rate  # ms
        else:
            self._motion_interval = None

    def connect_viewer(self, password):
        '''Start a connection.  Emits viewer-get-fd one or more times; call
        set_fd() with the provided token and the resulting fd.'''
        self._password = password
        self._want_reconnect = True
        self._backoff.reset()
        self._backoff.attempt()

    def _attempt_connection(self, _backoff):
        self._connect_viewer(self._password)

    def _connected(self, _obj):
        self._backoff.reset()

    def _disconnected(self, _obj):
        if self._want_reconnect:
            self._backoff.attempt()

    def _connect_viewer(self, password):
        raise NotImplementedError

    def set_fd(self, data, fd):
        '''Pass fd=None if the connection attempt failed.'''
        raise NotImplementedError

    def disconnect_viewer(self):
        self._want_reconnect = False
        self._backoff.reset()
        self._disconnect_viewer()

    def _disconnect_viewer(self):
        raise NotImplementedError

    def get_pixbuf(self):
        raise NotImplementedError

    def _reemit(self, _wid, target):
        self.emit(target)

    def _grab_focus(self, _wid):
        self.get_child().grab_focus()

    def _connect_display_signals(self, widget):
        # Subclasses should call this after creating the actual display
        # widget.  The argument should be the widget that will be the
        # target of mouse grabs.
        if self._motion_interval is not None:
            widget.connect('motion-notify-event', self._motion)

    def _motion(self, _wid, motion):
        if motion.time < self._last_motion_time + self._motion_interval:
            # Motion event came too soon; ignore it
            return True
        else:
            self._last_motion_time = motion.time
            return False


class AspectBin(gtk.Bin):
    # Like an AspectFrame but without the frame.

    __gtype_name__ = 'AspectBin'

    def __init__(self):
        gtk.Bin.__init__(self)
        self.connect('grab-focus', self._grab_focus)

    def _grab_focus(self, _wid):
        child = self.get_child()
        if child is not None:
            child.grab_focus()

    def do_size_request(self, req):
        child = self.get_child()
        if child is not None:
            req.width, req.height = child.size_request()

    def do_size_allocate(self, alloc):
        self.allocation = alloc
        child = self.get_child()
        if child is not None:
            width, height = child.get_child_requisition()
            if width > 0 and height > 0:
                scale = min(1.0, alloc.width / width, alloc.height / height)
            else:
                scale = 1.0
            rect = gtk.gdk.Rectangle()
            rect.width = int(width * scale)
            rect.height = int(height * scale)
            rect.x = alloc.x + max(0, (alloc.width - rect.width) // 2)
            rect.y = alloc.y + max(0, (alloc.height - rect.height) // 2)
            child.size_allocate(rect)


class VNCWidget(_ViewerWidget):
    # Don't warn on reimport of gtkvnc
    # pylint: disable=redefined-outer-name
    def __init__(self, max_mouse_rate=None):
        # Ensure silent import succeeded.  If not, fail loudly this time.
        import gtkvnc

        _ViewerWidget.__init__(self, max_mouse_rate)
        aspect = AspectBin()
        self.add(aspect)
        self._vnc = gtkvnc.Display()
        aspect.add(self._vnc)

        self._vnc.connect('vnc-connected', self._reemit, 'viewer-connect')
        self._vnc.connect('vnc-disconnected', self._reemit,
                'viewer-disconnect')
        self._vnc.connect('vnc-desktop-resize', self._resize)
        self._vnc.connect('vnc-keyboard-grab', self._grab, 'keyboard', True)
        self._vnc.connect('vnc-keyboard-ungrab', self._grab, 'keyboard', False)
        self._vnc.connect('vnc-pointer-grab', self._grab, 'mouse', True)
        self._vnc.connect('vnc-pointer-ungrab', self._grab, 'mouse', False)
        self._connect_display_signals(self._vnc)
        self._vnc.set_pointer_grab(True)
        self._vnc.set_keyboard_grab(True)
        self._vnc.set_scaling(True)
    # pylint: enable=redefined-outer-name

    def _resize(self, _wid, width, height):
        self.emit('viewer-resize', width, height)

    def _grab(self, _wid, what, whether):
        setattr(self, what + '_grabbed', whether)
        self.emit('viewer-%s-grab' % what, whether)

    def _connect_viewer(self, password):
        self._disconnect_viewer()
        self._vnc.set_credential(gtkvnc.CREDENTIAL_PASSWORD, password)
        self.emit('viewer-get-fd', None)

    def set_fd(self, _data, fd):
        if fd is None:
            self.emit('viewer-disconnect')
        else:
            self._vnc.open_fd(fd)

    def _disconnect_viewer(self):
        self._vnc.close()

    def get_pixbuf(self):
        return self._vnc.get_pixbuf()
gobject.type_register(VNCWidget)


class SpiceWidget(_ViewerWidget):
    # Defer attribute lookups: SpiceClientGtk is conditionally imported
    _ERROR_EVENTS = (
        'CHANNEL_CLOSED',
        'CHANNEL_ERROR_AUTH',
        'CHANNEL_ERROR_CONNECT',
        'CHANNEL_ERROR_IO',
        'CHANNEL_ERROR_LINK',
        'CHANNEL_ERROR_TLS',
    )

    def __init__(self, max_mouse_rate=None):
        _ViewerWidget.__init__(self, max_mouse_rate)
        self._session = None
        self._gtk_session = None
        self._audio = None
        self._display_channel = None
        self._display = None
        self._display_showing = False
        self._accept_next_mouse_event = False
        self._error_events = set([getattr(SpiceClientGtk, e)
                for e in self._ERROR_EVENTS])

        self._aspect = AspectBin()
        self._placeholder = gtk.EventBox()
        self._placeholder.modify_bg(gtk.STATE_NORMAL, gtk.gdk.Color())
        self._placeholder.set_property('can-focus', True)
        self.add(self._placeholder)

    def _connect_viewer(self, password):
        self._disconnect_viewer()
        self._session = SpiceClientGtk.Session()
        self._session.set_property('password', password)
        self._session.set_property('enable-usbredir', False)
        # Ensure clipboard sharing is disabled
        self._gtk_session = SpiceClientGtk.spice_gtk_session_get(self._session)
        self._gtk_session.set_property('auto-clipboard', False)
        try:
            # Enable audio
            self._audio = SpiceClientGtk.Audio(self._session)
        except RuntimeError:
            # No local PulseAudio, etc.
            pass
        self._session.connect_object('channel-new', self._new_channel,
                self._session)
        self._session.open_fd(-1)

    def _new_channel(self, session, channel):
        if session != self._session:
            # Stale channel; ignore
            return
        channel.connect_object('open-fd', self._request_fd, channel)
        channel.connect_object('channel-event', self._channel_event, channel)
        type = SpiceClientGtk.spice_channel_type_to_string(
                channel.get_property('channel-type'))
        if type == 'display':
            # Create the display but don't show it until configured by
            # the server
            channel.connect_object('display-primary-create',
                    self._display_create, channel)
            self._destroy_display()
            self._display_channel = channel
            self._display = SpiceClientGtk.Display(self._session,
                    channel.get_property('channel-id'))
            # Default was False in spice-gtk < 0.14
            self._display.set_property('scaling', True)
            self._display.connect('size-request', self._size_request)
            self._display.connect('keyboard-grab', self._grab, 'keyboard')
            self._display.connect('mouse-grab', self._grab, 'mouse')
            self._connect_display_signals(self._display)

    def _display_create(self, channel, _format, _width, _height, _stride,
            _shmid, _imgdata):
        if channel is self._display_channel and not self._display_showing:
            # Display is now configured; show it
            self._display_showing = True
            self.remove(self._placeholder)
            self.add(self._aspect)
            self._aspect.add(self._display)
            self._aspect.show_all()
            self.emit('viewer-connect')

    def _request_fd(self, chan, _with_tls):
        try:
            self.emit('viewer-get-fd', chan)
        except TypeError:
            # Channel is invalid because the session was closed while the
            # event was sitting in the queue.
            pass

    def set_fd(self, data, fd):
        if fd is None:
            self._disconnect_viewer()
        else:
            data.open_fd(fd)

    def _channel_event(self, channel, event):
        try:
            if channel.get_property('spice-session') != self._session:
                # Stale channel; ignore
                return
        except TypeError:
            # Channel is invalid because the session was closed while the
            # event was sitting in the queue.
            return
        if event in self._error_events:
            self._disconnect_viewer()

    def _size_request(self, _wid, _req):
        if self._display is not None:
            width, height = self._display.get_size_request()
            if width > 1 and height > 1:
                self.emit('viewer-resize', width, height)

    def _grab(self, _wid, whether, what):
        setattr(self, what + '_grabbed', whether)
        self.emit('viewer-%s-grab' % what, whether)

    def _motion(self, wid, motion):
        # In server mouse mode, spice-gtk warps the pointer after every
        # motion.  The next motion event it receives (generated by the warp)
        # is only used to set the zero point for the following event.  We
        # therefore have to accept motion events in pairs.
        if self._accept_next_mouse_event:
            # Accept motion
            self._accept_next_mouse_event = False
            return False
        if _ViewerWidget._motion(self, wid, motion):
            # Reject motion
            return True
        # Accept motion
        self._accept_next_mouse_event = True
        return False

    def _destroy_display(self):
        if self._display is not None:
            self._display.destroy()
            self._display = None
            if self.get_children() and self._display_showing:
                self.remove(self._aspect)
                self.add(self._placeholder)
                self._placeholder.show()
            self._display_showing = False

    def _disconnect_viewer(self):
        if self._session is not None:
            self._destroy_display()
            self._display_channel = None
            self._session.disconnect()
            self._audio = None
            self._gtk_session = None
            self._session = None
            for what in 'keyboard', 'mouse':
                self._grab(None, False, what)
            self.emit('viewer-disconnect')

    def get_pixbuf(self):
        if self._display is None:
            return None
        return self._display.get_pixbuf()
gobject.type_register(SpiceWidget)


class StatusBarWidget(gtk.HBox):
    def __init__(self, viewer, is_remote=False):
        gtk.HBox.__init__(self, spacing=3)
        self._theme = gtk.icon_theme_get_default()

        self._warnings = gtk.HBox()
        self.pack_start(self._warnings, expand=False)

        self.pack_start(gtk.Label())  # filler

        def add_icon(name, sensitive):
            icon = self._get_icon(name)
            icon.set_sensitive(sensitive)
            self.pack_start(icon, expand=False)
            return icon

        self._dirty_state_label = gtk.Label('-/-')
        self._dirty_state_label.set_padding(3, 0)
        self.pack_start(self._dirty_state_label, expand=False)

        escape_label = gtk.Label('Ctrl-Alt')
        escape_label.set_padding(3, 0)
        self.pack_start(escape_label, expand=False)

        keyboard_icon = add_icon('input-keyboard', viewer.keyboard_grabbed)
        mouse_icon = add_icon('input-mouse', viewer.mouse_grabbed)
        if is_remote:
            add_icon('network-idle', True)
        else:
            add_icon('computer', True)

        viewer.connect('viewer-keyboard-grab', self._grabbed, keyboard_icon)
        viewer.connect('viewer-mouse-grab', self._grabbed, mouse_icon)

    def _get_icon(self, name):
        icon = gtk.Image()
        icon.set_from_pixbuf(self._theme.load_icon(name, 24, 0))
        return icon

    def _grabbed(self, _wid, grabbed, icon):
        icon.set_sensitive(grabbed)

    def update_dirty_state(self, disk_total, memory_total):
        color = 'green'
        '''
        text = '<span foreground="%s">%d chunks / %d chunks</span>' % \
                (color, disk_total / 131072,  # / (1024 * 1024 * 8),
                    memory_total / 131072) # (1024 * 1024 * 8)))
        '''
        text = '<span foreground="%s">%d memory chunks / %d disk chunks</span>' % \
                (color, memory_total /131072, disk_total / 131072)

        self._dirty_state_label.set_markup(text)

    def add_warning(self, icon, message):
        image = self._get_icon(icon)
        image.set_tooltip_markup(message)
        self._warnings.pack_start(image)
        image.show()
        return image

    def remove_warning(self, warning):
        self._warnings.remove(warning)


class VMWindow(gtk.Window):
    INITIAL_VIEWER_SIZE = (640, 480)
    MIN_SCALE = 0.25
    SCREEN_SIZE_FUDGE = (-100, -100)

    __gsignals__ = {
        'viewer-get-fd': (gobject.SIGNAL_RUN_LAST, gobject.TYPE_NONE,
                (gobject.TYPE_OBJECT,)),
        'viewer-connect': (gobject.SIGNAL_RUN_LAST, gobject.TYPE_NONE, ()),
        'viewer-disconnect': (gobject.SIGNAL_RUN_LAST, gobject.TYPE_NONE, ()),
        'user-screenshot': (gobject.SIGNAL_RUN_LAST, gobject.TYPE_NONE,
                (gtk.gdk.Pixbuf,)),
        'user-restart': (gobject.SIGNAL_RUN_LAST, gobject.TYPE_NONE, ()),
        'user-quit': (gobject.SIGNAL_RUN_LAST, gobject.TYPE_NONE, ()),
    }

    def __init__(self, name, disk_stats, disk_chunks, disk_chunk_size,
            use_spice=True, max_mouse_rate=None, is_remote=False):
        gtk.Window.__init__(self)
        self._agrp = VMActionGroup(self)
        for sig in 'user-restart', 'user-quit':
            self._agrp.connect(sig, lambda _obj, s: self.emit(s), sig)
        self._agrp.connect('user-screenshot', self._screenshot)

        self.set_title(name)
        self.connect('window-state-event', self._window_state_changed)
        self.connect('delete-event',
                lambda _wid, _ev:
                self._agrp.get_action('quit').activate() or True)
        self.connect('destroy', self._destroy)

        self._log = LogWindow(name, self._agrp.get_action('show-log'))
        if disk_stats and disk_chunks and disk_chunk_size:
            self._activity = ActivityWindow(name, disk_stats, disk_chunks,
                    disk_chunk_size, self._agrp.get_action('show-activity'))
            self._agrp.set_statistics_available(True)
        else:
            self._activity = None

        self._viewer_width, self._viewer_height = self.INITIAL_VIEWER_SIZE
        self._is_fullscreen = False

        box = gtk.VBox()
        self.add(box)

        def item(name):
            return self._agrp.get_action(name).create_tool_item()
        tbar = gtk.Toolbar()
        tbar.set_style(gtk.TOOLBAR_ICONS)
        tbar.set_icon_size(gtk.ICON_SIZE_LARGE_TOOLBAR)
        tbar.insert(item('quit'), -1)
        tbar.insert(item('restart'), -1)
        tbar.insert(item('fullscreen'), -1)
        tbar.insert(item('screenshot'), -1)
        tbar.insert(gtk.SeparatorToolItem(), -1)
        tbar.insert(item('show-activity'), -1)
        tbar.insert(item('show-log'), -1)
        box.pack_start(tbar, expand=False)

        if use_spice:
            self._viewer = SpiceWidget(max_mouse_rate)
        else:
            self._viewer = VNCWidget(max_mouse_rate)
        self._viewer.connect('viewer-get-fd', self._viewer_get_fd)
        self._viewer.connect('viewer-resize', self._viewer_resized)
        self._viewer.connect('viewer-connect', self._viewer_connected)
        self._viewer.connect('viewer-disconnect', self._viewer_disconnected)
        box.pack_start(self._viewer)
        self.set_geometry_hints(self._viewer,
                min_width=self._viewer_width, max_width=self._viewer_width,
                min_height=self._viewer_height, max_height=self._viewer_height)
        self._viewer.grab_focus()

        self._statusbar = StatusBarWidget(self._viewer, is_remote)
        box.pack_end(self._statusbar, expand=False)

    def update_dirty_state(self, disk_total, memory_total):
        self._statusbar.update_dirty_state(disk_total, memory_total)

    def set_vm_running(self, running):
        self._agrp.set_vm_running(running)

    def connect_viewer(self, password):
        self._viewer.connect_viewer(password)

    def set_viewer_fd(self, data, fd):
        self._viewer.set_fd(data, fd)

    def disconnect_viewer(self):
        self._viewer.disconnect_viewer()

    def show_activity(self, enabled):
        if self._activity is None:
            return
        if enabled:
            self._activity.show()
        else:
            self._activity.hide()

    def show_log(self, enabled):
        if enabled:
            self._log.show()
        else:
            self._log.hide()

    def add_warning(self, icon, message):
        return self._statusbar.add_warning(icon, message)

    def remove_warning(self, warning):
        self._statusbar.remove_warning(warning)

    def take_screenshot(self):
        return self._viewer.get_pixbuf()

    def _viewer_get_fd(self, _obj, data):
        self.emit('viewer-get-fd', data)

    def _viewer_connected(self, _obj):
        self._agrp.set_viewer_connected(True)
        self.emit('viewer-connect')

    def _viewer_disconnected(self, _obj):
        self._agrp.set_viewer_connected(False)
        self.emit('viewer-disconnect')

    def _update_window_size_constraints(self):
        # If fullscreen, constrain nothing.
        if self._is_fullscreen:
            self.set_geometry_hints(self._viewer)
            return

        # Update window geometry constraints for the guest screen size.
        # We would like to use min_aspect and max_aspect as well, but they
        # seem to apply to the whole window rather than the geometry widget.
        self.set_geometry_hints(self._viewer,
                min_width=int(self._viewer_width * self.MIN_SCALE),
                min_height=int(self._viewer_height * self.MIN_SCALE),
                max_width=self._viewer_width, max_height=self._viewer_height)

        # Resize the window to the largest size that can comfortably fit on
        # the screen, constrained by the maximums.
        screen = self.get_screen()
        monitor = screen.get_monitor_at_window(self.get_window())
        geom = screen.get_monitor_geometry(monitor)
        ow, oh = self.SCREEN_SIZE_FUDGE
        self.resize(max(1, geom.width + ow), max(1, geom.height + oh))

    def _viewer_resized(self, _wid, width, height):
        self._viewer_width = width
        self._viewer_height = height
        self._update_window_size_constraints()

    def _window_state_changed(self, _obj, event):
        if event.changed_mask & gtk.gdk.WINDOW_STATE_FULLSCREEN:
            self._is_fullscreen = bool(event.new_window_state &
                    gtk.gdk.WINDOW_STATE_FULLSCREEN)
            self._agrp.get_action('fullscreen').set_active(self._is_fullscreen)
            self._update_window_size_constraints()

    def _screenshot(self, _obj):
        self.emit('user-screenshot', self._viewer.get_pixbuf())

    def _destroy(self, _wid):
        self._log.destroy()
        if self._activity is not None:
            self._activity.destroy()
gobject.type_register(VMWindow)


class VMActionGroup(gtk.ActionGroup):
    __gsignals__ = {
        'user-screenshot': (gobject.SIGNAL_RUN_LAST, gobject.TYPE_NONE, ()),
        'user-restart': (gobject.SIGNAL_RUN_LAST, gobject.TYPE_NONE, ()),
        'user-quit': (gobject.SIGNAL_RUN_LAST, gobject.TYPE_NONE, ()),
    }

    def __init__(self, parent):
        gtk.ActionGroup.__init__(self, 'vmnetx-global')
        def add_nonstock(name, label, tooltip, icon, handler):
            action = gtk.Action(name, label, tooltip, None)
            action.set_icon_name(icon)
            action.connect('activate', handler, parent)
            self.add_action(action)
        self.add_actions((
            ('restart', 'gtk-refresh', None, None, 'Restart', self._restart),
            ('quit', 'gtk-quit', None, None, 'Quit', self._quit),
        ), user_data=parent)
        add_nonstock('screenshot', 'Screenshot', 'Take Screenshot',
                'camera-photo', self._screenshot)
        self.add_toggle_actions((
            ('fullscreen', 'gtk-fullscreen', 'Full screen', None,
                    'Toggle full screen', self._fullscreen),
            ('show-activity', 'gtk-properties', 'Activity', None,
                    'Show virtual machine activity', self._show_activity),
            ('show-log', 'gtk-file', 'Log', None,
                    'Show log', self._show_log),
        ), user_data=parent)
        self.set_vm_running(False)
        self.set_viewer_connected(False)
        self.set_statistics_available(False)

    def set_vm_running(self, running):
        for name in ('restart',):
            self.get_action(name).set_sensitive(running)

    def set_viewer_connected(self, connected):
        for name in ('screenshot',):
            self.get_action(name).set_sensitive(connected)

    def set_statistics_available(self, available):
        for name in ('show-activity',):
            self.get_action(name).set_sensitive(available)

    def _confirm(self, parent, signal, message):
        dlg = gtk.MessageDialog(parent=parent,
                type=gtk.MESSAGE_WARNING,
                buttons=gtk.BUTTONS_OK_CANCEL,
                flags=gtk.DIALOG_MODAL | gtk.DIALOG_DESTROY_WITH_PARENT,
                message_format=message)
        dlg.set_default_response(gtk.RESPONSE_OK)
        result = dlg.run()
        dlg.destroy()
        if result == gtk.RESPONSE_OK:
            self.emit(signal)

    def _screenshot(self, _action, _parent):
        self.emit('user-screenshot')

    def _restart(self, _action, parent):
        self._confirm(parent, 'user-restart',
                'Really reboot the guest?  Unsaved data will be lost.')

    def _quit(self, _action, parent):
        self._confirm(parent, 'user-quit',
                'Really quit?  All changes will be lost.')

    def _fullscreen(self, action, parent):
        if action.get_active():
            parent.fullscreen()
        else:
            parent.unfullscreen()

    def _show_activity(self, action, parent):
        parent.show_activity(action.get_active())

    def _show_log(self, action, parent):
        parent.show_log(action.get_active())
gobject.type_register(VMActionGroup)


class _MainLoopCallbackHandler(logging.Handler):
    def __init__(self, callback):
        logging.Handler.__init__(self)
        self._callback = callback

    def emit(self, record):
        gobject.idle_add(self._callback, self.format(record))


class _LogWidget(gtk.ScrolledWindow):
    FONT = 'monospace 8'
    MIN_HEIGHT = 150

    def __init__(self):
        gtk.ScrolledWindow.__init__(self)
        self.set_policy(gtk.POLICY_NEVER, gtk.POLICY_AUTOMATIC)
        self._textview = gtk.TextView()
        self._textview.set_editable(False)
        self._textview.set_cursor_visible(False)
        self._textview.set_wrap_mode(gtk.WRAP_WORD_CHAR)
        font = pango.FontDescription(self.FONT)
        self._textview.modify_font(font)
        width = self._textview.get_pango_context().get_metrics(font,
                None).get_approximate_char_width()
        self._textview.set_size_request(80 * width // pango.SCALE,
                self.MIN_HEIGHT)
        self.add(self._textview)
        self._handler = _MainLoopCallbackHandler(self._log)
        logging.getLogger().addHandler(self._handler)
        self.connect('destroy', self._destroy)

    def _log(self, line):
        buf = self._textview.get_buffer()
        buf.insert(buf.get_end_iter(), line + '\n')

    def _destroy(self, _wid):
        logging.getLogger().removeHandler(self._handler)


class LogWindow(gtk.Window):
    def __init__(self, name, hide_action):
        gtk.Window.__init__(self)
        self.set_title('Log: %s' % name)
        self.set_type_hint(gtk.gdk.WINDOW_TYPE_HINT_UTILITY)
        self.connect('delete-event',
                lambda _wid, _ev: hide_action.activate() or True)

        widget = _LogWidget()
        self.add(widget)
        widget.show_all()


class ImageChunkWidget(gtk.DrawingArea):
    PATTERNS = {
        ChunkStateArray.INVALID: cairo.SolidPattern(0, 0, 0),
        ChunkStateArray.MISSING: cairo.SolidPattern(.35, .35, .35),
        ChunkStateArray.CACHED: cairo.SolidPattern(.63, .63, .63),
        ChunkStateArray.ACCESSED: cairo.SolidPattern(1, 1, 1),
        ChunkStateArray.MODIFIED: cairo.SolidPattern(.45, 0, 0),
        ChunkStateArray.ACCESSED_MODIFIED: cairo.SolidPattern(1, 0, 0),
        ChunkStateArray.UPLOADED: cairo.SolidPattern(0, 0, 1),
    }

    TIP = """Red: Accessed and modified this session
White: Accessed this session
Dark red: Modified this session
Light gray: Fetched in previous session
Dark gray: Not present"""

    # CHUNK MAP IS A ChunkStateArray


    def __init__(self, chunk_map):
        gtk.DrawingArea.__init__(self)
        self._map = chunk_map
        self._map_chunk_handler = None
        self._map_resize_handler = None
        self._width_history = [0, 0]
        self.set_tooltip_text(self.TIP)
        self.connect('realize', self._realize)
        self.connect('unrealize', self._unrealize)
        self.connect('configure-event', self._configure)
        self.connect('expose-event', self._expose)

    # pylint doesn't understand allocation.width
    # pylint: disable=no-member
    @property
    def valid_rows(self):
        """Return the number of rows where at least one pixel corresponds
        to a chunk."""
        row_width = self.allocation.width
        return (len(self._map) + row_width - 1) // row_width
    # pylint: enable=no-member

    def _realize(self, _widget):
        self._map_chunk_handler = self._map.connect('chunk-state-changed',
                self._chunk_changed)
        self._map_resize_handler = self._map.connect('image-resized',
                self._image_resized)
        self.queue_resize_no_redraw()

    def _unrealize(self, _widget):
        self._map.disconnect(self._map_chunk_handler)
        self._map.disconnect(self._map_resize_handler)

    def _configure(self, _widget, event):
        self._width_history.append(event.width)
        if (self._width_history.pop(0) == event.width and
                abs(self._width_history[0] - event.width) > 10):
            # We are cycling between two size allocations with significantly
            # different widths, which probably indicates that a parent
            # gtk.ScrolledWindow is oscillating adding and removing the
            # scroll bar.  This can happen when the viewport's size
            # allocation, with scroll bar, is just above the number of
            # pixels we need for the whole image.  Break the loop by
            # refusing to update our size request.
            return
        self.set_size_request(30, self.valid_rows)

    # pylint doesn't understand allocation.width or window.cairo_create()
    # pylint: disable=no-member
    def _expose(self, _widget, event):
        # This function is optimized; be careful when changing it.
        # Localize variables for performance (!!)
        patterns = self.PATTERNS
        chunk_states = self._map
        chunks = len(chunk_states)
        area_x, area_y, area_height, area_width = (event.area.x,
                event.area.y, event.area.height, event.area.width)
        row_width = self.allocation.width
        valid_rows = self.valid_rows
        default_state = ChunkStateArray.MISSING
        invalid_state = ChunkStateArray.INVALID

        cr = self.window.cairo_create()
        set_source = cr.set_source
        rectangle = cr.rectangle
        fill = cr.fill

        # Draw MISSING as background color in valid rows
        if valid_rows > area_y:
            set_source(patterns[default_state])
            rectangle(area_x, area_y, area_width,
                    min(area_height, valid_rows - area_y))
            fill()

        # Draw invalid rows
        if valid_rows < area_y + area_height:
            set_source(patterns[invalid_state])
            rectangle(area_x, valid_rows, area_width,
                    area_y + area_height - valid_rows)
            fill()

        # Fill in valid rows.  Avoid drawing MISSING chunks, since those
        # are handled by the background fill.  Combine adjacent pixels
        # of the same color on the same line into a single rectangle.
        last_state = None
        for y in xrange(area_y, min(area_y + area_height, valid_rows)):
            first_x = area_x
            for x in xrange(area_x, area_x + area_width):
                chunk = y * row_width + x
                if chunk < chunks:
                    state = chunk_states[chunk]
                else:
                    state = invalid_state
                if state != last_state:
                    if x > first_x and last_state != default_state:
                        rectangle(first_x, y, x - first_x, 1)
                        fill()
                    set_source(patterns[state])
                    first_x = x
                    last_state = state
                # HANDLE NEGATIVE THING??
                # if state
            if state != default_state:
                rectangle(first_x, y, area_x + area_width - first_x, 1)
                fill()
    # pylint: enable=no-member

    # pylint doesn't understand allocation.width
    # pylint: disable=no-member
    def _chunk_changed(self, _map, first, last):
        width = self.allocation.width
        for row in xrange(first // width, last // width + 1):
            row_first = max(width * row, first) % width
            row_last = min(width * (row + 1) - 1, last) % width
            self.queue_draw_area(row_first, row, row_last - row_first + 1, 1)
    # pylint: enable=no-member

    def _image_resized(self, _map, _chunks):
        self.queue_resize_no_redraw()


class ScrollingImageChunkWidget(gtk.ScrolledWindow):
    def __init__(self, chunk_map):
        gtk.ScrolledWindow.__init__(self)
        self.set_border_width(2)
        self.set_policy(gtk.POLICY_NEVER, gtk.POLICY_AUTOMATIC)
        self.add_with_viewport(ImageChunkWidget(chunk_map))
        viewport = self.get_child()
        viewport.set_shadow_type(gtk.SHADOW_NONE)


class StatWidget(gtk.EventBox):
    ACTIVITY_FLAG = gtk.gdk.Color('#ff4040')

    def __init__(self, stat, chunk_size=None, tooltip=None):
        gtk.EventBox.__init__(self)
        self._chunk_size = chunk_size
        self._stat = stat
        self._stat_handler = None
        self._label = gtk.Label('--')
        self._label.set_width_chars(7)
        self._label.set_alignment(1, 0.5)
        self.add(self._label)
        if tooltip:
            self.set_tooltip_text(tooltip)
        self._timer = None
        self.connect('realize', self._realize)
        self.connect('unrealize', self._unrealize)

    def _realize(self, _widget):
        self._label.set_text(self._format(self._stat.value))
        self._stat_handler = self._stat.connect('stat-changed', self._changed)

    def _unrealize(self, _widget):
        self._stat.disconnect(self._stat_handler)

    def _format(self, value):
        """Override this in subclasses."""
        return str(value)

    def _changed(self, _stat, _name, value):
        new = self._format(value)
        if self._label.get_text() != new:
            # Avoid unnecessary redraws
            self._label.set_text(new)

        # Update activity flag
        if self._timer is None:
            self.modify_bg(gtk.STATE_NORMAL, self.ACTIVITY_FLAG)
        else:
            # Clear timer before setting a new one
            glib.source_remove(self._timer)
        self._timer = glib.timeout_add(100, self._clear_flag)

    def _clear_flag(self):
        self.modify_bg(gtk.STATE_NORMAL, None)
        self._timer = None
        return False


class MBStatWidget(StatWidget):
    def _format(self, value):
        return '%.1f' % (value / (1 << 20))


class ChunkMBStatWidget(StatWidget):
    def _format(self, value):
        return '%.1f' % (value * self._chunk_size / (1 << 20))


class ImageStatTableWidget(gtk.Table):
    FIELDS = (
        ('Guest', (
            ('bytes_read', MBStatWidget,
                'Data read by guest OS this session (MB)'),
            ('bytes_written', MBStatWidget,
                'Data written by guest OS this session (MB)'),
        )),
        ('State', (
            ('chunk_fetches', ChunkMBStatWidget,
                'Distinct chunks fetched this session (MB)'),
            ('chunk_dirties', ChunkMBStatWidget,
                'Distinct chunks modified this session (MB)'),
        )),
    )

    def __init__(self, stats, chunk_size):
        gtk.Table.__init__(self, len(self.FIELDS), 3, True)
        self.set_border_width(2)
        for row, info in enumerate(self.FIELDS):
            caption, fields = info
            label = gtk.Label(caption)
            label.set_alignment(0, 0.5)
            self.attach(label, 0, 1, row, row + 1, xoptions=gtk.FILL)
            for col, info in enumerate(fields, 1):
                name, cls, tooltip = info
                field = cls(stats[name], chunk_size, tooltip)
                self.attach(field, col, col + 1, row, row + 1,
                        xoptions=gtk.FILL, xpadding=3, ypadding=2)


class ImageStatusWidget(gtk.VBox):
    def __init__(self, stats, chunk_map, chunk_size):
        gtk.VBox.__init__(self, spacing=5)

        # Stats table
        frame = gtk.Frame('Statistics')
        frame.add(ImageStatTableWidget(stats, chunk_size))
        self.pack_start(frame, expand=False)

        # Chunk bitmap
        frame = gtk.Frame('Chunk bitmap')
        vbox = gtk.VBox()
        label = gtk.Label()
        label.set_markup('<span size="small">Chunk size: %d KB</span>' %
                (chunk_size / 1024))
        label.set_alignment(0, 0.5)
        label.set_padding(2, 2)
        vbox.pack_start(label, expand=False)
        vbox.pack_start(ScrollingImageChunkWidget(chunk_map))
        frame.add(vbox)
        self.pack_start(frame)


class ActivityWindow(gtk.Window):
    def __init__(self, name, stats, chunk_map, chunk_size, hide_action):
        gtk.Window.__init__(self)
        self.set_title('Activity: %s' % name)
        self.set_type_hint(gtk.gdk.WINDOW_TYPE_HINT_UTILITY)
        self.connect('delete-event',
                lambda _wid, _ev: hide_action.activate() or True)

        status = ImageStatusWidget(stats, chunk_map, chunk_size)
        self.add(status)
        status.show_all()


class LoadProgressWindow(gtk.Dialog):
    __gsignals__ = {
        'user-cancel': (gobject.SIGNAL_RUN_LAST, gobject.TYPE_NONE, ()),
    }

    def __init__(self, parent):
        gtk.Dialog.__init__(self, parent.get_title(), parent, gtk.DIALOG_MODAL,
                (gtk.STOCK_CANCEL, gtk.RESPONSE_CANCEL))
        self._parent = parent
        self.set_resizable(False)
        self.connect('response', self._response)

        self._progress = gtk.ProgressBar()
        self.connect('destroy', self._destroy)

        box = self.get_content_area()

        label = gtk.Label()
        label.set_markup('<b>Loading...</b>')
        label.set_alignment(0, 0.5)
        label.set_padding(5, 5)
        box.pack_start(label)

        bin = gtk.Alignment(xscale=1)
        bin.add(self._progress)
        bin.set_padding(5, 5, 3, 3)
        box.pack_start(bin, expand=True)

        # Ensure a minimum width for the progress bar, without affecting
        # its height
        label = gtk.Label()
        label.set_size_request(300, 0)
        box.pack_start(label)

    def _destroy(self, _wid):
        set_window_progress(self._parent, None)

    def progress(self, count, total):
        if total != 0:
            fraction = count / total
        else:
            fraction = 1
        self._progress.set_fraction(fraction)
        set_window_progress(self._parent, fraction)

    def _response(self, _wid, _id):
        self.hide()
        set_window_progress(self._parent, None)
        self.emit('user-cancel')
gobject.type_register(LoadProgressWindow)

class CheckinProgressWindow(gtk.Dialog):
    __gsignals__ = {
        'user-cancel': (gobject.SIGNAL_RUN_LAST, gobject.TYPE_NONE, ()),
    }

    def __init__(self, parent):
        gtk.Dialog.__init__(self, parent.get_title(), parent, gtk.DIALOG_MODAL,
                (gtk.STOCK_CANCEL, gtk.RESPONSE_CANCEL))
        self._parent = parent
        self.set_resizable(False)
        self.connect('response', self._response)

        self._disk_progress = gtk.ProgressBar()
        self._memory_progress = gtk.ProgressBar()
        self.connect('destroy', self._destroy)

        box = self.get_content_area()

        label = gtk.Label()
        label.set_markup('<b>Checking in...</b>')
        label.set_alignment(0, 0.5)
        label.set_padding(5, 5)
        box.pack_start(label)

        self._disk_label = gtk.Label('Disk Chunks [-/-]')
        self._disk_label.set_alignment(0, 0.5)
        box.pack_start(self._disk_label)

        bin = gtk.Alignment(xscale=1)
        bin.add(self._disk_progress)
        bin.set_padding(5, 5, 3, 3)
        box.pack_start(bin, expand=True)

        self._memory_label = gtk.Label('Memory Chunks [-/-]')
        self._memory_label.set_alignment(0, 0.5)
        box.pack_start(self._memory_label)

        bin2 = gtk.Alignment(xscale=1)
        bin2.add(self._memory_progress)
        bin2.set_padding(5, 5, 3, 3)
        box.pack_start(bin2, expand=True)

        # Ensure a minimum width for the progress bar, without affecting
        # its height
        label = gtk.Label()
        label.set_size_request(300, 0)
        box.pack_start(label)

    def _destroy(self, _wid):
        set_window_progress(self._parent, None)
        self.emit('user-cancel')

    def _b2kb(self, bytes):
        return bytes / (1024 * 8)

    def progress(self, disk_count, disk_total, memory_count, memory_total):
        if disk_total != 0:
            disk_fraction = disk_count / disk_total
        else:
            disk_fraction = 1
        self._disk_progress.set_fraction(disk_fraction)
        self._disk_label.set_text('Disk Chunks [%d/%d KB]' % \
                (self._b2kb(disk_count), self._b2kb(disk_total)))

        if memory_total != 0:
            memory_fraction = memory_count / memory_total
        else:
            memory_fraction = 1
        self._memory_progress.set_fraction(memory_fraction)
        self._memory_label.set_text('Memory Chunks [%d/%d KB]' % \
                (self._b2kb(memory_count), self._b2kb(memory_total)))

        # WHAT TO DO ABOUT WINDOWS
        set_window_progress(self._parent, memory_fraction)

    def _response(self, _wid, _id):
        self.hide()
        set_window_progress(self._parent, None)
        self.emit('user-cancel')
gobject.type_register(CheckinProgressWindow)


class PasswordWindow(gtk.Dialog):
    def __init__(self, site, realm):
        gtk.Dialog.__init__(self, 'Log in', None, gtk.DIALOG_MODAL,
                (gtk.STOCK_CANCEL, gtk.RESPONSE_CANCEL, gtk.STOCK_OK,
                gtk.RESPONSE_OK))
        self.set_default_response(gtk.RESPONSE_OK)
        self.set_resizable(False)
        self.connect('response', self._response)

        table = gtk.Table()
        table.set_border_width(5)
        self.get_content_area().pack_start(table)

        row = 0
        for text in 'Site', 'Realm', 'Username', 'Password':
            label = gtk.Label(text + ':')
            label.set_alignment(1, 0.5)
            table.attach(label, 0, 1, row, row + 1, xpadding=5, ypadding=5)
            row += 1
        self._invalid = gtk.Label()
        self._invalid.set_markup('<span foreground="red">Invalid username' +
                ' or password.</span>')
        table.attach(self._invalid, 0, 2, row, row + 1, xpadding=5, ypadding=5)
        row += 1

        self._username = gtk.Entry()
        self._username.connect('activate', self._activate_username)
        self._password = gtk.Entry()
        self._password.set_visibility(False)
        self._password.set_activates_default(True)
        row = 0
        for text in site, realm:
            label = gtk.Label(text)
            label.set_alignment(0, 0.5)
            table.attach(label, 1, 2, row, row + 1, xpadding=5, ypadding=5)
            row += 1
        for widget in self._username, self._password:
            table.attach(widget, 1, 2, row, row + 1)
            row += 1

        table.show_all()
        self._invalid.hide()

    @property
    def username(self):
        return self._username.get_text()

    @username.setter
    def username(self, value):
        # Side effect: set focus to password field
        self._username.set_text(value)
        self._password.grab_focus()

    @property
    def password(self):
        return self._password.get_text()

    def _activate_username(self, _wid):
        self._password.grab_focus()

    def _set_sensitive(self, sensitive):
        self._username.set_sensitive(sensitive)
        self._password.set_sensitive(sensitive)
        for id in gtk.RESPONSE_OK, gtk.RESPONSE_CANCEL:
            self.set_response_sensitive(id, sensitive)
        self.set_deletable(sensitive)

        if not sensitive:
            self._invalid.hide()

    def _response(self, _wid, resp):
        if resp == gtk.RESPONSE_OK:
            self._set_sensitive(False)

    def fail(self):
        self._set_sensitive(True)
        self._invalid.show()
        self._password.grab_focus()


class SaveMediaWindow(gtk.FileChooserDialog):
    PREVIEW_SIZE = 250

    def __init__(self, parent, title, filename, preview):
        gtk.FileChooserDialog.__init__(self, title, parent,
                gtk.FILE_CHOOSER_ACTION_SAVE,
                (gtk.STOCK_CANCEL, gtk.RESPONSE_CANCEL,
                gtk.STOCK_SAVE, gtk.RESPONSE_OK))
        self.set_current_name(filename)
        self.set_do_overwrite_confirmation(True)

        w, h = preview.get_width(), preview.get_height()
        scale = min(1, self.PREVIEW_SIZE / w, self.PREVIEW_SIZE / h)
        preview = preview.scale_simple(int(w * scale), int(h * scale),
                gtk.gdk.INTERP_BILINEAR)
        image = gtk.Image()
        image.set_from_pixbuf(preview)
        image.set_padding(5, 5)
        frame = gtk.Frame('Preview')
        frame.add(image)
        image.show()
        self.set_preview_widget(frame)
        self.set_use_preview_label(False)


class UpdateWindow(gtk.MessageDialog):
    ICON_SIZE = 64

    __gsignals__ = {
        'user-defer-update': (gobject.SIGNAL_RUN_LAST, gobject.TYPE_NONE, ()),
        'user-skip-release': (gobject.SIGNAL_RUN_LAST, gobject.TYPE_NONE, ()),
        'user-update': (gobject.SIGNAL_RUN_LAST, gobject.TYPE_NONE, ()),
    }

    def __init__(self, parent, version, date):
        gtk.MessageDialog.__init__(self, parent,
                gtk.DIALOG_DESTROY_WITH_PARENT, gtk.MESSAGE_INFO,
                gtk.BUTTONS_NONE, 'VMNetX update available')
        theme = gtk.icon_theme_get_default()
        try:
            icon = theme.load_icon('vmnetx', 256, 0)
            icon = icon.scale_simple(self.ICON_SIZE, self.ICON_SIZE,
                    gtk.gdk.INTERP_BILINEAR)
        except glib.GError:
            # VMNetX icon not installed in search path
            icon = theme.load_icon('software-update-available',
                    self.ICON_SIZE, 0)
        self.set_image(gtk.image_new_from_pixbuf(icon))
        self.set_title('Update Available')
        datestr = '%s %s, %s' % (
            date.strftime('%B'),
            date.strftime('%d').lstrip('0'),
            date.strftime('%Y')
        )
        self.format_secondary_markup(
                'VMNetX <b>%s</b> was released on <b>%s</b>.' % (
                urllib.quote(version), datestr))
        self.add_buttons('Skip this version', gtk.RESPONSE_REJECT,
                'Remind me later', gtk.RESPONSE_CLOSE,
                'Download update', gtk.RESPONSE_ACCEPT)
        self.set_default_response(gtk.RESPONSE_ACCEPT)
        self.connect('response', self._response)

    def _response(self, _wid, response):
        if response == gtk.RESPONSE_ACCEPT:
            self.emit('user-update')
        elif response == gtk.RESPONSE_REJECT:
            self.emit('user-skip-release')
        else:
            self.emit('user-defer-update')


class ErrorWindow(gtk.MessageDialog):
    def __init__(self, parent, message):
        gtk.MessageDialog.__init__(self, parent=parent,
                flags=gtk.DIALOG_MODAL | gtk.DIALOG_DESTROY_WITH_PARENT,
                type=gtk.MESSAGE_ERROR, buttons=gtk.BUTTONS_OK,
                message_format='Error')
        self.format_secondary_text(message)


class IgnorableErrorWindow(gtk.MessageDialog):
    def __init__(self, parent, message):
        gtk.MessageDialog.__init__(self, parent=parent,
                flags=gtk.DIALOG_MODAL | gtk.DIALOG_DESTROY_WITH_PARENT,
                type=gtk.MESSAGE_ERROR, buttons=gtk.BUTTONS_NONE,
                message_format='Error')
        self.format_secondary_text(message)
        self.add_buttons('Continue', gtk.RESPONSE_CANCEL,
                gtk.STOCK_QUIT, gtk.RESPONSE_OK)
        self.set_default_response(gtk.RESPONSE_OK)


class FatalErrorWindow(gtk.MessageDialog):
    def __init__(self, parent, error=None):
        gtk.MessageDialog.__init__(self, parent=parent,
                flags=gtk.DIALOG_MODAL | gtk.DIALOG_DESTROY_WITH_PARENT,
                type=gtk.MESSAGE_ERROR, buttons=gtk.BUTTONS_OK,
                message_format='Fatal Error')
        if error is None:
            error = ErrorBuffer()
        self.format_secondary_text(error.exception)
        content = self.get_content_area()

        if error.detail:
            expander = gtk.Expander('Details')
            content.pack_start(expander)

            view = gtk.TextView()
            view.get_buffer().set_text(error.detail)
            view.set_editable(False)
            scroller = gtk.ScrolledWindow()
            view.set_scroll_adjustments(scroller.get_hadjustment(),
                    scroller.get_vadjustment())
            scroller.set_policy(gtk.POLICY_AUTOMATIC, gtk.POLICY_AUTOMATIC)
            scroller.add(view)
            scroller.set_size_request(600, 150)
            expander.add(scroller)

        # RHEL 6 doesn't have MessageDialog.get_widget_for_response()
        self.get_action_area().get_children()[0].grab_focus()
        content.show_all()

##############################################################################

class NewServerWindow(gtk.Dialog):
    def __init__(self):
        gtk.Dialog.__init__(self, 'Add new server', None, gtk.DIALOG_MODAL,
                (gtk.STOCK_CANCEL, gtk.RESPONSE_CANCEL,
                gtk.STOCK_OK, gtk.RESPONSE_OK))
        self.set_default_response(gtk.RESPONSE_OK)
        self.set_resizable(False)
        self.connect('response', self._response)

        self._server = gtk.Entry()
        self.get_content_area().pack_start(self._server)
        self.show_all()

    # CHANGE TO ATTRIBUTE
    def _response(self, _wid, response):
        if response == gtk.RESPONSE_OK:
            self.server = self._server.get_text()


class VersionWindow(gtk.Dialog):
    def __init__(self, versions, RESPONSE_RESUME, RESPONSE_CHECKIN):
        def _version_selected(tree_selection):
            model, iter = tree_selection.get_selected()
            self.selected_version = model.get(iter, COL_NUMBER)[0]

        gtk.Dialog.__init__(self, 'Select a version', None, gtk.DIALOG_MODAL,
            (gtk.STOCK_CANCEL, gtk.RESPONSE_CANCEL,
            'Resume', RESPONSE_RESUME, 'Checkin', RESPONSE_CHECKIN))
        self.set_default_response(gtk.RESPONSE_OK)
        self.set_resizable(True)

        # Set up content
        (COL_NUMBER, COL_DATE, COL_COMMENT) = range(3)

        scrolled_window = gtk.ScrolledWindow()
        scrolled_window.set_border_width(5)
        scrolled_window.set_policy(gtk.POLICY_AUTOMATIC, gtk.POLICY_AUTOMATIC)
        scrolled_window.set_size_request(400, 250)

        model = gtk.ListStore(str, str, str)
        treeview = gtk.TreeView(model)
        scrolled_window.add_with_viewport(treeview)
        treeview.show()

        # Add vms we fetched
        for v in range(len(versions)):
           num = str(v + 1)
           model.append([versions[num]['#'], versions[num]['Date created'],
               versions[num]['Comment']])

        col = 0
        for label in ['#', 'Date created', 'Comment']:
            treeview.insert_column_with_attributes(-1, label,
                    gtk.CellRendererText(), text=col)
            col += 1

        # set mode to browse (1 and only 1 selected)
        tree_selection = treeview.get_selection()
        tree_selection.set_mode(gtk.SELECTION_BROWSE)

        # add signal handler for selection
        tree_selection.connect('changed', _version_selected)

        self.get_content_area().pack_start(scrolled_window);
        self.show_all()


class CreateWindow(gtk.Dialog):
    def __init__(self, base_vm_info):
        def _vm_selected(tree_selection):
            model, iter = tree_selection.get_selected()
            self.uuid = model.get(iter, COL_UUID)[0]

        gtk.Dialog.__init__(self, 'Create a VM', None, gtk.DIALOG_MODAL,
                (gtk.STOCK_CANCEL, gtk.RESPONSE_CANCEL,
                    gtk.STOCK_OK, gtk.RESPONSE_OK))
        self.set_default_response(gtk.RESPONSE_OK)
        self.set_resizable(True)

        # Set up content
        (COL_UUID, COL_NAME, COL_DISK_SIZE, COL_MEMORY_SIZE) = range(4)

        scrolled_window = gtk.ScrolledWindow()
        scrolled_window.set_border_width(5)
        scrolled_window.set_policy(gtk.POLICY_AUTOMATIC, gtk.POLICY_AUTOMATIC)
        scrolled_window.set_size_request(400, 250)

        model = gtk.ListStore(str, str, str, str)
        treeview = gtk.TreeView(model)
        scrolled_window.add_with_viewport(treeview)
        treeview.show()

        for id in base_vm_info:
            base_vm = base_vm_info[id]
            model.append([base_vm['uuid'], base_vm['Name'],
                base_vm['Disk size'], base_vm['Memory size']])

        col = 1
        for label in ['Name', 'Disk size', 'Memory size']:
            treeview.insert_column_with_attributes(-1, label,
                    gtk.CellRendererText(), text=col)
            col += 1

        # set mode to browse (1 and only 1 selected)
        tree_selection = treeview.get_selection()
        tree_selection.set_mode(gtk.SELECTION_BROWSE)

        # add signal handler for selection
        tree_selection.connect('changed', _vm_selected)

        # user entry
        table = gtk.Table()
        table.set_border_width(5)

        row = 0
        for text in 'Name', :
            label = gtk.Label(text + ':')
            label.set_alignment(1, 0.5)
            table.attach(label, 0, 1, row, row + 1, xpadding=5, ypadding=5)
            row += 1
        self._invalid = gtk.Label()
        self._invalid.set_markup('<span foreground="gray">Leaving empty will ' +
                'use the basevm name</span>')
        table.attach(self._invalid, 0, 2, row, row + 1, xpadding=5, ypadding=5)
        row += 1

        self._name = gtk.Entry()
        self._name.set_activates_default(True)
        row = 0
        for widget in self._name, :
            table.attach(widget, 1, 2, row, row + 1)
            row += 1

        self.get_content_area().pack_start(scrolled_window)
        self.get_content_area().pack_start(table)
        self.show_all()
        self._name.grab_focus()

    @property
    def name(self):
        return self._name.get_text()


class PrefWindow(gtk.Dialog):
    def __init__(self, pref):
        gtk.Dialog.__init__(self, 'Edit preferences', None, gtk.DIALOG_MODAL,
                (gtk.STOCK_CANCEL, gtk.RESPONSE_CANCEL,
                gtk.STOCK_OK, gtk.RESPONSE_OK))
        self.set_default_response(gtk.RESPONSE_OK)
        self.set_resizable(False)
        self.connect('response', self._response)

        label = gtk.Label('Upload throttle rate:')
        self._throttle_rate = gtk.Entry()
        self._throttle_rate.set_text(pref['throttle-rate'])
        hbox = gtk.HBox(False, 0)
        hbox.pack_start(label)
        hbox.pack_start(self._throttle_rate)
        self.get_content_area().pack_start(hbox)
        self._invalid = gtk.Label()
        self._invalid.set_markup('<span foreground="gray">Please enter valid ' +
                'value between 0 and 1.0</span>')
        self.get_content_area().pack_start(self._invalid)

        self.pref = pref
        self.show_all()
        self._invalid.hide()

    # CHANGE TO ATTRIBUTE
    def _response(self, _wid, response):
        if response == gtk.RESPONSE_OK:
            tr = self._throttle_rate.get_text()
            try:
                tr = float(tr)
            except ValueError:
                self._invalid.show()
            if 0.0 > tr or tr > 1:
                self._invalid.show()
            else:
                self.pref['throttle-rate'] = self._throttle_rate.get_text()

class CommentWindow(gtk.Dialog):
    def __init__(self):
        gtk.Dialog.__init__(self, 'Checkin comment', None, gtk.DIALOG_MODAL,
                (gtk.STOCK_OK, gtk.RESPONSE_OK))
        self.set_default_response(gtk.RESPONSE_OK)
        self.set_resizable(True)

        # Set up content
        table = gtk.Table()
        table.set_border_width(5)

        row = 0
        self._invalid = gtk.Label()
        self._invalid.set_markup('Write a comment about ' +
                'this checkin (200 chars max)')
        table.attach(self._invalid, 0, 2, row, row + 1, xpadding=5, ypadding=5)
        row += 1

        self._comment = gtk.TextView()
        self._comment.set_wrap_mode(gtk.WRAP_WORD)
        self._comment.set_size_request(200, 50)
        self._comment.grab_focus()
        frame = gtk.Frame('Comment')
        frame.add(self._comment)
        for widget in frame, :
            table.attach(widget, 0, 2, row, row + 1)
            row += 1

        self.get_content_area().pack_start(table)
        self.show_all()

    @property
    def comment(self):
        textbuffer = self._comment.get_buffer()
        startiter, enditer = textbuffer.get_bounds()
        text = textbuffer.get_text(startiter, enditer)
        return text[:200]


class IsrToolbarWidget(gtk.HBox):
    TOOLBAR_BTNS = ['Resume', 'Checkin', 'Checkout', 'Discard']
    (RESUME_IDX, CHECKIN_IDX, CHECKOUT_IDX, DISCARD_IDX) = range(len(TOOLBAR_BTNS))

    VM_STATE_CHECKED_OUT = 1
    VM_STATE_CHECKED_IN = 2
    VM_STATE_RESUMED = 3
    VM_STATE_SUSPENDED = 4

    __gsignals__ = {
        'toolbar-clicked': (gobject.SIGNAL_RUN_LAST, gobject.TYPE_NONE,
            (gobject.TYPE_STRING,)),
        'vm-state-changed': (gobject.SIGNAL_RUN_LAST, gobject.TYPE_NONE,
            (gobject.TYPE_STRING,)),
    }

    def __init__(self):
        gtk.HBox.__init__(self)

        toolbar = gtk.Toolbar()
        toolbar.set_orientation(gtk.ORIENTATION_HORIZONTAL)
        toolbar.set_style(gtk.TOOLBAR_BOTH)
        toolbar.set_border_width(2)

        icons = [gtk.STOCK_MEDIA_PLAY, gtk.STOCK_GO_UP,
                gtk.STOCK_GO_DOWN, gtk.STOCK_DELETE]
        self._btns = []
        for index in range(len(self.TOOLBAR_BTNS)):
            text = self.TOOLBAR_BTNS[index]
            btn = gtk.ToolButton(icons[index])
            btn.set_label(text)
            btn.connect('clicked', self._toolbar_clicked, text)
            toolbar.insert(btn, -1)
            self._btns.append(btn)
        btn = gtk.ToolButton(gtk.STOCK_REFRESH)
        btn.connect('clicked', self._toolbar_clicked, 'Refresh')

        # TODO: Remove nuke
        nuke_btn = gtk.ToolButton(gtk.STOCK_DIALOG_WARNING)
        nuke_btn.connect('clicked', self._toolbar_clicked, 'Nuke')

        self._toolbar = toolbar
        self.connect('vm-state-changed', self._vm_state_changed)

        self.pack_start(toolbar, True, True, 0)
        self.pack_end(btn, False, True, 0)
        self.pack_end(nuke_btn, False, True, 0)

    def _toolbar_clicked(self, _wid, btn):
        self.emit('toolbar-clicked', btn)

    def _vm_state_changed(self, _wid, vm_info):
        vm_info = json.loads(vm_info)
        if vm_info['Status'] == self.VM_STATE_RESUMED:
            for btn in self._btns:
                btn.set_sensitive(False)
            return
        elif vm_info['Status'] == self.VM_STATE_SUSPENDED:
            self._btns[self.RESUME_IDX].set_sensitive(True)
        if vm_info['Lock owner'] == 'None':
            self._btns[self.CHECKIN_IDX].set_sensitive(False)
            self._btns[self.CHECKOUT_IDX].set_sensitive(True)
        else:
            self._btns[self.CHECKIN_IDX].set_sensitive(True)
            self._btns[self.CHECKOUT_IDX].set_sensitive(False)
        if vm_info['Local changes'] == True:
            self._btns[self.DISCARD_IDX].set_sensitive(True)
        else:
            self._btns[self.DISCARD_IDX].set_sensitive(False)


class IsrWindow(gtk.Window):
    SERVER_NONE = 'No servers added yet.'

    VM_STATE_CHECKED_OUT = 1
    VM_STATE_CHECKED_IN = 2
    VM_STATE_RESUMED = 3
    VM_STATE_SUSPENDED = 4

    # VM State not tracked on the server
    LOCAL_FIELDS = {'Status': VM_STATE_SUSPENDED, 'Key': '',
            'Local changes': False, 'Cache version': 1}

    # VM Fields to display to the user
    VM_FIELDS = ['Name', 'Base vm', 'Version', 'Disk size', 'Memory size',
            'Lock owner', 'Local changes', 'Cache version', 'Date created']


    __gsignals__ = {
        'select-vm': (gobject.SIGNAL_RUN_LAST, gobject.TYPE_NONE,
            (gobject.TYPE_STRING,)),
        'toolbar-clicked': (gobject.SIGNAL_RUN_LAST, gobject.TYPE_NONE,
            (gobject.TYPE_STRING,)),
        'checkin-stopped': (gobject.SIGNAL_RUN_LAST, gobject.TYPE_NONE,
            (gobject.TYPE_BOOLEAN,)),
    }

    def _get_pixbuf(self, name):
        return self._theme.load_icon(name, 24, 0)

    def _get_icon(self, name):
        icon = gtk.Image()
        icon.set_from_pixbuf(self._get_pixbuf(name))
        return icon

    def _on_destroy(self, _wid):
        with open(self._server_file_path, 'r') as server_file:
            servers = server_file.readlines()
            servers[0] = str(self._selected_server_idx) + '\n'

        with open(self._server_file_path, 'w') as server_file:
            for s in servers:
                server_file.write(s)

        with open(self._info_file_path, 'w+') as info_file:
            info_file.write(json.dumps(self._vm_cache, indent=2,
                sort_keys=True))

        with open(self._pref_file_path, 'w') as pref_file:
            pref_file.write(json.dumps(self._pref, indent=2,
                sort_keys=True))

        gtk.main_quit()

    def __init__(self):
        gtk.Window.__init__(self)
        self.set_title('OpenISR 2.0')
        self.set_position(gtk.WIN_POS_CENTER)
        self.connect("destroy", self._on_destroy)
        self.set_border_width(10)
        self.set_size_request(800, 600)
        main_box = gtk.VBox(False, 0)
        self.add(main_box)

        # Checkin structures
        self._checkin_window = None
        self._cont = None
        self._is_checkin_stopped = True
        self.connect('checkin-stopped', self._checkin_stopped)

        # TODO: correct user auth and stuff
        self._headers = {'X-Secret-Key': 'secret'}

        # main menu
        menubar = self._get_main_menu()
        main_box.pack_start(menubar, False, True, 0)

        # icon theme
        self._theme = gtk.icon_theme_get_default()

        # toolbar
        self._toolbar_widget = IsrToolbarWidget()
        self._toolbar_widget.connect('toolbar-clicked', self._toolbar_clicked)
        main_box.pack_start(self._toolbar_widget, False, True, 0)

        # Read server info from file. The first line is the index of the last
        # server that was highlighted at close.
        self._servers = []
        self._selected_server_idx = 0
        self._server_file_path = os.path.join(get_modified_cache_dir(), 'servers')
        try:
            with open(self._server_file_path, 'r') as server_file:
                self._selected_server_idx = int(server_file.readline())
                for line in server_file:
                    self._servers.append(line.strip())
        except IOError, e:
            with open(self._server_file_path, 'w') as server_file:
                server_file.write('0\n')

        # profile info
        server_selector = gtk.combo_box_new_text()
        for server in self._servers:
            server_selector.append_text(server)
        if len(self._servers) == 0:
            server_selector.append_text(self.SERVER_NONE)
        server_selector.append_text('Add new server...')
        server_selector.set_active(self._selected_server_idx)
        server_selector.connect('changed', self._server_changed)
        self._server_selector = server_selector
        self._selected_server = server_selector.get_active_text()
        frame = gtk.Frame('Server selector')
        frame.add(server_selector)
        main_box.pack_start(frame, False, False, 0)

        # fetch vm info from server
        self._vm_cache = self._get_all_vm_info()
        self._selected_vm = None
        self._selected_vm_name = None

        # add fields used to track local changes
        for vm in self._vm_cache:
            for field, init in self.LOCAL_FIELDS.items():
                self._vm_cache[vm][field] = init

        # read local vm state from file
        self._info_file_path = os.path.join(get_modified_cache_dir(), 'vm_info')
        if os.path.isfile(self._info_file_path):
            with open(self._info_file_path, 'r') as info_file:
                info = json.loads(info_file.read())
            for vm in info:
                if vm in self._vm_cache:
                    for field in self.LOCAL_FIELDS.keys():
                        self._vm_cache[vm][field] = info[vm][field]

        # read machine info from file or create a new file
        self._machine_info_path = os.path.join(get_modified_cache_dir(),
                'machine_info')
        try:
            with open(self._machine_info_path) as machine_info_file:
                info = json.loads(machine_info_file.read())
                self._machine_name = info['name']
        except IOError, e:
            with open(self._machine_info_path, 'w') as machine_info_file:
                self._machine_name = str(uuid.uuid4())
                info = {'name': self._machine_name}
                machine_info_file.write(json.dumps(info, indent=2,
                    sort_keys=True))

        # preferences
        self._pref_file_path = os.path.join(get_modified_cache_dir(),
                'preferences')
        if os.path.isfile(self._pref_file_path):
            with open(self._pref_file_path, 'r') as pref_file:
                self._pref = json.loads(pref_file.read())
        else:
            # Default values
            self._pref = {'throttle-rate': '0.5'}

        # main content
        content_box = gtk.HBox(False, 0)
        main_box.pack_start(content_box, True, True, 0)

        # vm info window
        vm_info = self._get_vm_info_widget()
        info_frame = gtk.Frame("VM Info")
        info_frame.add(vm_info)

        # sidebar for vm navigation
        sidebar_box = gtk.VBox(False, 0)
        vm_list = self._get_vm_browser()
        vm_list.set_size_request(220, -1)
        frame = gtk.Frame("My VMs")
        frame.add(vm_list)
        sidebar_box.pack_start(frame, True, True, 0)

        # footer
        footer = gtk.Label('Authenticated on machine %s' % \
            self._machine_name)
        main_box.pack_end(footer, False, True, 0)

        # show
        content_box.pack_start(sidebar_box, False, True, 0)
        content_box.pack_start(info_frame, True, True, 0)
        self.set_focus(vm_list)
        self.show_all()

        # set icon
        try:
            icon = self._theme.load_icon('isr', 256, 0)
            self.set_icon(icon)
        except glib.GError:
            pass


    def _validate_cache(self, current_version, new_version):
        url = urljoin(self._selected_server, 'vm/%s/%s/%s' %
                (self._selected_vm, current_version, new_version))
        response = requests.get(url, headers=self._headers)
        chunk_list = json.loads(response.text)
        for image in chunk_list:
            cache = _Image.get_pristine_cache_path(self._selected_vm)
            chunk_dir = os.path.join(cache, image, '131072')

            for chunk in chunk_list[image]:
                dir = int(chunk) // 4096
                chunk_path = os.path.join(chunk_dir, str(dir), str(chunk))
                if os.path.isfile(chunk_path):
                    os.remove(chunk_path)

        # delete modified cache
        cache = _Image.get_modified_cache_path(self._selected_vm)
        distutils.dir_util.remove_tree(cache)

        self._vm_cache[self._selected_vm]['Cache version'] = new_version

    def _toolbar_clicked(self, _wid, button):
        selected_vm = self._vm_cache.get(self._selected_vm)

        def checkout():
            url = urljoin(self._selected_server, 'vm/checkout/%s' % (self._selected_vm))
            payload = {'machine name': self._machine_name}
            response = requests.post(url, data=json.dumps(payload),
                    headers=self._headers)
            if response.status_code == 200:
                selected_vm['Key'] = response.text
                return True
            elif response.status_code == 403:
                ew = IgnorableErrorWindow(self,
                        'The lock for this VM is currectly held by another ' +
                        'machine. Do you want to forcibly acquire the lock? ')
                response = ew.run()
                ew.destroy()
                if response == gtk.RESPONSE_CANCEL:
                    payload['force'] = True
                    response = requests.post(url, data=json.dumps(payload),
                        headers=self._headers)
                    if response.status_code == 200:
                        selected_vm['Key'] = response.text
                        return True
                    else:
                        return False
                else:
                    return False
            else:
                return False


        # send signal to the info window which modifies itself and sends a
        # signal back which modifies the state of the toolbar
        if button == 'Resume':
            # Acquire lock
            if not selected_vm['Key']:
                if not checkout():
                    return

            # Update status icon
            selected_vm['Status'] = self.VM_STATE_RESUMED
            self.emit('toolbar-clicked', 'Checkout')
            self._toolbar_widget.emit('vm-state-changed',
                    json.dumps(selected_vm))
            # Remove the chunks in the pristine caches which are no longer valid
            cache_version = selected_vm['Cache version']
            current_version = selected_vm['Version']
            if cache_version != current_version:
                self._validate_cache(cache_version, current_version)
            url = urlunsplit(('isr', urlsplit(self._selected_server).netloc,
                'vm/%s/%s' % (self._selected_vm, current_version), '', ''))
            app = vmnetx.ui.VMNetXUI(url,
                    name=self._vm_cache[self._selected_vm]['Name'],
                    throttle_rate=self._pref['throttle-rate'])
            app.run()

        elif button == 'Discard':
            uuid = self._selected_vm
            modified_cache = _Image.get_modified_cache_path(uuid)

            disk_cache = os.path.join(modified_cache, 'disk')
            if os.path.exists(disk_cache):
                distutils.dir_util.remove_tree(disk_cache)

            memory_cache = os.path.join(modified_cache, 'memory')
            if os.path.exists(memory_cache):
                distutils.dir_util.remove_tree(memory_cache)

            url = urljoin(self._selected_server, 'vm/discard/%s' %
                    self._selected_vm)
            payload = {'key': selected_vm['Key']}
            response = requests.post(url, headers=self._headers,
                    data=json.dumps(payload))

        elif button == 'Checkin':
            version = selected_vm['Version']
            url = urljoin(self._selected_server, 'vm/%s/%s' %
                    (self._selected_vm, version))

            # if there are no local changes, only release the lock
            if not selected_vm['Local changes']:
                payload = {'key': selected_vm['Key']}
                response = requests.post(url, headers=self._headers,
                        data=json.dumps(payload))
                self.emit('toolbar-clicked', button)
                selected_vm['Key'] = ''
                return

            uuid = self._selected_vm
            modified_cache = _Image.get_modified_cache_path(uuid)
            disk_cache = os.path.join(modified_cache, 'disk', '131072')
            memory_cache = os.path.join(modified_cache, 'memory', '131072')

            if os.path.exists(disk_cache) or os.path.exists(memory_cache):
                # Commit comment
                comment_url = urljoin(self._selected_server, 'vm/comment/%s' %
                        self._selected_vm)
                comment_wind = CommentWindow()
                if comment_wind.run() == gtk.RESPONSE_OK:
                    payload = {'comment': comment_wind.comment,
                           'key': selected_vm['Key']}
                    response = requests.post(comment_url, headers=self._headers,
                            data=json.dumps(payload))
                    if response.status_code != 200:
                        ew = IgnorableErrorWindow(self,
                                'Server error: ' + response.text)
                        if ew.run() == gtk.RESPONSE_OK:
                            pass
                        ew.destroy()
                else:
                    comment_wind.destroy()
                    return
                comment_wind.destroy()

                # send sizes here for now (??)
                url = urljoin(self._selected_server, 'vm/%s/%s' %
                        (self._selected_vm, version))
                disk_url = urljoin(url + '/', 'disk/size')
                memory_url = urljoin(url + '/', 'memory/size')
                with open(os.path.join(disk_cache, 'size'), 'r') as file:
                    requests.post(disk_url, data=file.read(), headers=self._headers)
                with open(os.path.join(memory_cache, 'size'), 'r') as file:
                    requests.post(memory_url, data=file.read(), headers=self._headers)
                # TODO: Check which chunks are missing on the server before
                # sending all of them

                self._is_checkin_stopped = False

                isr_url = urlunsplit(('isr', urlsplit(self._selected_server).netloc,
                    'vm/%s/%s' % (self._selected_vm, version), '', ''))
                self._cont = LocalController(url=isr_url, checkin=True,
                        throttle_rate='1.0') # self._pref['throttle-rate'])
                self._cont.connect('checkin-progress', self._checkin_progress)
                self._cont.setup_environment()
                self._cont.initialize()

        elif button == 'Checkout':
            checkout()

        elif button == 'Refresh':
            pass

        # TODO: Remove me
        elif button == 'Nuke':
            distutils.dir_util.remove_tree('~/.local/share/vmnetx/chunks')
            distutils.dir_util.remove_tree('~/.cache/vmnetx/chunks')

        self.emit('toolbar-clicked', button)

#### checkin stuff

    def _checkin_stopped(self, _obj, complete):
        self._is_checkin_stopped = True
        if self._checkin_window is not None:
            self._checkin_window.destroy()
            self._checkin_window = None

        if self._cont is not None:
            self._cont.shutdown()
            self._cont = None

        if complete:
            # Send commit message to server
            selected_vm = self._vm_cache.get(self._selected_vm)
            version = selected_vm['Version']
            url = urljoin(self._selected_server, 'vm/commit/%s' %
                        (self._selected_vm))
            payload = {'key': selected_vm['Key']}
            response = requests.post(url, headers=self._headers,
                    data=json.dumps(payload))

            # Move modified cache chunks to pristine cache
            uuid = self._selected_vm
            modified_cache = _Image.get_modified_cache_path(uuid)
            disk_cache = os.path.join(modified_cache, 'disk', '131072')
            memory_cache = os.path.join(modified_cache, 'memory', '131072')

            pristine_cache = _Image.get_pristine_cache_path(uuid)
            pristine_disk_cache = os.path.join(pristine_cache, 'disk',
                    '131072')
            pristine_memory_cache = os.path.join(pristine_cache, 'memory',
                    '131072')

            distutils.dir_util.copy_tree(disk_cache, pristine_disk_cache)
            distutils.dir_util.remove_tree(disk_cache)
            # distutils.dir_util.copy_tree(memory_cache,
            #         pristine_memory_cache)
            # distutils.dir_util.remove_tree(memory_cache)

            selected_vm['Key'] = ''
            selected_vm['Cache version'] = version + 1
            selected_vm['Local changes'] = False
            self.emit('toolbar-clicked', 'Refresh')

    def _checkin_progress(self, _obj, disk_count, disk_total, memory_count, memory_total):
        if self._is_checkin_stopped:
            return
        if self._checkin_window is None:
            self._checkin_window = CheckinProgressWindow(self)
            self._checkin_window.connect('user-cancel', lambda _obj:
                    self.emit('checkin-stopped', False))
            self._checkin_window.show_all()
        else:
            self._checkin_window.progress(disk_count, disk_total, memory_count,
                   memory_total)

        # IS THIS RIGHT? DOES THIS CONDITION ALWAYS HOLD?
        # For memory images, the whole thing is rewriten, so the chunks =
        # modified chunks. However, this does not hold for disk chunks. I need
        # to add something in vmentfs to count keep track of the number of
        # modified chunks.
        if disk_count == disk_total and memory_count == memory_total:
            self.emit('checkin-stopped', True)

###

    def _server_changed(self, server_selector):
        server = server_selector.get_active_text()
        if server == 'Add new server...':
            new_server_wind = NewServerWindow()
            if new_server_wind.run() == gtk.RESPONSE_OK:
                server = new_server_wind.server
                index = len(self._servers)
                self._server_selector.insert_text(index, server)
                self._servers.append(server)
                self._server_selector.set_active(index)
                with open(self._server_file_path, 'a') as server_file:
                    server_file.write(server + '\n')
            new_server_wind.destroy()

        self._selected_server = server
        self._selected_server_idx = server_selector.get_active()

    def _alert(self, _obj):
        message = gtk.MessageDialog(type=gtk.MESSAGE_ERROR,
                buttons=gtk.BUTTONS_OK)
        message.set_markup('Not yet connected')
        message.run()
        message.destroy()

######################## MENU FUNCTIONS #####################################

    def _get_main_menu(self):
        menu_items = (
                ( '/_File',         None,           None, 0, '<Branch>'),
                ( '/File/Quit',     '<control>Q',   gtk.main_quit, 0, None),
                ( '/_Edit',         None,           None, 0, '<Branch>'),
                ( '/Edit/_Preferences',
                    '<control>P',   self._pref_dialog, 0, None),
                ( '/_Advanced',      None,   None, 0, '<Branch>'),
                ( '/Advanced/_Version explorer',
                    '<control>V',   self._version_dialog, 0, None),
                ( '/Advanced/_Create VM from base',
                    '<control>N',   self._create_dialog, 0, None),
                )

        accel_group = gtk.AccelGroup()
        item_factory = gtk.ItemFactory(gtk.MenuBar, '<main>', accel_group)
        item_factory.create_items(menu_items)
        self.add_accel_group(accel_group)
        self.item_factory = item_factory
        return item_factory.get_widget('<main>')

    def _version_dialog(self, _action, _wid):
        # fetch vm information from server
        url = urljoin(self._selected_server, 'vm/version/%s' % self._selected_vm)
        response = requests.get(url, headers=self._headers)
        data = json.loads(response.text)

        RESPONSE_RESUME = 1234
        RESPONSE_CHECKIN = 4321

        version_wind = VersionWindow(data, RESPONSE_RESUME, RESPONSE_CHECKIN)
        r = version_wind.run()
        if r == RESPONSE_RESUME:
            # Remove the chunks in the pristine caches which are no longer valid
            current_version = self._vm_cache[self._selected_vm]['Cache version']
            self._validate_cache(current_version, version_wind.selected_version)

            # Launch VM
            url = urlunsplit(('isr', urlsplit(self._selected_server).netloc,
                'vm/%s/%s' % (self._selected_vm, version_wind.selected_version),
                '', ''))
            version_wind.destroy()
            app = vmnetx.ui.VMNetXUI(url,
                    name=self._vm_cache[self._selected_vm]['Name'])
            app.run()

        elif r == RESPONSE_CHECKIN:
            # TODO: checkin a new version
            pass

        version_wind.destroy()

    def _create_dialog(self, _action, _wid):
        url = urljoin(self._selected_server, 'vm/base_info')
        # TODO: Real secrets
        response = requests.get(url, headers=self._headers)
        base_vm_info = json.loads(response.text)
        create_wind = CreateWindow(base_vm_info)
        if create_wind.run() == gtk.RESPONSE_OK:
            url = urljoin(self._selected_server, 'vm/create')
            payload = {'uuid': create_wind.uuid, 'name': create_wind.name}
            response = requests.post(url, data=payload, headers=self._headers)
            if response.status_code != 200:
                print response.text
        create_wind.destroy()
        self.emit('toolbar-clicked', 'Refresh')

    def _pref_dialog(self, _action, _wid):
        pref_wind = PrefWindow(self._pref)
        if pref_wind.run() == gtk.RESPONSE_OK:
            self._pref = pref_wind.pref
        pref_wind.destroy()

#################################### GUI COMPONENENTS #########################
    ''' Manage the sidebar containing the vm status and names '''
    def _get_vm_browser(self):
        (COL_STATUS, COL_LOCK, COL_NAME, COL_UUID) = range(4)

        def _add_vm(vm, list):
            # Status icon
            if vm['Status'] == self.VM_STATE_RESUMED:
                status_icon = self.render_icon(gtk.STOCK_MEDIA_PLAY,
                        gtk.ICON_SIZE_SMALL_TOOLBAR)
            elif vm['Local changes'] == True:
                status_icon = self.render_icon(gtk.STOCK_FLOPPY,
                        gtk.ICON_SIZE_SMALL_TOOLBAR)
            else:
                status_icon = None

            # Lock icon
            if vm['Lock owner'] == 'None':
                lock_icon = None
            elif vm['Lock owner'] == self._machine_name:
                lock_icon = self._get_pixbuf('changes-prevent')
            else:
                lock_icon = self.render_icon(gtk.STOCK_CONVERT,
                        gtk.ICON_SIZE_SMALL_TOOLBAR)

            list.append([status_icon, lock_icon, vm['Name'], vm['uuid']])

        def _vm_selected(tree_selection):
            model, iter = tree_selection.get_selected()
            if iter is not None:
                self._selected_vm = model.get(iter, COL_UUID)[0]
                self._selected_vm_name = model.get(iter, COL_NAME)[0]
                self.emit('select-vm', self._selected_vm)

        def _toolbar_clicked(self, button, treeview, model):
            path, _col = treeview.get_cursor()
            model.clear()
            for vm_id in self._vm_cache:
                _add_vm(self._vm_cache[vm_id], model)
            if path:
                treeview.set_cursor(path)

        scrolled_window = gtk.ScrolledWindow()
        scrolled_window.set_border_width(5)
        scrolled_window.set_policy(gtk.POLICY_AUTOMATIC, gtk.POLICY_AUTOMATIC)

        model = gtk.ListStore(gtk.gdk.Pixbuf, gtk.gdk.Pixbuf, str, str)
        treeview = gtk.TreeView(model)
        scrolled_window.add_with_viewport(treeview)
        treeview.show()

        # Add vms we fetched
        for vm_id in self._vm_cache:
            _add_vm(self._vm_cache[vm_id], model)

        treeview.insert_column_with_attributes(COL_STATUS, 'Status',
                gtk.CellRendererPixbuf(), pixbuf=COL_STATUS)
        lock_renderer = gtk.CellRendererPixbuf()
        lock_renderer.set_fixed_size(-1, 25)
        treeview.insert_column_with_attributes(COL_LOCK, 'Lock',
                lock_renderer, pixbuf=COL_LOCK)
        treeview.insert_column_with_attributes(COL_NAME, 'Name',
                gtk.CellRendererText(), text=COL_NAME)

        '''
        renderer = gtk.CellRendererText()
        column = gtk.TreeViewColumn('Name', renderer, text=COL_NAME)
        treeview.append_column(column)
        '''

        # set mode to browse (1 and only 1 selected)
        tree_selection = treeview.get_selection()
        tree_selection.set_mode(gtk.SELECTION_BROWSE)

        # add signal handler for selection
        tree_selection.connect('changed', _vm_selected)

        # highlight the first item
        treeview.set_cursor(0)

        self.connect('toolbar-clicked', _toolbar_clicked, treeview, model)

        return scrolled_window

    def _get_all_vm_info(self):
        if self._selected_server == self.SERVER_NONE:
            return {}

        url = urljoin(self._selected_server, 'vm/info')
        try:
            response = requests.get(url, headers=self._headers)
            if response.status_code != 200:
                ew = IgnorableErrorWindow(self,
                    'Could not fetch vm list from server')
                if ew.run() == gtk.RESPONSE_OK:
                    pass
                ew.destroy()
                vm_info = {}
            else:
                vm_info = json.loads(response.text,
                        object_pairs_hook=OrderedDict)
        except:
            return {}


        return vm_info

    def _get_vm_info_widget(self):
        def _select_vm(self, vm_id):
            self._vm_table.show_all()
            selected_vm = self._vm_cache[vm_id]
            for field in self.VM_FIELDS:
                if field == 'Disk size' or field == 'Memory size':
                    self._vm_display[field].set_text(str(selected_vm[field] // \
                        (1024 * 1024)) + ' MB')
                else:
                    self._vm_display[field].set_text(str(selected_vm[field]))
            self._toolbar_widget.emit('vm-state-changed',
                    json.dumps(selected_vm))


        ''' Whenever a button is pressed, the state of the vm is rechecked '''
        def _toolbar_clicked(self, button):
            selected_vm = self._vm_cache.get(self._selected_vm)
            if button in ['Refresh', 'Checkin', 'Checkout']:
                # Update fields from the server
                url = urljoin(self._selected_server, 'vm/info')
                response = requests.get(url, headers=self._headers)
                vm_info = json.loads(response.text,
                        object_pairs_hook=OrderedDict)

                for existing_vm in self._vm_cache:
                    if existing_vm not in vm_info:
                        del self._vm_cache[existing_vm]

                for vm in vm_info:
                    if vm not in self._vm_cache:
                        self._vm_cache[vm] = {}
                        for field, init in self.LOCAL_FIELDS.items():
                            self._vm_cache[vm][field] = init
                    for field in vm_info[vm]:
                        self._vm_cache[vm][field] = vm_info[vm][field]

            if button == 'Resume':
                selected_vm['Status'] = self.VM_STATE_SUSPENDED
                selected_vm['Local changes'] = True
            elif button == 'Checkin':
                pass #selected_vm['Local changes'] = False
            elif button == 'Checkout':
                pass
            elif button == 'Discard':
                selected_vm['Local changes'] = False

            for field in self.VM_FIELDS:
                self._vm_display[field].set_text(str(selected_vm[field]))
            self._toolbar_widget.emit('vm-state-changed',
                    json.dumps(selected_vm))

        scrolled_window = gtk.ScrolledWindow()
        scrolled_window.set_border_width(5)
        scrolled_window.set_policy(gtk.POLICY_AUTOMATIC, gtk.POLICY_AUTOMATIC)
        vbox = gtk.VBox(False, 0)
        hbox = gtk.HBox(False, 0)

        # setup vm info display
        vm_table = gtk.Table(len(self.VM_FIELDS), 2)
        vm_table.set_col_spacings(10)

        row = 0
        self._vm_display = {}
        for field in self.VM_FIELDS:
            label1 = gtk.Label(field + ': ')
            label1.set_alignment(0, 0)
            label1.set_width_chars(20)
            vm_table.attach(label1, 0, 1, row, row+1)
            label2 = gtk.Label()
            label2.set_alignment(0, 0)
            label2.set_line_wrap(True)
            vm_table.attach(label2, 1, 2, row, row+1)
            self._vm_display[field] = label2
            row += 1
        self._vm_table = vm_table

        self.connect('select-vm', _select_vm)
        self.connect('toolbar-clicked', _toolbar_clicked)
        vbox.pack_start(vm_table, False, False, 10)
        hbox.pack_start(vbox, False, False, 10)
        scrolled_window.add_with_viewport(hbox)
        scrolled_window.show_all()

        return scrolled_window

# pylint: enable=incomplete-protocol
