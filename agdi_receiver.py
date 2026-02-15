import socket
import struct
import threading

class AGDIReceiver(threading.Thread):
    def __init__(self, port=9999):
        super().__init__()
        self.port = port
        self.daemon = True
        self.running = False
        self.mem_cache = {} # {addr: data}

    def run(self):
        self.running = True
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(('127.0.0.1', self.port))
        server.listen(1)
        
        while self.running:
            try:
                server.settimeout(1.0)
                try:
                    conn, addr = server.accept()
                except socket.timeout:
                    continue
                
                while self.running:
                    header = conn.recv(8)
                    if not header: break
                    
                    addr, size = struct.unpack('<II', header)
                    data = b''
                    while len(data) < size:
                        chunk = conn.recv(size - len(data))
                        if not chunk: break
                        data += chunk
                    
                    if data:
                        # 粒度可以是按页或者按块缓存
                        self.mem_cache[addr] = data
                conn.close()
            except Exception as e:
                pass
                
    def read_mem(self, addr, size):
        # 从缓存中查找最接近的匹配
        # 这只是个示意实现
        for start_addr, data in self.mem_cache.items():
            if start_addr <= addr and addr + size <= start_addr + len(data):
                offset = addr - start_addr
                return data[offset:offset+size]
        return None

class AGDILink:
    def __init__(self, receiver):
        self.receiver = receiver
        self.mode = 'arm'
        self.core_regs = {}

    def open(self, mode, core, speed):
        pass

    def close(self):
        self.receiver.stop()

    def read_mem_U8(self, addr, count):
        data = self.receiver.read_mem(addr, count)
        if data:
            return list(data)
        return [0] * count

    def write_mem_U8(self, addr, data):
        # AGDI Proxy 模式通常是只读被动监听，若要写需要额外实现控制逻辑
        pass

    def read_U32(self, addr):
        data = self.read_mem_U8(addr, 4)
        return struct.unpack('<I', bytes(data))[0]

    def write_U32(self, addr, val):
        pass

    def read_reg(self, reg):
        return 0

    def halted(self):
        return False
                
    def stop(self):
        self.running = False
