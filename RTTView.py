#! python3
import os
import re
import sys
import ctypes
import struct
import datetime
import collections
import configparser
import time

from PyQt5 import QtCore, QtGui, QtWidgets, uic
from PyQt5.QtCore import pyqtSlot, pyqtSignal, Qt
from PyQt5.QtWidgets import QApplication, QWidget, QDialog, QFileDialog, QTableWidgetItem
from PyQt5.QtChart import QChart, QChartView, QLineSeries

import jlink
import xlink
import gdbserver

# 强制使用 CMSIS-DAP v2 (WinUSB) 后端，以尝试与 Keil 共享连接
os.environ['PYOCD_USB_BACKEND'] = 'pyusb_v2'

os.environ['PATH'] = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'libusb-1.0.24/MinGW64/dll') + os.pathsep + os.environ['PATH']


class RingBuffer(ctypes.Structure):
    _fields_ = [
        ('sName',        ctypes.c_uint),    # ctypes.POINTER(ctypes.c_char)，64位Python中 ctypes.POINTER 是64位的，与目标芯片不符
        ('pBuffer',      ctypes.c_uint),    # ctypes.POINTER(ctypes.c_byte)
        ('SizeOfBuffer', ctypes.c_uint),
        ('WrOff',        ctypes.c_uint),    # Position of next item to be written. 对于aUp：   芯片更新WrOff，主机更新RdOff
        ('RdOff',        ctypes.c_uint),    # Position of next item to be read.    对于aDown： 主机更新WrOff，芯片更新RdOff
        ('Flags',        ctypes.c_uint),
    ]

class SEGGER_RTT_CB(ctypes.Structure):      # Control Block
    _fields_ = [
        ('acID',              ctypes.c_char * 16),
        ('MaxNumUpBuffers',   ctypes.c_uint),
        ('MaxNumDownBuffers', ctypes.c_uint),
        ('aUp',               RingBuffer * 2),
        ('aDown',             RingBuffer * 2),
    ]


Variable = collections.namedtuple('Variable', 'name addr size')                 # variable from *.elf file
Valuable = collections.namedtuple('Valuable', 'name addr size typ fmt show')    # variable to read and display

zero_if = lambda i: 0 if i == -1 else i

