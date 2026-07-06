const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('InfiniteCanvasDesktop', {
  isDesktopClient: true,
  checkClientUpdate: () => ipcRenderer.invoke('client-update:check'),
});
