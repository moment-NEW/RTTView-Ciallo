#include <windows.h>
#include <stdio.h>
#include <winsock2.h>

#pragma comment(lib, "ws2_32.lib")

// 定义原 DLL 中的函数指针类型 (根据 AGDI 规范)
typedef int (__stdcall *P_AgReadMem)(unsigned int addr, unsigned char *pB, unsigned int nB, unsigned int *pnRead);
typedef int (__stdcall *P_Generic)(void);

// 全局变量保存原函数指针
HMODULE hOrigDll = NULL;
P_AgReadMem pOrigAgReadMem = NULL;

// IPC 用的 Socket
SOCKET clientSocket = INVALID_SOCKET;

// 简单的初始化 Socket 逻辑
void InitIPCOutput() {
    WSADATA wsaData;
    WSAStartup(MAKEWORD(2, 2), &wsaData);
    clientSocket = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
    sockaddr_in serverAddr;
    serverAddr.sin_family = AF_INET;
    serverAddr.sin_port = htons(9999); // Python 监听这个端口
    serverAddr.sin_addr.s_addr = inet_addr("127.0.0.1");
    connect(clientSocket, (sockaddr*)&serverAddr, sizeof(serverAddr));
}

// 劫持函数: AgReadMem
extern "C" __declspec(dllexport) int __stdcall AgReadMem(unsigned int addr, unsigned char *pB, unsigned int nB, unsigned int *pnRead) {
    if (!pOrigAgReadMem) return -1;

    // 1. 调用原始函数
    int result = pOrigAgReadMem(addr, pB, nB, pnRead);

    // 2. 如果读取成功，且符合我们关注的范围 (比如 RTT 可能所在的 RAM 区)
    // 或者我们直接把所有读取操作都发一份给 Python 过滤
    if (result == 0 && clientSocket != INVALID_SOCKET && nB > 0) {
        // 数据格式: [Address(4B)][Size(4B)][Data(Size B)]
        send(clientSocket, (char*)&addr, 4, 0);
        send(clientSocket, (char*)&nB, 4, 0);
        send(clientSocket, (char*)pB, nB, 0);
    }

    return result;
}

// 通用的转发宏或逻辑需要处理所有 AGDI 导出项
// 这里仅作示意，实际中需要导出 CMSIS_DAP.dll 的所有函数

BOOL APIENTRY DllMain(HMODULE hModule, DWORD ul_reason_for_call, LPVOID lpReserved) {
    switch (ul_reason_for_call) {
    case DLL_PROCESS_ATTACH:
        // 加载重命名后的原 DLL
        hOrigDll = LoadLibraryA("CMSIS_DAP_Original.dll");
        if (hOrigDll) {
            pOrigAgReadMem = (P_AgReadMem)GetProcAddress(hOrigDll, "AgReadMem");
        }
        InitIPCOutput();
        break;
    case DLL_PROCESS_DETACH:
        if (hOrigDll) FreeLibrary(hOrigDll);
        if (clientSocket != INVALID_SOCKET) {
            closesocket(clientSocket);
            WSACleanup();
        }
        break;
    }
    return TRUE;
}
