# This file is part of Xpra.
# Copyright (C) 2008, 2009 Nathaniel Smith <njs@pobox.com>
# Copyright (C) 2018-2019 Antoine Martin <antoine@xpra.org>
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

# According to ICCCM 2.8/4.3, a window manager for screen N is a client which
# acquires the selection WM_S<N>.  If another client already has this
# selection, we can either abort or steal it.  Once we have it, if someone
# else steals it, then we should exit.

from struct import pack, unpack, calcsize
from gi.repository import GObject, Gtk, Gdk

from xpra.gtk_common.gobject_util import no_arg_signal, one_arg_signal
from xpra.gtk_common.error import xsync, XError
from xpra.x11.bindings.window_bindings import constants, X11WindowBindings #@UnresolvedImport
from xpra.x11.gtk_x11.gdk_bindings import (
    get_xatom,                  #@UnresolvedImport
    get_pywindow,               #@UnresolvedImport
    add_event_receiver,         #@UnresolvedImport
    remove_event_receiver,      #@UnresolvedImport
    )
from xpra.log import Logger

log = Logger("x11", "util")

StructureNotifyMask = constants["StructureNotifyMask"]
XNone = constants["XNone"]


class AlreadyOwned(Exception):
    pass

class ManagerSelection(GObject.GObject):
    __gsignals__ = {
        "selection-lost": no_arg_signal,

        "xpra-destroy-event": one_arg_signal,
        }

    def __str__(self):
        return "ManagerSelection(%s)" % self.atom

    def __init__(self, selection):
        GObject.GObject.__init__(self)
        self.atom = selection
        atom = Gdk.Atom.intern(selection, False)
        self.clipboard = Gtk.Clipboard.get(atom)
        self._xwindow = None

    def _owner(self):
        return X11WindowBindings().XGetSelectionOwner(self.atom)

    def owned(self):
        "Returns True if someone owns the given selection."
        return self._owner() != XNone

    # If the selection is already owned, then raise AlreadyOwned rather
    # than stealing it.
    IF_UNOWNED = "if_unowned"
    # If the selection is already owned, then steal it, and then block until
    # the previous owner has signaled that they are done cleaning up.
    FORCE = "force"
    # If the selection is already owned, then steal it and return immediately.
    # Created for the use of tests.
    FORCE_AND_RETURN = "force_and_return"
    def acquire(self, when):
        old_owner = self._owner()
        if when is self.IF_UNOWNED and old_owner != XNone:
            raise AlreadyOwned

        #only strings with GTK3?
        thestring = "VERSION"
        self.clipboard.set_text(thestring, len(thestring))

        # Having acquired the selection, we have to announce our existence
        # (ICCCM 2.8, still).  The details here probably don't matter too
        # much; I've never heard of an app that cares about these messages,
        # and metacity actually gets the format wrong in several ways (no
        # MANAGER or owner_window atoms).  But might as well get it as right
        # as possible.

        # To announce our existence, we need:
        #   -- the timestamp we arrived at
        #   -- the manager selection atom
        #   -- the window that registered the selection
        # Of course, because Gtk is doing so much magic for us, we have to do
        # some weird tricks to get at these.

        # Ask ourselves when we acquired the selection:
        def wait_for_contents(clipboard, target):
            atom = Gdk.Atom.intern(target, False)
            return clipboard.wait_for_contents(atom)
        timestamp_atom = Gdk.Atom.intern("TIMESTAMP", False)
        contents = self.clipboard.wait_for_contents(timestamp_atom)
        ts_data = contents.get_data()

        #data is a timestamp, X11 datatype is Time which is CARD32,
        #(which is 64 bits on 64-bit systems!)
        Lsize = calcsize("@L")
        if len(ts_data)==Lsize:
            ts_num = unpack("@L", ts_data[:Lsize])[0]
        else:
            ts_num = 0      #CurrentTime
            log.warn("invalid data for 'TIMESTAMP': %s", ([hex(ord(x)) for x in ts_data]))
        # Calculate the X atom for this selection:
        selection_xatom = get_xatom(self.atom)
        # Ask X what window we used:
        self._xwindow = X11WindowBindings().XGetSelectionOwner(self.atom)

        root = self.clipboard.get_display().get_default_screen().get_root_window()
        xid = root.get_xid()
        X11WindowBindings().sendClientMessage(xid, xid, False, StructureNotifyMask,
                          "MANAGER",
                          ts_num, selection_xatom, self._xwindow)

        if old_owner != XNone and when is self.FORCE:
            # Block in a recursive mainloop until the previous owner has
            # cleared out.
            try:
                with xsync:
                    window = get_pywindow(self.clipboard, old_owner)
                    window.set_events(window.get_events() | Gdk.EventMask.STRUCTURE_MASK)
                log("got window")
            except XError:
                log("Previous owner is already gone, not blocking")
            else:
                log("Waiting for previous owner to exit...")
                add_event_receiver(window, self)
                Gtk.main()
                log("...they did.")
        window = get_pywindow(self.clipboard, self._xwindow)
        window.set_title("Xpra-ManagerSelection")

    def do_xpra_destroy_event(self, event):
        remove_event_receiver(event.window, self)
        Gtk.main_quit()

    def _get(self, _clipboard, outdata, _which, _userdata):
        # We are compliant with ICCCM version 2.0 (see section 4.3)
        outdata.set("INTEGER", 32, pack("@ii", 2, 0))

    def _clear(self, _clipboard, _userdata):
        self._xwindow = None
        self.emit("selection-lost")

    def window(self):
        if self._xwindow is None:
            return None
        return get_pywindow(self.clipboard, self._xwindow)

GObject.type_register(ManagerSelection)