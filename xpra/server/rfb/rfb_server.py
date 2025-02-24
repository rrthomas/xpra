# -*- coding: utf-8 -*-
# This file is part of Xpra.
# Copyright (C) 2017-2021 Antoine Martin <antoine@xpra.org>
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.
#pylint: disable-msg=E1101

from xpra.util import csv
from xpra.os_util import POSIX, OSX, bytestostr
from xpra.net.bytestreams import set_socket_timeout
from xpra.server.rfb.rfb_const import RFBEncoding, RFB_KEYNAMES
from xpra.server.rfb.rfb_protocol import RFBProtocol
from xpra.server.rfb.rfb_source import RFBSource
from xpra.server import server_features
from xpra.scripts.config import parse_bool, parse_number
from xpra.log import Logger

log = Logger("rfb")
mouselog = Logger("rfb", "mouse")
keylog = Logger("rfb", "keyboard")


"""
    Adds RFB packet handler to a server.
"""
class RFBServer:

    def __init__(self):
        self._window_to_id = {}
        self._rfb_upgrade = 0
        self.readonly = False
        self.rfb_buttons = 0
        self.x11_keycodes_for_keysym = {}
        if POSIX and not OSX:
            from xpra.x11.bindings.keyboard_bindings import X11KeyboardBindings #@UnresolvedImport
            self.X11Keyboard = X11KeyboardBindings()

    def init(self, opts):
        if not parse_bool("rfb-upgrade", opts.rfb_upgrade):
            self._rfb_upgrade = 0
        else:
            self._rfb_upgrade = parse_number(int, "rfb-upgrade", opts.rfb_upgrade, 0)
        log("init(..) rfb-upgrade=%i", self._rfb_upgrade)


    def _get_rfb_desktop_model(self):
        models = tuple(self._window_to_id.keys())
        if not models:
            log.error("RFB: no window models to export, dropping connection")
            return None
        if len(models)!=1:
            log.error("RFB can only handle a single desktop window, found %i", len(self._window_to_id))
            return None
        return models[0]

    def _get_rfb_desktop_wid(self):
        ids = tuple(self._window_to_id.values())
        if len(ids)!=1:
            log.error("RFB can only handle a single desktop window, found %i", len(self._window_to_id))
            return None
        return ids[0]


    def handle_rfb_connection(self, conn):
        model = self._get_rfb_desktop_model()
        if not model:
            conn.close()
            return
        def rfb_protocol_class(conn):
            auths = self.make_authenticators("rfb", "rfb", conn)
            assert len(auths)<=1, "rfb does not support multiple authentication modules"
            auth = None
            if len(auths)==1:
                auth = auths[0]
            return RFBProtocol(self, conn, auth,
                               self.process_rfb_packet, self.get_rfb_pixelformat, self.session_name or "Xpra Server")
        p = self.do_make_protocol("rfb", conn, {}, rfb_protocol_class)
        log("handle_rfb_connection(%s) protocol=%s", conn, p)
        p.send_protocol_handshake()

    def process_rfb_packet(self, proto, packet):
        #log("RFB packet: '%s'", packet)
        fn_name = "_process_rfb_%s" % bytestostr(packet[0]).replace("-", "_")
        fn = getattr(self, fn_name, None)
        if not fn:
            log.warn("Warning: no RFB handler for %s", fn_name)
            return
        self.idle_add(fn, proto, packet)


    def get_rfb_pixelformat(self):
        model = self._get_rfb_desktop_model()
        w, h = model.get_dimensions()
        #w, h, bpp, depth, bigendian, truecolor, rmax, gmax, bmax, rshift, bshift, gshift
        return w, h, 32, 32, False, True, 255, 255, 255, 16, 8, 0

    def _process_rfb_invalid(self, proto, packet):
        self.disconnect_protocol(proto, "invalid packet: %s" % (packet[1:]))

    def _process_rfb_connection_lost(self, proto, packet):
        self._process_connection_lost(proto, packet)

    def _process_rfb_authenticated(self, proto, _packet):
        model = self._get_rfb_desktop_model()
        if not model:
            proto.close()
            return
        self.accept_protocol(proto)
        #use blocking sockets from now on:
        set_socket_timeout(proto._conn, None)
        accepted, share_count, disconnected = self.handle_sharing(proto, share=proto.share)
        log("RFB handle sharing: accepted=%s, share count=%s, disconnected=%s", accepted, share_count, disconnected)
        if not accepted:
            return
        source = RFBSource(proto, proto.share)
        if server_features.input_devices:
            source.keyboard_config = self.get_keyboard_config()
            self.set_keymap(source)
        self._server_sources[proto] = source
        w, h = model.get_dimensions()
        source.damage(self._window_to_id[model], model, 0, 0, w, h)
        #ugly weak dependency,
        #shadow servers need to be told to start the refresh timer:
        start_refresh = getattr(self, "start_refresh", None)
        if start_refresh:
            for wid in tuple(self._window_to_id.values()):
                start_refresh(wid)      #pylint: disable=not-callable

    def _process_rfb_PointerEvent(self, _proto, packet):
        if not server_features.input_devices or self.readonly:
            return
        buttons, x, y = packet[1:4]
        wid = self._get_rfb_desktop_wid()
        mouselog("RFB PointerEvent(%#x, %s, %s) desktop wid=%s", buttons, x, y, wid)
        self._move_pointer(wid, (x, y))
        if buttons!=self.rfb_buttons:
            #figure out which buttons have changed:
            for button in range(8):
                mask = 2**button
                if buttons & mask != self.rfb_buttons & mask:
                    pressed = bool(buttons & mask)
                    mouselog(" %spressing %i", ["un",""][pressed], 1+button)
                    self.button_action((x, y), 1+button, pressed, -1)
            self.rfb_buttons = buttons

    def _process_rfb_KeyEvent(self, proto, packet):
        if not server_features.input_devices or self.readonly:
            return
        source = self.get_server_source(proto)
        if not source:
            return
        pressed, p1, p2, key = packet[1:5]
        wid = self._get_rfb_desktop_wid()
        keyname = RFB_KEYNAMES.get(key)
        keylog("RFB KeyEvent(%s, %s, %s, %s) keyname=%s, desktop wid=%s", pressed, p1, p2, key, keyname, wid)
        if not keyname:
            if 0<key<255:
                keyname = chr(key)
            elif self.X11Keyboard:
                keyname = self.X11Keyboard.keysym_str(key)
        if not keyname:
            keylog.warn("rfb unknown KeyEvent: %s, %i, %i, %#x", pressed, p1, p2, key)
            return
        modifiers = []
        keyval = 0
        keycode, group = source.keyboard_config.get_keycode(0, keyname, pressed, modifiers, 0, keyname, 0)
        keylog("RFB keycode(%s)=%s, %s", keyname, keycode, group)
        if keycode:
            is_mod = source.keyboard_config.is_modifier(keycode)
            self._handle_key(wid, bool(pressed), keyname, keyval, keycode, modifiers, is_mod, True)

    def _process_rfb_SetEncodings(self, _proto, packet):
        n, encodings = packet[2:4]
        known_encodings = [RFBEncoding.ENCODING_STR.get(x) for x in encodings if x in RFBEncoding.ENCODING_STR]
        log("RFB %i encodings: %s", n, csv(known_encodings))
        unknown_encodings = [x for x in encodings if x not in RFBEncoding.ENCODING_STR]
        if unknown_encodings:
            log("RFB %i unknown encodings: %s", len(unknown_encodings), csv(unknown_encodings))

    def _process_rfb_SetPixelFormat(self, _proto, packet):
        log("RFB: SetPixelFormat %s", packet)
        #w, h, bpp, depth, bigendian, truecolor, rmax, gmax, bmax, rshift, bshift, gshift = packet

    def _process_rfb_FramebufferUpdateRequest(self, _proto, packet):
        #pressed, _, _, keycode = packet[1:5]
        inc, x, y, w, h = packet[1:6]
        log("RFB: FramebufferUpdateRequest inc=%s, geometry=%s", inc, (x, y, w, h))
        if not inc:
            model = self._get_rfb_desktop_model()
            self.refresh_window_area(model, x, y, w, h)

    def _process_rfb_ClientCutText(self, _proto, packet):
        #l = packet[4]
        text = packet[5]
        log("RFB got clipboard text: %r", text)