'''
from RTTView_UI import Ui_RTTView
class RTTView(QWidget, Ui_RTTView):
    def __init__(self, parent=None):
        super(RTTView, self).__init__(parent)
        
        self.setupUi(self)
'''
class RTTView(QWidget):
    def __init__(self, parent=None):
        super(RTTView, self).__init__(parent)
        
        uic.loadUi('RTTView.ui', self)

        self.hWidget2.setVisible(False)

        self.tblVar.horizontalHeader().setSectionResizeMode(0, QtWidgets.QHeaderView.Stretch)

        self.Vars = {}  # {name: Variable}
        self.Vals = {}  # {row:  Valuable}

        self.initSetting()

        self.initQwtPlot()

        self.rcvbuff = b''
        self.rcvfile = None

        self.elffile = None
        
        self.gdb = None

        self.tmrRTT = QtCore.QTimer()
        self.tmrRTT.setInterval(10)
        self.tmrRTT.timeout.connect(self.on_tmrRTT_timeout)
        self.tmrRTT.start()

        self.tmrRTT_Cnt = 0
    
    def initSetting(self):
        if not os.path.exists('setting.ini'):
            open('setting.ini', 'w', encoding='utf-8')
        
        self.conf = configparser.ConfigParser()
        self.conf.read('setting.ini', encoding='utf-8')
        
        if not self.conf.has_section('link'):
            self.conf.add_section('link')
            self.conf.set('link', 'mode', 'ARM SWD')
            self.conf.set('link', 'speed', '4 MHz')
            self.conf.set('link', 'jlink', 'path/to/JLink_x64.dll')
            self.conf.set('link', 'select', '')
            self.conf.set('link', 'address', '["0x20000000"]')
            self.conf.set('link', 'variable', '{}')
            self.conf.set('link', 'gdbserver', '2331')

        self.cmbMode.setCurrentIndex(zero_if(self.cmbMode.findText(self.conf.get('link', 'mode'))))
        self.cmbSpeed.setCurrentIndex(zero_if(self.cmbSpeed.findText(self.conf.get('link', 'speed'))))

        self.cmbDLL.addItem(self.conf.get('link', 'jlink'), 'jlink')
        self.cmbDLL.addItem('OpenOCD Tcl RPC (6666)', 'openocd')
        self.cmbDLL.addItem('Keil uVision COM', 'keil')
        self.daplink_detect()    # add DAPLink

        self.cmbDLL.setCurrentIndex(zero_if(self.cmbDLL.findText(self.conf.get('link', 'select'))))

        self.cmbAddr.addItems(eval(self.conf.get('link', 'address')))

        self.Vals = eval(self.conf.get('link', 'variable'))

        if not self.conf.has_section('encode'):
            self.conf.add_section('encode')
            self.conf.set('encode', 'input', 'ASCII')
            self.conf.set('encode', 'output', 'ASCII')
            self.conf.set('encode', 'oenter', r'\r\n')  # output enter (line feed)

            self.conf.add_section('display')
            self.conf.set('display', 'ncurve', '4')     # max curve number supported
            self.conf.set('display', 'npoint', '1000')

            self.conf.add_section('others')
            self.conf.set('others', 'history', '11 22 33 AA BB CC')
            self.conf.set('others', 'savfile', os.path.join(os.getcwd(), 'rtt_data.txt'))

        self.cmbICode.setCurrentIndex(zero_if(self.cmbICode.findText(self.conf.get('encode', 'input'))))
        self.cmbOCode.setCurrentIndex(zero_if(self.cmbOCode.findText(self.conf.get('encode', 'output'))))
        self.cmbEnter.setCurrentIndex(zero_if(self.cmbEnter.findText(self.conf.get('encode', 'oenter'))))

        self.N_CURVE = int(self.conf.get('display', 'ncurve'), 10)
        self.N_POINT = int(self.conf.get('display', 'npoint'), 10)

        self.linFile.setText(self.conf.get('others', 'savfile'))

        self.txtSend.setPlainText(self.conf.get('others', 'history'))

    def initQwtPlot(self):
        self.PlotData  = [[0]*self.N_POINT for i in range(self.N_CURVE)]
        self.PlotPoint = [[QtCore.QPointF(j, 0) for j in range(self.N_POINT)] for i in range(self.N_CURVE)]

        self.PlotChart = QChart()

        self.ChartView = QChartView(self.PlotChart)
        self.ChartView.setVisible(False)
        self.vLayout.insertWidget(0, self.ChartView)
        
        self.PlotCurve = [QLineSeries() for i in range(self.N_CURVE)]

    def daplink_detect(self):
        if self.btnOpen.text() == '关闭连接':
            return
        
        try:
            from pyocd.probe import aggregator
            self.daplinks = aggregator.DebugProbeAggregator.get_all_connected_probes()
        except Exception as e:
            self.daplinks = []

        # 检查是否真的需要刷新列表（通过比较数量和数据，避免无谓的 removeItem 导致 UI 索引重置）
        # 这里的 cmbDLL.count() - 2 是因为前两个是固定的 J-Link 和 OpenOCD
        if len(self.daplinks) * 2 != self.cmbDLL.count() - 2:
            # 记录当前选中的文本，以便刷新后恢复
            current_text = self.cmbDLL.currentText()
            
            # 清除原有的 DAP-Link 项（从索引 2 开始）
            while self.cmbDLL.count() > 2:
                self.cmbDLL.removeItem(2)

            # 重新添加
            for i, daplink in enumerate(self.daplinks):
                self.cmbDLL.addItem(f'{daplink.product_name} ({daplink.unique_id})', i)
                self.cmbDLL.addItem(f'[Shared] {daplink.product_name} ({daplink.unique_id})', f'shared_{i}')
            
            # 关键：尝试恢复刷新前的选择，防止跳转到索引 0 或 1 (J-Link/OpenOCD)
            index = self.cmbDLL.findText(current_text)
            if index != -1:
                self.cmbDLL.setCurrentIndex(index)

    @pyqtSlot()
    def on_btnOpen_clicked(self):
        if self.btnOpen.text() == '打开连接':
            mode = self.cmbMode.currentText()
            mode = mode.replace(' SWD', '').replace(' cJTAG', '').replace(' JTAG', 'J').lower()
            core = 'Cortex-M0' if mode.startswith('arm') else 'RISC-V'
            speed= int(self.cmbSpeed.currentText().split()[0]) * 1000 # KHz
            try:
                item_data = self.cmbDLL.currentData()

                if item_data == 'jlink':
                    self.xlk = xlink.XLink(jlink.JLink(self.cmbDLL.currentText(), mode, core, speed))
                
                elif item_data == 'openocd':
                    import openocd
                    self.xlk = xlink.XLink(openocd.OpenOCD(mode=mode, core=core, speed=speed))
                
                elif item_data == 'keil':
                    import keil
                    self.xlk = xlink.XLink(keil.Keil())
                    self.xlk.open(mode, core, speed)
                    self.txtMain.append(f'\n[Keil uVision COM] 连接成功。\n')

                elif str(item_data).startswith('shared_'):
                    from pyocd.coresight import dap, ap, cortex_m
                    from pyocd.probe.debug_probe import DebugProbe
                    
                    os.environ['PYOCD_USB_BACKEND'] = 'pyusb_v2'
                    idx = int(item_data.split('_')[1])
                    daplink = self.daplinks[idx]
                    
                    if getattr(daplink, 'is_open', False):
                        try: daplink.close()
                        except: pass
                    
                    daplink.open()
                    try:
                        daplink.connect(DebugProbe.Protocol.SWD)
                    except:
                        pass

                    _dp = dap.DebugPort(daplink, None)
                    # 共享模式下尽量静默，仅在必要时初始化
                    _dp.set_clock(speed * 1000) 

                    _ap = ap.AHB_AP(_dp, 0)
                    
                    # 共享模式关键：强制失效 SELECT 寄存器缓存，防止与 Keil 冲突
                    if hasattr(daplink, '_invalidate_cached_registers'):
                        daplink._invalidate_cached_registers()

                    # 尝试读取 IDR 确认连接，增加重试以应对竞争
                    _ap.idr = 0
                    for i in range(3):
                        try:
                            _ap.idr = _ap.read_reg(ap.AP_IDR)
                            if _ap.idr: break
                        except:
                            time.sleep(0.05)
                            if hasattr(daplink, '_invalidate_cached_registers'):
                                daplink._invalidate_cached_registers()

                    self.xlk = xlink.XLink(cortex_m.CortexM(None, _ap))
                    self.txtMain.append(f'\n[DAP-Link Shared Mode] {daplink.product_name} 开启成功。\n')

                else:
                    from pyocd.coresight import dap, ap, cortex_m
                    # 普通模式：恢复默认后端并执行标准初始化
                    if 'PYOCD_USB_BACKEND' in os.environ:
                        del os.environ['PYOCD_USB_BACKEND']
                    
                    daplink = self.daplinks[item_data]
                    daplink.open()

                    _dp = dap.DebugPort(daplink, None)
                    _dp.init()
                    _dp.power_up_debug()
                    _dp.set_clock(speed * 1000)

                    _ap = ap.AHB_AP(_dp, 0)
                    _ap.init()

                    self.xlk = xlink.XLink(cortex_m.CortexM(None, _ap))
                
                if hasattr(self, 'xlk') and self.xlk:
                    port = int(self.conf.get('link', 'gdbserver', fallback='2331'))
                    self.gdb = gdbserver.GDBServer(self.xlk, port)
                    self.gdb.start()

                if self.chkSave.isChecked():
                    savfile, ext = os.path.splitext(self.linFile.text())
                    savfile = f'{savfile}_{datetime.datetime.now().strftime("%y%m%d%H%M%S")}{ext}'

                    self.rcvfile = open(savfile, 'w')

                if re.match(r'0[xX][0-9a-fA-F]{8}', self.cmbAddr.currentText()):
                    addr = int(self.cmbAddr.currentText(), 16)
                    for i in range(64):
                        self.xlk_invalidate_cache()
                        data = self.xlk.read_mem_U8(addr + 1024 * i, 1024 + 32) # 多读32字节，防止搜索内容在边界处
                        index = bytes(data).find(b'SEGGER RTT')
                        if index != -1:
                            self.RTTAddr = addr + 1024 * i + index

                            data = self.xlk.read_mem_U8(self.RTTAddr, ctypes.sizeof(SEGGER_RTT_CB))

                            rtt_cb = SEGGER_RTT_CB.from_buffer(bytearray(data))
                            self.aUpAddr = self.RTTAddr + 16 + 4 + 4
                            self.aDownAddr = self.aUpAddr + ctypes.sizeof(RingBuffer) * rtt_cb.MaxNumUpBuffers

                            self.txtMain.append(f'\n_SEGGER_RTT @ 0x{self.RTTAddr:08X} with {rtt_cb.MaxNumUpBuffers} aUp and {rtt_cb.MaxNumDownBuffers} aDown\n')
                            break
                        
                    else:
                        raise Exception('Can not find _SEGGER_RTT')

                    self.rtt_cb = True

                else:
                    self.rtt_cb = False

            except Exception as e:
                self.txtMain.append(f'\nerror: {str(e)}\n')
                if 'daplink' in locals():
                    try: daplink.close()
                    except: pass
                if hasattr(self, 'xlk') and self.xlk:
                    try: self.xlk.close()
                    except: pass

            else:
                self.cmbDLL.setEnabled(False)
                self.btnDLL.setEnabled(False)
                self.cmbAddr.setEnabled(False)
                self.chkSave.setEnabled(False)
                self.btnOpen.setText('关闭连接')

        else:
            if self.rcvfile and not self.rcvfile.closed:
                self.rcvfile.close()

            if self.gdb:
                self.gdb.stop()
                self.gdb = None

            try:
                self.xlk.close()
            except:
                pass

            self.cmbDLL.setEnabled(True)
            self.btnDLL.setEnabled(True)
            self.cmbAddr.setEnabled(True)
            self.chkSave.setEnabled(True)
            self.btnOpen.setText('打开连接')

            self.cmbDLL.setEnabled(True)
            self.btnDLL.setEnabled(True)
            self.cmbAddr.setEnabled(True)
            self.chkSave.setEnabled(True)
            self.btnOpen.setText('打开连接')
    
    def xlk_invalidate_cache(self):
        # 共享模式关键：由于 Keil 会修改 DP SELECT 寄存器，我们必须让 pyocd 失效相关缓存
        # 否则 RTTView 会读写错误的 AP/Bank。
        if hasattr(self, 'xlk') and hasattr(self.xlk, 'xlk') and hasattr(self.xlk.xlk, 'ap'):
            probe = getattr(self.xlk.xlk.ap.dp, 'link', None)
            if probe and hasattr(probe, '_invalidate_cached_registers'):
                probe._invalidate_cached_registers()

    def aUpRead(self):
        if isinstance(self.xlk, RawTCPLink):
            return self.xlk.recv()

        # 针对 DAP-Link 共享模式，每次读写前强制失效 SELECT 寄存器缓存，防止与 Keil 冲突
        self.xlk_invalidate_cache()

        data = self.xlk.read_mem_U8(self.aUpAddr, ctypes.sizeof(RingBuffer))

        aUp = RingBuffer.from_buffer(bytearray(data))
        
        if aUp.RdOff <= aUp.WrOff:
            cnt = aUp.WrOff - aUp.RdOff

        else:
            cnt = aUp.SizeOfBuffer - aUp.RdOff

        if 0 < cnt < 1024*1024:
            data = self.xlk.read_mem_U8(ctypes.cast(aUp.pBuffer, ctypes.c_void_p).value + aUp.RdOff, cnt)
            
            aUp.RdOff = (aUp.RdOff + cnt) % aUp.SizeOfBuffer
            
            self.xlk.write_U32(self.aUpAddr + 4*4, aUp.RdOff)

        else:
            data = []
        
        # 共享模式礼让
        if '[Shared]' in self.cmbDLL.currentText():
            time.sleep(0.005)

        return bytes(data)

    def aDownWrite(self, bytes):
        # 针对 DAP-Link 共享模式，写操作前同样需要失效 SELECT 缓存
        self.xlk_invalidate_cache()

        data = self.xlk.read_mem_U8(self.aDownAddr, ctypes.sizeof(RingBuffer))

        aDown = RingBuffer.from_buffer(bytearray(data))
        
        if aDown.WrOff >= aDown.RdOff:
            if aDown.RdOff != 0: cnt = min(aDown.SizeOfBuffer - aDown.WrOff, len(bytes))
            else:                cnt = min(aDown.SizeOfBuffer - 1 - aDown.WrOff, len(bytes))   # 写入操作不能使得 aDown.WrOff == aDown.RdOff，以区分满和空
            self.xlk.write_mem(ctypes.cast(aDown.pBuffer, ctypes.c_void_p).value + aDown.WrOff, bytes[:cnt])
            
            aDown.WrOff += cnt
            if aDown.WrOff == aDown.SizeOfBuffer: aDown.WrOff = 0

            bytes = bytes[cnt:]

        if bytes and aDown.RdOff != 0 and aDown.RdOff != 1:        # != 0 确保 aDown.WrOff 折返回 0，!= 1 确保有空间可写入
            cnt = min(aDown.RdOff - 1 - aDown.WrOff, len(bytes))   # - 1 确保写入操作不导致WrOff与RdOff指向同一位置
            self.xlk.write_mem(ctypes.cast(aDown.pBuffer, ctypes.c_void_p).value + aDown.WrOff, bytes[:cnt])

            aDown.WrOff += cnt

        self.xlk.write_U32(self.aDownAddr + 4*3, aDown.WrOff)
    
    def on_tmrRTT_timeout(self):
        self.tmrRTT_Cnt += 1
        if self.btnOpen.text() == '关闭连接':
            # 共享模式下，“深度礼让”：降低频率至 1/5 (每 50ms 访问一次)
            is_shared = '[Shared]' in self.cmbDLL.currentText()
            if is_shared and self.tmrRTT_Cnt % 5 != 0:
                return

            try:
                if self.rtt_cb:
                    rcvdbytes = self.aUpRead()

                else:
                    vals = []
                    for name, addr, size, typ, fmt, show in self.Vals.values():
                        if show:
                            self.xlk_invalidate_cache()
                            buf = self.xlk.read_mem_U8(addr, size)
                            vals.append(struct.unpack(fmt, bytes(buf))[0])
                            if is_shared: time.sleep(0.002) # 变量读取间隙
                    
                    if is_shared: time.sleep(0.005) # 完成一轮读取后的礼让

                    rcvdbytes = b'\t'.join(f'{val}'.encode() for val in vals) + b',\n'
            
            except Exception as e:
                rcvdbytes = b''
                # 共享模式下，不打印“通信异常”以免干扰 UI
                threshold = 100 if is_shared else 10
                if self.tmrRTT_Cnt % threshold == 0:
                    if not is_shared:
                        self.txtMain.append(f'\n通信异常: {str(e)}\n')
                        self.on_btnOpen_clicked() 
                        QtWidgets.QMessageBox.critical(self, "连接断开", f"与调试器通信失败: {str(e)}")
                        return

            if rcvdbytes:
                if self.rcvfile and not self.rcvfile.closed:
                    self.rcvfile.write(rcvdbytes.decode('latin-1'))

                self.rcvbuff += rcvdbytes
                
                if self.chkWave.isChecked():
                    if b',' in self.rcvbuff:
                        try:
                            d = self.rcvbuff[0:self.rcvbuff.rfind(b',')].split(b',')        # [b'12', b'34'] or [b'12 34', b'56 78']
                            if self.cmbICode.currentText() != 'HEX':
                                d = [[float(x)   for x in X.strip().split()] for X in d]    # [[12], [34]]   or [[12, 34], [56, 78]]
                            else:
                                d = [[int(x, 16) for x in X.strip().split()] for X in d]    # for example, d = [b'12', b'AA', b'5A5A']
                            for arr in d:
                                for i, x in enumerate(arr):
                                    if i == self.N_CURVE: break

                                    self.PlotData[i].pop(0)
                                    self.PlotData[i].append(x)
                                    self.PlotPoint[i].pop(0)
                                    self.PlotPoint[i].append(QtCore.QPointF(999, x))
                            
                            self.rcvbuff = self.rcvbuff[self.rcvbuff.rfind(b',')+1:]

                            if self.tmrRTT_Cnt % 4 == 0:
                                if len(d[-1]) != len([series for series in self.PlotChart.series() if series.isVisible()]):
                                    for series in self.PlotChart.series():
                                        self.PlotChart.removeSeries(series)
                                    for i in range(min(len(d[-1]), self.N_CURVE)):
                                        self.PlotCurve[i].setName(f'Curve {i+1}')
                                        self.PlotChart.addSeries(self.PlotCurve[i])
                                    self.PlotChart.createDefaultAxes()

                                for i in range(len(self.PlotChart.series())):
                                    for j, point in enumerate(self.PlotPoint[i]):
                                        point.setX(j)
                                
                                    self.PlotCurve[i].replace(self.PlotPoint[i])
                            
                                miny = min([min(d) for d in self.PlotData[:len(self.PlotChart.series())]])
                                maxy = max([max(d) for d in self.PlotData[:len(self.PlotChart.series())]])
                                self.PlotChart.axisY().setRange(miny, maxy)
                                self.PlotChart.axisX().setRange(0000, self.N_POINT)
            
                        except Exception as e:
                            self.rcvbuff = b''
                            print(e)

                else:
                    text = ''
                    if self.cmbICode.currentText() == 'ASCII':
                        text = ''.join([chr(x) for x in self.rcvbuff])
                        self.rcvbuff = b''

                    elif self.cmbICode.currentText() == 'HEX':
                        text = ' '.join([f'{x:02X}' for x in self.rcvbuff]) + ' '
                        self.rcvbuff = b''

                    elif self.cmbICode.currentText() == 'GBK':
                        while len(self.rcvbuff):
                            if self.rcvbuff[0:1].decode('GBK', 'ignore'):
                                text += self.rcvbuff[0:1].decode('GBK')
                                self.rcvbuff = self.rcvbuff[1:]

                            elif len(self.rcvbuff) > 1 and self.rcvbuff[0:2].decode('GBK', 'ignore'):
                                text += self.rcvbuff[0:2].decode('GBK')
                                self.rcvbuff = self.rcvbuff[2:]

                            elif len(self.rcvbuff) > 1:
                                text += chr(self.rcvbuff[0])
                                self.rcvbuff = self.rcvbuff[1:]

                            else:
                                break

                    elif self.cmbICode.currentText() == 'UTF-8':
                        while len(self.rcvbuff):
                            if self.rcvbuff[0:1].decode('UTF-8', 'ignore'):
                                text += self.rcvbuff[0:1].decode('UTF-8')
                                self.rcvbuff = self.rcvbuff[1:]

                            elif len(self.rcvbuff) > 1 and self.rcvbuff[0:2].decode('UTF-8', 'ignore'):
                                text += self.rcvbuff[0:2].decode('UTF-8')
                                self.rcvbuff = self.rcvbuff[2:]

                            elif len(self.rcvbuff) > 2 and self.rcvbuff[0:3].decode('UTF-8', 'ignore'):
                                text += self.rcvbuff[0:3].decode('UTF-8')
                                self.rcvbuff = self.rcvbuff[3:]

                            elif len(self.rcvbuff) > 3 and self.rcvbuff[0:4].decode('UTF-8', 'ignore'):
                                text += self.rcvbuff[0:4].decode('UTF-8')
                                self.rcvbuff = self.rcvbuff[4:]

                            elif len(self.rcvbuff) > 3:
                                text += chr(self.rcvbuff[0])
                                self.rcvbuff = self.rcvbuff[1:]

                            else:
                                break
                    
                    if len(self.txtMain.toPlainText()) > 25000: self.txtMain.clear()
                    self.txtMain.moveCursor(QtGui.QTextCursor.End)
                    self.txtMain.insertPlainText(text)

        else:
            if self.tmrRTT_Cnt % 100 == 1:
                self.daplink_detect()

            if self.tmrRTT_Cnt % 100 == 2:
                path = self.cmbAddr.currentText()
                if os.path.exists(path) and os.path.isfile(path):
                    if self.elffile != (path, os.path.getmtime(path)):
                        self.elffile = (path, os.path.getmtime(path))

                        self.parse_elffile(path)

    @pyqtSlot()
    def on_btnSend_clicked(self):
        if self.btnOpen.text() == '关闭连接':
            text = self.txtSend.toPlainText()

            if self.cmbOCode.currentText() == 'HEX':
                try:
                    self.aDownWrite(bytes([int(x, 16) for x in text.split()]))
                except Exception as e:
                    print(e)

            else:
                if self.cmbEnter.currentText() == r'\r\n':
                    text = text.replace('\n', '\r\n')
                
                try:
                    self.aDownWrite(text.encode(self.cmbOCode.currentText()))
                except Exception as e:
                    print(e)

    @pyqtSlot()
    def on_btnDLL_clicked(self):
        dllpath, filter = QFileDialog.getOpenFileName(caption='JLink_x64.dll path', filter='动态链接库文件 (*.dll *.so)', directory=self.cmbDLL.itemText(0))
        if dllpath != '':
            self.cmbDLL.setItemText(0, dllpath)

    @pyqtSlot()
    def on_btnAddr_clicked(self):
        elfpath, filter = QFileDialog.getOpenFileName(caption='elf file path', filter='elf file (*.elf *.axf *.out)', directory=self.cmbAddr.currentText())
        if elfpath != '':
            self.cmbAddr.insertItem(0, elfpath)
            self.cmbAddr.setCurrentIndex(0)

    @pyqtSlot(str)
    def on_cmbAddr_currentIndexChanged(self, text):
        if re.match(r'0[xX][0-9a-fA-F]{8}', text):
            self.tblVar.setVisible(False)
            self.gLayout2.removeWidget(self.tblVar)

            self.txtSend.setVisible(True)
            self.btnSend.setVisible(True)
            self.cmbICode.setEnabled(True)
            self.cmbOCode.setEnabled(True)
            self.cmbEnter.setEnabled(True)

        else:
            self.txtSend.setVisible(False)
            self.btnSend.setVisible(False)
            self.cmbICode.setEnabled(False)
            self.cmbOCode.setEnabled(False)
            self.cmbEnter.setEnabled(False)

            self.gLayout2.addWidget(self.tblVar, 0, 0, 5, 2)
            self.tblVar.setVisible(True)

    @pyqtSlot(int)
    def on_chkSave_stateChanged(self, state):
        self.hWidget2.setVisible(state == Qt.Checked)
    
    @pyqtSlot()
    def on_btnFile_clicked(self):
        savfile, filter = QFileDialog.getSaveFileName(caption='数据保存文件路径', filter='文本文件 (*.txt)', directory=self.linFile.text())
        if savfile:
            self.linFile.setText(savfile)

    def parse_elffile(self, path):
        try:
            from elftools.elf.elffile import ELFFile
            elffile = ELFFile(open(path, 'rb'))

            self.Vars = {}
            for sym in elffile.get_section_by_name('.symtab').iter_symbols():
                if sym.entry['st_info']['type'] == 'STT_OBJECT':
                    self.Vars[sym.name] = Variable(sym.name, sym.entry['st_value'], sym.entry['st_size'])

            if elffile.has_dwarf_info():
                dwarfinfo = elffile.get_dwarf_info()

                def get_type_die(die):
                    while 'DW_AT_type' in die.attributes:
                        die = die.get_DIE_from_attribute('DW_AT_type')
                        if die.tag not in ('DW_TAG_typedef', 'DW_TAG_const_type', 'DW_TAG_volatile_type'):
                            return die
                    return None

                def parse_struct(die, addr, name):
                    for child in die.iter_children():
                        if child.tag == 'DW_TAG_member':
                            if 'DW_AT_name' not in child.attributes or 'DW_AT_data_member_location' not in child.attributes:
                                continue
                            m_name = child.attributes['DW_AT_name'].value.decode('utf-8')
                            m_off = child.attributes['DW_AT_data_member_location'].value
                            if not isinstance(m_off, int):
                                if isinstance(m_off, list) and len(m_off) > 1 and m_off[0] == 0x23:
                                    val, shift, i = 0, 0, 1
                                    while i < len(m_off):
                                        byte = m_off[i]
                                        val |= (byte & 0x7F) << shift
                                        i += 1
                                        if not (byte & 0x80): break
                                        shift += 7
                                    m_off = val
                                else:
                                    m_off = 0
                            
                            t_die = get_type_die(child)
                            if t_die:
                                size = t_die.attributes['DW_AT_byte_size'].value if 'DW_AT_byte_size' in t_die.attributes else 0
                                f_name = f"{name}.{m_name}"
                                if size in (1, 2, 4, 8):
                                    self.Vars[f_name] = Variable(f_name, addr + m_off, size)
                                if t_die.tag == 'DW_TAG_structure_type':
                                    parse_struct(t_die, addr + m_off, f_name)

                for CU in dwarfinfo.iter_CUs():
                    for die in CU.get_top_DIE().iter_children():
                        if die.tag == 'DW_TAG_variable' and 'DW_AT_name' in die.attributes:
                            v_name = die.attributes['DW_AT_name'].value.decode('utf-8')
                            if v_name in self.Vars:
                                t_die = get_type_die(die)
                                if t_die and t_die.tag == 'DW_TAG_structure_type':
                                    parse_struct(t_die, self.Vars[v_name].addr, v_name)

            self.Vars = {k: v for k, v in self.Vars.items() if v.size in (1, 2, 4, 8)}

        except Exception as e:
            print(f'parse elf file fail: {e}')

        else:
            Vals = {row: val for row, val in self.Vals.items() if val.name in self.Vars}
            self.Vals = {i: val for i, val in enumerate(Vals.values())}

            for row, val in self.Vals.items():
                var = self.Vars[val.name]
                if val.addr != var.addr:
                    self.Vals[row] = self.Vals[row]._replace(addr = var.addr)
                if val.size != var.size:
                    typ, fmt = self.len2type[var.size][0]
                    self.Vals[row] = self.Vals[row]._replace(size = var.size, typ = typ, fmt = fmt)

            self.tblVar_redraw()

    len2type = {
        1: [('int8',  'b'), ('uint8',  'B')],
        2: [('int16', 'h'), ('uint16', 'H')],
        4: [('int32', 'i'), ('uint32', 'I'), ('float',  'f')],
        8: [('int64', 'q'), ('uint64', 'Q'), ('double', 'd')]
    }

    def tblVar_redraw(self):
        while self.tblVar.rowCount():
            self.tblVar.removeRow(0)

        for series in self.PlotChart.series():
            self.PlotChart.removeSeries(series)

        for row, val in self.Vals.items():
            self.tblVar.insertRow(row)
            self.tblVar_setRow(row, val)

        if self.tblVar.rowCount() < self.N_CURVE:
            self.tblVar.insertRow(self.tblVar.rowCount())

    def tblVar_setRow(self, row: int, val: Valuable):
        self.tblVar.setItem(row, 0, QTableWidgetItem(val.name))
        self.tblVar.setItem(row, 1, QTableWidgetItem(f'{val.addr:08X}'))
        self.tblVar.setItem(row, 2, QTableWidgetItem(val.typ))
        self.tblVar.setItem(row, 3, QTableWidgetItem('显示' if val.show else '不显示'))
        self.tblVar.setItem(row, 4, QTableWidgetItem('删除'))

        self.PlotCurve[row].setName(val.name)
        self.PlotCurve[row].setVisible(val.show)
        if self.PlotCurve[row] not in self.PlotChart.series():
            self.PlotChart.addSeries(self.PlotCurve[row])
            self.PlotChart.createDefaultAxes()

    @pyqtSlot(int, int)
    def on_tblVar_cellDoubleClicked(self, row, column):
        if self.btnOpen.text() == '关闭连接': return

        if column < 3:
            dlg = VarDialog(self, row)
            if dlg.exec() == QDialog.Accepted:
                var = self.Vars[dlg.cmbName.currentText()]
                typ, fmt = dlg.cmbType.currentText(), dlg.cmbType.currentData()

                self.Vals[row] = Valuable(var.name, var.addr, var.size, typ, fmt, True)

                self.tblVar_setRow(row, self.Vals[row])

                if self.tblVar.rowCount() < self.N_CURVE and row == self.tblVar.rowCount() - 1:
                    self.tblVar.insertRow(self.tblVar.rowCount())
        
        elif column == 3:
            if self.tblVar.item(row, 3):
                self.Vals[row] = self.Vals[row]._replace(show = not self.Vals[row].show)

                self.tblVar.item(row, 3).setText('显示' if self.Vals[row].show else '不显示')

                self.PlotCurve[row].setVisible(self.Vals[row].show)

        elif column == 4:
            if self.tblVar.item(row, 4):
                self.Vals.pop(row)
                self.Vals = {i: val for i, val in enumerate(self.Vals.values())}

                self.tblVar_redraw()

    @pyqtSlot(int)
    def on_chkWave_stateChanged(self, state):
        self.ChartView.setVisible(state == Qt.Checked)
        self.txtMain.setVisible(state == Qt.Unchecked)

    @pyqtSlot()
    def on_btnClear_clicked(self):
        self.txtMain.clear()
    
    def closeEvent(self, evt):
        if self.rcvfile and not self.rcvfile.closed:
            self.rcvfile.close()

        self.conf.set('link',   'mode',   self.cmbMode.currentText())
        self.conf.set('link',   'speed',  self.cmbSpeed.currentText())
        self.conf.set('link',   'jlink',  self.cmbDLL.itemText(0))
        self.conf.set('link',   'select', self.cmbDLL.currentText())
        self.conf.set('encode', 'input',  self.cmbICode.currentText())
        self.conf.set('encode', 'output', self.cmbOCode.currentText())
        self.conf.set('encode', 'oenter', self.cmbEnter.currentText())
        self.conf.set('others', 'history', self.txtSend.toPlainText())
        self.conf.set('others', 'savfile', self.linFile.text())

        addrs = [self.cmbAddr.currentText()] + [self.cmbAddr.itemText(i) for i in range(self.cmbAddr.count())]
        self.conf.set('link',   'address', repr(list(collections.OrderedDict.fromkeys(addrs))))   # 保留顺序去重

        self.conf.set('link',   'variable', repr(self.Vals))

        self.conf.write(open('setting.ini', 'w', encoding='utf-8'))
        


