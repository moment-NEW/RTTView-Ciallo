'''
GDB Server Remote Serial Protocol Bridge for RTTView.
Allows Ozone or other GDB clients to connect to the target via RTTView's connection.
'''
import socket
import threading
import re
import struct
import logging

LOG = logging.getLogger(__name__)

class GDBServer(threading.Thread):
    def __init__(self, xlk, port=2331):
        super().__init__()
        self.xlk = xlk
        self.port = port
        self.daemon = True
        self.running = False
        self.sock = None
        self._regs = ['r0', 'r1', 'r2', 'r3', 'r4', 'r5', 'r6', 'r7', 'r8', 'r9', 'r10', 'r11', 'r12', 'sp', 'lr', 'pc', 'xpsr']

    def _checksum(self, data):
        return sum(data.encode('latin-1')) % 256

    def _send_packet(self, conn, data):
        packet = f'${data}#{self._checksum(data):02x}'.encode('latin-1')
        conn.sendall(packet)

    def _handle_packet(self, conn, packet):
        if packet.startswith('qSupported'):
            self._send_packet(conn, 'PacketSize=1000;qXfer:features:read+')
        
        elif packet == 'qXfer:features:read:target.xml:0,ffb':
            # Ozone likes to see architecture
            xml = '<?xml version="1.0"?><!DOCTYPE target SYSTEM "gdb-target.dtd"><target><architecture>arm</architecture></target>'
            self._send_packet(conn, 'l' + xml)

        elif packet == '?':
            self._send_packet(conn, 'S05') # Stop reason: SIGTRAP

        elif packet == 'g':
            # All registers
            vals = []
            for r in self._regs:
                try:
                    v = self.xlk.read_reg(r)
                    vals.append(struct.pack('<I', v).hex())
                except:
                    vals.append('00000000')
            self._send_packet(conn, ''.join(vals))

        elif packet.startswith('p'): # pIDX
            try:
                idx = int(packet[1:], 16)
                if idx < len(self._regs):
                    v = self.xlk.read_reg(self._regs[idx])
                    self._send_packet(conn, struct.pack('<I', v).hex())
                else:
                    self._send_packet(conn, '00000000')
            except:
                self._send_packet(conn, 'E01')

        elif packet.startswith('m'): # mADDR,LEN
            m = re.match(r'm([0-9a-fA-F]+),([0-9a-fA-F]+)', packet)
            if m:
                addr, length = int(m.group(1), 16), int(m.group(2), 16)
                try:
                    data = self.xlk.read_mem_U8(addr, length)
                    self._send_packet(conn, bytes(data).hex())
                except:
                    self._send_packet(conn, 'E01')

        elif packet.startswith('M'): # MADDR,LEN:DATA
            m = re.match(r'M([0-9a-fA-F]+),([0-9a-fA-F]+):(.*)', packet)
            if m:
                addr, length = int(m.group(1), 16), int(m.group(2), 16)
                data = bytes.fromhex(m.group(3))
                try:
                    self.xlk.write_mem_U8(addr, list(data))
                    self._send_packet(conn, 'OK')
                except:
                    self._send_packet(conn, 'E01')

        elif packet == 'vCont?':
            self._send_packet(conn, 'vCont;c;s;t')

        elif packet.startswith('vCont;c') or packet == 'c':
            try:
                self.xlk.go()
                # We don't reply until stopped, but if we want Ozone to stay connected while target runs:
                # Actually, GDB RSP 'c' is expected to block. 
                # But RTTView needs to keep going. 
                # For a bridge, we'll just not reply 'OK' and let the client wait or timeout?
                # Actually, some servers reply 'OK' then send 'T05' later.
                pass
            except:
                self._send_packet(conn, 'E01')

        elif packet.startswith('vCont;s') or packet == 's':
            try:
                self.xlk.step()
                self._send_packet(conn, 'S05')
            except:
                self._send_packet(conn, 'E01')

        elif packet == 'D':
            self._send_packet(conn, 'OK')

        else:
            self._send_packet(conn, '')

    def run(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self.sock.bind(('localhost', self.port))
        except Exception as e:
            print(f"GDB Server bind failed: {e}")
            return

        self.sock.listen(1)
        self.running = True
        print(f"GDB Server listening on port {self.port}")

        while self.running:
            try:
                self.sock.settimeout(1.0)
                conn, addr = self.sock.accept()
                with conn:
                    print(f"GDB Client connected from {addr}")
                    conn.settimeout(5.0)
                    while self.running:
                        try:
                            data = conn.recv(1)
                            if not data: break
                            if data == b'\x03': # Interrupt
                                self.xlk.halt()
                                self._send_packet(conn, 'S05')
                                continue
                            if data == b'$':
                                payload = b''
                                while True:
                                    c = conn.recv(1)
                                    if c == b'#': break
                                    payload += c
                                checksum = conn.recv(2)
                                conn.sendall(b'+')
                                self._handle_packet(conn, payload.decode('latin-1'))
                        except socket.timeout:
                            continue
                        except Exception as e:
                            print(f"GDB Connection error: {e}")
                            break
            except socket.timeout:
                continue
            except Exception as e:
                if self.running:
                    print(f"GDB Server accept error: {e}")
                break

    def stop(self):
        self.running = False
        if self.sock:
            self.sock.close()
