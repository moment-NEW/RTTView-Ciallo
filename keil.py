import win32com.client
import pywintypes
import time

class Keil(object):
    def __init__(self):
        self.uv = None
        self.mode = 'arm'
        self.core_regs = {'pc': 15, 'sp': 13, 'lr': 14}

    def open(self, mode='arm', core='Cortex-M3', speed=1000000):
        try:
            self.uv = win32com.client.GetActiveObject("uVision.Application")
        except Exception:
            try:
                self.uv = win32com.client.Dispatch("uVision.Application")
            except Exception:
                raise Exception("Keil uVision is not running or could not be opened.")
        
        self.mode = mode.lower()

    def close(self):
        self.uv = None

    def read_U32(self, addr):
        return self.uv.Evaluate(f"_RDWORD(0x{addr:08X})") & 0xFFFFFFFF

    def write_U32(self, addr, val):
        self.uv.Evaluate(f"_WDWORD(0x{addr:08X}, 0x{val:08X})")

    def read_mem_U8(self, addr, count):
        # Optimization: Loop is slow but compatible. 
        # For RTT, we'll try to read as much as possible in one go if Keil supports it,
        # but the standard COM Evaluate doesn't return large blocks easily.
        data = []
        for i in range(count):
            data.append(self.uv.Evaluate(f"_RBYTE(0x{addr+i:08X})") & 0xFF)
        return data

    def write_mem_U8(self, addr, data):
        for i, b in enumerate(data):
            self.uv.Evaluate(f"_WBYTE(0x{addr+i:08X}, 0x{b:02X})")

    def read_mem_U32(self, addr, count):
        data = []
        for i in range(count):
            data.append(self.uv.Evaluate(f"_RDWORD(0x{addr+i*4:08X})") & 0xFFFFFFFF)
        return data

    def write_mem_U32(self, addr, data):
        for i, v in enumerate(data):
            self.uv.Evaluate(f"_WDWORD(0x{addr+i*4:08X}, 0x{v:08X})")

    def read_reg(self, reg):
        try:
            return self.uv.Evaluate(reg) & 0xFFFFFFFF
        except:
            return 0

    def read_regs(self, rlist):
        return {reg: self.read_reg(reg) for reg in rlist}

    def write_reg(self, reg, val):
        self.uv.Evaluate(f"{reg} = 0x{val:X}")

    def halted(self):
        # 1: Stopped, 2: Running, 3: Stepping
        return self.uv.Debugger.State == 1

    def halt(self):
        self.uv.Execute("BS")

    def go(self):
        self.uv.Execute("G")

    def reset(self):
        self.uv.Execute("RESET")
