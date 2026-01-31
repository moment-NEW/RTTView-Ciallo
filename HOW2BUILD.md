```
pyinstaller --noconfirm --onedir --windowed `
    --add-data "libusb-1.0.24;libusb-1.0.24" `
    --icon "Image/serial.ico" `
    --name "RTTView" `
    RTTView.py
```