from PyQt5.QtWidgets import QSizePolicy, QDialogButtonBox

class VarDialog(QDialog):
    def __init__(self, parent, row):
        super(VarDialog, self).__init__(parent)

        self.resize(400, 150)
        self.setWindowTitle('选择变量')

        self.linSearch = QtWidgets.QLineEdit(self)
        self.linSearch.setPlaceholderText('输入关键字搜索变量...')
        self.linSearch.textChanged.connect(self.on_linSearch_textChanged)

        self.cmbType = QtWidgets.QComboBox(self)
        self.cmbType.setMinimumSize(QtCore.QSize(80, 0))

        self.cmbName = QtWidgets.QComboBox(self)
        self.cmbName.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.cmbName.currentTextChanged.connect(self.on_cmbName_currentTextChanged)
        
        self.hLayout = QtWidgets.QHBoxLayout()
        self.hLayout.addWidget(QtWidgets.QLabel('变量：', self))
        self.hLayout.addWidget(self.cmbName)
        self.hLayout.addWidget(QtWidgets.QLabel('    ', self))
        self.hLayout.addWidget(QtWidgets.QLabel('类型：', self))
        self.hLayout.addWidget(self.cmbType)

        self.btnBox = QtWidgets.QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel, self)
        self.btnBox.accepted.connect(self.accept)
        self.btnBox.rejected.connect(self.reject)
        
        self.vLayout = QtWidgets.QVBoxLayout(self)
        self.vLayout.addWidget(QtWidgets.QLabel('搜索：', self))
        self.vLayout.addWidget(self.linSearch)
        self.vLayout.addLayout(self.hLayout)
        self.vLayout.addItem(QtWidgets.QSpacerItem(20, 40, QSizePolicy.Minimum, QSizePolicy.Expanding))
        self.vLayout.addWidget(self.btnBox)

        self.all_vars = sorted(parent.Vars.keys())
        self.cmbName.addItems(self.all_vars)

        if parent.tblVar.item(row, 0):
            self.cmbName.setCurrentText(parent.tblVar.item(row, 0).text())
            self.cmbType.setCurrentText(parent.tblVar.item(row, 2).text())

    @pyqtSlot(str)
    def on_linSearch_textChanged(self, text):
        self.cmbName.clear()
        if not text:
            self.cmbName.addItems(self.all_vars)
        else:
            filtered = [v for v in self.all_vars if text.lower() in v.lower()]
            self.cmbName.addItems(filtered)

    @pyqtSlot(str)
    def on_cmbName_currentTextChanged(self, name):
        if not name or name not in self.parent().Vars:
            return
        size = self.parent().Vars[name].size

        self.cmbType.clear()
        for typ, fmt in self.parent().len2type[size]:
            self.cmbType.addItem(typ, fmt)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    rtt = RTTView()
    rtt.show()
    app.exec()
