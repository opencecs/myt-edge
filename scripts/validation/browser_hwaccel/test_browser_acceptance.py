#!/usr/bin/env python3
"""Protocol/helper tests only. Does not launch a browser or contact the board."""
import base64
import hashlib
import http.server
import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import struct
import tempfile
import threading
import time
import types
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

spec=importlib.util.spec_from_file_location("harness",Path(__file__).with_name("browser_acceptance.py"))
h=importlib.util.module_from_spec(spec);spec.loader.exec_module(h)


def frame(payload, opcode=1, final=True):
    head=bytes([(128 if final else 0)|opcode])
    if len(payload)<126:
        head+=bytes([len(payload)])
    elif len(payload)<65536:
        head+=bytes([126])+struct.pack("!H",len(payload))
    else:
        head+=bytes([127])+struct.pack("!Q",len(payload))
    return head+payload


class WebSocketTests(unittest.TestCase):
    def setUp(self):
        self.client,self.server=socket.socketpair()
        self.addCleanup(self.client.close);self.addCleanup(self.server.close)
        self.ws=h.WebSocket.__new__(h.WebSocket)
        self.ws.sock=self.client;self.ws.buffer=bytearray()
        self.ws.fragments=bytearray();self.ws.fragment_opcode=None

    def test_fragmented_json_and_ping(self):
        self.server.sendall(frame(b'{"value":',final=False)+frame(b"hi",opcode=9)+frame(b"42}",opcode=0))
        self.assertEqual(self.ws.receive(1),{"value":42})
        reply=self.server.recv(128)
        self.assertEqual(reply[0],0x8a);self.assertTrue(reply[1]&128)
        mask=reply[2:6]
        self.assertEqual(bytes(v^mask[i%4] for i,v in enumerate(reply[6:])),b"hi")

    def test_partial_timeout_retains_frame(self):
        data=frame(b'{"partial":"works"}')
        self.server.sendall(data[:7])
        with self.assertRaises(TimeoutError):
            self.ws.receive(0.01)
        self.server.sendall(data[7:])
        self.assertEqual(self.ws.receive(1),{"partial":"works"})

    def test_extended_frame_lengths(self):
        for length in (400,70000):
            value={"data":"x"*length}
            payload=json.dumps(value).encode()
            writer=threading.Thread(target=self.server.sendall,args=(frame(payload),))
            writer.start()
            self.assertEqual(self.ws.receive(2),value)
            writer.join()

    def test_client_messages_are_masked(self):
        self.ws.send({"id":1})
        reply=self.server.recv(1024)
        self.assertEqual(reply[0],0x81);self.assertTrue(reply[1]&128)
        mask=reply[2:6]
        payload=bytes(value^mask[index%4] for index,value in enumerate(reply[6:]))
        self.assertEqual(json.loads(payload),{"id":1})

    def test_remote_hosts_refused(self):
        with self.assertRaisesRegex(ValueError,"loopback"):
            h.WebSocket("ws://example.com/debugger")

    def test_real_loopback_handshake(self):
        listener=socket.socket()
        listener.bind(("127.0.0.1",0));listener.listen()
        self.addCleanup(listener.close)
        errors=[]
        def serve():
            try:
                conn,_=listener.accept()
                with conn:
                    request=b""
                    while b"\r\n\r\n" not in request:
                        request+=conn.recv(4096)
                    key=[line.split(b":",1)[1].strip() for line in request.split(b"\r\n") if line.startswith(b"Sec-WebSocket-Key:")][0]
                    accept=base64.b64encode(hashlib.sha1(key+b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11").digest())
                    conn.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: "+accept+b"\r\n\r\n"+frame(b'{"ok":true}'))
            except Exception as error:
                errors.append(error)
        thread=threading.Thread(target=serve);thread.start()
        ws=h.WebSocket("ws://127.0.0.1:%d/devtools/browser/test"%listener.getsockname()[1])
        try:self.assertEqual(ws.receive(1),{"ok":True})
        finally:ws.close()
        thread.join()
        self.assertEqual(errors,[])


class CDPTests(unittest.TestCase):
    def test_events_and_responses_interleave(self):
        cdp=h.CDP.__new__(h.CDP)
        cdp.sequence=0;cdp.responses={};cdp.events=[];cdp.log=io.StringIO();cdp.commands=io.StringIO();cdp.expected_boot="one"
        messages=iter([{"method":"Media.playerPropertiesChanged","params":{"playerId":"p","properties":[]}},
                       {"id":1,"result":{"ok":True}}])
        cdp.ws=types.SimpleNamespace(send=lambda message:None,receive=lambda timeout:next(messages))
        with patch.object(h,"boot_id",return_value="one"):
            self.assertEqual(cdp.call("Media.enable"),{"ok":True})
        self.assertEqual(len(cdp.events),1)
        self.assertIn("Media.playerPropertiesChanged",cdp.log.getvalue())

    def test_boot_change_prevents_send(self):
        cdp=h.CDP.__new__(h.CDP);cdp.expected_boot="old"
        with patch.object(h,"boot_id",return_value="new"):
            with self.assertRaisesRegex(RuntimeError,"boot ID"):
                cdp.check_boot()


class EvidenceTests(unittest.TestCase):
    def player(self,name):
        return {"p":{"properties":{"kVideoDecoderName":name},"errors":[],"messages":[],"events":[]}}

    def test_hardware_requires_named_decoder_and_frames(self):
        self.assertEqual(h.decoder_evidence(self.player("V4L2StatefulVideoDecoder"),10,"hardware")["status"],"confirmed")
        self.assertEqual(h.decoder_evidence(self.player("V4L2StatefulVideoDecoder"),0,"hardware")["status"],"not_proven")
        self.assertEqual(h.decoder_evidence(self.player("MojoVideoDecoder"),10,"hardware")["status"],"not_proven")
        p=self.player("V4L2StatefulVideoDecoder");p["p"]["properties"]["kIsPlatformVideoDecoder"]="false"
        self.assertEqual(h.decoder_evidence(p,10,"hardware")["status"],"not_proven")

    def test_software_decoder_not_accepted_as_hardware(self):
        p=self.player("FFmpegVideoDecoder")
        self.assertEqual(h.decoder_evidence(p,10,"hardware")["status"],"not_proven")
        self.assertEqual(h.decoder_evidence(p,10,"software")["status"],"confirmed")

    def test_properties_and_errors_collected(self):
        events=[{"method":"Media.playerPropertiesChanged","params":{"playerId":"p","properties":[{"name":"kVideoDecoderName","value":"V4L2VideoDecoder"}]}},
                {"method":"Media.playerErrorsRaised","params":{"playerId":"p","errors":[{"errorType":"test"}]}}]
        players=h.collect_players(events)
        self.assertEqual(players["p"]["properties"]["kVideoDecoderName"],"V4L2VideoDecoder")
        self.assertEqual(len(players["p"]["errors"]),1)

    def test_h264_codec_derived_from_avcc(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/"fragment.mp4"
            path.write_bytes(b"\x00\x00\x00\x14avcC\x01\x64\x00\x2a"+b"\0"*8)
            self.assertEqual(h.h264_codec(path),"avc1.64002A")

    def test_cpu_delta_ignores_pid_reuse(self):
        first={"monotonic":1,"system_total_ticks":1000,"system_idle_ticks":100,
               "processes":{"4":{"name":"chromium","ticks":10,"start_ticks":1,"group":"browser","rss_bytes":10}}}
        second={"monotonic":2,"system_total_ticks":1100,"system_idle_ticks":150,
                "processes":{"4":{"name":"chromium","ticks":1000,"start_ticks":2,"group":"browser","rss_bytes":10}}}
        self.assertEqual(h.cpu_delta(first,second)["cpu_percent_one_core"]["browser"],0)

    def test_proc_parser_handles_spaces_and_parentheses(self):
        parts=["S"]+["0"]*21
        parts[2]="10";parts[3]="10";parts[11]="20";parts[12]="5";parts[19]="99";parts[21]="2"
        result=h.parse_proc_stat("123 (a ) tricky name) "+" ".join(parts))
        self.assertEqual(result["name"],"a ) tricky name")
        self.assertEqual(result["ticks"],25);self.assertEqual(result["start_ticks"],99)

    def test_owned_options_cannot_be_overridden(self):
        args=types.SimpleNamespace(browser="chromium",cdp_port=9223,browser_arg=["--user-data-dir=/other"],
                                   hardware_arg=[],software_arg=[])
        with self.assertRaisesRegex(ValueError,"cannot be overridden"):
            h.browser_command(args,Path("/test"),"hardware")


class ProfileTests(unittest.TestCase):
    def args(self, common=None):
        return types.SimpleNamespace(browser="chromium",cdp_port=9223,browser_arg=common or [],
                                     hardware_arg=[],software_arg=[])

    def test_basic_is_scoped_to_new_test_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile=Path(tmp)/"new-profile"
            command=h.browser_command(self.args(),profile,"hardware")
            self.assertIn("--password-store=basic",command)
            self.assertIn("--user-data-dir="+str(profile),command)
            self.assertFalse(profile.exists())

    def test_existing_profile_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError,"new empty test profile"):
                h.browser_command(self.args(),Path(tmp),"hardware")

    def test_previous_explicit_basic_flag_not_duplicated(self):
        with tempfile.TemporaryDirectory() as tmp:
            command=h.browser_command(self.args(["--password-store=basic"]),Path(tmp)/"new","hardware")
            self.assertEqual(command.count("--password-store=basic"),1)

    def test_keyring_override_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError,"basic password store"):
                h.browser_command(self.args(["--password-store=gnome-libsecret"]),Path(tmp)/"new","hardware")


class MediaHTTPTests(unittest.TestCase):
    def test_range_formats_and_invalid_requests(self):
        self.assertEqual(h.range_bounds(None,100),(0,99,False))
        self.assertEqual(h.range_bounds("bytes=10-19",100),(10,19,True))
        self.assertEqual(h.range_bounds("bytes=-10",100),(90,99,True))
        self.assertEqual(h.range_bounds("bytes=90-",100),(90,99,True))
        for value in ("bytes=100-","bytes=10-2","bytes=0-2,4-8","bytes=-0"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):h.range_bounds(value,100)

    def test_real_http_range_and_path_restriction(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/"h264.mp4").write_bytes(b"0123456789");(root/"player.html").write_text("test")
            requests=[]
            server=http.server.ThreadingHTTPServer(("127.0.0.1",0),h.make_handler(root,root/"player.html",requests))
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            try:
                url="http://127.0.0.1:%d"%server.server_port
                request=urllib.request.Request(url+"/media/h264.mp4",headers={"Range":"bytes=2-5"})
                with urllib.request.urlopen(request) as response:
                    self.assertEqual(response.status,206);self.assertEqual(response.read(),b"2345")
                    self.assertEqual(response.headers["Content-Range"],"bytes 2-5/10")
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    urllib.request.urlopen(url+"/media/../../etc/passwd")
                self.assertEqual(caught.exception.code,404)
                caught.exception.close()
                self.assertEqual(requests[0]["status"],206)
                self.assertEqual(requests[1]["status"],404)
                health=h.http_health(server.server_port,root/"player.html")
                self.assertEqual(health["bytes"],4)
                self.assertEqual(requests[-1]["user_agent"],"Y4-harness-health")
            finally:
                server.shutdown();server.server_close();thread.join()


if __name__=="__main__":
    unittest.main(verbosity=2)
