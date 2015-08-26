#
# vmnetx.controller.local.qmp_af_unix - QEMU QMP protocol support
#
# Copyright (C) 2015 Carnegie Mellon University
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

import socket
import json
import time

QMP_UNIX_SOCK = "/tmp/qmp_cloudlet"

class QmpAfUnix:
    def __init__(self, s_name):
        self.s_name = s_name

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(self.s_name)

    def disconnect(self):
        self.sock.close()

    # Given a string, which pay contain more than 1 json object, parse the
    # objects and return them as a list
    def _parse_responses(self, data):
        num_open = 0
        start = -1
        objs = []
        for i in range(len(data)):
            if data[i] == '{':
                if num_open == 0:
                    start = i
                num_open += 1
            elif data[i] == '}':
                num_open -= 1
            if num_open == 0 and start != -1:
                objs.append(json.loads(data[start:i+1]))
                start = -1
        return objs

    # first we need to negotiate qmp capabilities before
    # issuing commands.
    # returns True on success, False otherwise
    def qmp_negotiate(self):
        # qemu provides capabilities information first
        capabilities = json.loads(self.sock.recv(1024))

        json_cmd = json.dumps({"execute":"qmp_capabilities"})
        self.sock.sendall(json_cmd)
        response = json.loads(self.sock.recv(1024))
        if "return" in response:
            return True
        else:
            return False

    # returns timestamp of VM suspend on success, None otherwise
    def stop_raw_live(self):
        json_cmd = json.dumps({"execute":"stop-raw-live"})
        self.sock.sendall(json_cmd)
        data = self.sock.recv(1024)
        print "STOP RAW LIVE:", data
        responses = self._parse_responses(data)
        ret = False
        for response in responses:
            if "return" in response:
                ret = True
        if not ret:
            return None

        # wait for QEVENT_STOP in next 10 responses
        for i in range(10):
            response = json.loads(self.sock.recv(1024))
            if "event" in response and response["event"] == "STOP":
                timestamp = response["timestamp"]
                ts = float(timestamp["seconds"]) + float(timestamp["microseconds"]) / 1000000
                return ts

        return None

    # returns True on success, False otherwise
    def iterate_raw_live(self):
        json_cmd = json.dumps({"execute":"iterate-raw-live"})
        self.sock.sendall(json_cmd)
        data = self.sock.recv(1024)
        print "ITERATE RAW LIVE", data
        responses = self._parse_responses(data)
        for response in responses:
            if "return" in response:
                return True
        return False

    # returns True on success, False otherwise
    def randomize_raw_live(self):
        json_cmd = json.dumps({"execute":"randomize-raw-live"})
        self.sock.sendall(json_cmd)
        response = json.loads(self.sock.recv(1024))
        if "return" in response:
            return True
        else:
            return False

    # returns True on success, False otherwise
    def unrandomize_raw_live(self):
        json_cmd = json.dumps({"execute":"unrandomize-raw-live"})
        self.sock.sendall(json_cmd)
        response = json.loads(self.sock.recv(1024))
        if "return" in response:
            return True
        else:
            return False

    def stop_raw_live_once(self):
        self.connect()
        ret = self.qmp_negotiate()
        if ret:
            ret = self.stop_raw_live()
        self.disconnect()

        return ret

    def iterate_raw_live_once(self):
        self.connect()
        ret = self.qmp_negotiate()
        #ret = self.randomize_raw_live()  # randomize page output order
        ret = self.unrandomize_raw_live()  # make page output order sequential
        if not ret:
            print "Failed"
        time.sleep(40)
        if ret:
            #print "iterating"
            ret = self.iterate_raw_live()
        if ret:
            time.sleep(10)
            #print "iterating"
            ret = self.iterate_raw_live()
        if ret:
            time.sleep(10)
            #print "stopping"
            ret = self.stop_raw_live()
        self.disconnect()
        return ret

# for debugging
if __name__ == "__main__":

    qmp = QmpAfUnix("potato")
    qmp._parse_response('''{"return": {}}
            {"timestamp": {"seconds": 1433959275, "microseconds": 275513}, "event":
                "VNC_DISCONNECTED", "data": {"server": {"auth": "vnc", "family":
                    "ipv4", "service": "5900", "host": "127.0.0.1"}, "client":
                    {"family": "ipv4", "service": "53826", "host":
                    "127.0.0.1"}}}''')

